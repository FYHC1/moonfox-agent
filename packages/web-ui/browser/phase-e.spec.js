import { test, expect } from '@playwright/test';

const backend = process.env.MOONFOX_TEST_BACKEND;
const token = process.env.MOONFOX_TEST_TOKEN;
const fixture = process.env.MOONFOX_UI_FIXTURE === '1';
const fixtureExtensions = async (request, installed = ['persona', 'memory']) => {
  const response = await request.post(`${backend}/test/extensions`, { data: { installed } });
  expect(response.ok()).toBeTruthy();
};
const commandRequests = (page) => {
  const requests = [];
  page.on('request', (request) => {
    if (request.url().includes('/extensions/memory/commands/')) requests.push(request);
  });
  return requests;
};
const bodyOf = (request) => request.postDataJSON();
const argsOf = (request) => JSON.parse(bodyOf(request).arguments_json);
const action = (root, name) => root.getByRole('button', { name, exact: true });

async function login(page) {
  await page.goto('/');
  await page.getByLabel('后端地址', { exact: true }).fill(backend);
  await page.getByLabel('管理员令牌', { exact: true }).fill(token);
  await action(page, '连接工作台').click();
  await expect(page.locator('.workspace')).toBeVisible();
  await expect(page.locator('.connection-label')).toHaveText('实时连接');
}
async function inspectorTab(page, name) {
  const tab = page.getByRole('tab', { name, exact: true });
  if (!await tab.isVisible()) await action(page, '会话详情').click();
  await tab.click();
  return tab;
}
async function noHorizontalOverflow(page) {
  const size = await page.evaluate(() => ({ width: innerWidth, actual: document.documentElement.scrollWidth }));
  expect(size.actual).toBeLessThanOrEqual(size.width + 1);
}

 test.beforeEach(async ({ request }) => {
  test.skip(!backend || !token || !fixture, 'Requires the explicit local-only UI fixture service.');
  await fixtureExtensions(request);
});

test('schema-backed persona and memory settings preserve other extension configs and require explicit CAS reload', async ({ page, request }) => {
  const errors = []; const puts = [];
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('request', (request) => { if (request.method() === 'PUT' && request.url().endsWith('/config')) puts.push(request); });
  await login(page);
  await inspectorTab(page, '配置');
  const personaName = page.getByLabel('人设名称', { exact: true });
  const examples = page.getByLabel('对话示例 JSON', { exact: true });
  const extraction = page.getByLabel('自动抽取', { exact: true });
  await expect(personaName).toHaveValue('月狐');
  await expect(extraction).toHaveValue('off');
  await page.getByLabel('最多召回条数', { exact: true }).fill('7');
  await extraction.selectOption('model');
  await expect(page.getByText(/额外请求模型.*共享请求与 token 预算/)).toBeVisible();
  await extraction.selectOption('off');
  await personaName.fill('测试人设');
  await page.getByLabel('配置档案 ID', { exact: true }).fill('phase_e');
  await examples.fill(JSON.stringify([{ user: '你好', assistant: '你好，我是月狐。' }], null, 2));
  const id = await page.locator('.session-item.is-selected').getAttribute('data-session-id');
  const save = page.waitForResponse((response) => response.url().endsWith(`/${id}/config`) && response.request().method() === 'PUT');
  await action(page, '保存会话配置').click();
  const response = await save;
  expect(response.status()).toBe(200);
  const sent = bodyOf(puts[0]);
  expect(sent.expected_revision).toBe(0);
  expect(sent.settings.enabled_extensions).not.toContain('memory');
  expect(sent.settings.enabled_extensions).not.toContain('persona');
  expect(sent.settings.allowed_tools).toEqual(['echo']);
  expect(sent.settings.approval_tools).toEqual(['echo']);
  const configurations = sent.settings.extension_config;
  const persona = JSON.parse(configurations.find((entry) => entry.extension_id === 'persona').config_json);
  const memory = JSON.parse(configurations.find((entry) => entry.extension_id === 'memory').config_json);
  expect(persona).toMatchObject({ version: 1, profile_id: 'phase_e', revision: 1, name: '测试人设', honesty_mode: 'roleplay' });
  expect(persona.examples).toEqual([{ user: '你好', assistant: '你好，我是月狐。' }]);
  expect(memory).toMatchObject({ version: 1, recall: true, max_results: 7, max_reference_tokens: 800, extraction: 'off', extraction_max_tokens: 512 });
  expect(configurations.find((entry) => entry.extension_id === 'fixture-hidden')).toEqual({ extension_id: 'fixture-hidden', config_json: '{"opaque":"retain"}' });
  await expect(page.locator('.settings-footer')).toContainText('已保存 r1');

  await personaName.fill('未保存草稿');
  await request.post(`${backend}/test/conflict`, { data: {} });
  const conflict = page.waitForResponse((item) => item.url().endsWith(`/${id}/config`) && item.request().method() === 'PUT');
  await action(page, '保存会话配置').click();
  expect((await conflict).status()).toBe(409);
  await expect(page.getByRole('alert')).toContainText('修订冲突');
  await expect(personaName).toHaveValue('未保存草稿');
  await expect(action(page, '保存会话配置')).toBeDisabled();
  expect(puts).toHaveLength(2);
  await action(page, '重新载入服务端配置').click();
  await action(page.getByRole('dialog', { name: '重新载入服务端配置？', exact: true }), '放弃编辑并载入').click();
  await expect(personaName).toHaveValue('测试人设');
  await expect(page.locator('.revision-label')).toHaveText('r2');
  expect(puts).toHaveLength(2);
  expect(errors).toEqual([]);
});

test('memory management adds, edits, forgets, confirms deletes and manually refreshes jobs without chat writes', async ({ page, request }) => {
  test.setTimeout(50000);
  await page.setViewportSize({ width: 390, height: 844 });
  const errors = []; page.on('pageerror', (error) => errors.push(error.message));
  const requests = commandRequests(page);
  await login(page);
  const list = page.waitForRequest((request) => request.url().endsWith('/extensions/memory/commands/list'));
  await inspectorTab(page, '记忆');
  const firstList = await list;
  expect(new URL(firstList.url()).pathname).toContain('/sessions/fixture-new/extensions/memory/commands/list');
  expect(argsOf(firstList)).toEqual({ query: '', limit: 50, include_forgotten: false });
  expect(Object.keys(bodyOf(firstList)).sort()).toEqual(['arguments_json', 'expected_revision', 'request_id']);
  expect(bodyOf(firstList)).not.toHaveProperty('domain'); expect(bodyOf(firstList)).not.toHaveProperty('scope');
  await expect(page.locator('.memory-panel')).toContainText('自动召回已停用');
  await expect(page.getByText('此会话还没有记忆', { exact: true })).toBeVisible();

  const original = '<img src=x onerror=alert(1)> 手动偏好候选';
  await page.getByLabel('新记忆内容', { exact: true }).fill(original);
  await page.getByLabel('类型', { exact: true }).selectOption('preference');
  await page.getByLabel('主题', { exact: true }).fill('conversation');
  const remember = page.waitForRequest((request) => request.url().endsWith('/extensions/memory/commands/remember'));
  await action(page, '添加记忆').click();
  const rememberRequest = await remember; const rememberArgs = argsOf(rememberRequest);
  expect(rememberArgs).toEqual({ text: original, kind: 'preference', subject: 'conversation' });
  expect(bodyOf(rememberRequest).expected_revision).toBe(bodyOf(firstList).expected_revision);
  expect(bodyOf(rememberRequest)).not.toHaveProperty('domain'); expect(bodyOf(rememberRequest)).not.toHaveProperty('scope');
  const recordId = await page.locator('.memory-record').filter({ hasText: '手动偏好候选' }).getAttribute('data-memory-id');
  const card = page.locator(`[data-memory-id="${recordId}"]`);
  await expect(card).toContainText('手动录入'); await expect(card).toContainText('置信度 · 100%'); await expect(card).toContainText('修订 r1');
  await expect(card.locator('img')).toHaveCount(0);
  const search = page.getByLabel('搜索记忆', { exact: true });
  await search.fill('手动偏好');
  const searched = page.waitForRequest((item) => item.url().endsWith('/extensions/memory/commands/list'));
  await action(page, '应用筛选').click();
  expect(argsOf(await searched)).toEqual({ query: '手动偏好', limit: 50, include_forgotten: false });
  await expect(card).toBeVisible();
  await search.fill('');
  const unfiltered = page.waitForRequest((item) => item.url().endsWith('/extensions/memory/commands/list'));
  await action(page, '应用筛选').click();
  await unfiltered;

  await action(card, '编辑').click();
  const editInput = page.getByLabel('编辑记忆内容', { exact: true });
  await editInput.fill('保留但不自动覆盖的草稿');
  await request.post(`${backend}/test/memory-conflict`, { data: {} });
  const conflictedUpdate = page.waitForRequest((item) => item.url().endsWith('/extensions/memory/commands/update'));
  const conflictResponse = page.waitForResponse((item) => item.url().endsWith('/extensions/memory/commands/update'));
  await action(card, '保存记忆').click();
  expect((await conflictResponse).status()).toBe(409);
  expect(argsOf(await conflictedUpdate)).toMatchObject({ id: recordId, expected_revision: 1, text: '保留但不自动覆盖的草稿' });
  await expect(editInput).toHaveValue('保留但不自动覆盖的草稿');
  await expect(page.locator('.memory-panel')).toContainText('输入已保留');
  expect(requests.filter((item) => item.url().endsWith('/extensions/memory/commands/update'))).toHaveLength(1);
  const conflictRefresh = page.waitForRequest((item) => item.url().endsWith('/extensions/memory/commands/list'));
  await action(page, '刷新并核对').click();
  await conflictRefresh;
  await expect(editInput).toHaveValue('保留但不自动覆盖的草稿');
  await action(card, '取消编辑').click();

  await action(card, '编辑').click();
  await page.getByLabel('编辑记忆内容', { exact: true }).fill('更新后的偏好记录');
  const update = page.waitForRequest((item) => item.url().endsWith('/extensions/memory/commands/update'));
  await action(card, '保存记忆').click();
  const updateRequest = await update;
  expect(argsOf(updateRequest)).toEqual({ id: recordId, expected_revision: 1, text: '更新后的偏好记录', kind: 'preference', subject: 'conversation' });
  await expect(card).toContainText('修订 r2');

  const forget = page.waitForRequest((request) => request.url().endsWith('/extensions/memory/commands/forget'));
  await action(card, '停止召回').click();
  const forgetRequest = await forget;
  expect(argsOf(forgetRequest)).toMatchObject({ expected_revision: 2 });
  await expect(card).toHaveCount(0);
  await page.getByLabel('包含已遗忘记录', { exact: true }).check();
  const forgottenList = page.waitForRequest((request) => request.url().endsWith('/extensions/memory/commands/list'));
  await action(page, '应用筛选').click();
  expect(argsOf(await forgottenList)).toEqual({ query: '', limit: 50, include_forgotten: true });
  await expect(card).toContainText('已遗忘 · 停止召回');
  await expect(card).toContainText('修订 r3');

  await action(card, '删除扩展副本').click();
  const confirm = page.getByRole('dialog', { name: '删除这条记忆副本？', exact: true });
  await expect(confirm).toContainText('JSONL 聊天/工具历史');
  await expect(confirm).toContainText('云模型副本或外部备份');
  await action(confirm, '返回').click();
  expect(requests.filter((request) => request.url().endsWith('/extensions/memory/commands/delete'))).toHaveLength(0);
  await action(card, '删除扩展副本').click();
  const deleteConfirm = page.getByRole('dialog', { name: '删除这条记忆副本？', exact: true });
  const deletion = page.waitForRequest((request) => request.url().endsWith('/extensions/memory/commands/delete'));
  await action(deleteConfirm, '确认删除扩展副本').click();
  const deleteRequest = await deletion;
  expect(argsOf(deleteRequest)).toMatchObject({ expected_revision: 3 });
  await expect(card).toHaveCount(0);
  await expect(page.locator('.memory-panel')).toContainText('原始聊天和工具历史未更改');

  const jobs = page.waitForRequest((request) => request.url().endsWith('/extensions/memory/commands/jobs'));
  await action(page, '手动刷新任务状态').click();
  expect(argsOf(await jobs)).toEqual({});
  await expect(page.locator('.memory-jobs')).toContainText('已完成');
  await noHorizontalOverflow(page);
  expect(await page.evaluate(() => ({ local: localStorage.length, session: sessionStorage.length }))).toEqual({ local: 0, session: 0 });
  expect(requests.some((request) => /\/inputs$|\/control$/.test(new URL(request.url()).pathname))).toBeFalsy();
  expect(errors).toEqual([]);
});

test('memory list aborts on scope change, never paints late records, and keeps QQ as QQ', async ({ page, request }) => {
  test.setTimeout(50000);
  const requests = commandRequests(page);
  await login(page);
  const initialList = page.waitForRequest((item) => item.url().endsWith('/extensions/memory/commands/list'));
  await inspectorTab(page, '记忆');
  await initialList;
  const unique = `仅 fixture-new ${Date.now()}`;
  await page.getByLabel('新记忆内容', { exact: true }).fill(unique);
  const save = page.waitForRequest((item) => item.url().endsWith('/extensions/memory/commands/remember'));
  await action(page, '添加记忆').click(); await save;
  await expect(page.locator('.memory-record').filter({ hasText: unique })).toContainText(unique);
  const staleDraft = '不应带入新会话的草稿';
  await page.getByLabel('新记忆内容', { exact: true }).fill(staleDraft);
  await request.post(`${backend}/test/memory-delay`, { data: { milliseconds: 700 } });
  const delayed = page.waitForRequest((item) => item.url().endsWith('/extensions/memory/commands/list'));
  await action(page, '刷新记忆列表').click();
  const lateRequest = await delayed;
  expect(new URL(lateRequest.url()).pathname).toContain('/sessions/fixture-new/');
  await page.locator('[data-session-id="fixture-history"]').click();
  await expect(page.locator('.title-stack h1')).toHaveText('工具与边界 · UI 夹具');
  await expect(page.locator('.connection-label')).toHaveText('实时连接');
  await expect(page.locator('.memory-scope')).toContainText('fixture-history');
  await expect(page.locator('.memory-scope')).toContainText('Agent 会话');
  await expect(page.getByLabel('新记忆内容', { exact: true })).toHaveValue('');
  await page.waitForTimeout(850);
  await expect(page.locator('.memory-panel')).not.toContainText(unique);
  await expect(page.locator('.memory-panel')).toContainText('加载当前会话记忆');
  expect(requests.filter((item) => new URL(item.url()).pathname.includes('/sessions/fixture-history/extensions/memory/commands/')).length).toBe(0);
  const historyList = page.waitForRequest((item) => item.url().endsWith('/extensions/memory/commands/list'));
  await action(page, '加载当前会话记忆').click();
  expect(new URL((await historyList).url()).pathname).toContain('/sessions/fixture-history/');

  await page.locator('[data-session-id="fixture-qq"]').click();
  await expect(page.locator('.title-stack h1')).toHaveText('QQ 来源 · 只读夹具');
  await expect(page.locator('.memory-scope')).toContainText('QQ 来源');
  await expect(page.locator('.memory-scope')).not.toContainText('Agent 会话');
  const qqList = page.waitForRequest((item) => item.url().endsWith('/extensions/memory/commands/list'));
  await action(page, '加载当前会话记忆').click();
  const qqRequest = await qqList;
  expect(new URL(qqRequest.url()).pathname).toContain('/sessions/fixture-qq/');
  expect(bodyOf(qqRequest)).not.toHaveProperty('domain'); expect(bodyOf(qqRequest)).not.toHaveProperty('scope');
});

test('non-administrators receive no memory data and an uninstalled extension has an explicit empty state', async ({ page, request }) => {
  const calls = commandRequests(page);
  await page.route('**/api/v1/me', (route) => route.fulfill({ status: 200, json: { id: 'fixture-viewer', is_admin: false } }));
  await login(page);
  await inspectorTab(page, '记忆');
  await expect(page.locator('.memory-panel')).toContainText('仅对管理员开放');
  await expect.poll(() => calls.length).toBe(0);

  await page.unroute('**/api/v1/me');
  await fixtureExtensions(request, []);
  await page.reload();
  await expect(page.getByLabel('后端地址', { exact: true })).toBeVisible();
  await page.getByLabel('后端地址', { exact: true }).fill(backend);
  await page.getByLabel('管理员令牌', { exact: true }).fill(token);
  await action(page, '连接工作台').click();
  await expect(page.locator('.connection-label')).toHaveText('实时连接');
  await inspectorTab(page, '记忆');
  await expect(page.locator('.memory-panel')).toContainText('记忆扩展未安装');
  await expect.poll(() => calls.length).toBe(0);
});

test('390px memory/extension side panels reflow without JS errors', async ({ page, request }) => {
  await fixtureExtensions(request);
  await page.setViewportSize({ width: 390, height: 844 });
  const errors = []; page.on('pageerror', (error) => errors.push(error.message));
  await login(page);
  await inspectorTab(page, '配置');
  await expect(page.getByLabel('自动抽取', { exact: true })).toBeVisible();
  await noHorizontalOverflow(page);
  await inspectorTab(page, '记忆');
  await expect(page.locator('.memory-scope')).toBeVisible();
  await expect.poll(async () => await page.locator('.memory-panel').evaluate(node => node.scrollWidth <= node.clientWidth + 1)).toBeTruthy();
  await noHorizontalOverflow(page);
  expect(errors).toEqual([]);
});
