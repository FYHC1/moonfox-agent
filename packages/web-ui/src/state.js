import { assertContract, assertEvent } from './protocol.js';

const upsert = (items, item) => {
  const index = items.findIndex((existing) => existing.id === item.id);
  return index < 0 ? [...items, item] : items.map((existing, i) => i === index ? item : existing);
};

export function fromSnapshot(snapshot) {
  assertContract('Snapshot', snapshot);
  return {
    sessionId: snapshot.session_id,
    epoch: snapshot.epoch,
    seq: snapshot.seq,
    session: structuredClone(snapshot.session),
    messages: structuredClone(snapshot.messages),
    inputs: structuredClone(snapshot.inputs),
    approvals: structuredClone(snapshot.approvals),
    settings: structuredClone(snapshot.settings),
    configRevision: snapshot.config_revision,
    extensions: structuredClone(snapshot.extensions),
    capabilities: [...snapshot.capabilities],
    streamingIds: snapshot.messages.filter((message) => message.status === 'streaming').map((message) => message.id),
  };
}

// A projection of wire facts, NOT a second Agent/run state machine.
export function projectEvent(state, event) {
  const result = (next = state, effect = null, reason = '') => ({ state: next, effect, reason });
  if (!event || typeof event.session_id !== 'string') return result(state, 'resync', '事件缺少会话标识');
  if (event.session_id !== state.sessionId) return result();
  try { assertEvent(event); } catch { return result(state, 'resync', '收到无法校验的事件'); }
  if (event.epoch !== state.epoch) return result(state, 'resync', '服务端已进入新的事件周期');
  if (event.type === 'resync_required') return result(state, 'resync', '服务端要求重新同步');
  if (event.type === 'heartbeat') return result();
  if (event.seq <= state.seq) return result();
  if (event.seq !== state.seq + 1) return result(state, 'resync', '事件序号不连续');

  const next = { ...state, seq: event.seq };
  const payload = event.payload;
  switch (event.type) {
    case 'message_start':
      next.messages = upsert(state.messages, structuredClone(payload.message));
      next.streamingIds = [...new Set([...state.streamingIds, payload.message.id])];
      break;
    case 'text_delta': {
      if (!state.messages.some((message) => message.id === payload.message_id)) {
        return result(state, 'resync', '文本增量缺少对应消息');
      }
      next.messages = state.messages.map((message) => message.id === payload.message_id
        ? { ...message, text: message.text + payload.delta } : message);
      next.streamingIds = [...new Set([...state.streamingIds, payload.message_id])];
      break;
    }
    case 'message_end':
      next.messages = upsert(state.messages, structuredClone(payload.message));
      next.streamingIds = state.streamingIds.filter((id) => id !== payload.message.id);
      break;
    case 'input_received':
      next.inputs = upsert(state.inputs, structuredClone(payload.input));
      break;
    case 'run_started':
    case 'run_finished':
      next.inputs = upsert(state.inputs, structuredClone(payload.input));
      next.session = { ...state.session, status: payload.status };
      if (event.type === 'run_finished') return result(next, 'refresh', '运行已结束');
      break;
    case 'approval_requested':
    case 'approval_resolved':
      next.approvals = upsert(state.approvals, structuredClone(payload.approval));
      break;
    case 'session_changed':
    case 'config_changed':
      return result(next, 'refresh', '会话信息已更新');
    default: break;
  }
  return result(next);
}

export function acceptApproval(state, approval) {
  assertContract('ApprovalView', approval);
  if (approval.session_id !== state.sessionId) return state;
  return { ...state, approvals: upsert(state.approvals, structuredClone(approval)) };
}

export function lastInput(state) {
  return state?.inputs.at(-1) ?? null;
}

export function resumableInput(state) {
  const input = lastInput(state);
  return input && ['limit_reached', 'error', 'aborted'].includes(input.status) ? input : null;
}

export function pendingInputs(state) {
  return state?.inputs.filter((input) => ['pending', 'queued', 'received'].includes(input.status)) ?? [];
}

export function activeInput(state) {
  return state?.inputs.find((input) => input.status === 'running') ?? lastInput(state);
}

export function executionIdle(state) {
  return Boolean(state) && state.session.status !== 'running' && !state.inputs.some((input) => input.status === 'running') && pendingInputs(state).length === 0;
}

export function pendingSubmission(text, uuid = () => crypto.randomUUID()) {
  return { request_id: uuid(), text, idempotency_key: uuid() };
}

export function createDraft(settings, revision) {
  const draftSettings = structuredClone(settings);
  draftSettings.extension_config ??= [];
  return { settings: draftSettings, revision, dirty: false, saving: false, error: null, saved: false };
}

export function submissionFailure(error) {
  return {
    kind: error.kind ?? 'network',
    message: error.message,
    // A failed input is only retried after a click, with the SAME DTO.
    retryable: ['network', 'server'].includes(error.kind ?? 'network'),
  };
}
