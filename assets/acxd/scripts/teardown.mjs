#!/usr/bin/env node
// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Delete the acxdmem* assets from a workspace, in reverse dependency order.
// Dry run by default; --apply deletes. Only the names in src/names.mjs are touched.
//
// Usage:
//   node scripts/teardown.mjs --workspace-id <uuid> --api-key-secret-id <SM id> [--apply]
//     [--region eu-central-1] [--api-key-json-key apiKey]
//
// Before running it, remove the Agentic CX block that points at the sample application from
// any Connect Customer flow, so no live contact reaches a deleted application.

import * as sdk from 'amazon-connect-acxd-sdk';
import { NAMES, assertOwned } from '../src/names.mjs';
import { acxdClient, httpStatus } from '../src/client.mjs';
import { parseCommon, requireOptions } from '../src/cli.mjs';

const opts = parseCommon(process.argv.slice(2));
requireOptions(opts, ['workspace-id', 'api-key-secret-id']);
const { acxd } = await acxdClient({
  region: opts.region,
  workspaceId: opts['workspace-id'],
  apiKeySecretId: opts['api-key-secret-id'],
  apiKeyJsonKey: opts['api-key-json-key'],
});

/** @param {string} label @param {string} id @param {() => any} command */
async function remove(label, id, command) {
  assertOwned(id);
  if (!opts.apply) return console.log(`would delete ${label}`);
  try {
    await acxd.send(command());
    console.log(`deleted ${label}`);
  } catch (e) {
    if (httpStatus(e) === 404) console.log(`gone    ${label}`);
    else throw e;
  }
}

/** @type {any} */
let app;
let token;
do {
  const page = await acxd.send(new sdk.ListApplicationsCommand({ nextToken: token }));
  app = page.items?.find((x) => x.name === NAMES.application) ?? app;
  token = page.nextToken;
} while (token && !app);

if (app) {
  const deps = await acxd.send(new sdk.ListApplicationDeploymentsCommand({ applicationIdentifier: app.applicationId }));
  for (const d of deps.items ?? []) {
    await remove(`deployment ${d.deploymentId}`, app.name, () => new sdk.DeleteApplicationDeploymentCommand({ applicationIdentifier: app.applicationId, deploymentIdentifier: d.deploymentId }));
  }
  await remove(`application ${app.name} ${app.applicationId}`, app.name, () => new sdk.DeleteApplicationCommand({ applicationIdentifier: app.applicationId }));
}
for (const f of [NAMES.sampleFlow, NAMES.exitFlow]) {
  await remove(`flow ${f}`, f, () => new sdk.DeleteFlowCommand({ flowIdentifier: f }));
}
for (const d of [NAMES.readRequest, NAMES.writeRequest]) {
  await remove(`data request ${d}`, d, () => new sdk.DeleteDataRequestCommand({ dataRequestIdentifier: d }));
}
await remove(`context variable ${NAMES.contextVariable}`, NAMES.contextVariable, () => new sdk.DeleteContextVariableCommand({ contextVariableIdentifier: NAMES.contextVariable }));
for (const s of [NAMES.readSecret, NAMES.writeSecret]) {
  await remove(`secret ${s}`, s, () => new sdk.DeleteSecretCommand({ secretIdentifier: s }));
}
if (!opts.apply) console.log('dry run; re-run with --apply to delete.');
