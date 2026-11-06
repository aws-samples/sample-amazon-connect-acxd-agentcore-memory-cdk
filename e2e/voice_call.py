#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Place the acxdmem voice test call to YOUR phone (design.md section 5).

Run it yourself, so your number never leaves your terminal:

    COMMON="--instance-id <id> --domain <profiles domain> --source-number <instance number>"
    python3 e2e/voice_call.py $COMMON --phone +XXXXXXXXXXX            # dry run, prints the plan
    python3 e2e/voice_call.py $COMMON --phone +XXXXXXXXXXX --apply    # adds number, places call
    python3 e2e/voice_call.py $COMMON --phone +XXXXXXXXXXX --cleanup  # removes number from profile

What --apply does:
  1. Sets PhoneNumber on the synthetic test profile (acxdmem-e2e-1@example.com), so the
     acxdmem-e2e-inbound flow's voice lookup (_phone = $.CustomerEndpoint.Address, the
     number Connect dialled) resolves to it. Voice uses the caller line as the
     authenticated identifier, as in the shipped snippet (T9).
  2. StartOutboundVoiceContact from --source-number into the NEW acxdmem-e2e-inbound
     flow. No existing flow, queue or number setting is changed.
  3. Prints the contact id for your verification notes.

On the call (the agent was told in an earlier chat about a vegetarian meal and aisle seat):
  - wait for the greeting, then ask: "What seat and meal should I request on my next flight?"
    It should answer aisle seat and vegetarian meal (recall from the chat contacts).
  - then say a new fact: "Also, I'm travelling with my dog Bruno next time."
  - then: "That's all, goodbye." The call should end with the goodbye message.
  - note roughly how long the greeting took after you answered.

The number is not printed by this script or written to any file. Run --cleanup afterwards
(teardown also deletes the whole test profile).
"""

from __future__ import annotations

import argparse
import re
import sys

import boto3

FLOW_NAME = "acxdmem-e2e-inbound"
TEST_EMAIL = "acxdmem-e2e-1@example.com"
E164 = re.compile(r"^\+[1-9]\d{6,14}$")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instance-id", required=True, help="Amazon Connect Customer instance id")
    ap.add_argument("--domain", required=True, help="Customer Profiles domain name")
    ap.add_argument("--source-number", required=True, help="a number claimed by the instance, E.164")
    ap.add_argument("--phone", required=True, help="your phone in E.164, e.g. +15555550100")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--cleanup", action="store_true")
    ap.add_argument("--aws-profile", default=None)
    ap.add_argument("--region", default="eu-central-1")
    args = ap.parse_args()
    if not E164.match(args.phone) or not E164.match(args.source_number):
        print("--phone and --source-number must be E.164, e.g. +15555550100", file=sys.stderr)
        return 2

    s = boto3.Session(profile_name=args.aws_profile, region_name=args.region)
    profiles, connect = s.client("customer-profiles"), s.client("connect")
    hits = profiles.search_profiles(DomainName=args.domain, KeyName="_email", Values=[TEST_EMAIL])["Items"]
    if len(hits) != 1:
        print(f"expected exactly one test profile for {TEST_EMAIL}, found {len(hits)}", file=sys.stderr)
        return 1
    profile_id = hits[0]["ProfileId"]
    flows = [f["Id"] for page in connect.get_paginator("list_contact_flows").paginate(
                 InstanceId=args.instance_id, ContactFlowTypes=["CONTACT_FLOW"])
             for f in page["ContactFlowSummaryList"] if f["Name"] == FLOW_NAME]
    if len(flows) != 1:
        print(f"flow {FLOW_NAME} not found", file=sys.stderr)
        return 1

    if args.cleanup:
        profiles.update_profile(DomainName=args.domain, ProfileId=profile_id, PhoneNumber="")
        print(f"removed the phone number from test profile {profile_id}")
        return 0

    # Refuse if the number already belongs to another profile: the lookup must be unique.
    others = [p["ProfileId"] for p in profiles.search_profiles(
        DomainName=args.domain, KeyName="_phone", Values=[args.phone])["Items"] if p["ProfileId"] != profile_id]
    if others:
        print(f"your number is already on {len(others)} other profile(s); the flow would take the "
              "no-memory path (MultipleFoundError). Not placing the call.", file=sys.stderr)
        return 1

    if not args.apply:
        print(f"dry run: would set the phone on test profile {profile_id}, then call you from "
              f"{args.source_number} into {FLOW_NAME} ({flows[0]}). Re-run with --apply.")
        return 0

    profiles.update_profile(DomainName=args.domain, ProfileId=profile_id, PhoneNumber=args.phone)
    out = connect.start_outbound_voice_contact(
        InstanceId=args.instance_id, ContactFlowId=flows[0], DestinationPhoneNumber=args.phone,
        SourcePhoneNumber=args.source_number,
        Attributes={"acxdmemTest": "voice"},
    )
    print(f"calling you now. contactId={out['ContactId']}")
    print("after the call: note the contactId, then run with --cleanup")
    return 0


if __name__ == "__main__":
    sys.exit(main())
