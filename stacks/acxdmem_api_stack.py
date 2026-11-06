# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""API stack: REST API with one Lambda REQUEST authorizer per route, read + ingest Lambdas.

Design §4, §6, §8. Two routes, called by ACXD data requests over public HTTPS:

- ``POST /memory/read``  -> read Lambda (``RetrieveMemoryRecords``), always ``200``.
- ``POST /memory/write`` -> ingest Lambda (validate, enqueue), ``202``.

Security rules (design §6):

- No Lambda Function URLs and no wildcard principal anywhere. Every Lambda permission names
  ``apigateway.amazonaws.com`` with a ``SourceArn`` on this API: the route integrations are
  scoped to ``<api>/<stage>/POST/<path>``, the authorizers to ``<api>/authorizers/<id>``.
- An authorizer on every method, from the first deploy. Each route has its own authorizer
  function bound to its own secret, and the authorizer's Allow is scoped to that route's
  ``methodArn``. Cache 300 s, identity source = the secret header.
- Request models with ``additionalProperties: false`` and per-field ``maxLength`` (write body
  is IDs only), enforced by a request validator. ACXD "Send context" on the write would add
  ``nlx_context``; the model rejects it on the AWS side too.
- Stage throttling, JSON access logs (ids, status, latency, caller IP, TLS version; no
  headers, body or validation detail), execution
  logging at ``ERROR`` with data trace off, X-Ray off. Optional AWS WAF rate-based rule
  (``enableWaf``).
- TLS 1.2 minimum on the default ``execute-api`` endpoint: enhanced security policy (TLS 1.3 +
  TLS 1.2 with forward-secret AES-GCM suites only) with ``STRICT`` endpoint access mode. A
  regional REST API otherwise defaults to ``TLS_1_0``.
- Alarms (no actions attached): Lambda errors per function, stage 5XX and 4XX rate.
"""

from __future__ import annotations

import cdk_nag
from aws_cdk import (
    Aws,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigateway as apigw,
    aws_cloudwatch as cloudwatch,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_secretsmanager as secretsmanager,
    aws_sqs as sqs,
    aws_wafv2 as wafv2,
)
from constructs import Construct

from stacks.common import errors_alarm, lambda_function, memory_id_from_arn

STAGE_NAME = "v1"
# Enhanced REST API security policy: TLS 1.3 and TLS 1.2 (ECDHE + AES-GCM only). RestApi (L2)
# has no securityPolicy / endpointAccessMode props, so both are set on the L1 CfnRestApi,
# which takes the CloudFormation strings. apigw.SecurityPolicy.TLS13_1_2_PFS_PQ_2025_09 does
# exist, but in Python its .value is the member name ("TLS13_1_2_PFS_PQ_2025_09"), not the
# CloudFormation value, so the string is spelled out (asserted in tests/test_stacks.py).
# API Gateway requires an endpoint access mode with any SecurityPolicy_* policy; STRICT also
# enforces SNI host matching.
API_SECURITY_POLICY = "SecurityPolicy_TLS13_1_2_PFS_PQ_2025_09"
API_ENDPOINT_ACCESS_MODE = "STRICT"
# Common starting point for an API Gateway 4XXError alarm: average > 5 % for 5 minutes.
CLIENT_ERROR_RATE = 0.05
# Header name, not a secret value.
SECRET_HEADER = "x-acxdmem-secret"  # nosec B105
AUTHORIZER_CACHE = Duration.seconds(300)
READ_LAMBDA_TIMEOUT = Duration.seconds(4)  # design §4
READ_BUDGET_SECONDS = "1.5"  # per namespace, parallel (design §4)
# Customer-facing path (authorizer + read/ingest): more memory means more CPU for the boto3
# import and TLS setup. The cost difference is negligible at two calls per conversation.
API_PATH_MEMORY_MB = 1024

PROFILE_ID_SCHEMA = apigw.JsonSchema(type=apigw.JsonSchemaType.STRING, max_length=64)
CONVERSATION_ID_SCHEMA = apigw.JsonSchema(
    type=apigw.JsonSchemaType.STRING, min_length=1, max_length=100
)


class AcxdmemApiStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        prefix: str,
        memory_resource_arn: str,
        work_queue: sqs.IQueue,
        enable_waf: bool = False,
        throttle_rate_limit: int = 50,
        throttle_burst_limit: int = 100,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- Endpoint secrets, one per route (generated, never in source) -------------
        # No fixed secret_name: CloudFormation generates a unique name per creation. On
        # stack deletion Secrets Manager only schedules the secret for deletion, and a fixed
        # name could not be created again until that recovery window ends, so a destroy
        # followed by a deploy would fail. Everything refers to the secrets by ARN (stack
        # outputs EndpointSecretReadArn / EndpointSecretWriteArn).
        secrets = {
            route: secretsmanager.Secret(
                self,
                f"EndpointSecret{route.capitalize()}",
                description=f"{prefix}: x-acxdmem-secret value for POST /memory/{route}. "
                "Store it as an ACXD Secret referenced by the data request header.",
                generate_secret_string=secretsmanager.SecretStringGenerator(
                    password_length=48, exclude_punctuation=True
                ),
                removal_policy=RemovalPolicy.DESTROY,
            )
            for route in ("read", "write")
        }

        # --- Lambdas --------------------------------------------------------------------
        read_fn, read_role = lambda_function(
            self,
            "ReadFunction",
            prefix=prefix,
            name="read",
            bundle="read",
            description="acxdmem: POST /memory/read -> RetrieveMemoryRecords",
            timeout=READ_LAMBDA_TIMEOUT,
            memory_size=API_PATH_MEMORY_MB,
            environment={
                "MEMORY_ID": memory_id_from_arn(memory_resource_arn),
                "READ_BUDGET_SECONDS": READ_BUDGET_SECONDS,
            },
        )
        read_role.add_to_policy(
            iam.PolicyStatement(
                sid="MemoryRead",
                actions=["bedrock-agentcore:RetrieveMemoryRecords"],
                resources=[memory_resource_arn],
            )
        )

        ingest_fn, ingest_role = lambda_function(
            self,
            "IngestFunction",
            prefix=prefix,
            name="ingest",
            bundle="ingest",
            description="acxdmem: POST /memory/write -> validate, enqueue",
            timeout=Duration.seconds(3),
            memory_size=API_PATH_MEMORY_MB,
            environment={"QUEUE_URL": work_queue.queue_url},
        )
        work_queue.grant_send_messages(ingest_role)

        authorizer_fns: dict[str, _lambda.Function] = {}
        for route, secret in secrets.items():
            fn, role = lambda_function(
                self,
                f"Authorizer{route.capitalize()}Function",
                prefix=prefix,
                name=f"authorizer-{route}",
                bundle="authorizer",
                description=f"acxdmem: REQUEST authorizer for POST /memory/{route}",
                timeout=Duration.seconds(3),
                memory_size=API_PATH_MEMORY_MB,
                environment={
                    "SECRET_ARN": secret.secret_arn,
                    "EXPECTED_ROUTE": f"POST/memory/{route}",
                    # Cache TTL in seconds, not a secret value.
                    "SECRET_CACHE_SECONDS": "300",  # nosec B105
                },
            )
            secret.grant_read(role)
            authorizer_fns[route] = fn

        # --- REST API -------------------------------------------------------------------
        access_logs = logs.LogGroup(
            self,
            "ApiAccessLogs",
            log_group_name=f"/aws/apigateway/{prefix}-api-access",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        api = apigw.RestApi(
            self,
            "Api",
            rest_api_name=f"{prefix}-api",
            description="acxdmem: ACXD data request endpoints for AgentCore Memory",
            endpoint_types=[apigw.EndpointType.REGIONAL],
            cloud_watch_role=False,  # account-level setting; checked by scripts/preflight.py
            deploy_options=apigw.StageOptions(
                stage_name=STAGE_NAME,
                throttling_rate_limit=throttle_rate_limit,
                throttling_burst_limit=throttle_burst_limit,
                logging_level=apigw.MethodLoggingLevel.ERROR,
                data_trace_enabled=False,
                metrics_enabled=True,
                tracing_enabled=False,
                access_log_destination=apigw.LogGroupLogDestination(access_logs),
                access_log_format=apigw.AccessLogFormat.custom(
                    '{"requestId":"$context.requestId",'
                    '"extendedRequestId":"$context.extendedRequestId",'
                    '"requestTime":"$context.requestTimeEpoch",'
                    '"httpMethod":"$context.httpMethod",'
                    '"resourcePath":"$context.resourcePath",'
                    '"status":"$context.status",'
                    '"responseLength":"$context.responseLength",'
                    '"responseLatency":"$context.responseLatency",'
                    '"integrationLatency":"$context.integrationLatency",'
                    '"authorizerStatus":"$context.authorizer.status",'
                    '"authorizerLatency":"$context.authorizer.latency",'
                    # Gateway response message only ("Invalid request body"), not
                    # validationErrorString, which can echo rejected body values.
                    '"errorMessage":"$context.error.message",'
                    '"errorType":"$context.error.responseType",'
                    # The caller's address (ACXD egress, not the customer) to act on the
                    # 4XX-rate alarm.
                    '"sourceIp":"$context.identity.sourceIp",'
                    '"tlsVersion":"$context.tlsVersion"}'
                ),
            ),
        )
        self.api = api
        cfn_api: apigw.CfnRestApi = api.node.default_child  # type: ignore[assignment]
        cfn_api.security_policy = API_SECURITY_POLICY
        cfn_api.endpoint_access_mode = API_ENDPOINT_ACCESS_MODE

        validator = apigw.RequestValidator(
            self,
            "BodyValidator",
            rest_api=api,
            request_validator_name=f"{prefix}-body",
            validate_request_body=True,
            validate_request_parameters=True,
        )
        models = {
            "read": api.add_model(
                "ReadRequestModel",
                content_type="application/json",
                model_name="AcxdmemReadRequest",
                schema=apigw.JsonSchema(
                    schema=apigw.JsonSchemaVersion.DRAFT4,
                    type=apigw.JsonSchemaType.OBJECT,
                    properties={"profileId": PROFILE_ID_SCHEMA},
                    required=["profileId"],
                    additional_properties=False,
                ),
            ),
            "write": api.add_model(
                "WriteRequestModel",
                content_type="application/json",
                model_name="AcxdmemWriteRequest",
                schema=apigw.JsonSchema(
                    schema=apigw.JsonSchemaVersion.DRAFT4,
                    type=apigw.JsonSchemaType.OBJECT,
                    properties={
                        "profileId": PROFILE_ID_SCHEMA,
                        "conversationId": CONVERSATION_ID_SCHEMA,
                    },
                    required=["profileId", "conversationId"],
                    additional_properties=False,
                ),
            ),
        }

        memory_resource = api.root.add_resource("memory")
        handlers = {"read": read_fn, "write": ingest_fn}
        for route, handler_fn in handlers.items():
            authorizer = apigw.RequestAuthorizer(
                self,
                f"Authorizer{route.capitalize()}",
                authorizer_name=f"{prefix}-{route}",
                handler=authorizer_fns[route],
                identity_sources=[apigw.IdentitySource.header(SECRET_HEADER)],
                results_cache_ttl=AUTHORIZER_CACHE,
            )
            resource = memory_resource.add_resource(route)
            resource.add_method(
                "POST",
                _lambda_proxy_integration(handler_fn),
                authorizer=authorizer,
                authorization_type=apigw.AuthorizationType.CUSTOM,
                request_validator=validator,
                request_models={"application/json": models[route]},
                request_parameters={f"method.request.header.{SECRET_HEADER}": True},
            )
            # Invoke permission scoped to this API, stage, method and path (no stage wildcard). Built by
            # hand because LambdaIntegration grants <api>/*/POST/<path> (stage wildcard).
            handler_fn.add_permission(
                f"ApiInvoke{route.capitalize()}",
                principal=iam.ServicePrincipal("apigateway.amazonaws.com"),
                action="lambda:InvokeFunction",
                source_arn=api.arn_for_execute_api("POST", f"/memory/{route}", STAGE_NAME),
                source_account=Aws.ACCOUNT_ID,
            )

        if enable_waf:
            web_acl = wafv2.CfnWebACL(
                self,
                "WebAcl",
                name=f"{prefix}-api",
                scope="REGIONAL",
                default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
                visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                    cloud_watch_metrics_enabled=True,
                    metric_name=f"{prefix}-api",
                    sampled_requests_enabled=False,
                ),
                rules=[
                    wafv2.CfnWebACL.RuleProperty(
                        name="rate-limit",
                        priority=0,
                        action=wafv2.CfnWebACL.RuleActionProperty(block={}),
                        statement=wafv2.CfnWebACL.StatementProperty(
                            rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                                aggregate_key_type="IP", limit=1000
                            )
                        ),
                        visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                            cloud_watch_metrics_enabled=True,
                            metric_name=f"{prefix}-rate-limit",
                            sampled_requests_enabled=False,
                        ),
                    )
                ],
            )
            wafv2.CfnWebACLAssociation(
                self,
                "WebAclAssociation",
                resource_arn=api.deployment_stage.stage_arn,
                web_acl_arn=web_acl.attr_arn,
            )

        # --- Alarms (no actions attached; wire them to your on-call topic) --------------
        for construct_id, name, fn in (
            ("ReadErrorsAlarm", "read", read_fn),
            ("IngestErrorsAlarm", "ingest", ingest_fn),
            ("AuthorizerReadErrorsAlarm", "authorizer-read", authorizer_fns["read"]),
            ("AuthorizerWriteErrorsAlarm", "authorizer-write", authorizer_fns["write"]),
        ):
            errors_alarm(self, construct_id, prefix=prefix, name=name, fn=fn)
        stage = api.deployment_stage
        cloudwatch.Alarm(
            self,
            "Api5xxAlarm",
            alarm_name=f"{prefix}-api-5xx",
            alarm_description=f"{prefix}-api: any 5XX response (stage {STAGE_NAME})",
            metric=stage.metric_server_error(period=Duration.minutes(5), statistic="Sum"),
            threshold=0,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        # ACXD never sends a request that should fail validation or authorization, so a
        # sustained 4XX rate means a wrong secret after rotation, a changed data request, or
        # someone probing the endpoint (AuthorizerDeny has the per-route breakdown).
        cloudwatch.Alarm(
            self,
            "Api4xxRateAlarm",
            alarm_name=f"{prefix}-api-4xx-rate",
            alarm_description=f"{prefix}-api: 4XX rate above 5 % for 5 minutes",
            metric=stage.metric_client_error(period=Duration.minutes(1), statistic="Average"),
            threshold=CLIENT_ERROR_RATE,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=5,
            datapoints_to_alarm=5,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )

        CfnOutput(self, "ReadUrl", value=api.url_for_path("/memory/read"))
        CfnOutput(self, "WriteUrl", value=api.url_for_path("/memory/write"))
        for route, secret in secrets.items():
            CfnOutput(
                self,
                f"EndpointSecret{route.capitalize()}Arn",
                value=secret.secret_arn,
                description=f"Secret for x-acxdmem-secret on /memory/{route} (value not output)",
            )

        # --- CDK Nag suppressions (justified) ------------------------------------------
        for secret in secrets.values():
            cdk_nag.NagSuppressions.add_resource_suppressions(
                secret,
                [
                    cdk_nag.NagPackSuppression(
                        id="AwsSolutions-SMG4",
                        reason="Rotated manually: every rotation needs the new value pasted "
                        "into the ACXD Secret. The authorizer accepts AWSCURRENT and "
                        "AWSPREVIOUS during the switch (runbook in the docs).",
                    )
                ],
            )
        api_suppressions = [
            cdk_nag.NagPackSuppression(
                id="AwsSolutions-COG4",
                reason="ACXD data requests send a static header and cannot run an OAuth or "
                "Cognito flow; every method uses a Lambda REQUEST authorizer instead.",
            ),
        ]
        if not enable_waf:
            api_suppressions.append(
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-APIG3",
                    reason="AWS WAF is opt-in (context enableWaf) because of its cost; the "
                    "stage is throttled and every route has its own authorizer.",
                )
            )
        # The next line matches gitleaks' generic-api-key rule ("api, api_..."); no secret.
        cdk_nag.NagSuppressions.add_resource_suppressions(
            api, api_suppressions, apply_to_children=True  # gitleaks:allow
        )


def _lambda_proxy_integration(fn: _lambda.IFunction) -> apigw.Integration:
    """AWS_PROXY integration without CDK's automatic (stage-wildcard) invoke permission."""
    stack = Stack.of(fn)
    return apigw.Integration(
        type=apigw.IntegrationType.AWS_PROXY,
        integration_http_method="POST",
        uri=(
            f"arn:{stack.partition}:apigateway:{stack.region}:lambda:path/2015-03-31/"
            f"functions/{fn.function_arn}/invocations"
        ),
    )
