# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""SQS consumer: fetch the ACXD transcript, wait until stable, write it (design §3.2-§3.4).

Event source: the ``acxdmem`` work queue (standard), batch size 1, concurrency capped with
the event-source mapping ``maxConcurrency``. One message = one work item
(``acxd_memory.state_machine.WorkItem``).

Per message:

1. Invoke the Node.js fetch step (``lambdas/fetch``, official ``amazon-connect-acxd-sdk``
   ``GetConversation``) synchronously; it returns ``{status, conversation?}``.
2. ``state_machine.decide`` picks re-queue (a new ``SendMessage`` with ``DelaySeconds``,
   then a normal return so this message is deleted), write, or give up.
3. Write: ``acxd_memory.writer.MemoryWriter.write_segment`` (identity cross-check,
   watermark filter, segment claim, optional stripping, ``CreateEvent``).

Only hard failures raise, so SQS redelivers and, after ``maxReceiveCount`` 3, moves the
message to the DLQ: fetch-step crash or configuration error (API key secret unreadable or
malformed, reported as a ``FunctionError``), ACXD auth/validation errors, a
``CreateEvent`` error, or an unexpected exception. A transcript that never indexes is not a
hard failure.

No transcript or memory text is logged; ids only, profileId hashed (threat T5).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Mapping

import boto3
from botocore.config import Config

from acxd_memory import metrics
from acxd_memory.acxd_transcript import parse_conversation
from acxd_memory.dedup import SegmentStore
from acxd_memory.identity import log_id
from acxd_memory.state_machine import (
    GIVE_UP,
    NOT_FOUND,
    OK,
    REQUEUE,
    RETRYABLE,
    FetchOutcome,
    MalformedWorkItem,
    WorkItem,
    decide,
)
from acxd_memory.writer import MemoryWriter, MemoryWriterConfig

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# The fetch step times out at 10 s. One attempt, 12 s read timeout: a hung invoke raises a
# classified botocore error well inside the writer's 30 s timeout instead of timing the
# writer out. Retries come from the state machine and SQS redelivery, not from here.
FETCH_INVOKE_CONFIG = Config(
    connect_timeout=3, read_timeout=12, retries={"total_max_attempts": 1}
)


class FetchStepError(RuntimeError):
    """The fetch step failed in a way a retry with the same config will not fix soon."""


_clients: dict[str, Any] = {}


def _client(name: str, **kwargs: Any):
    if name not in _clients:
        _clients[name] = boto3.client(name, **kwargs)
    return _clients[name]


def invoke_fetch(lambda_client, function_name: str, conversation_id: str) -> Mapping[str, Any]:
    response = lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps({"conversationId": conversation_id}).encode("utf-8"),
    )
    raw = response["Payload"].read()
    if response.get("FunctionError"):
        raise FetchStepError(f"fetch step error: {response['FunctionError']}")
    result = json.loads(raw or b"{}")
    if not isinstance(result, dict) or not isinstance(result.get("status"), int):
        raise FetchStepError("fetch step returned no status")
    return result


def classify(status: int) -> str:
    if status == 200:
        return OK
    if status == 404:
        return NOT_FOUND
    if status == 429 or status >= 500:
        return RETRYABLE
    raise FetchStepError(f"ACXD GetConversation returned {status}")


class Deps:
    """AWS clients and config, built lazily so unit tests can pass their own."""

    def __init__(self, *, lambda_client=None, sqs=None, memory=None, dynamodb=None,
                 env: Mapping[str, str] | None = None) -> None:
        env = env if env is not None else os.environ
        self.queue_url = env["QUEUE_URL"]
        self.fetch_function = env["FETCH_FUNCTION_NAME"]
        self.lambda_client = lambda_client or _client("lambda", config=FETCH_INVOKE_CONFIG)
        self.sqs = sqs or _client("sqs")
        store = SegmentStore(dynamodb or _client("dynamodb"), env["DEDUP_TABLE_NAME"])
        self.writer = MemoryWriter(
            config=MemoryWriterConfig(
                memory_id=env["MEMORY_ID"],
                application_id=env.get("ACXD_APPLICATION_ID") or None,
                enable_stripping=str(env.get("ENABLE_STRIPPING", "false")).lower() == "true",
            ),
            memory=memory or _client("bedrock-agentcore"),
            store=store,
        )


def process(record: Mapping[str, Any], deps: Deps) -> str:
    """Handle one SQS record. Returns a short outcome label (for tests and logs)."""
    message_id = record.get("messageId", "")
    try:
        item = WorkItem.from_body(json.loads(record.get("body") or "null"))
    except (ValueError, MalformedWorkItem) as exc:
        logger.error("malformed work item message=%s: %s", message_id, exc)
        metrics.emit("MalformedMessage")
        return "malformed"

    result = invoke_fetch(deps.lambda_client, deps.fetch_function, item.conversation_id)
    kind = classify(result["status"])
    transcript = None
    if kind == OK:
        transcript = parse_conversation(result.get("conversation") or {})
        outcome = FetchOutcome(kind=OK, watermark=transcript.watermark_ms,
                               count=transcript.message_count)
    else:
        outcome = FetchOutcome(kind=kind)
        metrics.emit("FetchRetry", dimensions={"Status": str(result["status"])})

    decision = decide(item, outcome)
    if decision.action == REQUEUE:
        deps.sqs.send_message(
            QueueUrl=deps.queue_url,
            MessageBody=json.dumps(decision.next_item.to_body()),
            DelaySeconds=decision.delay_seconds,
        )
        logger.info(
            "requeue conversation=%s phase=%s attempt=%d delay=%ds status=%d",
            item.conversation_id, decision.next_item.phase, decision.next_item.attempt,
            decision.delay_seconds, result["status"],
        )
        return "requeue"

    if decision.metric:
        metrics.emit(decision.metric)
    if decision.action == GIVE_UP:
        logger.info("give up conversation=%s reason=%s", item.conversation_id, decision.metric)
        return "give_up"

    outcome_w = deps.writer.write_segment(
        profile_id=item.profile_id,
        transcript=transcript,
        attempt=item.attempt,
        message_id=message_id,
    )
    if outcome_w.turns_stripped:
        metrics.emit("MemoryRestatementStripped", outcome_w.turns_stripped)
    if outcome_w.strip_skipped:
        metrics.emit("MemoryStripSkipped")
    if outcome_w.written:
        metrics.emit("WriteSucceeded")
        metrics.emit("TurnsWritten", outcome_w.turns_written)
        return "written"
    metrics.emit("WriteSkipped", dimensions={"SkipReason": outcome_w.skip_reason or "unknown"})
    logger.info("skip conversation=%s profile=%s reason=%s", item.conversation_id,
                log_id(item.profile_id), outcome_w.skip_reason)
    return "skipped"


_deps: Deps | None = None


def lambda_handler(event: Mapping[str, Any], context: Any) -> None:
    global _deps
    if _deps is None:
        _deps = Deps()
    # Batch size is 1; an exception fails the batch, so SQS redelivers that one message.
    for record in event.get("Records", []):
        process(record, _deps)
