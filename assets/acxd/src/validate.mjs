// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Validates the ACXD asset definitions against the ACXD SDK service model and the design rules.
//
// Two sources of truth:
//  1. Enums are read at runtime from amazon-connect-acxd-sdk, which is generated from the
//     model, so an enum value the model does not know fails here.
//  2. Constraint traits (@pattern, @length, @range, @required) are not carried by the
//     generated client, so the ones that apply to the shapes we write are restated below with
//     the model area they come from (checked 2026-10-08).
// Design rules (docs/design.md) are checked after the model rules; they are what keeps a
// future edit from silently re-introducing a spike finding (S2, S4, S9).

import * as sdk from 'amazon-connect-acxd-sdk';
import { NAMES, PREFIX, SECRET_HEADER, TOKENS, isOwned } from './names.mjs';
import { EXIT_NODE_TYPE, READ_LAMBDA_TIMEOUT_MS } from './definitions.mjs';

/** ACXD default data request timeout when a node sets none (spike S4, observed). */
const DEFAULT_DR_TIMEOUT_MS = 5000;

const enumValues = (e) => new Set(Object.values(e));

export const ENUMS = Object.freeze({
  flowNodeType: enumValues(sdk.FlowNodeType),
  operandType: enumValues(sdk.OperandType),
  conditionOperator: enumValues(sdk.ConditionOperator),
  stateModificationType: enumValues(sdk.FlowStateModificationType),
  stateModificationAction: enumValues(sdk.FlowStateModificationAction),
  redirectType: enumValues(sdk.RedirectType),
  messageType: enumValues(sdk.MessageType),
  languageCode: enumValues(sdk.LanguageCode),
  generativeModelType: enumValues(sdk.GenerativeModelType),
  dataRequestFieldType: enumValues(sdk.DataRequestFieldType),
  webhookImplementation: enumValues(sdk.WebhookImplementation),
  webhookMethod: enumValues(sdk.WebhookMethod),
  contextVariableType: enumValues(sdk.ContextVariableType),
});

/** Node types accepted beyond the model enum. Only the Exit application node, see definitions.mjs. */
export const NODE_TYPE_DEVIATIONS = Object.freeze(new Set([EXIT_NODE_TYPE]));

// Constraint traits restated from the model.
export const RULES = Object.freeze({
  // model: commons
  uuidV4: /^[0-9a-f]{8}-[0-9a-f]{4}-[0-5][0-9a-f]{3}-[089ab][0-9a-f]{3}-[0-9a-f]{12}$/,
  // model: contextVariables ContextVariableName
  contextVariableName: { pattern: /^(?!acxd_context)[A-Za-z_]+$/, min: 1, max: 64 },
  // model: secrets SecretName, SecretValue, Description
  secretName: { pattern: /^[A-Za-z0-9_]+$/, min: 3, max: 100 },
  secretValue: { min: 1, max: 4096 },
  secretDescription: { pattern: /^[^\p{C}]*$/u, min: 0, max: 200 },
  // model: dataRequests DataRequestId, description, WebhookEnvironment.url, WebhookHeader
  dataRequestId: { pattern: /^[A-Za-z0-9]+$/, min: 3, max: 100 },
  dataRequestDescription: { max: 200 },
  webhookUrl: { max: 2048 },
  headerKey: { max: 128 },
  headerValue: { max: 4096 },
  // model: flows FlowId, RedirectTarget, FlowDescription, FlowAiDescription, metadata.timeout,
  // NodeDataRequestList, GenerativeJourneyConfig.temperature
  flowId: { pattern: /^[A-Za-z]+$/, min: 3, max: 64 },
  redirectTarget: { pattern: /^[A-Za-z.{}:]+$/, min: 3, max: 64 },
  flowDescription: { pattern: /^[ -~]*$/, max: 200 },
  flowAiDescription: { pattern: /^[ -~]*$/, max: 1000 },
  nodeTimeoutMax: 30000,
  nodeDataRequestsMax: 10,
  temperature: { min: 0, max: 2 },
  // model: applications ApplicationName, ApplicationDescription, conversationTTL
  applicationName: { pattern: /^[A-Za-z0-9 _-]+$/, min: 1, max: 100 },
  applicationDescription: { pattern: /^[ -~]*$/, max: 200 },
  conversationTTL: { min: 1, max: 60 },
});

/** Data request statuses a Data request node must wire (design.md section 3.1). */
const REQUIRED_DR_PATHS = ['success', 'failure', 'timeout'];

class Report {
  constructor() {
    /** @type {string[]} */
    this.errors = [];
  }
  /** @param {string} where @param {string} msg */
  add(where, msg) {
    this.errors.push(`${where}: ${msg}`);
  }
  /**
   * @param {string} where @param {unknown} value
   * @param {{ pattern?: RegExp, min?: number, max?: number }} rule
   */
  str(where, value, rule) {
    if (typeof value !== 'string') return this.add(where, `expected string, got ${typeof value}`);
    if (rule.min !== undefined && value.length < rule.min) this.add(where, `shorter than ${rule.min}`);
    if (rule.max !== undefined && value.length > rule.max) this.add(where, `longer than ${rule.max}`);
    if (rule.pattern && !rule.pattern.test(value)) this.add(where, `does not match ${rule.pattern}`);
  }
  /** @param {string} where @param {unknown} value @param {Set<unknown>} allowed @param {string} name */
  oneOf(where, value, allowed, name) {
    if (!allowed.has(value)) this.add(where, `${JSON.stringify(value)} is not a ${name} value`);
  }
  /** @param {string} where @param {string} id */
  owned(where, id) {
    if (!isOwned(id)) this.add(where, `${id} does not start with ${PREFIX}`);
  }
}

/** @param {unknown} url */
const isPlaceholder = (url) => typeof url === 'string' && /__[A-Z0-9_]+__/.test(url);

// --- operands and state ------------------------------------------------------------------

/** @param {Report} r @param {string} where @param {any} op */
function checkOperand(r, where, op) {
  if (!op || typeof op !== 'object') return r.add(where, 'operand missing');
  r.oneOf(`${where}.type`, op.type, ENUMS.operandType, 'OperandType');
  if (op.type === 'recursive') {
    for (const [k, v] of Object.entries(op.value ?? {})) checkOperand(r, `${where}.${k}`, v);
  }
}

/** @param {Report} r @param {string} where @param {any[]} mods @param {Set<string>} contextVars */
function checkStateModifications(r, where, mods = [], contextVars) {
  mods.forEach((m, i) => {
    const w = `${where}.stateModifications[${i}]`;
    r.oneOf(`${w}.type`, m.type, ENUMS.stateModificationType, 'FlowStateModificationType');
    r.oneOf(`${w}.modification`, m.modification, ENUMS.stateModificationAction, 'FlowStateModificationAction');
    if (m.type === 'context' && !contextVars.has(m.name)) r.add(w, `context variable ${m.name} is not defined`);
    if (m.value) checkOperand(r, `${w}.value`, m.value);
  });
}

// --- resources ---------------------------------------------------------------------------

/** @param {Report} r @param {any[]} vars */
function checkContextVariables(r, vars) {
  for (const v of vars) {
    const w = `contextVariable ${v.name}`;
    r.str(`${w}.name`, v.name, RULES.contextVariableName);
    r.owned(w, v.name);
    r.oneOf(`${w}.schema.type`, v.schema?.type, ENUMS.contextVariableType, 'ContextVariableType');
  }
}

/** @param {Report} r @param {any[]} list */
function checkSecrets(r, list) {
  for (const s of list) {
    const w = `secret ${s.name}`;
    r.str(`${w}.name`, s.name, RULES.secretName);
    r.owned(w, s.name);
    r.str(`${w}.secretValue`, s.secretValue, RULES.secretValue);
    if (s.description !== undefined) r.str(`${w}.description`, s.description, RULES.secretDescription);
    if (s.isSensitive !== true) r.add(w, 'isSensitive must be true');
  }
}

/** @param {Report} r @param {any[]} list @param {Set<string>} secretNames */
function checkDataRequests(r, list, secretNames) {
  for (const d of list) {
    const w = `dataRequest ${d.dataRequestId}`;
    r.str(`${w}.dataRequestId`, d.dataRequestId, RULES.dataRequestId);
    r.owned(w, d.dataRequestId);
    r.oneOf(`${w}.type`, d.type, ENUMS.dataRequestFieldType, 'DataRequestFieldType');
    if (d.description !== undefined) r.str(`${w}.description`, d.description, RULES.dataRequestDescription);
    if (d.requestSchema?.additionalProperties !== false) r.add(w, 'design: requestSchema must set additionalProperties false');
    const wh = d.webhook ?? {};
    r.oneOf(`${w}.webhook.implementation`, wh.implementation, ENUMS.webhookImplementation, 'WebhookImplementation');
    if (wh.implementation !== 'external') r.add(w, 'design: implementation must be external');
    r.oneOf(`${w}.webhook.method`, wh.method, ENUMS.webhookMethod, 'WebhookMethod');
    if (wh.sendContext !== false) r.add(w, 'design: sendContext must be false (memory text would leak into nlx_context)');
    const envs = Object.entries(wh.environments ?? {});
    if (envs.length === 0) r.add(w, 'no webhook environments');
    for (const [env, cfg] of envs) {
      const we = `${w}.webhook.environments.${env}`;
      r.str(`${we}.url`, cfg.url, { ...RULES.webhookUrl, min: 1 });
      if (!isPlaceholder(cfg.url) && !String(cfg.url).startsWith('https://')) r.add(we, 'url must be https');
      const headers = cfg.headers ?? [];
      for (const h of headers) {
        r.str(`${we}.header.key`, h.key, { ...RULES.headerKey, min: 1 });
        r.str(`${we}.header.value`, h.value, { ...RULES.headerValue, min: 1 });
      }
      const auth = headers.find((h) => h.key === SECRET_HEADER);
      if (!auth) {
        r.add(we, `design: missing ${SECRET_HEADER} header`);
        continue;
      }
      const m = /^\{([A-Za-z0-9_]+):NLX\.Secret\}$/.exec(auth.value);
      if (!m) r.add(we, `design: ${SECRET_HEADER} must reference an ACXD Secret, never a literal value`);
      else if (!secretNames.has(m[1])) r.add(we, `secret ${m[1]} is not defined`);
      if (auth.sensitive !== true) r.add(we, `design: ${SECRET_HEADER} header must be sensitive`);
    }
  }
}

/**
 * @param {Report} r @param {any} flow
 * @param {{ contextVars: Set<string>, dataRequests: Set<string>, flowIds: Set<string> }} ctx
 */
function checkFlow(r, flow, ctx) {
  const w = `flow ${flow.flowId}`;
  r.str(`${w}.flowId`, flow.flowId, RULES.flowId);
  r.owned(w, flow.flowId);
  if (flow.description !== undefined) r.str(`${w}.description`, flow.description, RULES.flowDescription);
  if (flow.aiDescription !== undefined) r.str(`${w}.aiDescription`, flow.aiDescription, RULES.flowAiDescription);
  for (const lc of [flow.mainLanguageCode, ...(flow.languageCodes ?? [])].filter(Boolean)) {
    r.oneOf(`${w}.languageCode`, lc, ENUMS.languageCode, 'LanguageCode');
  }

  const nodes = flow.nodes ?? {};
  const ids = new Set(Object.keys(nodes));
  const starts = Object.values(nodes).filter((n) => n.type === 'start');
  if (starts.length !== 1) r.add(w, `expected exactly one start node, found ${starts.length}`);

  for (const [key, node] of Object.entries(nodes)) {
    const wn = `${w} node ${node.metadata?.name ?? node.type} (${key})`;
    if (!RULES.uuidV4.test(key)) r.add(wn, 'node key is not a UUIDv4');
    if (node.nodeId !== key) r.add(wn, 'nodeId does not equal its map key');
    if (!ENUMS.flowNodeType.has(node.type) && !NODE_TYPE_DEVIATIONS.has(node.type)) {
      r.add(wn, `${JSON.stringify(node.type)} is not a FlowNodeType value`);
    }
    if (node.type === 'end') {
      r.add(wn, `design: use ${EXIT_NODE_TYPE} for Exit application; an end node does not end the conversation`);
    }
    for (const c of node.childNodes ?? []) {
      if (c.nodeId !== undefined && !ids.has(c.nodeId)) r.add(wn, `child ${c.nodeId} does not exist`);
      for (const cond of c.conditions ?? []) {
        r.oneOf(`${wn}.condition.operator`, cond.operator, ENUMS.conditionOperator, 'ConditionOperator');
        checkOperand(r, `${wn}.condition.left`, cond.left);
        if (cond.right) checkOperand(r, `${wn}.condition.right`, cond.right);
      }
    }
    for (const m of node.messages ?? []) {
      r.oneOf(`${wn}.message.type`, m.type, ENUMS.messageType, 'MessageType');
      if (m.messageId !== undefined && !RULES.uuidV4.test(m.messageId)) r.add(wn, `messageId ${m.messageId} is not a UUIDv4`);
    }
    const meta = node.metadata ?? {};
    if (meta.timeout !== undefined && (meta.timeout < 0 || meta.timeout > RULES.nodeTimeoutMax)) {
      r.add(wn, `metadata.timeout above ${RULES.nodeTimeoutMax}`);
    }
    checkStateModifications(r, wn, meta.stateModifications, ctx.contextVars);

    if ((node.dataRequests ?? []).length > RULES.nodeDataRequestsMax) r.add(wn, 'too many dataRequests');
    for (const dr of node.dataRequests ?? []) {
      if (!ctx.dataRequests.has(dr.dataRequestId)) r.add(wn, `data request ${dr.dataRequestId} is not defined`);
      if (dr.payload) checkOperand(r, `${wn}.payload`, dr.payload);
    }

    if (node.type === 'data_request') {
      const statuses = new Set((node.childNodes ?? []).flatMap((c) => (c.conditions ?? []).map((x) => x.right?.value)));
      for (const s of REQUIRED_DR_PATHS) if (!statuses.has(s)) r.add(wn, `design: ${s} path not wired`);
      const calls = (/** @type {string} */ id) => (node.dataRequests ?? []).some((d) => d.dataRequestId === id);
      if (statuses.has('in_progress') && (calls(NAMES.writeRequest) || calls(NAMES.readRequest))) {
        r.add(wn, 'design: memory data request nodes must not have an In progress path (spike S4)');
      }
      if (calls(NAMES.readRequest) && (meta.timeout ?? DEFAULT_DR_TIMEOUT_MS) <= READ_LAMBDA_TIMEOUT_MS) {
        r.add(wn, `design: read node timeout must exceed the ${READ_LAMBDA_TIMEOUT_MS} ms read Lambda timeout`);
      }
    }

    if (node.type === 'redirect') {
      const red = meta.redirect;
      if (!red) {
        r.add(wn, 'redirect node without metadata.redirect');
      } else {
        r.oneOf(`${wn}.redirect.type`, red.type, ENUMS.redirectType, 'RedirectType');
        r.str(`${wn}.redirect.flowId`, red.flowId, RULES.redirectTarget);
        const dynamic = /^\{System\.[A-Za-z]+:NLX\.System\}$/.test(red.flowId);
        if (!dynamic && !ctx.flowIds.has(red.flowId)) r.add(wn, `redirect target ${red.flowId} is not a defined flow`);
        const continuation = (node.childNodes ?? []).find((c) =>
          (c.conditions ?? []).some((x) => x.left?.type === 'node_status' && x.right?.value === 'continuation'),
        );
        if (red.flowId === NAMES.exitFlow && !continuation?.nodeId) {
          r.add(wn, 'design: a redirect into the exit sub-flow must wire its Continuation (return address)');
        }
      }
    }

    if ((node.type === EXIT_NODE_TYPE || node.type === 'escalate') && flow.flowId !== NAMES.exitFlow) {
      const clears = (meta.stateModifications ?? []).some(
        (m) => m.type === 'context' && m.name === NAMES.contextVariable && m.modification === 'clear',
      );
      if (!clears) r.add(wn, `design: terminal node must clear ${NAMES.contextVariable} (spike S9)`);
    }

    if (node.type === 'generative_journey') {
      const gj = meta.generativeJourney ?? {};
      r.oneOf(`${wn}.modelType`, gj.modelType, ENUMS.generativeModelType, 'GenerativeModelType');
      if (gj.temperature !== undefined && (gj.temperature < RULES.temperature.min || gj.temperature > RULES.temperature.max)) {
        r.add(wn, 'temperature out of range');
      }
    }
  }

  // Reachability from start: an unreachable node is dead design, usually a mis-wired id.
  if (starts.length === 1) {
    const seen = new Set([starts[0].nodeId]);
    const queue = [starts[0].nodeId];
    while (queue.length) {
      const n = nodes[queue.shift()];
      for (const c of n?.childNodes ?? []) {
        if (c.nodeId && !seen.has(c.nodeId)) {
          seen.add(c.nodeId);
          queue.push(c.nodeId);
        }
      }
    }
    for (const id of ids) if (!seen.has(id)) r.add(w, `node ${id} is not reachable from start`);
  }
}

/** Design rules specific to the exit sub-flow. @param {Report} r @param {any} flow */
function checkExitFlow(r, flow) {
  const nodes = Object.values(flow.nodes ?? {});
  const w = `flow ${flow.flowId}`;
  if (!nodes.some((n) => n.type === 'data_request' && n.dataRequests?.some((d) => d.dataRequestId === NAMES.writeRequest))) {
    r.add(w, `design: exit sub-flow must call ${NAMES.writeRequest}`);
  }
  for (const n of nodes) {
    if (n.type === 'redirect' && n.metadata?.redirect?.flowId === TOKENS.lastFlow) {
      const clears = (n.metadata?.stateModifications ?? []).some(
        (m) => m.type === 'context' && m.name === NAMES.contextVariable && m.modification === 'clear',
      );
      if (!clears) r.add(w, `design: the return to the caller must clear ${NAMES.contextVariable}`);
    }
    if (n.type === EXIT_NODE_TYPE || n.type === 'escalate') {
      r.add(w, 'design: the exit sub-flow returns to its caller and must not end or escalate itself');
    }
    const leaf = (n.childNodes ?? []).filter((c) => c.nodeId).length === 0;
    if (leaf && !(n.type === 'redirect' && n.metadata?.redirect?.flowId === TOKENS.lastFlow)) {
      r.add(w, `design: path ending at ${n.nodeId} does not return through ${TOKENS.lastFlow}`);
    }
  }
}

/** @param {Report} r @param {any} app @param {Set<string>} flowIds @param {any[]} flowList */
function checkApplication(r, app, flowIds, flowList) {
  const w = `application ${app.name}`;
  r.str(`${w}.name`, app.name, RULES.applicationName);
  r.owned(w, app.name);
  if (app.description !== undefined) r.str(`${w}.description`, app.description, RULES.applicationDescription);
  const ttl = app.settings?.conversationTTL;
  if (ttl !== undefined && (ttl < RULES.conversationTTL.min || ttl > RULES.conversationTTL.max)) r.add(w, 'conversationTTL out of range');
  for (const lc of [app.settings?.languageCode, ...(app.settings?.languageCodes ?? [])].filter(Boolean)) {
    r.oneOf(`${w}.languageCode`, lc, ENUMS.languageCode, 'LanguageCode');
  }
  const attached = new Set((app.flows ?? []).map((f) => f.flowId));
  for (const id of attached) if (!flowIds.has(id)) r.add(w, `attached flow ${id} is not defined`);
  for (const [k, v] of Object.entries(app.settings?.defaultFlows ?? {})) {
    if (v?.flowId && !attached.has(v.flowId)) r.add(w, `defaultFlows.${k} ${v.flowId} is not attached`);
  }
  // A redirect target must be attached to the application (ACXD docs: Redirect node).
  for (const f of flowList) {
    if (!attached.has(f.flowId)) continue;
    for (const n of Object.values(f.nodes ?? {})) {
      const t = /** @type {any} */ (n).metadata?.redirect?.flowId;
      if (t && flowIds.has(t) && !attached.has(t)) r.add(w, `flow ${f.flowId} redirects to ${t}, which is not attached`);
    }
  }
}

/**
 * Validate a full asset set as produced by `allAssets()`.
 * @param {ReturnType<typeof import('./definitions.mjs').allAssets>} assets
 * @returns {string[]} errors, empty when valid
 */
export function validateAssets(assets) {
  const r = new Report();
  const contextVars = new Set(assets.contextVariables.map((v) => v.name ?? ''));
  const secretNames = new Set(assets.secrets.map((s) => s.name ?? ''));
  const dataRequests = new Set(assets.dataRequests.map((d) => d.dataRequestId ?? ''));
  const flowIds = new Set(assets.flows.map((f) => f.flowId ?? ''));

  checkContextVariables(r, assets.contextVariables);
  checkSecrets(r, assets.secrets);
  checkDataRequests(r, assets.dataRequests, secretNames);
  for (const f of assets.flows) {
    checkFlow(r, f, { contextVars, dataRequests, flowIds });
    if (f.flowId === NAMES.exitFlow) checkExitFlow(r, f);
  }
  if (!flowIds.has(NAMES.exitFlow)) r.add('flows', `exit sub-flow ${NAMES.exitFlow} missing`);
  checkApplication(r, assets.application, flowIds, assets.flows);
  return r.errors;
}
