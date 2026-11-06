# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""In-memory SegmentStore with the same conditional semantics as acxd_memory.dedup."""

from __future__ import annotations

from acxd_memory import dedup


class FakeStore:
    def __init__(self, now: float = 1_800_000_000) -> None:
        self.now = now
        self.watermarks: dict[str, int] = {}
        self.segments: dict[str, dict] = {}
        self.get_calls = 0
        self.on_get = None  # hook to simulate a racing writer between reads

    def get_last_watermark(self, conversation_id):
        self.get_calls += 1
        if self.on_get:
            self.on_get(self, self.get_calls)
        return self.watermarks.get(conversation_id)

    def advance_watermark(self, conversation_id, watermark_ms):
        current = self.watermarks.get(conversation_id)
        if current is not None and not current < watermark_ms:
            return False
        self.watermarks[conversation_id] = watermark_ms
        return True

    def claim(self, segment, *, attempt, message_id):
        row = self.segments.get(segment)
        ok = (
            row is None
            or row["status"] == dedup.FAILED
            or (
                row["status"] == dedup.PENDING
                and (row["claimedAt"] < self.now - dedup.STALE_PENDING_SECONDS
                     or row["messageId"] == message_id)
            )
        )
        if ok:
            self.segments[segment] = {"status": dedup.PENDING, "attempt": attempt,
                                      "claimedAt": self.now, "messageId": message_id}
        return ok

    def transition(self, segment, *, attempt, status):
        row = self.segments.get(segment)
        if not row or row["status"] != dedup.PENDING or row["attempt"] != attempt:
            return False
        row["status"] = status
        return True

    def release(self, segment, *, attempt):
        return self.transition(segment, attempt=attempt, status=dedup.FAILED)
