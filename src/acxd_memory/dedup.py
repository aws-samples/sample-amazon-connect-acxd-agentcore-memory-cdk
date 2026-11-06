# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Segment dedup on DynamoDB (design §3.4).

Ported from the AI Agents sample (``connect_memory/dedup.py``): one table, conditional
writes as the atomic check-and-set, 90-day TTL matching the memory event expiry, and
``ConditionalCheckFailedException`` detected by error code so this module needs no botocore
import. Extended for ACXD because one ``conversationId`` can exit more than once (spike
S10):

- **Conversation row** ``acxd#{conversationId}`` holds ``lastWatermark`` (epoch ms of the
  last message already written). It only moves forward:
  ``attribute_not_exists(pk) OR lastWatermark < :new``.
- **Segment row** ``acxd#{conversationId}#{watermark}`` is a small state machine:
  ``pending`` -> ``written`` | ``superseded`` | ``empty``, or ``pending`` -> ``failed``
  (released after a ``CreateEvent`` error so the SQS retry can claim it again).

Claim condition: no row yet, or a released (``failed``) row, or a ``pending`` row that is
stale (claimed more than 15 minutes ago) or was claimed by the same SQS message (a
redelivery after a crash or timeout). Every later transition requires
``status = pending AND attempt = :n``, so an older attempt cannot overwrite a newer one.
Two copies that both pass the claim (same SQS message delivered twice) are covered by the
``CreateEvent`` ``clientToken``.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Callable, Protocol

TTL_SECONDS = 90 * 24 * 60 * 60
STALE_PENDING_SECONDS = 15 * 60

PENDING = "pending"
WRITTEN = "written"
SUPERSEDED = "superseded"
EMPTY = "empty"
FAILED = "failed"


class DynamoDbClient(Protocol):
    """The subset of the boto3 low-level DynamoDB client this module calls."""

    def get_item(self, **kwargs: Any) -> Any: ...

    def put_item(self, **kwargs: Any) -> Any: ...

    def update_item(self, **kwargs: Any) -> Any: ...


def conversation_key(conversation_id: str) -> str:
    return f"acxd#{conversation_id}"


def segment_key(conversation_id: str, watermark_ms: int) -> str:
    """Design §3.4 segment key ``acxd#{conversationId}#{watermark}``."""
    return f"acxd#{conversation_id}#{watermark_ms}"


def client_token(segment: str) -> str:
    """``CreateEvent`` ``clientToken`` derived from the segment key.

    The design uses the segment key as the token. The public API reference gives no length
    or character constraint for ``clientToken``, and the raw key (~60 characters with
    ``#``) is close to the 64-character limit many AWS idempotency tokens have, so the token
    is the SHA-256 hex digest of the key: same key, same token, still idempotent.
    """
    return hashlib.sha256(segment.encode("utf-8")).hexdigest()


def _is_condition_failure(exc: Exception) -> bool:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code")
    return code == "ConditionalCheckFailedException"


class SegmentStore:
    """Conversation watermark + segment claim state on one DynamoDB table (pk ``pk``)."""

    def __init__(
        self,
        client: DynamoDbClient,
        table_name: str,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._table = table_name
        self._clock = clock

    # --- conversation row -------------------------------------------------------------

    def get_last_watermark(self, conversation_id: str) -> int | None:
        response = self._client.get_item(
            TableName=self._table,
            Key={"pk": {"S": conversation_key(conversation_id)}},
            ConsistentRead=True,
            ProjectionExpression="lastWatermark",
        )
        item = response.get("Item") or {}
        value = item.get("lastWatermark", {}).get("N")
        return int(value) if value is not None else None

    def advance_watermark(self, conversation_id: str, watermark_ms: int) -> bool:
        """Move ``lastWatermark`` forward. False when a newer watermark is already stored."""
        now = int(self._clock())
        try:
            self._client.update_item(
                TableName=self._table,
                Key={"pk": {"S": conversation_key(conversation_id)}},
                UpdateExpression="SET lastWatermark = :new, updatedAt = :now, expires_at = :exp",
                ConditionExpression="attribute_not_exists(pk) OR lastWatermark < :new",
                ExpressionAttributeValues={
                    ":new": {"N": str(watermark_ms)},
                    ":now": {"N": str(now)},
                    ":exp": {"N": str(now + TTL_SECONDS)},
                },
            )
            return True
        except Exception as exc:  # noqa: BLE001
            if _is_condition_failure(exc):
                return False
            raise

    # --- segment rows -----------------------------------------------------------------

    def claim(self, segment: str, *, attempt: int, message_id: str) -> bool:
        """Claim a segment for writing. False means another attempt owns or wrote it."""
        now = int(self._clock())
        try:
            self._client.put_item(
                TableName=self._table,
                Item={
                    "pk": {"S": segment},
                    "status": {"S": PENDING},
                    "attempt": {"N": str(attempt)},
                    "claimedAt": {"N": str(now)},
                    "messageId": {"S": message_id},
                    "expires_at": {"N": str(now + TTL_SECONDS)},
                },
                ConditionExpression=(
                    "attribute_not_exists(pk) OR #s = :failed OR "
                    "(#s = :pending AND (claimedAt < :stale OR messageId = :mid))"
                ),
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":failed": {"S": FAILED},
                    ":pending": {"S": PENDING},
                    ":stale": {"N": str(now - STALE_PENDING_SECONDS)},
                    ":mid": {"S": message_id},
                },
            )
            return True
        except Exception as exc:  # noqa: BLE001
            if _is_condition_failure(exc):
                return False
            raise

    def transition(self, segment: str, *, attempt: int, status: str) -> bool:
        """``pending`` -> ``status``, only for the attempt that holds the claim."""
        now = int(self._clock())
        try:
            self._client.update_item(
                TableName=self._table,
                Key={"pk": {"S": segment}},
                UpdateExpression="SET #s = :status, finishedAt = :now",
                ConditionExpression="#s = :pending AND attempt = :n",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":status": {"S": status},
                    ":pending": {"S": PENDING},
                    ":n": {"N": str(attempt)},
                    ":now": {"N": str(now)},
                },
            )
            return True
        except Exception as exc:  # noqa: BLE001
            if _is_condition_failure(exc):
                return False
            raise

    def release(self, segment: str, *, attempt: int) -> bool:
        """Mark a claimed segment ``failed`` so a retry can claim it again."""
        return self.transition(segment, attempt=attempt, status=FAILED)
