# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""scripts/verify_live_security.py finds the endpoint secrets by stack output, not by name."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

from botocore.stub import Stubber

from tests.helpers import client

_spec = importlib.util.spec_from_file_location(
    "acxdmem_verify_live_security",
    Path(__file__).resolve().parent.parent / "scripts" / "verify_live_security.py",
)
verify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify)

READ_ARN = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:EndpointSecretRead-AbCdEf"
WRITE_ARN = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:EndpointSecretWrite-AbCdEf"


def test_endpoint_secret_arns_from_outputs_of_prefixed_stack():
    cfn = client("cloudformation")
    with Stubber(cfn) as stub:
        stub.add_response("describe_stacks", {"Stacks": [{
            "StackName": "acxdmemtest-api", "StackStatus": "CREATE_COMPLETE",
            "CreationTime": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "Outputs": [
                {"OutputKey": "ReadUrl", "OutputValue": "https://example.invalid/v1/memory/read"},
                {"OutputKey": "EndpointSecretReadArn", "OutputValue": READ_ARN},
                {"OutputKey": "EndpointSecretWriteArn", "OutputValue": WRITE_ARN},
            ],
        }]}, {"StackName": "acxdmemtest-api"})
        assert verify.endpoint_secret_arns(cfn, "acxdmemtest-api") == {
            "read": READ_ARN, "write": WRITE_ARN}
