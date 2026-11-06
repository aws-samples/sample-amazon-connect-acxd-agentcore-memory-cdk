// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Turns resources read back from a live workspace into placeholder JSON fit for the repository:
// no URLs, workspace/application/build ids, timestamps, user names or secret values.

import { PLACEHOLDERS, ROUTES } from './names.mjs';

/** Server-managed fields dropped on export (Flow, DataRequest, ContextVariable, Application shapes). */
const SERVER_FIELDS = new Set(['createdAt', 'updatedAt', 'updatedBy', 'saveId', 'applicationId', 'workspaceId', 'canvasMetadata']);

/**
 * Replace a webhook URL with the placeholder base URL, keeping the known route.
 * @param {string} url @param {string} base
 */
function placeholderUrl(url, base) {
  const route = Object.values(ROUTES).find((r) => String(url).endsWith(r));
  return route ? `${base}${route}` : `${base}/__UNKNOWN_ROUTE__`;
}

/** @param {any} value @returns {any} */
function strip(value) {
  if (Array.isArray(value)) return value.map(strip);
  if (!value || typeof value !== 'object') return value;
  /** @type {Record<string, any>} */
  const out = {};
  for (const [k, v] of Object.entries(value)) {
    if (SERVER_FIELDS.has(k) || k === '$metadata') continue;
    out[k] = strip(v);
  }
  return out;
}

/** @param {any} dr a DataRequest from GetDataRequest */
export function sanitizeDataRequest(dr) {
  const out = strip(dr);
  const envs = out.webhook?.environments ?? {};
  if (envs.production) envs.production.url = placeholderUrl(envs.production.url, PLACEHOLDERS.apiBaseUrl);
  if (envs.development) envs.development.url = placeholderUrl(envs.development.url, PLACEHOLDERS.devApiBaseUrl);
  if (out.webhook?.url) out.webhook.url = placeholderUrl(out.webhook.url, PLACEHOLDERS.apiBaseUrl);
  // Header values are secret references ({name:NLX.Secret}); a literal value is never exported.
  for (const env of Object.values(envs)) {
    for (const h of /** @type {any} */ (env).headers ?? []) {
      if (!/^\{[A-Za-z0-9_]+:NLX\.Secret\}$/.test(h.value)) h.value = PLACEHOLDERS.secretValue;
    }
  }
  return out;
}

/** @param {any} flow a Flow from GetFlow */
export const sanitizeFlow = (flow) => strip(flow);

/** @param {any} cv a ContextVariable from ListContextVariables */
export const sanitizeContextVariable = (cv) => strip(cv);

/** @param {any} app an Application from GetApplication; deploymentSettings may hold ids */
export function sanitizeApplication(app) {
  const out = strip(app);
  delete out.deploymentSettings;
  return out;
}

/** @param {any} secret a SecretSummary; the value is never exported */
export function sanitizeSecret(secret) {
  const out = strip(secret);
  out.secretValue = PLACEHOLDERS.secretValue;
  return out;
}
