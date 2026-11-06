# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
from __future__ import annotations

import pytest

from acxd_memory.acxd_transcript import (
    ASSISTANT,
    USER,
    TranscriptError,
    parse_conversation,
    parse_timestamp,
    to_conversational_payload,
    turns_after,
)
from tests.helpers import (
    APPLICATION_ID,
    CONVERSATION_ID,
    PROFILE_ID,
    SAMPLE_MESSAGES,
    SAMPLE_WATERMARK,
    conversation,
    msg,
)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2026-10-08T14:48:03.000Z", SAMPLE_WATERMARK),
        ("2026-10-08T14:48:03+00:00", SAMPLE_WATERMARK),
        ("2026-10-08T14:48:03", SAMPLE_WATERMARK),  # naive -> UTC
        (SAMPLE_WATERMARK, SAMPLE_WATERMARK),  # epoch ms
        (SAMPLE_WATERMARK / 1000, SAMPLE_WATERMARK),  # epoch s
        (str(SAMPLE_WATERMARK), SAMPLE_WATERMARK),
        ("not a time", None),
        ("", None),
        (None, None),
        (True, None),
        (-1, None),
    ],
)
def test_parse_timestamp(value, expected):
    assert parse_timestamp(value) == expected


def test_maps_roles_and_watermark():
    t = parse_conversation(conversation(SAMPLE_MESSAGES))
    assert t.conversation_id == CONVERSATION_ID
    assert t.user_id == PROFILE_ID
    assert t.application_id == APPLICATION_ID
    assert [x.role for x in t.turns] == [ASSISTANT, USER, ASSISTANT, USER, ASSISTANT]
    assert t.message_count == 5
    assert t.watermark_ms == SAMPLE_WATERMARK


def test_drops_empty_and_structured_but_keeps_them_in_count_and_watermark():
    messages = [
        msg("hello", "2026-10-08T14:47:44Z"),
        msg("", "2026-10-08T14:47:45Z", app=True),
        msg("card", "2026-10-08T14:47:46Z", app=True, isStructured=True),
        msg("   ", "2026-10-08T14:48:03Z", app=True),
    ]
    t = parse_conversation(conversation(messages))
    assert [x.text for x in t.turns] == ["hello"]
    assert t.message_count == 4
    assert t.watermark_ms == SAMPLE_WATERMARK


def test_equal_timestamps_keep_list_order():
    messages = [msg("a", "2026-10-08T14:48:03Z"), msg("b", "2026-10-08T14:48:03Z", app=True)]
    t = parse_conversation(conversation(messages))
    assert [(x.index, x.text) for x in t.turns] == [(0, "a"), (1, "b")]


def test_unparseable_timestamp_inherits_previous():
    messages = [msg("a", "2026-10-08T14:48:03Z"), msg("b", "garbage", app=True)]
    t = parse_conversation(conversation(messages))
    assert [x.timestamp_ms for x in t.turns] == [SAMPLE_WATERMARK, SAMPLE_WATERMARK]


def test_no_timestamps_means_no_watermark():
    t = parse_conversation(conversation([msg("a", None)]))
    assert t.watermark_ms is None
    assert t.turns == ()


def test_missing_user_id_is_none():
    t = parse_conversation(conversation(SAMPLE_MESSAGES, user_id=None))
    assert t.user_id is None


@pytest.mark.parametrize(
    "bad",
    [None, [], {"messages": []}, {"conversationId": "x"}, {"conversationId": "x", "messages": "s"}],
)
def test_rejects_bad_shapes(bad):
    with pytest.raises(TranscriptError):
        parse_conversation(bad)


def test_turns_after_watermark_is_strict():
    t = parse_conversation(conversation(SAMPLE_MESSAGES))
    cut = t.turns[2].timestamp_ms
    assert [x.text for x in turns_after(t.turns, cut)] == [t.turns[3].text, t.turns[4].text]
    assert turns_after(t.turns, None) == list(t.turns)


def test_payload_shape_matches_create_event():
    t = parse_conversation(conversation(SAMPLE_MESSAGES[:2]))
    assert to_conversational_payload(t.turns) == [
        {"conversational": {"role": "ASSISTANT", "content": {"text": SAMPLE_MESSAGES[0]["text"]}}},
        {"conversational": {"role": "USER", "content": {"text": SAMPLE_MESSAGES[1]["text"]}}},
    ]
