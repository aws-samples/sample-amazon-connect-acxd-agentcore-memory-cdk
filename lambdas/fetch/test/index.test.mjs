// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Unit tests for the fetch step. The SDK client is replaced by a fake with the same
// `send(command)` contract; no network, no AWS credentials.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { GetConversationCommand } from 'amazon-connect-acxd-sdk';
import { FetchConfigError, apiKeyFromSecretString, makeFetcher, project, statusOf } from '../index.mjs';

const CONV = '11111111-2222-4333-8444-555555555555';

function fakeClient(impl) {
  const calls = [];
  return {
    calls,
    async send(command) {
      calls.push(command);
      return impl(command);
    },
  };
}

function sdkError(name, status) {
  const err = new Error(name);
  err.name = name;
  if (status) err.$metadata = { httpStatusCode: status };
  return err;
}

test('200: calls GetConversation with conversationIdentifier and projects fields', async () => {
  const client = fakeClient(() => ({
    conversationId: CONV,
    userId: 'aacd000000000000000000000000c0de',
    analyticsTags: ['x'],
    messages: [
      { isApplication: true, text: 'Hi', timestamp: '2026-10-08T14:47:44.000Z', analyticsTags: [], type: 'bot_message' },
      { isApplication: false, text: 'Hello', timestamp: '2026-10-08T14:47:50.000Z', correlationId: 'c' },
    ],
  }));
  const fetch = makeFetcher({ getApiKey: async () => 'k', createClient: () => client });
  const r = await fetch({ conversationId: CONV });
  assert.equal(r.status, 200);
  assert.ok(client.calls[0] instanceof GetConversationCommand);
  assert.deepEqual(client.calls[0].input, { conversationIdentifier: CONV });
  assert.deepEqual(r.conversation, {
    conversationId: CONV,
    userId: 'aacd000000000000000000000000c0de',
    applicationId: null,
    messages: [
      { isApplication: true, text: 'Hi', timestamp: '2026-10-08T14:47:44.000Z' },
      { isApplication: false, text: 'Hello', timestamp: '2026-10-08T14:47:50.000Z' },
    ],
  });
});

test('not yet indexed: ResourceNotFoundException maps to 404', async () => {
  const fetch = makeFetcher({
    getApiKey: async () => 'k',
    createClient: () => fakeClient(() => { throw sdkError('ResourceNotFoundException', 404); }),
  });
  assert.deepEqual(await fetch({ conversationId: CONV }), { status: 404 });
});

test('throttling maps to 429, including the unmodelled TooManyRequestsException', () => {
  assert.equal(statusOf(sdkError('TooManyRequestsException', 429)), 429);
  assert.equal(statusOf(sdkError('TooManyRequestsException')), 429);
  assert.equal(statusOf(sdkError('ThrottlingException')), 429);
  assert.equal(statusOf(sdkError('InternalServerException', 500)), 500);
  assert.equal(statusOf(new Error('socket hang up')), 500);
  assert.equal(statusOf(sdkError('UnauthorizedException', 401)), 401);
});

test('invalid conversationId is rejected without calling ACXD', async () => {
  let created = false;
  const fetch = makeFetcher({ getApiKey: async () => 'k', createClient: () => { created = true; } });
  assert.deepEqual(await fetch({ conversationId: '../x' }), { status: 400 });
  assert.deepEqual(await fetch({}), { status: 400 });
  assert.equal(created, false);
});

test('API key is cached, and reloaded after a 401', async () => {
  let loads = 0;
  let fail = false;
  const client = fakeClient(() => {
    if (fail) throw sdkError('UnauthorizedException', 401);
    return { conversationId: CONV, messages: [] };
  });
  const fetch = makeFetcher({ getApiKey: async () => { loads += 1; return 'k'; }, createClient: () => client });
  await fetch({ conversationId: CONV });
  await fetch({ conversationId: CONV });
  assert.equal(loads, 1);
  fail = true;
  assert.equal((await fetch({ conversationId: CONV })).status, 401);
  fail = false;
  await fetch({ conversationId: CONV });
  assert.equal(loads, 2);
});

test('project tolerates missing messages and userId', () => {
  assert.deepEqual(project({ conversationId: CONV }), { conversationId: CONV, userId: null, applicationId: null, messages: [] });
});

test('secret string: plain key or JSON apiKey', () => {
  assert.equal(apiKeyFromSecretString(' acxd_live_x \n'), 'acxd_live_x');
  assert.equal(apiKeyFromSecretString('{"apiKey":"acxd_live_y"}'), 'acxd_live_y');
  assert.throws(() => apiKeyFromSecretString(''));
  assert.throws(() => apiKeyFromSecretString('{"other":1}'));
});

test('config errors throw (FunctionError -> DLQ), never a retryable status', async () => {
  const cases = [
    async () => apiKeyFromSecretString(''), // empty secret
    async () => apiKeyFromSecretString('{"other":1}'), // JSON without apiKey
    async () => apiKeyFromSecretString('{not json, acxd_live_secret'), // malformed JSON
    async () => { throw sdkError('ResourceNotFoundException', 400); }, // secret missing
    async () => { throw new Error('connect ETIMEDOUT'); }, // Secrets Manager unreachable
  ];
  for (const getApiKey of cases) {
    let calledAcxd = false;
    const fetch = makeFetcher({ getApiKey, createClient: () => fakeClient(() => { calledAcxd = true; }) });
    await assert.rejects(fetch({ conversationId: CONV }), (err) => {
      assert.ok(err instanceof FetchConfigError);
      assert.doesNotMatch(err.message, /acxd_live_secret/);
      return true;
    });
    assert.equal(calledAcxd, false);
  }
});

test('client construction error throws FetchConfigError', async () => {
  const fetch = makeFetcher({ getApiKey: async () => 'k', createClient: () => { throw new TypeError('bad region'); } });
  await assert.rejects(fetch({ conversationId: CONV }), FetchConfigError);
});
