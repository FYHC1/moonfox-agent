import test from 'node:test';
import assert from 'node:assert/strict';
import protocol from '../src/generated/protocol.json' with { type: 'json' };
import fixtures from '../src/generated/fixtures.json' with { type: 'json' };
import { ApiClient, ApiError } from '../src/client.js';
import { isValid, ProtocolError, generationParametersSchema, providerSettingsSchema, modelSettingsSchema, managedModelIdSchema } from '../src/protocol.js';
import { emptyGeneration, generationEditor, nullableNumber, stopParameters, generationError, generationForProtocol } from '../src/generation.js';
import { providerFormError, modelFormError, managementError } from '../src/provider-manager.js';
import { autoModelId, isModelId, modelIdBase, modelIdError, modelIdMaxLength } from '../src/model-id.js';
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

// Minimal DOM stub so generationEditor's control state can be asserted without a browser.
class FakeNode {}
class FakeElement extends FakeNode {
  constructor(tag) {
    super();
    this.tagName = tag; this.children = []; this.attributes = {}; this.dataset = {}; this.listeners = {};
    this.className = ''; this.value = ''; this.checked = false; this.disabled = false;
  }
  get id() { return this.attributes.id; }
  set id(value) { this.attributes.id = String(value); }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  getAttribute(key) { return this.attributes[key] ?? null; }
  addEventListener(type, listener) { (this.listeners[type] ??= []).push(listener); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  setCustomValidity(message) { this.validationMessage = message; }
}
globalThis.Node = FakeNode;
globalThis.document = {
  createElement: (tag) => new FakeElement(tag),
  createTextNode: (value) => Object.assign(new FakeNode(), { textContent: value }),
  createElementNS: () => new FakeElement('svg'),
  getElementById: () => null,
};
const descendants = (node) => [node, ...(node.children ?? []).flatMap(descendants)];
const byId = (root, id) => descendants(root).find((node) => node.attributes?.id === id);
const textOf = (root, className) => descendants(root).filter((node) => node.className === className).map((node) => node.textContent).join(' | ');
const fire = (node, type) => { for (const listener of node.listeners[type] ?? []) listener({ target: node }); };

test('OpenAI Responses keeps the exact enum spelling, forbids penalties/seed/stop and preserves Anthropic rules', () => {
  assert.equal(isValid('ProviderSettings', { ...providerSettings, protocol: 'openai-responses' }), true);
  for (const protocol of ['responses', 'openai_responses', 'OpenAI-Responses']) {
    assert.equal(isValid('ProviderSettings', { ...providerSettings, protocol }), false, protocol);
  }
  const responses = { ...providerSettings, protocol: 'openai-responses' };
  assert.equal(providerFormError(responses, { storage: 'dpapi' }), '');
  assert.equal(generationError(emptyGeneration(), 'openai-responses'), '');
  assert.equal(generationError({ ...emptyGeneration(), temperature: 0.7, top_p: 0.9, max_tokens: 512 }, 'openai-responses'), '');
  for (const key of ['frequency_penalty', 'presence_penalty', 'seed']) {
    assert.notEqual(generationError({ ...emptyGeneration(), [key]: 0 }, 'openai-responses'), '', key);
  }
  assert.notEqual(generationError({ ...emptyGeneration(), stop: [] }, 'openai-responses'), '');
  assert.notEqual(generationError({ ...emptyGeneration(), stop: ['END'] }, 'openai-responses'), '');
  // Reasoning stays available for Responses; the retained Chat-only token_field is inert.
  assert.equal(modelFormError(modelSettings, 'openai-responses'), '');
  assert.equal(modelFormError({ ...modelSettings, supports_reasoning: true }, 'openai-responses'), '');
  assert.equal(modelFormError({ ...modelSettings, token_field: 'max_completion_tokens' }, 'openai-responses'), '');
  assert.equal(modelFormError({ ...modelSettings, token_field: 'max_completion_tokens' }, 'openai'), '');
  assert.notEqual(modelFormError({ ...modelSettings, supports_reasoning: true }, 'anthropic'), '');
  assert.notEqual(generationError({ ...emptyGeneration(), seed: 0 }, 'anthropic'), '');
  assert.notEqual(generationError({ ...emptyGeneration(), temperature: 1.5 }, 'anthropic'), '');
});

test('generationForProtocol clears exactly the unsupported fields on an explicit change', () => {
  const values = { ...emptyGeneration(), temperature: 0.8, top_p: 0.9, seed: 2, frequency_penalty: 0.1, presence_penalty: 0.2, stop: ['END'] };
  const responses = generationForProtocol(values, 'openai-responses');
  assert.deepEqual(responses, { ...values, seed: null, frequency_penalty: null, presence_penalty: null, stop: null });
  assert.deepEqual(values.stop, ['END']); // The caller's object is never mutated.
  const anthropic = generationForProtocol(values, 'anthropic');
  assert.equal(anthropic.stop[0], 'END'); // Anthropic keeps stop; only penalties/seed are cleared.
  assert.equal(anthropic.seed, null);
  assert.deepEqual(generationForProtocol(values, 'openai'), values);
});

test('model IDs use the generated ManagedModelView contract, never the provider pattern', () => {
  assert.equal(managedModelIdSchema, protocol.schemas.ManagedModelView.properties.id);
  assert.notEqual(managedModelIdSchema.pattern, modelSettingsSchema.properties.provider_id.pattern);
  assert.equal(modelIdMaxLength, 200);
  for (const id of ['gpt-4.1', 'Gpt-4.1-Turbo', '中文模型', 'a.b-c_d:e', 'v2.5-turbo', 'a'.repeat(200)]) {
    assert.equal(isModelId(id), true, id);
    assert.equal(modelIdError(id), '', id);
  }
  for (const id of ['.hidden', 'a/b', 'a\\b', 'a?b', 'a#b', 'a%b', 'a b', 'a\tb', 'a\nb', 'a\u0000b', 'a\u007fb', 'a\n', 'a\r', 'a\u2028', '', 'a'.repeat(201), 42]) {
    assert.equal(isModelId(id), false, JSON.stringify(id));
    assert.notEqual(modelIdError(id), '');
  }
  // The provider rule is untouched: lowercase start, 64 chars, no dot.
  assert.equal(isValid('ModelSettings', { ...modelSettings, provider_id: 'primary-1_x' }), true);
  assert.equal(isValid('ModelSettings', { ...modelSettings, provider_id: 'Primary' }), false);
  assert.equal(isValid('ModelSettings', { ...modelSettings, provider_id: 'p.1' }), false);
  assert.equal(isValid('ModelSettings', { ...modelSettings, provider_id: 'a'.repeat(65) }), false);
});

test('automatic model ID preview mirrors the server rule with provider prefix and numeric fallback', () => {
  assert.equal(modelIdBase('  我的 模型 / v2  '), '我的-模型-v2');
  assert.equal(modelIdBase('...dots'), 'dots');
  assert.equal(modelIdBase(''), 'model');
  assert.equal(modelIdBase('a/b\\c?d#e%f'), 'a-b-c-d-e-f');
  assert.equal([...modelIdBase('x'.repeat(250))].length, 200);
  assert.equal(autoModelId({ name: 'gpt-4.1', providerId: 'openai' }), 'gpt-4.1');
  assert.equal(autoModelId({ name: 'gpt-4.1', providerId: 'openai', occupied: ['gpt-4.1'] }), 'openai-gpt-4.1');
  assert.equal(autoModelId({ name: 'gpt-4.1', providerId: 'openai', occupied: ['gpt-4.1', 'openai-gpt-4.1'] }), 'openai-gpt-4.1-2');
  assert.equal(autoModelId({ name: 'gpt-4.1', providerId: 'openai', occupied: ['gpt-4.1', 'openai-gpt-4.1', 'openai-gpt-4.1-2'] }), 'openai-gpt-4.1-3');
  // Builtin IDs also occupy the namespace.
  assert.equal(autoModelId({ name: 'demo', providerId: 'primary', occupied: ['demo'] }), 'primary-demo');
  assert.equal(autoModelId({ name: '模型 A', providerId: 'p' }), '模型-A');
  const longName = 'x'.repeat(250);
  const longBase = modelIdBase(longName);
  const stem = `p-${longBase}`.slice(0, 200);
  const capped = autoModelId({ name: longName, providerId: 'p', occupied: [longBase, stem] });
  assert.equal([...capped].length, 200);
  assert.match(capped, /-2$/);
  assert.equal(isModelId(capped), true);
});

test('provider form rejects /responses and other request endpoints but keeps retarget-key behavior', () => {
  const responses = { ...providerSettings, protocol: 'openai-responses', base_url: 'https://api.openai.com/v1' };
  assert.equal(providerFormError(responses, { storage: 'dpapi' }), '');
  for (const suffix of ['/chat/completions', '/responses', '/messages', '/models']) {
    assert.notEqual(providerFormError({ ...responses, base_url: `https://api.openai.com/v1${suffix}` }), '', suffix);
    assert.notEqual(providerFormError({ ...providerSettings, base_url: `https://provider.invalid/v1${suffix}` }), '', suffix);
  }
  assert.notEqual(providerFormError({ ...responses, base_url: 'http://api.openai.com/v1' }), '');
  assert.notEqual(providerFormError({ ...responses, base_url: 'https://api.openai.com/v1?key=secret' }), '');
  const retarget = { ...providerSettings, protocol: 'openai-responses' };
  assert.notEqual(providerFormError(retarget, { original: providerView }), '');
  assert.equal(providerFormError(retarget, { original: providerView, replacing: true }), '');
  assert.equal(providerFormError(retarget, { original: providerView, clearing: true }), '');
  assert.equal(providerFormError({ ...retarget, base_url: 'https://api.openai.com/v1' }, { original: { ...providerView, key_configured: false, credential_source: 'environment' } }), '');
});

test('createModel posts an automatic ID with expected_revision 0 and is never retried', async () => {
  const calls = [];
  const created = { ...modelView, id: 'openai-gpt-4.1-2' };
  const api = create(async (url, init) => { calls.push({ url, method: init.method, body: JSON.parse(init.body) }); return json(created); });
  const body = { ...command, expected_revision: 0, settings: modelSettings };
  assert.deepEqual(await api.createModel(body), created);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, 'http://127.0.0.1:8777/api/v1/models');
  assert.equal(calls[0].method, 'POST');
  assert.deepEqual(calls[0].body, body);
  for (const invalid of [
    { ...body, expected_revision: 1 }, { ...body, expected_revision: -1 }, { ...body, expected_revision: '0' },
    { ...body, settings: { ...modelSettings, context_window: '128000' } },
    { ...body, settings: { ...modelSettings, provider_id: 'Primary' } },
    { ...body, settings: { ...modelSettings, token_field: 'max_tokens_v2' } },
    { ...body, invented: true },
  ]) await assert.rejects(api.createModel(invalid), ProtocolError);
  assert.equal(calls.length, 1);
  let attempts = 0;
  const failing = create(async () => { attempts++; throw new TypeError('offline'); });
  await assert.rejects(failing.createModel(body), (error) => error.kind === 'network');
  assert.equal(attempts, 1);
});

test('model IDs containing - . or Unicode are percent-encoded on PUT, delete and default routes', async () => {
  const calls = [];
  const api = create(async (url, init) => {
    calls.push({ url, method: init.method });
    if (url.endsWith('/delete')) return json({ deleted: true });
    if (url.endsWith('/default')) return json(configView);
    return json(modelView);
  });
  await api.putModel('gpt-4.1', { ...command, expected_revision: 0, settings: modelSettings });
  assert.deepEqual(calls[0], { url: 'http://127.0.0.1:8777/api/v1/models/gpt-4.1', method: 'PUT' });
  await api.putModel('模型·v2.1', { ...command, expected_revision: 0, settings: modelSettings });
  assert.equal(calls[1].url, `http://127.0.0.1:8777/api/v1/models/${encodeURIComponent('模型·v2.1')}`);
  await api.deleteModel('gpt-4.1', { ...command, expected_revision: 3 });
  assert.equal(calls[2].url.endsWith('/models/gpt-4.1/delete'), true);
  await api.setDefaultModel('a.b-c', { ...command, expected_revision: 9 });
  assert.equal(calls[3].url.endsWith('/models/a.b-c/default'), true);
});

test('generation editor reports incompatible saved values on initial render and only clears on explicit changes', () => {
  const saved = { ...emptyGeneration(), frequency_penalty: 0.5, presence_penalty: 0.2, seed: 7, stop: ['END'] };
  const editor = generationEditor({ value: saved, prefix: 'unit-generation', protocol: 'openai-responses', modelDefaults: true, onChange: () => {} });
  const seed = byId(editor.element, 'unit-generation-seed');
  const stop = byId(editor.element, 'unit-generation-stop');
  const clear = byId(editor.element, 'unit-generation-clear-stop');
  // Initial render: nothing is silently changed, the incompatibility is only reported.
  assert.equal(editor.value().frequency_penalty, 0.5);
  assert.equal(editor.value().seed, 7);
  assert.deepEqual(editor.value().stop, ['END']);
  assert.equal(seed.disabled, true);
  assert.equal(seed.value, 7);
  assert.equal(stop.disabled, true);
  assert.equal(stop.value, 'END');
  assert.equal(clear.disabled, true);
  assert.match(textOf(editor.element, 'field-hint'), /Responses/);
  assert.notEqual(textOf(editor.element, 'error-text'), '');
  assert.equal(byId(editor.element, 'unit-generation-temperature').max, 2);
  // Explicit protocol change clears exactly the forbidden fields and syncs the stop controls.
  editor.setProtocol('openai-responses');
  assert.equal(editor.value().frequency_penalty, null);
  assert.equal(editor.value().presence_penalty, null);
  assert.equal(editor.value().seed, null);
  assert.equal(editor.value().stop, null);
  assert.equal(seed.value, '');
  assert.equal(stop.value, '');
  assert.equal(clear.checked, false);
  assert.equal(textOf(editor.element, 'error-text'), '');
  // Switching back to Chat re-enables the controls without inventing values.
  editor.setProtocol('openai');
  assert.equal(seed.disabled, false);
  assert.equal(stop.disabled, false);
  assert.equal(clear.disabled, false);
  assert.equal(seed.value, '');
  stop.value = 'A\nB'; fire(stop, 'input');
  assert.deepEqual(editor.value().stop, ['A', 'B']);
  clear.checked = true; fire(clear, 'change');
  assert.deepEqual(editor.value().stop, []);
  assert.equal(stop.value, '');
  // An explicit change to Responses after Chat also clears stop, including the [] form.
  editor.setProtocol('openai-responses');
  assert.equal(editor.value().stop, null);
  assert.equal(clear.checked, false);
  assert.equal(stop.value, '');
  // Anthropic keeps its own constraints.
  const anthropic = generationEditor({ value: { ...emptyGeneration(), temperature: 1.2 }, prefix: 'unit-anthropic', protocol: 'anthropic', modelDefaults: true, onChange: () => {} });
  assert.equal(byId(anthropic.element, 'unit-anthropic-temperature').max, 1);
  assert.equal(byId(anthropic.element, 'unit-anthropic-seed').disabled, true);
  assert.equal(byId(anthropic.element, 'unit-anthropic-stop').disabled, false);
  assert.equal(anthropic.value().temperature, 1.2); // Reported, not repaired.
  assert.notEqual(textOf(anthropic.element, 'error-text'), '');
});
