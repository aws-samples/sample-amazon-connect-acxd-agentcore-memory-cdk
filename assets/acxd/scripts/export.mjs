#!/usr/bin/env node
// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Export the acxdmem* assets from a live workspace as placeholder JSON (read-only calls only).
// Use it to capture changes made in the designer Canvas, then fold them into
// src/definitions.mjs and run `npm run render`.
//
// Usage:
//   node scripts/export.mjs --workspace-id <uuid> --api-key-secret-id <SM id> [--out <dir>]
//     [--region eu-central-1] [--api-key-json-key apiKey]
// The default output directory is build/export/ (git-ignored), never exported/ directly.

import { mkdirSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import * as sdk from 'amazon-connect-acxd-sdk';
import { NAMES, isOwned } from '../src/names.mjs';
import { acxdClient, httpStatus } from '../src/client.mjs';
import { parseCommon, requireOptions } from '../src/cli.mjs';
import { sanitizeApplication, sanitizeContextVariable, sanitizeDataRequest, sanitizeFlow, sanitizeSecret } from '../src/sanitize.mjs';

const opts = parseCommon(process.argv.slice(2), { out: { type: 'string' } });
requireOptions(opts, ['workspace-id', 'api-key-secret-id']);
const out = resolve(opts.out ?? fileURLToPath(new URL('../build/export/', import.meta.url)));
mkdirSync(out, { recursive: true });

const { acxd } = await acxdClient({
  region: opts.region,
  workspaceId: opts['workspace-id'],
  apiKeySecretId: opts['api-key-secret-id'],
  apiKeyJsonKey: opts['api-key-json-key'],
});

/** @param {string} name @param {unknown} body */
const write = (name, body) => {
  writeFileSync(join(out, name), `${JSON.stringify(body, null, 2)}\n`);
  console.log(`wrote ${join(out, name)}`);
};
/** @param {Promise<any>} p */
const orNull = (p) => p.catch((e) => (httpStatus(e) === 404 ? null : Promise.reject(e)));

const cvs = await acxd.send(new sdk.ListContextVariablesCommand({}));
write('context-variables.json', (cvs.items ?? []).filter((v) => isOwned(v.name)).map(sanitizeContextVariable));

/** @type {any[]} */
const secrets = [];
let token;
do {
  const page = await acxd.send(new sdk.ListSecretsCommand({ nextToken: token }));
  secrets.push(...(page.items ?? []).filter((s) => isOwned(s.name)));
  token = page.nextToken;
} while (token);
write('secrets.json', secrets.map(sanitizeSecret));

for (const id of [NAMES.readRequest, NAMES.writeRequest]) {
  const dr = await orNull(acxd.send(new sdk.GetDataRequestCommand({ dataRequestIdentifier: id })));
  if (dr) write(`data-request-${id}.json`, sanitizeDataRequest(dr));
  else console.log(`missing data request ${id}`);
}

for (const id of [NAMES.exitFlow, NAMES.sampleFlow]) {
  const f = await orNull(acxd.send(new sdk.GetFlowCommand({ flowIdentifier: id })));
  if (f) write(`flow-${id}.json`, sanitizeFlow(f));
  else console.log(`missing flow ${id}`);
}

/** @type {any} */
let app;
token = undefined;
do {
  const page = await acxd.send(new sdk.ListApplicationsCommand({ nextToken: token }));
  app = page.items?.find((x) => x.name === NAMES.application) ?? app;
  token = page.nextToken;
} while (token && !app);
if (app) {
  const full = await acxd.send(new sdk.GetApplicationCommand({ applicationIdentifier: app.applicationId }));
  write(`application-${NAMES.application}.json`, sanitizeApplication(full));
} else {
  console.log(`missing application ${NAMES.application}`);
}
