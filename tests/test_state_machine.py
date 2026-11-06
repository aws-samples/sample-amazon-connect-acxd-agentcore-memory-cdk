# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Writer state machine (design §3.2)."""

from __future__ import annotations

import pytest

from acxd_memory.state_machine import (
    CONFIRM,
    FETCH,
    GIVE_UP,
    MAX_REQUEUES,
    NOT_FOUND,
    OK,
    REQUEUE,
    RETRYABLE,
    SQS_MAX_DELAY_SECONDS,
    TRANSCRIPT_INCOMPLETE,
    TRANSCRIPT_NEVER_INDEXED,
    WRITE,
    FetchOutcome,
    MalformedWorkItem,
    WorkItem,
    decide,
)
from tests.helpers import CONVERSATION_ID, PROFILE_ID


def item(**kw):
    return WorkItem(profile_id=PROFILE_ID, conversation_id=CONVERSATION_ID, **kw)


def test_fetch_ok_requeues_confirm_after_30s():
    d = decide(item(), FetchOutcome(OK, watermark=10, count=5))
    assert d.action == REQUEUE
    assert d.delay_seconds == 30
    assert d.next_item == item(attempt=1, phase=CONFIRM, last_seen_watermark=10, last_seen_count=5)


def test_confirm_unchanged_writes():
    d = decide(item(attempt=1, phase=CONFIRM, last_seen_watermark=10, last_seen_count=5),
               FetchOutcome(OK, watermark=10, count=5))
    assert d.action == WRITE and d.metric is None


def test_confirm_changed_stores_new_values_and_requeues_confirm():
    d = decide(item(attempt=1, phase=CONFIRM, last_seen_watermark=10, last_seen_count=2),
               FetchOutcome(OK, watermark=20, count=7))
    assert d.action == REQUEUE and d.delay_seconds == 30
    assert d.next_item.phase == CONFIRM
    assert (d.next_item.last_seen_watermark, d.next_item.last_seen_count) == (20, 7)


@pytest.mark.parametrize("attempt, delay", [(0, 60), (1, 120), (2, 300), (5, 300)])
def test_not_found_backoff_same_phase(attempt, delay):
    d = decide(item(attempt=attempt), FetchOutcome(NOT_FOUND))
    assert d.action == REQUEUE and d.delay_seconds == delay
    assert d.next_item.phase == FETCH and d.next_item.attempt == attempt + 1


def test_flap_200_then_404_keeps_confirm_phase_and_last_seen():
    start = item(attempt=1, phase=CONFIRM, last_seen_watermark=10, last_seen_count=5)
    d = decide(start, FetchOutcome(NOT_FOUND))
    assert d.next_item.phase == CONFIRM
    assert d.next_item.last_seen_watermark == 10


def test_retryable_is_30s_plus_jitter():
    assert decide(item(), FetchOutcome(RETRYABLE), rand=lambda: 0.0).delay_seconds == 30
    assert decide(item(), FetchOutcome(RETRYABLE), rand=lambda: 0.999).delay_seconds == 44


def test_cap_with_200_writes_incomplete():
    d = decide(item(attempt=MAX_REQUEUES, phase=CONFIRM, last_seen_watermark=1, last_seen_count=1),
               FetchOutcome(OK, watermark=2, count=2))
    assert d.action == WRITE and d.metric == TRANSCRIPT_INCOMPLETE


def test_cap_never_indexed_gives_up():
    d = decide(item(attempt=MAX_REQUEUES), FetchOutcome(NOT_FOUND))
    assert d.action == GIVE_UP and d.metric == TRANSCRIPT_NEVER_INDEXED


def test_cap_after_seen_but_now_404_gives_up_incomplete():
    d = decide(item(attempt=MAX_REQUEUES, phase=CONFIRM, last_seen_watermark=1, last_seen_count=1),
               FetchOutcome(NOT_FOUND))
    assert d.action == GIVE_UP and d.metric == TRANSCRIPT_INCOMPLETE


def test_worst_case_total_delay_is_within_design_bound():
    """Initial 120 s + six 404 re-queues stays near the documented ~630 s after initial delay
    and every single delay is <= the SQS maximum."""
    current = item()
    delays = []
    while True:
        d = decide(current, FetchOutcome(NOT_FOUND))
        if d.action != REQUEUE:
            break
        delays.append(d.delay_seconds)
        current = d.next_item
    assert len(delays) == MAX_REQUEUES
    assert all(x <= SQS_MAX_DELAY_SECONDS for x in delays)
    assert sum(delays) <= 60 + 120 + 300 * 4


def test_body_round_trip():
    original = item(attempt=2, phase=CONFIRM, last_seen_watermark=5, last_seen_count=3)
    assert WorkItem.from_body(original.to_body()) == original


@pytest.mark.parametrize(
    "body",
    [
        None,
        {"conversationId": CONVERSATION_ID},
        {"profileId": PROFILE_ID},
        {"profileId": PROFILE_ID, "conversationId": "../x"},
        {"profileId": PROFILE_ID, "conversationId": CONVERSATION_ID, "attempt": -1},
        {"profileId": PROFILE_ID, "conversationId": CONVERSATION_ID, "attempt": True},
        {"profileId": PROFILE_ID, "conversationId": CONVERSATION_ID, "phase": "write"},
        {"profileId": PROFILE_ID, "conversationId": CONVERSATION_ID, "lastSeenCount": "3"},
    ],
)
def test_malformed_bodies(body):
    with pytest.raises(MalformedWorkItem):
        WorkItem.from_body(body)
