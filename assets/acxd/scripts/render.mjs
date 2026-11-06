#!/usr/bin/env node
// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Render the asset definitions to placeholder JSON under exported/ (no real ids, URLs or secrets).
// Usage: node scripts/render.mjs [--check]
//   --check  fail if exported/ differs from the definitions (CI drift check), write nothing.

import { mkdirSync, readFileSync, writeFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { allAssets, PLACEHOLDER_CONFIG } from '../src/definitions.mjs';
import { validateAssets } from '../src/validate.mjs';
import { checkConnectSnippet, loadSnippetTemplate } from '../src/connect.mjs';

const OUT = fileURLToPath(new URL('../exported/', import.meta.url));
const check = process.argv.includes('--check');

const assets = allAssets(PLACEHOLDER_CONFIG);
const errors = [...validateAssets(assets), ...checkConnectSnippet(loadSnippetTemplate()).map((e) => `connect snippet ${e}`)];
if (errors.length) {
  console.error(errors.join('\n'));
  process.exit(1);
}

/** @type {Record<string, unknown>} */
const files = {
  'context-variables.json': assets.contextVariables,
  'secrets.json': assets.secrets,
  ...Object.fromEntries(assets.dataRequests.map((d) => [`data-request-${d.dataRequestId}.json`, d])),
  ...Object.fromEntries(assets.flows.map((f) => [`flow-${f.flowId}.json`, f])),
  [`application-${assets.application.name}.json`]: assets.application,
};

let drift = 0;
if (!check) mkdirSync(OUT, { recursive: true });
for (const [name, body] of Object.entries(files)) {
  const path = join(OUT, name);
  const text = `${JSON.stringify(body, null, 2)}\n`;
  if (check) {
    const current = existsSync(path) ? readFileSync(path, 'utf8') : '';
    if (current !== text) {
      console.error(`drift: exported/${name} differs from src/definitions.mjs (run npm run render)`);
      drift++;
    }
  } else {
    writeFileSync(path, text);
    console.log(`wrote exported/${name}`);
  }
}
if (drift) process.exit(1);
if (check) console.log(`exported/ matches definitions (${Object.keys(files).length} files), validation passed`);
