// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Print an ACXD conversation record (GetConversation) for evidence: userId, applicationId, and
// each message's raw timestamp value and JS type, role and the first characters of its text.
// Synthetic test data only. The API key comes from Secrets Manager (never env/args/disk).
// Usage: node conversation.mjs <conversationId> --workspace-id <uuid> [--secret-id <id> --json-key k]
import * as sdk from 'amazon-connect-acxd-sdk';
import { GetSecretValueCommand, SecretsManagerClient } from '@aws-sdk/client-secrets-manager';

const argv = process.argv.slice(2);
const opt = (k, d) => (argv.includes(k) ? argv[argv.indexOf(k) + 1] : d);
const id = argv[0];
const workspaceId = opt('--workspace-id', process.env.ACXDMEM_WORKSPACE_ID);
if (!id || !workspaceId) throw new Error('usage: node conversation.mjs <conversationId> --workspace-id <uuid>');
const region = process.env.AWS_REGION ?? 'eu-central-1';
const sm = new SecretsManagerClient({ region });
const raw = (await sm.send(new GetSecretValueCommand({ SecretId: opt('--secret-id', 'acxdmem-acxd-reader-api-key') }))).SecretString;
const jsonKey = opt('--json-key');
const apiKey = jsonKey ? JSON.parse(raw)[jsonKey] : raw;
const c = new sdk.AgenticCXDesignerClient({ apiKey, workspaceId, region });
const conv = await c.send(new sdk.GetConversationCommand({ conversationIdentifier: id }));
console.log(JSON.stringify({
  conversationId: conv.conversationId ?? id,
  userId: conv.userId,
  applicationId: conv.applicationId,
  messages: (conv.messages ?? []).map((m) => ({
    timestamp: m.timestamp instanceof Date ? `Date(${m.timestamp.toISOString()})` : m.timestamp,
    timestampType: m.timestamp instanceof Date ? 'Date' : typeof m.timestamp,
    role: m.isApplication ? 'assistant' : 'user',
    type: m.type,
    flowId: m.flowId,
    text: String(m.text ?? '').slice(0, 80),
  })),
}, null, 1));
