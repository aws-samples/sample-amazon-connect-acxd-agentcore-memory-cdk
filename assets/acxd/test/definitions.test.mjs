// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  allAssets,
  exitFlow,
  sampleFlow,
  writeRequest,
  readRequest,
  application,
  contextVariables,
  PLACEHOLDER_CONFIG,
  PROMPT_RULE,
  EXIT_NODES,
  SAMPLE_NODES,
} from '../src/definitions.mjs';
import { validateAssets, RULES } from '../src/validate.mjs';
import { NAMES, TOKENS, SECRET_HEADER, isOwned } from '../src/names.mjs';

const REAL = { apiBaseUrl: 'https://abc123.execute-api.eu-central-1.amazonaws.com/prod/' };

test('placeholder and real configurations validate against the model and design rules', () => {
  assert.deepEqual(validateAssets(allAssets(PLACEHOLDER_CONFIG)), []);
  assert.deepEqual(validateAssets(allAssets(REAL, { read: 'r'.repeat(40), write: 'w'.repeat(40) })), []);
});

test('every resource name is owned by the sample', () => {
  const a = allAssets(PLACEHOLDER_CONFIG);
  const names = [
    ...a.contextVariables.map((v) => v.name),
    ...a.secrets.map((s) => s.name),
    ...a.dataRequests.map((d) => d.dataRequestId),
    ...a.flows.map((f) => f.flowId),
    a.application.name,
  ];
  for (const n of names) assert.ok(isOwned(n), n);
});

test('write request carries IDs only (Option B) and never sends context', () => {
  const d = writeRequest(REAL);
  assert.deepEqual(Object.keys(/** @type {any} */ (d.requestSchema).properties).sort(), ['conversationId', 'profileId']);
  assert.equal(d.webhook?.sendContext, false);
  assert.equal(d.webhook?.environments?.production?.url, 'https://abc123.execute-api.eu-central-1.amazonaws.com/prod/memory/write');
  assert.equal(d.webhook?.environments?.development?.url, 'https://abc123.execute-api.eu-central-1.amazonaws.com/prod/memory/write');
  const h = d.webhook?.environments?.production?.headers?.[0];
  assert.deepEqual(h, { key: SECRET_HEADER, value: `{${NAMES.writeSecret}:NLX.Secret}`, sensitive: true, dynamic: false, required: true });
});

test('read request uses its own secret and marks memory text sensitive', () => {
  const d = readRequest({ ...REAL, devApiBaseUrl: 'https://dev.example.com/stage' });
  assert.equal(d.webhook?.environments?.development?.url, 'https://dev.example.com/stage/memory/read');
  assert.equal(d.webhook?.environments?.production?.headers?.[0].value, `{${NAMES.readSecret}:NLX.Secret}`);
  assert.equal(/** @type {any} */ (d.responseSchema).properties.memoryContext.isSensitive, true);
  assert.equal(/** @type {any} */ (d.requestSchema).properties.profileId.isSensitive, true);
  assert.equal(d.webhook?.sendContext, false);
});

test('memory context variable cannot be set by the frontend and is sensitive', () => {
  const [v] = contextVariables();
  assert.equal(v.name, NAMES.contextVariable);
  assert.equal(v.disallowExternalModification, true);
  assert.equal(/** @type {any} */ (v.schema).isSensitive, true);
});

test('exit sub-flow: write with IDs, all outcomes continue, return via System.lastFlow', () => {
  const f = exitFlow();
  const write = f.nodes[EXIT_NODES.write];
  assert.equal(write.type, 'data_request');
  const statuses = (write.childNodes ?? []).map((c) => c.conditions?.[0].right?.value).sort();
  assert.deepEqual(statuses, ['failure', 'success', 'timeout']);
  assert.ok((write.childNodes ?? []).every((c) => c.nodeId === EXIT_NODES.back));
  const payload = /** @type {any} */ (write.dataRequests?.[0].payload);
  assert.deepEqual(payload, {
    type: 'recursive',
    value: {
      profileId: { type: 'constant', value: TOKENS.userId },
      conversationId: { type: 'constant', value: TOKENS.conversationId },
    },
  });
  assert.equal(write.dataRequests?.[0].alwaysRetrigger, true);
  assert.equal(write.metadata?.timeout, undefined, 'keeps the 5 s default');
  assert.deepEqual(f.nodes[EXIT_NODES.back].metadata?.redirect, { type: 'flow', flowId: '{System.lastFlow:NLX.System}' });
  assert.deepEqual(f.nodes[EXIT_NODES.back].metadata?.stateModifications, [{ type: 'context', name: NAMES.contextVariable, modification: 'clear' }]);
  assert.equal(/** @type {any} */ (writeRequest(REAL).requestSchema).additionalProperties, false);
});

test('sample flow: read, store, agent, and both terminals through the exit sub-flow', () => {
  const f = sampleFlow();
  const N = SAMPLE_NODES;
  const read = f.nodes[N.read];
  assert.deepEqual(/** @type {any} */ (read.dataRequests?.[0].payload).value.profileId.value, TOKENS.userId);
  assert.deepEqual(f.nodes[N.load].metadata?.stateModifications?.[0], {
    type: 'context',
    name: NAMES.contextVariable,
    modification: 'set',
    value: { type: 'variable', name: 'acxdmemRead.memoryContext' },
  });
  for (const [redirect, terminal] of [
    [N.toExit, N.exit],
    [N.toEscalate, N.escalate],
  ]) {
    const node = f.nodes[redirect];
    assert.equal(node.metadata?.redirect?.flowId, NAMES.exitFlow);
    assert.equal(node.childNodes?.[0].nodeId, terminal);
    assert.equal(node.childNodes?.[0].conditions?.[0].right?.value, 'continuation');
  }
  assert.equal(f.nodes[N.exit].type, 'terminate');
  for (const t of [N.exit, N.escalate]) {
    assert.deepEqual(f.nodes[t].metadata?.stateModifications, [{ type: 'context', name: NAMES.contextVariable, modification: 'clear' }]);
  }
  const agentChildren = (f.nodes[N.agent].childNodes ?? []).map((c) => c.nodeId);
  assert.ok(!agentChildren.includes(N.exit) && !agentChildren.includes(N.escalate), 'agent never skips the write');
});

test('agent prompt carries the D2 rule and the memory token', () => {
  const p = sampleFlow().nodes[SAMPLE_NODES.agent].metadata?.generativeJourney?.prompt ?? '';
  assert.ok(p.includes(PROMPT_RULE));
  assert.ok(p.includes(TOKENS.memoryContext));
});

test('application attaches both flows and stays within model ranges', () => {
  const app = application();
  assert.deepEqual(app.flows?.map((f) => f.flowId).sort(), [NAMES.exitFlow, NAMES.sampleFlow].sort());
  assert.match(app.name ?? '', RULES.applicationName.pattern);
});

test('names satisfy their model patterns', () => {
  assert.match(NAMES.contextVariable, RULES.contextVariableName.pattern);
  assert.match(NAMES.readSecret, RULES.secretName.pattern);
  assert.match(NAMES.readRequest, RULES.dataRequestId.pattern);
  assert.match(NAMES.exitFlow, RULES.flowId.pattern);
  assert.match(TOKENS.lastFlow, RULES.redirectTarget.pattern);
});
