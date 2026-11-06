// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// acxdmem fetch step (design §3.2, Option B): one ACXD GetConversation call through the
// official npm SDK `amazon-connect-acxd-sdk` (design §3.2). Invoked synchronously by
// the Python writer Lambda with IAM (no resource policy, no public endpoint).
//
// Input:  { "conversationId": "<ACXD conversationId>" }
// Output: { "status": 200, "conversation": { conversationId, userId, applicationId, messages[] } }
//         { "status": <404 | 429 | 5xx | other HTTP status> }
//         throws FetchConfigError when the API key or the client cannot be set up
//         (a FunctionError for the writer: hard failure, DLQ, Errors alarm)
//
// The writer owns the retry/stability state machine; this step never retries beyond the
// SDK's own single extra attempt. It never logs message text (threat T5): ids and status only.
//
// Environment:
//   ACXD_API_KEY_SECRET_ARN  Secrets Manager secret holding the API key of a workspace-scoped
//                            read-only ACXD programmatic user (threat T6)
//   ACXD_WORKSPACE_ID        ACXD workspace id (sent as x-workspace-id by the SDK wrapper)
//   ACXD_REGION              region of the ACXD API (default: AWS_REGION)
//   API_KEY_CACHE_SECONDS    in-container cache for the API key (default 300)
import { AgenticCXDesignerClient, GetConversationCommand } from 'amazon-connect-acxd-sdk';
import { SecretsManagerClient, GetSecretValueCommand } from '@aws-sdk/client-secrets-manager';

// Same rule as acxd_memory.identity.is_valid_conversation_id (AgentCore sessionId pattern).
const CONVERSATION_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$/;

// Message fields the writer uses. Everything else (analytics tags, evaluations) is dropped.
const MESSAGE_FIELDS = ['isApplication', 'text', 'timestamp', 'isStructured'];

export function project(conversation) {
  const messages = Array.isArray(conversation?.messages) ? conversation.messages : [];
  return {
    conversationId: conversation?.conversationId,
    userId: conversation?.userId ?? null,
    applicationId: conversation?.applicationId ?? null,
    messages: messages.map((m) => Object.fromEntries(MESSAGE_FIELDS.filter((k) => m?.[k] !== undefined).map((k) => [k, m[k]]))),
  };
}

export function statusOf(err) {
  const code = err?.$metadata?.httpStatusCode ?? err?.$response?.statusCode;
  if (Number.isInteger(code)) return code;
  switch (err?.name) {
    case 'ResourceNotFoundException':
      return 404;
    case 'ThrottlingException':
    case 'TooManyRequestsException': // seen live in spike S0 (429), not in the SDK error model
      return 429;
    case 'ValidationException':
      return 400;
    case 'UnauthorizedException':
      return 401;
    default:
      return 500; // network/timeout: retryable from the writer's point of view
  }
}

// The secret holds the plain API key, or JSON {"apiKey": "..."}.
export function apiKeyFromSecretString(secretString) {
  const text = typeof secretString === 'string' ? secretString.trim() : '';
  if (!text) throw new Error('ACXD API key secret is empty');
  if (text.startsWith('{')) {
    const key = JSON.parse(text).apiKey;
    if (typeof key !== 'string' || !key) throw new Error('ACXD API key secret JSON has no apiKey');
    return key;
  }
  return text;
}

/** The API key or the SDK client could not be set up. Never carries the key or secret text. */
export class FetchConfigError extends Error {
  constructor(cause) {
    super(`ACXD fetch step is misconfigured (${cause?.name ?? 'Error'})`);
    this.name = 'FetchConfigError';
  }
}

export function makeFetcher({ getApiKey, createClient, now = () => Date.now(), cacheSeconds = 300 }) {
  let cached = null; // { key, client, expires }
  async function client() {
    if (cached && now() < cached.expires) return cached.client;
    const key = await getApiKey();
    cached = { client: createClient(key), expires: now() + cacheSeconds * 1000 };
    return cached.client;
  }
  return async function fetchConversation(event) {
    const conversationId = event?.conversationId;
    if (typeof conversationId !== 'string' || !CONVERSATION_ID.test(conversationId)) {
      console.log(JSON.stringify({ msg: 'invalid conversationId' }));
      return { status: 400 };
    }
    const started = now();
    // Configuration failures (secret unreadable, empty or without apiKey, client
    // construction) are not "not yet indexed": they throw, so the invoke returns a
    // FunctionError, the writer raises FetchStepError, the message reaches the DLQ after
    // maxReceiveCount, and the fetch Errors alarm fires.
    let c;
    try {
      c = await client();
    } catch (err) {
      console.log(JSON.stringify({ msg: 'fetch config error', conversationId, error: err?.name, ms: now() - started }));
      throw new FetchConfigError(err);
    }
    try {
      const conversation = await c.send(new GetConversationCommand({ conversationIdentifier: conversationId }));
      const projected = project(conversation);
      console.log(JSON.stringify({ msg: 'fetched', conversationId, status: 200, messages: projected.messages.length, ms: now() - started }));
      return { status: 200, conversation: projected };
    } catch (err) {
      const status = statusOf(err);
      if (status === 401 || status === 403) cached = null; // key rotated or revoked: reload next time
      console.log(JSON.stringify({ msg: 'fetch failed', conversationId, status, error: err?.name, ms: now() - started }));
      return { status };
    }
  };
}

function defaultFetcher() {
  const secrets = new SecretsManagerClient({});
  const region = process.env.ACXD_REGION || process.env.AWS_REGION;
  const workspaceId = process.env.ACXD_WORKSPACE_ID;
  return makeFetcher({
    cacheSeconds: Number(process.env.API_KEY_CACHE_SECONDS || 300),
    getApiKey: async () => {
      const r = await secrets.send(new GetSecretValueCommand({ SecretId: process.env.ACXD_API_KEY_SECRET_ARN }));
      return apiKeyFromSecretString(r.SecretString);
    },
    // maxAttempts 2: one SDK retry; the writer's state machine handles longer backoff.
    createClient: (apiKey) => new AgenticCXDesignerClient({ apiKey, workspaceId, region, maxAttempts: 2 }),
  });
}

let fetcher;
export const handler = async (event) => {
  fetcher ??= defaultFetcher();
  return fetcher(event);
};
