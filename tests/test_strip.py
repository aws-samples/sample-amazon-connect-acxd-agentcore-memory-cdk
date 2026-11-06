# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Restatement stripping (design §4.2): exact, partial, paraphrase, user exemption."""

from __future__ import annotations

from acxd_memory.acxd_strip import strip_restated_memory, strip_text, build_ngram_index
from acxd_memory.acxd_transcript import ASSISTANT, USER, Turn

RECORD = "The customer prefers window seats and travelled to Lisbon in August with two children."
FORMATTED = f"- {RECORD}"


def turn(role, text, i=0):
    return Turn(index=i, role=role, text=text, timestamp_ms=1000 + i)


def test_exact_restatement_in_assistant_turn_is_dropped():
    r = strip_restated_memory([turn(ASSISTANT, RECORD)], [RECORD, FORMATTED])
    assert r.turns == ()
    assert r.turns_changed == 1


def test_partial_restatement_removes_only_the_matching_span():
    text = (
        "Welcome back! I see the customer prefers window seats and travelled to Lisbon in "
        "August. Where would you like to go this time?"
    )
    r = strip_restated_memory([turn(ASSISTANT, text)], [RECORD])
    assert r.turns_changed == 1
    out = r.turns[0].text
    assert "Lisbon" not in out
    assert out.startswith("Welcome back! I see")
    assert "Where would you like to go this time?" in out


def test_casing_and_punctuation_do_not_prevent_a_match_but_original_text_is_kept():
    text = "OK. THE CUSTOMER PREFERS WINDOW SEATS, AND TRAVELLED TO LISBON IN AUGUST! Next: Porto."
    out = strip_text(text, build_ngram_index([RECORD]))
    assert out == "OK. Next: Porto."


def test_paraphrase_is_not_stripped():
    text = "Last time you flew to Lisbon in summer and liked sitting by the window, right?"
    r = strip_restated_memory([turn(ASSISTANT, text)], [RECORD])
    assert r.turns_changed == 0
    assert r.turns[0].text == text


def test_seven_word_overlap_is_below_threshold():
    text = "So the customer prefers window seats and travelled somewhere nice."
    r = strip_restated_memory([turn(ASSISTANT, text)], [RECORD])
    assert r.turns_changed == 0


def test_user_turns_are_never_stripped():
    r = strip_restated_memory([turn(USER, RECORD)], [RECORD])
    assert r.turns[0].text == RECORD
    assert r.turns_changed == 0


def test_raw_context_token_is_removed():
    text = "Sure. memoryContext:NLX.Context|ACXDMEM-CANARY-7731 Let me check the hotels for you now."
    r = strip_restated_memory([turn(ASSISTANT, text)], [])
    assert "NLX.Context" not in r.turns[0].text
    assert r.turns_changed == 1


def test_turn_with_fewer_than_four_words_left_is_dropped():
    text = f"Right, {RECORD}"
    r = strip_restated_memory([turn(ASSISTANT, text)], [RECORD])
    assert r.turns == ()


def test_corpus_includes_formatted_context():
    text = "- The customer prefers window seats and travelled to Lisbon in August. Anything else today?"
    r = strip_restated_memory([turn(ASSISTANT, text)], [FORMATTED])
    assert r.turns_changed == 1
