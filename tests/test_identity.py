# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
from __future__ import annotations

import pytest

from acxd_memory.identity import (
    is_valid_conversation_id,
    is_valid_profile_id,
    log_id,
    read_namespaces,
)
from tests.helpers import CONVERSATION_ID, PROFILE_ID


def test_profile_id_rule_ported_from_ai_agents_sample():
    assert is_valid_profile_id(PROFILE_ID)
    assert not is_valid_profile_id("")
    assert not is_valid_profile_id(None)
    assert not is_valid_profile_id(PROFILE_ID[:-1])
    assert not is_valid_profile_id("z" * 32)


@pytest.mark.parametrize("value, ok", [
    (CONVERSATION_ID, True), ("a", True), ("-a", False), ("a/b", False), ("a" * 101, False), ("", False),
])
def test_conversation_id_is_a_valid_session_id(value, ok):
    assert is_valid_conversation_id(value) is ok


def test_namespaces_shared_with_ai_agents_variant():
    assert read_namespaces(PROFILE_ID) == (
        f"connect/profile/{PROFILE_ID}/semantic",
        f"connect/profile/{PROFILE_ID}/recap",
    )


def test_log_id_hides_profile():
    assert PROFILE_ID not in log_id(PROFILE_ID)
    assert log_id(PROFILE_ID) == log_id(PROFILE_ID)
    assert log_id(None) == "-"
