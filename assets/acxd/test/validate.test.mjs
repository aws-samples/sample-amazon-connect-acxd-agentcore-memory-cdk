// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Mutation tests: each design or model rule must reject the change it exists to prevent.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { allAssets, PLACEHOLDER_CONFIG, EXIT_NODES, SAMPLE_NODES } from '../src/definitions.mjs';
import { validateAssets } from '../src/validate.mjs';
import { NAMES } from '../src/names.mjs';

/** @returns {any} */
const fresh = () => structuredClone(allAssets({ apiBaseUrl: 'https://api.example.com/prod' }));
const flow = (/** @type {any} */ a, /** @type {string} */ id) => a.flows.find((/** @type {any} */ f) => f.flowId === id);

/** @param {(a: any) => void} mutate @param {RegExp} expected */
function rejects(mutate, expected) {
  const a = fresh();
  mutate(a);
  const errors = validateAssets(a);
  assert.ok(errors.some((e) => expected.test(e)), `expected ${expected}, got:\n${errors.join('\n')}`);
}

test('baseline is valid', () => assert.deepEqual(validateAssets(fresh()), []));

test('write node with an In progress path is rejected', () =>
  rejects((a) => {
    flow(a, NAMES.exitFlow).nodes[EXIT_NODES.write].childNodes.push({
      nodeId: EXIT_NODES.write,
      conditions: [{ left: { type: 'node_status' }, operator: 'eq', right: { type: 'constant', value: 'in_progress' } }],
    });
  }, /must not have an In progress path/));

test('open request model is rejected', () =>
  rejects((a) => {
    delete a.dataRequests[1].requestSchema.additionalProperties;
  }, /additionalProperties false/));

test('read node timeout at or below the read Lambda timeout is rejected', () =>
  rejects((a) => {
    flow(a, NAMES.sampleFlow).nodes[SAMPLE_NODES.read].metadata.timeout = 4000;
  }, /read node timeout must exceed/));

test('exit sub-flow return that keeps the memory context is rejected', () =>
  rejects((a) => {
    flow(a, NAMES.exitFlow).nodes[EXIT_NODES.back].metadata.stateModifications = [];
  }, /return to the caller must clear/));

test('unwired timeout path is rejected', () =>
  rejects((a) => {
    const n = flow(a, NAMES.exitFlow).nodes[EXIT_NODES.write];
    n.childNodes = n.childNodes.filter((/** @type {any} */ c) => c.conditions[0].right.value !== 'timeout');
  }, /timeout path not wired/));

test('Send context on is rejected', () =>
  rejects((a) => {
    a.dataRequests[1].webhook.sendContext = true;
  }, /sendContext must be false/));

test('literal secret in the auth header is rejected', () =>
  rejects((a) => {
    a.dataRequests[0].webhook.environments.production.headers[0].value = 'hunter2';
  }, /must reference an ACXD Secret/));

test('reference to an undefined ACXD Secret is rejected', () =>
  rejects((a) => {
    a.secrets = a.secrets.filter((/** @type {any} */ s) => s.name !== NAMES.writeSecret);
  }, /secret acxdmemWriteSecret is not defined/));

test('plain http endpoint is rejected', () =>
  rejects((a) => {
    a.dataRequests[0].webhook.environments.production.url = 'http://api.example.com/memory/read';
  }, /url must be https/));

test('node type unknown to the model is rejected; end is not the Exit node', () => {
  rejects((a) => {
    flow(a, NAMES.sampleFlow).nodes[SAMPLE_NODES.exit].type = 'exit_application';
  }, /not a FlowNodeType value/);
  // `end` is in the model enum but is not the Exit application node.
  rejects((a) => {
    flow(a, NAMES.sampleFlow).nodes[SAMPLE_NODES.exit].type = 'end';
  }, /use terminate for Exit application/);
});

test('redirect into the exit sub-flow without a wired Continuation is rejected', () =>
  rejects((a) => {
    flow(a, NAMES.sampleFlow).nodes[SAMPLE_NODES.toExit].childNodes = [];
  }, /must wire its Continuation/));

test('terminal node that does not clear the memory context is rejected', () =>
  rejects((a) => {
    flow(a, NAMES.sampleFlow).nodes[SAMPLE_NODES.escalate].metadata.stateModifications = [];
  }, /must clear acxdmemMemoryContext/));

test('exit sub-flow that ends instead of returning is rejected', () =>
  rejects((a) => {
    const f = flow(a, NAMES.exitFlow);
    f.nodes[EXIT_NODES.back] = { nodeId: EXIT_NODES.back, type: 'terminate', childNodes: [], messages: [], metadata: {} };
  }, /must not end or escalate itself/));

test('flow id with a hyphen violates the model FlowId pattern', () =>
  rejects((a) => {
    flow(a, NAMES.exitFlow).flowId = 'acxdmem-exit';
  }, /flowId: does not match/));

test('context variable name with digits violates the model pattern', () =>
  rejects((a) => {
    a.contextVariables[0].name = 'acxdmem2';
  }, /name: does not match/));

test('resource outside the acxdmem prefix is rejected', () =>
  rejects((a) => {
    a.application.name = 'travel-bot';
  }, /does not start with acxdmem/));

test('unknown data request reference and dangling child are rejected', () => {
  rejects((a) => {
    flow(a, NAMES.exitFlow).nodes[EXIT_NODES.write].dataRequests[0].dataRequestId = 'acxdmemOther';
  }, /data request acxdmemOther is not defined/);
  rejects((a) => {
    flow(a, NAMES.exitFlow).nodes[EXIT_NODES.start].childNodes[0].nodeId = 'acd0e000-0000-4000-8000-000000000099';
  }, /does not exist/);
});

test('unreachable node is rejected', () =>
  rejects((a) => {
    const id = 'acd0e000-0000-4000-8000-000000000042';
    flow(a, NAMES.exitFlow).nodes[id] = { nodeId: id, type: 'basic', childNodes: [{ nodeId: EXIT_NODES.back, conditions: [] }], messages: [], metadata: {} };
  }, /not reachable from start/));

test('node timeout above the model range is rejected', () =>
  rejects((a) => {
    flow(a, NAMES.exitFlow).nodes[EXIT_NODES.write].metadata.timeout = 60000;
  }, /metadata.timeout above 30000/));

test('unattached redirect target is rejected', () =>
  rejects((a) => {
    a.application.flows = [{ flowId: NAMES.sampleFlow }];
  }, /redirects to acxdmemExit, which is not attached/));

test('unknown model type is rejected', () =>
  rejects((a) => {
    flow(a, NAMES.sampleFlow).nodes[SAMPLE_NODES.agent].metadata.generativeJourney.modelType = 'gpt-5';
  }, /not a GenerativeModelType value/));

test('placeholder config is also valid', () => assert.deepEqual(validateAssets(allAssets(PLACEHOLDER_CONFIG)), []));
