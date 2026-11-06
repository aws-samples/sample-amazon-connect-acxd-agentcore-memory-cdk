# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Identity rules and AgentCore Memory namespaces (design §3.3, §5).

In ACXD the Customer Profiles
``profileId`` arrives in the request as ``{System.userId}`` (design §5).

No profile, no memory. A missing or malformed ``profileId`` is never replaced by a
synthetic key (phone number, contactId, hash): that would merge unrelated people or split
one person across keys.
"""

from __future__ import annotations

import hashlib
import re

# A Customer Profiles profileId is 32 lowercase hex characters. That also satisfies the
# AgentCore actorId pattern [a-zA-Z0-9][a-zA-Z0-9-_/]*(?::[a-zA-Z0-9-_/]+)*[a-zA-Z0-9-_/]*.
_PROFILE_ID_LEN = 32

# ACXD conversationId becomes the AgentCore sessionId, whose pattern is
# [a-zA-Z0-9][a-zA-Z0-9-_]* with max length 100 (botocore bedrock-agentcore model).
_CONVERSATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")


class SkipReason:
    """Why a request produced no memory read or write. Emitted as a metric dimension."""

    NO_PROFILE_ID = "no_profile_id"
    MALFORMED_PROFILE_ID = "malformed_profile_id"
    IDENTITY_MISMATCH = "identity_mismatch"


def is_valid_profile_id(value: str | None) -> bool:
    """True when the value looks like a Customer Profiles profileId (32 lowercase hex)."""
    if not value or len(value) != _PROFILE_ID_LEN:
        return False
    return all(c in "0123456789abcdef" for c in value.lower())


def is_valid_conversation_id(value: str | None) -> bool:
    """True when the value is usable as an AgentCore ``sessionId`` (and as a dedup key)."""
    return bool(value) and bool(_CONVERSATION_ID_PATTERN.match(value))


def log_id(value: str | None) -> str:
    """Short, stable, non-reversible form of an identifier for log lines (threat T5).

    Logs carry ids only, and the profileId only in this hashed form.
    """
    if not value:
        return "-"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def semantic_namespace(actor_id: str) -> str:
    """Cross-conversation durable-fact namespace."""
    return f"connect/profile/{actor_id}/semantic"


def recap_namespace(actor_id: str) -> str:
    """Conversation-recap namespace (custom strategy), flat so it is retrievable without a
    prior session id."""
    return f"connect/profile/{actor_id}/recap"


def read_namespaces(actor_id: str) -> tuple[str, ...]:
    """Namespaces the read path queries, one exact-match call each."""
    return (semantic_namespace(actor_id), recap_namespace(actor_id))
