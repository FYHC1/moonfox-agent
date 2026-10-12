import test from 'node:test';
import assert from 'node:assert/strict';
import fixtures from '../src/generated/fixtures.json' with { type: 'json' };
import protocol from '../src/generated/protocol.json' with { type: 'json' };
import { assertContract, assertEvent, isValid, ProtocolError } from '../src/protocol.js';

const clone = structuredClone;

test('every exported DTO schema compiles independently under AJV2020', () => {
  for (const name of Object.keys(protocol.schemas)) assert.doesNotThrow(() => isValid(name, {}), name);
});

test('server-exported snapshot and all wire fixtures satisfy their exact schemas', () => {
  assert.equal(assertContract('Snapshot', fixtures.snapshot), fixtures.snapshot);
  for (const event of fixtures.events) assert.equal(assertEvent(event), event);
});

test('protocol version, invalid integer and forbidden properties are rejected', () => {
  const wrongVersion = clone(fixtures.snapshot); wrongVersion.protocol_version = 2;
  assert.throws(() => assertContract('Snapshot', wrongVersion), ProtocolError);
  const wrongSeq = clone(fixtures.snapshot); wrongSeq.seq = '0';
  assert.equal(isValid('Snapshot', wrongSeq), false);
  const extra = clone(fixtures.snapshot); extra.provider_secret = 'never allowed';
  assert.equal(isValid('Snapshot', extra), false);
  const input = { request_id: 'r', idempotency_key: 'k', text: '' };
  assert.equal(isValid('SubmitInput', input), false);
  input.text = '你好'; assert.equal(isValid('SubmitInput', input), true);
  assert.equal(isValid('CreateSession', { request_id: 'r', domain: 'qq' }), false);
});

test('exported $defs validate full settings without copying the backend schema', () => {
  assert.equal(isValid('SessionSettings', fixtures.snapshot.settings), true);
  const settings = { ...fixtures.snapshot.settings, max_turns_per_run: 0 };
  assert.equal(isValid('SessionSettings', settings), false);
  assert.equal(isValid('UpdateConfig', { request_id: 'r', expected_revision: -1, settings: fixtures.snapshot.settings }), false);
  assert.equal(isValid('Subscribe', { type: 'subscribe', token: '', epoch: 'e', after_seq: 0 }), false);
});

test('required event payload identities are checked using exported Message/Input schemas', () => {
  const start = clone(fixtures.events.find((event) => event.type === 'message_start'));
  delete start.payload.message.id;
  assert.throws(() => assertEvent(start), ProtocolError);
  const delta = clone(fixtures.events.find((event) => event.type === 'text_delta'));
  delta.payload.delta = { partial: 'not public text' };
  assert.throws(() => assertEvent(delta), ProtocolError);
});

test('protocol diagnostics contain only paths, not input data or credentials', () => {
  try { assertContract('Subscribe', { token: 'secret-value' }); assert.fail('should reject'); }
  catch (error) { assert.equal(JSON.stringify(error).includes('secret-value'), false); }
});
