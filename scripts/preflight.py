#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Read-only pre-deploy checks for the acxdmem stacks (pattern from the AI Agents sample).

Run before ``cdk deploy``. Each check prints PASS/FAIL with an actionable message; the
script exits non-zero if any check fails. It only calls read/describe APIs and never reads
a secret value.

Checks:
    1. AgentCore Memory resource exists, is ACTIVE, and every strategy is ACTIVE
    2. ACXD API key secret exists (DescribeSecret only)
    3. API Gateway account has a CloudWatch Logs role (needed for access and ERROR logs;
       the stack sets ``cloudWatchRole: false`` so it never changes this account setting)
    4. CDK bootstrap stack present
    5. No ``<prefix>-dedup`` table left behind by an earlier ``cdk destroy`` (the table is
       RETAIN with a fixed name, so a new writer stack could not create it)

Usage:
    python3 scripts/preflight.py [--profile PROFILE] [--region REGION] [-c key=value ...]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError, ProfileNotFound

REPO_ROOT = Path(__file__).resolve().parent.parent


class CheckResult:
    def __init__(self, name: str, passed: bool, message: str) -> None:
        self.name = name
        self.passed = passed
        self.message = message

    def print(self) -> None:
        print(f"[{'PASS' if self.passed else 'FAIL'}] {self.name}: {self.message}")


def load_context(overrides: list[str] | None = None) -> dict[str, Any]:
    """cdk.json context, then ``key=value`` overrides (same as ``cdk deploy -c key=value``)."""
    cdk_json = REPO_ROOT / "cdk.json"
    context = dict(json.loads(cdk_json.read_text()).get("context", {}))
    for item in overrides or []:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise ValueError(f"context override must be key=value, got {item!r}")
        context[key] = value
    return context


def _context_value(context: dict[str, Any], key: str) -> str | None:
    value = context.get(key)
    if not value or str(value).startswith("REPLACE_WITH_"):
        return None
    return str(value)


def check_memory(control, memory_resource_arn: str | None) -> CheckResult:
    name = "AgentCore Memory resource is ACTIVE"
    if not memory_resource_arn:
        return CheckResult(name, False, "context 'memoryResourceArn' is not set")
    memory_id = memory_resource_arn.rsplit("/", 1)[-1]
    try:
        memory = control.get_memory(memoryId=memory_id).get("memory", {})
    except ClientError as exc:
        return CheckResult(name, False, f"get_memory failed for '{memory_id}': "
                           f"{exc.response['Error']['Code']}. Run scripts/provision_memory.sh first.")
    if memory.get("status") != "ACTIVE":
        return CheckResult(name, False, f"memory status is '{memory.get('status')}'")
    statuses = [s.get("status") for s in memory.get("strategies", [])]
    if not statuses or any(s != "ACTIVE" for s in statuses):
        return CheckResult(name, False, f"strategy statuses {statuses}")
    return CheckResult(name, True, f"'{memory_id}' ACTIVE with {len(statuses)} strategies")


def check_secret(secrets, secret_arn: str | None) -> CheckResult:
    name = "ACXD API key secret exists"
    if not secret_arn:
        return CheckResult(name, False, "context 'acxdApiKeySecretArn' is not set")
    try:
        secrets.describe_secret(SecretId=secret_arn)
    except ClientError as exc:
        return CheckResult(name, False, f"describe_secret failed: {exc.response['Error']['Code']}")
    return CheckResult(name, True, "found (value not read)")


def check_apigw_logging_role(apigw) -> CheckResult:
    name = "API Gateway CloudWatch Logs role set"
    try:
        arn = apigw.get_account().get("cloudwatchRoleArn")
    except ClientError as exc:
        return CheckResult(name, False, f"get_account failed: {exc.response['Error']['Code']}")
    if not arn:
        return CheckResult(
            name, False,
            "no account-level CloudWatch role; access and ERROR logging would fail the deploy. "
            "See https://docs.aws.amazon.com/apigateway/latest/developerguide/set-up-logging.html",
        )
    return CheckResult(name, True, arn)


def check_cdk_bootstrap(cfn) -> CheckResult:
    name = "CDK bootstrap present"
    try:
        stacks = cfn.describe_stacks(StackName="CDKToolkit").get("Stacks", [])
    except ClientError:
        return CheckResult(name, False, "stack 'CDKToolkit' not found; run `cdk bootstrap`")
    status = stacks[0].get("StackStatus") if stacks else "missing"
    if status not in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
        return CheckResult(name, False, f"CDKToolkit status is '{status}'")
    return CheckResult(name, True, status)


def _stack_exists(cfn, stack_name: str) -> bool:
    try:
        stacks = cfn.describe_stacks(StackName=stack_name).get("Stacks", [])
    except ClientError as exc:
        if "does not exist" in exc.response["Error"].get("Message", ""):
            return False
        raise
    return any(st.get("StackStatus") != "DELETE_COMPLETE" for st in stacks)


def check_retained_dedup_table(dynamodb, cfn, prefix: str) -> CheckResult:
    name = "No dedup table left from an earlier destroy"
    table, stack = f"{prefix}-dedup", f"{prefix}-writer"
    try:
        if _stack_exists(cfn, stack):
            return CheckResult(name, True, f"stack '{stack}' exists (update, not a new create)")
        dynamodb.describe_table(TableName=table)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ResourceNotFoundException":
            return CheckResult(name, True, f"'{table}' does not exist yet")
        return CheckResult(name, False, f"check failed: {exc.response['Error']['Code']}")
    return CheckResult(
        name, False,
        f"table '{table}' exists but stack '{stack}' does not: it was kept by an earlier "
        f"`cdk destroy` (RETAIN) and would make the writer stack fail with 'already exists'. "
        f"Delete it first: aws dynamodb delete-table --table-name {table} "
        "(it holds dedup state only; see docs/deployment-guide.md, Redeploying after a teardown)",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default=None)
    parser.add_argument("--region", default=None)
    parser.add_argument("-c", "--context", action="append", default=[], metavar="KEY=VALUE",
                        help="override a cdk.json context value, as with cdk deploy -c")
    args = parser.parse_args()

    try:
        context = load_context(args.context)
    except ValueError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    region = args.region or _context_value(context, "region")
    try:
        session = boto3.Session(profile_name=args.profile, region_name=region)
    except ProfileNotFound as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2

    results = [
        check_memory(session.client("bedrock-agentcore-control"),
                     _context_value(context, "memoryResourceArn")),
        check_secret(session.client("secretsmanager"),
                     _context_value(context, "acxdApiKeySecretArn")),
        check_apigw_logging_role(session.client("apigateway")),
        check_cdk_bootstrap(session.client("cloudformation")),
        check_retained_dedup_table(session.client("dynamodb"), session.client("cloudformation"),
                                   _context_value(context, "resourcePrefix") or "acxdmem"),
    ]
    for result in results:
        result.print()
    failed = [r for r in results if not r.passed]
    if failed:
        print(f"{len(failed)}/{len(results)} check(s) failed; fix them before `cdk deploy`.")
        return 1
    print(f"All {len(results)} checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
