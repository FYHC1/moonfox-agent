import { el, icon, logo, button, badge, statusLabel, statusTone, timeLabel, asMillis, modelLabel } from './dom.js';

const examples = [
  { icon: 'chat', title: '从打个招呼开始', description: '体验输入与回复', text: '你好，Moonfox。请介绍一下离线演示的工作方式。' },
  { icon: 'tool', title: '了解工具与审批', description: '让执行过程可见', text: '请说明当前工作台如何展示工具调用，以及哪些操作需要我确认。' },
  { icon: 'settings', title: '给执行设个边界', description: '认识运行预算', text: '请介绍运行轮次、请求次数和 token 预算的区别。' },
];

export function emptyConversation(state, { onNew, onExample }) {
  const hasSession = Boolean(state);
  const demo = !state || state.settings.model_id === 'demo';
  return el('section', { class: 'empty-conversation', 'aria-labelledby': 'empty-title' },
    el('div', { class: 'empty-moon', 'aria-hidden': true }, el('span', { class: 'empty-orbit' }), logo(56)),
    el('p', { class: 'eyebrow' }, 'MOONFOX · 你的个人工作台'),
    el('h2', { id: 'empty-title' }, hasSession ? '留一点空间，让想法开始。' : '下一段思路，从这里开始。'),
    el('p', { class: 'empty-description' }, hasSession ? '写下你的第一条输入。每一步运行，都有迹可循。' : '创建一个会话，让对话、工具和执行状态各就其位。'),
    el('div', { class: 'empty-demo-note' }, badge(demo ? '离线演示环境' : modelLabel(state.settings.model_id), 'copper'),
      el('p', {}, demo ? '当前示例使用预设回复，不进行真实 AI 推理。' : '模型与工具由宿主提供，执行权限以当前会话配置为准。')),
    hasSession && state.session.domain !== 'qq' && state.capabilities.includes('prompt')
      ? el('div', { class: 'example-grid' }, examples.map((example, index) => el('button', {
        type: 'button', class: 'example-card', style: `--stagger:${index * 45}ms`, onClick: () => onExample(example.text),
      }, icon(example.icon, 20), el('strong', {}, example.title), el('span', {}, example.description), icon('chevron', 14))))
      : !hasSession && button('新建第一个会话', { variant: 'primary', icon: 'plus', onClick: onNew }),
    hasSession && state.session.domain !== 'qq' && el('p', { class: 'example-hint' }, '点击填入示例，不会自动发送。'));
}

function toolCalls(calls) {
  return calls.map((call) => el('details', { class: 'tool-card' },
    el('summary', {}, icon('tool', 16), el('span', {}, typeof call.name === 'string' ? call.name : typeof call.tool_name === 'string' ? call.tool_name : '工具调用'),
      el('span', { class: 'tool-detail-label' }, call.arguments ? '查看参数' : '调用详情'), icon('down', 14)),
    el('pre', { class: 'safe-code' }, JSON.stringify(call, null, 2))));
}

function createMessage(message) {
  const root = el('article', { class: `message message-${message.role}`, dataset: { messageId: message.id } });
  const content = el('div', { class: 'message-content' });
  const text = el(message.role === 'toolResult' ? 'pre' : 'div', { class: message.role === 'toolResult' ? 'safe-code tool-output' : 'message-text', dir: 'auto' });
  const tools = el('div', { class: 'message-tools' });
  const streaming = el('span', { class: 'streaming-label', hidden: true },
    el('span', { class: 'stream-dots', 'aria-hidden': true }, el('i'), el('i'), el('i')), '正在生成文本');
  const name = message.role === 'user' ? '你' : message.role === 'toolResult' ? message.tool_name || '工具结果' : 'Moonfox';
  const label = el('div', { class: 'message-meta' }, el('strong', {}, name),
    message.role === 'toolResult' && badge(message.is_error ? '工具失败' : '工具结果', message.is_error ? 'danger' : 'neutral'),
    el('time', { datetime: new Date(asMillis(message.timestamp)).toISOString() }, timeLabel(message.timestamp)));
  content.append(label);
  if (message.role === 'toolResult') {
    content.append(el('details', { class: `tool-card result-card ${message.is_error ? 'tool-error' : ''}`, open: true },
      el('summary', {}, icon('tool', 16), el('span', {}, message.tool_name || '工具返回'), el('span', { class: 'tool-detail-label' }, '输出'), icon('down', 14)), text));
  } else content.append(text);
  content.append(tools, streaming);
  root.append(el('div', { class: `message-avatar avatar-${message.role}`, 'aria-hidden': true },
    message.role === 'assistant' ? logo(30) : message.role === 'user' ? '你' : icon('tool', 15)), content);
  return { root, text, tools, streaming, lastText: null, lastTools: null };
}

export class ConversationView {
  constructor(container) {
    this.container = container;
    this.nodes = new Map();
    this.sessionId = null;
  }
  reset() { this.nodes.clear(); this.container.replaceChildren(); this.sessionId = null; }
  render(state) {
    if (this.sessionId !== state.sessionId) { this.reset(); this.sessionId = state.sessionId; }
    const ids = new Set(state.messages.map((message) => message.id));
    for (const [id, view] of this.nodes) {
      if (!ids.has(id)) { view.root.remove(); this.nodes.delete(id); }
    }
    state.messages.forEach((message, index) => {
      let view = this.nodes.get(message.id);
      if (!view) {
        view = createMessage(message);
        view.root.style.setProperty('--stagger', `${Math.min(index, 3) * 35}ms`);
        this.nodes.set(message.id, view);
      }
      if (view.lastText !== message.text) { view.text.textContent = message.text; view.lastText = message.text; }
      const calls = JSON.stringify(message.tool_calls);
      if (view.lastTools !== calls) { view.tools.replaceChildren(...toolCalls(message.tool_calls)); view.lastTools = calls; }
      view.streaming.hidden = !state.streamingIds.includes(message.id);
      view.root.setAttribute('aria-busy', String(!view.streaming.hidden));
      if (this.container.children[index] !== view.root) this.container.insertBefore(view.root, this.container.children[index] ?? null);
    });
  }
}

export function approvalCard(approval, { capable, connected = true, busy, error, onDecision, onRefresh, now = Date.now() }) {
  const expired = asMillis(approval.expires_at) <= now;
  const pending = approval.status === 'pending';
  const available = capable && connected && pending && !expired && !busy && !error;
  const state = expired && pending ? 'expired' : approval.status;
  return el('article', { class: `approval-card ${pending && !expired ? 'approval-pending' : ''}`, dataset: { approvalId: approval.id } },
    el('div', { class: 'approval-heading' }, el('span', { class: 'approval-icon' }, icon('shield', 20)),
      el('div', {}, el('h3', {}, pending && !expired ? '这一步，需要你的确认' : '工具审批记录'),
        el('p', {}, el('code', {}, approval.tool_name), ' · ', statusLabel(state))), badge(statusLabel(state), statusTone(state))),
    el('details', { class: 'approval-arguments' }, el('summary', {}, '查看工具参数', icon('down', 14)),
      el('pre', { class: 'safe-code' }, JSON.stringify(approval.arguments, null, 2))),
    pending && el('div', { class: 'approval-footer' }, el('span', { class: 'field-hint' }, expired ? '审批已过期，不再接受决定。' : `${timeLabel(approval.expires_at)} 前有效 · 仅本次调用`),
      el('div', { class: 'approval-actions' },
        button('拒绝', { variant: 'secondary', disabled: !available, onClick: () => onDecision('deny') }),
        button(busy ? '提交中…' : '批准执行', { variant: 'primary', icon: 'check', disabled: !available, onClick: () => onDecision('approve') }))),
    !capable && pending && el('p', { class: 'field-hint' }, '当前会话未授予审批权限。'),
    capable && !connected && pending && el('p', { class: 'field-hint' }, '等待实时连接恢复后再提交审批。'),
    error && el('div', { class: 'approval-error', role: 'alert' }, el('p', {}, error), button('重新核对审批状态', { icon: 'retry', onClick: onRefresh })));
}
