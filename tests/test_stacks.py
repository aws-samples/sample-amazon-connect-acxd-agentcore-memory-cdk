# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CDK assertions: security rules (§6), queue settings (§3.2), IAM scope, CDK Nag."""

from __future__ import annotations

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Annotations, Match, Template

import app as app_module

CONTEXT = {
    "memoryResourceArn": "arn:aws:bedrock-agentcore:eu-central-1:123456789012:memory/acxdmem_test-AbCdEf1234",
    "acxdWorkspaceId": "33333333-4444-4555-8666-777777777777",
    "acxdApplicationId": "22222222-3333-4444-8555-666666666666",
    "acxdApiKeySecretArn": "arn:aws:secretsmanager:eu-central-1:123456789012:secret:acxdmem-acxd-api-key-AbCdEf",
    "accountId": "123456789012",
    "region": "eu-central-1",
}


def synth(**overrides):
    app = cdk.App(context={**CONTEXT, **overrides})
    writer, api = app_module.build_app(app)
    return app, writer, api


@pytest.fixture(scope="module")
def stacks():
    app, writer, api = synth()
    return {
        "app": app,
        "writer": writer,
        "api": api,
        "writer_t": Template.from_stack(writer),
        "api_t": Template.from_stack(api),
    }


def _all(templates):
    for t in templates:
        yield t.to_json()


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


# --- §6 endpoint security rules -------------------------------------------------------


def test_no_lambda_function_urls(stacks):
    for t in (stacks["writer_t"], stacks["api_t"]):
        t.resource_count_is("AWS::Lambda::Url", 0)


def _is_tls_only_deny(stmt):
    """The one allowed wildcard: CDK's enforce_ssl queue policy (CDK Nag SQS4, design §9).
    It only ever denies, and only requests without TLS."""
    return (
        stmt.get("Effect") == "Deny"
        and stmt.get("Condition") == {"Bool": {"aws:SecureTransport": "false"}}
    )


def _has_wildcard(principal):
    if principal == "*":
        return True
    if isinstance(principal, dict):
        for value in principal.values():
            values = value if isinstance(value, list) else [value]
            if "*" in values:
                return True
    return False


def test_no_wildcard_principal_anywhere(stacks):
    tls_denies = 0
    for doc in _all((stacks["writer_t"], stacks["api_t"])):
        for node in _walk(doc):
            if "Principal" in node and _has_wildcard(node["Principal"]):
                assert _is_tls_only_deny(node), f"wildcard principal outside TLS-only Deny: {node}"
                tls_denies += 1
        for res in doc["Resources"].values():
            if res["Type"] == "AWS::Lambda::Permission":
                assert res["Properties"]["Principal"] != "*"
    assert tls_denies == 2  # work queue + DLQ


def _rest_api_logical_id(api_t):
    ids = list(api_t.find_resources("AWS::ApiGateway::RestApi"))
    assert len(ids) == 1
    return ids[0]


def test_every_lambda_permission_is_scoped_to_this_api(stacks):
    """Principal apigateway.amazonaws.com, SourceArn on this API, no stage wildcard on
    route integrations (CDK's LambdaIntegration default is <api>/*/POST/<path>)."""
    api_t = stacks["api_t"]
    api_id = _rest_api_logical_id(api_t)
    perms = api_t.find_resources("AWS::Lambda::Permission")
    assert perms, "expected API Gateway invoke permissions"
    route_perms = 0
    for logical_id, res in perms.items():
        props = res["Properties"]
        assert props["Principal"] == "apigateway.amazonaws.com", logical_id
        source = json.dumps(props.get("SourceArn"))
        assert api_id in source, f"{logical_id} SourceArn not scoped to this API: {source}"
        if "/authorizers/" in source:
            continue
        route_perms += 1
        assert '"/v1/POST/memory/' in source, f"{logical_id}: {source}"
        assert "/*/" not in source, f"{logical_id} has a stage wildcard: {source}"
    assert route_perms == 2
    # no permissions at all on the writer side (fetch step is IAM-invoked only)
    stacks["writer_t"].resource_count_is("AWS::Lambda::Permission", 0)


def test_every_method_has_a_custom_authorizer_and_validator(stacks):
    api_t = stacks["api_t"]
    methods = api_t.find_resources("AWS::ApiGateway::Method")
    assert len(methods) == 2
    authorizers = api_t.find_resources("AWS::ApiGateway::Authorizer")
    assert len(authorizers) == 2
    used = set()
    for m in methods.values():
        p = m["Properties"]
        assert p["HttpMethod"] == "POST"
        assert p["AuthorizationType"] == "CUSTOM"
        assert "Ref" in p["AuthorizerId"]
        assert "RequestValidatorId" in p
        used.add(p["AuthorizerId"]["Ref"])
    assert used == set(authorizers), "each route must have its own authorizer"
    for a in authorizers.values():
        p = a["Properties"]
        assert p["Type"] == "REQUEST"
        assert p["IdentitySource"] == "method.request.header.x-acxdmem-secret"
        assert p["AuthorizerResultTtlInSeconds"] == 300


def test_authorizers_bound_to_different_secrets_and_routes(stacks):
    fns = stacks["api_t"].find_resources(
        "AWS::Lambda::Function",
        {"Properties": {"Environment": {"Variables": {"EXPECTED_ROUTE": Match.any_value()}}}},
    )
    routes = sorted(f["Properties"]["Environment"]["Variables"]["EXPECTED_ROUTE"] for f in fns.values())
    assert routes == ["POST/memory/read", "POST/memory/write"]
    secrets = {json.dumps(f["Properties"]["Environment"]["Variables"]["SECRET_ARN"]) for f in fns.values()}
    assert len(secrets) == 2


def test_g11_request_models_reject_additional_properties(stacks):
    models = stacks["api_t"].find_resources("AWS::ApiGateway::Model")
    assert len(models) == 2
    by_name = {m["Properties"]["Name"]: m["Properties"]["Schema"] for m in models.values()}
    write = by_name["AcxdmemWriteRequest"]
    read = by_name["AcxdmemReadRequest"]
    for schema in (write, read):
        assert schema["additionalProperties"] is False
        assert schema["type"] == "object"
    assert set(write["properties"]) == {"profileId", "conversationId"}
    assert set(write["required"]) == {"profileId", "conversationId"}
    assert set(read["properties"]) == {"profileId"}
    for prop in (*write["properties"].values(), *read["properties"].values()):
        assert prop["maxLength"] <= 100


def test_validator_validates_body(stacks):
    stacks["api_t"].has_resource_properties(
        "AWS::ApiGateway::RequestValidator", {"ValidateRequestBody": True}
    )


def test_stage_throttling_and_logging(stacks):
    stacks["api_t"].has_resource_properties("AWS::ApiGateway::Stage", {
        "StageName": "v1",
        "TracingEnabled": False,
        "AccessLogSetting": Match.object_like({"DestinationArn": Match.any_value()}),
        "MethodSettings": Match.array_with([Match.object_like({
            "DataTraceEnabled": False,
            "LoggingLevel": "ERROR",
            "ThrottlingRateLimit": 50,
            "ThrottlingBurstLimit": 100,
        })]),
    })


def test_api_is_regional(stacks):
    stacks["api_t"].has_resource_properties("AWS::ApiGateway::RestApi", {
        "EndpointConfiguration": {"Types": ["REGIONAL"]}})


def test_api_rejects_tls_below_1_2(stacks):
    """A regional REST API defaults to TLS_1_0 (TLS 1.0/1.1 accepted). The enhanced policy
    allows TLS 1.3 and TLS 1.2 with ECDHE + AES-GCM only, and needs an access mode."""
    stacks["api_t"].has_resource_properties("AWS::ApiGateway::RestApi", {
        "SecurityPolicy": "SecurityPolicy_TLS13_1_2_PFS_PQ_2025_09",
        "EndpointAccessMode": "STRICT",
    })


def test_access_log_records_tls_version_and_caller(stacks):
    stage = next(iter(stacks["api_t"].find_resources("AWS::ApiGateway::Stage").values()))
    access_format = json.loads(stage["Properties"]["AccessLogSetting"]["Format"])
    assert access_format["tlsVersion"] == "$context.tlsVersion"
    # The caller is ACXD, so its address is not customer data and lets you act on probes.
    assert access_format["sourceIp"] == "$context.identity.sourceIp"
    identity = [v for v in access_format.values() if "$context.identity." in v]
    assert identity == ["$context.identity.sourceIp"]
    # No headers, body, or validation detail (validationErrorString can echo body values).
    for v in access_format.values():
        assert "header" not in v.lower() and "body" not in v.lower(), v
        assert "validationErrorString" not in v, v


def test_endpoint_secrets_have_generated_names(stacks):
    """A fixed name collides with the previous secret's recovery window after a destroy."""
    secrets = stacks["api_t"].find_resources("AWS::SecretsManager::Secret")
    assert len(secrets) == 2
    for logical_id, res in secrets.items():
        assert "Name" not in res["Properties"], logical_id
    outputs = stacks["api_t"].find_outputs("*")
    assert {"EndpointSecretReadArn", "EndpointSecretWriteArn"} <= set(outputs)


def test_no_test_or_echo_endpoint(stacks):
    paths = sorted(r["Properties"]["PathPart"]
                   for r in stacks["api_t"].find_resources("AWS::ApiGateway::Resource").values())
    assert paths == ["memory", "read", "write"]


# --- §4 read path ----------------------------------------------------------------------


def test_read_lambda_timeout_and_budget(stacks):
    stacks["api_t"].has_resource_properties("AWS::Lambda::Function", {
        "FunctionName": "acxdmem-read",
        "Timeout": 4,
        "Environment": {"Variables": Match.object_like({"READ_BUDGET_SECONDS": "1.5"})},
    })


@pytest.mark.parametrize("name", ["acxdmem-read", "acxdmem-ingest",
                                  "acxdmem-authorizer-read", "acxdmem-authorizer-write"])
def test_customer_facing_functions_have_cold_start_headroom(stacks, name):
    # Live test: 256 MB double cold starts took 3.8-4.4 s against the 5 s ACXD timeout.
    stacks["api_t"].has_resource_properties("AWS::Lambda::Function", {
        "FunctionName": name, "MemorySize": 1024,
    })


# --- §3.2 writer settings ---------------------------------------------------------------


def test_work_queue_settings(stacks):
    t = stacks["writer_t"]
    t.has_resource_properties("AWS::SQS::Queue", {
        "QueueName": "acxdmem-write-queue",
        "VisibilityTimeout": 180,
        "MessageRetentionPeriod": 4 * 24 * 3600,
        "RedrivePolicy": Match.object_like({"maxReceiveCount": 3}),
        "SqsManagedSseEnabled": True,
    })
    assert not any("FifoQueue" in q["Properties"] for q in t.find_resources("AWS::SQS::Queue").values())


def test_writer_timeout_and_event_source(stacks):
    t = stacks["writer_t"]
    t.has_resource_properties("AWS::Lambda::Function", {"FunctionName": "acxdmem-writer", "Timeout": 30})
    t.has_resource_properties("AWS::Lambda::EventSourceMapping", {
        "BatchSize": 1, "ScalingConfig": {"MaximumConcurrency": 2}})
    for f in t.find_resources("AWS::Lambda::Function").values():
        assert "ReservedConcurrentExecutions" not in f["Properties"]


def test_fetch_step_is_node(stacks):
    stacks["writer_t"].has_resource_properties("AWS::Lambda::Function", {
        "FunctionName": "acxdmem-fetch", "Runtime": "nodejs22.x", "Handler": "index.handler"})


def test_dedup_table(stacks):
    stacks["writer_t"].has_resource("AWS::DynamoDB::Table", {
        "DeletionPolicy": "Retain",
        "Properties": Match.object_like({
            "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}],
            "TimeToLiveSpecification": {"AttributeName": "expires_at", "Enabled": True},
            "PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": True},
        }),
    })


# --- IAM least privilege (§9) --------------------------------------------------------------


def _policy_actions(template, role_fragment):
    actions = []
    for logical_id, res in template.find_resources("AWS::IAM::Policy").items():
        if role_fragment not in logical_id:
            continue
        for stmt in res["Properties"]["PolicyDocument"]["Statement"]:
            a = stmt["Action"]
            actions.extend(a if isinstance(a, list) else [a])
    return actions


def test_writer_role_actions_without_stripping(stacks):
    actions = _policy_actions(stacks["writer_t"], "WriterFunctionRole")
    assert "bedrock-agentcore:CreateEvent" in actions
    assert "bedrock-agentcore:RetrieveMemoryRecords" not in actions
    assert "sqs:SendMessage" in actions
    assert "lambda:InvokeFunction" in actions
    # No service-wide or action-prefix wildcards (the SQS event source adds named receive
    # and delete actions, which are expected).
    assert not any(a.endswith("*") for a in actions), actions
    forbidden = ("dynamodb:Delete", "dynamodb:Scan", "sqs:DeleteQueue", "sqs:PurgeQueue")
    assert not any(a.startswith(forbidden) for a in actions), actions


def test_writer_role_gains_retrieve_only_with_stripping():
    _, writer, _ = synth(enableStripping=True)
    actions = _policy_actions(Template.from_stack(writer), "WriterFunctionRole")
    assert "bedrock-agentcore:RetrieveMemoryRecords" in actions
    t = Template.from_stack(writer)
    t.has_resource_properties("AWS::Lambda::Function", {
        "FunctionName": "acxdmem-writer",
        "Environment": {"Variables": Match.object_like({"ENABLE_STRIPPING": "true"})}})


def test_memory_permissions_scoped_to_memory_arn(stacks):
    arn = CONTEXT["memoryResourceArn"]
    for t in (stacks["writer_t"], stacks["api_t"]):
        for res in t.find_resources("AWS::IAM::Policy").values():
            for stmt in res["Properties"]["PolicyDocument"]["Statement"]:
                actions = stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]]
                if any(a.startswith("bedrock-agentcore:") for a in actions):
                    assert stmt["Resource"] == arn


def _resource_strings(resource):
    """Every literal string inside a policy Resource (plain, list, or Fn::Join parts)."""
    items = resource if isinstance(resource, list) else [resource]
    for item in items:
        if isinstance(item, str):
            yield item
        elif isinstance(item, dict) and "Fn::Join" in item:
            sep, parts = item["Fn::Join"]
            yield sep.join(p if isinstance(p, str) else "<token>" for p in parts)


def test_no_iam_resource_star(stacks):
    """No ``*``, and no ARN ending in a wildcard, in any Resource (plain or list)."""
    for t in (stacks["writer_t"], stacks["api_t"]):
        for res in t.find_resources("AWS::IAM::Policy").values():
            for stmt in res["Properties"]["PolicyDocument"]["Statement"]:
                assert "Resource" in stmt, stmt
                items = stmt["Resource"] if isinstance(stmt["Resource"], list) else [stmt["Resource"]]
                for item in items:
                    # Non-literal resources must reference one specific resource.
                    assert isinstance(item, str) or set(item) <= {
                        "Ref", "Fn::GetAtt", "Fn::ImportValue", "Fn::Join"}, stmt
                for value in _resource_strings(stmt["Resource"]):
                    assert value != "*", stmt
                    assert not value.endswith((":*", "/*")), stmt


def test_resource_star_guard_catches_lists_and_suffixes():
    assert list(_resource_strings(["arn:a", "*"]))[-1] == "*"
    joined = {"Fn::Join": ["", ["arn:aws:logs:", {"Ref": "AWS::Region"}, ":1:log-group:x:*"]]}
    assert list(_resource_strings(joined))[0].endswith(":*")


# --- Logging and alarms (docs/security-posture.md) ------------------------------------------


def test_every_function_logs_json(stacks):
    for t in (stacks["writer_t"], stacks["api_t"]):
        for logical_id, fn in t.find_resources("AWS::Lambda::Function").items():
            assert fn["Properties"]["LoggingConfig"]["LogFormat"] == "JSON", logical_id


def _alarms(template, metric_name):
    return [a["Properties"] for a in template.find_resources("AWS::CloudWatch::Alarm").values()
            if a["Properties"].get("MetricName") == metric_name]


def test_every_function_has_an_errors_alarm(stacks):
    for t in (stacks["writer_t"], stacks["api_t"]):
        functions = set(t.find_resources("AWS::Lambda::Function"))
        alarmed = set()
        for props in _alarms(t, "Errors"):
            assert props["Namespace"] == "AWS/Lambda"
            assert props["Threshold"] == 0
            (dim,) = props["Dimensions"]
            assert dim["Name"] == "FunctionName"
            alarmed.add(dim["Value"]["Ref"])
        assert alarmed == functions


def test_api_stage_error_alarms(stacks):
    t = stacks["api_t"]
    (server,) = _alarms(t, "5XXError")
    assert server["Statistic"] == "Sum" and server["Threshold"] == 0
    (client,) = _alarms(t, "4XXError")
    assert client["Statistic"] == "Average" and client["Threshold"] == 0.05
    assert client["EvaluationPeriods"] == 5 and client["DatapointsToAlarm"] == 5
    (stage_id,) = t.find_resources("AWS::ApiGateway::Stage", {"Properties": {"StageName": "v1"}})
    for props in (server, client):
        dims = {d["Name"]: d["Value"] for d in props["Dimensions"]}
        assert dims["Stage"] == {"Ref": stage_id} and dims["ApiName"] == "acxdmem-api"


def test_work_queue_age_alarm_well_inside_retention(stacks):
    t = stacks["writer_t"]
    (age,) = _alarms(t, "ApproximateAgeOfOldestMessage")
    assert age["Namespace"] == "AWS/SQS" and age["Statistic"] == "Maximum"
    assert age["Threshold"] == 1800
    (queue_id,) = t.find_resources("AWS::SQS::Queue", {"Properties": {"QueueName": "acxdmem-write-queue"}})
    (dim,) = age["Dimensions"]
    assert dim["Name"] == "QueueName" and dim["Value"] == {"Fn::GetAtt": [queue_id, "QueueName"]}
    queues = {q["Properties"]["QueueName"]: q["Properties"]
              for q in t.find_resources("AWS::SQS::Queue").values()}
    work, dlq = queues["acxdmem-write-queue"], queues["acxdmem-write-dlq"]
    assert age["Threshold"] * 10 < work["MessageRetentionPeriod"]
    # A message keeps its enqueue time in the DLQ, so the DLQ must retain longer.
    assert dlq["MessageRetentionPeriod"] > work["MessageRetentionPeriod"]


def test_alarm_count_matches_docs(stacks):
    # README Cost and the deployment guide's Operations section say ten alarms.
    total = sum(len(t.find_resources("AWS::CloudWatch::Alarm"))
                for t in (stacks["writer_t"], stacks["api_t"]))
    assert total == 10


def test_alarms_have_no_actions(stacks):
    """Alarms ship without actions: the builder wires them to their own on-call topic."""
    for t in (stacks["writer_t"], stacks["api_t"]):
        for props in t.find_resources("AWS::CloudWatch::Alarm").values():
            assert "AlarmActions" not in props["Properties"]


def test_waf_opt_in():
    _, _, api = synth(enableWaf=True)
    t = Template.from_stack(api)
    t.resource_count_is("AWS::WAFv2::WebACL", 1)
    t.resource_count_is("AWS::WAFv2::WebACLAssociation", 1)


# --- CDK Nag ------------------------------------------------------------------------------


@pytest.mark.parametrize("flags", [{}, {"enableWaf": True, "enableStripping": True}])
def test_cdk_nag_has_no_unsuppressed_findings(flags):
    _, writer, api = synth(**flags)
    for stack in (writer, api):
        annotations = Annotations.from_stack(stack)
        errors = annotations.find_error("*", Match.string_like_regexp("AwsSolutions-.*"))
        warnings = annotations.find_warning("*", Match.string_like_regexp("AwsSolutions-.*"))
        assert errors == [], [e.entry.data for e in errors]
        assert warnings == [], [w.entry.data for w in warnings]


# --- app.py context validation --------------------------------------------------------------


def test_missing_context_fails_fast():
    app = cdk.App(context={k: v for k, v in CONTEXT.items() if k != "acxdWorkspaceId"})
    with pytest.raises(app_module.ConfigError, match="acxdWorkspaceId"):
        app_module.build_app(app)


def test_placeholder_context_fails_fast():
    app = cdk.App(context={**CONTEXT, "memoryResourceArn": "REPLACE_WITH_MEMORY_RESOURCE_ARN"})
    with pytest.raises(app_module.ConfigError, match="memoryResourceArn"):
        app_module.build_app(app)


@pytest.mark.parametrize("key, value", [
    ("memoryResourceArn", "arn:aws:s3:::bucket"),
    ("acxdApplicationId", "not-a-uuid"),
    ("acxdApiKeySecretArn", "arn:aws:secretsmanager:eu-central-1:123456789012:secret:nosuffix"),
    ("resourcePrefix", "other"),
    ("writerMaxConcurrency", 9),
    ("enableWaf", "maybe"),
])
def test_malformed_context_fails_fast(key, value):
    app = cdk.App(context={**CONTEXT, key: value})
    with pytest.raises(app_module.ConfigError):
        app_module.build_app(app)


def test_allow_placeholders_synthesizes():
    app = cdk.App(context={"allowPlaceholders": "true"})
    writer, api = app_module.build_app(app)
    Template.from_stack(writer)
    Template.from_stack(api)
