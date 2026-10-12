import { test, expect } from '@playwright/test';
import fixtures from '../src/generated/fixtures.json' with { type: 'json' };
import { assertContract } from '../src/protocol.js';
import { autoModelId } from '../src/model-id.js';

// Deterministic route fixtures only. No real provider, credential, or network discovery is used.
const backend = process.env.MOONFOX_TEST_BACKEND;
const token = process.env.MOONFOX_TEST_TOKEN;
const fixture = process.env.MOONFOX_UI_FIXTURE === '1';
const defaults = () => structuredClone(fixtures.snapshot.settings.generation);
const providerSettings = (extra = {}) => ({ name: '主提供商', protocol: 'openai', base_url: 'https://provider.invalid/v1', enabled: true, timeout_seconds: 60, auth_mode: 'stored', env_var: null, ...extra });
const provider = (id = 'primary', extra = {}) => ({ id, revision: 1, settings: providerSettings(extra), key_configured: true, credential_source: extra.auth_mode === 'none' ? 'none' : 'stored' });
const modelSettings = (extra = {}) => ({ provider_id: 'primary', name: '托管模型', remote_id: 'remote-test', enabled: true, context_window: 128000, max_output_tokens: 4096, supports_tools: false, supports_images: false, supports_reasoning: false, token_field: 'max_tokens', parameters: defaults(), ...extra });
const manager = (page) => page.getByRole('dialog', { name: '提供商与模型', exact: true });
const action = (root, name) => root.getByRole('button', { name, exact: true });

async function mockManagement(page, { zeroSessions = true, admin = true, storage = 'dpapi', providers = [], models = [], testModels = [{ id: 'remote-safe', name: '<img src=x onerror=alert(1)>' }] } = {}) {
  const state = { providers: structuredClone(providers), models: structuredClone(models), storage, defaultId: 'demo', revision: 6, calls: [], failure: null, beforePut: null };
  const config = () => ({ revision: state.revision, settings: { defaults: { ...fixtures.snapshot.settings, model_id: state.defaultId } },
    models: [{ id: 'demo', label: 'Demo · 离线演示', provider: 'demo' }, ...state.models.filter(item => item.available).map(item => ({ id: item.id, label: item.settings.name, provider: item.settings.provider_id }))], tools: ['echo'], extensions: [] });
  const updateAvailability = () => {
    for (const model of state.models) {
      const item = state.providers.find(p => p.id === model.settings.provider_id);
      model.available = Boolean(model.settings.enabled && item?.settings.enabled && (item.settings.auth_mode === 'none' || item.key_configured));
      model.unavailable_reason = model.available ? null : !model.settings.enabled ? '模型已停用' : !item?.settings.enabled ? '提供商已停用' : '凭据不可用或模型参数不兼容';
    }
  };
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request(); const path = new URL(request.url()).pathname.slice('/api/v1'.length); const method = request.method();
    const reply = (value, schema, status = 200) => {
      if (schema) assertContract(schema, value);
      return route.fulfill({ status, json: value, headers: { 'Access-Control-Allow-Origin': '*', 'Cache-Control': 'no-store' } });
    };
    if (method === 'OPTIONS') return route.fulfill({ status: 204, headers: { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Headers': '*', 'Access-Control-Allow-Methods': '*' } });
    if (path === '/me') return reply({ id: 'management-test', is_admin: admin });
    if (path === '/sessions' && method === 'GET' && zeroSessions) return reply({ sessions: [] });
    if (path === '/config') return reply(config(), 'ConfigView');
    if (!/^\/(providers|models)(\/|$)/.test(path)) return route.continue();
    updateAvailability();
    if (method === 'GET' && path === '/providers') return reply({ providers: state.providers, secret_storage: storage }, 'ProviderCatalog');
    if (method === 'GET' && path === '/models') return reply({ models: state.models, builtin_models: [{ id: 'demo', label: 'Demo · 离线演示', provider: 'demo' }], default_model_id: state.defaultId, config_revision: state.revision }, 'ModelCatalog');
    const [, kind, encodedId, operation] = path.split('/');
    const automatic = path === '/models' && method === 'POST';
    const body = request.postDataJSON();
    state.calls.push({ path, method, body });
    assertContract(method === 'PUT' || automatic ? kind === 'providers' ? 'PutProvider' : 'PutModel' : operation === 'default' ? 'SetDefaultModel' : 'RevisionCommand', body);
    if ((method === 'PUT' || automatic) && state.beforePut) await state.beforePut();
    if (state.failure) { const failure = state.failure; state.failure = null; return reply({ error: 'test-only', detail: failure.detail }, null, failure.status); }
    const id = automatic ? autoModelId({ name: body.settings.name, providerId: body.settings.provider_id, occupied: ['demo', ...state.models.map(item => item.id)] }) : decodeURIComponent(encodedId ?? '');
    const list = state[kind]; const existing = list.find(item => item.id === id);
    if (body.expected_revision !== (operation === 'default' ? state.revision : existing?.revision ?? 0)) return reply({ error: 'conflict', detail: 'fixture revision' }, null, 409);
    if (method === 'PUT' || automatic) {
      const saved = { id, revision: (existing?.revision ?? 0) + 1, settings: structuredClone(body.settings) };
      if (kind === 'providers') Object.assign(saved, { key_configured: body.secret_action === 'replace' ? true : body.secret_action === 'clear' ? false : existing?.key_configured ?? false, credential_source: body.settings.auth_mode === 'env' ? 'environment' : body.settings.auth_mode === 'none' ? 'none' : 'stored' });
      else Object.assign(saved, { available: true, unavailable_reason: null });
      if (existing) list.splice(list.indexOf(existing), 1, saved); else list.push(saved);
      state.revision++; updateAvailability(); return reply(saved, kind === 'providers' ? 'ProviderView' : 'ManagedModelView');
    }
    if (operation === 'test') return reply({ ok: true, status: 'ok', detail: 'ignored untrusted detail', latency_ms: 17, models: testModels }, 'ProviderTestResult');
    if (operation === 'default') { state.defaultId = id; state.revision++; return reply(config(), 'ConfigView'); }
    if (operation === 'delete') {
      if (kind === 'providers' && state.models.some(model => model.settings.provider_id === id)) return reply({ error: 'conflict', detail: 'referenced' }, null, 409);
      list.splice(list.indexOf(existing), 1); state.revision++; return reply({ deleted: true }, 'Deleted');
    }
    return reply({ error: 'not-found', detail: 'test-only' }, null, 404);
  });
  return state;
}

async function login(page) {
  await page.goto('/');
  await page.getByLabel('后端地址', { exact: true }).fill(backend);
  await page.getByLabel('管理员令牌', { exact: true }).fill(token);
  await action(page, '连接工作台').click();
  await expect(page.locator('.workspace')).toBeVisible();
}
async function open(page) {
  if (page.viewportSize().width < 760) await action(page, '打开会话列表').click();
  await action(page, '提供商与模型').click();
  await expect(action(manager(page), '新建提供商')).toBeEnabled();
}
async function fillProvider(page, id = 'primary') {
  await action(manager(page), '新建提供商').click();
  await page.getByLabel('提供商 ID', { exact: true }).fill(id);
  await page.getByLabel('提供商名称', { exact: true }).fill('主提供商');
  await page.getByLabel('API 基础地址', { exact: true }).fill('https://provider.invalid/v1');
}

test.beforeEach(() => { test.skip(!backend || !token || !fixture, 'Uses the explicit loopback fixture and test-only route interception.'); });

test('zero-session administrator manages provider secrets, CAS, discovery, models and defaults without inference', async ({ page }) => {
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  const state = await mockManagement(page);
  await login(page); await expect(page.getByText('还没有会话', { exact: true })).toBeVisible(); await open(page);
  await fillProvider(page);
  const key = page.getByLabel('API 密钥（只写）', { exact: true });
  await expect(key).toHaveAttribute('type', 'password');
  const secret = 'ui-test-only-write-key';
  await key.fill(secret);
  let release;
  state.beforePut = () => new Promise(resolve => { release = resolve; });
  await action(manager(page), '保存提供商').click();
  await expect.poll(() => state.calls.length).toBe(1);
  await expect(key).toHaveValue('');
  await expect(action(manager(page), '保存提供商')).toBeDisabled();
  await expect(page.getByLabel('提供商名称', { exact: true })).toBeDisabled();
  expect(state.calls[0].body).toMatchObject({ expected_revision: 0, secret_action: 'replace', api_key: secret });
  state.beforePut = null; release();
  const card = manager(page).locator('[data-provider-id="primary"]');
  await expect(card).toContainText('本机密钥已配置');
  expect(state.calls.filter(call => call.path.endsWith('/test'))).toHaveLength(0);
  await action(card, '编辑').click();
  await expect(page.getByLabel('提供商 ID', { exact: true })).toHaveAttribute('readonly', '');
  await expect(key).toHaveValue('');
  await page.getByLabel('提供商名称', { exact: true }).fill('已编辑提供商');
  await action(manager(page), '保存提供商').click();
  await expect(card).toContainText('已编辑提供商');
  expect(state.calls.at(-1).body.secret_action).toBe('keep'); expect(state.calls.at(-1).body).not.toHaveProperty('api_key');
  expect(state.calls.at(-1).body.expected_revision).toBe(1);
  await action(card, '编辑').click();
  await page.getByLabel('API 基础地址', { exact: true }).fill('https://changed.invalid/v1');
  await action(manager(page), '保存提供商').click();
  await expect(manager(page).getByRole('alert')).toContainText('必须重新输入密钥');
  expect(state.calls).toHaveLength(2);
  await page.getByLabel('清除已保存的密钥（保存时生效）', { exact: true }).check();
  await action(manager(page), '保存提供商').click();
  await expect(card).toContainText('缺少可用凭据');
  expect(state.calls.at(-1).body.secret_action).toBe('clear'); expect(state.calls.at(-1).body).not.toHaveProperty('api_key');
  await action(card, '编辑').click(); await key.fill(secret);
  await action(manager(page), '保存提供商').click(); await expect(card).toContainText('本机密钥已配置');
  await action(card, '检测 / 获取模型').click();
  await expect(card).toContainText('17 ms'); await expect(card).toContainText('<img src=x onerror=alert(1)>');
  await expect(card.locator('img')).toHaveCount(0);
  expect(state.calls.at(-1)).toMatchObject({ method: 'POST', path: '/providers/primary/test', body: { expected_revision: 4 } });
  expect(Object.keys(state.calls.at(-1).body).sort()).toEqual(['expected_revision', 'request_id']);
  await action(card, '添加模型').click();
  await expect(page.getByLabel('所属提供商', { exact: true })).toHaveValue('primary');
  await expect(page.getByLabel('远端模型 ID', { exact: true })).toHaveValue('remote-safe');
  await expect(page.getByLabel('支持工具调用', { exact: true })).not.toBeChecked();
  await page.getByLabel('模型 ID', { exact: true }).fill('managed');
  await page.getByLabel('模型名称', { exact: true }).fill('主模型');
  await page.getByLabel('温度 · temperature', { exact: true }).fill('0.6');
  await page.getByLabel('单次输出上限 · max_tokens', { exact: true }).fill('600');
  await page.getByLabel('停止词 · stop', { exact: true }).fill('END\nDONE');
  await action(manager(page), '保存模型').click();
  const model = manager(page).locator('[data-model-id="managed"]');
  await expect(model).toContainText('主模型');
  expect(state.calls.at(-1).body.settings.parameters).toEqual({ ...defaults(), temperature: 0.6, max_tokens: 600, stop: ['END', 'DONE'] });
  await action(model, '编辑').click();
  await expect(page.getByLabel('模型 ID', { exact: true })).toHaveAttribute('readonly', '');
  await page.getByLabel('OpenAI 输出 token 字段', { exact: true }).selectOption('max_completion_tokens');
  await page.getByLabel('随机种子 · seed', { exact: true }).fill('42');
  await page.getByLabel('支持工具调用', { exact: true }).check();
  await action(manager(page), '保存模型').click(); await expect(model).toContainText('工具');
  expect(state.calls.at(-1).body).toMatchObject({ expected_revision: 1, settings: { token_field: 'max_completion_tokens', supports_tools: true, parameters: { seed: 42 } } });
  const configRevision = state.revision;
  await action(model, '设为默认').click();
  await expect(action(model, '默认模型')).toBeDisabled();
  expect(state.calls.at(-1).body.expected_revision).toBe(configRevision);
  await action(model, '停用').click(); await expect(model).toContainText('模型已停用');
  await action(model, '启用').click(); await expect(action(model, '停用')).toBeVisible();
  await manager(page).getByRole('tab', { name: '提供商', exact: true }).click();
  await action(card, '停用').click(); await expect(action(card, '启用')).toBeVisible();
  await manager(page).getByRole('tab', { name: '模型', exact: true }).click();
  await expect(model).toContainText('提供商已停用');
  const builtin = manager(page).locator('[data-builtin-model-id="demo"]');
  await expect(action(builtin, '编辑')).toHaveCount(0); await action(builtin, '设为默认').click();
  await expect(action(builtin, '默认模型')).toBeDisabled();
  await action(model, '删除').click();
  await expect(page.getByRole('dialog', { name: '删除此模型？', exact: true })).toContainText('r4');
  await action(page.getByRole('dialog', { name: '删除此模型？', exact: true }), '确认删除').click();
  await expect(model).toHaveCount(0);
  expect(state.calls.at(-1)).toMatchObject({ method: 'POST', path: '/models/managed/delete', body: { expected_revision: 4 } });
  await manager(page).getByRole('tab', { name: '提供商', exact: true }).click();
  await action(card, '删除').click(); await action(page.getByRole('dialog', { name: '删除此提供商？', exact: true }), '确认删除').click();
  await expect(card).toHaveCount(0); expect(state.calls.at(-1).body.expected_revision).toBe(5);
  await action(manager(page), '关闭提供商与模型').click();
  await expect(action(page, '提供商与模型')).toBeFocused();
  expect(await page.evaluate(() => ({ local: localStorage.length, session: sessionStorage.length }))).toEqual({ local: 0, session: 0 });
  expect(await page.locator('body').textContent()).not.toContain(secret);
  expect(state.calls.some(call => /inputs|control|completions|messages/.test(call.path))).toBeFalsy();
  expect(errors).toEqual([]);
});

test('failed key save clears immediately, generic 422 and 409 block overwrite until explicit reload', async ({ page }) => {
  const state = await mockManagement(page, { providers: [provider()] }); await login(page); await open(page);
  const card = manager(page).locator('[data-provider-id="primary"]');
  await action(card, '编辑').click();
  const secret = 'ui-test-only-rejected-key'; const key = page.getByLabel('API 密钥（只写）', { exact: true });
  await key.fill(secret); state.failure = { status: 422, detail: `Do not echo ${secret}` };
  await action(manager(page), '保存提供商').click();
  await expect(manager(page).locator('.management-feedback')).toContainText('提交被拒绝');
  await expect(key).toHaveValue(''); expect(await manager(page).textContent()).not.toContain(secret);
  await key.fill(secret); state.failure = { status: 409, detail: secret };
  await action(manager(page), '保存提供商').click();
  await expect(manager(page).locator('.management-feedback')).toContainText('修订冲突');
  await expect(key).toHaveValue(''); await expect(action(manager(page), '保存提供商')).toBeDisabled();
  expect(state.calls).toHaveLength(2);
  await action(manager(page), '重新加载目录核对').click();
  await action(page.getByRole('dialog', { name: '放弃未保存的管理编辑？', exact: true }), '放弃编辑').click();
  await expect(action(card, '编辑')).toBeEnabled();
  state.failure = { status: 409, detail: secret };
  await action(card, '检测 / 获取模型').click();
  await expect(manager(page).locator('.management-feedback')).toContainText('修订冲突');
  await expect(action(card, '检测 / 获取模型')).toBeDisabled();
  await action(manager(page), '重新加载目录核对').click(); await expect(action(card, '检测 / 获取模型')).toBeEnabled();
  await action(card, '编辑').click(); await expect(key).toHaveValue('');
  expect(state.calls).toHaveLength(3);
});

test('env-only, Anthropic parameters, responsive native dialog and administrator visibility', async ({ page }) => {
  const state = await mockManagement(page, { storage: 'env_only', providers: [provider('anthropic', { name: 'Anthropic 测试', protocol: 'anthropic', auth_mode: 'none' })],
    models: [{ id: 'legacy', revision: 1, settings: modelSettings({ provider_id: 'anthropic', supports_reasoning: true, token_field: 'max_completion_tokens', parameters: { ...defaults(), seed: 42 } }), available: false, unavailable_reason: '凭据不可用或模型参数不兼容' }] });
  await login(page); await open(page); await fillProvider(page, 'environment');
  await expect(page.getByLabel('认证方式', { exact: true })).toHaveValue('env');
  await expect(page.getByLabel('认证方式', { exact: true }).locator('option[value="stored"]')).toBeDisabled();
  await expect(page.getByLabel('API 密钥（只写）', { exact: true })).toBeDisabled();
  await page.getByLabel('协议', { exact: true }).selectOption('anthropic');
  await expect(page.getByLabel('API 基础地址', { exact: true })).toHaveAttribute('placeholder', 'https://api.anthropic.com/v1');
  await page.getByLabel('环境变量名', { exact: true }).fill('TEST_ONLY_PROVIDER_KEY');
  await action(manager(page), '保存提供商').click();
  await expect(manager(page).locator('[data-provider-id="environment"]')).toBeVisible();
  expect(state.calls.at(-1).body).toMatchObject({ secret_action: 'keep', settings: { auth_mode: 'env', env_var: 'TEST_ONLY_PROVIDER_KEY' } });
  expect(state.calls.at(-1).body).not.toHaveProperty('api_key');
  await manager(page).getByRole('tab', { name: '提供商', exact: true }).focus(); await page.keyboard.press('End');
  await expect(manager(page).getByRole('tab', { name: '模型', exact: true })).toBeFocused();
  await action(manager(page), '新建模型').click();
  await page.getByLabel('模型 ID', { exact: true }).fill('anthropic-model');
  await page.getByLabel('模型名称', { exact: true }).fill('Anthropic 模型');
  await page.getByLabel('远端模型 ID', { exact: true }).fill('manual-model');
  await expect(page.getByLabel('支持思考级别（仅 OpenAI）', { exact: true })).toBeDisabled();
  await expect(page.getByLabel('随机种子 · seed', { exact: true })).toBeDisabled();
  await expect(page.getByLabel('频率惩罚 · frequency_penalty', { exact: true })).toBeDisabled();
  await expect(page.getByLabel('OpenAI 输出 token 字段', { exact: true })).toBeDisabled();
  await expect(page.getByLabel('温度 · temperature', { exact: true })).toHaveAttribute('max', '1');
  await page.getByLabel('温度 · temperature', { exact: true }).fill('0.5');
  await page.getByLabel('核采样 · top_p', { exact: true }).fill('0.8');
  await action(manager(page), '保存模型').click();
  await expect(manager(page).getByRole('alert')).toContainText('之一'); expect(state.calls).toHaveLength(1);
  await page.getByLabel('核采样 · top_p', { exact: true }).fill('');
  await page.getByLabel('明确清空停止词（发送 []）', { exact: true }).check();
  for (const width of [320, 390, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 844 });
    const size = await manager(page).evaluate(node => ({ scroll: node.scrollWidth, client: node.clientWidth, right: node.getBoundingClientRect().right, width: innerWidth }));
    expect(size.scroll).toBeLessThanOrEqual(size.client + 1); expect(size.right).toBeLessThanOrEqual(size.width);
  }
  await page.setViewportSize({ width: 390, height: 844 });
  for (let i = 0; i < 25; i++) { await page.keyboard.press('Tab'); expect(await manager(page).evaluate(node => node.contains(document.activeElement))).toBeTruthy(); }
  await action(manager(page), '保存模型').click();
  await expect(manager(page).locator('[data-model-id="anthropic-model"]')).toBeVisible();
  expect(state.calls.at(-1).body.settings.parameters).toEqual({ ...defaults(), temperature: 0.5, stop: [] });
  await action(manager(page).locator('[data-model-id="legacy"]'), '编辑').click();
  await expect(manager(page)).toContainText('当前保存值包含 Anthropic 不支持的字段');
  await action(manager(page), '清除不支持的字段').click();
  await expect(page.getByLabel('随机种子 · seed', { exact: true })).toHaveValue('');
  await expect(page.getByLabel('支持思考级别（仅 OpenAI）', { exact: true })).not.toBeChecked();
  await action(manager(page), '保存模型').click();
  await expect(manager(page).locator('[data-model-id="legacy"]')).toBeVisible();
  expect(state.calls.at(-1).body.settings).toMatchObject({ supports_reasoning: false, token_field: 'max_tokens', parameters: { seed: null } });
  await page.keyboard.press('Escape'); await expect(manager(page)).toHaveCount(0);
  await page.unroute('**/api/v1/**'); await mockManagement(page, { admin: false });
  await login(page); await action(page, '打开会话列表').click(); await expect(action(page, '提供商与模型')).toHaveCount(0);
});

test('catalog refresh preserves unsaved session generation and stale current model; full PUT keeps overrides', async ({ page }) => {
  const state = await mockManagement(page, { zeroSessions: false, providers: [provider()], models: [{ id: 'managed', revision: 1, settings: modelSettings(), available: true, unavailable_reason: null }] });
  await login(page);
  await expect(page.locator('.connection-label')).toHaveText('实时连接');
  await action(page, '新建会话').click(); await page.getByLabel('会话名称', { exact: true }).fill('管理目录刷新 · UI 夹具');
  const created = page.waitForResponse(response => response.url().endsWith('/api/v1/sessions') && response.request().method() === 'POST');
  await action(page, '创建会话').click(); const id = (await (await created).json()).id;
  await expect(page.locator('.connection-label')).toHaveText('实时连接');
  await page.getByRole('tab', { name: '配置', exact: true }).click();
  await page.getByLabel('每次运行轮次', { exact: true }).fill('3');
  await page.getByLabel('温度 · temperature', { exact: true }).fill('0.25');
  await page.getByLabel('单次输出上限 · max_tokens', { exact: true }).fill('250');
  await page.getByLabel('明确清空停止词（发送 []）', { exact: true }).check();
  await page.getByLabel('模型', { exact: true }).selectOption('managed');
  await open(page); await manager(page).getByRole('tab', { name: '模型', exact: true }).click();
  await action(manager(page).locator('[data-model-id="managed"]'), '停用').click();
  await expect(manager(page).locator('[data-model-id="managed"]')).toContainText('模型已停用');
  await action(manager(page), '关闭提供商与模型').click();
  await expect(page.getByLabel('每次运行轮次', { exact: true })).toHaveValue('3');
  await expect(page.getByLabel('温度 · temperature', { exact: true })).toHaveValue('0.25');
  await expect(page.getByLabel('单次输出上限 · max_tokens', { exact: true })).toHaveValue('250');
  await expect(page.getByLabel('明确清空停止词（发送 []）', { exact: true })).toBeChecked();
  await expect(page.getByLabel('模型', { exact: true })).toHaveValue('managed');
  await expect(page.getByLabel('模型', { exact: true }).locator('option:checked')).toContainText('当前目录不可用');
  await expect(page.locator('.composer-model')).toContainText('Demo');
  await page.getByLabel('模型', { exact: true }).selectOption('demo');
  const saved = page.waitForResponse(response => response.url().endsWith(`/${id}/config`) && response.request().method() === 'PUT');
  await action(page, '保存会话配置').click();
  const response = await saved; const body = response.request().postDataJSON();
  expect(body.settings.generation).toEqual({ ...defaults(), temperature: 0.25, max_tokens: 250, stop: [] });
  expect(body.settings.max_turns_per_run).toBe(3);
  expect(body.settings.max_tokens_per_input).toBe(fixtures.snapshot.settings.max_tokens_per_input);
  expect(state.calls.filter(call => call.path.endsWith('/test'))).toHaveLength(0);
});

test('Responses protocol uses OpenAI base URL and clears unsupported fields only on explicit switching', async ({ page }) => {
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  const state = await mockManagement(page, { providers: [provider('primary', { auth_mode: 'none' })] });
  await login(page); await open(page); await fillProvider(page, 'responses');
  const input = label => manager(page).getByLabel(label, { exact: true });
  await input('协议').selectOption('openai-responses');
  await expect(input('API 基础地址')).toHaveAttribute('placeholder', 'https://api.openai.com/v1');
  await input('认证方式').selectOption('none');
  await input('API 基础地址').fill('https://provider.invalid/v1/responses');
  await action(manager(page), '保存提供商').click();
  await expect(manager(page).getByRole('alert')).toContainText('API 基础地址');
  expect(state.calls).toHaveLength(0);
  await input('API 基础地址').fill('https://provider.invalid/v1');
  await action(manager(page), '保存提供商').click();
  await expect(manager(page).locator('[data-provider-id="responses"]')).toContainText('OpenAI · Responses');
  expect(state.calls[0].body.settings.protocol).toBe('openai-responses');
  await manager(page).getByRole('tab', { name: '模型', exact: true }).click();
  await action(manager(page), '新建模型').click();
  await input('模型名称').fill('gpt-4.1'); await input('远端模型 ID').fill('gpt-4.1');
  await input('随机种子 · seed').fill('7');
  await input('频率惩罚 · frequency_penalty').fill('0.5');
  await input('明确清空停止词（发送 []）').check();
  await input('OpenAI 输出 token 字段').selectOption('max_completion_tokens');
  await input('所属提供商').selectOption('responses');
  for (const label of ['随机种子 · seed', '频率惩罚 · frequency_penalty', '存在惩罚 · presence_penalty', '停止词 · stop']) {
    await expect(input(label)).toBeDisabled(); await expect(input(label)).toHaveValue('');
  }
  await expect(input('明确清空停止词（发送 []）')).toBeDisabled();
  await expect(input('明确清空停止词（发送 []）')).not.toBeChecked();
  await expect(input('OpenAI 输出 token 字段')).toBeDisabled();
  await expect(manager(page)).toContainText('固定映射为 max_output_tokens');
  await expect(input('支持思考级别（仅 OpenAI）')).toBeEnabled();
  await input('支持思考级别（仅 OpenAI）').check();
  await input('温度 · temperature').fill('0.4');
  await input('单次输出上限 · max_tokens').fill('256');
  await expect(input('模型 ID')).toHaveValue('gpt-4.1');
  expect(await input('模型 ID').getAttribute('pattern')).toBeNull();
  expect(await input('模型 ID').evaluate(node => node.checkValidity())).toBe(true);
  await action(manager(page), '保存模型').click();
  await expect(manager(page).locator('[data-model-id="gpt-4.1"]')).toBeVisible();
  expect(state.calls.at(-1)).toMatchObject({ method: 'POST', path: '/models', body: { expected_revision: 0, settings: { supports_reasoning: true, token_field: 'max_tokens' } } });
  expect(state.calls.at(-1).body.settings.parameters).toEqual({ ...defaults(), temperature: 0.4, max_tokens: 256 });
  expect(state.calls.some(call => call.path.endsWith('/test'))).toBe(false);
  expect(errors).toEqual([]);
});

test('model name defaults, discovery, provider collisions and stale-catalog fallback use automatic POST', async ({ page }) => {
  const existing = { id: 'gpt-4.1', revision: 1, settings: modelSettings({ name: 'gpt-4.1' }), available: true, unavailable_reason: null };
  const state = await mockManagement(page, {
    providers: [provider('primary'), provider('secondary')], models: [existing],
    testModels: [{ id: 'gpt-4.1', name: 'gpt-4.1' }],
  });
  await login(page); await open(page);
  const primary = manager(page).locator('[data-provider-id="primary"]');
  await action(primary, '检测 / 获取模型').click(); await action(primary, '添加模型').click();
  const input = label => manager(page).getByLabel(label, { exact: true });
  await expect(input('模型名称')).toHaveValue('gpt-4.1');
  await expect(input('远端模型 ID')).toHaveValue('gpt-4.1');
  await expect(input('模型 ID')).toHaveValue('primary-gpt-4.1');
  await input('所属提供商').selectOption('secondary');
  await expect(input('模型 ID')).toHaveValue('secondary-gpt-4.1');
  await input('模型名称').fill('demo'); await expect(input('模型 ID')).toHaveValue('secondary-demo');
  await input('模型名称').fill('GPT-4.1'); await expect(input('模型 ID')).toHaveValue('GPT-4.1');
  await input('模型名称').fill('gpt-4.1');
  await action(manager(page), '保存模型').click();
  await expect(manager(page).locator('[data-model-id="secondary-gpt-4.1"]')).toBeVisible();
  expect(state.calls.at(-1)).toMatchObject({ method: 'POST', path: '/models' });
  await action(manager(page), '新建模型').click();
  await input('所属提供商').selectOption('secondary');
  await input('模型名称').fill('gpt-4.1'); await input('远端模型 ID').fill('gpt-4.1');
  await expect(input('模型 ID')).toHaveValue('secondary-gpt-4.1-2');
  await input('模型 ID').fill('Custom-GPT.4.1');
  await input('模型名称').fill('changed name'); await expect(input('模型 ID')).toHaveValue('Custom-GPT.4.1');
  await input('模型 ID').fill(''); await expect(input('模型 ID')).toHaveValue('changed-name');
  await input('模型名称').fill('gpt-4.1');
  await expect(input('模型 ID')).toHaveValue('secondary-gpt-4.1-2');
  // Another client creates the previewed ID after this browser has read its catalog.
  state.models.push({ ...structuredClone(existing), id: 'secondary-gpt-4.1-2', settings: modelSettings({ provider_id: 'secondary', name: 'gpt-4.1' }) });
  await action(manager(page), '保存模型').click();
  await expect(manager(page).locator('[data-model-id="secondary-gpt-4.1-3"]')).toBeVisible();
  expect(state.calls.at(-1)).toMatchObject({ method: 'POST', path: '/models', body: { expected_revision: 0, settings: { name: 'gpt-4.1' } } });
  expect(state.calls.at(-1).body).not.toHaveProperty('id');
});

test('manual dotted Unicode IDs remain stable and Responses legacy values require explicit repair', async ({ page }) => {
  const state = await mockManagement(page, {
    providers: [provider('primary', { protocol: 'openai-responses' }), provider('secondary')],
    models: [
      { id: 'legacy-GPT.4.1', revision: 1, settings: modelSettings({ token_field: 'max_completion_tokens', supports_reasoning: true }), available: true, unavailable_reason: null },
      { id: 'needs-repair', revision: 1, settings: modelSettings({ supports_reasoning: true, token_field: 'max_completion_tokens', parameters: { ...defaults(), temperature: 0.2, seed: 7, stop: [] } }), available: false, unavailable_reason: 'incompatible' },
    ],
  });
  await login(page); await open(page);
  await manager(page).getByRole('tab', { name: '模型', exact: true }).click();
  await action(manager(page), '新建模型').click();
  const input = label => manager(page).getByLabel(label, { exact: true });
  await input('模型名称').fill('手工模型'); await input('远端模型 ID').fill('remote-4.1');
  await input('模型 ID').fill('bad/name');
  expect(await input('模型 ID').evaluate(node => node.checkValidity())).toBe(false);
  await input('模型 ID').fill('手工-GPT-4.1');
  expect(await input('模型 ID').evaluate(node => node.checkValidity())).toBe(true);
  await action(manager(page), '保存模型').click();
  const manual = manager(page).locator('[data-model-id="手工-GPT-4.1"]');
  await expect(manual).toBeVisible();
  expect(state.calls.at(-1)).toMatchObject({ method: 'PUT', path: `/models/${encodeURIComponent('手工-GPT-4.1')}` });
  await action(manual, '编辑').click();
  await expect(input('模型 ID')).toHaveAttribute('readonly', '');
  await input('模型名称').fill('新显示名'); await input('所属提供商').selectOption('secondary');
  await expect(input('模型 ID')).toHaveValue('手工-GPT-4.1');
  await action(manager(page), '保存模型').click(); await expect(manual).toContainText('新显示名');
  expect(state.calls.at(-1)).toMatchObject({ method: 'PUT', path: `/models/${encodeURIComponent('手工-GPT-4.1')}`, body: { expected_revision: 1 } });
  const legacy = manager(page).locator('[data-model-id="legacy-GPT.4.1"]');
  await action(legacy, '编辑').click();
  await expect(input('OpenAI 输出 token 字段')).toHaveValue('max_completion_tokens');
  await expect(input('OpenAI 输出 token 字段')).toBeDisabled();
  await input('模型名称').fill('保留旧 Chat 字段');
  await action(manager(page), '保存模型').click(); await expect(legacy).toContainText('保留旧 Chat 字段');
  expect(state.calls.at(-1).body.settings.token_field).toBe('max_completion_tokens');
  const repair = manager(page).locator('[data-model-id="needs-repair"]');
  await action(repair, '编辑').click();
  await expect(manager(page)).toContainText('当前保存值包含 OpenAI Responses 不支持的字段');
  await expect(input('随机种子 · seed')).toHaveValue('7');
  await expect(input('明确清空停止词（发送 []）')).toBeChecked();
  await action(manager(page), '清除不支持的字段').click();
  await expect(input('随机种子 · seed')).toHaveValue('');
  await expect(input('明确清空停止词（发送 []）')).not.toBeChecked();
  await expect(input('支持思考级别（仅 OpenAI）')).toBeChecked();
  await action(manager(page), '保存模型').click(); await expect(repair).toBeVisible();
  expect(state.calls.at(-1).body.settings.parameters).toEqual({ ...defaults(), temperature: 0.2 });
});
