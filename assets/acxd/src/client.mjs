// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// ACXD SDK client whose API key comes from AWS Secrets Manager, never from the environment
// or the command line, so it does not land in shell history or process listings.

import { GetSecretValueCommand, SecretsManagerClient } from '@aws-sdk/client-secrets-manager';
import { AgenticCXDesignerClient } from 'amazon-connect-acxd-sdk';

/**
 * Read a secret string. A JSON object secret is accepted when `jsonKey` names the field.
 * @param {SecretsManagerClient} sm
 * @param {string} secretId name or ARN
 * @param {string} [jsonKey]
 */
export async function readSecret(sm, secretId, jsonKey) {
  const out = await sm.send(new GetSecretValueCommand({ SecretId: secretId }));
  const raw = out.SecretString;
  if (!raw) throw new Error(`secret ${secretId} has no SecretString`);
  if (!jsonKey) return raw;
  const parsed = JSON.parse(raw);
  if (typeof parsed[jsonKey] !== 'string' || !parsed[jsonKey]) throw new Error(`secret ${secretId} has no string field ${jsonKey}`);
  return parsed[jsonKey];
}

/**
 * @param {{ region: string, workspaceId: string, apiKeySecretId: string, apiKeyJsonKey?: string }} opts
 */
export async function acxdClient(opts) {
  const sm = new SecretsManagerClient({ region: opts.region });
  const apiKey = await readSecret(sm, opts.apiKeySecretId, opts.apiKeyJsonKey);
  return {
    sm,
    acxd: new AgenticCXDesignerClient({ apiKey, workspaceId: opts.workspaceId, region: opts.region }),
  };
}

/** @param {unknown} e */
export const httpStatus = (e) => /** @type {any} */ (e)?.$metadata?.httpStatusCode;
