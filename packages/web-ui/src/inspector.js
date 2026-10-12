import { el, icon, button, badge, note, statusLabel, statusTone, countLabel, modelLabel, timeLabel } from './dom.js';
import { lastInput, activeInput, pendingInputs, executionIdle } from './state.js';
import { sessionSettingsSchema } from './protocol.js';
import { emptyGeneration, generationEditor, generationError } from './generation.js';

const section = (title, content, extra) => el('section', { class: 'inspector-section' },
  el('div', { class: 'section-heading' }, el('h3', {}, title), extra), content);
const pair = (key, value) => el('div', { class: 'detail-pair' }, el('dt', {}, key), el('dd', {}, value));

export function overviewPanel(state, { onSettings, onCompact, compactBusy, connected = true }) {
  if (!state) return el('div', { class: 'inspector-empty' }, icon('panel', 32), el('h3', {}, '会话的细节，都在这里'), el('p', {}, '选择或新建会话后，可查看模型、运行预算、扩展和协议水位。'));
  const input = activeInput(state);
  const requestBudget = input?.max_requests ?? state.settings.max_requests_per_input;
  const requests = input?.requests_used ?? 0;
  const tokens = input?.tokens_used ?? 0;
  return el('div', { class: 'overview-panel' },
    section('当前会话', el('dl', { class: 'detail-list' },
      pair('会话域', state.session.domain === 'agent' ? 'Agent · 个人会话' : 'QQ · 只读来源'),
      pair('执行状态', badge(statusLabel(state.session.status), statusTone(state.session.status))),
      pair('服务端队列', `${pendingInputs(state).length} 条待处理`),
      pair('配置修订', el('code', {}, `r${state.configRevision}`)))),
    section('模型与思考', el('div', { class: 'model-card' }, el('span', { class: 'model-symbol' }, icon('cube', 20)),
      el('div', {}, el('strong', {}, modelLabel(state.settings.model_id)), el('span', {}, `思考级别 · ${state.settings.thinking_level}`))),
    button('查看配置', { variant: 'ghost', className: 'small-button', icon: 'settings', onClick: onSettings })),
    section('本次逻辑输入', el('div', { class: 'budget-card' },
      el('div', { class: 'budget-label' }, el('span', {}, '请求预算'), el('strong', {}, `${requests} / ${requestBudget}`)),
      el('progress', { max: requestBudget || 1, value: requests, 'aria-label': '已使用请求预算' }),
      el('dl', { class: 'detail-list compact-details' }, pair('累计 tokens', countLabel(tokens)),
        pair('Token 上限', countLabel(state.settings.max_tokens_per_input)),
        pair('每次运行轮次', state.settings.max_turns_per_run), pair('运行超时', `${state.settings.run_timeout_seconds} 秒`)),
      el('p', { class: 'field-hint' }, input ? '请求用量由服务端报告；Token、轮次与超时展示当前保存配置，运行中配置保持不变。' : '提交输入后，显示服务端报告的真实用量。'))),
    section('扩展', el('div', { class: 'extension-list' }, state.extensions.length ? state.extensions.map((extension) =>
      el('details', { class: 'extension-card' }, el('summary', {}, icon('cube', 16), el('span', {}, extension.id),
        badge(extension.enabled ? '已启用' : '未启用', extension.enabled ? 'success' : 'neutral')),
      el('div', { class: 'extension-content' }, el('p', {}, `版本 ${extension.version} · ${extension.scope}`),
        el('p', {}, extension.capabilities.length ? `能力：${extension.capabilities.join(' · ')}` : '未声明额外能力'))))
      : el('div', { class: 'quiet-empty' }, icon('cube', 20), el('p', {}, '尚无已注册扩展'), el('span', {}, '扩展由宿主加载，不从浏览器安装。'))), el('span', { class: 'count-pill' }, state.extensions.length)),
    input && section('最近输入', el('div', { class: 'input-record' }, badge(statusLabel(input.status), statusTone(input.status)),
      el('code', { class: 'identifier' }, input.id), el('p', { class: 'field-hint' }, `${timeLabel(input.created_at)} · ${input.kind}`),
      input.error && el('p', { class: 'error-text' }, input.error))),
    el('details', { class: 'protocol-details' }, el('summary', {}, icon('link', 14), '同步与能力', icon('down', 14)),
      el('dl', { class: 'detail-list' }, pair('事件水位', el('code', { id: 'inspector-seq' }, `seq ${state.seq}`)), pair('Epoch', el('code', {}, state.epoch)),
        pair('会话 ID', el('code', {}, state.sessionId))),
      el('div', { class: 'capability-tags' }, state.capabilities.map((capability) => el('span', {}, capability)))),
    state.session.domain === 'agent' && state.capabilities.includes('compact') && section('上下文维护',
      el('div', {}, el('p', { class: 'field-hint' }, '仅空闲时可压缩；由服务端注入的策略执行。'),
        button(compactBusy ? '正在提交…' : '压缩上下文', { disabled: compactBusy || !executionIdle(state) || !connected, icon: 'cube', onClick: onCompact }))));
}

function checkOption(label, checked, disabled, onChange, description) {
  const input = el('input', { type: 'checkbox', checked, disabled, onChange: (event) => onChange(event.target.checked) });
  return el('label', { class: 'check-option' }, input,
    el('span', {}, el('strong', {}, label), description && el('small', {}, description)));
}

export function settingsPanel(state, draft, catalog, { catalogError, catalogLoading, onCatalog, onChange, onSave, onReload, canConfigure }) {
  if (!state || !draft) return el('div', { class: 'inspector-empty' }, icon('settings', 32), el('h3', {}, '先选择一个会话'), el('p', {}, '配置始终保存于服务端，按修订号更新。'));
  const canEdit = canConfigure && Boolean(catalog) && !draft.saving;
  const settings = draft.settings;
  const form = el('form', { class: 'settings-form', 'aria-label': '会话配置' });
  const change = (key, value) => { onChange(key, value); refreshSaveState(); };
  const options = (catalog?.models ?? []).map((model) => ({ id: model.id ?? model.model_id, name: model.id === 'demo' || model.model_id === 'demo' ? modelLabel('demo') : model.name ?? model.label ?? model.id ?? model.model_id })).filter((model) => model.id);
  if (!options.some((model) => model.id === settings.model_id)) options.unshift({ id: settings.model_id, name: `${modelLabel(settings.model_id)} · 当前目录不可用` });
  const model = el('select', { id: 'setting-model', disabled: !canEdit, onChange: (event) => change('model_id', event.target.value) },
    options.map((item) => el('option', { value: item.id, selected: item.id === settings.model_id }, item.name)));
  const thinking = el('input', {
    id: 'setting-thinking', value: settings.thinking_level, required: true, maxlength: sessionSettingsSchema.properties.thinking_level.maxLength, list: 'thinking-levels',
    disabled: !canEdit, spellcheck: 'false', onInput: (event) => change('thinking_level', event.target.value),
  });
  form.append(...[note('保存后仅影响下一次运行，当前执行使用原有配置。'),
    !canConfigure && note('当前身份或会话未授予配置权限，只读展示。', 'warning'),
    catalogError && note(catalogError, 'error', button('重新加载配置目录', { icon: 'retry', onClick: onCatalog })),
    catalogLoading && el('p', { class: 'field-hint', role: 'status' }, '正在加载宿主配置目录…'),
    section('模型与思考', el('div', { class: 'field-stack' },
      el('div', { class: 'field' }, el('label', { for: 'setting-model' }, '模型'), model),
      el('div', { class: 'field' }, el('label', { for: 'setting-thinking' }, '思考级别'), thinking,
        el('datalist', { id: 'thinking-levels' }, ['off', 'low', 'medium', 'high'].map((value) => el('option', { value }))),
        el('p', { class: 'field-hint' }, 'off / low / medium / high；仅支持的模型可启用，由服务端校验。不展示内部推理。'))), el('code', { class: 'revision-label' }, `r${draft.revision}`)),
    section('模型生成参数', generationEditor({ value: settings.generation, prefix: 'session-generation', disabled: !canEdit,
      onChange: (value) => change('generation', value) }).element)].filter(Boolean));
  const numericFields = [
    ['max_turns_per_run', '每次运行轮次', '轮'], ['max_requests_per_input', '单输入请求上限', '次'],
    ['max_tokens_per_input', '单输入 token 上限', 'tokens'], ['run_timeout_seconds', '单次运行超时', '秒'],
  ];
  form.append(section('执行预算', el('div', { class: 'budget-fields' }, numericFields.map(([key, label, unit]) => {
    const schema = sessionSettingsSchema.properties[key];
    return el('div', { class: 'field' }, el('label', { for: `setting-${key}` }, label),
      el('div', { class: 'unit-input' }, el('input', {
        id: `setting-${key}`, type: 'number', step: 1, min: schema.minimum, max: schema.maximum,
        value: settings[key], required: true, disabled: !canEdit,
        onInput: (event) => change(key, event.target.value === '' ? null : Number(event.target.value)),
      }), el('span', {}, unit)));
  }))));

  const approvalInputs = new Map();
  const tools = [...new Set([...(catalog?.tools ?? []), ...settings.allowed_tools, ...settings.approval_tools])];
  form.append(section('工具权限', el('div', { class: 'tool-settings' },
    tools.length ? tools.map((name) => {
      const allowed = settings.allowed_tools.includes(name);
      const registered = Boolean(catalog?.tools.includes(name));
      const approval = checkOption('每次需要审批', settings.approval_tools.includes(name), !canEdit || !allowed || !registered,
        (checked) => change('approval_tools', checked ? [...draft.settings.approval_tools, name] : draft.settings.approval_tools.filter((tool) => tool !== name)));
      approval.classList.add('approval-option');
      approvalInputs.set(name, approval.querySelector('input'));
      return el('div', { class: 'tool-setting' }, checkOption(name, allowed, !canEdit, (checked) => {
        change('allowed_tools', checked ? [...draft.settings.allowed_tools, name] : draft.settings.allowed_tools.filter((tool) => tool !== name));
        if (!checked) change('approval_tools', draft.settings.approval_tools.filter((tool) => tool !== name));
        const input = approvalInputs.get(name);
        input.disabled = !checked || !canEdit;
        input.checked = draft.settings.approval_tools.includes(name);
      }, registered ? null : '不在当前宿主目录中'), approval);
    }) : el('p', { class: 'field-hint' }, '宿主未注册可用工具。'))));
  const extensions = (catalog?.extensions ?? []).filter((extension) => !extension.scope || extension.scope === 'session').map((extension) => ({ ...extension, id: extension.id ?? extension.name })).filter((extension) => extension.id);
  for (const id of settings.enabled_extensions) if (!extensions.some((extension) => extension.id === id)) extensions.push({ id, unregistered: true });
  form.append(section('扩展启用', el('div', { class: 'field-stack' }, extensions.length ? extensions.map((extension) => checkOption(
    extension.id, settings.enabled_extensions.includes(extension.id), !canEdit,
    (checked) => change('enabled_extensions', checked ? [...draft.settings.enabled_extensions, extension.id] : draft.settings.enabled_extensions.filter((id) => id !== extension.id)),
    extension.unregistered ? '不在当前宿主目录中' : `v${extension.version ?? '—'}${Array.isArray(extension.capabilities) && extension.capabilities.length ? ` · ${extension.capabilities.join(' / ')}` : ''}`,
  )) : el('p', { class: 'field-hint' }, '暂无可启用扩展；只能使用宿主登记的能力。'))));
  const validation = el('p', { class: 'error-text', role: 'status' });
  const save = button(draft.saving ? '正在保存…' : '保存会话配置', { type: 'submit', variant: 'primary', icon: 'check' });
  const footer = el('div', { class: 'settings-footer' }, validation,
    draft.error && note(draft.error.kind === 'conflict' ? '修订冲突：配置已在其他位置更新。请重新载入后编辑，不会自动覆盖。' : draft.error.message, 'error',
      button('重新载入服务端配置', { icon: 'retry', onClick: onReload })),
    draft.saved && !draft.dirty && note(`已保存 r${draft.revision}，下次运行生效。`, 'success'), save,
    button('重新载入', { variant: 'ghost', disabled: draft.saving, onClick: onReload }));
  function refreshSaveState() {
    const inconsistent = draft.settings.max_turns_per_run > draft.settings.max_requests_per_input;
    const generationIssue = generationError(draft.settings.generation ?? emptyGeneration());
    validation.textContent = inconsistent ? '每次运行轮次不能超过单输入请求上限。' : generationIssue;
    if (draft.dirty) footer.querySelector('.notice-success')?.remove();
    save.disabled = !canEdit || !draft.dirty || draft.saving || inconsistent || Boolean(generationIssue) || ['conflict', 'uncertain'].includes(draft.error?.kind);
  }
  refreshSaveState();
  form.append(footer);
  form.addEventListener('submit', (event) => { event.preventDefault(); if (!save.disabled && form.reportValidity()) onSave(); });
  return form;
}
