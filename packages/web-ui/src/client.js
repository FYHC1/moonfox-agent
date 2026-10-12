import { assertContract, ProtocolError } from './protocol.js';

const errorLabels = {
  401: ['unauthorized', '身份验证失败，请重新输入管理员令牌。'],
  403: ['forbidden', '当前身份没有此操作权限。'],
  404: ['not-found', '会话或资源已不存在，请刷新会话列表。'],
  409: ['conflict', '操作冲突，请核对最新状态后再操作。'],
  422: ['invalid', '提交内容不符合服务端约束。'],
  429: ['limit', '服务端预算或队列已达到上限。'],
};

export class ApiError extends Error {
  constructor(status, detail = '') {
    const [kind, message] = errorLabels[status] ?? (status === 0
      ? ['network', '未能确认服务端响应，请检查网络后手动重试。']
      : ['server', '服务端暂时无法完成请求。']);
    super(detail ? `${message} ${detail}` : message);
    this.name = 'ApiError';
    this.status = status;
    this.kind = kind;
  }
}

export function normalizeOrigin(value, fallback = globalThis.location?.origin) {
  let url;
  try { url = new URL(value.trim() || fallback); } catch { throw new Error('请输入有效的 HTTP 或 HTTPS 后端地址。'); }
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash || url.pathname !== '/') {
    throw new Error('后端地址只能包含协议、主机和端口，不能包含路径、凭据或查询参数。');
  }
  return url.origin;
}

export class ApiClient {
  #token;
  constructor({ origin, token, fetchImpl = globalThis.fetch, WebSocketImpl = globalThis.WebSocket, timeout = 20000 }) {
    this.origin = normalizeOrigin(origin);
    this.#token = token;
    this.fetchImpl = fetchImpl?.bind(globalThis);
    this.WebSocketImpl = WebSocketImpl;
    this.timeout = timeout;
  }

  clearCredentials() { this.#token = ''; }

  async request(path, { method = 'GET', body, inputSchema, outputSchema, signal } = {}) {
    if (inputSchema) assertContract(inputSchema, body);
    const timeoutController = new AbortController();
    const timer = setTimeout(() => timeoutController.abort(), this.timeout);
    const requestSignal = signal ? AbortSignal.any([signal, timeoutController.signal]) : timeoutController.signal;
    let response;
    let data;
    try {
      response = await this.fetchImpl(`${this.origin}/api/v1${path}`, {
        method,
        headers: { Authorization: `Bearer ${this.#token}`, ...(body ? { 'Content-Type': 'application/json' } : {}) },
        body: body ? JSON.stringify(body) : undefined,
        signal: requestSignal,
        cache: 'no-store',
        credentials: 'omit',
        redirect: 'error',
      });
      let raw;
      try { raw = await response.text(); } catch { throw new ApiError(0); }
      try { data = JSON.parse(raw); } catch {
        if (!response.ok) throw new ApiError(response.status);
        throw new ProtocolError('JSON response');
      }
    } catch (error) {
      if (signal?.aborted) throw error;
      if (error instanceof ApiError || error instanceof ProtocolError) throw error;
      throw new ApiError(0);
    } finally { clearTimeout(timer); }
    if (!response.ok) {
      let detail = typeof data?.detail === 'string' ? data.detail : '';
      if (this.#token) detail = detail.replaceAll(this.#token, '[凭据已隐藏]');
      throw new ApiError(response.status, detail.slice(0, 1200));
    }
    return outputSchema ? assertContract(outputSchema, data) : data;
  }

  async me(signal) {
    const data = await this.request('/me', { signal });
    if (typeof data?.id !== 'string' || typeof data?.is_admin !== 'boolean') throw new ProtocolError('me');
    return data;
  }

  async sessions(signal) {
    const data = await this.request('/sessions', { signal });
    if (!Array.isArray(data?.sessions)) throw new ProtocolError('sessions');
    return data.sessions.map((session) => assertContract('SessionSummary', session));
  }

  createSession(body, signal) {
    return this.request('/sessions', { method: 'POST', body, inputSchema: 'CreateSession', outputSchema: 'SessionSummary', signal });
  }

  snapshot(id, signal) {
    return this.request(`/sessions/${encodeURIComponent(id)}`, { outputSchema: 'Snapshot', signal });
  }

  submit(id, body, signal) {
    return this.request(`/sessions/${encodeURIComponent(id)}/inputs`, {
      method: 'POST', body, inputSchema: 'SubmitInput', outputSchema: 'Receipt', signal,
    });
  }

  control(id, body, signal) {
    return this.request(`/sessions/${encodeURIComponent(id)}/control`, {
      method: 'POST', body, inputSchema: 'ControlCommand', outputSchema: 'Receipt', signal,
    });
  }

  catalog(signal) { return this.request('/config', { outputSchema: 'ConfigView', signal }); }

  async configure(id, body, signal) {
    const data = await this.request(`/sessions/${encodeURIComponent(id)}/config`, {
      method: 'PUT', body, inputSchema: 'UpdateConfig', signal,
    });
    if (!Number.isInteger(data?.revision) || data.revision < 0) throw new ProtocolError('config revision');
    assertContract('SessionSettings', data.settings);
    return data;
  }

  approve(id, approvalId, body, signal) {
    return this.request(`/sessions/${encodeURIComponent(id)}/approvals/${encodeURIComponent(approvalId)}`, {
      method: 'POST', body, inputSchema: 'ApprovalCommand', outputSchema: 'ApprovalView', signal,
    });
  }

  subscribe(snapshot, callbacks = {}) {
    const url = new URL(`${this.origin}/api/v1/sessions/${encodeURIComponent(snapshot.session_id)}/events`);
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    const socket = new this.WebSocketImpl(url.href);
    socket.onopen = () => {
      const frame = assertContract('Subscribe', {
        type: 'subscribe', token: this.#token, epoch: snapshot.epoch, after_seq: snapshot.seq,
      });
      socket.send(JSON.stringify(frame));
      callbacks.open?.();
    };
    socket.onmessage = (event) => {
      let value;
      try { value = JSON.parse(event.data); } catch { callbacks.invalid?.(); return; }
      callbacks.event?.(value);
    };
    socket.onerror = () => callbacks.error?.();
    socket.onclose = (event) => callbacks.close?.(event);
    return socket;
  }
}
