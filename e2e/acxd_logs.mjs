// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Dump the ACXD runtime log events (QueryLogs) of one conversation as JSON lines, oldest
// first, for evidence. Uses the read-only acxdmem reader key from Secrets Manager.
// Strings longer than 120 characters are cut, so a line never carries a full transcript.
// Usage: ACXDMEM_WORKSPACE_ID=<uuid> node acxd_logs.mjs <conversationId> [hoursBack=3]
// Optional: ACXDMEM_READER_SECRET_ID (default acxdmem-acxd-reader-api-key), AWS_REGION.
import * as sdk from 'amazon-connect-acxd-sdk';
import { GetSecretValueCommand, SecretsManagerClient } from '@aws-sdk/client-secrets-manager';

const [conv, hours = '3'] = process.argv.slice(2);
const workspaceId = process.env.ACXDMEM_WORKSPACE_ID;
if (!conv || !workspaceId) throw new Error('usage: ACXDMEM_WORKSPACE_ID=<uuid> node acxd_logs.mjs <conversationId> [hoursBack]');
const region = process.env.AWS_REGION ?? 'eu-central-1';
const sm = new SecretsManagerClient({ region });
const apiKey = (await sm.send(new GetSecretValueCommand({ SecretId: process.env.ACXDMEM_READER_SECRET_ID ?? 'acxdmem-acxd-reader-api-key' }))).SecretString;
const c = new sdk.AgenticCXDesignerClient({ apiKey, workspaceId, region });
const end = new Date();
const start = new Date(end.getTime() - Number(hours) * 3600e3);
const events = [];
let nextToken;
do {
  const r = await c.send(
    new sdk.QueryLogsCommand({ timeFilter: { absolute: { startTimestamp: start, endTimestamp: end } }, searchFilter: { conversationId: conv }, maxResults: 100, nextToken }),
  );
  events.push(...(r.items ?? []));
  nextToken = r.nextToken;
} while (nextToken);
const cut = (_k, v) => (typeof v === 'string' && v.length > 120 ? `${v.slice(0, 120)}…` : v);
events.sort((a, b) => String(a.eventTime).localeCompare(String(b.eventTime)));
for (const e of events) {
  const { commonProperties: _drop, ...rest } = e;
  console.log(JSON.stringify(rest, cut));
}
