# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""MemoryWriter: identity checks, watermark dedup, pre-write re-read, CreateEvent shape (Stubber)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from botocore.stub import ANY, Stubber

from acxd_memory import dedup
from acxd_memory.acxd_transcript import parse_conversation
from acxd_memory.reader import MemoryContext
from acxd_memory.writer import CreateEventFailed, MemoryWriter, MemoryWriterConfig, SkipReason
from tests.fakes import FakeStore
from tests.helpers import (
    APPLICATION_ID,
    CONVERSATION_ID,
    MEMORY_ID,
    OTHER_PROFILE_ID,
    PROFILE_ID,
    SAMPLE_MESSAGES,
    SAMPLE_WATERMARK,
    client,
    conversation,
    msg,
)

SEGMENT = dedup.segment_key(CONVERSATION_ID, SAMPLE_WATERMARK)


@pytest.fixture
def memory():
    c = client("bedrock-agentcore")
    with Stubber(c) as stub:
        yield c, stub
        stub.assert_no_pending_responses()


def writer(mem, store, **cfg):
    return MemoryWriter(
        config=MemoryWriterConfig(memory_id=MEMORY_ID, application_id=APPLICATION_ID, **cfg),
        memory=mem,
        store=store,
    )


def expected_create(payload, watermark=SAMPLE_WATERMARK, segment=SEGMENT):
    return {
        "memoryId": MEMORY_ID,
        "actorId": PROFILE_ID,
        "sessionId": CONVERSATION_ID,
        "eventTimestamp": datetime.fromtimestamp(watermark / 1000, tz=timezone.utc),
        "payload": payload,
        "clientToken": dedup.client_token(segment),
    }


def ok_event(eid="evt-1"):
    return {"event": {"memoryId": MEMORY_ID, "actorId": PROFILE_ID, "sessionId": CONVERSATION_ID,
                      "eventId": eid, "eventTimestamp": datetime(2026, 10, 8, tzinfo=timezone.utc),
                      "payload": [], "branch": {"name": "main"}}}


def payload_of(messages):
    return [
        {"conversational": {"role": "ASSISTANT" if m["isApplication"] else "USER",
                            "content": {"text": m["text"]}}}
        for m in messages
    ]


def test_writes_full_segment_and_advances_watermark(memory):
    mem, stub = memory
    stub.add_response("create_event", ok_event(), expected_create(payload_of(SAMPLE_MESSAGES)))
    store = FakeStore()
    out = writer(mem, store).write_segment(
        profile_id=PROFILE_ID, transcript=parse_conversation(conversation(SAMPLE_MESSAGES)),
        attempt=1, message_id="m1")
    assert out.written and out.event_id == "evt-1" and out.turns_written == 5
    assert store.watermarks[CONVERSATION_ID] == SAMPLE_WATERMARK
    assert store.segments[SEGMENT]["status"] == dedup.WRITTEN


def test_second_exit_writes_only_newer_messages(memory):
    """Same conversationId exits twice; GetConversation returns the whole history."""
    mem, stub = memory
    first = SAMPLE_MESSAGES[:3]
    first_wm = 1791470872000  # 14:47:52Z
    later = SAMPLE_MESSAGES[3:]
    stub.add_response("create_event", ok_event(),
                      expected_create(payload_of(later)))
    store = FakeStore()
    store.watermarks[CONVERSATION_ID] = first_wm
    assert parse_conversation(conversation(first)).watermark_ms == first_wm
    out = writer(mem, store).write_segment(
        profile_id=PROFILE_ID, transcript=parse_conversation(conversation(SAMPLE_MESSAGES)),
        attempt=0, message_id="m2")
    assert out.written and out.turns_written == 2


def test_redelivered_segment_is_skipped_as_duplicate(memory):
    mem, _ = memory
    store = FakeStore()
    store.segments[SEGMENT] = {"status": dedup.WRITTEN, "attempt": 1, "claimedAt": 0,
                               "messageId": "m1"}
    out = writer(mem, store).write_segment(
        profile_id=PROFILE_ID, transcript=parse_conversation(conversation(SAMPLE_MESSAGES)),
        attempt=1, message_id="m9")
    assert out.skip_reason == SkipReason.DUPLICATE


def test_already_written_watermark_is_no_new_messages(memory):
    mem, _ = memory
    store = FakeStore()
    store.watermarks[CONVERSATION_ID] = SAMPLE_WATERMARK
    out = writer(mem, store).write_segment(
        profile_id=PROFILE_ID, transcript=parse_conversation(conversation(SAMPLE_MESSAGES)),
        attempt=0, message_id="m")
    assert out.skip_reason == SkipReason.NO_NEW_MESSAGES


def test_g4_racing_copy_wrote_meanwhile_supersedes(memory):
    mem, _ = memory
    store = FakeStore()

    def race(s, n):
        if n == 2:  # between first read and the pre-CreateEvent re-read
            s.watermarks[CONVERSATION_ID] = SAMPLE_WATERMARK

    store.on_get = race
    out = writer(mem, store).write_segment(
        profile_id=PROFILE_ID, transcript=parse_conversation(conversation(SAMPLE_MESSAGES)),
        attempt=0, message_id="m")
    assert out.skip_reason == SkipReason.DUPLICATE
    assert store.segments[SEGMENT]["status"] == dedup.SUPERSEDED


def test_g4_racing_copy_wrote_older_part_refilters(memory):
    mem, stub = memory
    store = FakeStore()
    mid = 1791470872000

    def race(s, n):
        if n == 2:
            s.watermarks[CONVERSATION_ID] = mid

    store.on_get = race
    stub.add_response("create_event", ok_event(), expected_create(payload_of(SAMPLE_MESSAGES[3:])))
    out = writer(mem, store).write_segment(
        profile_id=PROFILE_ID, transcript=parse_conversation(conversation(SAMPLE_MESSAGES)),
        attempt=0, message_id="m")
    assert out.written and out.turns_written == 2


@pytest.mark.parametrize(
    "profile, user, reason",
    [
        ("", PROFILE_ID, SkipReason.NO_PROFILE_ID),
        ("not-a-profile", PROFILE_ID, SkipReason.MALFORMED_PROFILE_ID),
        (PROFILE_ID, OTHER_PROFILE_ID, SkipReason.IDENTITY_MISMATCH),
        (PROFILE_ID, None, SkipReason.IDENTITY_MISMATCH),
    ],
)
def test_identity_rules(memory, profile, user, reason):
    mem, _ = memory
    out = writer(mem, FakeStore()).write_segment(
        profile_id=profile,
        transcript=parse_conversation(conversation(SAMPLE_MESSAGES, user_id=user)),
        attempt=0, message_id="m")
    assert out.skip_reason == reason


def test_other_application_is_skipped(memory):
    mem, _ = memory
    t = parse_conversation(conversation(SAMPLE_MESSAGES,
                                        application_id="00000000-0000-4000-8000-00000000ffff"))
    out = writer(mem, FakeStore()).write_segment(profile_id=PROFILE_ID, transcript=t,
                                                 attempt=0, message_id="m")
    assert out.skip_reason == SkipReason.APPLICATION_MISMATCH


def test_missing_application_is_skipped(memory):
    mem, _ = memory
    t = parse_conversation(conversation(SAMPLE_MESSAGES, application_id=None))
    out = writer(mem, FakeStore()).write_segment(profile_id=PROFILE_ID, transcript=t,
                                                 attempt=0, message_id="m")
    assert out.skip_reason == SkipReason.APPLICATION_MISMATCH


def test_create_event_failure_releases_claim_and_raises(memory):
    mem, stub = memory
    stub.add_client_error("create_event", "ThrottledException", http_status_code=429,
                          expected_params=expected_create(payload_of(SAMPLE_MESSAGES)))
    store = FakeStore()
    with pytest.raises(CreateEventFailed):
        writer(mem, store).write_segment(
            profile_id=PROFILE_ID, transcript=parse_conversation(conversation(SAMPLE_MESSAGES)),
            attempt=0, message_id="m")
    assert store.segments[SEGMENT]["status"] == dedup.FAILED
    assert CONVERSATION_ID not in store.watermarks
    # the SQS retry can claim again
    assert store.claim(SEGMENT, attempt=0, message_id="m") is True


def test_stripping_off_never_calls_retrieve(memory):
    mem, stub = memory
    stub.add_response("create_event", ok_event(), None)

    def boom(*a, **k):
        raise AssertionError("retrieve must not run with stripping off")

    w = MemoryWriter(config=MemoryWriterConfig(memory_id=MEMORY_ID), memory=mem,
                     store=FakeStore(), retrieve=boom)
    assert w.write_segment(profile_id=PROFILE_ID,
                           transcript=parse_conversation(conversation(SAMPLE_MESSAGES)),
                           attempt=0, message_id="m").written


def test_stripping_on_strips_assistant_restatement(memory):
    mem, stub = memory
    record = "The customer prefers window seats and travelled to Lisbon in August."
    messages = [
        msg(f"Welcome back. {record} How can I help?", "2026-10-08T14:47:44Z", app=True),
        msg(record, "2026-10-08T14:48:03Z"),
    ]
    stub.add_response(
        "create_event", ok_event(),
        {**expected_create([
            {"conversational": {"role": "ASSISTANT", "content": {"text": "Welcome back. How can I help?"}}},
            {"conversational": {"role": "USER", "content": {"text": record}}},
        ]), "eventTimestamp": ANY},
    )
    ctx = MemoryContext(actor_id=PROFILE_ID, available=True, records=(record,))
    w = MemoryWriter(config=MemoryWriterConfig(memory_id=MEMORY_ID, enable_stripping=True),
                     memory=mem, store=FakeStore(), retrieve=lambda *a, **k: ctx)
    out = w.write_segment(profile_id=PROFILE_ID, transcript=parse_conversation(conversation(messages)),
                          attempt=0, message_id="m")
    assert out.written and out.turns_stripped == 1 and not out.strip_skipped


def test_stripping_retrieval_failure_writes_unstripped(memory):
    mem, stub = memory
    stub.add_response("create_event", ok_event(), expected_create(payload_of(SAMPLE_MESSAGES)))
    failed = MemoryContext(actor_id=PROFILE_ID, available=False, skip_reason="retrieval_error")
    w = MemoryWriter(config=MemoryWriterConfig(memory_id=MEMORY_ID, enable_stripping=True),
                     memory=mem, store=FakeStore(), retrieve=lambda *a, **k: failed)
    out = w.write_segment(profile_id=PROFILE_ID,
                          transcript=parse_conversation(conversation(SAMPLE_MESSAGES)),
                          attempt=0, message_id="m")
    assert out.written and out.strip_skipped


def test_long_segment_is_split_into_100_entry_events(memory):
    mem, stub = memory
    base = 1791470000000
    messages = [msg(f"turn {i}", base + i * 1000, app=bool(i % 2)) for i in range(150)]
    t = parse_conversation(conversation(messages))
    segment = dedup.segment_key(CONVERSATION_ID, t.watermark_ms)
    payload = payload_of(messages)
    stub.add_response("create_event", ok_event("e1"), {
        **expected_create(payload[:100], watermark=base + 99 * 1000, segment=segment)})
    stub.add_response("create_event", ok_event("e2"), {
        **expected_create(payload[100:], watermark=t.watermark_ms, segment=segment),
        "clientToken": dedup.client_token(f"{segment}#1")})
    out = writer(mem, FakeStore()).write_segment(profile_id=PROFILE_ID, transcript=t,
                                                 attempt=0, message_id="m")
    assert out.written and out.turns_written == 150 and out.event_id == "e1"


def test_oversize_text_is_truncated_to_create_event_limit(memory):
    mem, stub = memory
    messages = [msg("x" * 100_050, "2026-10-08T14:48:03Z")]
    stub.add_response("create_event", ok_event(), expected_create(
        [{"conversational": {"role": "USER", "content": {"text": "x" * 100_000}}}]))
    out = writer(mem, FakeStore()).write_segment(
        profile_id=PROFILE_ID, transcript=parse_conversation(conversation(messages)),
        attempt=0, message_id="m")
    assert out.written
