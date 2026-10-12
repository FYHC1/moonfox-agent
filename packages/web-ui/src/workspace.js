import { el, icon, iconButton, button, badge, note, logo, announce, statusLabel, statusTone, modelLabel, timeLabel, asMillis, confirmAction } from './dom.js';
import { SessionStream } from './stream.js';
import { pendingSubmission, createDraft, lastInput, resumableInput, submissionFailure, acceptApproval, pendingInputs, activeInput, executionIdle } from './state.js';
import { ProtocolError } from './protocol.js';
import { ConversationView, emptyConversation, approvalCard } from './conversation.js';
import { overviewPanel, settingsPanel } from './inspector.js';
import { ProviderManager } from './provider-manager.js';

export class Workspace {
  constructor({ api, identity, sessions, onDisconnect }) {
    Object.assign(this, { api, identity, sessions, onDisconnect });
    this.abort = new AbortController();
    this.activeId = null;
    this.selection = 0;
    this.state = null;
    this.drafts = new Map();
    this.composers = new Map();
    this.approvalRequests = new Map();
    this.catalog = null;
    this.catalogError = null;
    this.catalogLoading = false;
    this.listGeneration = 0;
    this.streamStatus = 'idle';
    this.streamDetail = '';
    this.operation = {};
    this.inspectorTab = 'overview';
    this.inspectorOpen = matchMedia('(min-width: 1180px)').matches;
    this.sidebarOpen = false;
    this.disposed = false;
    this.stream = new SessionStream(api, {
      state: (state, source) => this.receiveState(state, source),
      status: (status, detail) => { this.streamStatus = status; this.streamDetail = detail; this.renderConnection(); this.renderComposer(); this.renderInspector(); },
      error: (error) => { this.streamDetail = error.message; this.renderConnection(); },
      unauthorized: () => this.disconnect('身份验证已失效，请重新连接。正在运行的任务不会被取消。'),
      refresh: () => void this.refreshSessions(),
    });
  }

  mount(root) {
    this.root = root;
    this.sidebar = this.buildSidebar();
    this.main = this.buildMain();
    this.inspector = this.buildInspector();
    this.backdrop = el('button', { class: 'drawer-backdrop', 'aria-label': '关闭侧边面板', type: 'button', hidden: true, tabindex: -1, onClick: () => this.closeDrawers() });
    this.shell = el('div', { class: 'workspace' }, this.sidebar, this.main, this.inspector, this.backdrop);
    root.replaceChildren(this.shell);
    this.conversation = new ConversationView(this.messageList);
    const signal = this.abort.signal;
    window.addEventListener('resize', () => this.syncPanels(), { signal });
    window.addEventListener('online', () => void this.stream.reload('online'), { signal });
    window.addEventListener('offline', () => { this.streamStatus = 'offline'; this.streamDetail = '浏览器处于离线状态，服务端任务不受影响。'; this.renderConnection(); this.renderComposer(); }, { signal });
    window.addEventListener('pagehide', () => this.dispose(), { signal });
    document.addEventListener('keydown', (event) => this.handleKey(event), { signal });
    this.renderSessions();
    this.renderHeader();
    this.renderConversation();
    this.renderComposer();
    this.renderInspector();
    this.syncPanels();
    if (this.sessions.length) void this.selectSession(this.sessions[0].id);
    if (this.identity.is_admin) void this.loadCatalog();
  }

  buildSidebar() {
    this.newButton = button('新建会话', { variant: 'primary', icon: 'plus', className: 'new-session-button', onClick: () => this.createSessionDialog() });
    this.sessionSearch = el('input', { type: 'search', placeholder: '查找会话', 'aria-label': '查找会话', onInput: () => this.renderSessions() });
    this.sessionCount = el('span', { class: 'count-pill' }, this.sessions.length);
    this.listRefresh = iconButton('刷新会话列表', 'retry', { onClick: () => void this.refreshSessions() });
    this.listNotice = el('div', { class: 'session-list-notice' });
    this.sessionList = el('nav', { class: 'session-list', 'aria-label': '会话列表' });
    return el('aside', { id: 'sidebar', class: 'sidebar', 'aria-label': '工作空间导航', tabindex: -1 },
      el('div', { class: 'sidebar-brand-row' }, el('div', { class: 'brand' }, logo(36), el('span', { class: 'brand-copy' }, el('strong', {}, 'Moonfox'), el('span', {}, '个人 Agent 工作台'))),
        iconButton('关闭会话列表', 'close', { class: 'icon-button sidebar-close', onClick: () => this.toggleSidebar(false) })),
      el('div', { class: 'workspace-switch-label' }, el('span', { class: 'workspace-symbol' }, 'M'), el('span', {}, '我的工作空间'), el('span', { class: 'local-label' }, 'PERSONAL')),
      this.newButton,
      el('div', { class: 'session-search input-with-icon' }, icon('search', 16), this.sessionSearch),
      el('div', { class: 'session-section-heading' }, el('div', {}, '会话', this.sessionCount), this.listRefresh),
      this.listNotice, this.sessionList,
      el('div', { class: 'sidebar-footer' },
        this.identity.is_admin && button('提供商与模型', { icon: 'cube', variant: 'ghost', className: 'provider-manager-entry', onClick: () => this.openProviderManager() }),
        el('div', { class: 'local-note' }, icon('shield', 16), el('div', {}, el('strong', {}, '你的空间，你的边界'), el('span', {}, '关闭页面不停止服务端任务'))),
        el('div', { class: 'connection-tile' }, el('span', { class: 'identity-avatar' }, this.identity.is_admin ? '管' : '我'),
          el('div', { class: 'identity-copy' }, el('strong', {}, this.identity.is_admin ? '管理员连接' : '已认证连接'), el('span', { title: this.api.origin }, new URL(this.api.origin).host)),
          iconButton('断开连接', 'plug', { onClick: () => this.requestDisconnect() })),
        el('div', { class: 'sidebar-version' }, el('span', {}, 'MOONFOX'), el('span', {}, 'STAGE D / 01'))));
  }

  buildMain() {
    this.sidebarToggle = iconButton('打开会话列表', 'menu', { class: 'icon-button sidebar-toggle', 'aria-controls': 'sidebar', 'aria-expanded': 'false', onClick: () => this.toggleSidebar(true) });
    this.title = el('h1', {}, '新的开始');
    this.headerStatus = el('div', { class: 'header-status' });
    this.connectionLabel = el('span', { class: 'connection-label' });
    this.inspectorToggle = button('会话详情', { icon: 'panel', variant: 'ghost', className: 'inspector-toggle', 'aria-label': '会话详情', 'aria-controls': 'inspector', 'aria-expanded': String(this.inspectorOpen), onClick: () => this.toggleInspector(!this.inspectorOpen) });
    this.contextStrip = el('div', { class: 'context-strip' });
    this.connectionNotice = el('div', { class: 'connection-notice' });
    this.emptyArea = el('div', { class: 'empty-area' });
    this.messageList = el('div', { class: 'message-list', role: 'log', 'aria-label': '会话消息', 'aria-live': 'off' });
    this.approvalsArea = el('div', { class: 'approvals-area', 'aria-label': '工具审批' });
    this.outcomeArea = el('div', { class: 'outcome-area' });
    this.scrollArea = el('div', { class: 'conversation-scroll', tabindex: 0, 'aria-label': '对话记录' },
      el('div', { class: 'conversation-inner' }, this.emptyArea, this.messageList, this.approvalsArea, this.outcomeArea));
    this.jumpButton = button('回到最新消息', { icon: 'down', className: 'jump-latest', hidden: true, onClick: () => this.scrollToBottom() });
    this.scrollArea.addEventListener('scroll', () => { if (this.nearBottom()) this.jumpButton.hidden = true; }, { passive: true });
    this.composerNotice = el('div', { class: 'composer-notice' });
    this.queueLabel = el('div', { class: 'queue-label' });
    this.textarea = el('textarea', { id: 'composer-input', rows: 2, maxlength: 32000, placeholder: '写下你的想法…', 'aria-label': '发送给 Moonfox 的输入', 'aria-describedby': 'composer-hint',
      onInput: () => { this.composer().text = this.textarea.value; this.resizeComposer(); this.updateSendButton(); },
      onKeydown: (event) => {
        if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) { event.preventDefault(); if (!this.sendButton.disabled) this.composerForm.requestSubmit(); }
      },
    });
    this.modelButton = button('选择会话', { variant: 'ghost', icon: 'cube', className: 'composer-model', onClick: () => this.openSettings() });
    this.thinkingLabel = el('span', { class: 'thinking-label' });
    this.stopButton = button('停止', { icon: 'stop', variant: 'ghost', className: 'stop-button', hidden: true, onClick: () => void this.control('cancel') });
    this.sendButton = button('发送', { icon: 'arrow', variant: 'primary', type: 'submit', className: 'send-button', disabled: true });
    this.composerForm = el('form', { class: 'composer-form', onSubmit: (event) => { event.preventDefault(); void this.submitInput(); } }, this.textarea,
      el('div', { class: 'composer-toolbar' }, el('div', { class: 'composer-options' }, this.modelButton, this.thinkingLabel), el('div', { class: 'composer-actions' }, this.stopButton, this.sendButton)));
    this.composerHint = el('span', { id: 'composer-hint' }, 'Enter 发送 · Shift + Enter 换行');
    return el('main', { class: 'main-panel', id: 'main-content' },
      el('header', { class: 'topbar' }, el('div', { class: 'topbar-heading' }, this.sidebarToggle,
        el('div', { class: 'title-stack' }, el('p', { class: 'breadcrumb' }, '工作空间', el('span', {}, '/'), '会话'), this.title)),
        el('div', { class: 'topbar-actions' }, this.headerStatus, this.inspectorToggle)),
      this.contextStrip, this.connectionNotice,
      el('div', { class: 'conversation-frame' }, this.scrollArea, this.jumpButton),
      el('section', { class: 'composer-section', 'aria-label': '输入与执行控制' }, this.queueLabel, this.composerNotice, this.composerForm,
        el('div', { class: 'composer-footnote' }, this.composerHint, this.connectionLabel)));
  }

  buildInspector() {
    this.overviewTab = button('概览', { variant: 'ghost', role: 'tab', id: 'overview-tab', 'aria-controls': 'inspector-body', 'aria-selected': 'true', onClick: () => this.setInspectorTab('overview') });
    this.settingsTab = button('配置', { variant: 'ghost', role: 'tab', id: 'settings-tab', 'aria-controls': 'inspector-body', 'aria-selected': 'false', tabindex: -1, onClick: () => this.setInspectorTab('settings') });
    const tabs = el('div', { class: 'inspector-tabs', role: 'tablist', 'aria-label': '会话详情视图', onKeydown: (event) => {
      if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
        event.preventDefault(); this.setInspectorTab(event.key === 'Home' ? 'overview' : event.key === 'End' ? 'settings' : this.inspectorTab === 'overview' ? 'settings' : 'overview');
        (this.inspectorTab === 'overview' ? this.overviewTab : this.settingsTab).focus();
      }
    } }, this.overviewTab, this.settingsTab);
    this.inspectorBody = el('div', { class: 'inspector-body', id: 'inspector-body', role: 'tabpanel', 'aria-labelledby': 'overview-tab', tabindex: 0 });
    return el('aside', { id: 'inspector', class: 'inspector', 'aria-label': '会话详情', tabindex: -1 },
      el('header', { class: 'inspector-heading' }, el('div', {}, icon('panel', 17), el('h2', {}, '会话详情')),
        iconButton('关闭会话详情', 'close', { onClick: () => this.toggleInspector(false) })), tabs, this.inspectorBody);
  }

  composer() {
    const id = this.activeId ?? '__empty';
    if (!this.composers.has(id)) this.composers.set(id, { text: '', pending: null, receipt: null, error: null, sending: false });
    return this.composers.get(id);
  }

  current(id, selection) { return !this.disposed && this.activeId === id && this.selection === selection; }
  capable(action) { return this.state?.session.domain === 'agent' && this.state.capabilities.includes(action); }
  ready() { return this.streamStatus === 'live' && navigator.onLine && Boolean(this.state); }
  running() { return this.state?.session.status === 'running' || this.state?.inputs.some((input) => input.status === 'running'); }
  pendingCount() { return this.state ? pendingInputs(this.state).length : 0; }

  async selectSession(id) {
    if (this.activeId === id && this.state) { this.toggleSidebar(false); return; }
    const previous = this.composer();
    if (previous.sending) {
      previous.sending = false;
      previous.error = { kind: 'network', retryable: true, message: '切换时回执尚未确认。请先核对会话记录；重试将使用原始幂等键。' };
    }
    const previousDraft = this.drafts.get(this.activeId);
    if (previousDraft?.saving) {
      previousDraft.saving = false;
      previousDraft.error = { kind: 'uncertain', message: '切换时保存回执未确认，请重新载入配置核对。' };
    }
    this.activeId = id;
    ++this.selection;
    this.state = null;
    this.operation = {};
    this.approvalRequests.clear();
    this.approvalsKey = '';
    this.inspectorRenderKey = '';
    this.conversation.reset();
    this.textarea.value = this.composer().text;
    this.toggleSidebar(false);
    this.renderSessions(); this.renderHeader(); this.renderConversation(); this.renderComposer(); this.renderInspector(true);
    await this.stream.select(id);
  }

  receiveState(state, source) {
    if (this.disposed || state.sessionId !== this.activeId) return;
    const stick = this.nearBottom();
    this.state = state;
    const seqLabel = this.inspectorBody.querySelector('#inspector-seq');
    if (seqLabel) seqLabel.textContent = `seq ${state.seq}`;
    const index = this.sessions.findIndex((session) => session.id === state.sessionId);
    if (index >= 0) this.sessions[index] = state.session;
    else this.sessions.unshift(state.session);
    const existing = this.drafts.get(state.sessionId);
    if (!existing || (!existing.dirty && !existing.saving && existing.revision !== state.configRevision)) {
      this.drafts.set(state.sessionId, createDraft(state.settings, state.configRevision));
    } else if (existing.dirty && !existing.saving && existing.revision !== state.configRevision) {
      existing.error = { kind: 'conflict', message: '配置修订已变化，请重新载入。' };
    }
    const receipt = this.composer().receipt;
    if (receipt && state.inputs.some((input) => input.id === receipt.input_id && ['completed', 'aborted', 'cancelled', 'canceled', 'error', 'limit_reached', 'unknown'].includes(input.status))) this.composer().receipt = null;
    this.renderHeader(); this.renderConversation();
    if (source !== 'text_delta') { this.renderSessions(); this.renderComposer(); this.renderInspector(); }
    if (source === 'message_end') announce('一条消息已完成。');
    if (source === 'approval_requested') announce('有工具调用需要审批。');
    if (source === 'run_finished') announce(`本次运行${statusLabel(lastInput(state)?.status)}。`);
    if (stick) requestAnimationFrame(() => { if (!this.disposed) this.scrollToBottom(); });
    else this.jumpButton.hidden = false;
  }

  renderHeader() {
    const summary = this.state?.session ?? this.sessions.find((session) => session.id === this.activeId);
    this.title.textContent = summary?.title ?? '新的开始';
    this.title.title = summary?.title ?? '';
    this.headerStatus.replaceChildren(...(summary ? [badge(statusLabel(summary.status), statusTone(summary.status))] : []));
    this.contextStrip.replaceChildren(
      el('span', { class: 'context-domain' }, icon('chat', 14), summary?.domain === 'qq' ? 'QQ 来源 · 只读' : 'AGENT 工作空间'),
      el('span', { class: 'context-provider' }, el('span', { class: 'mini-moon', 'aria-hidden': true }),
        !summary || summary.model_id === 'demo' ? '离线演示 · 预设回复，不调用真实 AI' : `宿主模型 · ${summary.model_id}`));
  }

  renderSessions() {
    const query = this.sessionSearch.value.trim().toLocaleLowerCase();
    const sessions = this.sessions.filter((session) => session.title.toLocaleLowerCase().includes(query));
    this.sessionCount.textContent = String(this.sessions.length);
    const listKey = JSON.stringify([this.activeId, query, sessions]);
    if (listKey === this.sessionListKey) return;
    this.sessionListKey = listKey;
    const focusedSession = this.sessionList.contains(document.activeElement) ? document.activeElement.dataset.sessionId : null;
    this.sessionList.replaceChildren(...sessions.map((session, index) => el('button', {
      type: 'button', class: `session-item ${session.id === this.activeId ? 'is-selected' : ''}`, 'aria-current': session.id === this.activeId ? 'page' : null,
      dataset: { sessionId: session.id }, style: `--stagger:${Math.min(index, 5) * 25}ms`, onClick: () => void this.selectSession(session.id),
    }, el('span', { class: 'session-item-icon' }, icon(session.domain === 'qq' ? 'globe' : 'chat', 17)),
      el('span', { class: 'session-item-copy' }, el('strong', {}, session.title),
        el('span', { class: 'session-item-meta' }, el('span', {}, session.domain === 'qq' ? 'QQ · 只读' : statusLabel(session.status)), el('time', {}, timeLabel(session.updated_at)), session.pending_count > 0 && el('span', { class: 'queued-count' }, `+${session.pending_count}`))))));
    if (!sessions.length) this.sessionList.append(el('div', { class: 'session-empty' }, icon(query ? 'search' : 'chat', 24),
      el('p', {}, query ? '没有找到匹配会话' : '还没有会话'), el('span', {}, query ? '试试其他关键词。' : '给新的想法留一个位置。')));
    if (focusedSession) [...this.sessionList.querySelectorAll('button')].find((node) => node.dataset.sessionId === focusedSession)?.focus({ preventScroll: true });
  }

  async refreshSessions() {
    if (this.disposed) return;
    const generation = ++this.listGeneration;
    this.listRefresh.disabled = true;
    try {
      const sessions = await this.api.sessions(this.abort.signal);
      if (this.disposed || generation !== this.listGeneration) return;
      this.sessions = sessions;
      this.listNotice.replaceChildren();
      this.renderSessions();
    } catch (error) {
      if (this.disposed || generation !== this.listGeneration || this.handleUnauthorized(error)) return;
      this.listNotice.replaceChildren(note(error.message, 'error', button('重试列表', { icon: 'retry', onClick: () => void this.refreshSessions() })));
    } finally { if (!this.disposed && generation === this.listGeneration) this.listRefresh.disabled = false; }
  }

  renderConnection() {
    if (this.disposed || !this.connectionLabel) return;
    const labels = { live: '实时连接', loading: '载入会话', connecting: '订阅中', syncing: '同步快照', reconnecting: '正在重连', blocked: '订阅被拒绝', error: '同步失败', offline: '已离线', idle: '已认证' };
    this.connectionLabel.replaceChildren(el('span', { class: `status-dot ${this.streamStatus === 'live' ? 'dot-success' : 'dot-warning'}` }), labels[this.streamStatus] ?? '等待连接');
    this.connectionNotice.replaceChildren();
    if (['offline', 'reconnecting', 'blocked', 'error'].includes(this.streamStatus)) {
      this.connectionNotice.append(note(`${this.streamDetail} 只恢复订阅，不会自动重新执行输入。`, this.streamStatus === 'blocked' || this.streamStatus === 'error' ? 'error' : 'warning',
        el('div', { class: 'inline-actions' }, button('重新同步', { icon: 'retry', onClick: () => void this.stream.reload() }),
          this.streamStatus === 'blocked' && button('重新认证', { onClick: () => this.disconnect('请核对身份与服务端 Origin allowlist 后重新连接。') }))));
    }
    if (!this.state && this.activeId) this.renderConversation();
  }

  renderConversation() {
    if (!this.state && this.activeId) {
      const error = ['blocked', 'error', 'offline'].includes(this.streamStatus);
      this.emptyArea.replaceChildren(el('div', { class: 'loading-state' },
        el('span', { class: error ? 'loading-symbol' : 'loading-symbol is-loading' }, icon(error ? 'alert' : 'link', 24)),
        el('h2', {}, error ? '暂时无法读取会话' : '正在接回这段思路'),
        el('p', {}, error ? '消息以服务端记录为准，请重新同步。' : '先获取完整快照，再订阅增量事件。'),
        error && button('重新读取', { icon: 'retry', onClick: () => void this.stream.reload() })));
    } else if (!this.state || this.state.messages.length === 0) {
      const key = `${this.activeId}:${this.state?.settings.model_id}:${this.state?.capabilities.join()}`;
      if (this.emptyKey !== key || !this.emptyArea.firstChild) {
        this.emptyArea.replaceChildren(emptyConversation(this.state, { onNew: () => this.createSessionDialog(), onExample: (value) => this.fillExample(value) }));
        this.emptyKey = key;
      }
    } else this.emptyArea.replaceChildren();
    if (this.state) this.conversation.render(this.state);
    this.renderApprovals();
    this.renderOutcome();
  }

  renderApprovals() {
    clearTimeout(this.approvalExpiryTimer);
    if (!this.state) { this.approvalsArea.replaceChildren(); this.approvalsKey = ''; return; }
    const now = Date.now();
    const key = JSON.stringify([this.state.approvals, [...this.approvalRequests], this.state.capabilities, this.ready(), this.state.approvals.map((approval) => asMillis(approval.expires_at) <= now)]);
    if (key !== this.approvalsKey) {
      const open = new Set([...this.approvalsArea.querySelectorAll('article')].filter((article) => article.querySelector('details')?.open).map((article) => article.dataset.approvalId));
      this.approvalsArea.replaceChildren(...this.state.approvals.map((approval) => {
        const request = this.approvalRequests.get(approval.id);
        const card = approvalCard(approval, { capable: this.capable('approve'), connected: this.ready(), busy: request?.busy, error: request?.error,
          onDecision: (decision) => void this.approve(approval, decision), onRefresh: () => { this.approvalRequests.delete(approval.id); void this.stream.reload(); }, now });
        if (open.has(approval.id)) card.querySelector('details').open = true;
        return card;
      }));
      this.approvalsKey = key;
    }
    const deadlines = this.state.approvals.filter((approval) => approval.status === 'pending' && asMillis(approval.expires_at) > now).map((approval) => asMillis(approval.expires_at));
    if (deadlines.length) this.approvalExpiryTimer = setTimeout(() => this.renderApprovals(), Math.min(2147483647, Math.min(...deadlines) - now + 30));
  }

  renderOutcome() {
    this.outcomeArea.replaceChildren();
    const input = lastInput(this.state);
    if (!input) return;
    const messages = {
      completed: '本次运行已完成。', aborted: '本次运行已停止。不会自动继续执行。',
      error: '运行遇到错误。检查记录后，可显式继续最后一条输入。',
      limit_reached: '本次运行达到限制。继续仍共用该输入的请求与 token 总预算。',
      unknown: '此输入的最终状态待核实。为避免重复副作用，不会自动重放。',
    };
    if (messages[input.status]) this.outcomeArea.append(el('div', { class: `run-outcome outcome-${input.status}` },
      icon(input.status === 'completed' ? 'check' : input.status === 'aborted' ? 'stop' : 'info', 16),
      el('div', {}, el('p', {}, messages[input.status]), input.error && el('p', { class: 'error-text' }, input.error)),
      resumableInput(this.state) && this.capable('continue') && button('继续此输入', { icon: 'arrow', disabled: Boolean(this.operation.busy) || !this.ready(), onClick: () => void this.control('continue') })));
  }

  renderComposer() {
    if (!this.textarea || this.disposed) return;
    const entry = this.composer();
    const capable = this.capable('prompt');
    this.textarea.disabled = !this.state || !capable;
    this.textarea.readOnly = entry.sending || Boolean(entry.pending && entry.error);
    this.textarea.placeholder = !this.activeId ? '先新建一个会话，再开始输入…' : this.state?.session.domain === 'qq' ? 'QQ 来源会话在此工作台仅供查看。' : !capable ? '当前会话未授予输入权限。' : this.running() || this.pendingCount() ? '写下下一条输入，将进入服务端队列…' : '写下你的想法…';
    this.modelButton.querySelector('span').textContent = this.state ? modelLabel(this.state.settings.model_id) : '尚未选择会话';
    this.modelButton.disabled = !this.state;
    this.thinkingLabel.textContent = this.state ? `思考 · ${this.state.settings.thinking_level}` : '';
    this.stopButton.hidden = !this.capable('cancel') || !(this.running() || this.pendingCount() > 0);
    this.stopButton.disabled = Boolean(this.operation.busy) || !this.ready();
    this.queueLabel.replaceChildren();
    if (this.pendingCount() > 0) this.queueLabel.append(icon('clock', 14), `${this.pendingCount()} 条输入在服务端排队`, el('span', {}, '按接收顺序执行 · 关闭页面不中断'));
    else if (this.running()) this.queueLabel.append(el('span', { class: 'stream-dots', 'aria-hidden': true }, el('i'), el('i'), el('i')), '正在运行', el('span', {}, '由服务端管理 · 可显式停止'));
    this.composerNotice.replaceChildren();
    if (entry.sending) this.composerNotice.append(note('正在提交输入，等待服务端回执。'));
    if (entry.error) this.composerNotice.append(note(entry.error.message, 'error', el('div', { class: 'inline-actions' },
      entry.error.retryable && button('按原幂等键重试', { icon: 'retry', disabled: !this.ready(), onClick: () => void this.submitInput(true) }),
      button('返回编辑', { variant: 'ghost', onClick: () => void this.editPending() }))));
    if (entry.receipt && !entry.error && !entry.sending) this.composerNotice.append(note(`输入已接收（${statusLabel(entry.receipt.status)}），不代表执行完成。请以会话结果为准。`, 'success',
      button('收起回执', { variant: 'ghost', className: 'small-button', onClick: () => { entry.receipt = null; this.renderComposer(); } })));
    if (this.operation.error) this.composerNotice.append(note(this.operation.error, 'error', button('核对最新状态', { icon: 'retry', onClick: () => void this.stream.reload() })));
    if (this.operation.notice) this.composerNotice.append(note(this.operation.notice, 'info', button('收起', { variant: 'ghost', className: 'small-button', onClick: () => { this.operation.notice = null; this.renderComposer(); } })));
    this.updateSendButton();
    this.renderApprovals();
    this.renderOutcome();
  }

  updateSendButton() {
    const entry = this.composer();
    this.sendButton.disabled = !this.ready() || !this.capable('prompt') || entry.sending || Boolean(entry.pending && entry.error) || !entry.text.trim();
    this.sendButton.querySelector('span').textContent = entry.sending ? '提交中' : this.running() || this.pendingCount() > 0 ? '加入队列' : '发送';
  }

  resizeComposer() {
    this.textarea.style.height = 'auto';
    this.textarea.style.height = `${Math.min(160, Math.max(60, this.textarea.scrollHeight))}px`;
  }
  fillExample(value) {
    if (!this.capable('prompt') || this.composer().pending) return;
    this.composer().text = value; this.textarea.value = value; this.resizeComposer(); this.updateSendButton(); this.textarea.focus();
  }
  nearBottom() { return this.scrollArea.scrollHeight - this.scrollArea.scrollTop - this.scrollArea.clientHeight < 110; }
  scrollToBottom() { this.scrollArea.scrollTop = this.scrollArea.scrollHeight; this.jumpButton.hidden = true; }

  async submitInput(retry = false) {
    const entry = this.composer();
    if (!this.ready() || !this.capable('prompt') || entry.sending || (!retry && (entry.pending || !entry.text.trim()))) return;
    if (retry && (!entry.pending || !entry.error?.retryable)) return;
    if (!entry.pending) entry.pending = pendingSubmission(entry.text.trim());
    const body = entry.pending;
    const id = this.activeId; const selection = this.selection;
    entry.sending = true; entry.error = null; entry.receipt = null; this.renderComposer();
    try {
      const receipt = await this.api.submit(id, body, this.abort.signal);
      if (!this.current(id, selection) || entry.pending !== body) return;
      if (receipt.session_id !== id || receipt.request_id !== body.request_id) throw new ProtocolError('Receipt request identity');
      entry.receipt = receipt; entry.pending = null; entry.text = ''; entry.sending = false;
      this.textarea.value = ''; this.resizeComposer();
      announce('输入已接收，等待服务端执行结果。');
      void this.refreshSessions();
    } catch (error) {
      if (!this.current(id, selection) || entry.pending !== body || this.handleUnauthorized(error)) return;
      entry.error = submissionFailure(error); entry.sending = false;
      announce('输入回执未确认，请检查错误并手动操作。');
    } finally {
      if (this.current(id, selection)) { this.renderComposer(); if (!entry.error) this.textarea.focus(); }
    }
  }

  async editPending() {
    const entry = this.composer(); const id = this.activeId; const selection = this.selection;
    if ((entry.error?.retryable || entry.error?.kind === 'protocol') && !await confirmAction({ title: '返回编辑这条输入？', description: '未确认响应不代表服务端未接收。建议先核对会话记录；重新编辑并发送会使用新的幂等键，可能产生第二条输入。', confirmLabel: '确认返回编辑' })) return;
    if (!this.current(id, selection)) return;
    entry.pending = null; entry.error = null; this.renderComposer(); this.textarea.focus();
  }

  async control(action) {
    if (!this.capable(action) || !this.ready() || this.operation.busy) return;
    const id = this.activeId; const selection = this.selection;
    const input = action === 'continue' ? resumableInput(this.state) : null;
    if (action === 'continue' && !input) return;
    const descriptions = {
      cancel: ['停止运行并取消队列？', '这会停止当前运行，并取消此会话所有待处理输入。只关闭页面或切换会话不会停止任务。', '停止并清空队列'],
      continue: ['明确继续这条输入？', '将继续最后一条可恢复输入，不创建新的逻辑输入。请求与 token 总预算不会重置，也不会自动重试。', '继续执行'],
      compact: ['压缩此会话上下文？', '由宿主注入的压缩策略处理上下文；仅在会话空闲时执行。', '确认压缩'],
    };
    const [title, description, confirmLabel] = descriptions[action];
    if (!await confirmAction({ title, description, confirmLabel, danger: action === 'cancel' }) || !this.current(id, selection)) return;
    const body = { request_id: crypto.randomUUID(), action, ...(input ? { input_id: input.id } : {}) };
    this.operation = { busy: action }; this.renderComposer(); this.renderInspector(true);
    try {
      const receipt = await this.api.control(id, body, this.abort.signal);
      if (!this.current(id, selection)) return;
      if (receipt.session_id !== id || receipt.request_id !== body.request_id) throw new ProtocolError('Control receipt');
      this.operation = { notice: `${action === 'cancel' ? '停止与清队列' : action === 'continue' ? '继续' : '压缩'}请求已接收；最终状态以服务端快照为准。` };
      void this.stream.reload('control'); void this.refreshSessions();
    } catch (error) {
      if (!this.current(id, selection) || this.handleUnauthorized(error)) return;
      this.operation = { error: `${error.message} 不会自动重试执行，请先核对最新状态。` };
    } finally { if (this.current(id, selection)) { this.renderComposer(); this.renderInspector(true); } }
  }

  async approve(approval, decision) {
    if (!this.ready() || !this.capable('approve') || approval.status !== 'pending' || asMillis(approval.expires_at) <= Date.now() || this.approvalRequests.get(approval.id)?.busy) return;
    const id = this.activeId; const selection = this.selection;
    const request = { busy: true };
    this.approvalRequests.set(approval.id, request); this.renderApprovals();
    try {
      const result = await this.api.approve(id, approval.id, { request_id: crypto.randomUUID(), decision }, this.abort.signal);
      if (!this.current(id, selection) || this.approvalRequests.get(approval.id) !== request) return;
      if (result.id !== approval.id || result.session_id !== id) throw new ProtocolError('Approval identity');
      this.state = acceptApproval(this.state, result); this.stream.state = this.state;
      this.approvalRequests.delete(approval.id); announce(`工具审批${statusLabel(result.status)}。`);
      void this.stream.reload('approval');
    } catch (error) {
      if (!this.current(id, selection) || this.handleUnauthorized(error)) return;
      this.approvalRequests.set(approval.id, { busy: false, error: error.message });
    } finally { if (this.current(id, selection)) this.renderApprovals(); }
  }

  renderInspector(force = false) {
    if (!this.inspectorBody || this.disposed) return;
    const draft = this.drafts.get(this.activeId);
    const key = this.inspectorTab === 'overview' ? JSON.stringify([this.state?.session, this.state?.inputs, this.state?.settings, this.state?.configRevision, this.state?.extensions, this.state?.capabilities, this.state?.epoch, this.operation.busy, this.ready()])
      : JSON.stringify([this.activeId, Boolean(this.state), this.state?.capabilities, this.catalog, this.catalogError, this.catalogLoading, draft?.revision, draft?.saving, draft?.error, draft?.saved]);
    if (!force && key === this.inspectorRenderKey) return;
    this.inspectorRenderKey = key;
    const scroll = this.inspectorBody.scrollTop;
    this.inspectorBody.replaceChildren(this.inspectorTab === 'overview' ? overviewPanel(this.state, { onSettings: () => this.setInspectorTab('settings'), onCompact: () => void this.control('compact'), compactBusy: this.operation.busy === 'compact', connected: this.ready() })
      : settingsPanel(this.state, draft, this.catalog, {
        catalogError: this.catalogError, catalogLoading: this.catalogLoading, onCatalog: () => void this.loadCatalog(),
        canConfigure: this.identity.is_admin && this.capable('configure'),
        onChange: (key, value) => {
          draft.settings[key] = value; draft.dirty = true; draft.saved = false;
          if (draft.error?.kind !== 'conflict' && draft.error?.kind !== 'uncertain') draft.error = null;
          this.inspectorRenderKey = JSON.stringify([this.activeId, Boolean(this.state), this.state?.capabilities, this.catalog, this.catalogError, this.catalogLoading, draft.revision, draft.saving, draft.error, draft.saved]);
        },
        onSave: () => void this.saveSettings(), onReload: () => void this.reloadSettings(),
      }));
    this.inspectorBody.scrollTop = scroll;
  }

  openProviderManager() {
    if (!this.identity.is_admin || this.disposed || this.providerManager) return;
    this.providerManager = new ProviderManager({ api: this.api,
      onChanged: () => this.loadCatalog(),
      onClose: () => { this.providerManager = null; void this.loadCatalog(); },
      onUnauthorized: (error) => this.handleUnauthorized(error),
    });
    this.providerManager.open();
  }

  async loadCatalog() {
    if (!this.identity.is_admin || this.disposed) return;
    if (this.catalogLoading) { this.catalogRefreshQueued = true; return; }
    this.catalogRefreshQueued = false;
    this.catalogLoading = true; this.catalogError = null; this.renderInspector();
    try { const catalog = await this.api.catalog(this.abort.signal); if (!this.disposed) this.catalog = catalog; }
    catch (error) { if (!this.disposed && !this.handleUnauthorized(error)) this.catalogError = error.message; }
    finally {
      if (!this.disposed) {
        this.catalogLoading = false; this.renderInspector();
        if (this.catalogRefreshQueued) void this.loadCatalog();
      }
    }
  }

  async saveSettings() {
    const id = this.activeId; const selection = this.selection; const draft = this.drafts.get(id);
    if (!draft || draft.saving || !draft.dirty || ['conflict', 'uncertain'].includes(draft.error?.kind) || !this.capable('configure')) return;
    draft.saving = true; draft.error = null; this.renderInspector(true);
    try {
      const result = await this.api.configure(id, { request_id: crypto.randomUUID(), expected_revision: draft.revision, settings: structuredClone(draft.settings) }, this.abort.signal);
      if (!this.current(id, selection)) return;
      this.drafts.set(id, { ...createDraft(result.settings, result.revision), saved: true });
      announce('配置已保存，仅下次运行生效。');
      void this.stream.reload('config');
    } catch (error) {
      if (!this.current(id, selection) || this.handleUnauthorized(error)) return;
      draft.error = { kind: error.kind, message: error.message };
    } finally {
      if (this.current(id, selection)) draft.saving = false;
      if (this.current(id, selection)) this.renderInspector(true);
    }
  }

  async reloadSettings() {
    const id = this.activeId; const selection = this.selection;
    const draft = this.drafts.get(id);
    if (draft?.dirty && !await confirmAction({ title: '重新载入服务端配置？', description: '这会放弃当前未保存的编辑，使用服务端最新配置与修订号。不会覆盖服务端内容。', confirmLabel: '放弃编辑并载入' })) return;
    if (!this.current(id, selection)) return;
    const snapshot = await this.stream.reload('reload-config');
    if (snapshot && this.current(id, selection)) { this.drafts.set(id, createDraft(snapshot.settings, snapshot.config_revision)); this.renderInspector(true); announce('已载入最新配置。'); }
  }

  createSessionDialog() {
    const requestId = crypto.randomUUID();
    const title = el('input', { id: 'new-session-title', value: '新对话', required: true, maxlength: 120, autocomplete: 'off' });
    const feedback = el('div');
    const submit = button('创建会话', { type: 'submit', variant: 'primary', icon: 'plus' });
    const dialog = el('dialog', { class: 'confirm-dialog create-dialog', 'aria-labelledby': 'create-title' });
    const close = () => { dialog.close(); dialog.remove(); };
    const cancel = button('返回', { onClick: close });
    const form = el('form', {}, el('h2', { id: 'create-title' }, '给一个想法，新的空间'),
      el('p', {}, '创建个人 Agent 会话，使用宿主当前默认配置。'),
      el('div', { class: 'field' }, el('label', { for: 'new-session-title' }, '会话名称'), title),
      el('p', { class: 'field-hint' }, '浏览器不会设置会话域、所有者或内部 scope。'), feedback,
      el('div', { class: 'dialog-actions' }, cancel, submit));
    form.addEventListener('submit', async (event) => {
      event.preventDefault(); if (!form.reportValidity() || submit.disabled) return;
      submit.disabled = true; title.disabled = true;
      cancel.disabled = true;
      submit.querySelector('span').textContent = '正在创建…';
      const selection = this.selection;
      try {
        const session = await this.api.createSession({ request_id: requestId, title: title.value.trim() || '新对话' }, this.abort.signal);
        if (this.disposed) return;
        if (!this.sessions.some((item) => item.id === session.id)) this.sessions.unshift(session);
        this.renderSessions(); close();
        if (selection === this.selection) void this.selectSession(session.id);
        announce('会话已创建。');
      } catch (error) {
        if (this.disposed || this.handleUnauthorized(error)) { close(); return; }
        feedback.replaceChildren(note(`${error.message}${['network', 'server'].includes(error.kind) ? ' 创建结果可能已生效，请先刷新会话列表核对，避免重复创建。' : ''}`, 'error',
          button('关闭并刷新列表', { onClick: () => { close(); void this.refreshSessions(); } })));
        submit.disabled = ['network', 'server'].includes(error.kind); title.disabled = false;
        cancel.disabled = false;
        submit.querySelector('span').textContent = '重新提交';
      }
    });
    dialog.addEventListener('cancel', (event) => { event.preventDefault(); if (!cancel.disabled) close(); });
    dialog.append(form); document.body.append(dialog); dialog.showModal(); title.focus(); title.select();
  }

  setInspectorTab(tab) {
    this.inspectorTab = tab;
    this.overviewTab.setAttribute('aria-selected', String(tab === 'overview')); this.overviewTab.tabIndex = tab === 'overview' ? 0 : -1;
    this.settingsTab.setAttribute('aria-selected', String(tab === 'settings')); this.settingsTab.tabIndex = tab === 'settings' ? 0 : -1;
    this.inspectorBody.setAttribute('aria-labelledby', tab === 'overview' ? 'overview-tab' : 'settings-tab');
    this.inspectorRenderKey = ''; this.renderInspector(true); this.inspectorBody.scrollTop = 0;
  }
  openSettings() { this.setInspectorTab('settings'); this.toggleInspector(true); }
  toggleSidebar(open) {
    if (open) { this.returnFocus = document.activeElement; this.inspectorOpen = false; }
    this.sidebarOpen = open; this.syncPanels();
    if (open) this.sidebar.querySelector('button')?.focus(); else if (this.returnFocus?.isConnected && !this.returnFocus.closest('[hidden]')) this.returnFocus.focus();
  }
  toggleInspector(open) {
    if (open) { this.returnFocus = document.activeElement; this.sidebarOpen = false; }
    this.inspectorOpen = open; this.syncPanels();
    if (open && innerWidth < 1180) this.inspector.querySelector('button')?.focus();
    if (!open) (this.returnFocus?.isConnected ? this.returnFocus : this.inspectorToggle).focus();
  }
  closeDrawers() { this.sidebarOpen = false; this.inspectorOpen = false; this.syncPanels(); if (this.returnFocus?.isConnected) this.returnFocus.focus(); }
  syncPanels() {
    if (!this.shell) return;
    const mobile = innerWidth < 760; const narrow = innerWidth < 1180;
    this.sidebar.hidden = mobile && !this.sidebarOpen;
    this.inspector.hidden = !this.inspectorOpen;
    this.shell.classList.toggle('inspector-closed', !this.inspectorOpen);
    this.sidebar.classList.toggle('is-open', this.sidebarOpen);
    this.inspector.classList.toggle('is-open', this.inspectorOpen);
    this.sidebarToggle.setAttribute('aria-expanded', String(this.sidebarOpen));
    this.inspectorToggle.setAttribute('aria-expanded', String(this.inspectorOpen));
    const overlay = mobile && this.sidebarOpen ? this.sidebar : narrow && this.inspectorOpen ? this.inspector : null;
    this.overlay = overlay; this.backdrop.hidden = !overlay;
    this.main.inert = Boolean(overlay);
    this.sidebar.inert = Boolean(overlay && overlay !== this.sidebar);
    this.inspector.inert = Boolean(overlay && overlay !== this.inspector);
    for (const panel of [this.sidebar, this.inspector]) {
      if (panel === overlay) { panel.setAttribute('role', 'dialog'); panel.setAttribute('aria-modal', 'true'); }
      else { panel.removeAttribute('role'); panel.removeAttribute('aria-modal'); }
    }
  }
  handleKey(event) {
    if (document.querySelector('dialog[open]')) return;
    if (event.key === 'Escape' && this.overlay) { event.preventDefault(); this.closeDrawers(); return; }
    if (event.key !== 'Tab' || !this.overlay) return;
    const nodes = [...this.overlay.querySelectorAll('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), summary, [tabindex="0"]')].filter((node) => node.getClientRects().length && !node.closest('[hidden]'));
    const first = nodes[0]; const last = nodes.at(-1);
    if (event.shiftKey && (document.activeElement === first || !this.overlay.contains(document.activeElement))) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && (document.activeElement === last || !this.overlay.contains(document.activeElement))) { event.preventDefault(); first?.focus(); }
  }
  handleUnauthorized(error) { if (error.kind !== 'unauthorized') return false; this.disconnect('身份验证失败，请重新连接。服务端任务不会被取消。'); return true; }
  async requestDisconnect() {
    if (await confirmAction({ title: '断开此浏览器的连接？', description: '令牌将从内存清除，运行与排队任务继续由服务端管理。此操作不会调用停止。', confirmLabel: '断开连接' })) this.disconnect();
  }
  disconnect(message) { this.dispose(); this.api.clearCredentials(); this.onDisconnect(message); }
  dispose() {
    if (this.disposed) return;
    this.disposed = true; ++this.selection; ++this.listGeneration;
    this.abort.abort(); this.stream.dispose(); clearTimeout(this.approvalExpiryTimer);
    this.providerManager?.dispose(); this.providerManager = null;
    this.api.clearCredentials(); this.composers.clear(); this.drafts.clear();
    document.querySelectorAll('dialog').forEach((dialog) => { dialog.close(); dialog.remove(); });
  }
}
