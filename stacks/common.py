# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared helpers for the acxdmem stacks.

Pattern carried over from the AI Agents sample's stacks: each Lambda gets its own role and
its own log group, created before the function and passed in through ``log_group``. That
replaces ``AWSLambdaBasicExecutionRole`` (whose logs statement is ``Resource: *``) and avoids
the Role -> LogGroup -> Function -> Role dependency cycle.
"""

from __future__ import annotations

from pathlib import Path

import cdk_nag
from aws_cdk import Duration, RemovalPolicy, aws_iam as iam, aws_lambda as _lambda
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_logs as logs
from constructs import Construct

PREFIX = "acxdmem"
REPO_ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = REPO_ROOT / "dist"

PYTHON_RUNTIME = _lambda.Runtime.PYTHON_3_13
NODE_RUNTIME = _lambda.Runtime.NODEJS_22_X


def bundle_dir(name: str) -> Path:
    """Lambda bundle produced by ``scripts/bundle_lambdas.sh`` (``make bundle``)."""
    path = DIST_DIR / name
    if not path.exists():
        raise RuntimeError(
            f"Lambda bundle not found at {path}. Run `make bundle` "
            "(scripts/bundle_lambdas.sh) before `cdk synth` or `cdk deploy`."
        )
    return path


def memory_id_from_arn(memory_resource_arn: str) -> str:
    """Bare memory id from ``arn:aws:bedrock-agentcore:<region>:<account>:memory/<id>``."""
    return memory_resource_arn.rsplit("/", 1)[-1]


def lambda_function(
    scope: Construct,
    construct_id: str,
    *,
    prefix: str,
    name: str,
    bundle: str,
    description: str,
    timeout: Duration,
    environment: dict[str, str],
    runtime: _lambda.Runtime = PYTHON_RUNTIME,
    handler: str = "lambda_function.lambda_handler",
    memory_size: int = 256,
) -> tuple[_lambda.Function, iam.Role]:
    """Function + dedicated role + dedicated log group (one month retention)."""
    role = iam.Role(
        scope,
        f"{construct_id}Role",
        assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        description=f"{prefix}-{name} Lambda execution role",
    )
    log_group = logs.LogGroup(
        scope,
        f"{construct_id}LogGroup",
        log_group_name=f"/aws/lambda/{prefix}-{name}",
        retention=logs.RetentionDays.ONE_MONTH,
        removal_policy=RemovalPolicy.DESTROY,
    )
    log_group.grant_write(role)
    fn = _lambda.Function(
        scope,
        construct_id,
        function_name=f"{prefix}-{name}",
        description=description,
        runtime=runtime,
        handler=handler,
        code=_lambda.Code.from_asset(str(bundle_dir(bundle))),
        role=role,
        timeout=timeout,
        memory_size=memory_size,
        log_group=log_group,
        # Structured JSON application logs: every line carries the Lambda requestId. EMF lines
        # are printed (Python print / stdout), which Lambda passes through unchanged.
        logging_format=_lambda.LoggingFormat.JSON,
        environment=environment,
        architecture=_lambda.Architecture.X86_64,
    )
    cdk_nag.NagSuppressions.add_resource_suppressions(
        fn,
        [
            cdk_nag.NagPackSuppression(
                id="AwsSolutions-L1",
                reason=(
                    "Runtime is pinned on purpose (Python 3.13 / Node.js 22). The Python "
                    "bundles vendor boto3/botocore for this runtime; moving the runtime "
                    "means re-validating the vendored SDKs."
                ),
            )
        ],
    )
    return fn, role


def errors_alarm(
    scope: Construct, construct_id: str, *, prefix: str, name: str, fn: _lambda.IFunction
) -> cloudwatch.Alarm:
    """Lambda ``Errors`` > 0 in 5 minutes. No actions: wire them to your on-call topic."""
    return cloudwatch.Alarm(
        scope,
        construct_id,
        alarm_name=f"{prefix}-{name}-errors",
        alarm_description=f"{prefix}-{name} Lambda errors",
        metric=fn.metric_errors(period=Duration.minutes(5)),
        threshold=0,
        comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        evaluation_periods=1,
        treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
    )
