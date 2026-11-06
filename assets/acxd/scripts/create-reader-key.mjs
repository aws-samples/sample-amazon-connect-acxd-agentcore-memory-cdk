#!/usr/bin/env node
// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Create the workspace-scoped read-only ACXD programmatic user the writer's fetch step uses
// (design.md section 3.2, threat model T6), mint one API key for it, and store the key in a
// new AWS Secrets Manager secret. The key goes straight from the ACXD response into Secrets
// Manager: it is never printed, logged or written to disk.
//
// Dry run by default. --apply makes the changes and prints the secret ARN, which is the
// `acxdApiKeySecretArn` CDK context value.
//
// Usage:
//   node scripts/create-reader-key.mjs --workspace-id <uuid> \
//     --api-key-secret-id <SM id of an admin key> [--api-key-json-key <field>] \
//     [--user-name acxdmem-reader] [--secret-name acxdmem-acxd-reader-api-key] [--apply]
//
// Teardown: delete the API key, then the programmatic user (a user with keys cannot be
// deleted), then the secret. `--delete --apply` does all three. The secret is scheduled for
// deletion with a 7-day recovery window, during which its name cannot be reused; add
// --force-delete-without-recovery to delete it at once (sample teardown).
//
// Rotation (create first, switch, then delete; docs/deployment-guide.md "Rotating the ACXD
// API key"): create a second user and secret under new names, deploy the writer with the
// new acxdApiKeySecretArn, then --delete the old user and secret by their names.

import * as sdk from 'amazon-connect-acxd-sdk';
import {
  CreateSecretCommand,
  DeleteSecretCommand,
  DescribeSecretCommand,
} from '@aws-sdk/client-secrets-manager';
import { acxdClient, httpStatus } from '../src/client.mjs';
import { parseCommon, requireOptions } from '../src/cli.mjs';
import { assertOwned } from '../src/names.mjs';

const opts = parseCommon(process.argv.slice(2), {
  'user-name': { type: 'string', default: 'acxdmem-reader' },
  'secret-name': { type: 'string', default: 'acxdmem-acxd-reader-api-key' },
  delete: { type: 'boolean', default: false },
  'force-delete-without-recovery': { type: 'boolean', default: false },
});
const forceDelete = opts['force-delete-without-recovery'];
requireOptions(opts, ['workspace-id', ...(opts.apply ? ['api-key-secret-id'] : [])]);
const userName = opts['user-name'];
const secretName = opts['secret-name'];
assertOwned(userName);
assertOwned(secretName);

if (!opts.apply) {
  console.log(
    opts.delete
      ? `dry run: would delete the API keys and programmatic user ${userName}, then secret ${secretName} (${forceDelete ? 'without recovery' : '7-day recovery window'})`
      : `dry run: would create programmatic user ${userName} (read-only on ${opts['workspace-id']}), mint one key and store it in secret ${secretName}`,
  );
  process.exit(0);
}

const { sm, acxd } = await acxdClient({
  region: opts.region,
  workspaceId: opts['workspace-id'],
  apiKeySecretId: opts['api-key-secret-id'],
  apiKeyJsonKey: opts['api-key-json-key'],
});

/** @returns {Promise<any>} */
async function findUser() {
  let token;
  do {
    const page = await acxd.send(new sdk.ListProgrammaticUsersCommand({ nextToken: token }));
    const hit = page.items?.find((u) => u.name === userName);
    if (hit) return hit;
    token = page.nextToken;
  } while (token);
  return undefined;
}

if (opts.delete) {
  const user = await findUser();
  if (user) {
    const keys = await acxd.send(new sdk.ListApiTokensCommand({ userId: user.userId }));
    for (const k of keys.items ?? []) {
      await acxd.send(new sdk.DeleteApiTokenCommand({ userId: user.userId, keyPrefix: k.keyPrefix }));
      console.log(`deleted API key ${k.keyPrefix}`);
    }
    await acxd.send(new sdk.DeleteProgrammaticUserCommand({ userId: user.userId }));
    console.log(`deleted programmatic user ${userName} ${user.userId}`);
  } else {
    console.log(`programmatic user ${userName} not found`);
  }
  const deletion = forceDelete ? { ForceDeleteWithoutRecovery: true } : { RecoveryWindowInDays: 7 };
  await sm
    .send(new DeleteSecretCommand({ SecretId: secretName, ...deletion }))
    .then(() =>
      console.log(
        forceDelete
          ? `deleted secret ${secretName} without recovery`
          : `scheduled deletion of secret ${secretName} (7-day recovery window; the name is reserved until then)`,
      ),
    )
    .catch((e) => console.log(`secret ${secretName}: ${e.name}`));
  process.exit(0);
}

const exists = await sm
  .send(new DescribeSecretCommand({ SecretId: secretName }))
  .then(() => true)
  .catch((e) => (e.name === 'ResourceNotFoundException' ? false : Promise.reject(e)));
if (exists) {
  throw new Error(
    `secret ${secretName} already exists (or is scheduled for deletion); pass another --secret-name, or delete it with --force-delete-without-recovery`,
  );
}
if (await findUser()) throw new Error(`programmatic user ${userName} already exists; run with --delete first`);

const user = await acxd.send(
  new sdk.CreateProgrammaticUserCommand({
    name: userName,
    roleConfig: { workspaceRoles: [{ workspaceId: opts['workspace-id'], role: 'read-only' }] },
  }),
);
console.log(`created programmatic user ${userName} ${user.userId} (read-only)`);
try {
  const tok = await acxd.send(
    new sdk.CreateApiTokenCommand({ userId: user.userId, description: 'acxdmem writer fetch step (GetConversation)' }),
  );
  if (!tok.token) throw new Error('CreateApiToken returned no token');
  const out = await sm.send(
    new CreateSecretCommand({
      Name: secretName,
      Description: 'acxdmem: API key of the read-only ACXD programmatic user for the writer fetch step',
      SecretString: tok.token,
      Tags: [{ Key: 'project', Value: 'acxdmem' }],
    }),
  );
  console.log(`minted API key ${tok.keyPrefix}, stored in ${out.ARN}`);
  console.log(`set CDK context acxdApiKeySecretArn=${out.ARN}`);
} catch (e) {
  console.error(`failed after creating the user (${httpStatus(e) ?? e}); run with --delete --apply to clean up`);
  throw e;
}
