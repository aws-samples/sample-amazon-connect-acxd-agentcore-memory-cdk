#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Create or update the acxdmem live-test Connect Customer inbound flows.

Two flows, same shape as the shipped snippet (assets/acxd/connect/), plus a chat path:

    acxdmem-e2e-inbound  shipped shape: never reads $.AgenticCX.ContextVariables back
    acxdmem-e2e-probe    same, plus a TEST-ONLY copy of the returned memory context
                         variable into a contact attribute, to prove it comes back empty.
                         Synthetic data only; never ship this pattern.

Identity (design.md section 5, T9):
    voice: Customer Profiles lookup on _phone = $.CustomerEndpoint.Address (caller ANI)
    chat:  lookup on _email = $.Attributes.acxdmemVerifiedEmail. That attribute stands for an
           identity a trusted backend verified before starting the chat (server-side
           StartChatContact with IAM, or a JWT-secured communications widget). The live-test
           driver plays that backend. No attribute, or no unique profile: no-memory block.
    nlx_userId is only ever $.Customer.ProfileId.

Usage:
    python3 e2e/connect_flow.py --instance-id <id> --workspace-id <uuid> \
        --application-id <uuid> --alias <deployment alias> [--idle-seconds 60] [--apply]
"""

from __future__ import annotations

import argparse
import json

import boto3

FLOWS = {"acxdmem-e2e-inbound": False, "acxdmem-e2e-probe": True}
MEMORY_VAR = "acxdmemMemoryContext"
BRANCHES = ("Default", "Escalation", "Error", "IdleChatTimeout")


def _id(n: int) -> str:
    return f"acd0e2e0-0000-4000-8000-{n:012d}"


def render(workspace_id: str, application_id: str, alias: str, probe: bool,
           idle_seconds: int | None) -> dict:
    ids = {
        "log": _id(1), "channel": _id(2), "voiceLookup": _id(3), "chatLookup": _id(4),
        "withMemory": _id(5), "noMemory": _id(6),
        **{f"branch{b}": _id(10 + i) for i, b in enumerate(BRANCHES)},
        "disconnect": _id(20),
    }

    def agentic(with_memory: bool) -> dict:
        cfg = {"WorkspaceId": workspace_id, "ApplicationId": application_id, "Alias": alias}
        if with_memory:
            cfg["ContextVariables"] = {"nlx_userId": "$.Customer.ProfileId"}
        params = {
            "AgentConfiguration": cfg,
            "SpeechRecognitionConfiguration": {"SpeechRecognitionEngine": "AMAZON_AGENTIC_VOICE"},
            "AudioFillerConfiguration": {"Enabled": False},
        }
        if idle_seconds:
            # Not in the public flow-language docs. Connect validates this key as an integer
            # (a string value is rejected with "Invalid Action property value"), while unknown
            # keys such as the spike's InputTimeLimitSeconds are accepted and ignored.
            params["IdleChatTimeoutSeconds"] = int(idle_seconds)
        return {
            "Identifier": ids["withMemory" if with_memory else "noMemory"],
            "Type": "ConnectParticipantWithAgenticCX",
            "Parameters": params,
            "Transitions": {
                "NextAction": ids["branchDefault"],
                "Conditions": [{"NextAction": ids["branchEscalation"],
                                "Condition": {"Operator": "Equals", "Operands": ["Escalation"]}}],
                "Errors": [
                    {"NextAction": ids["branchError"], "ErrorType": "NoMatchingError"},
                    {"NextAction": ids["branchDefault"], "ErrorType": "NoMatchingCondition"},
                    {"NextAction": ids["branchIdleChatTimeout"], "ErrorType": "InputTimeLimitExceeded"},
                ],
            },
        }

    def lookup(key: str, name: str, value: str) -> dict:
        return {
            "Identifier": ids[key],
            "Type": "GetCustomerProfile",
            "Parameters": {"ProfileRequestData": {"IdentifierName": name, "IdentifierValue": value},
                           "ProfileResponseData": []},
            "Transitions": {
                "NextAction": ids["withMemory"],
                "Errors": [{"NextAction": ids["noMemory"], "ErrorType": e}
                           for e in ("NoneFoundError", "MultipleFoundError", "NoMatchingError")],
            },
        }

    def branch(b: str) -> dict:
        attrs = {"acxdmemBranch": b}
        if probe:
            # TEST ONLY: proves the exit sub-flow cleared the variable (design 4.1).
            attrs["acxdmemProbeMemory"] = f"$.AgenticCX.ContextVariables.{MEMORY_VAR}"
        nxt = ids["disconnect"]
        return {"Identifier": ids[f"branch{b}"], "Type": "UpdateContactAttributes",
                "Parameters": {"Attributes": attrs, "TargetContact": "Current"},
                "Transitions": {"NextAction": nxt,
                                "Errors": [{"NextAction": nxt, "ErrorType": "NoMatchingError"}]}}

    actions = [
        {"Identifier": ids["log"], "Type": "UpdateFlowLoggingBehavior",
         "Parameters": {"FlowLoggingBehavior": "Enabled"},
         "Transitions": {"NextAction": ids["channel"]}},
        {"Identifier": ids["channel"], "Type": "Compare",
         "Parameters": {"ComparisonValue": "$.Channel"},
         "Transitions": {
             "NextAction": ids["noMemory"],
             "Conditions": [
                 {"NextAction": ids["voiceLookup"], "Condition": {"Operator": "Equals", "Operands": ["VOICE"]}},
                 {"NextAction": ids["chatLookup"], "Condition": {"Operator": "Equals", "Operands": ["CHAT"]}},
             ],
             "Errors": [{"NextAction": ids["noMemory"], "ErrorType": "NoMatchingCondition"}]}},
        lookup("voiceLookup", "_phone", "$.CustomerEndpoint.Address"),
        lookup("chatLookup", "_email", "$.Attributes.acxdmemVerifiedEmail"),
        agentic(True),
        agentic(False),
        *[branch(b) for b in BRANCHES],
        {"Identifier": ids["disconnect"], "Type": "DisconnectParticipant", "Parameters": {}, "Transitions": {}},
    ]
    return {"Version": "2019-10-30", "StartAction": ids["log"], "Actions": actions}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance-id", required=True)
    ap.add_argument("--workspace-id", required=True)
    ap.add_argument("--application-id", required=True)
    ap.add_argument("--alias", required=True)
    ap.add_argument("--idle-seconds", type=int, default=None)
    ap.add_argument("--profile")
    ap.add_argument("--region", default="eu-central-1")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    connect = boto3.Session(profile_name=args.profile, region_name=args.region).client("connect")
    existing = {
        f["Name"]: f["Id"]
        for page in connect.get_paginator("list_contact_flows").paginate(
            InstanceId=args.instance_id, ContactFlowTypes=["CONTACT_FLOW"])
        for f in page["ContactFlowSummaryList"] if f["Name"].startswith("acxdmem-e2e")
    }
    for name, probe in FLOWS.items():
        if not name.startswith("acxdmem-"):
            raise SystemExit(f"refusing to touch non-acxdmem flow {name}")
        content = json.dumps(render(args.workspace_id, args.application_id, args.alias, probe,
                                    args.idle_seconds))
        if not args.apply:
            print(f"dry run: would {'update' if name in existing else 'create'} {name}")
            continue
        if name in existing:
            connect.update_contact_flow_content(InstanceId=args.instance_id,
                                                ContactFlowId=existing[name], Content=content)
            print(f"updated {name} {existing[name]}")
        else:
            out = connect.create_contact_flow(
                InstanceId=args.instance_id, Name=name, Type="CONTACT_FLOW", Content=content,
                Description="acxdmem live test; delete after verification",
                Tags={"project": "acxdmem"})
            print(f"created {name} {out['ContactFlowId']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
