#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Provision the AgentCore Memory resource and its execution role for the Agentic CX Designer (ACXD) sample.
#
# The strategies carry the extraction-prompt override (see the memory resource section).
#
# Deliberately OUTSIDE CDK's lifecycle: the memory resource holds
# customer data with its own 90-day event-expiry retention, and a failed CloudFormation
# custom resource mid-rollback is a much worse failure mode for a resource like this than a
# plain, idempotent shell script. `cdk destroy` never touches what this script creates -
# see the README's teardown section.
#
# Idempotent: skips the role if it already exists, and refuses to create a second memory
# resource if one with the same name is already present in the target account/region.
#
# All identifiers are parameterized via environment variables (with sane defaults) - NONE
# of the prototype's original hardcoded account/instance/region values are baked in here.
set -euo pipefail
command -v jq >/dev/null || { echo "jq is required" >&2; exit 1; }

PROFILE="${AWS_PROFILE_OVERRIDE:-default}"
REGION="${AWS_REGION_OVERRIDE:?set AWS_REGION_OVERRIDE, e.g. eu-central-1}"
ROLE_NAME="${MEMORY_EXECUTION_ROLE_NAME:-acxdmem-memory-execution-role}"
MEMORY_NAME="${MEMORY_RESOURCE_NAME:-acxdmem_memory}"
# Model ID must be a regional inference profile, NOT a bare foundation-model ID.
# The AgentCore Memory API validates that the execution role can invoke it and
# rejects base model IDs.  Each region has its own prefix (us., eu., ap., etc.).
# Override via MEMORY_EXTRACTION_MODEL_ID if using a different model or region prefix.
EXTRACTION_MODEL_ID="${MEMORY_EXTRACTION_MODEL_ID:-}"
if [[ -z "$EXTRACTION_MODEL_ID" ]]; then
  # Derive the region prefix from REGION (us-east-1 → us, eu-central-1 → eu, etc.)
  REGION_PREFIX="${REGION%%-*}"
  EXTRACTION_MODEL_ID="${REGION_PREFIX}.anthropic.claude-haiku-4-5-20251001-v1:0"
  echo "    auto-derived extraction model: $EXTRACTION_MODEL_ID (from region $REGION)"
fi
EVENT_EXPIRY_DAYS="${MEMORY_EVENT_EXPIRY_DAYS:-90}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

aws_() { aws --profile "$PROFILE" --region "$REGION" "$@"; }

echo "==> account check"
ACCOUNT_ID="$(aws_ sts get-caller-identity --query Account --output text)"
echo "    account: $ACCOUNT_ID  region: $REGION"

# --- execution role trust policy: bedrock-agentcore.amazonaws.com may assume this role,
# scoped to memory resources in this account.  Region is wildcarded so the same role
# works across multiple regions (multi-region deployments share one IAM role). ---------
cat > "$WORKDIR/trust-policy.json" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowAgentCoreMemoryAssume",
      "Effect": "Allow",
      "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": {"aws:SourceAccount": "$ACCOUNT_ID"},
        "ArnLike": {"aws:SourceArn": "arn:aws:bedrock-agentcore:*:$ACCOUNT_ID:memory/*"}
      }
    }
  ]
}
EOF

# --- execution role permissions: allow the extraction strategies to invoke foundation
# models and inference profiles in ANY region, so one role serves multi-region deploys. -
cat > "$WORKDIR/execution-policy.json" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowExtractionModelInvoke",
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel", "bedrock:GetInferenceProfile"],
      "Resource": [
        "arn:aws:bedrock:*::foundation-model/*",
        "arn:aws:bedrock:*:$ACCOUNT_ID:inference-profile/*"
      ]
    }
  ]
}
EOF

echo "==> execution role"
if aws iam get-role --profile "$PROFILE" --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  echo "    role $ROLE_NAME already exists, updating trust + inline policies"
  aws iam update-assume-role-policy --profile "$PROFILE" \
    --role-name "$ROLE_NAME" \
    --policy-document "file://$WORKDIR/trust-policy.json"
else
  aws iam create-role --profile "$PROFILE" \
    --role-name "$ROLE_NAME" \
    --assume-role-policy-document "file://$WORKDIR/trust-policy.json" \
    --description "Execution role for AgentCore Memory extraction (acxdmem)" \
    --tags Key=project,Value=acxdmem \
    --query 'Role.Arn' --output text
fi
aws iam put-role-policy --profile "$PROFILE" \
  --role-name "$ROLE_NAME" \
  --policy-name acxdmem-memory-extraction \
  --policy-document "file://$WORKDIR/execution-policy.json"
echo "    inline policy applied"

ROLE_ARN="arn:aws:iam::$ACCOUNT_ID:role/$ROLE_NAME"

# --- memory resource: three strategies (semantic facts,
# custom recap at a flat namespace, built-in summary off the read path). The semantic strategy is a CUSTOM strategy with a semanticOverride so
# both read-path strategies carry the extraction-prompt rule below. That rule is the
# primary backend control against the memory feedback loop (the agent restates injected
# memory, which would otherwise be re-extracted as a new fact on the next write).
# API shape checked against SemanticOverrideExtractionConfigurationInput (appendToPrompt
# 1-30000 chars, modelId; both required) in the AgentCore Control API reference.
FEEDBACK_RULE="Do not extract facts about the customer that only the assistant stated; extract only what the customer said or confirmed."
SEMANTIC_PROMPT="Extract durable facts and preferences about the customer that are useful in later conversations. $FEEDBACK_RULE"
RECAP_PROMPT="You are summarising a single customer conversation for an assistant that will handle this customer's NEXT conversation. Produce at most three records. Each record must be a self-contained recap sentence covering: what the customer contacted us about, what was resolved or promised, and any commitment still outstanding. Always begin each record with 'Previous conversation: '. Omit small talk and anything already captured as a durable customer preference or fact. $FEEDBACK_RULE"

jq -n \
  --arg name "$MEMORY_NAME" \
  --argjson expiry "$EVENT_EXPIRY_DAYS" \
  --arg role "$ROLE_ARN" \
  --arg model "$EXTRACTION_MODEL_ID" \
  --arg semantic_prompt "$SEMANTIC_PROMPT" \
  --arg recap_prompt "$RECAP_PROMPT" \
  '{
    name: $name,
    description: "Cross-conversation memory for Amazon Connect Customer ACXD applications. actorId = Customer Profiles profileId, sessionId = ACXD conversationId.",
    eventExpiryDuration: $expiry,
    memoryExecutionRoleArn: $role,
    memoryStrategies: [
      {customMemoryStrategy: {
        name: "acxdmem_customer_semantic",
        description: "Durable cross-conversation facts about a customer, keyed on the Customer Profiles profileId.",
        namespaces: ["connect/profile/{actorId}/semantic"],
        configuration: {semanticOverride: {extraction: {modelId: $model, appendToPrompt: $semantic_prompt}}}
      }},
      {customMemoryStrategy: {
        name: "acxdmem_conversation_recap",
        description: "Conversation recap at a flat namespace, retrievable at the start of the next conversation.",
        namespaces: ["connect/profile/{actorId}/recap"],
        configuration: {semanticOverride: {extraction: {modelId: $model, appendToPrompt: $recap_prompt}}}
      }},
      {summaryMemoryStrategy: {
        name: "acxdmem_conversation_summary",
        description: "Built-in per-conversation summary, off the read path (namespace needs {sessionId}).",
        namespaces: ["connect/profile/{actorId}/summary/{sessionId}"]
      }}
    ],
    tags: {project: "acxdmem"}
  }' > "$WORKDIR/memory-resource.json"

echo "==> memory resource"
EXISTING=$(aws_ bedrock-agentcore-control list-memories \
  --query "memories[?name=='$MEMORY_NAME'].id | [0]" --output text 2>/dev/null || echo "None")
if [[ "$EXISTING" != "None" && -n "$EXISTING" ]]; then
  echo "    memory already exists: $EXISTING - not creating a second one"
  MEMORY_ID="$EXISTING"
else
  # A freshly created role takes a few seconds to propagate; until then CreateMemory fails
  # with "Please provide a role with a valid trust policy" (seen on the first live deploy).
  MEMORY_ID=""
  for attempt in 1 2 3 4 5 6; do
    if MEMORY_ID=$(aws_ bedrock-agentcore-control create-memory \
        --cli-input-json "file://$WORKDIR/memory-resource.json" \
        --client-token "provision-$(date +%Y%m%d%H%M%S)" \
        --query 'memory.id' --output text 2>"$WORKDIR/create.err"); then
      break
    fi
    if ! grep -q "valid trust policy" "$WORKDIR/create.err" || [[ $attempt -eq 6 ]]; then
      cat "$WORKDIR/create.err" >&2
      exit 1
    fi
    echo "    role not yet usable by AgentCore (IAM propagation), retry $attempt/5 in 10 s"
    sleep 10
  done
  echo "    created: $MEMORY_ID"
fi

echo "==> waiting for ACTIVE (strategy creation takes ~2-3 min)"
for _ in $(seq 1 60); do
  STATUS=$(aws_ bedrock-agentcore-control get-memory --memory-id "$MEMORY_ID" \
    --query 'memory.status' --output text)
  STRATS=$(aws_ bedrock-agentcore-control get-memory --memory-id "$MEMORY_ID" \
    --query 'memory.strategies[].status' --output text)
  echo "    memory=$STATUS strategies=$STRATS"
  if [[ "$STATUS" == "ACTIVE" && "$STRATS" != *"CREATING"* ]]; then
    break
  fi
  if [[ "$STATUS" == "FAILED" ]]; then
    echo "!!! memory FAILED" >&2
    aws_ bedrock-agentcore-control get-memory --memory-id "$MEMORY_ID" --output json >&2
    exit 1
  fi
  sleep 10
done

if [[ "$STATUS" != "ACTIVE" || "$STRATS" == *"CREATING"* ]]; then
  echo "!!! memory not ACTIVE after 10 minutes (memory=$STATUS strategies=$STRATS); re-run this script to keep waiting" >&2
  exit 1
fi

aws_ bedrock-agentcore-control get-memory --memory-id "$MEMORY_ID" \
  --query 'memory.strategies[].{name:name,type:type,namespaces:namespaces}' --output table
# The ARN goes last, so it is what a reader sees at the end of the output.
echo
echo "memory id : $MEMORY_ID"
echo "memory arn: arn:aws:bedrock-agentcore:$REGION:$ACCOUNT_ID:memory/$MEMORY_ID"
echo "Set cdk.json's context.memoryResourceArn to the ARN above before running cdk deploy."
