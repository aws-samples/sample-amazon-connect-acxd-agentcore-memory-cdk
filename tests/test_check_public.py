# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""scripts/check_public.py: pre-publication scan for real ids and internal references."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "check_public", Path(__file__).resolve().parents[1] / "scripts" / "check_public.py"
)
check_public = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_public)


def kinds(text: str, deny: list[str] | None = None) -> list[str]:
    return [kind for _, kind, _ in check_public.scan_text(text, deny or [])]


@pytest.mark.parametrize(
    "text",
    [
        "arn:aws:bedrock-agentcore:eu-central-1:111122223333:memory/acxdmem_memory-AbCdEf1234",
        "account 111111111111, region eu-central-1",
        "workspace 00000000-0000-4000-8000-000000000000",
        "node acd05000-0000-4000-8000-000000000004",
        "contact 11111111-2222-4333-8444-555555555555",
        "profile aacd000000000000000000000000c0de",
        "call +15555550100",
        "https://abcdef1234.execute-api.eu-central-1.amazonaws.com/v1/memory/read",
        "See https://docs.aws.amazon.com/connect/latest/adminguide/agentic-cx-block.html",
        "watermark 0000001791500187977, epoch ms 1791500187977",
        "opensource-codeofconduct@amazon.com",
    ],
)
def test_placeholders_and_public_refs_pass(text):
    assert kinds(text) == []


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("account 210987654321", "account id"),
        ("arn:aws:iam::210987654321:role/x", "arn account"),
        ("contact 9fd12ed1-6abd-418e-b93b-4971537da6a9", "uuid"),
        ("profile 3a7c9e1f0b2d4c6e8a0b1c2d3e4f5a6b", "profile id"),
        ("https://q1w2e3r4t5.execute-api.eu-central-1.amazonaws.com", "api gateway id"),
        ("memory acxdmem_memory-Zx81Qw03Lp", "memory id"),
        ("call +442079460000", "phone number"),
        # Built from parts so the repository itself never contains the internal URL.
        ("see https://" + "w." + "amazon.com/bin/view/X", "internal host"),
        ("gitlab.aws.dev/group/repo", "internal host"),
        ("reviewed by Fable", "internal term"),
        ("blocked until Gate P", "internal term"),
        ("done in task 3cda336e", "internal term"),
        ("/Users/someone/repo", "internal term"),
        ("scanned with ProtoShield", "internal term"),
        ("per Aristotle guidance", "internal term"),
        ("the Golden Path for Lambda", "internal term"),
    ],
)
def test_real_ids_and_internal_refs_fail(text, kind):
    assert kind in kinds(text)


def test_denied_literal():
    assert kinds("stage v1 of api zz9", deny=["zz9"]) == ["denied literal"]


def test_repository_is_clean():
    """The published tree itself passes (also run by `make scan`)."""
    assert check_public.main(["--root", str(Path(__file__).resolve().parents[1])]) == 0
