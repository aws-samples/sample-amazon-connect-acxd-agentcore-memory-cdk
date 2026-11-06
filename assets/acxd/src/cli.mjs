// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Shared CLI option handling for the provisioning, export and teardown scripts.

import { parseArgs } from 'node:util';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/**
 * @param {string[]} argv
 * @param {Record<string, { type: 'string' | 'boolean', default?: string | boolean }>} extra
 */
export function parseCommon(argv, extra = {}) {
  const { values } = parseArgs({
    args: argv,
    strict: true,
    options: {
      'workspace-id': { type: 'string' },
      region: { type: 'string', default: 'eu-central-1' },
      'api-key-secret-id': { type: 'string' },
      'api-key-json-key': { type: 'string' },
      apply: { type: 'boolean', default: false },
      help: { type: 'boolean', default: false },
      ...extra,
    },
  });
  return /** @type {Record<string, any>} */ (values);
}

/** @param {Record<string, any>} v @param {string[]} names */
export function requireOptions(v, names) {
  const missing = names.filter((n) => !v[n]);
  if (missing.length) throw new Error(`missing required option(s): ${missing.map((m) => `--${m}`).join(', ')}`);
  if (v['workspace-id'] && !UUID.test(v['workspace-id'])) throw new Error('--workspace-id must be a UUID');
  if (v.region && !/^[a-z]{2}(-[a-z]+)+-\d$/.test(v.region)) throw new Error('--region is not an AWS Region name');
}
