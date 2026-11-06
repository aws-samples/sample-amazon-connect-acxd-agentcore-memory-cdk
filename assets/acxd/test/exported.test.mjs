// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// The committed JSON must match the definitions and must not contain real identifiers.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { allAssets, PLACEHOLDER_CONFIG } from '../src/definitions.mjs';
import { sanitizeApplication, sanitizeDataRequest, sanitizeFlow, sanitizeSecret } from '../src/sanitize.mjs';
import { PLACEHOLDERS } from '../src/names.mjs';

const ROOT = fileURLToPath(new URL('..', import.meta.url));
const files = [
  ...readdirSync(join(ROOT, 'exported')).map((f) => join(ROOT, 'exported', f)),
  join(ROOT, 'connect', 'acxdmem-inbound-snippet.template.json'),
];

test('exported/ holds exactly the rendered definitions', () => {
  const a = allAssets(PLACEHOLDER_CONFIG);
  const expected = {
    'context-variables.json': a.contextVariables,
    'secrets.json': a.secrets,
    'data-request-acxdmemRead.json': a.dataRequests[0],
    'data-request-acxdmemWrite.json': a.dataRequests[1],
    'flow-acxdmemExit.json': a.flows[0],
    'flow-acxdmemSample.json': a.flows[1],
    'application-acxdmem-sample.json': a.application,
  };
  assert.deepEqual(readdirSync(join(ROOT, 'exported')).sort(), Object.keys(expected).sort());
  for (const [name, body] of Object.entries(expected)) {
    assert.deepEqual(JSON.parse(readFileSync(join(ROOT, 'exported', name), 'utf8')), body, name);
  }
});

test('no real ids, URLs, account numbers or secret values in committed JSON', () => {
  const uuid = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/g;
  for (const f of files) {
    const text = readFileSync(f, 'utf8');
    for (const id of text.match(uuid) ?? []) assert.ok(id.startsWith('acd0'), `${f}: real-looking id ${id}`);
    assert.ok(!/https?:\/\//.test(text), `${f}: contains a URL`);
    assert.ok(!/\b\d{12}\b/.test(text.replace(uuid, '')), `${f}: contains a 12-digit number`);
    assert.ok(!/amazonaws\.com/.test(text), `${f}: contains an AWS endpoint`);
  }
  const secrets = JSON.parse(readFileSync(join(ROOT, 'exported', 'secrets.json'), 'utf8'));
  for (const s of secrets) assert.equal(s.secretValue, PLACEHOLDERS.secretValue);
});

test('sanitizers strip live values', () => {
  const dr = sanitizeDataRequest({
    dataRequestId: 'acxdmemRead',
    createdAt: '2026-10-08T00:00:00Z',
    updatedBy: 'someone',
    webhook: {
      environments: {
        production: { url: 'https://abc.execute-api.eu-central-1.amazonaws.com/prod/memory/read', headers: [{ key: 'x-acxdmem-secret', value: 'literal' }] },
        development: { url: 'https://dev.example.com/memory/read', headers: [{ key: 'x-acxdmem-secret', value: '{acxdmemReadSecret:NLX.Secret}' }] },
      },
    },
  });
  assert.equal(dr.createdAt, undefined);
  assert.equal(dr.updatedBy, undefined);
  assert.equal(dr.webhook.environments.production.url, `${PLACEHOLDERS.apiBaseUrl}/memory/read`);
  assert.equal(dr.webhook.environments.development.url, `${PLACEHOLDERS.devApiBaseUrl}/memory/read`);
  assert.equal(dr.webhook.environments.production.headers[0].value, PLACEHOLDERS.secretValue);
  assert.equal(dr.webhook.environments.development.headers[0].value, '{acxdmemReadSecret:NLX.Secret}');

  const flow = sanitizeFlow({ flowId: 'acxdmemExit', saveId: 's', nodes: { a: { nodeId: 'a', canvasMetadata: { x: 1 } } } });
  assert.deepEqual(flow, { flowId: 'acxdmemExit', nodes: { a: { nodeId: 'a' } } });
  const app = sanitizeApplication({ name: 'acxdmem-sample', applicationId: 'x', deploymentSettings: { a: 1 }, settings: {} });
  assert.deepEqual(app, { name: 'acxdmem-sample', settings: {} });
  assert.equal(sanitizeSecret({ name: 'acxdmemReadSecret', secretValue: 'live' }).secretValue, PLACEHOLDERS.secretValue);
});
