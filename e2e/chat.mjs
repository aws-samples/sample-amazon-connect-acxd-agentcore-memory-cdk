// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Drive one Amazon Connect Customer chat through an acxdmem-e2e flow and record the bot side.
// The driver plays the trusted backend that verified the
// customer before starting the chat: --email sets the acxdmemVerifiedEmail contact attribute
// the e2e flow keys the Customer Profiles lookup on (design.md section 5, T9).
//
// Usage: ACXDMEM_INSTANCE_ID=<id> node chat.mjs --flow <contactFlowId> [--email x@example.com] --say "..." [--say ...]
//                      [--end disconnect|none] [--idle N] [--attr k=v ...]
// Prints JSON: contactId, per-turn latency and bot messages, branch attributes, contact info.
import { ConnectClient, StartChatContactCommand, DescribeContactCommand, GetContactAttributesCommand } from '@aws-sdk/client-connect';
import {
  ConnectParticipantClient,
  CreateParticipantConnectionCommand,
  SendMessageCommand,
  GetTranscriptCommand,
  DisconnectParticipantCommand,
} from '@aws-sdk/client-connectparticipant';

const INSTANCE = process.env.ACXDMEM_INSTANCE_ID;
if (!INSTANCE) throw new Error('set ACXDMEM_INSTANCE_ID to the Amazon Connect Customer instance id');
const region = process.env.AWS_REGION ?? 'eu-central-1';

const argv = process.argv.slice(2);
const all = (k) => argv.flatMap((a, i) => (a === k ? [argv[i + 1]] : []));
const one = (k, d) => all(k)[0] ?? d;
const FLOW = one('--flow');
if (!FLOW) throw new Error('--flow <contactFlowId> is required');
const says = all('--say');
const end = one('--end', 'disconnect');
const idle = Number(one('--idle', '0'));
const email = one('--email');
const attrs = Object.fromEntries(all('--attr').map((kv) => kv.split(/=(.*)/s).slice(0, 2)));
if (email) attrs.acxdmemVerifiedEmail = email;

const connect = new ConnectClient({ region });
const part = new ConnectParticipantClient({ region });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const start = await connect.send(
  new StartChatContactCommand({
    InstanceId: INSTANCE,
    ContactFlowId: FLOW,
    ParticipantDetails: { DisplayName: 'acxdmem-e2e-tester' },
    Attributes: Object.keys(attrs).length ? attrs : undefined,
  }),
);
console.error('contactId', start.ContactId);
const conn = await part.send(
  new CreateParticipantConnectionCommand({ ParticipantToken: start.ParticipantToken, Type: ['WEBSOCKET', 'CONNECTION_CREDENTIALS'] }),
);
const token = conn.ConnectionCredentials.ConnectionToken;
// A non-streaming chat only starts its flow once the customer's websocket is subscribed.
const ws = new WebSocket(conn.Websocket.Url);
await new Promise((resolve, reject) => {
  ws.onopen = () => {
    ws.send(JSON.stringify({ topic: 'aws/subscribe', content: { topics: ['aws/chat'] } }));
    resolve();
  };
  ws.onerror = reject;
});

const seen = new Set();
let chatEnded = false;
async function botMessages() {
  if (chatEnded) return [];
  let r;
  try {
    r = await part.send(new GetTranscriptCommand({ ConnectionToken: token, MaxResults: 100, SortOrder: 'ASCENDING' }));
  } catch (e) {
    if (e.name === 'AccessDeniedException') {
      chatEnded = true;
      return [{ role: 'SYSTEM', type: 'EVENT', content: 'token-revoked (chat ended)', at: new Date().toISOString() }];
    }
    throw e;
  }
  const fresh = [];
  for (const it of r.Transcript ?? []) {
    if (seen.has(it.Id)) continue;
    seen.add(it.Id);
    if (it.ParticipantRole !== 'CUSTOMER') fresh.push({ role: it.ParticipantRole, type: it.Type, content: it.Content ?? it.ContentType, at: it.AbsoluteTime });
  }
  return fresh;
}
async function waitForBot(t0, maxMs = 45000) {
  const out = [];
  let firstAt = null;
  let quietSince = null;
  while (Date.now() - t0 < maxMs) {
    const m = (await botMessages()).filter((x) => x.type === 'MESSAGE' || x.type === 'EVENT');
    if (m.length) {
      if (!firstAt && m.some((x) => x.type === 'MESSAGE')) firstAt = Date.now();
      out.push(...m);
      quietSince = Date.now();
      if (chatEnded || m.some((x) => /participant\.left|chat\.ended/i.test(x.content ?? ''))) break;
    }
    if (firstAt && quietSince && Date.now() - quietSince > 3500) break;
    await sleep(700);
  }
  return { firstResponseMs: firstAt ? firstAt - t0 : null, messages: out };
}

const result = { contactId: start.ContactId, flow: FLOW, startedAt: new Date().toISOString(), attrs, turns: [] };
let t0 = Date.now();
result.turns.push({ say: null, ...(await waitForBot(t0)) });
for (const s of says) {
  if (chatEnded) {
    result.turns.push({ say: s, skipped: 'chat already ended' });
    continue;
  }
  t0 = Date.now();
  try {
    await part.send(new SendMessageCommand({ ConnectionToken: token, ContentType: 'text/plain', Content: s }));
  } catch (e) {
    result.turns.push({ say: s, sendError: `${e.name}: ${e.message}` });
    continue;
  }
  result.turns.push({ say: s, ...(await waitForBot(t0)) });
}
if (idle) {
  const tIdle = Date.now();
  while (!chatEnded && Date.now() - tIdle < idle * 1000) {
    const m = await botMessages();
    if (m.length) (result.afterIdle ??= []).push(...m);
    if (m.some((x) => /participant\.left|chat\.ended/i.test(x.content ?? ''))) break;
    await sleep(2000);
  }
  result.idleWaitedMs = Date.now() - tIdle;
}
if (end === 'disconnect' && !chatEnded) {
  await part.send(new DisconnectParticipantCommand({ ConnectionToken: token })).catch((e) => (result.disconnectError = e.name));
  result.endedAt = new Date().toISOString();
}
await sleep(4000);
const d = await connect.send(new DescribeContactCommand({ InstanceId: INSTANCE, ContactId: start.ContactId }));
const a = await connect.send(new GetContactAttributesCommand({ InstanceId: INSTANCE, InitialContactId: start.ContactId }));
result.contact = { disconnectReason: d.Contact?.DisconnectReason, disconnectTimestamp: d.Contact?.DisconnectTimestamp };
result.attributes = a.Attributes;
ws.close();
console.log(JSON.stringify(result, null, 1));
