# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""SegmentStore against the real DynamoDB request shapes (botocore Stubber)."""

from __future__ import annotations

import pytest
from botocore.stub import Stubber

from acxd_memory.dedup import (
    FAILED,
    PENDING,
    TTL_SECONDS,
    WRITTEN,
    SegmentStore,
    client_token,
    conversation_key,
    segment_key,
)
from tests.helpers import CONVERSATION_ID, TABLE, client

NOW = 1_800_000_000


@pytest.fixture
def ddb():
    c = client("dynamodb")
    with Stubber(c) as stub:
        yield c, stub
        stub.assert_no_pending_responses()


def store(c):
    return SegmentStore(c, TABLE, clock=lambda: NOW)


def test_keys():
    assert conversation_key("abc") == "acxd#abc"
    assert segment_key("abc", 123) == "acxd#abc#123"
    token = client_token(segment_key("abc", 123))
    assert token == client_token("acxd#abc#123") and len(token) == 64


def test_get_last_watermark(ddb):
    c, stub = ddb
    stub.add_response(
        "get_item",
        {"Item": {"pk": {"S": "acxd#x"}, "lastWatermark": {"N": "42"}}},
        {
            "TableName": TABLE,
            "Key": {"pk": {"S": conversation_key(CONVERSATION_ID)}},
            "ConsistentRead": True,
            "ProjectionExpression": "lastWatermark",
        },
    )
    stub.add_response("get_item", {}, None)
    s = store(c)
    assert s.get_last_watermark(CONVERSATION_ID) == 42
    assert s.get_last_watermark(CONVERSATION_ID) is None


def test_advance_watermark_condition_and_conflict(ddb):
    c, stub = ddb
    stub.add_response(
        "update_item",
        {},
        {
            "TableName": TABLE,
            "Key": {"pk": {"S": conversation_key(CONVERSATION_ID)}},
            "UpdateExpression": "SET lastWatermark = :new, updatedAt = :now, expires_at = :exp",
            "ConditionExpression": "attribute_not_exists(pk) OR lastWatermark < :new",
            "ExpressionAttributeValues": {
                ":new": {"N": "100"},
                ":now": {"N": str(NOW)},
                ":exp": {"N": str(NOW + TTL_SECONDS)},
            },
        },
    )
    stub.add_client_error("update_item", "ConditionalCheckFailedException")
    s = store(c)
    assert s.advance_watermark(CONVERSATION_ID, 100) is True
    assert s.advance_watermark(CONVERSATION_ID, 90) is False


def test_claim_condition_allows_new_failed_stale_or_same_message(ddb):
    c, stub = ddb
    seg = segment_key(CONVERSATION_ID, 100)
    stub.add_response(
        "put_item",
        {},
        {
            "TableName": TABLE,
            "Item": {
                "pk": {"S": seg},
                "status": {"S": PENDING},
                "attempt": {"N": "2"},
                "claimedAt": {"N": str(NOW)},
                "messageId": {"S": "m-1"},
                "expires_at": {"N": str(NOW + TTL_SECONDS)},
            },
            "ConditionExpression": (
                "attribute_not_exists(pk) OR #s = :failed OR "
                "(#s = :pending AND (claimedAt < :stale OR messageId = :mid))"
            ),
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {
                ":failed": {"S": FAILED},
                ":pending": {"S": PENDING},
                ":stale": {"N": str(NOW - 900)},
                ":mid": {"S": "m-1"},
            },
        },
    )
    stub.add_client_error("put_item", "ConditionalCheckFailedException")
    s = store(c)
    assert s.claim(seg, attempt=2, message_id="m-1") is True
    assert s.claim(seg, attempt=2, message_id="m-2") is False


def test_transition_requires_pending_and_same_attempt(ddb):
    c, stub = ddb
    seg = segment_key(CONVERSATION_ID, 100)
    stub.add_response(
        "update_item",
        {},
        {
            "TableName": TABLE,
            "Key": {"pk": {"S": seg}},
            "UpdateExpression": "SET #s = :status, finishedAt = :now",
            "ConditionExpression": "#s = :pending AND attempt = :n",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {
                ":status": {"S": WRITTEN},
                ":pending": {"S": PENDING},
                ":n": {"N": "3"},
                ":now": {"N": str(NOW)},
            },
        },
    )
    stub.add_client_error("update_item", "ConditionalCheckFailedException")
    s = store(c)
    assert s.transition(seg, attempt=3, status=WRITTEN) is True
    assert s.release(seg, attempt=2) is False


def test_other_errors_propagate(ddb):
    c, stub = ddb
    stub.add_client_error("put_item", "ProvisionedThroughputExceededException")
    with pytest.raises(Exception) as exc:
        store(c).claim("acxd#x#1", attempt=0, message_id="m")
    assert "ProvisionedThroughputExceeded" in str(exc.value)
