# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Lambda handlers: authorizer, read, ingest, writer (Stubber for AWS calls)."""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone

import pytest
from botocore.stub import ANY, Stubber

from acxd_memory import dedup
from tests.conftest import load_lambda
from tests.fakes import FakeStore
from tests.helpers import (
    CONVERSATION_ID,
    MEMORY_ID,
    PROFILE_ID,
    SAMPLE_MESSAGES,
    SAMPLE_WATERMARK,
    client,
    conversation,
)

API = "arn:aws:execute-api:eu-central-1:111111111111:abc123"
READ_ARN = f"{API}/v1/POST/memory/read"
WRITE_ARN = f"{API}/v1/POST/memory/write"
SECRET_ARN = "arn:aws:secretsmanager:eu-central-1:111111111111:secret:acxdmem-endpoint-secret-read-AbCdEf"


# --- authorizer ----------------------------------------------------------------------


@pytest.fixture
def authorizer(monkeypatch):
    mod = load_lambda("authorizer")
    sm = client("secretsmanager")
    stub = Stubber(sm)
    stub.activate()
    monkeypatch.setattr(mod, "_secrets_client", sm)
    mod._cache.update(values=(), expires=0.0)
    monkeypatch.setenv("SECRET_ARN", SECRET_ARN)
    monkeypatch.setenv("EXPECTED_ROUTE", "POST/memory/read")
    yield mod, stub
    stub.deactivate()


def _secret(stub, current="s3cret-current", previous="s3cret-previous"):
    stub.add_response("get_secret_value", {"SecretString": current},
                      {"SecretId": SECRET_ARN, "VersionStage": "AWSCURRENT"})
    if previous is None:
        stub.add_client_error("get_secret_value", "ResourceNotFoundException",
                              expected_params={"SecretId": SECRET_ARN, "VersionStage": "AWSPREVIOUS"})
    else:
        stub.add_response("get_secret_value", {"SecretString": previous},
                          {"SecretId": SECRET_ARN, "VersionStage": "AWSPREVIOUS"})


def _auth_event(secret, arn=READ_ARN, header="x-acxdmem-secret"):
    return {"type": "REQUEST", "methodArn": arn, "headers": {header: secret} if secret else {}}


def _effect(result):
    stmt = result["policyDocument"]["Statement"]
    assert len(stmt) == 1
    return stmt[0]["Effect"], stmt[0]["Resource"]


def test_authorizer_allows_current_scoped_to_method_arn(authorizer):
    mod, stub = authorizer
    _secret(stub)
    assert _effect(mod.lambda_handler(_auth_event("s3cret-current"), None)) == ("Allow", READ_ARN)


def test_authorizer_allows_previous_during_rotation_and_header_case(authorizer):
    mod, stub = authorizer
    _secret(stub)
    event = _auth_event("s3cret-previous", header="X-Acxdmem-Secret")
    assert _effect(mod.lambda_handler(event, None)) == ("Allow", READ_ARN)


def test_authorizer_denies_wrong_and_missing_secret(authorizer):
    mod, stub = authorizer
    _secret(stub, previous=None)
    assert _effect(mod.lambda_handler(_auth_event("nope"), None))[0] == "Deny"
    assert _effect(mod.lambda_handler(_auth_event(None), None))[0] == "Deny"  # cached secret


def test_authorizer_denies_other_route_even_with_valid_secret(authorizer):
    """A read-route authorizer must never allow the write route (per-route secrets, §6)."""
    mod, stub = authorizer
    effect, resource = _effect(mod.lambda_handler(_auth_event("s3cret-current", arn=WRITE_ARN), None))
    assert effect == "Deny"
    assert resource == WRITE_ARN


def test_authorizer_never_returns_wildcard_allow(authorizer):
    mod, stub = authorizer
    _secret(stub)
    result = mod.lambda_handler(_auth_event("s3cret-current"), None)
    assert all(s["Resource"] != "*" for s in result["policyDocument"]["Statement"])


def test_authorizer_init_prefetch_warms_cache_and_never_raises(authorizer):
    mod, stub = authorizer
    _secret(stub)
    mod._warm()
    assert mod._cache["values"] == ("s3cret-current", "s3cret-previous")
    # The handler then answers from the cache, without another Secrets Manager call.
    assert _effect(mod.lambda_handler(_auth_event("s3cret-current"), None))[0] == "Allow"
    stub.assert_no_pending_responses()
    mod._cache.update(values=(), expires=0.0)
    stub.add_client_error("get_secret_value", "AccessDeniedException")
    mod._warm()  # logged, not raised: a failed init fetch must not break the cold start
    assert mod._cache["values"] == ()


def test_constant_time_compare_helper():
    mod = load_lambda("authorizer")
    assert mod.is_authorized("a", ("b", "a"))
    assert not mod.is_authorized("", ("",))
    assert not mod.is_authorized(None, ("a",))


# --- read ----------------------------------------------------------------------------


@pytest.fixture
def read(monkeypatch):
    monkeypatch.setenv("MEMORY_ID", MEMORY_ID)
    monkeypatch.setenv("READ_BUDGET_SECONDS", "1.5")
    mod = load_lambda("read")
    mem = client("bedrock-agentcore")
    with Stubber(mem) as stub:
        yield mod, mem, stub


def _records(*texts):
    return {"memoryRecordSummaries": [
        {"memoryRecordId": f"mem-{i:037d}", "content": {"text": t}, "memoryStrategyId": "s",
         "namespaces": ["n"], "createdAt": datetime(2026, 1, 1, tzinfo=timezone.utc)}
        for i, t in enumerate(texts)
    ]}


def test_read_returns_formatted_memory(read):
    mod, mem, stub = read
    # The two namespace calls run in parallel threads, so the Stubber queue order is not
    # tied to a namespace; the namespaces themselves are asserted in test_identity.
    for ns in ("semantic", "recap"):
        stub.add_response("retrieve_memory_records", _records(f"{ns} fact"), {
            "memoryId": MEMORY_ID,
            "namespace": ANY,
            "searchCriteria": {"searchQuery": ANY, "topK": 3},
            "maxResults": 3,
        })
    r = mod.lambda_handler({"body": json.dumps({"profileId": PROFILE_ID})}, None, memory=mem)
    body = json.loads(r["body"])
    assert r["statusCode"] == 200 and body["hasMemory"] is True
    assert sorted(body["memoryContext"].split("\n")) == ["- recap fact", "- semantic fact"]


@pytest.mark.parametrize("event", [
    {"body": json.dumps({"profileId": ""})},
    {"body": json.dumps({"profileId": "bad"})},
    {"body": "not json"},
    {"body": None},
    {"body": "e30=", "isBase64Encoded": True},
])
def test_read_degrades_to_empty(read, event):
    mod, mem, _ = read
    r = mod.lambda_handler(event, None, memory=mem)
    assert r["statusCode"] == 200
    assert json.loads(r["body"]) == {"memoryContext": "", "hasMemory": False}


def test_read_retrieval_error_is_empty_200(read):
    mod, mem, stub = read
    stub.add_client_error("retrieve_memory_records", "ThrottledException")
    stub.add_client_error("retrieve_memory_records", "ThrottledException")
    r = mod.lambda_handler({"body": json.dumps({"profileId": PROFILE_ID})}, None, memory=mem)
    assert json.loads(r["body"]) == {"memoryContext": "", "hasMemory": False}


# --- ingest --------------------------------------------------------------------------

QUEUE_URL = "https://sqs.eu-central-1.amazonaws.com/111111111111/acxdmem-write-queue"


@pytest.fixture
def ingest(monkeypatch):
    monkeypatch.setenv("QUEUE_URL", QUEUE_URL)
    mod = load_lambda("ingest")
    sqs = client("sqs")
    with Stubber(sqs) as stub:
        yield mod, sqs, stub
        stub.assert_no_pending_responses()


def _write_event(body, headers=None):
    return {"body": json.dumps(body), "headers": headers or {}}


def test_ingest_enqueues_with_120s_delay(ingest):
    mod, sqs, stub = ingest
    stub.add_response("send_message", {"MessageId": "m"}, {
        "QueueUrl": QUEUE_URL,
        "MessageBody": json.dumps({"profileId": PROFILE_ID, "conversationId": CONVERSATION_ID,
                                   "attempt": 0, "phase": "fetch", "lastSeenWatermark": None,
                                   "lastSeenCount": None}),
        "DelaySeconds": 120,
    })
    r = mod.lambda_handler(_write_event(
        {"profileId": PROFILE_ID, "conversationId": CONVERSATION_ID},
        {"x-nlx-conversationid": CONVERSATION_ID, "x-nlx-userid": PROFILE_ID},
    ), None, sqs=sqs)
    assert r["statusCode"] == 202 and json.loads(r["body"]) == {"accepted": True}


@pytest.mark.parametrize("headers", [
    {"x-nlx-conversationid": "00000000-0000-0000-0000-000000000000"},
    {"X-NLX-UserId": "bbbb000000000000000000000000beef"},
])
def test_ingest_rejects_header_mismatch(ingest, headers):
    mod, sqs, _ = ingest
    r = mod.lambda_handler(_write_event(
        {"profileId": PROFILE_ID, "conversationId": CONVERSATION_ID}, headers), None, sqs=sqs)
    assert r["statusCode"] == 400


@pytest.mark.parametrize("body, status", [
    ({"profileId": "", "conversationId": CONVERSATION_ID}, 202),
    ({"profileId": "bad", "conversationId": CONVERSATION_ID}, 202),
    ({"profileId": PROFILE_ID, "conversationId": "../etc"}, 400),
    ({"profileId": PROFILE_ID}, 400),
    ([], 400),
])
def test_ingest_skips_or_rejects_without_enqueue(ingest, body, status):
    mod, sqs, _ = ingest
    r = mod.lambda_handler(_write_event(body), None, sqs=sqs)
    assert r["statusCode"] == status
    assert json.loads(r["body"])["accepted"] is False


def test_ingest_enqueue_failure_is_503(ingest):
    mod, sqs, stub = ingest
    stub.add_client_error("send_message", "InternalError", http_status_code=500)
    r = mod.lambda_handler(_write_event({"profileId": PROFILE_ID, "conversationId": CONVERSATION_ID}),
                           None, sqs=sqs)
    assert r["statusCode"] == 503


# --- writer --------------------------------------------------------------------------

FETCH_FN = "acxdmem-fetch"


class Harness:
    def __init__(self):
        self.mod = load_lambda("writer")
        self.lam = client("lambda")
        self.sqs = client("sqs")
        self.mem = client("bedrock-agentcore")
        self.stubs = [Stubber(c) for c in (self.lam, self.sqs, self.mem)]
        for s in self.stubs:
            s.activate()
        self.lam_stub, self.sqs_stub, self.mem_stub = self.stubs
        self.store = FakeStore()
        self.deps = self.mod.Deps(
            lambda_client=self.lam, sqs=self.sqs, memory=self.mem, dynamodb=object(),
            env={"QUEUE_URL": QUEUE_URL, "FETCH_FUNCTION_NAME": FETCH_FN,
                 "DEDUP_TABLE_NAME": "t", "MEMORY_ID": MEMORY_ID},
        )
        self.deps.writer._store = self.store

    def fetch_returns(self, payload, function_error=None):
        resp = {"StatusCode": 200, "Payload": io.BytesIO(json.dumps(payload).encode())}
        if function_error:
            resp["FunctionError"] = function_error
        self.lam_stub.add_response("invoke", resp, {
            "FunctionName": FETCH_FN, "InvocationType": "RequestResponse",
            "Payload": json.dumps({"conversationId": CONVERSATION_ID}).encode(),
        })

    def expect_requeue(self, body, delay):
        self.sqs_stub.add_response("send_message", {"MessageId": "n"}, {
            "QueueUrl": QUEUE_URL, "MessageBody": json.dumps(body), "DelaySeconds": delay})

    def close(self):
        for s in self.stubs:
            s.assert_no_pending_responses()
            s.deactivate()


@pytest.fixture
def harness():
    h = Harness()
    yield h
    h.close()


def _record(**body):
    base = {"profileId": PROFILE_ID, "conversationId": CONVERSATION_ID, "attempt": 0,
            "phase": "fetch", "lastSeenWatermark": None, "lastSeenCount": None}
    base.update(body)
    return {"messageId": "msg-1", "body": json.dumps(base)}


def test_writer_fetch_200_requeues_confirm(harness):
    harness.fetch_returns({"status": 200, "conversation": conversation(SAMPLE_MESSAGES)})
    harness.expect_requeue({"profileId": PROFILE_ID, "conversationId": CONVERSATION_ID,
                            "attempt": 1, "phase": "confirm",
                            "lastSeenWatermark": SAMPLE_WATERMARK, "lastSeenCount": 5}, 30)
    assert harness.mod.process(_record(), harness.deps) == "requeue"


def test_writer_confirm_stable_writes(harness):
    harness.fetch_returns({"status": 200, "conversation": conversation(SAMPLE_MESSAGES)})
    harness.mem_stub.add_response("create_event", {"event": {
        "memoryId": MEMORY_ID, "actorId": PROFILE_ID, "sessionId": CONVERSATION_ID,
        "eventId": "e", "eventTimestamp": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "payload": [], "branch": {"name": "main"}}}, {
        "memoryId": MEMORY_ID, "actorId": PROFILE_ID, "sessionId": CONVERSATION_ID,
        "eventTimestamp": ANY, "payload": ANY,
        "clientToken": dedup.client_token(dedup.segment_key(CONVERSATION_ID, SAMPLE_WATERMARK)),
    })
    rec = _record(attempt=1, phase="confirm", lastSeenWatermark=SAMPLE_WATERMARK, lastSeenCount=5)
    assert harness.mod.process(rec, harness.deps) == "written"


def test_writer_404_backoff(harness):
    harness.fetch_returns({"status": 404})
    harness.expect_requeue({"profileId": PROFILE_ID, "conversationId": CONVERSATION_ID,
                            "attempt": 1, "phase": "fetch", "lastSeenWatermark": None,
                            "lastSeenCount": None}, 60)
    assert harness.mod.process(_record(), harness.deps) == "requeue"


def test_writer_never_indexed_gives_up_without_raising(harness):
    harness.fetch_returns({"status": 404})
    assert harness.mod.process(_record(attempt=6), harness.deps) == "give_up"


def test_writer_malformed_message_is_dropped(harness):
    assert harness.mod.process({"messageId": "x", "body": "{bad"}, harness.deps) == "malformed"


@pytest.mark.parametrize("status", [400, 401, 403])
def test_writer_config_errors_raise_for_dlq(harness, status):
    harness.fetch_returns({"status": status})
    with pytest.raises(harness.mod.FetchStepError):
        harness.mod.process(_record(), harness.deps)


def test_writer_fetch_function_error_raises(harness):
    harness.fetch_returns({"errorMessage": "boom"}, function_error="Unhandled")
    with pytest.raises(harness.mod.FetchStepError):
        harness.mod.process(_record(), harness.deps)


def test_writer_fetch_config_error_raises_not_retryable(harness):
    # lambdas/fetch throws FetchConfigError on an unreadable or malformed API key secret.
    harness.fetch_returns({"errorType": "FetchConfigError",
                           "errorMessage": "ACXD fetch step is misconfigured (Error)"},
                          function_error="Unhandled")
    with pytest.raises(harness.mod.FetchStepError):
        harness.mod.process(_record(), harness.deps)


def test_writer_fetch_invoke_fits_inside_writer_timeout(harness):
    cfg = harness.mod.FETCH_INVOKE_CONFIG
    assert cfg.retries == {"total_max_attempts": 1}
    # One attempt, connect + read, must end before the writer's 30 s timeout with room left
    # for DynamoDB and CreateEvent.
    assert cfg.connect_timeout + cfg.read_timeout <= 15
