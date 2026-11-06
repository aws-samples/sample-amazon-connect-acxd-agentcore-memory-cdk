#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CDK app entry point for the ACXD x AgentCore Memory sample.

Configuration comes from ``cdk.json`` context (or ``-c key=value``), never from source. The
validation here is offline (string shapes only, no AWS calls), so ``cdk synth`` stays
credential-free; live checks are in ``scripts/preflight.py``. Pattern carried over from the
AI Agents sample's ``app.py``: a missing key and an un-overridden ``REPLACE_WITH_...``
placeholder both fail synth with the same message.

Required context keys:
    memoryResourceArn     AgentCore Memory resource ARN (scripts/provision_memory.sh)
    acxdWorkspaceId       ACXD workspace id (UUID)
    acxdApplicationId     ACXD application id (UUID); the writer only writes its conversations
    acxdApiKeySecretArn   complete ARN of the Secrets Manager secret holding the API key of a
                          workspace-scoped read-only ACXD programmatic user (created manually)

Optional context keys:
    accountId / region    target account and region (default: CDK_DEFAULT_*)
    resourcePrefix        resource name prefix, must start with "acxdmem" (default "acxdmem")
    enableWaf             "true" adds an AWS WAF web ACL with a rate-based rule (default false)
    enableStripping       "true" turns on writer-side restatement stripping (default false)
    writerMaxConcurrency  event-source maxConcurrency for the writer, 2-5 (default 2)

Synth with placeholders (CI, ``make synth-check``): pass ``-c allowPlaceholders=true`` to
substitute well-formed dummy values for the required keys. The result is not deployable.
"""

from __future__ import annotations

import os
import re

import aws_cdk as cdk
import cdk_nag

from stacks.acxdmem_api_stack import AcxdmemApiStack
from stacks.acxdmem_writer_stack import AcxdmemWriterStack

_PLACEHOLDER_PREFIX = "REPLACE_WITH_"

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_MEMORY_ARN = re.compile(r"^arn:aws[a-zA-Z-]*:bedrock-agentcore:[a-z0-9-]+:\d{12}:memory/\S+$")
_SECRET_ARN = re.compile(
    r"^arn:aws[a-zA-Z-]*:secretsmanager:[a-z0-9-]+:\d{12}:secret:[A-Za-z0-9/_+=.@-]+-[A-Za-z0-9]{6}$"
)
_ACCOUNT_ID = re.compile(r"^\d{12}$")
_PREFIX = re.compile(r"^acxdmem[a-z0-9-]{0,20}$")

# Well-formed dummies for placeholder synth only (allowPlaceholders=true).
_DUMMIES = {
    "memoryResourceArn": "arn:aws:bedrock-agentcore:eu-central-1:111111111111:memory/acxdmem_placeholder-AbCdEf1234",
    "acxdWorkspaceId": "00000000-0000-4000-8000-000000000000",
    "acxdApplicationId": "00000000-0000-4000-8000-000000000001",
    "acxdApiKeySecretArn": "arn:aws:secretsmanager:eu-central-1:111111111111:secret:acxdmem-acxd-api-key-AbCdEf",
}


class ConfigError(ValueError):
    pass


def _is_unset(value: object) -> bool:
    return value is None or value == "" or str(value).startswith(_PLACEHOLDER_PREFIX)


def _flag(value: object, key: str) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or str(value).lower() in ("", "false", "0", "no"):
        return False
    if str(value).lower() in ("true", "1", "yes"):
        return True
    raise ConfigError(f"CDK context key '{key}' must be true or false, got {value!r}.")


def load_config(context) -> dict:
    """Validate context and return the stack parameters. ``context`` is ``try_get_context``."""
    allow_placeholders = _flag(context("allowPlaceholders"), "allowPlaceholders")

    def require(key: str, pattern: re.Pattern[str], description: str) -> str:
        value = context(key)
        if _is_unset(value):
            if allow_placeholders:
                return _DUMMIES[key]
            raise ConfigError(
                f"Missing or unconfigured required CDK context key '{key}' (value: {value!r}). "
                f"Set it in cdk.json or pass -c {key}=<value>. See README.md."
            )
        value = str(value)
        if not pattern.match(value):
            raise ConfigError(
                f"CDK context key '{key}' is set to {value!r}, which does not look like "
                f"{description}."
            )
        return value

    memory_arn = require("memoryResourceArn", _MEMORY_ARN,
                         "an AgentCore Memory ARN (arn:aws:bedrock-agentcore:<region>:<account>:memory/<id>)")
    workspace_id = require("acxdWorkspaceId", _UUID, "an ACXD workspace id (UUID)")
    application_id = require("acxdApplicationId", _UUID, "an ACXD application id (UUID)")
    secret_arn = require("acxdApiKeySecretArn", _SECRET_ARN,
                         "a complete Secrets Manager secret ARN (ending in -XXXXXX)")

    prefix = context("resourcePrefix") or "acxdmem"
    if not _PREFIX.match(str(prefix)):
        raise ConfigError(
            f"CDK context key 'resourcePrefix' is {prefix!r}; it must start with 'acxdmem' "
            "and use lowercase letters, digits and hyphens (max 27 characters)."
        )

    account = context("accountId")
    account = None if _is_unset(account) else str(account)
    if account and not _ACCOUNT_ID.match(account):
        raise ConfigError(f"CDK context key 'accountId' is {account!r}, not a 12-digit account id.")
    region = context("region")
    region = None if _is_unset(region) else str(region)

    concurrency_raw = context("writerMaxConcurrency")
    try:
        concurrency = int(concurrency_raw) if concurrency_raw not in (None, "") else 2
    except (TypeError, ValueError) as exc:
        raise ConfigError("CDK context key 'writerMaxConcurrency' must be an integer 2-5.") from exc
    if not 2 <= concurrency <= 5:
        raise ConfigError("CDK context key 'writerMaxConcurrency' must be an integer 2-5.")

    return {
        "prefix": str(prefix),
        "memory_resource_arn": memory_arn,
        "acxd_workspace_id": workspace_id,
        "acxd_application_id": application_id,
        "acxd_api_key_secret_arn": secret_arn,
        "enable_waf": _flag(context("enableWaf"), "enableWaf"),
        "enable_stripping": _flag(context("enableStripping"), "enableStripping"),
        "writer_max_concurrency": concurrency,
        # Placeholder synth never picks up the caller's real account from the environment.
        "account": account or ("111111111111" if allow_placeholders else os.getenv("CDK_DEFAULT_ACCOUNT")),
        "region": region or ("eu-central-1" if allow_placeholders else os.getenv("CDK_DEFAULT_REGION")),
    }


def build_app(app: cdk.App) -> tuple[AcxdmemWriterStack, AcxdmemApiStack]:
    cfg = load_config(app.node.try_get_context)
    env = (
        cdk.Environment(account=cfg["account"], region=cfg["region"])
        if cfg["account"] and cfg["region"]
        else None
    )
    prefix = cfg["prefix"]
    writer = AcxdmemWriterStack(
        app,
        f"{prefix}-writer",
        prefix=prefix,
        memory_resource_arn=cfg["memory_resource_arn"],
        acxd_workspace_id=cfg["acxd_workspace_id"],
        acxd_application_id=cfg["acxd_application_id"],
        acxd_api_key_secret_arn=cfg["acxd_api_key_secret_arn"],
        enable_stripping=cfg["enable_stripping"],
        writer_max_concurrency=cfg["writer_max_concurrency"],
        env=env,
        description="acxdmem: ACXD x AgentCore Memory writer (queue, fetch step, writer, dedup)",
    )
    api = AcxdmemApiStack(
        app,
        f"{prefix}-api",
        prefix=prefix,
        memory_resource_arn=cfg["memory_resource_arn"],
        work_queue=writer.work_queue,
        enable_waf=cfg["enable_waf"],
        env=env,
        description="acxdmem: ACXD x AgentCore Memory API (authorizers, read, ingest)",
    )
    for stack in (writer, api):
        cdk.Tags.of(stack).add("project", prefix)
    cdk.Aspects.of(app).add(cdk_nag.AwsSolutionsChecks())
    return writer, api


if __name__ == "__main__":
    _app = cdk.App()
    build_app(_app)
    _app.synth()
