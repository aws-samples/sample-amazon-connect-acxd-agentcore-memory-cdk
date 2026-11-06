# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from botocore.stub import Stubber

from tests.helpers import client

_spec = importlib.util.spec_from_file_location(
    "acxdmem_preflight", Path(__file__).resolve().parent.parent / "scripts" / "preflight.py"
)
preflight = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(preflight)

ARN = "arn:aws:bedrock-agentcore:eu-central-1:123456789012:memory/acxdmem_test-AbCdEf1234"


def _memory(status="ACTIVE", strategies=("ACTIVE", "ACTIVE")):
    from datetime import datetime, timezone
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return {"memory": {
        "arn": ARN, "id": "acxdmem_test-AbCdEf1234", "name": "acxdmem_test", "eventExpiryDuration": 90,
        "status": status, "createdAt": now, "updatedAt": now,
        "strategies": [{"strategyId": f"s{i}-0000000000", "name": f"s{i}", "type": "SEMANTIC",
                        "namespaces": ["n"], "namespaceTemplates": ["n"], "status": s} for i, s in enumerate(strategies)],
    }}


@pytest.mark.parametrize("response, ok", [
    (_memory(), True),
    (_memory(status="CREATING"), False),
    (_memory(strategies=("ACTIVE", "CREATING")), False),
])
def test_check_memory(response, ok):
    c = client("bedrock-agentcore-control")
    with Stubber(c) as stub:
        stub.add_response("get_memory", response, {"memoryId": "acxdmem_test-AbCdEf1234"})
        assert preflight.check_memory(c, ARN).passed is ok


def test_check_memory_missing_context():
    assert not preflight.check_memory(None, None).passed


def test_check_apigw_logging_role():
    c = client("apigateway")
    with Stubber(c) as stub:
        stub.add_response("get_account", {})
        stub.add_response("get_account", {"cloudwatchRoleArn": "arn:aws:iam::123456789012:role/x"})
        assert not preflight.check_apigw_logging_role(c).passed
        assert preflight.check_apigw_logging_role(c).passed


def test_check_secret_does_not_read_value():
    c = client("secretsmanager")
    with Stubber(c) as stub:
        stub.add_response("describe_secret", {"Name": "x"}, {"SecretId": "arn:x"})
        assert preflight.check_secret(c, "arn:x").passed
        stub.assert_no_pending_responses()


def test_load_context_overrides():
    ctx = preflight.load_context(["memoryResourceArn=" + ARN, "region=eu-central-1"])
    assert ctx["memoryResourceArn"] == ARN
    assert ctx["region"] == "eu-central-1"
    assert ctx["resourcePrefix"] == "acxdmem"  # untouched cdk.json value


def test_load_context_rejects_malformed_override():
    with pytest.raises(ValueError):
        preflight.load_context(["noequals"])


def _stack(status="CREATE_COMPLETE"):
    from datetime import datetime, timezone
    return {"Stacks": [{"StackName": "acxdmem-writer", "StackStatus": status,
                        "CreationTime": datetime(2026, 1, 1, tzinfo=timezone.utc)}]}


def _no_stack(stub):
    stub.add_client_error("describe_stacks", "ValidationError",
                          "Stack with id acxdmem-writer does not exist",
                          expected_params={"StackName": "acxdmem-writer"})


def test_retained_dedup_table_without_writer_stack_fails():
    ddb, cfn = client("dynamodb"), client("cloudformation")
    with Stubber(ddb) as d, Stubber(cfn) as c:
        _no_stack(c)
        d.add_response("describe_table", {"Table": {"TableName": "acxdmem-dedup"}},
                       {"TableName": "acxdmem-dedup"})
        result = preflight.check_retained_dedup_table(ddb, cfn, "acxdmem")
    assert not result.passed
    assert "aws dynamodb delete-table --table-name acxdmem-dedup" in result.message


def test_no_table_without_stack_passes():
    ddb, cfn = client("dynamodb"), client("cloudformation")
    with Stubber(ddb) as d, Stubber(cfn) as c:
        _no_stack(c)
        d.add_client_error("describe_table", "ResourceNotFoundException")
        assert preflight.check_retained_dedup_table(ddb, cfn, "acxdmem").passed


def test_existing_writer_stack_passes_without_table_lookup():
    ddb, cfn = client("dynamodb"), client("cloudformation")
    with Stubber(ddb) as d, Stubber(cfn) as c:
        c.add_response("describe_stacks", _stack(), {"StackName": "acxdmem-writer"})
        assert preflight.check_retained_dedup_table(ddb, cfn, "acxdmem").passed
        d.assert_no_pending_responses()
