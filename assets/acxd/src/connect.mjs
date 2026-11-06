// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Amazon Connect Customer flow snippet: Customer Profiles lookup on the caller's phone number,
// then the Agentic CX block with nlx_userId = $.Customer.ProfileId (design.md section 5, I2).
//
// The snippet is applied by the builder; nothing here calls Connect. renderConnectSnippet()
// fills the three ACXD placeholders, checkConnectSnippet() enforces the design rules:
//  - nlx_userId comes only from $.Customer.ProfileId, never from contact attributes (T9).
//  - nothing reads $.AgenticCX.ContextVariables back into the contact (T10, design 4.1).

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { PLACEHOLDERS } from './names.mjs';

export const SNIPPET_TEMPLATE_PATH = fileURLToPath(new URL('../connect/acxdmem-inbound-snippet.template.json', import.meta.url));

/** Flow language action identifiers are at most 50 characters (Connect API reference). */
const MAX_IDENTIFIER = 50;

/** @returns {any} */
export function loadSnippetTemplate() {
  return JSON.parse(readFileSync(SNIPPET_TEMPLATE_PATH, 'utf8'));
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/**
 * @param {any} template
 * @param {{ workspaceId: string, applicationId: string, alias: string }} values
 */
export function renderConnectSnippet(template, values) {
  if (!UUID.test(values.workspaceId)) throw new Error('workspaceId must be a UUID');
  if (!UUID.test(values.applicationId)) throw new Error('applicationId must be a UUID');
  if (!/^[A-Za-z0-9_-]+$/.test(values.alias)) throw new Error('alias must be a deployment alias');
  const json = JSON.stringify(template)
    .replaceAll(PLACEHOLDERS.workspaceId, values.workspaceId)
    .replaceAll(PLACEHOLDERS.applicationId, values.applicationId)
    .replaceAll(PLACEHOLDERS.deploymentAlias, values.alias);
  return JSON.parse(json);
}

/** @param {any} flow @returns {string[]} errors */
export function checkConnectSnippet(flow) {
  /** @type {string[]} */
  const errors = [];
  const actions = flow.Actions ?? [];
  const ids = new Set(actions.map((/** @type {any} */ a) => a.Identifier));
  if (flow.Version !== '2019-10-30') errors.push('Version must be 2019-10-30');
  if (!ids.has(flow.StartAction)) errors.push(`StartAction ${flow.StartAction} does not exist`);
  if (ids.size !== actions.length) errors.push('duplicate action identifiers');

  for (const a of actions) {
    if (String(a.Identifier).length > MAX_IDENTIFIER) errors.push(`${a.Identifier}: identifier longer than ${MAX_IDENTIFIER}`);
    const t = a.Transitions ?? {};
    const targets = [t.NextAction, ...(t.Conditions ?? []).map((/** @type {any} */ c) => c.NextAction), ...(t.Errors ?? []).map((/** @type {any} */ e) => e.NextAction)];
    for (const n of targets.filter(Boolean)) if (!ids.has(n)) errors.push(`${a.Identifier}: transition to missing action ${n}`);

    if (a.Type === 'ConnectParticipantWithAgenticCX') {
      const vars = a.Parameters?.AgentConfiguration?.ContextVariables ?? {};
      const keys = Object.keys(vars);
      if (keys.length > 10) errors.push(`${a.Identifier}: more than 10 context variables`);
      for (const [k, v] of Object.entries(vars)) {
        if (k === 'nlx_userId' && v !== '$.Customer.ProfileId') {
          errors.push(`${a.Identifier}: nlx_userId must be $.Customer.ProfileId (authenticated profile), got ${v}`);
        }
        if (String(v).startsWith('$.Attributes.')) {
          errors.push(`${a.Identifier}: ${k} comes from a contact attribute, which a client can set (T9)`);
        }
      }
    }
    if (JSON.stringify(a.Parameters ?? {}).includes('$.AgenticCX.ContextVariables')) {
      errors.push(`${a.Identifier}: reads $.AgenticCX.ContextVariables; memory text must not be copied into the contact (T10)`);
    }
  }
  return errors;
}
