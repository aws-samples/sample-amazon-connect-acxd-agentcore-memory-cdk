# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Write one stable ACXD conversation segment to AgentCore Memory (design §3.3, §3.4, §5).

Ported from the AI Agents sample (``connect_memory/writer.py``): the ``MemoryWriter``
shape (injected clients, ``WriteOutcome``, ``SkipReason`` constants, skip-before-dedup-
before-write order, ``CreateEvent`` error handling) and the ``conversational`` payload
mapping. Changed for ACXD:

- Identity arrives in the request (``profileId``) and is cross-checked against
  ``Conversation.userId`` instead of resolved through Q in Connect and contact attributes.
- Dedup is per segment with a conversation watermark (``acxd_memory.dedup``), and
  ``lastWatermark`` is re-read right before ``CreateEvent`` (design §3.4).
- ``eventTimestamp`` is the last message time of the segment, not ``now()``, so recap
  ordering stays right when retries delay the write.
- Optional restatement stripping of assistant turns (``acxd_memory.acxd_strip``, §4.2).
- Conversations of another ACXD application (``applicationId`` != configured) are skipped.
- Segments over 100 turns are split into several events (``CreateEvent`` payload limit).

Namespaces are not chosen here: the memory strategies fan the event out into
``connect/profile/{actorId}/semantic`` and ``.../recap``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol

from acxd_memory import dedup as dedup_mod
from acxd_memory.acxd_strip import strip_restated_memory
from acxd_memory.acxd_transcript import Transcript, to_conversational_payload, turns_after
from acxd_memory.identity import SkipReason as IdentitySkipReason
from acxd_memory.identity import is_valid_profile_id, log_id
from acxd_memory.reader import MemoryContext, format_memory_context, retrieve_memory_context

logger = logging.getLogger(__name__)

# CreateEvent limits (botocore bedrock-agentcore model): payload 0-100 entries,
# conversational text 1-100000 characters.
MAX_EVENT_ENTRIES = 100
MAX_TEXT_CHARS = 100_000


class SkipReason:
    """Why a segment produced no ``CreateEvent``. Emitted as the ``SkipReason`` dimension."""

    NO_PROFILE_ID = IdentitySkipReason.NO_PROFILE_ID
    MALFORMED_PROFILE_ID = IdentitySkipReason.MALFORMED_PROFILE_ID
    IDENTITY_MISMATCH = IdentitySkipReason.IDENTITY_MISMATCH
    APPLICATION_MISMATCH = "application_mismatch"
    NO_TIMESTAMPS = "no_timestamps"
    NO_NEW_MESSAGES = "no_new_messages"
    DUPLICATE = "duplicate"
    NO_CONVERSATIONAL_TURNS = "no_conversational_turns"


class MemoryClient(Protocol):
    """The subset of the ``bedrock-agentcore`` data-plane client this module calls."""

    def create_event(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def retrieve_memory_records(self, **kwargs: Any) -> Mapping[str, Any]: ...


class SegmentStoreLike(Protocol):
    def get_last_watermark(self, conversation_id: str) -> int | None: ...

    def advance_watermark(self, conversation_id: str, watermark_ms: int) -> bool: ...

    def claim(self, segment: str, *, attempt: int, message_id: str) -> bool: ...

    def transition(self, segment: str, *, attempt: int, status: str) -> bool: ...

    def release(self, segment: str, *, attempt: int) -> bool: ...


class CreateEventFailed(RuntimeError):
    """``CreateEvent`` raised. The claim was released; the caller lets SQS retry."""


@dataclass(frozen=True)
class WriteOutcome:
    conversation_id: str
    written: bool
    skip_reason: str | None = None
    event_id: str | None = None
    turns_written: int = 0
    turns_stripped: int = 0
    strip_skipped: bool = False
    """True when stripping was on but retrieval failed, so the write went out unstripped
    (``MemoryStripSkipped``)."""


@dataclass(frozen=True)
class MemoryWriterConfig:
    memory_id: str
    application_id: str | None = None
    """When set, only conversations of this ACXD application are written."""
    enable_stripping: bool = False
    strip_timeout_seconds: float = 3.0


class MemoryWriter:
    def __init__(
        self,
        *,
        config: MemoryWriterConfig,
        memory: MemoryClient,
        store: SegmentStoreLike,
        retrieve: Callable[..., MemoryContext] = retrieve_memory_context,
    ) -> None:
        self._config = config
        self._memory = memory
        self._store = store
        self._retrieve = retrieve

    def write_segment(
        self,
        *,
        profile_id: str,
        transcript: Transcript,
        attempt: int,
        message_id: str,
    ) -> WriteOutcome:
        conversation_id = transcript.conversation_id

        def skip(reason: str, **extra: Any) -> WriteOutcome:
            logger.info(
                "skip conversation=%s profile=%s reason=%s",
                conversation_id, log_id(profile_id), reason,
            )
            return WriteOutcome(conversation_id=conversation_id, written=False,
                                skip_reason=reason, **extra)

        if not profile_id:
            return skip(SkipReason.NO_PROFILE_ID)
        if not is_valid_profile_id(profile_id):
            return skip(SkipReason.MALFORMED_PROFILE_ID)
        # T3: a forged write cannot pair someone else's conversation with a chosen profile.
        # A conversation without a userId or applicationId is never written.
        if transcript.user_id is None or transcript.user_id != profile_id:
            return skip(SkipReason.IDENTITY_MISMATCH)
        if (
            self._config.application_id
            and transcript.application_id != self._config.application_id
        ):
            return skip(SkipReason.APPLICATION_MISMATCH)
        if transcript.watermark_ms is None:
            return skip(SkipReason.NO_TIMESTAMPS)

        last = self._store.get_last_watermark(conversation_id)
        new_turns = turns_after(transcript.turns, last)
        if last is not None and transcript.watermark_ms <= last:
            return skip(SkipReason.NO_NEW_MESSAGES)
        if not new_turns:
            return skip(SkipReason.NO_NEW_MESSAGES)

        segment = dedup_mod.segment_key(conversation_id, transcript.watermark_ms)
        if not self._store.claim(segment, attempt=attempt, message_id=message_id):
            return skip(SkipReason.DUPLICATE)

        turns_stripped = 0
        strip_skipped = False
        if self._config.enable_stripping:
            new_turns, turns_stripped, strip_skipped = self._strip(profile_id, new_turns)

        # Race guard (design §3.4): a racing copy may have written an overlapping segment since the first read.
        last_again = self._store.get_last_watermark(conversation_id)
        if last_again != last:
            new_turns = turns_after(new_turns, last_again)
            if not new_turns or (last_again is not None and transcript.watermark_ms <= last_again):
                self._store.transition(segment, attempt=attempt, status=dedup_mod.SUPERSEDED)
                return skip(SkipReason.DUPLICATE)

        # Truncate oversize turn text to the CreateEvent per-entry limit.
        new_turns = [
            t if len(t.text) <= MAX_TEXT_CHARS else replace(t, text=t.text[:MAX_TEXT_CHARS])
            for t in new_turns
        ]
        payload = to_conversational_payload(new_turns)
        if not payload:
            # Nothing to write (all turns stripped or empty); move the watermark anyway so
            # the next exit does not reconsider these messages.
            self._store.advance_watermark(conversation_id, transcript.watermark_ms)
            self._store.transition(segment, attempt=attempt, status=dedup_mod.EMPTY)
            return skip(SkipReason.NO_CONVERSATIONAL_TURNS, turns_stripped=turns_stripped,
                        strip_skipped=strip_skipped)

        # CreateEvent accepts at most 100 payload entries, so long segments are split. Each
        # chunk has its own deterministic clientToken (chunk 0 uses the plain segment token),
        # so a retry after a partial failure re-sends earlier chunks idempotently.
        written_turns = [t for t in new_turns if t.text and t.text.strip()]
        event_id = None
        for index, start in enumerate(range(0, len(payload), MAX_EVENT_ENTRIES)):
            chunk = payload[start : start + MAX_EVENT_ENTRIES]
            last_turn = written_turns[start + len(chunk) - 1]
            chunk_ts = transcript.watermark_ms if start + len(chunk) == len(payload) else last_turn.timestamp_ms
            token_key = segment if index == 0 else f"{segment}#{index}"
            try:
                response = self._memory.create_event(
                    memoryId=self._config.memory_id,
                    actorId=profile_id,
                    sessionId=conversation_id,
                    eventTimestamp=datetime.fromtimestamp(chunk_ts / 1000, tz=timezone.utc),
                    payload=chunk,
                    clientToken=dedup_mod.client_token(token_key),
                )
            except Exception as exc:  # noqa: BLE001 - released, then re-raised for SQS retry
                logger.error(
                    "CreateEvent failed conversation=%s profile=%s chunk=%d: %s",
                    conversation_id, log_id(profile_id), index, type(exc).__name__,
                )
                self._store.release(segment, attempt=attempt)
                raise CreateEventFailed(type(exc).__name__) from exc
            event_id = event_id or (response or {}).get("event", {}).get("eventId")

        if not self._store.advance_watermark(conversation_id, transcript.watermark_ms):
            logger.info("conversation=%s: a newer watermark is already stored", conversation_id)
        if not self._store.transition(segment, attempt=attempt, status=dedup_mod.WRITTEN):
            logger.warning("conversation=%s: segment claim was taken over before finish",
                           conversation_id)

        logger.info(
            "wrote conversation=%s profile=%s eventId=%s turns=%d",
            conversation_id, log_id(profile_id), event_id, len(payload),
        )
        return WriteOutcome(
            conversation_id=conversation_id,
            written=True,
            event_id=event_id,
            turns_written=len(payload),
            turns_stripped=turns_stripped,
            strip_skipped=strip_skipped,
        )

    def _strip(self, profile_id: str, turns: list) -> tuple[list, int, bool]:
        """Stateless re-retrieve, then strip assistant turns (§4.2). Fail-open."""
        try:
            context = self._retrieve(
                profile_id,
                memory=self._memory,
                memory_id=self._config.memory_id,
                timeout_seconds=self._config.strip_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - never block the write on stripping
            logger.warning("strip retrieval raised: %s", type(exc).__name__)
            return turns, 0, True
        if not context.available:
            failed = context.skip_reason == "retrieval_error"
            return turns, 0, failed
        corpus = list(context.records) + [format_memory_context(context)]
        result = strip_restated_memory(turns, corpus)
        return list(result.turns), result.turns_changed, False
