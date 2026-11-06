# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Test helpers: botocore clients with Stubber, sample ACXD conversations."""

from __future__ import annotations

import boto3

PROFILE_ID = "aacd000000000000000000000000c0de"
OTHER_PROFILE_ID = "bbbb000000000000000000000000beef"
CONVERSATION_ID = "11111111-2222-4333-8444-555555555555"
APPLICATION_ID = "22222222-3333-4444-8555-666666666666"
MEMORY_ID = "acxdmem_test-AbCdEf1234"
TABLE = "acxdmem-dedup"


def client(service: str):
    return boto3.client(
        service,
        region_name="eu-central-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )


def conversation(messages, *, user_id=PROFILE_ID, application_id=APPLICATION_ID,
                 conversation_id=CONVERSATION_ID):
    return {
        "conversationId": conversation_id,
        "userId": user_id,
        "applicationId": application_id,
        "messages": messages,
    }


def msg(text, ts, *, app=False, **extra):
    return {"isApplication": app, "text": text, "timestamp": ts, **extra}


SAMPLE_MESSAGES = [
    msg("Hi, how can I help with your trip today?", "2026-10-08T14:47:44.000Z", app=True),
    msg("I need a hotel in Porto for two nights in November.", "2026-10-08T14:47:50.000Z"),
    msg("Which dates in November?", "2026-10-08T14:47:52.000Z", app=True),
    msg("The 12th to the 14th, budget 120 euros a night.", "2026-10-08T14:48:01.000Z"),
    msg("Thanks, goodbye.", "2026-10-08T14:48:03.000Z", app=True),
]
SAMPLE_WATERMARK = 1791470883000  # 2026-10-08T14:48:03Z in epoch ms
