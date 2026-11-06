# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``POST /memory/read``: conversation-start memory for an ACXD data request (design §4).

Request body (API Gateway model, ``additionalProperties: false``): ``{"profileId": "..."}``,
where ACXD sends ``{System.userId}`` (the Customer Profiles ``profileId``).

Response is always ``200 {"memoryContext": "<text or empty>", "hasMemory": bool}``. Every
failure (no profile, malformed body, retrieval error or timeout) returns the empty form, so
the conversation never waits on memory. The ACXD node's failure and timeout paths also
continue with ``memoryContext`` empty.

Time budget: ``retrieve_memory_context`` runs both namespaces in parallel with a shared
``READ_BUDGET_SECONDS`` (1.5 s) budget; the botocore client uses short timeouts and one
attempt. Lambda timeout is 4 s (design §4), under the 5 s ACXD data request
default.

Replaces the AI Agents reader handler (Connect flow module + ``UpdateSessionData``); the
retrieval and formatting code is reused unchanged from ``acxd_memory.reader``.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Mapping

import boto3
from botocore.config import Config

from acxd_memory import metrics
from acxd_memory.identity import log_id
from acxd_memory.reader import format_memory_context, retrieve_memory_context

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_memory_client = None


def _memory():
    global _memory_client
    if _memory_client is None:
        budget = float(os.environ.get("READ_BUDGET_SECONDS", "1.5"))
        _memory_client = boto3.client(
            "bedrock-agentcore",
            config=Config(
                connect_timeout=1,
                read_timeout=budget,
                retries={"max_attempts": 1, "mode": "standard"},
            ),
        )
    return _memory_client


if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
    # Build the client during init so the first (customer-facing) request does not pay for it.
    _memory()


def _response(memory_context: str, has_memory: bool) -> dict[str, Any]:
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"memoryContext": memory_context, "hasMemory": has_memory}),
    }


def _profile_id(event: Mapping[str, Any]) -> str | None:
    body = event.get("body")
    if not isinstance(body, str) or event.get("isBase64Encoded"):
        return None
    try:
        parsed = json.loads(body)
    except ValueError:
        return None
    value = parsed.get("profileId") if isinstance(parsed, dict) else None
    return value if isinstance(value, str) else None


def lambda_handler(event: Mapping[str, Any], context: Any, *, memory=None) -> dict[str, Any]:
    profile_id = _profile_id(event)
    try:
        result = retrieve_memory_context(
            profile_id,
            memory=memory or _memory(),
            memory_id=os.environ["MEMORY_ID"],
            timeout_seconds=float(os.environ.get("READ_BUDGET_SECONDS", "1.5")),
        )
    except Exception as exc:  # noqa: BLE001 - never fail the conversation on memory
        logger.error("read failed profile=%s: %s", log_id(profile_id), type(exc).__name__)
        metrics.emit("ReadSkipped", dimensions={"SkipReason": "unexpected_error"})
        return _response("", False)

    text = format_memory_context(result)
    if result.available:
        metrics.emit("ReadHit")
        metrics.emit("ReadRecords", len(result.records))
    else:
        metrics.emit("ReadSkipped", dimensions={"SkipReason": result.skip_reason or "unknown"})
    logger.info(
        "read profile=%s hasMemory=%s records=%d reason=%s",
        log_id(profile_id), result.available, len(result.records), result.skip_reason,
    )
    return _response(text, result.available)
