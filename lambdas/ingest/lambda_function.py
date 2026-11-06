# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``POST /memory/write``: validate and enqueue an exit write (design §3.1, §3.2).

Request body (API Gateway model, ``additionalProperties: false``):
``{"profileId": "{System.userId}", "conversationId": "{System.conversationId}"}``. IDs only:
the writer fetches the transcript from ACXD (Option B), so no request text can reach memory.

Responses (the ACXD node continues on success, failure and timeout alike):

- ``202 {"accepted": true}``: enqueued with ``DelaySeconds = 120`` for the writer.
- ``202 {"accepted": false}``: anonymous or malformed profile, nothing to write
  (``WriteSkipped``, ``SkipReason=no_profile_id|malformed_profile_id``).
- ``400``: malformed body, or ``x-nlx-conversationid`` / ``x-nlx-userid`` present and
  different from the body (``RequestRejected``). The header check catches a misconfigured
  flow; it is not a security control (a holder of the write secret can set any header).
- ``503``: the enqueue failed (``EnqueueFailed``).

Answers in tens of milliseconds.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Mapping

import boto3

from acxd_memory import metrics
from acxd_memory.identity import (
    SkipReason,
    is_valid_conversation_id,
    is_valid_profile_id,
    log_id,
)
from acxd_memory.state_machine import INITIAL_DELAY_SECONDS, WorkItem

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_sqs_client = None


def _sqs():
    global _sqs_client
    if _sqs_client is None:
        _sqs_client = boto3.client("sqs")
    return _sqs_client


if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
    # Build the client during init so the first request does not pay for it.
    _sqs()


def _respond(status: int, body: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _reject(reason: str) -> dict[str, Any]:
    logger.info("reject reason=%s", reason)
    metrics.emit("RequestRejected", dimensions={"Reason": reason})
    return _respond(400, {"accepted": False, "error": reason})


def _header(headers: Mapping[str, Any] | None, name: str) -> str | None:
    for key, value in (headers or {}).items():
        if key.lower() == name:
            return value if isinstance(value, str) else None
    return None


def lambda_handler(event: Mapping[str, Any], context: Any, *, sqs=None) -> dict[str, Any]:
    body = event.get("body")
    if not isinstance(body, str) or event.get("isBase64Encoded"):
        return _reject("body")
    try:
        parsed = json.loads(body)
    except ValueError:
        return _reject("body")
    if not isinstance(parsed, dict):
        return _reject("body")

    profile_id = parsed.get("profileId")
    conversation_id = parsed.get("conversationId")
    if not isinstance(conversation_id, str) or not is_valid_conversation_id(conversation_id):
        return _reject("conversation_id")
    if profile_id is not None and not isinstance(profile_id, str):
        return _reject("profile_id")

    headers = event.get("headers")
    header_conversation = _header(headers, "x-nlx-conversationid")
    header_user = _header(headers, "x-nlx-userid")
    if header_conversation is not None and header_conversation != conversation_id:
        return _reject("header_mismatch")
    if header_user is not None and profile_id and header_user != profile_id:
        return _reject("header_mismatch")

    if not profile_id:
        metrics.emit("WriteSkipped", dimensions={"SkipReason": SkipReason.NO_PROFILE_ID})
        return _respond(202, {"accepted": False})
    if not is_valid_profile_id(profile_id):
        metrics.emit("WriteSkipped", dimensions={"SkipReason": SkipReason.MALFORMED_PROFILE_ID})
        return _respond(202, {"accepted": False})

    item = WorkItem(profile_id=profile_id, conversation_id=conversation_id)
    try:
        (sqs or _sqs()).send_message(
            QueueUrl=os.environ["QUEUE_URL"],
            MessageBody=json.dumps(item.to_body()),
            DelaySeconds=INITIAL_DELAY_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("enqueue failed conversation=%s: %s", conversation_id, type(exc).__name__)
        metrics.emit("EnqueueFailed")
        return _respond(503, {"accepted": False})

    logger.info("enqueued conversation=%s profile=%s", conversation_id, log_id(profile_id))
    metrics.emit("WriteAccepted")
    return _respond(202, {"accepted": True})
