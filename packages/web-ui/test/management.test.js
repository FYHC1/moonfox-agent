import test from 'node:test';
import assert from 'node:assert/strict';
import protocol from '../src/generated/protocol.json' with { type: 'json' };
import fixtures from '../src/generated/fixtures.json' with { type: 'json' };
import { ApiClient, ApiError } from '../src/client.js';
import { isValid, ProtocolError, generationParametersSchema, providerSettingsSchema, modelSettingsSchema } from '../src/protocol.js';
import { emptyGeneration, nullableNumber, stopParameters, generationError, generationForProtocol } from '../src/generation.js';
import { providerFormError, modelFormError, managementError } from '../src/provider-manager.js';
import { createDraft } from '../src/state.js';

const providerSettings = { name: '测试提供商', protocol: 'openai', base_url: 'https://provider.invalid/v1', enabled: true, timeout_seconds: 60, auth_mode: 'stored', env_var: null };
const modelSettings = { provider_id: 'primary', name: '测试模型', remote_id: 'remote-test', enabled: true, context_window: 128000, max_output_tokens: 4096, supports_tools: false, supports_images: false, supports_reasoning: false, token_field: 'max_tokens', parameters: emptyGeneration() };
const providerView = { id: 'primary', revision: 2, settings: providerSettings, key_configured: true, credential_source: 'stored' };
const modelView = { id: 'managed', revision: 3, settings: modelSettings, available: true, unavailable_reason: null };
const command = { request_id: 'test-request', expected_revision: 2 };
const json = (data, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
const create = (fetchImpl) => new ApiClient({ origin: 'http://127.0.0.1:8777', token: 'test-only-admin', fetchImpl });
const configView = { revision: 10, settings: { defaults: fixtures.snapshot.settings }, models: [{ id: 'managed', label: '测试模型', provider: 'primary' }], tools: [], extensions: [] };

test('management and generation validators reuse generated DTO $defs, key remains writeOnly', () => {
  assert.equal(providerSettingsSchema, protocol.schemas.PutProvider.$defs.ProviderSettings);
  assert.equal(modelSettingsSchema, protocol.schemas.PutModel.$defs.ModelSettings);
  assert.equal(generationParametersSchema, protocol.schemas.PutModel.$defs.GenerationParameters);
  assert.equal(protocol.schemas.PutProvider.properties.api_key.anyOf.find(item => item.type === 'string').writeOnly, true);
  for (const [name, value] of [['ProviderSettings', providerSettings], ['ModelSettings', modelSettings], ['GenerationParameters', emptyGeneration()]]) {
    assert.equal(isValid(name, value), true);
    assert.equal(isValid(name, { ...value, invented_field: true }), false);
  }
  assert.equal(isValid('ModelSettings', { ...modelSettings, context_window: 255 }), false);
  assert.equal(isValid('ProviderSettings', { ...providerSettings, protocol: 'responses' }), false);
  assert.equal(isValid('ProviderView', { ...providerView, api_key: 'must-not-be-returned' }), false);
});

test('provider key keep/replace/clear have exact write-only DTOs and never auto-test', async () => {
  const calls = [];
  const api = create(async (url, init) => { calls.push({ url, method: init.method, body: JSON.parse(init.body) }); return json(providerView); });
  for (const action of ['keep', 'replace', 'clear']) {
    const body = { ...command, settings: providerSettings, secret_action: action, ...(action === 'replace' ? { api_key: 'test-only-provider-key' } : {}) };
    const result = await api.putProvider('primary', body);
    assert.deepEqual(calls.at(-1).body, body);
    assert.equal(calls.at(-1).method, 'PUT');
    assert.equal(calls.at(-1).url, 'http://127.0.0.1:8777/api/v1/providers/primary');
    assert.equal(Object.hasOwn(result, 'api_key'), false);
  }
  assert.equal(calls.length, 3);
  assert.equal(calls.some(call => call.url.endsWith('/test')), false);
  assert.equal(JSON.stringify(api).includes('test-only-provider-key'), false);
});

test('client rejects malformed secret actions and schema-invalid model commands before transport', async () => {
  let calls = 0;
  const api = create(async () => { calls++; return json(providerView); });
  for (const extra of [
    { secret_action: 'replace' }, { secret_action: 'replace', api_key: ' ' },
    { secret_action: 'replace', api_key: 'bad\nkey' }, { secret_action: 'replace', api_key: 'x'.repeat(4097) },
    { secret_action: 'keep', api_key: 'test-key' }, { secret_action: 'clear', api_key: 'test-key' },
    { secret_action: 'replace', api_key: 'test-key', settings: { ...providerSettings, auth_mode: 'none' } },
  ]) await assert.rejects(api.putProvider('primary', { ...command, settings: providerSettings, ...extra }), ProtocolError);
  await assert.rejects(api.putModel('managed', { ...command, settings: { ...modelSettings, context_window: '128000' } }), ProtocolError);
  await assert.rejects(api.testProvider('primary', { ...command, expected_revision: -1 }), ProtocolError);
  assert.equal(calls, 0);
});

test('provider failures and returned data never expose the submitted key or upstream details', async () => {
  const secret = 'test-only-provider-key';
  const body = { ...command, settings: providerSettings, secret_action: 'replace', api_key: secret };
  for (const status of [401, 409, 422, 503]) {
    const api = create(async () => json({ detail: `untrusted ${secret}`, nested: { api_key: secret } }, status));
    await assert.rejects(api.putProvider('primary', body), error => {
      assert.equal(error instanceof ApiError, true);
      assert.equal(`${error.message}${JSON.stringify(error)}`.includes(secret), false);
      assert.equal(error.message.includes('untrusted'), false);
      return true;
    });
  }
  const echoed = create(async () => json({ ...providerView, settings: { ...providerSettings, name: `upstream ${secret}` } }));
  assert.equal(JSON.stringify(await echoed.putProvider('primary', body)).includes(secret), false);
  assert.equal(JSON.stringify(echoed).includes(secret), false);
  const rawField = create(async () => json({ ...providerView, api_key: secret }));
  await assert.rejects(rawField.putProvider('primary', body), error => error instanceof ProtocolError && !JSON.stringify(error).includes(secret));
  for (const status of [409, 422, 500]) assert.equal(managementError({ status, message: secret }).includes(secret), false);
});

test('management routes use generated catalog contracts and exact revision POSTs', async () => {
  const calls = [];
  const providers = { providers: [providerView], secret_storage: 'dpapi' };
  const models = { models: [modelView], builtin_models: [{ id: 'demo', label: 'Demo', provider: 'demo' }], default_model_id: 'demo', config_revision: 9 };
  const testResult = { ok: true, status: 'ok', detail: 'safe', latency_ms: 12, models: [{ id: 'remote-test', name: '测试模型' }] };
  const api = create(async (url, init) => {
    calls.push({ url, method: init.method, body: init.body && JSON.parse(init.body) });
    if (url.endsWith('/providers')) return json(providers);
    if (url.endsWith('/models')) return json(models);
    if (url.endsWith('/test')) return json(testResult);
    if (url.endsWith('/delete')) return json({ deleted: true });
    if (url.endsWith('/default')) return json(configView);
    return json(modelView);
  });
  assert.deepEqual(await api.providers(), providers); assert.deepEqual(await api.models(), models);
  const put = { ...command, expected_revision: 0, settings: modelSettings };
  await api.putModel('managed', put); assert.deepEqual(calls.at(-1).body, put); assert.equal(calls.at(-1).method, 'PUT');
  assert.deepEqual(await api.testProvider('primary', command), testResult);
  assert.deepEqual(calls.at(-1), { url: 'http://127.0.0.1:8777/api/v1/providers/primary/test', method: 'POST', body: command });
  await api.deleteProvider('primary', command); assert.deepEqual(calls.at(-1).body, command); assert.equal(calls.at(-1).method, 'POST');
  await api.deleteModel('managed', { ...command, expected_revision: 3 }); assert.equal(calls.at(-1).url.endsWith('/models/managed/delete'), true);
  await api.setDefaultModel('managed', { ...command, expected_revision: models.config_revision });
  assert.deepEqual(calls.at(-1), { url: 'http://127.0.0.1:8777/api/v1/models/managed/default', method: 'POST', body: { ...command, expected_revision: 9 } });
  await assert.rejects(create(async () => json({ providers: [], secret_storage: 'plaintext' })).providers(), ProtocolError);
  await assert.rejects(create(async () => json({ ...models, config_revision: '9' })).models(), ProtocolError);
});

test('generation empty values inherit, zero remains explicit, stop [] is distinct from null', () => {
  assert.equal(Object.values(emptyGeneration()).every(value => value === null), true);
  assert.equal(nullableNumber(''), null); assert.equal(nullableNumber('  '), null); assert.equal(nullableNumber('0'), 0); assert.equal(nullableNumber('0.8'), 0.8);
  assert.equal(stopParameters(''), null); assert.deepEqual(stopParameters('', true), []);
  assert.deepEqual(stopParameters('END\r\nDONE'), ['END', 'DONE']);
  assert.equal(generationError({ ...emptyGeneration(), stop: [] }), '');
  assert.notEqual(generationError({ ...emptyGeneration(), stop: ['a', 'b', 'c', 'd', 'e'] }), '');
  assert.notEqual(generationError({ ...emptyGeneration(), stop: [''] }), '');
  assert.notEqual(generationError({ ...emptyGeneration(), stop: ['x'.repeat(201)] }), '');
  assert.equal(generationError({ ...emptyGeneration(), stop: ['🌙'.repeat(200)] }), '');
  for (const [key, value] of [['top_p', 0], ['seed', 1.5], ['temperature', 3], ['temperature', NaN], ['temperature', Infinity], ['max_tokens', 0], ['frequency_penalty', -3]]) assert.notEqual(generationError({ ...emptyGeneration(), [key]: value }), '');
});

test('model and protocol validation is explicit: Anthropic constraints and independent output limits', () => {
  const values = { ...emptyGeneration(), temperature: 0.8, top_p: 0.9, seed: 2, frequency_penalty: 0.1, presence_penalty: 0.2 };
  const changed = generationForProtocol(values, 'anthropic');
  assert.equal(changed.seed, null); assert.equal(changed.frequency_penalty, null); assert.equal(changed.presence_penalty, null);
  assert.equal(values.seed, 2); assert.equal(changed.temperature, 0.8); // Do not guess how to resolve sampling choices.
  assert.match(generationError(changed, 'anthropic'), /二选一|之一/);
  assert.notEqual(generationError({ ...emptyGeneration(), temperature: 1.1 }, 'anthropic'), '');
  assert.notEqual(generationError({ ...emptyGeneration(), seed: 0 }, 'anthropic'), '');
  assert.equal(generationError({ ...emptyGeneration(), temperature: 1 }, 'anthropic'), '');
  assert.equal(modelFormError(modelSettings, 'anthropic'), '');
  assert.notEqual(modelFormError({ ...modelSettings, supports_reasoning: true }, 'anthropic'), '');
  assert.notEqual(modelFormError({ ...modelSettings, token_field: 'max_completion_tokens' }, 'anthropic'), '');
  assert.notEqual(modelFormError({ ...modelSettings, max_output_tokens: 128000 }, 'openai'), '');
  assert.notEqual(modelFormError({ ...modelSettings, parameters: { ...emptyGeneration(), max_tokens: 4097 } }, 'openai'), '');
});

test('provider form rejects public HTTP, stored-key retargeting and unsupported storage without exposing inputs', () => {
  assert.equal(providerFormError(providerSettings, { storage: 'dpapi' }), '');
  for (const base_url of ['http://provider.invalid/v1', 'https://user:secret@provider.invalid/v1', 'https://provider.invalid/v1?key=secret', 'https://provider.invalid/v1/messages']) assert.notEqual(providerFormError({ ...providerSettings, base_url }), '');
  assert.equal(providerFormError({ ...providerSettings, base_url: 'http://127.0.0.1:8779/v1', auth_mode: 'none' }), '');
  assert.notEqual(providerFormError(providerSettings, { storage: 'env_only' }), '');
  assert.equal(providerFormError({ ...providerSettings, auth_mode: 'env', env_var: 'MY_TEST_KEY' }, { storage: 'env_only' }), '');
  const retargeted = { ...providerSettings, protocol: 'anthropic' };
  assert.notEqual(providerFormError(retargeted, { original: providerView }), '');
  assert.equal(providerFormError(retargeted, { original: providerView, replacing: true }), '');
  assert.equal(providerFormError(retargeted, { original: providerView, clearing: true }), '');
});

test('session full PUT preserves generation overrides and retains independent original draft', async () => {
  const draft = createDraft(fixtures.snapshot.settings, 7);
  draft.settings.generation = { ...emptyGeneration(), temperature: 0, max_tokens: 300, stop: [] };
  let outgoing;
  const api = create(async (_url, init) => { outgoing = JSON.parse(init.body); return json({ revision: 8, settings: outgoing.settings }); });
  await api.configure('session', { request_id: 'r', expected_revision: draft.revision, settings: draft.settings });
  assert.deepEqual(outgoing.settings.generation, draft.settings.generation);
  assert.equal(outgoing.settings.max_tokens_per_input, fixtures.snapshot.settings.max_tokens_per_input);
  assert.equal(fixtures.snapshot.settings.generation.temperature, null);
});
