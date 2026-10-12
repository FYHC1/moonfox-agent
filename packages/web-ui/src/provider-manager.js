import { el, icon, iconButton, button, badge, note, confirmAction, announce } from './dom.js';
import { providerSettingsSchema, modelSettingsSchema, isValid } from './protocol.js';
import { emptyGeneration, generationEditor, generationError } from './generation.js';

const protocols = { openai: 'OpenAI-compatible · Chat Completions', anthropic: 'Anthropic · Messages' };
const testLabels = { ok: '检测成功', auth_error: '认证失败，请检查密钥或环境变量', unavailable: '端点暂不可用', unsupported: '此端点不支持模型发现，可手工添加', invalid_response: '返回的模型目录无法识别' };
const field = (label, control, hint) => el('div', { class: 'field' }, el('label', { for: control.id }, label), control, hint && el('p', { class: 'field-hint' }, hint));
const check = (label, control) => el('label', { class: 'check-option' }, control, el('span', {}, el('strong', {}, label)));
const revisionBody = (revision) => ({ request_id: crypto.randomUUID(), expected_revision: revision });
const schemaDefaults = (schema) => Object.fromEntries(Object.entries(schema.properties).filter(([, value]) => Object.hasOwn(value, 'default')).map(([key, value]) => [key, structuredClone(value.default)]));
const nonblank = (value) => value.trim() && !/[\x00-\x1f\x7f]/.test(value);

export function providerFormError(settings, { storage, original, replacing = false, clearing = false } = {}) {
  if (!isValid('ProviderSettings', settings) || !nonblank(settings.name)) return '请检查提供商名称、协议、超时与认证字段。';
  try {
    const url = new URL(settings.base_url);
    const local = url.hostname === 'localhost' || url.hostname === '[::1]' || /^127(?:\.\d{1,3}){3}$/.test(url.hostname);
    if (!['https:', 'http:'].includes(url.protocol) || (url.protocol === 'http:' && !local) || url.username || url.password || url.search || url.hash || /[\\\s?#]/.test(settings.base_url)) throw Error();
  } catch { return '请使用不含凭据、查询参数或片段的 HTTPS 基础地址；HTTP 仅限本机回环地址。'; }
  if (/\/(chat\/completions|messages|models)\/?$/.test(settings.base_url)) return '请填写 API 基础地址，不要填写 /chat/completions、/messages 或 /models。';
  if (settings.auth_mode === 'env' && !/^[A-Z][A-Z0-9_]*$/.test(settings.env_var ?? '')) return '环境变量名须以大写字母开头，仅含大写字母、数字和下划线。';
  if (settings.auth_mode === 'stored' && storage === 'env_only') return '此宿主不能保存密钥，请选择环境变量或无认证。';
  if (original?.credential_source === 'stored' && original.key_configured && !replacing && !clearing
    && (settings.protocol !== original.settings.protocol || settings.base_url.replace(/\/+$/, '') !== original.settings.base_url.replace(/\/+$/, ''))) return '更改协议或地址时，必须重新输入密钥或明确清除旧密钥。';
  return '';
}

export function modelFormError(settings, protocol) {
  if (!isValid('ModelSettings', settings) || !nonblank(settings.name) || !nonblank(settings.remote_id)) return '请检查模型字段与数值范围。';
  if (settings.max_output_tokens >= settings.context_window) return '最大输出 tokens 必须小于上下文窗口。';
  const parameters = settings.parameters ?? emptyGeneration();
  if (parameters.max_tokens != null && parameters.max_tokens > settings.max_output_tokens) return '生成默认值 max_tokens 不能超过模型最大输出 tokens。';
  if (protocol === 'anthropic' && (settings.supports_reasoning || (settings.token_field ?? 'max_tokens') !== 'max_tokens')) return 'Anthropic 暂不支持扩展思考或其他 token 字段。';
  return generationError(parameters, protocol);
}

export function managementError(error) {
  if (error.status === 409) return '修订冲突或资源仍被引用。请重新加载目录核对；不会自动覆盖，也不会静默改绑会话。';
  if (error.status === 422) return '提交被拒绝。请检查字段、协议限制与认证方式；更改地址或协议时须重新输入或清除旧密钥。';
  if (error.status === 403) return '仅管理员可管理提供商与模型。';
  if (error.status === 404) return '资源已不存在，请重新加载目录。';
  if (error.kind === 'protocol') return '服务端协议不匹配，请核对前后端版本并重新加载。';
  return '未能确认操作结果，请检查连接并重新加载目录核对。不会自动重试或发起模型调用。';
}

export class ProviderManager {
  constructor({ api, onChanged, onClose, onUnauthorized }) {
    Object.assign(this, { api, onChanged, onClose, onUnauthorized });
    this.abort = new AbortController();
    this.tab = 'providers'; this.tests = new Map(); this.busy = false; this.stale = false; this.closed = false;
  }

  open() {
    this.returnFocus = document.activeElement;
    this.feedback = el('div', { class: 'management-feedback', 'aria-live': 'polite' });
    this.content = el('div', { class: 'management-content', id: 'management-content', role: 'tabpanel', tabindex: 0 });
    this.tabs = ['providers', 'models'].map((tab) => button(tab === 'providers' ? '提供商' : '模型', {
      variant: 'ghost', role: 'tab', id: `management-${tab}-tab`, 'aria-controls': 'management-content',
      onClick: () => void this.switchTab(tab),
    }));
    const tablist = el('div', { class: 'management-tabs', role: 'tablist', 'aria-label': '提供商与模型视图', onKeydown: async (event) => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key) || this.busy) return;
      event.preventDefault();
      await this.switchTab(event.key === 'Home' ? 'providers' : event.key === 'End' ? 'models' : this.tab === 'providers' ? 'models' : 'providers');
      this.tabs[this.tab === 'providers' ? 0 : 1].focus();
    } }, this.tabs);
    this.closeButton = iconButton('关闭提供商与模型', 'close', { onClick: () => void this.close() });
    this.controls = el('fieldset', { class: 'management-controls' },
      el('legend', { class: 'sr-only' }, '管理员模型目录'),
      el('div', { class: 'management-nav' }, tablist, button('重新加载目录', { icon: 'retry', variant: 'ghost', onClick: () => void this.reload() })),
      this.feedback, this.content);
    this.dialog = el('dialog', { class: 'management-dialog', 'aria-modal': 'true', 'aria-labelledby': 'management-title', 'aria-describedby': 'management-description' },
      el('header', { class: 'management-heading' }, el('div', {}, el('p', { class: 'eyebrow' }, 'WORKSPACE / ADMIN'), el('h2', { id: 'management-title' }, '提供商与模型')), this.closeButton),
      el('p', { id: 'management-description', class: 'management-description' }, '统一管理多提供商接入与模型默认值，供会话及其他入口共用。配置保存在宿主；更改仅影响后续运行，不切换当前会话。'), this.controls);
    this.dialog.addEventListener('cancel', (event) => { event.preventDefault(); void this.close(); });
    this.dialog.addEventListener('keydown', (event) => {
      if (event.key !== 'Tab') return;
      const nodes = [...this.dialog.querySelectorAll('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex="0"]')]
        .filter((node) => node.tabIndex >= 0 && node.getClientRects().length && !node.closest('[hidden]'));
      const first = nodes[0]; const last = nodes.at(-1);
      if (event.shiftKey && (document.activeElement === first || !this.dialog.contains(document.activeElement))) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && (document.activeElement === last || !this.dialog.contains(document.activeElement))) { event.preventDefault(); first?.focus(); }
    });
    document.body.append(this.dialog); this.render(); this.dialog.showModal(); void this.load();
  }

  clearSecrets() { this.dialog?.querySelectorAll('input[type="password"]').forEach((input) => { input.value = ''; }); }
  setBusy(value) {
    this.busy = value; this.controls.disabled = value; this.closeButton.disabled = value;
    this.content.setAttribute('aria-busy', String(value));
  }
  async discard() {
    if (this.confirming) return false;
    if (!this.dirty) return true;
    this.confirming = true;
    try { return await confirmAction({ title: '放弃未保存的管理编辑？', description: '当前表单的编辑将被放弃，密钥输入会清空。已保存的目录不受影响。', confirmLabel: '放弃编辑' }); }
    finally { this.confirming = false; }
  }
  async close() {
    if (this.busy || this.closed || !await this.discard() || this.closed) return;
    this.dispose(); this.onClose?.();
    if (this.returnFocus?.isConnected && !this.returnFocus.closest('[hidden]')) this.returnFocus.focus();
  }
  dispose() {
    this.closed = true; this.abort.abort(); this.clearSecrets(); this.dialog?.close(); this.dialog?.remove();
    this.tests.clear(); this.editor = null;
  }
  async switchTab(tab) {
    if (this.busy || this.closed || tab === this.tab || !await this.discard() || this.closed) return;
    this.tab = tab; this.editor = null; this.dirty = false; this.render();
  }
  async edit(kind, original = null, prefill = {}) {
    if (this.busy || this.stale || this.closed || !await this.discard() || this.closed) return;
    this.tab = kind; this.editor = { kind, original, prefill }; this.dirty = false; this.feedback.replaceChildren(); this.render();
    this.content.querySelector('input:not([readonly])')?.focus();
  }
  async back() {
    if (this.busy || !await this.discard() || this.closed) return;
    this.editor = null; this.dirty = false; this.render();
  }
  async reload() {
    if (this.busy || !await this.discard() || this.closed) return;
    this.clearSecrets(); this.editor = null; this.dirty = false; this.render(); await this.load();
  }
  async fetchCatalogs() {
    const [providers, models] = await Promise.all([this.api.providers(this.abort.signal), this.api.models(this.abort.signal)]);
    if (this.closed) return;
    this.providers = providers; this.models = models; this.stale = false;
  }
  async load() {
    if (this.busy || this.closed) return;
    this.setBusy(true); this.feedback.replaceChildren(note('正在读取提供商与模型目录…'));
    try {
      await this.fetchCatalogs();
      if (this.closed) return;
      this.tests.clear(); this.render(); this.feedback.replaceChildren();
    } catch (error) { this.showError(error); }
    finally { if (!this.closed) this.setBusy(false); }
  }
  showError(error) {
    if (this.closed || this.onUnauthorized?.(error)) return;
    if ([0, 404, 409].includes(error.status) || error.status >= 500 || error.kind === 'protocol') this.stale = true;
    this.feedback.replaceChildren(note(managementError(error), 'error', button('重新加载目录核对', { icon: 'retry', onClick: () => void this.reload() })));
    if (this.stale) this.content.querySelectorAll('[data-write]').forEach((node) => { node.disabled = true; });
  }
  async perform(action, { message = '目录已保存；后续运行使用新配置。', testResult } = {}) {
    if (this.busy || this.stale || this.closed) return;
    this.setBusy(true); this.feedback.replaceChildren(note(testResult ? '正在检测已保存的提供商；不发送会话历史…' : '正在保存目录…'));
    try {
      const result = await action();
      if (this.closed) return;
      if (testResult) testResult(result);
      else {
        this.editor = null; this.dirty = false; this.tests.clear(); this.stale = true;
        void this.onChanged?.();
        await this.fetchCatalogs();
      }
      if (this.closed) return;
      this.render(); this.feedback.replaceChildren(note(message, 'success')); announce(message);
    } catch (error) { this.showError(error); }
    finally { if (!this.closed) this.setBusy(false); }
  }

  writeButton(label, action, options = {}) {
    return button(label, { variant: 'ghost', className: 'small-button', ...options, disabled: this.stale || options.disabled,
      dataset: { write: 'true' }, onClick: action });
  }
  render() {
    if (this.closed) return;
    this.clearSecrets();
    for (const [index, tab] of ['providers', 'models'].entries()) {
      this.tabs[index].setAttribute('aria-selected', String(this.tab === tab)); this.tabs[index].tabIndex = this.tab === tab ? 0 : -1;
    }
    this.content.setAttribute('aria-labelledby', `management-${this.tab}-tab`);
    this.content.replaceChildren();
    if (!this.providers || !this.models) { this.content.append(el('div', { class: 'management-empty' }, icon('cube', 32), el('p', {}, '加载目录后即可配置接入，无需先创建会话。'))); return; }
    if (this.editor) {
      this.content.append(this.editor.kind === 'providers' ? this.providerForm() : this.modelForm());
      return;
    }
    this.content.append(this.tab === 'providers' ? this.providerList() : this.modelList());
  }

  providerList() {
    const storage = { dpapi: '本机密钥由当前 Windows 用户的 DPAPI 保护。', env_only: '此宿主仅支持环境变量或无认证，不能保存密钥。', custom: '本机密钥由宿主配置的保护器管理。' };
    return el('div', { class: 'management-stack' },
      el('div', { class: 'management-toolbar' }, el('div', {}, el('h3', {}, `提供商 · ${this.providers.providers.length}`), el('p', { class: 'field-hint' }, storage[this.providers.secret_storage])),
        this.writeButton('新建提供商', () => void this.edit('providers'), { icon: 'plus', variant: 'primary' })),
      el('p', { class: 'field-hint' }, '保存不会自动访问远端。仅点击「检测 / 获取模型」请求已保存端点的 /models，不发起计费生成或发送会话历史。'),
      this.providers.providers.length ? el('div', { class: 'management-list' }, this.providers.providers.map((provider) => {
        const settings = provider.settings;
        const key = settings.auth_mode === 'none' ? '无认证' : provider.key_configured ? provider.credential_source === 'environment' ? '环境变量可用' : '本机密钥已配置' : '缺少可用凭据';
        return el('article', { class: 'management-card', dataset: { providerId: provider.id } },
          el('div', { class: 'management-card-heading' }, el('h4', {}, settings.name), badge(settings.enabled ? '已启用' : '已停用', settings.enabled ? 'success' : 'neutral')),
          el('code', { class: 'management-id' }, provider.id), el('p', { class: 'management-protocol' }, protocols[settings.protocol]),
          el('code', { class: 'management-endpoint' }, settings.base_url),
          el('div', { class: 'management-meta' }, badge(key, provider.key_configured ? 'success' : 'warning'), el('span', {}, `${settings.timeout_seconds} 秒 · r${provider.revision}`)),
          el('div', { class: 'inline-actions' },
            this.writeButton('编辑', () => void this.edit('providers', provider), { icon: 'settings' }),
            this.writeButton(settings.enabled ? '停用' : '启用', () => void this.perform(() => this.api.putProvider(provider.id, { ...revisionBody(provider.revision), settings: { ...settings, enabled: !settings.enabled }, secret_action: 'keep' }, this.abort.signal))),
            this.writeButton('检测 / 获取模型', () => void this.test(provider), { icon: 'link' }),
            this.writeButton('删除', () => void this.remove('providers', provider), { variant: 'danger' })),
          this.testView(provider));
      })) : el('div', { class: 'management-empty' }, icon('globe', 32), el('h3', {}, '还没有提供商'), el('p', {}, '先添加 API 接入，再登记可供所有会话选择的模型。客户端不预置任何凭据。')));
  }
  test(provider) {
    return this.perform(() => this.api.testProvider(provider.id, revisionBody(provider.revision), this.abort.signal), {
      message: '检测已完成。发现模型不代表已经验证其能力，请核对规格后添加。',
      testResult: (result) => this.tests.set(provider.id, { revision: provider.revision, status: result.status, latency_ms: result.latency_ms, models: result.models }),
    });
  }
  testView(provider) {
    const result = this.tests.get(provider.id);
    if (!result || result.revision !== provider.revision) return null;
    return el('section', { class: 'provider-test-result', 'aria-label': `${provider.settings.name} 检测结果` },
      note(`${testLabels[result.status]} · ${result.latency_ms} ms · 已保存 r${result.revision}`, result.status === 'ok' ? 'success' : 'warning'),
      result.models.length ? el('ul', { class: 'discovered-models' }, result.models.map((model) => el('li', {},
        el('div', {}, el('strong', {}, model.name), el('code', {}, model.id)),
        this.writeButton('添加模型', () => void this.edit('models', null, { provider_id: provider.id, remote_id: model.id, name: model.name }), { icon: 'plus' }))))
        : el('p', { class: 'field-hint' }, '没有可展示的发现结果；仍可在模型页手工录入。'));
  }
  async remove(kind, item) {
    if (this.busy || this.stale || this.closed || this.confirming) return;
    this.confirming = true;
    let confirmed;
    try { confirmed = await confirmAction({ title: kind === 'providers' ? '删除此提供商？' : '删除此模型？', description: `将按 r${item.revision} 删除「${item.settings.name}」。如仍被模型、会话或默认配置引用，服务端会拒绝，不会修改历史或自动改绑。`, confirmLabel: '确认删除', danger: true }); }
    finally { this.confirming = false; }
    if (!confirmed || this.closed) return;
    await this.perform(() => kind === 'providers' ? this.api.deleteProvider(item.id, revisionBody(item.revision), this.abort.signal) : this.api.deleteModel(item.id, revisionBody(item.revision), this.abort.signal), { message: '已删除目录项。' });
  }
  unavailable(model) {
    if (model.available) return '';
    const provider = this.providers.providers.find((item) => item.id === model.settings.provider_id);
    if (!model.settings.enabled) return '模型已停用';
    if (!provider) return '提供商不存在';
    if (!provider.settings.enabled) return '提供商已停用';
    if (provider.settings.auth_mode !== 'none' && !provider.key_configured) return '提供商缺少可用密钥或环境变量';
    return '服务端判定不可用，请检查提供商与模型配置';
  }
  defaultButton(id, available = true) {
    return this.writeButton(this.models.default_model_id === id ? '默认模型' : '设为默认', () => void this.perform(() => this.api.setDefaultModel(id, revisionBody(this.models.config_revision), this.abort.signal), { message: '默认模型已更新，仅新建会话采用；现有会话不变。' }), { disabled: !available || this.models.default_model_id === id });
  }
  modelList() {
    return el('div', { class: 'management-stack' },
      el('div', { class: 'management-toolbar' }, el('div', {}, el('h3', {}, `模型 · ${this.models.models.length}`), el('p', { class: 'field-hint' }, `默认模型 ${this.models.default_model_id} · 全局修订 r${this.models.config_revision}。更改默认仅影响新会话。`)),
        this.writeButton('新建模型', () => void this.edit('models'), { icon: 'plus', variant: 'primary', disabled: !this.providers.providers.length })),
      !this.providers.providers.length && note('请先在提供商页添加一个接入，再创建托管模型。'),
      this.models.models.length ? el('div', { class: 'management-list' }, this.models.models.map((model) => {
        const settings = model.settings;
        return el('article', { class: 'management-card', dataset: { modelId: model.id } },
          el('div', { class: 'management-card-heading' }, el('h4', {}, settings.name), badge(model.available ? '可用' : '不可用', model.available ? 'success' : 'warning')),
          el('code', { class: 'management-id' }, model.id),
          el('p', { class: 'management-protocol' }, `${settings.provider_id} / ${settings.remote_id}`),
          el('p', { class: 'field-hint' }, `上下文 ${settings.context_window.toLocaleString()} · 最大输出 ${settings.max_output_tokens.toLocaleString()} · r${model.revision}`),
          el('p', { class: 'field-hint' }, `能力：${[settings.supports_tools && '工具', settings.supports_images && '图片', settings.supports_reasoning && '思考'].filter(Boolean).join(' / ') || '文本'} · ${settings.enabled ? '已启用' : '已停用'}`),
          !model.available && el('p', { class: 'error-text' }, this.unavailable(model)),
          el('div', { class: 'inline-actions' }, this.writeButton('编辑', () => void this.edit('models', model), { icon: 'settings' }),
            this.writeButton(settings.enabled ? '停用' : '启用', () => void this.perform(() => this.api.putModel(model.id, { ...revisionBody(model.revision), settings: { ...settings, enabled: !settings.enabled } }, this.abort.signal))),
            this.defaultButton(model.id, model.available), this.writeButton('删除', () => void this.remove('models', model), { variant: 'danger' })));
      })) : el('div', { class: 'management-empty' }, icon('cube', 32), el('h3', {}, '还没有托管模型'), el('p', {}, '手工填写远端模型 ID，或从提供商检测结果添加。')),
      el('h3', {}, '内置模型 · 只读'),
      el('div', { class: 'management-list' }, this.models.builtin_models.map((model) => el('article', { class: 'management-card', dataset: { builtinModelId: model.id } },
        el('div', { class: 'management-card-heading' }, el('h4', {}, model.label), badge('内置 · 只读')),
        el('p', { class: 'field-hint' }, `${model.id} · ${model.provider}`), this.defaultButton(model.id)))));
  }

  formShell(title, revision) {
    const form = el('form', { class: 'management-form', 'aria-label': title, autocomplete: 'off' });
    form.addEventListener('input', () => { this.dirty = true; });
    form.addEventListener('change', () => { this.dirty = true; });
    form.append(el('div', { class: 'management-toolbar' }, el('div', {}, el('h3', {}, title), el('p', { class: 'field-hint' }, revision ? `当前 r${revision} · 按修订保存，不自动覆盖` : '新建 · expected_revision = 0')), button('返回列表', { variant: 'ghost', onClick: () => void this.back() })));
    return form;
  }
  identifier(kind, original) {
    return el('input', { id: `${kind}-id`, value: original?.id ?? '', required: true, readOnly: Boolean(original), pattern: modelSettingsSchema.properties.provider_id.pattern.replace('_-', '_\\-'), maxlength: 64, spellcheck: 'false', autocapitalize: 'none' });
  }
  providerForm() {
    const { original } = this.editor;
    const settings = original?.settings ?? { ...schemaDefaults(providerSettingsSchema), name: '', base_url: '', auth_mode: this.providers.secret_storage === 'env_only' ? 'env' : 'stored' };
    const form = this.formShell(original ? '编辑提供商' : '新建提供商', original?.revision);
    const id = this.identifier('provider', original);
    const name = el('input', { id: 'provider-name', value: settings.name, required: true, maxlength: providerSettingsSchema.properties.name.maxLength });
    const protocol = el('select', { id: 'provider-protocol' }, Object.entries(protocols).map(([value, label]) => el('option', { value, selected: value === settings.protocol }, label)));
    const base = el('input', { id: 'provider-base-url', type: 'url', value: settings.base_url, required: true, maxlength: providerSettingsSchema.properties.base_url.maxLength, spellcheck: 'false' });
    const timeoutSchema = providerSettingsSchema.properties.timeout_seconds;
    const timeout = el('input', { id: 'provider-timeout', type: 'number', min: timeoutSchema.minimum, max: timeoutSchema.maximum, step: 1, required: true, value: settings.timeout_seconds });
    const enabled = el('input', { type: 'checkbox', checked: settings.enabled });
    const envOnly = this.providers.secret_storage === 'env_only';
    const auth = el('select', { id: 'provider-auth' }, [['stored', envOnly ? '本机存储（宿主不支持）' : '本机安全存储'], ['env', '环境变量引用'], ['none', '无认证']].map(([value, label]) => el('option', { value, selected: value === settings.auth_mode, disabled: value === 'stored' && envOnly }, label)));
    const env = el('input', { id: 'provider-env-var', value: settings.env_var ?? '', maxlength: 100, pattern: '[A-Z][A-Z0-9_]*', placeholder: 'MY_PROVIDER_API_KEY', spellcheck: 'false' });
    // The only place a key is kept before submission is this password control; never a draft or instance property.
    const key = el('input', { id: 'provider-api-key', type: 'password', value: '', autocomplete: 'new-password', maxlength: 4096, spellcheck: 'false', autocapitalize: 'none', placeholder: original ? '留空保留现有密钥' : '可稍后配置' });
    const clear = el('input', { type: 'checkbox', id: 'provider-clear-key' });
    const authHint = el('div');
    const endpointHint = el('p', { class: 'field-hint', id: 'provider-endpoint-hint' });
    base.setAttribute('aria-describedby', 'provider-endpoint-hint');
    const refresh = () => {
      base.placeholder = protocol.value === 'openai' ? 'https://api.openai.com/v1' : 'https://api.anthropic.com/v1';
      endpointHint.textContent = `基础地址示例：${base.placeholder}。不要追加具体请求路径；HTTP 仅允许 localhost / 回环 IP。`;
      env.disabled = auth.value !== 'env'; env.required = auth.value === 'env';
      key.disabled = envOnly || auth.value !== 'stored' || clear.checked;
      authHint.replaceChildren(auth.value === 'none' ? note('无认证不会发送任何 API 密钥，仅适合可信本机端点或明确无需认证的服务。', 'warning')
        : auth.value === 'env' ? note('仅保存变量名，由宿主进程读取环境变量；不要在这里填写密钥内容。')
          : note(envOnly ? '此宿主不能替换或保存密钥，请使用环境变量或无认证。' : '密钥只写不读，返回目录仅含配置状态；输入提交后立即清空，包括保存失败。'));
    };
    auth.addEventListener('change', () => { key.value = ''; refresh(); });
    clear.addEventListener('change', () => { key.value = ''; refresh(); });
    protocol.addEventListener('change', refresh); refresh();
    form.append(el('div', { class: 'management-form-grid' },
      field('提供商 ID', id, '小写字母开头，最多 64 位；创建后不可修改。'), field('提供商名称', name),
      field('协议', protocol), field('超时（秒）', timeout),
      el('div', { class: 'management-wide field' }, el('label', { for: base.id }, 'API 基础地址'), base, endpointHint),
      field('认证方式', auth), field('环境变量名', env),
      el('div', { class: 'management-wide field' }, authHint, field('API 密钥（只写）', key, '空白 = keep；填写 = replace；勾选下方清除 = clear。密钥不会写入浏览器存储或编辑历史。'), check('清除已保存的密钥（保存时生效）', clear)),
      check('启用此提供商', enabled)));
    if (original?.key_configured) form.append(note('已有凭据：更改协议或 API 地址时，若使用已存储密钥，必须重新输入密钥或明确清除旧密钥。', 'warning'));
    const validation = el('p', { class: 'error-text', role: 'alert' });
    const save = this.writeButton('保存提供商', null, { type: 'submit', variant: 'primary', icon: 'check' });
    form.append(validation, el('div', { class: 'dialog-actions' }, button('取消编辑', { onClick: () => void this.back() }), save));
    form.addEventListener('submit', (event) => {
      event.preventDefault(); if (this.busy || this.stale || !form.reportValidity()) return;
      const next = { name: name.value.trim(), protocol: protocol.value, base_url: base.value.trim(), timeout_seconds: Number(timeout.value), enabled: enabled.checked, auth_mode: auth.value, env_var: auth.value === 'env' ? env.value.trim() : null };
      const replacing = !key.disabled && Boolean(key.value.trim());
      validation.textContent = providerFormError(next, { storage: this.providers.secret_storage, original, replacing, clearing: clear.checked });
      if (validation.textContent) return;
      const body = { ...revisionBody(original?.revision ?? 0), settings: next, secret_action: clear.checked ? 'clear' : replacing ? 'replace' : 'keep' };
      if (replacing) body.api_key = key.value;
      key.value = ''; // Clear synchronously, before even waiting for the HTTP request (also on rejection).
      void this.perform(async () => {
        try { return await this.api.putProvider(id.value, body, this.abort.signal); }
        finally { delete body.api_key; }
      });
    });
    return form;
  }

  modelForm() {
    const { original, prefill } = this.editor;
    const settings = original?.settings ?? { ...schemaDefaults(modelSettingsSchema), provider_id: this.providers.providers[0]?.id ?? '', name: '', remote_id: '', supports_tools: false, supports_images: false, supports_reasoning: false, parameters: emptyGeneration(), ...prefill };
    const form = this.formShell(original ? '编辑模型' : '新建模型', original?.revision);
    const id = this.identifier('model', original);
    const name = el('input', { id: 'model-name', value: settings.name, required: true, maxlength: modelSettingsSchema.properties.name.maxLength });
    const remote = el('input', { id: 'model-remote-id', value: settings.remote_id, required: true, maxlength: modelSettingsSchema.properties.remote_id.maxLength, spellcheck: 'false' });
    const providers = [...this.providers.providers];
    if (!providers.some((item) => item.id === settings.provider_id) && settings.provider_id) providers.push({ id: settings.provider_id, settings: { name: `${settings.provider_id}（已不存在）`, enabled: false } });
    const provider = el('select', { id: 'model-provider', required: true }, providers.map((item) => el('option', { value: item.id, selected: item.id === settings.provider_id }, `${item.settings.name}${item.settings.enabled ? '' : ' · 已停用'}`)));
    const numeric = (key, controlId) => el('input', { id: controlId, type: 'number', step: 1, required: true, min: modelSettingsSchema.properties[key].minimum, max: modelSettingsSchema.properties[key].maximum, value: settings[key] });
    const context = numeric('context_window', 'model-context'); const output = numeric('max_output_tokens', 'model-output');
    const enabled = el('input', { type: 'checkbox', checked: settings.enabled });
    const tools = el('input', { type: 'checkbox', checked: settings.supports_tools });
    const images = el('input', { type: 'checkbox', checked: settings.supports_images });
    const reasoning = el('input', { type: 'checkbox', checked: settings.supports_reasoning });
    const tokenField = el('select', { id: 'model-token-field' }, modelSettingsSchema.properties.token_field.enum.map((value) => el('option', { value, selected: value === settings.token_field }, value)));
    const selectedProtocol = () => this.providers.providers.find((item) => item.id === provider.value)?.settings.protocol;
    const parameters = generationEditor({ value: settings.parameters, prefix: 'model-generation', protocol: selectedProtocol(), modelDefaults: true, onChange: () => { this.dirty = true; } });
    const protocolRepair = el('div');
    const syncProtocol = (changed = false) => {
      const anthropic = selectedProtocol() === 'anthropic';
      reasoning.disabled = anthropic; tokenField.disabled = anthropic;
      if (changed) { if (anthropic) { reasoning.checked = false; tokenField.value = 'max_tokens'; } parameters.setProtocol(selectedProtocol()); }
      protocolRepair.replaceChildren();
      if (anthropic && (reasoning.checked || tokenField.value !== 'max_tokens' || ['frequency_penalty', 'presence_penalty', 'seed'].some((key) => parameters.value()[key] != null))) {
        protocolRepair.append(note('当前保存值包含 Anthropic 不支持的字段，可能是提供商协议已更改。请明确清除后保存。', 'warning',
          button('清除不支持的字段', { onClick: () => syncProtocol(true) })));
      }
    };
    provider.addEventListener('change', () => syncProtocol(true)); syncProtocol();
    form.append(note('能力与容量不会从模型名称推断。请核对远端文档后填写；初始容量仅为表单默认值，检测与保存不代表已验证兼容性。'),
      el('div', { class: 'management-form-grid' }, field('模型 ID', id, '本地目录标识，创建后不可修改；不是远端模型 ID。'), field('模型名称', name),
        field('所属提供商', provider), field('远端模型 ID', remote), field('上下文窗口（tokens）', context), field('最大输出 tokens', output),
        field('OpenAI 输出 token 字段', tokenField, 'Chat Completions 选择 max_tokens 或 max_completion_tokens；Anthropic 固定 max_tokens。'),
        el('div', { class: 'field-stack' }, check('启用此模型', enabled), check('支持工具调用', tools), check('支持图片输入', images), check('支持思考级别（仅 OpenAI）', reasoning))),
      protocolRepair,
      el('section', { class: 'management-generation' }, el('h3', {}, '模型生成参数 · 默认值'), parameters.element));
    const validation = el('p', { class: 'error-text', role: 'alert' });
    const save = this.writeButton('保存模型', null, { type: 'submit', variant: 'primary', icon: 'check' });
    form.append(validation, el('div', { class: 'dialog-actions' }, button('取消编辑', { onClick: () => void this.back() }), save));
    form.addEventListener('submit', (event) => {
      event.preventDefault(); if (this.busy || this.stale || !form.reportValidity()) return;
      const next = { provider_id: provider.value, name: name.value.trim(), remote_id: remote.value.trim(), context_window: Number(context.value), max_output_tokens: Number(output.value), enabled: enabled.checked,
        supports_tools: tools.checked, supports_images: images.checked, supports_reasoning: reasoning.checked, token_field: tokenField.value, parameters: parameters.value() };
      validation.textContent = modelFormError(next, selectedProtocol());
      if (validation.textContent) return;
      void this.perform(() => this.api.putModel(id.value, { ...revisionBody(original?.revision ?? 0), settings: next }, this.abort.signal));
    });
    return form;
  }
}
