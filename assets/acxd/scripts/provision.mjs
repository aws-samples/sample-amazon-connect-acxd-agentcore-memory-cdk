#!/usr/bin/env node
// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Create or update the Agentic CX Designer (ACXD) assets in an ACXD workspace with the ACXD SDK.
//
// Dry run by default: validates the definitions with your values and prints the plan, with no
// network calls. --apply makes the changes. --deploy (with --apply) also builds the sample
// application and deploys the build to its Development environment.
//
// Usage:
//   node scripts/provision.mjs --workspace-id <uuid> --api-base-url https://<api>/<stage> \
//     [--dev-api-base-url https://...] [--region eu-central-1] \
//     --api-key-secret-id <SM id> [--api-key-json-key apiKey] \
//     --read-secret-id <SM id> --write-secret-id <SM id> [--apply] [--deploy]
//
// Secrets: the ACXD API key and both endpoint secrets are read from AWS Secrets Manager with
// your current AWS credentials. The API key needs a programmatic user that can create
// resources in the workspace; it is not the read-only key the writer Lambda uses.
// Only resources named acxdmem* are created or changed.

import * as sdk from 'amazon-connect-acxd-sdk';
import { allAssets } from '../src/definitions.mjs';
import { validateAssets } from '../src/validate.mjs';
import { assertOwned, NAMES } from '../src/names.mjs';
import { acxdClient, httpStatus, readSecret } from '../src/client.mjs';
import { parseCommon, requireOptions } from '../src/cli.mjs';

const opts = parseCommon(process.argv.slice(2), {
  'api-base-url': { type: 'string' },
  'dev-api-base-url': { type: 'string' },
  'read-secret-id': { type: 'string' },
  'write-secret-id': { type: 'string' },
  deploy: { type: 'boolean', default: false },
});
if (opts.help) {
  console.log('see the header of scripts/provision.mjs');
  process.exit(0);
}
requireOptions(opts, ['workspace-id', 'api-base-url', ...(opts.apply ? ['api-key-secret-id', 'read-secret-id', 'write-secret-id'] : [])]);
for (const k of ['api-base-url', 'dev-api-base-url']) {
  if (opts[k] && !/^https:\/\/[^\s/]+(\/[^\s]*)?$/.test(opts[k])) throw new Error(`--${k} must be an https URL`);
}
if (opts.deploy && !opts.apply) throw new Error('--deploy requires --apply');

const cfg = { apiBaseUrl: opts['api-base-url'], devApiBaseUrl: opts['dev-api-base-url'] };
const plannedSecrets = { read: `<Secrets Manager ${opts['read-secret-id'] ?? '?'}>`, write: `<Secrets Manager ${opts['write-secret-id'] ?? '?'}>` };

// Validate with the real URLs before anything leaves this machine.
const errors = validateAssets(allAssets(cfg, plannedSecrets));
if (errors.length) {
  console.error(`validation failed:\n${errors.join('\n')}`);
  process.exit(1);
}

if (!opts.apply) {
  const a = allAssets(cfg, plannedSecrets);
  console.log('dry run (no changes). Would create or update, in this order:');
  for (const s of a.secrets) console.log(`  secret            ${s.name}  value ${s.secretValue}`);
  for (const v of a.contextVariables) console.log(`  context variable  ${v.name}`);
  for (const d of a.dataRequests) console.log(`  data request      ${d.dataRequestId}  ${d.webhook?.environments?.production?.url}`);
  for (const f of a.flows) console.log(`  flow              ${f.flowId}  (${Object.keys(f.nodes).length} nodes)`);
  console.log(`  application       ${a.application.name}`);
  console.log('re-run with --apply to make these changes.');
  process.exit(0);
}

const { sm, acxd } = await acxdClient({
  region: opts.region,
  workspaceId: opts['workspace-id'],
  apiKeySecretId: opts['api-key-secret-id'],
  apiKeyJsonKey: opts['api-key-json-key'],
});
const secretValues = {
  read: await readSecret(sm, opts['read-secret-id']),
  write: await readSecret(sm, opts['write-secret-id']),
};
const assets = allAssets(cfg, secretValues);

/** @param {string} label @param {() => Promise<unknown>} create @param {() => Promise<unknown>} update */
async function upsert(label, create, update) {
  try {
    await create();
    console.log(`created ${label}`);
    return 'created';
  } catch (e) {
    if (httpStatus(e) !== 409 && /** @type {any} */ (e)?.name !== 'ConflictException') throw e;
    await update();
    console.log(`updated ${label}`);
    return 'updated';
  }
}

/** @type {string[]} */
const created = [];

for (const s of assets.secrets) {
  assertOwned(s.name);
  const r = await upsert(
    `secret ${s.name}`,
    () => acxd.send(new sdk.CreateSecretCommand(s)),
    () => acxd.send(new sdk.UpdateSecretCommand({ secretIdentifier: s.name, secretValue: s.secretValue, description: s.description, isSensitive: s.isSensitive })),
  );
  if (r === 'created') created.push(`ACXD secret ${s.name}`);
}

for (const v of assets.contextVariables) {
  assertOwned(v.name);
  const r = await upsert(
    `context variable ${v.name}`,
    () => acxd.send(new sdk.CreateContextVariableCommand(v)),
    () => acxd.send(new sdk.UpdateContextVariableCommand({ contextVariableIdentifier: v.name, schema: v.schema, disallowExternalModification: v.disallowExternalModification })),
  );
  if (r === 'created') created.push(`ACXD context variable ${v.name}`);
}

for (const d of assets.dataRequests) {
  assertOwned(d.dataRequestId);
  const { dataRequestId, ...rest } = d;
  const r = await upsert(
    `data request ${dataRequestId}`,
    () => acxd.send(new sdk.CreateDataRequestCommand(d)),
    () => acxd.send(new sdk.UpdateDataRequestCommand({ dataRequestIdentifier: dataRequestId, ...rest })),
  );
  if (r === 'created') created.push(`ACXD data request ${dataRequestId}`);
}

for (const f of assets.flows) {
  assertOwned(f.flowId);
  const exists = await acxd
    .send(new sdk.GetFlowCommand({ flowIdentifier: f.flowId }))
    .then(() => true)
    .catch((e) => (httpStatus(e) === 404 ? false : Promise.reject(e)));
  // 'terminate' is outside the generated FlowNodeType union on purpose (see definitions.mjs).
  const input = /** @type {any} */ (f);
  if (exists) {
    const { flowId, ...rest } = input;
    await acxd.send(new sdk.UpdateFlowCommand({ flowIdentifier: flowId, ...rest }));
    console.log(`updated flow ${flowId}`);
  } else {
    await acxd.send(new sdk.CreateFlowCommand(input));
    console.log(`created flow ${f.flowId}`);
    created.push(`ACXD flow ${f.flowId}`);
  }
}

assertOwned(assets.application.name);
/** @type {any} */
let app;
let token;
do {
  const page = await acxd.send(new sdk.ListApplicationsCommand({ nextToken: token }));
  app = page.items?.find((x) => x.name === assets.application.name) ?? app;
  token = page.nextToken;
} while (token && !app);
if (app) {
  await acxd.send(new sdk.UpdateApplicationCommand({ applicationIdentifier: app.applicationId, ...assets.application }));
  console.log(`updated application ${app.name} ${app.applicationId}`);
} else {
  app = await acxd.send(new sdk.CreateApplicationCommand(assets.application));
  console.log(`created application ${app.name} ${app.applicationId}`);
  created.push(`ACXD application ${NAMES.application} ${app.applicationId}`);
}

if (opts.deploy) {
  const build = await acxd.send(new sdk.CreateApplicationBuildCommand({ applicationIdentifier: app.applicationId, description: 'acxdmem provision' }));
  // ApplicationBuild.status is a free-form string in the SDK model (Smithy `String`, max 64,
  // no enum), so terminal states are matched by keyword. Only an explicit success deploys:
  // a failure, an unknown status or the 5-minute timeout stops here.
  const SUCCEEDED = /succe|complete|ready|built/i;
  const FAILED = /fail|error|cancel|reject/i;
  let b = build;
  for (let i = 0; i < 60 && !SUCCEEDED.test(String(b.status)) && !FAILED.test(String(b.status)); i++) {
    await new Promise((res) => setTimeout(res, 5000));
    b = await acxd.send(new sdk.GetApplicationBuildCommand({ applicationIdentifier: app.applicationId, buildIdentifier: build.buildId }));
  }
  console.log(`build ${build.buildId} ${b.status}`);
  if (FAILED.test(String(b.status)) || !SUCCEEDED.test(String(b.status))) {
    console.error(`build ${build.buildId} did not succeed (status ${b.status}); not deploying`);
    process.exit(1);
  }
  const deps = await acxd.send(new sdk.ListApplicationDeploymentsCommand({ applicationIdentifier: app.applicationId }));
  const dev = deps.items?.find((d) => d.environment === 'development');
  if (dev) {
    await acxd.send(new sdk.UpdateApplicationDeploymentCommand({ applicationIdentifier: app.applicationId, deploymentIdentifier: dev.deploymentId, buildIdentifier: build.buildId, languageCodes: ['en-US'] }));
    console.log(`updated development deployment ${dev.deploymentId} alias ${dev.deploymentAlias}`);
  } else {
    const d = await acxd.send(new sdk.CreateApplicationDeploymentCommand({ applicationIdentifier: app.applicationId, buildIdentifier: build.buildId, environment: 'development', description: 'acxdmem provision' }));
    console.log(`created development deployment ${d.deploymentId} alias ${d.deploymentAlias}`);
    created.push(`ACXD deployment ${d.deploymentId} (development)`);
  }
}

if (created.length) {
  console.log('\nNew resources (record them for teardown):');
  for (const c of created) console.log(`  - ${c}`);
}
