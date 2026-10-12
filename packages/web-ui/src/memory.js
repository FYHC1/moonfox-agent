import { el, button, badge, note, icon } from './dom.js';
import { ProtocolError } from './protocol.js';

const MEMORY_KINDS = new Set(['fact', 'preference', 'profile']);
const MEMORY_ORIGINS = new Set(['manual', 'tool', 'extraction']);
const JOB_STATUSES = new Set(['pending', 'running', 'done', 'failed', 'unknown', 'cancelled']);
const kindLabels = { fact: '事实候选', preference: '偏好', profile: '个人资料' };
const originLabels = { manual: '手动录入', tool: '工具记录', extraction: '自动抽取 · 尚未证实' };
const jobLabels = { pending: '等待中', running: '运行中', done: '已完成', failed: '失败', unknown: '待核实', cancelled: '已取消' };

export function createMemoryView(sessionId = null, domain = null) {
  return {
    sessionId, domain, status: 'idle', records: [], scopeRevision: null, loadedQuery: '', loadedIncludeForgotten: false,
    query: '', includeForgotten: false, busy: null, error: null, notice: null, requiresRefresh: false, dirtyAfterWrite: false,
    newDraft: { text: '', kind: 'fact', subject: 'conversation' }, editDraft: null,
    jobs: [], jobsStatus: 'idle', jobsError: null,
  };
}

const object = (value) => Boolean(value && typeof value === 'object' && !Array.isArray(value));
const stringValue = (value, allowEmpty = false) => typeof value === 'string' && (allowEmpty || value.length > 0);
const revisionValue = (value, minimum = 0) => Number.isInteger(value) && value >= minimum;
const timestampValue = (value) => typeof value === 'string' || (typeof value === 'number' && Number.isFinite(value));

export function validateMemoryRecord(record) {
  if (!object(record)
    || !stringValue(record.id)
    || !revisionValue(record.revision, 1)
    || !stringValue(record.text)
    || record.text.trim().length < 1 || record.text.length > 2000
    || !MEMORY_KINDS.has(record.kind)
    || !stringValue(record.subject) || record.subject.length > 128
    || !object(record.source) || !MEMORY_ORIGINS.has(record.source.origin)
    || !(record.source.entry_id == null || stringValue(record.source.entry_id, true))
    || typeof record.confidence !== 'number' || !Number.isFinite(record.confidence) || record.confidence < 0 || record.confidence > 1
    || typeof record.active !== 'boolean'
    || !timestampValue(record.created_at) || !timestampValue(record.updated_at)) {
    throw new ProtocolError('MemoryRecord');
  }
  return {
    id: record.id, revision: record.revision, text: record.text, kind: record.kind, subject: record.subject,
    source: { origin: record.source.origin, entry_id: record.source.entry_id ?? null }, confidence: record.confidence,
    active: record.active, created_at: record.created_at, updated_at: record.updated_at,
  };
}

export function parseMemoryResult(command, result) {
  if (!object(result)) throw new ProtocolError(`memory.${command} result`);
  if (command === 'list') {
    if (!Array.isArray(result.records) || !revisionValue(result.revision)) throw new ProtocolError('memory.list result');
    return { records: result.records.map(validateMemoryRecord), revision: result.revision };
  }
  if (['remember', 'update', 'forget', 'get'].includes(command)) return { record: validateMemoryRecord(result.record) };
  if (command === 'delete' || command === 'clear') {
    if (result.deleted !== true || !stringValue(result.notice, true)) throw new ProtocolError(`memory.${command} result`);
    return { deleted: true, notice: result.notice };
  }
  if (command === 'jobs') {
    if (!Array.isArray(result.jobs)) throw new ProtocolError('memory.jobs result');
    const jobs = result.jobs.map((job) => {
      if (!object(job) || !stringValue(job.id) || !JOB_STATUSES.has(job.status)) throw new ProtocolError('memory.jobs item');
      return { id: job.id, status: job.status };
    });
    return { jobs };
  }
  throw new ProtocolError(`memory.${command} command`);
}

function section(title, content, extra) {
  return el('section', { class: 'inspector-section memory-section' }, el('div', { class: 'section-heading' }, el('h3', {}, title), extra), content);
}
function field(label, id, control, hint) {
  return el('div', { class: 'field' }, el('label', { for: id }, label), control, hint && el('p', { class: 'field-hint' }, hint));
}
function safeTime(value) {
  if (typeof value === 'number') {
    const date = new Date(value > 1e12 ? value : value * 1000);
    return Number.isNaN(date.valueOf()) ? String(value) : date.toLocaleString('zh-CN');
  }
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString('zh-CN');
}
function recordKind(value) { return kindLabels[value] ?? value; }
function sourceLabel(record) {
  const origin = originLabels[record.source.origin] ?? record.source.origin;
  const entry = record.source.entry_id ? ` · ${record.source.entry_id}` : '';
  return `${origin}${entry}`;
}
function isValidDraft(draft) {
  return Boolean(draft && typeof draft.text === 'string' && draft.text.trim().length > 0 && draft.text.length <= 2000
    && MEMORY_KINDS.has(draft.kind) && typeof draft.subject === 'string' && draft.subject.trim().length > 0 && draft.subject.length <= 128);
}

export function memoryPanel(state, view, {
  isAdmin = false, catalogLoading = false, catalogError = null, installed = false, commands = [], enabled = false, recall = true,
  onCatalog = () => {}, onList = () => {}, onJobs = () => {}, onRemember = () => {}, onEdit = () => {}, onUpdate = () => {},
  onCancelEdit = () => {}, onRebase = () => {}, onForget = () => {}, onDelete = () => {},
} = {}) {
  const currentState = state && state.sessionId === view.sessionId ? state : null;
  const body = el('div', { class: 'memory-panel' });
  if (!isAdmin) {
    body.append(note('记忆管理仅对管理员开放。此页面不会读取或修改记忆数据。', 'warning'));
    return body;
  }
  if (!view.sessionId) {
    body.append(el('div', { class: 'inspector-empty' }, icon('info', 28), el('h3', {}, '先选择一个会话'), el('p', {}, '记忆管理始终针对当前选中的会话。')));
    return body;
  }
  body.append(section('当前管理范围', el('div', { class: 'memory-scope' },
    el('p', {}, '目标会话'), el('code', { class: 'identifier' }, view.sessionId),
    el('p', {}, `会话域 · ${view.domain === 'qq' ? 'QQ 来源' : view.domain === 'agent' ? 'Agent 会话' : '正在读取'}`),
    el('p', { class: 'field-hint' }, '记忆按会话隔离；管理不会写入聊天输入，也不会发送 domain 或 scope。'))));
  if (catalogLoading && !installed) {
    body.append(note('正在核对宿主已登记扩展…'));
    return body;
  }
  if (catalogError && !installed) {
    body.append(note(catalogError, 'error', button('重新加载扩展目录', { icon: 'retry', onClick: onCatalog })));
    return body;
  }
  if (!installed) {
    body.append(section('记忆', el('div', { class: 'quiet-empty memory-empty' }, icon('cube', 20),
      el('p', {}, '记忆扩展未安装'), el('span', {}, '宿主尚未登记 memory 扩展；这里不会尝试调用命令。'))));
    return body;
  }
  if (!commands.includes('list')) {
    body.append(section('记忆', note('此扩展版本没有声明记忆管理命令；只可在宿主登记后使用。', 'warning')));
    return body;
  }
  if (!currentState) {
    body.append(note('正在读取当前会话快照；读取完成后可显式加载记忆。'));
    return body;
  }

  if (!enabled) body.append(note('记忆扩展在此会话未启用：自动召回已停用；仍可管理已保存的记忆副本。', 'warning'));
  else if (!recall) body.append(note('此会话已关闭记忆召回；已保存的记录仍可管理。', 'warning'));
  body.append(note('来源为“自动抽取”的内容只是候选，不代表已证实事实。删除只清除扩展副本，不会删除聊天或工具历史。', 'info'));

  const refresh = button(view.status === 'idle' ? '加载当前会话记忆' : '刷新记忆列表', {
    icon: 'retry', disabled: Boolean(view.busy), onClick: () => onList(view.query, view.includeForgotten),
  });
  body.append(section('记忆记录', el('div', { class: 'memory-toolbar' },
    el('div', {}, el('p', {}, view.scopeRevision == null ? '尚未读取服务端修订' : `最近列表修订 r${view.scopeRevision}`),
      el('p', { class: 'field-hint' }, '修订号由服务端记录；写入后请显式刷新核对最新列表。')),
    refresh)));

  if (view.error) body.append(note(view.error.message, 'error'));
  if (view.notice) body.append(note(view.notice.message, view.notice.tone ?? 'success'));
  if (view.requiresRefresh) body.append(note('为避免覆盖或重复写入，请先刷新当前会话记录核对状态；不会自动重发写操作。', 'warning',
    button('刷新并核对', { icon: 'retry', disabled: Boolean(view.busy), onClick: () => onList(view.query, view.includeForgotten) })));
  if (view.status === 'loading') body.append(el('p', { class: 'field-hint', role: 'status' }, '正在读取服务端记忆记录…'));
  if (view.status === 'error' && !view.requiresRefresh) body.append(note('读取失败；可手动重试读取，不会执行写操作。', 'error',
    button('重新读取', { icon: 'retry', disabled: Boolean(view.busy), onClick: () => onList(view.query, view.includeForgotten) })));
  if (view.dirtyAfterWrite) body.append(note('最近一次写入已收到服务端回执；列表修订可能已过期，请手动刷新。', 'warning'));

  const search = el('input', { type: 'search', id: 'memory-search', maxlength: 1000, value: view.query, placeholder: '搜索记忆文字或主题', 'aria-label': '搜索记忆',
    disabled: Boolean(view.busy), onInput: (event) => { view.query = event.target.value; } });
  const forgotten = el('input', { type: 'checkbox', id: 'memory-include-forgotten', checked: view.includeForgotten, disabled: Boolean(view.busy),
    onChange: (event) => { view.includeForgotten = event.target.checked; } });
  const searchForm = el('form', { class: 'memory-search-form', onSubmit: (event) => {
    event.preventDefault(); view.query = search.value; view.includeForgotten = forgotten.checked; onList(view.query, view.includeForgotten);
  } }, field('搜索', search.id, search), el('label', { class: 'check-option', for: forgotten.id }, forgotten, el('span', {}, el('strong', {}, '包含已遗忘记录'))),
  button('应用筛选', { type: 'submit', icon: 'search', disabled: Boolean(view.busy) }));
  body.append(searchForm);

  const canWrite = Boolean(currentState && view.status === 'loaded' && !view.busy && !view.requiresRefresh && commands.includes('remember'));
  const draft = view.newDraft;
  const rememberText = el('textarea', { id: 'memory-new-text', rows: 4, maxlength: 2000, required: true, value: draft.text,
    'aria-label': '新记忆内容', disabled: !canWrite, onInput: (event) => { draft.text = event.target.value; updateRememberButton(); } });
  const rememberKind = el('select', { id: 'memory-new-kind', value: draft.kind, disabled: !canWrite, onChange: (event) => { draft.kind = event.target.value; updateRememberButton(); } },
    [...MEMORY_KINDS].map((kind) => el('option', { value: kind, selected: draft.kind === kind }, recordKind(kind))));
  const rememberSubject = el('input', { id: 'memory-new-subject', maxlength: 128, required: true, value: draft.subject, disabled: !canWrite,
    onInput: (event) => { draft.subject = event.target.value; updateRememberButton(); } });
  const rememberError = el('p', { class: 'error-text', role: 'status' });
  const rememberButton = button(view.busy === 'remember' ? '正在保存…' : '添加记忆', { type: 'submit', variant: 'primary', icon: 'plus', disabled: true });
  function updateRememberButton() {
    const valid = isValidDraft(draft);
    rememberButton.disabled = !canWrite || !valid;
    rememberError.textContent = draft.text.trim().length === 0 && draft.text.length ? '内容不能只包含空白字符。'
      : draft.text.length > 2000 ? '内容最多 2000 个字符。'
        : draft.subject.length > 128 ? '主题最多 128 个字符。' : '';
  }
  updateRememberButton();
  const addForm = el('form', { class: 'memory-add-form', onSubmit: (event) => { event.preventDefault(); if (isValidDraft(draft) && canWrite) onRemember({ ...draft, text: draft.text.trim(), subject: draft.subject.trim() }); } },
    field('记忆内容', rememberText.id, rememberText, '1–2000 字符；添加记忆不会修改聊天记录。'),
    field('类型', rememberKind.id, rememberKind),
    field('主题', rememberSubject.id, rememberSubject, '例如 conversation；1–128 字符。'), rememberError, rememberButton);
  body.append(section('手动添加', addForm));

  if (view.status === 'loaded') {
    const resultQuery = view.loadedQuery.trim().toLocaleLowerCase();
    const visibleRecords = view.records.filter((record) => (view.loadedIncludeForgotten || record.active)
      && (!resultQuery || `${record.text} ${record.subject} ${record.kind}`.toLocaleLowerCase().includes(resultQuery)));
    const records = visibleRecords.map((record) => memoryRecordCard(record, view, {
      canWrite: canWrite && commands.includes('update'),
      canForget: canWrite && commands.includes('forget'),
      canDelete: canWrite && commands.includes('delete'),
      onEdit, onUpdate, onCancelEdit, onRebase, onForget, onDelete,
    }));
    body.append(section('记录列表', records.length ? el('div', { class: 'memory-record-list' }, records)
      : el('div', { class: 'quiet-empty memory-empty' }, icon('search', 20),
        el('p', {}, view.records.length ? '没有符合筛选条件的记忆' : '此会话还没有记忆'),
        el('span', {}, view.records.length ? '试试其他搜索词或包含已遗忘记录。' : '可在上方手动添加；管理内容不会进入聊天输入。')),
    el('span', { class: 'count-pill' }, visibleRecords.length)));
  }

  if (commands.includes('jobs')) {
    const jobsButton = button(view.jobsStatus === 'loading' ? '正在核对…' : '手动刷新任务状态', {
      icon: 'retry', disabled: Boolean(view.busy), onClick: onJobs,
    });
    const jobs = view.jobs.map((job) => el('li', { class: 'memory-job' }, el('code', {}, job.id), badge(jobLabels[job.status] ?? job.status, job.status === 'failed' ? 'danger' : job.status === 'running' || job.status === 'done' ? 'success' : 'neutral')));
    body.append(section('记忆任务', el('div', { class: 'memory-jobs' }, jobsButton,
      view.jobsError && note(view.jobsError.message, 'error'),
      view.jobsStatus === 'loaded' && (jobs.length ? el('ul', { class: 'memory-job-list' }, jobs) : el('p', { class: 'field-hint' }, '没有已登记的记忆任务。')),
      el('p', { class: 'field-hint' }, '任务状态只在手动刷新时读取；不会自动重试，也不会展示凭据或 ticket。'))));
  }
  return body;
}

function memoryRecordCard(record, view, { canWrite, canForget, canDelete, onEdit, onUpdate, onCancelEdit, onRebase, onForget, onDelete }) {
  const edit = view.editDraft?.id === record.id ? view.editDraft : null;
  const meta = el('div', { class: 'memory-record-meta' },
    badge(recordKind(record.kind), 'neutral'), badge(record.active ? '正在召回' : '已遗忘 · 停止召回', record.active ? 'success' : 'warning'),
    el('span', {}, `主题 · ${record.subject}`), el('span', {}, `来源 · ${sourceLabel(record)}`),
    el('span', {}, `置信度 · ${Math.round(record.confidence * 100)}%`), el('span', {}, `修订 r${record.revision}`),
    el('span', {}, `创建 · ${safeTime(record.created_at)}`), el('span', {}, `更新 · ${safeTime(record.updated_at)}`));
  if (record.source.origin === 'extraction') meta.append(note('自动抽取的候选内容尚未证实，请核对后再使用。', 'warning'));
  if (edit) {
    const stale = edit.expected_revision !== record.revision;
    const text = el('textarea', { id: `memory-edit-text-${record.id}`, rows: 4, maxlength: 2000, required: true, value: edit.text,
      'aria-label': '编辑记忆内容', disabled: !canWrite, onInput: (event) => { edit.text = event.target.value; refreshEdit(); } });
    const kind = el('select', { id: `memory-edit-kind-${record.id}`, disabled: !canWrite, onChange: (event) => { edit.kind = event.target.value; refreshEdit(); } },
      [...MEMORY_KINDS].map((value) => el('option', { value, selected: edit.kind === value }, recordKind(value))));
    const subject = el('input', { id: `memory-edit-subject-${record.id}`, maxlength: 128, required: true, value: edit.subject, disabled: !canWrite,
      onInput: (event) => { edit.subject = event.target.value; refreshEdit(); } });
    const error = el('p', { class: 'error-text', role: 'status' });
    const save = button('保存记忆', { type: 'submit', variant: 'primary', icon: 'check' });
    function refreshEdit() {
      const valid = isValidDraft(edit);
      save.disabled = !canWrite || !valid || stale;
      error.textContent = edit.text.trim().length === 0 && edit.text.length ? '内容不能只包含空白字符。'
        : edit.text.length > 2000 ? '内容最多 2000 个字符。' : edit.subject.length > 128 ? '主题最多 128 个字符。' : '';
    }
    refreshEdit();
    const form = el('form', { class: 'memory-edit-form', onSubmit: (event) => { event.preventDefault(); if (isValidDraft(edit) && canWrite && !stale) onUpdate({ ...edit, text: edit.text.trim(), subject: edit.subject.trim() }); } },
      stale && note(`服务端记录已更新为 r${record.revision}。草稿仍保留；核对最新内容后可显式继续。`, 'warning',
        button(`基于 r${record.revision} 继续编辑`, { onClick: () => onRebase(record) })),
      field('记忆内容', text.id, text), field('类型', kind.id, kind), field('主题', subject.id, subject), error,
      el('div', { class: 'memory-actions' }, save, button('取消编辑', { variant: 'ghost', onClick: onCancelEdit })));
    return el('article', { class: 'memory-record', dataset: { memoryId: record.id } }, el('div', { class: 'memory-record-heading' }, el('strong', {}, `记忆 · r${record.revision}`), el('code', {}, record.id)),
      meta, form);
  }
  const actions = el('div', { class: 'memory-actions' },
    button('编辑', { icon: 'settings', disabled: !canWrite, onClick: () => onEdit(record) }),
    record.active && button('停止召回', { icon: 'stop', variant: 'ghost', disabled: !canForget, onClick: () => onForget(record) }),
    button('删除扩展副本', { icon: 'close', variant: 'danger', disabled: !canDelete, onClick: () => onDelete(record) }));
  return el('article', { class: 'memory-record', dataset: { memoryId: record.id } },
    el('div', { class: 'memory-record-heading' }, el('strong', {}, `记忆 · r${record.revision}`), el('code', {}, record.id)),
    el('p', { class: 'memory-record-text' }, record.text), meta, actions);
}
