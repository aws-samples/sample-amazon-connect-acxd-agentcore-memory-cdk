# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Writer stack: work queue + DLQ, writer Lambda (Python), fetch step (Node.js), dedup table.

Design §3.2-§3.4, §8. The AgentCore Memory resource and its execution role are created by
``scripts/provision_memory.sh``, outside this stack's lifecycle (customer data). The ACXD API key secret is created manually (``acxdApiKeySecretArn``).

Settings fixed by the design (§3.2):

- Work queue: SQS **standard** (per-message ``DelaySeconds`` is not available on FIFO),
  visibility timeout 180 s (6 x the writer timeout), retention 4 days (SQS default), DLQ after
  ``maxReceiveCount`` 3, alarm when the oldest message is older than 30 minutes.
- Writer Lambda timeout 30 s, batch size 1, concurrency capped with the event-source
  mapping ``maxConcurrency`` (not reserved concurrency, which throttles the poller and
  burns receive counts).
- The fetch step has no resource policy and no URL: only the writer role may invoke it.
"""

from __future__ import annotations

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_cloudwatch as cloudwatch,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda_event_sources as event_sources,
    aws_secretsmanager as secretsmanager,
    aws_sqs as sqs,
)
from constructs import Construct

from stacks.common import NODE_RUNTIME, errors_alarm, lambda_function, memory_id_from_arn

WRITER_TIMEOUT = Duration.seconds(30)
QUEUE_VISIBILITY = Duration.seconds(180)
# SQS default (4 days). The state machine never needs more than ~25 minutes per work item,
# but a writer outage (disabled event source, broken deploy, an API key rotation gap) must
# not expire work silently. Kept below the DLQ's 14 days: a message moved to the DLQ keeps
# its original enqueue time.
QUEUE_RETENTION = Duration.days(4)
MAX_RECEIVE_COUNT = 3
# Normal age of the oldest message stays under ~5 minutes (every re-queue is a new message
# with a delay of at most 300 s). 30 minutes means the writer is not consuming.
QUEUE_AGE_ALARM_SECONDS = 1800


class AcxdmemWriterStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        prefix: str,
        memory_resource_arn: str,
        acxd_workspace_id: str,
        acxd_application_id: str,
        acxd_api_key_secret_arn: str,
        acxd_region: str | None = None,
        enable_stripping: bool = False,
        writer_max_concurrency: int = 2,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        if not 2 <= writer_max_concurrency <= 5:
            raise ValueError("writerMaxConcurrency must be between 2 and 5 (design §3.2)")

        # --- Dedup table (segment rows + conversation watermark rows, design §3.4) -----
        self.dedup_table = dynamodb.Table(
            self,
            "DedupTable",
            table_name=f"{prefix}-dedup",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expires_at",
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            # RETAIN: losing the table would let redelivered work re-write old segments.
            removal_policy=RemovalPolicy.RETAIN,
        )

        # --- Queues ---------------------------------------------------------------------
        self.dlq = sqs.Queue(
            self,
            "WriterDlq",
            queue_name=f"{prefix}-write-dlq",
            retention_period=Duration.days(14),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
        )
        self.work_queue = sqs.Queue(
            self,
            "WorkQueue",
            queue_name=f"{prefix}-write-queue",
            visibility_timeout=QUEUE_VISIBILITY,
            retention_period=QUEUE_RETENTION,
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            dead_letter_queue=sqs.DeadLetterQueue(
                queue=self.dlq, max_receive_count=MAX_RECEIVE_COUNT
            ),
        )

        # --- Fetch step (Node.js, official ACXD SDK) ----------------------------------
        api_key_secret = secretsmanager.Secret.from_secret_complete_arn(
            self, "AcxdApiKeySecret", acxd_api_key_secret_arn
        )
        fetch_env = {
            "ACXD_API_KEY_SECRET_ARN": acxd_api_key_secret_arn,
            "ACXD_WORKSPACE_ID": acxd_workspace_id,
            "API_KEY_CACHE_SECONDS": "300",
        }
        if acxd_region:
            fetch_env["ACXD_REGION"] = acxd_region
        self.fetch_function, fetch_role = lambda_function(
            self,
            "FetchFunction",
            prefix=prefix,
            name="fetch",
            bundle="fetch",
            description="acxdmem: ACXD GetConversation via amazon-connect-acxd-sdk",
            runtime=NODE_RUNTIME,
            handler="index.handler",
            timeout=Duration.seconds(10),
            environment=fetch_env,
        )
        api_key_secret.grant_read(fetch_role)

        # --- Writer (Python) -----------------------------------------------------------
        self.writer_function, writer_role = lambda_function(
            self,
            "WriterFunction",
            prefix=prefix,
            name="writer",
            bundle="writer",
            description="acxdmem: stable transcript -> AgentCore Memory CreateEvent",
            timeout=WRITER_TIMEOUT,
            environment={
                "MEMORY_ID": memory_id_from_arn(memory_resource_arn),
                "DEDUP_TABLE_NAME": self.dedup_table.table_name,
                "QUEUE_URL": self.work_queue.queue_url,
                "FETCH_FUNCTION_NAME": self.fetch_function.function_name,
                "ACXD_APPLICATION_ID": acxd_application_id,
                "ENABLE_STRIPPING": "true" if enable_stripping else "false",
            },
        )
        memory_actions = ["bedrock-agentcore:CreateEvent"]
        if enable_stripping:
            # Design §4.2 / §8: only when the stripping backstop is on.
            memory_actions.append("bedrock-agentcore:RetrieveMemoryRecords")
        writer_role.add_to_policy(
            iam.PolicyStatement(
                sid="MemoryWrite",
                actions=memory_actions,
                resources=[memory_resource_arn],
            )
        )
        self.dedup_table.grant(
            writer_role, "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"
        )
        # Self re-queue (state machine) - SendMessage only, on the work queue only.
        self.work_queue.grant_send_messages(writer_role)
        # Exact function ARN only (grant_invoke would add <arn>:* for every version/alias).
        writer_role.add_to_policy(
            iam.PolicyStatement(
                sid="InvokeFetchStep",
                actions=["lambda:InvokeFunction"],
                resources=[self.fetch_function.function_arn],
            )
        )

        self.writer_function.add_event_source(
            event_sources.SqsEventSource(
                self.work_queue,
                batch_size=1,
                max_concurrency=writer_max_concurrency,
            )
        )

        # --- Alarms (no actions attached; wire them to your on-call topic) --------------
        cloudwatch.Alarm(
            self,
            "DlqNotEmptyAlarm",
            alarm_name=f"{prefix}-write-dlq-not-empty",
            alarm_description="acxdmem writer hard failures reached the DLQ",
            metric=self.dlq.metric_approximate_number_of_messages_visible(
                period=Duration.minutes(5)
            ),
            threshold=0,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        cloudwatch.Alarm(
            self,
            "WriterErrorsAlarm",
            alarm_name=f"{prefix}-writer-errors",
            alarm_description="acxdmem writer Lambda errors",
            metric=self.writer_function.metric_errors(period=Duration.minutes(5)),
            threshold=0,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        errors_alarm(
            self, "FetchErrorsAlarm", prefix=prefix, name="fetch", fn=self.fetch_function
        )
        # Work that is not consumed never reaches the DLQ, so the DLQ alarm cannot see a
        # stalled writer; the age of the oldest message can.
        cloudwatch.Alarm(
            self,
            "WorkQueueAgeAlarm",
            alarm_name=f"{prefix}-write-queue-age",
            alarm_description=f"{prefix} work queue: oldest message older than "
            f"{QUEUE_AGE_ALARM_SECONDS} s (writer not consuming)",
            metric=self.work_queue.metric_approximate_age_of_oldest_message(
                period=Duration.minutes(5), statistic="Maximum"
            ),
            threshold=QUEUE_AGE_ALARM_SECONDS,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )

        CfnOutput(self, "WorkQueueUrl", value=self.work_queue.queue_url)
        CfnOutput(self, "WriterDlqUrl", value=self.dlq.queue_url)
        CfnOutput(self, "DedupTableName", value=self.dedup_table.table_name)
