import { test, expect } from '@playwright/test';

const backend = process.env.MOONFOX_TEST_BACKEND;
const token = process.env.MOONFOX_TEST_TOKEN;
const fixture = process.env.MOONFOX_UI_FIXTURE === '1';
const connected = Boolean(backend && token);
const input = (page) => page.getByLabel('发送给 Moonfox 的输入');
const waitLive = (page) => expect(page.locator('.connection-label')).toHaveText('实时连接');
const requestBody = (request) => request.postDataJSON();
const testAction = async (request, action, data = {}) => {
  const response = await request.post(`${backend}/test/${action}`, { data });
  expect(response.ok()).toBeTruthy(); return response.json();
};

async function login(page) {
  await page.goto('/');
  await page.getByLabel('后端地址', { exact: true }).fill(backend);
  await page.getByLabel('管理员令牌', { exact: true }).fill(token);
  await page.getByRole('button', { name: '连接工作台', exact: true }).click();
  await expect(page.locator('.workspace')).toBeVisible();
}
async function newSession(page, title = `UI 验收 ${Date.now()}`) {
  if (page.viewportSize().width < 760) await page.getByRole('button', { name: '打开会话列表', exact: true }).click();
  await page.getByRole('button', { name: '新建会话', exact: true }).click();
  await page.getByLabel('会话名称', { exact: true }).fill(title);
  const response = page.waitForResponse((response) => response.url().endsWith('/api/v1/sessions') && response.request().method() === 'POST');
  await page.getByRole('button', { name: '创建会话', exact: true }).click();
  const created = await response; const body = await created.json();
  expect(Object.keys(requestBody(created.request())).sort()).toEqual(['request_id', 'title']);
  expect(requestBody(created.request()).request_id).toMatch(/^[\da-f-]{36}$/i);
  await expect(page.locator('.title-stack h1')).toHaveText(title);
  await waitLive(page); return body.id;
}
async function settings(page) {
  if (!await page.getByRole('tab', { name: '配置', exact: true }).isVisible()) await page.getByRole('button', { name: '会话详情', exact: true }).click();
  await page.getByRole('tab', { name: '配置', exact: true }).click();
  await expect(page.getByLabel('每次运行轮次', { exact: true })).toBeEnabled();
}
async function noHorizontalOverflow(page) {
  const bounds = await page.evaluate(() => ({ width: innerWidth, actual: document.documentElement.scrollWidth }));
  expect(bounds.actual).toBeLessThanOrEqual(bounds.width + 1);
}

test('connection page is explicit about offline demo; keyboard focus and reduced motion work', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '欢迎回到 Moonfox' })).toBeVisible();
  await expect(page.getByText('离线演示为可选模式', { exact: true })).toBeVisible();
  await expect(page.getByLabel('管理员令牌', { exact: true })).toHaveAttribute('type', 'password');
  await page.keyboard.press('Tab');
  await expect(page.locator('.skip-link')).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page.getByLabel('后端地址', { exact: true })).toBeFocused();
  expect(await page.getByLabel('后端地址', { exact: true }).evaluate((node) => getComputedStyle(node).outlineWidth)).toBe('2px');
  await page.emulateMedia({ reducedMotion: 'reduce' });
  expect(await page.locator('.connection-card').evaluate((node) => getComputedStyle(node).animationName)).toBe('none');
  for (const width of [320, 390, 768, 1024, 1440]) { await page.setViewportSize({ width, height: 844 }); await noHorizontalOverflow(page); }
});

test('authenticated create, Enter/Shift+Enter, safe content, real subscription and disconnect', async ({ page }) => {
  test.skip(!connected, 'Set MOONFOX_TEST_BACKEND and MOONFOX_TEST_TOKEN for backend integration.');
  const errors = []; page.on('pageerror', (error) => errors.push(error.message));
  const posts = []; page.on('request', (request) => { if (request.method() === 'POST') posts.push(request); });
  await login(page); const id = await newSession(page);
  await expect(page.getByRole('heading', { name: '留一点空间，让想法开始。' })).toBeVisible();
  await expect(page.getByRole('button', { name: '压缩上下文', exact: true })).toHaveCount(0);
  const text = '<img src=x onerror=alert(1)>\n这是一条 UI 验收输入';
  await input(page).fill('<img src=x onerror=alert(1)>'); await input(page).press('Shift+Enter'); await input(page).pressSequentially('这是一条 UI 验收输入');
  await expect(input(page)).toHaveValue(text);
  expect(posts.filter((request) => request.url().endsWith('/inputs'))).toHaveLength(0);
  await input(page).press('Enter');
  await expect(page.locator('.message-user .message-text').last()).toHaveText(text);
  await expect(page.locator('.message-user .message-text img')).toHaveCount(0);
  await expect(page.locator('.outcome-completed')).toBeVisible(); await waitLive(page);
  const submission = posts.filter((request) => request.url().endsWith('/inputs'));
  expect(submission).toHaveLength(1); expect(requestBody(submission[0]).idempotency_key).toMatch(/^[\da-f-]{36}$/i);
  await expect(page.locator('.composer-notice')).not.toContainText('不代表执行完成');
  const ids = await page.locator('.message').evaluateAll((nodes) => nodes.map((node) => node.dataset.messageId));
  expect(new Set(ids).size).toBe(ids.length);
  expect(await page.evaluate(() => ({ local: localStorage.length, session: sessionStorage.length })) ).toEqual({ local: 0, session: 0 });
  expect(page.url()).not.toContain(token);
  await page.getByRole('button', { name: '断开连接', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: '断开连接', exact: true }).click();
  await expect(page.getByLabel('管理员令牌', { exact: true })).toHaveValue('');
  expect(posts.some((request) => request.url().includes(`/${id}/control`))).toBeFalsy();
  expect(errors).toEqual([]);
});

test('configuration catalog controls save full CAS settings; conflict requires explicit reload', async ({ page, request }) => {
  test.skip(!connected || !fixture, 'Requires the explicit external UI fixture service, never product mocks.');
  await login(page); const id = await newSession(page); await settings(page);
  await page.getByLabel('每次运行轮次', { exact: true }).fill('3');
  await page.getByLabel('echo', { exact: true }).uncheck();
  await expect(page.getByLabel('每次需要审批', { exact: true })).toBeDisabled();
  await page.getByLabel('echo', { exact: true }).check();
  await page.getByLabel('每次需要审批', { exact: true }).check();
  await page.getByLabel('fixture-notes', { exact: false }).check();
  const saved = page.waitForResponse((response) => response.url().endsWith(`/${id}/config`) && response.request().method() === 'PUT');
  await page.getByRole('button', { name: '保存会话配置', exact: true }).click();
  const response = await saved; const dto = requestBody(response.request());
  expect(dto.expected_revision).toBe(0);
  expect(Object.keys(dto.settings).sort()).toEqual(['allowed_tools', 'approval_tools', 'enabled_extensions', 'generation', 'max_requests_per_input', 'max_tokens_per_input', 'max_turns_per_run', 'model_id', 'run_timeout_seconds', 'thinking_level']);
  expect(dto.settings.approval_tools).toEqual(['echo']);
  await expect(page.locator('.settings-footer')).toContainText('已保存 r1');
  await waitLive(page);
  await page.getByLabel('单输入请求上限', { exact: true }).fill('9');
  await testAction(request, 'conflict');
  const putCount = []; page.on('request', (r) => { if (r.method() === 'PUT') putCount.push(r); });
  await page.getByRole('button', { name: '保存会话配置', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('修订冲突');
  await expect(page.getByRole('button', { name: '保存会话配置', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: '重新载入服务端配置', exact: true }).click();
  await page.getByRole('button', { name: '放弃编辑并载入', exact: true }).click();
  await expect(page.getByLabel('单输入请求上限', { exact: true })).toHaveValue('8');
  await expect(page.locator('.revision-label')).toHaveText('r2');
  expect(putCount).toHaveLength(1);
});

test('network input retry keeps original idempotency key; 429 and 401 are visible', async ({ page, request }) => {
  test.skip(!connected || !fixture, 'Requires external fixture service.');
  await login(page); const id = await newSession(page);
  const submissions = []; page.on('request', (r) => { if (r.url().endsWith(`/${id}/inputs`)) submissions.push(requestBody(r)); });
  await testAction(request, 'fail', { path: `/${id}/inputs`, status: 0, persistent: true });
  await input(page).fill('【UI 夹具】手动重试'); await input(page).press('Enter');
  await expect(page.getByRole('button', { name: '按原幂等键重试', exact: true })).toBeVisible();
  await page.waitForTimeout(600); expect(submissions).toHaveLength(1);
  await testAction(request, 'fail', null);
  await page.getByRole('button', { name: '按原幂等键重试', exact: true }).click();
  await expect(page.locator('.outcome-completed')).toBeVisible();
  expect(submissions).toHaveLength(2); expect(submissions[0]).toEqual(submissions[1]);
  await testAction(request, 'fail', { path: `/${id}/inputs`, status: 429 });
  await input(page).fill('【UI 夹具】预算限制'); await input(page).press('Enter');
  await expect(page.getByRole('alert')).toContainText('预算或队列已达到上限');
  await expect(page.getByRole('button', { name: '按原幂等键重试', exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: '返回编辑', exact: true }).click();
  await expect(input(page)).toHaveValue('【UI 夹具】预算限制');
  await testAction(request, 'fail', { path: `/${id}/inputs`, status: 401 });
  await input(page).press('Enter');
  await expect(page.getByLabel('管理员令牌', { exact: true })).toHaveValue('');
  await expect(page.locator('.login-feedback')).toContainText('身份验证失败');
});

test('approval details are safe text, approve/deny settle, expiry and uncertain response disable decisions', async ({ page, request }) => {
  test.skip(!connected || !fixture, 'Requires external fixture service.');
  await login(page); const id = await newSession(page);
  for (const decision of ['deny', 'approve']) {
    const approval = await testAction(request, 'approval', { id });
    const card = page.locator(`[data-approval-id="${approval.id}"]`);
    await card.getByText('查看工具参数', { exact: true }).click();
    await expect(card.locator('pre')).toContainText('<img src=x onerror=alert(1)>');
    await expect(card.locator('pre img')).toHaveCount(0);
    await card.getByRole('button', { name: decision === 'deny' ? '拒绝' : '批准执行', exact: true }).click();
    await expect(card).toContainText(decision === 'deny' ? '已拒绝' : '已批准'); await waitLive(page);
    await expect(card.getByRole('button', { name: '批准执行', exact: true })).toHaveCount(0);
  }
  const expiring = await testAction(request, 'approval', { id, expires: 350 });
  const expired = page.locator(`[data-approval-id="${expiring.id}"]`);
  await expect(expired).toContainText('已过期'); await expect(expired.getByRole('button', { name: '批准执行', exact: true })).toBeDisabled();
  const uncertain = await testAction(request, 'approval', { id });
  const card = page.locator(`[data-approval-id="${uncertain.id}"]`);
  await testAction(request, 'fail', { path: `/approvals/${uncertain.id}`, status: 503 });
  await card.getByRole('button', { name: '批准执行', exact: true }).click();
  await expect(card.getByRole('button', { name: '批准执行', exact: true })).toBeDisabled();
  await card.getByRole('button', { name: '重新核对审批状态', exact: true }).click();
  await waitLive(page); await expect(card.getByRole('button', { name: '批准执行', exact: true })).toBeEnabled();
});

test('limit continuation uses input_id, cancel is explicit, resync never replays inference', async ({ page, request }) => {
  test.skip(!connected || !fixture, 'Requires external fixture service.');
  await login(page); const id = await newSession(page);
  const controls = []; let submissions = 0;
  page.on('request', (r) => { if (r.url().endsWith(`/${id}/control`)) controls.push(requestBody(r)); if (r.url().endsWith(`/${id}/inputs`)) submissions++; });
  await input(page).fill('【UI 夹具】limit'); await input(page).press('Enter');
  await expect(page.locator('.outcome-limit_reached')).toBeVisible();
  await page.waitForTimeout(300); expect(controls).toHaveLength(0);
  await page.getByRole('button', { name: '继续此输入', exact: true }).click();
  await page.getByRole('button', { name: '继续执行', exact: true }).click();
  await expect(page.locator('.outcome-completed')).toBeVisible();
  expect(controls[0].action).toBe('continue'); expect(controls[0].input_id).toBeTruthy();
  await testAction(request, 'resync', { id }); await waitLive(page); expect(submissions).toBe(1);
  await input(page).fill('【UI 夹具】等待停止'); await input(page).press('Enter');
  await page.getByRole('button', { name: '停止', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('所有待处理输入');
  await page.getByRole('button', { name: '停止并清空队列', exact: true }).click();
  await expect(page.locator('.outcome-aborted')).toBeVisible();
  expect(controls.at(-1).action).toBe('cancel');
  await page.locator('[data-session-id="fixture-history"]').click(); await waitLive(page);
  expect(controls).toHaveLength(2);
});

test('mobile drawers trap focus, long content reflows, QQ is read-only, reduced-motion removes entrances', async ({ page, request }) => {
  test.skip(!connected || !fixture, 'Requires external fixture service.');
  await login(page); const id = await newSession(page);
  await testAction(request, 'long', { id });
  await expect(page.locator('.message-text').last()).toContainText('long-unbroken-string');
  for (const width of [320, 390, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 844 });
    if (width < 1180 && await page.locator('#inspector').isVisible()) await page.getByRole('button', { name: '关闭会话详情', exact: true }).click();
    await noHorizontalOverflow(page);
    const overlaps = await page.evaluate(() => { const message = document.querySelector('.conversation-frame').getBoundingClientRect(); const composer = document.querySelector('.composer-section').getBoundingClientRect(); return message.bottom > composer.top + 1; });
    expect(overlaps).toBeFalsy();
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: '打开会话列表', exact: true }).click();
  await expect(page.locator('#sidebar')).toHaveAttribute('aria-modal', 'true');
  for (let i = 0; i < 18; i++) { await page.keyboard.press('Tab'); expect(await page.evaluate(() => document.querySelector('#sidebar').contains(document.activeElement))).toBeTruthy(); }
  await page.keyboard.press('Escape'); await expect(page.getByRole('button', { name: '打开会话列表', exact: true })).toBeFocused();
  await page.getByRole('button', { name: '会话详情', exact: true }).click();
  await expect(page.locator('#inspector')).toHaveAttribute('aria-modal', 'true');
  await page.getByRole('tab', { name: '概览', exact: true }).focus(); await page.keyboard.press('End');
  await expect(page.getByRole('tab', { name: '配置', exact: true })).toBeFocused();
  for (let i = 0; i < 24; i++) { await page.keyboard.press('Tab'); expect(await page.evaluate(() => document.querySelector('#inspector').contains(document.activeElement))).toBeTruthy(); }
  await page.keyboard.press('Escape'); await expect(page.getByRole('button', { name: '会话详情', exact: true })).toBeFocused();
  await page.emulateMedia({ reducedMotion: 'reduce' });
  expect(await page.locator('.message').last().evaluate((node) => getComputedStyle(node).animationName)).toBe('none');
  await page.getByRole('button', { name: '打开会话列表', exact: true }).click();
  await page.locator('[data-session-id="fixture-qq"]').click(); await waitLive(page);
  await expect(input(page)).toBeDisabled();
  await expect(page.locator('.context-domain')).toContainText('QQ 来源 · 只读');
  await expect(page.getByRole('button', { name: '继续此输入', exact: true })).toHaveCount(0);
});
