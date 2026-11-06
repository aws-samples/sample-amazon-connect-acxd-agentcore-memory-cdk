// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { checkConnectSnippet, loadSnippetTemplate, renderConnectSnippet } from '../src/connect.mjs';

const VALUES = {
  workspaceId: '11111111-2222-4333-8444-555555555555',
  applicationId: '66666666-7777-4888-9999-000000000000',
  alias: 'dev-alias_1',
};

test('template passes the snippet rules', () => assert.deepEqual(checkConnectSnippet(loadSnippetTemplate()), []));

test('profileId reaches ACXD only as nlx_userId from Customer Profiles', () => {
  const t = loadSnippetTemplate();
  const blocks = t.Actions.filter((/** @type {any} */ a) => a.Type === 'ConnectParticipantWithAgenticCX');
  assert.equal(blocks.length, 2);
  const withMemory = blocks.find((/** @type {any} */ b) => b.Parameters.AgentConfiguration.ContextVariables);
  assert.deepEqual(withMemory.Parameters.AgentConfiguration.ContextVariables, { nlx_userId: '$.Customer.ProfileId' });
  const lookup = t.Actions.find((/** @type {any} */ a) => a.Type === 'GetCustomerProfile');
  assert.deepEqual(lookup.Parameters.ProfileRequestData, { IdentifierName: '_phone', IdentifierValue: '$.CustomerEndpoint.Address' });
  // Every lookup error falls through to the block that passes no profile: no profile, no memory.
  assert.ok(lookup.Transitions.Errors.every((/** @type {any} */ e) => e.NextAction === 'acxdmem-agentic-cx-no-memory'));
});

test('render fills the placeholders and nothing else', () => {
  const out = JSON.stringify(renderConnectSnippet(loadSnippetTemplate(), VALUES));
  assert.ok(!out.includes('__ACXD_'));
  assert.ok(out.includes(VALUES.workspaceId) && out.includes(VALUES.applicationId) && out.includes(VALUES.alias));
});

test('render rejects malformed values', () => {
  assert.throws(() => renderConnectSnippet(loadSnippetTemplate(), { ...VALUES, workspaceId: 'x' }), /UUID/);
  assert.throws(() => renderConnectSnippet(loadSnippetTemplate(), { ...VALUES, alias: 'a"b' }), /alias/);
});

test('profileId from a contact attribute is rejected (T9)', () => {
  const t = loadSnippetTemplate();
  t.Actions[1].Parameters.AgentConfiguration.ContextVariables.nlx_userId = '$.Attributes.profileId';
  const errors = checkConnectSnippet(t);
  assert.ok(errors.some((e) => /nlx_userId must be/.test(e)));
  assert.ok(errors.some((e) => /contact attribute/.test(e)));
});

test('copying memory text back into the contact is rejected (T10)', () => {
  const t = loadSnippetTemplate();
  t.Actions.push({
    Identifier: 'copy',
    Type: 'UpdateContactAttributes',
    Parameters: { Attributes: { m: '$.AgenticCX.ContextVariables.acxdmemMemoryContext' }, TargetContact: 'Current' },
    Transitions: { NextAction: 'acxdmem-default' },
  });
  assert.ok(checkConnectSnippet(t).some((e) => /T10/.test(e)));
});

test('dangling transition is rejected', () => {
  const t = loadSnippetTemplate();
  t.Actions[0].Transitions.NextAction = 'nowhere';
  assert.ok(checkConnectSnippet(t).some((e) => /missing action nowhere/.test(e)));
});
