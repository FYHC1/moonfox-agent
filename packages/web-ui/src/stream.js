import { fromSnapshot, projectEvent } from './state.js';
import { ApiError } from './client.js';

export class SessionStream {
  constructor(api, callbacks = {}, timers = globalThis) {
    this.api = api;
    this.callbacks = callbacks;
    this.timers = timers;
    this.generation = 0;
    this.activeId = null;
    this.state = null;
    this.attempt = 0;
    this.disposed = false;
  }

  armWatchdog(delay, reason) {
    if (this.watchdog != null) this.timers.clearTimeout(this.watchdog);
    const generation = this.generation;
    this.watchdog = this.timers.setTimeout(() => {
      this.watchdog = null;
      if (!this.disposed && generation === this.generation) this.schedule(reason);
    }, delay);
  }

  closeTransport() {
    if (this.timer != null) this.timers.clearTimeout(this.timer);
    if (this.watchdog != null) this.timers.clearTimeout(this.watchdog);
    this.timer = this.watchdog = null;
    this.abort?.abort();
    this.abort = null;
    if (this.socket) {
      this.socket.onopen = this.socket.onmessage = this.socket.onerror = this.socket.onclose = null;
      this.socket.close();
      this.socket = null;
    }
  }

  async select(id, reason = 'select') {
    if (this.disposed) return null;
    const changed = id !== this.activeId;
    const generation = ++this.generation;
    this.closeTransport();
    this.activeId = id;
    if (changed) { this.state = null; this.attempt = 0; }
    this.callbacks.status?.(reason === 'select' ? 'loading' : 'syncing', reason);
    this.abort = new AbortController();
    const current = () => !this.disposed && generation === this.generation && id === this.activeId;
    try {
      const snapshot = await this.api.snapshot(id, this.abort.signal);
      if (!current()) return null;
      if (snapshot.session_id !== id || snapshot.session.id !== id) throw new Error('快照不属于当前会话。');
      this.state = fromSnapshot(snapshot);
      this.callbacks.state?.(this.state, 'snapshot');
      this.callbacks.status?.('connecting', '正在订阅会话事件');
      this.armWatchdog(10000, 'WebSocket 建立超时，将重新核对连接');
      this.socket = this.api.subscribe(snapshot, {
        open: () => {
          if (!current()) return;
          this.attempt = 0;
          this.armWatchdog(45000, '心跳超时，将重新获取快照');
          this.callbacks.status?.('live', '实时连接正常');
        },
        event: (event) => {
          if (!current()) return;
          this.armWatchdog(45000, '心跳超时，将重新获取快照');
          const result = projectEvent(this.state, event);
          if (result.state !== this.state) {
            this.state = result.state;
            this.callbacks.state?.(this.state, event.type);
          }
          if (result.effect === 'resync') this.schedule(result.reason, 200);
          if (result.effect === 'refresh') {
            this.callbacks.refresh?.(event.type);
            void this.select(id, 'refresh');
          }
        },
        invalid: () => { if (current()) this.schedule('收到无法解析的事件', 500); },
        close: (event) => {
          if (!current()) return;
          if ([1008, 4401, 4403].includes(event.code)) {
            ++this.generation;
            this.closeTransport();
            this.callbacks.status?.('blocked', '订阅被拒绝，请检查令牌权限与服务端 Origin allowlist。');
            return;
          }
          this.schedule('连接已断开，将重新获取快照');
        },
        // close is the retry signal; errors alone must not create a second subscription.
        error: () => { if (current()) this.callbacks.status?.('reconnecting', '实时连接暂时中断'); },
      });
      return snapshot;
    } catch (error) {
      if (!current() || this.abort?.signal.aborted) return null;
      this.closeTransport();
      if (error instanceof ApiError && error.status === 401) {
        this.callbacks.unauthorized?.(error);
        return null;
      }
      this.callbacks.error?.(error);
      if (['network', 'server'].includes(error.kind)) this.schedule(error.message);
      else this.callbacks.status?.('error', error.message);
      return null;
    }
  }

  schedule(reason, delay) {
    if (this.disposed || !this.activeId) return;
    // Invalidate the old stream now; a late frame cannot mutate the next snapshot.
    ++this.generation;
    this.closeTransport();
    this.callbacks.status?.('reconnecting', reason);
    const wait = delay ?? Math.min(15000, 800 * 2 ** Math.min(this.attempt++, 5));
    this.timer = this.timers.setTimeout(() => {
      this.timer = null;
      void this.select(this.activeId, 'reconnect');
    }, wait);
  }

  reload(reason = 'manual') { return this.activeId ? this.select(this.activeId, reason) : Promise.resolve(null); }

  dispose() {
    this.disposed = true;
    ++this.generation;
    this.closeTransport();
    this.activeId = null;
    this.state = null;
  }
}
