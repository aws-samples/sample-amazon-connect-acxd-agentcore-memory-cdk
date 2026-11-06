// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Resource names and placeholders for the ACXD-side assets.
//
// Every name starts with PREFIX so that provision/teardown can refuse to touch anything else
// in the workspace. Name formats follow the ACXD SDK service model (checked 2026-10-08):
//   ContextVariableName ^(?!acxd_context)[A-Za-z_]+$, 1-64   (model: contextVariables)
//   DataRequestId       ^[A-Za-z0-9]+$, 3-100                (model: dataRequests)
//   SecretName          ^[A-Za-z0-9_]+$, 3-100               (model: secrets)
//   FlowId              ^[A-Za-z]+$, 3-64                    (model: flows)
//   ApplicationName     ^[A-Za-z0-9 _-]+$, 1-100             (model: applications)
// so hyphens are only possible in the application name.

export const PREFIX = 'acxdmem';

export const NAMES = Object.freeze({
  contextVariable: 'acxdmemMemoryContext',
  readSecret: 'acxdmemReadSecret',
  writeSecret: 'acxdmemWriteSecret',
  readRequest: 'acxdmemRead',
  writeRequest: 'acxdmemWrite',
  exitFlow: 'acxdmemExit',
  sampleFlow: 'acxdmemSample',
  application: 'acxdmem-sample',
});

/** Header that carries the per-route shared secret (design.md section 6). */
export const SECRET_HEADER = 'x-acxdmem-secret';

/** API Gateway routes of the CDK stack (design.md section 6). */
export const ROUTES = Object.freeze({ read: '/memory/read', write: '/memory/write' });

/**
 * Placeholders used in the exported JSON. `scripts/provision.mjs` substitutes them; the
 * Connect snippet placeholders are substituted by the builder (or `renderConnectSnippet`).
 */
export const PLACEHOLDERS = Object.freeze({
  apiBaseUrl: '__ACXDMEM_API_BASE_URL__',
  devApiBaseUrl: '__ACXDMEM_DEV_API_BASE_URL__',
  secretValue: '__FROM_AWS_SECRETS_MANAGER__',
  workspaceId: '__ACXD_WORKSPACE_ID__',
  applicationId: '__ACXD_APPLICATION_ID__',
  deploymentAlias: '__ACXD_DEPLOYMENT_ALIAS__',
});

/** ACXD template tokens (design.md sections 3.2, 4, 5; verified in the spike S2, S6, S8). */
export const TOKENS = Object.freeze({
  userId: '{System.userId:NLX.System}',
  conversationId: '{System.conversationId:NLX.System}',
  lastFlow: '{System.lastFlow:NLX.System}',
  memoryContext: `{${NAMES.contextVariable}:NLX.Context}`,
  secret: (name) => `{${name}:NLX.Secret}`,
});

/** True when the id belongs to this sample. Case-insensitive like the spike guard. */
export function isOwned(id) {
  return String(id).toLowerCase().startsWith(PREFIX);
}

/** Throws unless the id belongs to this sample. */
export function assertOwned(id) {
  if (!isOwned(id)) throw new Error(`refusing to touch non-${PREFIX} resource: ${id}`);
}
