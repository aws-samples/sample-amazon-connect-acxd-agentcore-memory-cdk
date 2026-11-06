# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Writer state machine for the Option B transcript fetch (design §3.2).

Pure decision logic: given the work item from the SQS message and the result of one
``GetConversation`` fetch, decide whether to re-queue (and with which delay and state),
write, or give up. The writer Lambda performs the side effects.

Message body: ``{profileId, conversationId, attempt, phase, lastSeenWatermark,
lastSeenCount}``.

- Ingest enqueues ``{attempt: 0, phase: fetch}`` with ``DelaySeconds = 120``.
- ``fetch`` + 200: record watermark and count, re-queue ``confirm`` after 30 s.
- ``confirm`` + 200, watermark and count unchanged: stable, write.
- ``confirm`` + 200, changed (partial index): store the new values, re-queue ``confirm``
  after 30 s.
- 404 in either phase ("not yet indexed", including a 200 -> 404 flap): re-queue the same
  phase with backoff 60, 120, 300 s, indexed by the re-queue count (300 s from the third
  re-queue on).
- 429 or 5xx: re-queue the same phase after 30 s plus up to 15 s jitter.
- Cap: 6 re-queues. At the cap a 200 response is written (``TranscriptIncomplete``);
  otherwise the item is dropped with ``TranscriptIncomplete`` (seen before, content not
  available in this invocation) or ``TranscriptNeverIndexed``. A missing transcript is
  never sent to the DLQ.

``attempt`` counts re-queues of this work item. Every delay is <= 900 s (SQS maximum).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping

from acxd_memory.identity import is_valid_conversation_id

FETCH = "fetch"
CONFIRM = "confirm"
PHASES = (FETCH, CONFIRM)

INITIAL_DELAY_SECONDS = 120
CONFIRM_DELAY_SECONDS = 30
NOT_FOUND_BACKOFF_SECONDS = (60, 120, 300)
RETRYABLE_BASE_SECONDS = 30
RETRYABLE_JITTER_SECONDS = 15
MAX_REQUEUES = 6
SQS_MAX_DELAY_SECONDS = 900

# Fetch outcomes, from the HTTP status the Node fetch step reports.
OK = "ok"
NOT_FOUND = "not_found"
RETRYABLE = "retryable"

REQUEUE = "requeue"
WRITE = "write"
GIVE_UP = "give_up"

TRANSCRIPT_INCOMPLETE = "TranscriptIncomplete"
TRANSCRIPT_NEVER_INDEXED = "TranscriptNeverIndexed"


class MalformedWorkItem(ValueError):
    """The SQS message body is not a valid work item."""


@dataclass(frozen=True)
class WorkItem:
    profile_id: str
    conversation_id: str
    attempt: int = 0
    phase: str = FETCH
    last_seen_watermark: int | None = None
    last_seen_count: int | None = None

    def to_body(self) -> dict[str, Any]:
        return {
            "profileId": self.profile_id,
            "conversationId": self.conversation_id,
            "attempt": self.attempt,
            "phase": self.phase,
            "lastSeenWatermark": self.last_seen_watermark,
            "lastSeenCount": self.last_seen_count,
        }

    @classmethod
    def from_body(cls, body: Mapping[str, Any]) -> "WorkItem":
        if not isinstance(body, Mapping):
            raise MalformedWorkItem("body is not an object")
        profile_id = body.get("profileId")
        conversation_id = body.get("conversationId")
        attempt = body.get("attempt", 0)
        phase = body.get("phase", FETCH)
        watermark = body.get("lastSeenWatermark")
        count = body.get("lastSeenCount")
        if not isinstance(profile_id, str) or not profile_id:
            raise MalformedWorkItem("profileId missing")
        if not isinstance(conversation_id, str) or not is_valid_conversation_id(conversation_id):
            raise MalformedWorkItem("conversationId missing or malformed")
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 0:
            raise MalformedWorkItem("attempt must be a non-negative integer")
        if phase not in PHASES:
            raise MalformedWorkItem("unknown phase")
        for name, value in (("lastSeenWatermark", watermark), ("lastSeenCount", count)):
            if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
                raise MalformedWorkItem(f"{name} must be an integer or null")
        return cls(
            profile_id=profile_id,
            conversation_id=conversation_id,
            attempt=attempt,
            phase=phase,
            last_seen_watermark=watermark,
            last_seen_count=count,
        )


@dataclass(frozen=True)
class FetchOutcome:
    kind: str
    """``ok``, ``not_found`` or ``retryable``."""
    watermark: int | None = None
    count: int | None = None


@dataclass(frozen=True)
class Decision:
    action: str
    """``requeue``, ``write`` or ``give_up``."""
    next_item: WorkItem | None = None
    delay_seconds: int = 0
    metric: str | None = None
    """``TranscriptIncomplete`` / ``TranscriptNeverIndexed`` when the cap was reached."""


def _not_found_delay(item: WorkItem) -> int:
    return NOT_FOUND_BACKOFF_SECONDS[min(item.attempt, len(NOT_FOUND_BACKOFF_SECONDS) - 1)]


def _retryable_delay(rand: Callable[[], float]) -> int:
    return RETRYABLE_BASE_SECONDS + int(rand() * RETRYABLE_JITTER_SECONDS)


def decide(
    item: WorkItem,
    outcome: FetchOutcome,
    *,
    rand: Callable[[], float] = random.random,
) -> Decision:
    """Next step for ``item`` after one fetch."""
    if outcome.kind == OK and item.phase == CONFIRM and (
        outcome.watermark == item.last_seen_watermark and outcome.count == item.last_seen_count
    ):
        return Decision(action=WRITE)

    if outcome.kind == OK:
        next_item = replace(
            item,
            phase=CONFIRM,
            last_seen_watermark=outcome.watermark,
            last_seen_count=outcome.count,
        )
        delay = CONFIRM_DELAY_SECONDS
    elif outcome.kind == NOT_FOUND:
        next_item = item
        delay = _not_found_delay(item)
    elif outcome.kind == RETRYABLE:
        next_item = item
        delay = _retryable_delay(rand)
    else:
        raise ValueError(f"unknown fetch outcome {outcome.kind!r}")

    if item.attempt >= MAX_REQUEUES:
        if outcome.kind == OK:
            return Decision(action=WRITE, metric=TRANSCRIPT_INCOMPLETE)
        if item.last_seen_watermark is not None or item.last_seen_count is not None:
            return Decision(action=GIVE_UP, metric=TRANSCRIPT_INCOMPLETE)
        return Decision(action=GIVE_UP, metric=TRANSCRIPT_NEVER_INDEXED)

    return Decision(
        action=REQUEUE,
        next_item=replace(next_item, attempt=item.attempt + 1),
        delay_seconds=min(delay, SQS_MAX_DELAY_SECONDS),
    )
