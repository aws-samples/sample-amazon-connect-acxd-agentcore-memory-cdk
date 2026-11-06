#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""T10 check: the contact record and contact attributes must not carry memory text.

For each contact id, reads DescribeContact (the contact record Connect keeps, the source of
the CTR) and GetContactAttributes, and searches both for the context variable name and for
every memory record text currently stored for the profile (exact substrings, case-folded).
Read-only. Prints one line per contact and exits non-zero on any hit.

Usage:
    python3 e2e/check_contact_memory.py --instance-id <id> --profile-id <profileId> --memory-id <id> \
        [--aws-profile <name>] <contactId> [...]
"""

from __future__ import annotations

import argparse
import json
import sys

import boto3

VAR = "acxdmemMemoryContext"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance-id", required=True)
    ap.add_argument("--profile-id", required=True)
    ap.add_argument("--memory-id", required=True)
    ap.add_argument("--aws-profile", default=None)
    ap.add_argument("--region", default="eu-central-1")
    ap.add_argument("contacts", nargs="+")
    args = ap.parse_args()
    s = boto3.Session(profile_name=args.aws_profile, region_name=args.region)
    connect, mem = s.client("connect"), s.client("bedrock-agentcore")

    needles = {VAR.lower()}
    for ns in ("semantic", "recap"):
        out = mem.list_memory_records(memoryId=args.memory_id,
                                      namespace=f"connect/profile/{args.profile_id}/{ns}")
        needles |= {r["content"]["text"].lower() for r in out["memoryRecordSummaries"]}

    hits = 0
    for cid in args.contacts:
        record = connect.describe_contact(InstanceId=args.instance_id, ContactId=cid)["Contact"]
        attrs = connect.get_contact_attributes(InstanceId=args.instance_id, InitialContactId=cid)["Attributes"]
        blob = (json.dumps(record, default=str) + json.dumps(attrs)).lower()
        found = sorted(n for n in needles if n in blob)
        hits += bool(found)
        print(f"{cid}: {'FAIL ' + str(found) if found else 'PASS'} attributes={sorted(attrs)}")
    print(f"searched {len(needles)} needles (variable name + {len(needles) - 1} memory texts)")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
