// UI acceptance fixture only. Loopback/in-memory; never imported by the production build.
import http from 'node:http';
import { createHash, randomUUID } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { assertContract, assertEvent } from '../src/protocol.js';

const fixtures = JSON.parse(await readFile(new URL('../src/generated/fixtures.json', import.meta.url), 'utf8'));
const token = 'ui-fixture-token'; // NOT a real credential.
const sessions = new Map(), subscribers = new Map(), buffers = new Map(), idempotency = new Map(), timers = new Map();
let failure = null, configConflict = false;
const clone = structuredClone;
const origins = ['http://127.0.0.1:5173', 'http://localhost:5173'];
function makeSession(id, title, mode = 'empty') {
  const snapshot = clone(fixtures.snapshot);
  snapshot.session_id = snapshot.session.id = id;
  snapshot.session.title = title;
  snapshot.session.created_at = snapshot.session.updated_at = Date.now();
  snapshot.capabilities = ['prompt', 'cancel', 'continue', 'configure', 'approve'];
  snapshot.settings.allowed_tools = ['echo']; snapshot.settings.approval_tools = ['echo'];
  snapshot.extensions = [{ id: 'fixture-notes', version: '1.0.0', scope: 'session', enabled: false, capabilities: ['test-only'] }];
  if (mode === 'history') {
    snapshot.messages = [
      { ...clone(fixtures.events[1].payload.message), text: '【UI 夹具】历史输入', id: `${id}-user` },
      { ...clone(fixtures.events[6].payload.message), id: `${id}-assistant`, text: '【UI 夹具 / 非真实 AI】固定工具说明。', tool_calls: [{ id: 'call-fixture', name: 'echo' }] },
      { ...clone(fixtures.events[6].payload.message), id: `${id}-tool`, role: 'toolResult', tool_name: 'echo', text: '{"result":"<img src=x onerror=alert(1)>"}' },
    ];
    snapshot.inputs = [{ ...clone(fixtures.events[7].payload.input), status: 'limit_reached', id: `${id}-input` }];
    snapshot.session.status = 'idle';
  }
  if (mode === 'qq') { snapshot.session.domain = 'qq'; snapshot.capabilities = []; }
  sessions.set(id, snapshot); buffers.set(id, []); assertContract('Snapshot', snapshot); return snapshot;
}
makeSession('fixture-new', '新思路 · UI 夹具');
makeSession('fixture-history', '工具与边界 · UI 夹具', 'history');
makeSession('fixture-qq', 'QQ 来源 · 只读夹具', 'qq');
const host = { defaults: clone(fixtures.snapshot.settings), max_concurrent_requests: 2, approval_timeout_seconds: 60, event_window: 256, subscriber_buffer: 128, max_pending_inputs: 100 };
const catalog = () => assertContract('ConfigView', { revision: 0, settings: host, models: [{ id: 'demo', name: 'Demo · 离线演示' }], tools: ['echo'], extensions: [{ id: 'fixture-notes', version: '1.0.0', capabilities: ['test-only'] }] });
function updateItem(list, value) { const at = list.findIndex(item => item.id === value.id); if (at < 0) list.push(clone(value)); else list[at] = clone(value); }
function project(snapshot, { seq, payload, type }) {
  snapshot.seq = seq;
  if (type === 'message_start' || type === 'message_end') updateItem(snapshot.messages, payload.message);
  if (type === 'text_delta') snapshot.messages.find(message => message.id === payload.message_id).text += payload.delta;
  if (['input_received', 'run_started', 'run_finished'].includes(type)) updateItem(snapshot.inputs, payload.input);
  if (type.startsWith('approval_')) updateItem(snapshot.approvals, payload.approval);
  if (type === 'run_started') snapshot.session.status = 'running';
  if (type === 'run_finished') { snapshot.session.status = 'idle'; snapshot.session.pending_count = 0; }
  if (type === 'input_received') snapshot.session.pending_count++;
}
function frame(value, opcode = 1) {
  const body = Buffer.from(typeof value === 'string' ? value : JSON.stringify(value));
  let head;
  if (body.length < 126) head = Buffer.from([0x80 | opcode, body.length]);
  else { head = Buffer.alloc(4); head[0] = 0x80 | opcode; head[1] = 126; head.writeUInt16BE(body.length, 2); }
  return Buffer.concat([head, body]);
}
function emit(id, type, payload) {
  const snapshot = sessions.get(id);
  const event = { protocol_version: 1, epoch: snapshot.epoch, session_id: id, seq: snapshot.seq + 1, type, payload };
  assertEvent(event); project(snapshot, event); buffers.get(id).push(event);
  for (const socket of subscribers.get(id) ?? []) socket.write(frame(event));
}
function schedule(id, fn, delay) { const timer = setTimeout(fn, delay); if (!timers.has(id)) timers.set(id, []); timers.get(id).push(timer); }
function stop(id) { for (const timer of timers.get(id) ?? []) clearTimeout(timer); timers.delete(id); }
function run(id, input, text) {
  input = { ...input, run_id: randomUUID(), status: 'running' };
  const message = { ...clone(fixtures.events[3].payload.message), id: randomUUID(), timestamp: Date.now() };
  schedule(id, () => emit(id, 'run_started', { input, status: 'running' }), 250);
  schedule(id, () => emit(id, 'message_start', { message }), 500);
  const chunks = ['【UI 夹具 / 非真实 AI】\n', '已收到输入。', '\n固定文本验证流式拼接和消息替换。'];
  chunks.forEach((delta, index) => schedule(id, () => emit(id, 'text_delta', { message_id: message.id, delta }), 800 + index * 400));
  schedule(id, () => {
    emit(id, 'message_end', { message: { ...message, text: chunks.join(''), status: 'complete' } });
    const status = text.endsWith('limit') ? 'limit_reached' : text.endsWith('error') ? 'error' : 'completed';
    emit(id, 'run_finished', { input: { ...input, status, requests_used: 1, tokens_used: 34 }, status });
  }, 2200);
}
const server = http.createServer(async (req, res) => {
  if (origins.includes(req.headers.origin)) res.setHeader('Access-Control-Allow-Origin', req.headers.origin);
  res.setHeader('Access-Control-Allow-Headers', 'Authorization,Content-Type');
  res.setHeader('Access-Control-Allow-Methods', 'GET,POST,PUT,OPTIONS');
  if (req.method === 'OPTIONS') { res.writeHead(204); res.end(); return; }
  const send = (value, status = 200, schema) => { if (schema) assertContract(schema, value); res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }); res.end(JSON.stringify(value)); };
  try {
    let body = ''; for await (const chunk of req) { body += chunk; if (body.length > 131072) throw Error('fixture body too large'); }
    body = body ? JSON.parse(body) : null;
    const url = new URL(req.url, 'http://127.0.0.1:8777');
    if (url.pathname === '/test/health') return send({ status: 'fixture-only' });
    if (url.pathname === '/test/fail') { failure = body; return send({ ok: true }); }
    if (url.pathname === '/test/conflict') { configConflict = true; return send({ ok: true }); }
    if (url.pathname === '/test/approval') {
      const id = body.id; const s = sessions.get(id);
      const approval = { id: randomUUID(), session_id: id, input_id: s.inputs.at(-1)?.id ?? 'fixture-input', tool_name: 'echo', arguments: { text: '<img src=x onerror=alert(1)>\n【UI 夹具】' }, status: 'pending', expires_at: Date.now() + (body.expires ?? 60000) };
      emit(id, 'approval_requested', { approval }); return send(approval);
    }
    if (url.pathname === '/test/resync') {
      const id = body.id; const s = sessions.get(id);
      for (const socket of subscribers.get(id) ?? []) socket.write(frame({ protocol_version: 1, epoch: s.epoch, session_id: id, seq: s.seq, type: 'resync_required', payload: { reason: 'fixture-resync' } }));
      return send({ ok: true });
    }
    if (url.pathname === '/test/long') {
      const s = sessions.get(body.id);
      s.session.title = '非常长的会话标题'.repeat(10);
      emit(s.session_id, 'message_end', { message: { ...clone(fixtures.events[6].payload.message), id: randomUUID(), text: '<script>alert(1)</script>' + 'long-unbroken-string-'.repeat(50) } });
      return send({ ok: true });
    }
    if (req.headers.authorization !== `Bearer ${token}`) return send({ error: 'unauthorized', detail: 'UI fixture authentication rejected' }, 401);
    if (failure && url.pathname.includes(failure.path)) {
      const fail = failure; if (!fail.persistent) failure = null;
      if (fail.status === 0) { res.writeHead(200, { 'Content-Type': 'application/json', 'Content-Length': 100 }); res.write('{'); setTimeout(() => res.destroy(), 20); }
      else send({ error: 'fixture-error', detail: '【UI 夹具】预期错误' }, fail.status);
      return;
    }
    const parts = url.pathname.split('/').filter(Boolean), id = parts[3], snapshot = sessions.get(id);
    if (url.pathname === '/api/v1/me') return send({ id: 'fixture-admin', is_admin: true });
    if (url.pathname === '/api/v1/config') return send(catalog());
    if (url.pathname === '/api/v1/sessions' && req.method === 'GET') return send({ sessions: [...sessions.values()].map(s => s.session) });
    if (url.pathname === '/api/v1/sessions' && req.method === 'POST') { assertContract('CreateSession', body); const s = makeSession(randomUUID(), body.title); return send(s.session, 201, 'SessionSummary'); }
    if (!snapshot) return send({ error: 'not-found', detail: 'fixture missing' }, 404);
    if (parts.length === 4 && req.method === 'GET') return send(snapshot, 200, 'Snapshot');
    if (parts[4] === 'inputs') {
      assertContract('SubmitInput', body); const key = `${id}:${body.idempotency_key}`;
      if (idempotency.has(key)) return send(idempotency.get(key), 202, 'Receipt');
      const input = { ...clone(fixtures.events[0].payload.input), id: randomUUID(), request_id: body.request_id, created_at: Date.now() }; input.logical_id = input.id;
      const receipt = { request_id: body.request_id, session_id: id, input_id: input.id, run_id: null, status: 'queued' };
      idempotency.set(key, receipt); send(receipt, 202, 'Receipt');
      emit(id, 'input_received', { input });
      emit(id, 'message_end', { message: { ...clone(fixtures.events[1].payload.message), id: randomUUID(), text: body.text, timestamp: Date.now() } });
      run(id, input, body.text); return;
    }
    if (parts[4] === 'control') {
      assertContract('ControlCommand', body);
      send({ request_id: body.request_id, session_id: id, input_id: body.input_id ?? null, run_id: null, status: 'accepted' }, 202, 'Receipt');
      if (body.action === 'cancel') { stop(id); const input = snapshot.inputs.at(-1); if (input) emit(id, 'run_finished', { input: { ...input, status: 'aborted' }, status: 'aborted' }); }
      else if (body.action === 'continue') { const input = snapshot.inputs.find(i => i.id === body.input_id); if (input) run(id, input, 'continue'); }
      return;
    }
    if (parts[4] === 'config') {
      assertContract('UpdateConfig', body);
      if (configConflict) { configConflict = false; snapshot.config_revision++; return send({ error: 'revision-conflict', detail: '其他客户端已更新修订' }, 409); }
      if (body.expected_revision !== snapshot.config_revision) return send({ error: 'revision-conflict', detail: 'revision mismatch' }, 409);
      snapshot.settings = clone(body.settings); snapshot.config_revision++; snapshot.session.model_id = body.settings.model_id;
      send({ revision: snapshot.config_revision, settings: snapshot.settings }); emit(id, 'config_changed', {}); return;
    }
    if (parts[4] === 'approvals') {
      assertContract('ApprovalCommand', body); const approval = snapshot.approvals.find(a => a.id === parts[5]);
      if (!approval || approval.status !== 'pending' || approval.expires_at <= Date.now()) return send({ error: 'conflict', detail: 'approval expired or resolved' }, 409);
      approval.status = body.decision === 'approve' ? 'approved' : 'denied'; send(approval, 200, 'ApprovalView'); emit(id, 'approval_resolved', { approval }); return;
    }
    send({ error: 'not-found', detail: 'fixture endpoint not implemented' }, 404);
  } catch (error) { send({ error: 'fixture-contract', detail: error.message }, 422); }
});
server.on('upgrade', (req, socket) => {
  const id = req.url.split('/')[4], snapshot = sessions.get(id);
  if (!snapshot || !origins.includes(req.headers.origin)) { socket.end('HTTP/1.1 403 Forbidden\r\n\r\n'); return; }
  const accept = createHash('sha1').update(req.headers['sec-websocket-key'] + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest('base64');
  socket.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`);
  let pending = Buffer.alloc(0), authenticated = false;
  const timeout = setTimeout(() => socket.destroy(), 5000);
  socket.on('data', chunk => {
    pending = Buffer.concat([pending, chunk]);
    while (pending.length >= 2) {
      const opcode = pending[0] & 15; let length = pending[1] & 127, offset = 2;
      if (length === 126) { if (pending.length < 4) return; length = pending.readUInt16BE(2); offset = 4; }
      if (length === 127) { socket.destroy(); return; }
      const masked = Boolean(pending[1] & 128), total = offset + (masked ? 4 : 0) + length;
      if (pending.length < total) return;
      const mask = masked ? pending.subarray(offset, offset + 4) : null; if (masked) offset += 4;
      const data = Buffer.from(pending.subarray(offset, offset + length)); pending = pending.subarray(total);
      if (masked) for (let i = 0; i < length; i++) data[i] ^= mask[i % 4];
      if (opcode === 8) { socket.end(frame('', 8)); return; }
      if (opcode !== 1) continue;
      try {
        const value = JSON.parse(data.toString()); assertContract('Subscribe', value);
        if (authenticated || value.token !== token) { socket.destroy(); return; }
        authenticated = true; clearTimeout(timeout);
        if (!subscribers.has(id)) subscribers.set(id, new Set()); subscribers.get(id).add(socket);
        if (value.epoch !== snapshot.epoch || value.after_seq > snapshot.seq) socket.write(frame({ protocol_version: 1, epoch: snapshot.epoch, session_id: id, seq: snapshot.seq, type: 'resync_required', payload: { reason: 'fixture-watermark' } }));
        else for (const event of buffers.get(id).filter(e => e.seq > value.after_seq)) socket.write(frame(event));
      } catch { socket.destroy(); }
    }
  });
  const clear = () => { clearTimeout(timeout); subscribers.get(id)?.delete(socket); };
  socket.on('close', clear); socket.on('error', clear);
});
server.listen(8777, '127.0.0.1', () => console.log('UI TEST FIXTURE ONLY: http://127.0.0.1:8777'));
