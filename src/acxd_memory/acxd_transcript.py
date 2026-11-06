# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""ACXD ``GetConversation`` messages -> ordered turns + segment watermark (design §3.2-§3.4).

Input is the conversation object the Node fetch step returns: the
``amazon-connect-acxd-sdk`` ``GetConversation`` output, projected to the fields used here
(``conversationId``, ``userId``, ``applicationId``, ``messages[]`` with ``isApplication``, ``text``,
``timestamp``, ``isStructured``).

Mapping rules (design §3.3):

- ``isApplication: false`` -> ``USER``, ``isApplication: true`` -> ``ASSISTANT``.
- Messages with empty ``text`` and messages with ``isStructured: true`` are not turns
  (``isStructured`` dropped defensively).
- Order is the order of ``messages[]``; equal timestamps are tie-broken by list index.

Watermark: epoch milliseconds of the **last** message in the fetched list, whether or not
that message becomes a turn. The timestamp wire format is a string in the SDK model, so ``parse_timestamp``
accepts ISO 8601 and numeric epoch seconds or milliseconds. A message whose timestamp does
not parse inherits the previous message's value (index still breaks ties).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

USER = "USER"
ASSISTANT = "ASSISTANT"

# Numeric epochs above this are milliseconds, below are seconds (1e11 s is year 5138).
_EPOCH_MS_THRESHOLD = 1e11


@dataclass(frozen=True)
class Turn:
    index: int
    """Position in ``messages[]``; tie-breaker for equal timestamps."""
    role: str
    """``USER`` or ``ASSISTANT`` (AgentCore ``conversational`` roles)."""
    text: str
    timestamp_ms: int


@dataclass(frozen=True)
class Transcript:
    conversation_id: str
    user_id: str | None
    application_id: str | None
    turns: tuple[Turn, ...]
    message_count: int
    """Number of messages in ``messages[]`` (not only turns); part of the stability check."""
    watermark_ms: int | None
    """Epoch ms of the last message; None when the conversation has no parseable timestamp."""


class TranscriptError(ValueError):
    """The conversation object does not have the expected shape."""


def parse_timestamp(value: Any) -> int | None:
    """Return epoch milliseconds for an ISO 8601 string or a numeric epoch, else None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp() * 1000)
    if not math.isfinite(number) or number < 0:
        return None
    return int(number if number >= _EPOCH_MS_THRESHOLD else number * 1000)


def parse_conversation(conversation: Mapping[str, Any]) -> Transcript:
    """Build a ``Transcript`` from a projected ``GetConversation`` response."""
    if not isinstance(conversation, Mapping):
        raise TranscriptError("conversation is not an object")
    conversation_id = conversation.get("conversationId")
    if not isinstance(conversation_id, str) or not conversation_id:
        raise TranscriptError("conversation has no conversationId")
    messages = conversation.get("messages")
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        raise TranscriptError("conversation has no messages list")

    user_id = conversation.get("userId")
    user_id = user_id if isinstance(user_id, str) and user_id else None
    application_id = conversation.get("applicationId")
    application_id = application_id if isinstance(application_id, str) and application_id else None

    turns: list[Turn] = []
    last_ts: int | None = None
    for index, message in enumerate(messages):
        if not isinstance(message, Mapping):
            continue
        ts = parse_timestamp(message.get("timestamp"))
        if ts is None:
            ts = last_ts
        else:
            last_ts = ts if last_ts is None else max(last_ts, ts)
            ts = last_ts
        text = message.get("text")
        if message.get("isStructured") is True:
            continue
        if not isinstance(text, str) or not text.strip():
            continue
        if ts is None:
            # No timestamp seen yet; cannot place this turn against a watermark.
            continue
        role = ASSISTANT if message.get("isApplication") is True else USER
        turns.append(Turn(index=index, role=role, text=text, timestamp_ms=ts))

    return Transcript(
        conversation_id=conversation_id,
        user_id=user_id,
        application_id=application_id,
        turns=tuple(turns),
        message_count=len(messages),
        watermark_ms=last_ts,
    )


def turns_after(turns: Sequence[Turn], watermark_ms: int | None) -> list[Turn]:
    """Turns strictly newer than ``watermark_ms`` (all turns when it is None), design §3.4."""
    if watermark_ms is None:
        return list(turns)
    return [t for t in turns if t.timestamp_ms > watermark_ms]


def to_conversational_payload(turns: Sequence[Turn]) -> list[dict[str, Any]]:
    """Map turns to ``CreateEvent`` ``payload`` entries.

    Output shape: ``{"conversational": {"role", "content": {"text"}}}``.
    """
    payload: list[dict[str, Any]] = []
    for turn in turns:
        if not turn.text or not turn.text.strip():
            continue
        payload.append(
            {"conversational": {"role": turn.role, "content": {"text": turn.text}}}
        )
    return payload
