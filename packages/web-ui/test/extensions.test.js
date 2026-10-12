import test from 'node:test';
import assert from 'node:assert/strict';
import protocol from '../src/generated/protocol.json' with { type: 'json' };
import { ApiClient, ApiError } from '../src/client.js';
import { ProtocolError, isValid, validateJsonSchema } from '../src/protocol.js';
import { setExtensionConfig } from '../src/extension-config.js';
import { parseMemoryResult } from '../src/memory.js';
import { createDraft } from '../src/state.js';

const json = (value, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } });
const api = (fetchImpl) => new ApiClient({ origin: 'http://127.0.0.1:8777', token: 'fixture-token-only', fetchImpl });
const commandBody = (argumentsValue = { query: '', limit: 50, include_forgotten: false }) => ({
  request_id: 'fixture-request', expected_revision: 4, arguments_json: JSON.stringify(argumentsValue),
});
const record = (extra = {}) => ({
  id: 'memory-1', revision: 1, text: 'test record', kind: 'fact', subject: 'conversation',
  source: { origin: 'manual', entry_id: null }, confidence: 1, active: true, created_at: 1700000000000, updated_at: 1700000000000,
  ...extra,
});

test('extension config serialization replaces only its ID and preserves unrelated/unknown entries and enable state', () => {
  const settings = {
    enabled_extensions: ['fixture-other'],
    extension_config: [
      { extension_id: 'memory', config_json: '{"recall":true}', marker: 'keep-target-metadata' },
      { extension_id: 'unregistered-extension', config_json: '{"opaque":7}', future_field: true },
    ],
  };
  const next = setExtensionConfig(settings, 'memory', { version: 1, recall: false, extraction: 'off' });
  assert.deepEqual(JSON.parse(next[0].config_json), { version: 1, recall: false, extraction: 'off' });
  assert.equal(next[0].marker, 'keep-target-metadata');
  assert.deepEqual(next[1], settings.extension_config[1]);
  assert.deepEqual(settings.enabled_extensions, ['fixture-other']);
  assert.equal(settings.extension_config[0].config_json, '{"recall":true}');
  assert.deepEqual(setExtensionConfig({}, 'persona', { name: '月狐' }), [{ extension_id: 'persona', config_json: '{"name":"月狐"}' }]);
  const legacy = { model_id: 'demo', enabled_extensions: [] };
  assert.deepEqual(createDraft(legacy, 0).settings.extension_config, []);
  assert.equal(Object.hasOwn(legacy, 'extension_config'), false);
});

test('extension metadata JSON Schema is validated at runtime without dropping defaults', () => {
  const schema = { type: 'object', additionalProperties: false, properties: {
    version: { const: 1, default: 1 }, extraction: { enum: ['off', 'model'], default: 'off' }, max_results: { type: 'integer', minimum: 1, maximum: 20, default: 6 },
  } };
  assert.equal(validateJsonSchema(schema, { version: 1, extraction: 'off', max_results: 6 }), true);
  assert.equal(validateJsonSchema(schema, { version: 1, extraction: 'model', max_results: 20 }), true);
  assert.equal(validateJsonSchema(schema, { version: 1, extraction: 'unknown', max_results: 6 }), false);
  assert.equal(validateJsonSchema(schema, { version: 1, extraction: 'off', max_results: 21 }), false);
  assert.equal(validateJsonSchema(schema, { version: 1, extraction: 'off', max_results: 6, invented: true }), false);
  assert.equal(isValid('ExtensionCommand', commandBody()), true);
  assert.equal(isValid('ExtensionCommand', { ...commandBody(), scope: 'agent' }), false);
  assert.equal(protocol.schemas.UpdateConfig.$defs.SessionSettings.properties.extension_config.type, 'array');
});

test('extensionCommand posts the exact revision DTO, parses a safe object result and percent-encodes path segments', async () => {
  let call;
  const body = commandBody();
  const client = api(async (url, init) => {
    call = { url, method: init.method, body: JSON.parse(init.body), signal: init.signal };
    return json({ request_id: body.request_id, result_json: JSON.stringify({ records: [], revision: 8 }) });
  });
  const result = await client.extensionCommand('session /一', 'memory', 'list', body);
  assert.equal(call.url, `http://127.0.0.1:8777/api/v1/sessions/${encodeURIComponent('session /一')}/extensions/memory/commands/list`);
  assert.equal(call.method, 'POST');
  assert.deepEqual(call.body, body);
  assert.deepEqual(result.result, { records: [], revision: 8 });
  assert.deepEqual(Object.keys(result).sort(), ['request_id', 'result', 'result_json']);
});

test('extensionCommand rejects malformed, mismatched and unsafe JSON responses as protocol failures', async () => {
  const body = commandBody();
  for (const response of [
    { request_id: 'wrong', result_json: '{"records":[],"revision":0}' },
    { request_id: body.request_id, result_json: '{not-json' },
    { request_id: body.request_id, result_json: '[]' },
    { request_id: body.request_id, result_json: '{"__proto__":{"polluted":true}}' },
    { request_id: body.request_id },
  ]) {
    await assert.rejects(api(async () => json(response)).extensionCommand('s', 'memory', 'list', body), ProtocolError);
  }
  assert.equal({}.polluted, undefined);
});

test('extension writes are never automatically retried after network or conflict errors', async () => {
  const body = commandBody({ text: 'manual', kind: 'fact', subject: 'conversation' });
  let calls = 0;
  const offline = api(async () => { calls++; throw new TypeError('offline fixture'); });
  await assert.rejects(offline.extensionCommand('s', 'memory', 'remember', body), (error) => error.kind === 'network');
  assert.equal(calls, 1);
  calls = 0;
  const conflict = api(async () => { calls++; return json({ error: 'conflict', detail: 'stale' }, 409); });
  await assert.rejects(conflict.extensionCommand('s', 'memory', 'update', body), (error) => error instanceof ApiError && error.kind === 'conflict');
  assert.equal(calls, 1);
});

test('memory command result shapes are validated before rendering untrusted strings as text', () => {
  assert.deepEqual(parseMemoryResult('list', { records: [record()], revision: 3 }), { records: [record()], revision: 3 });
  assert.equal(parseMemoryResult('remember', { record: record() }).record.id, 'memory-1');
  assert.deepEqual(parseMemoryResult('delete', { deleted: true, notice: '<b>notice</b>' }), { deleted: true, notice: '<b>notice</b>' });
  assert.deepEqual(parseMemoryResult('jobs', { jobs: [{ id: 'job-1', status: 'unknown', ticket: 'not-rendered' }] }), { jobs: [{ id: 'job-1', status: 'unknown' }] });
  for (const invalid of [
    ['list', { records: [], revision: '3' }],
    ['list', { records: [{ ...record(), confidence: 1.1 }], revision: 3 }],
    ['update', { record: { ...record(), kind: 'invented' } }],
    ['forget', { record: { ...record(), source: { origin: 'extraction', entry_id: {} } } }],
    ['delete', { deleted: false, notice: 'no' }],
    ['jobs', { jobs: [{ id: 'job-1', status: 'retrying' }] }],
  ]) assert.throws(() => parseMemoryResult(...invalid), ProtocolError);
});
