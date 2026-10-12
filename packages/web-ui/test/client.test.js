import test from 'node:test';
import assert from 'node:assert/strict';
import fixtures from '../src/generated/fixtures.json' with { type: 'json' };
import { ApiClient, ApiError, normalizeOrigin } from '../src/client.js';
import { ProtocolError } from '../src/protocol.js';

const json = (value, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } });
const create = (fetchImpl, extra = {}) => new ApiClient({ origin: 'http://localhost:8765', token: 'memory-only-secret', fetchImpl, ...extra });

test('backend address permits only origins, never credentials/path/query/fragment', () => {
  assert.equal(normalizeOrigin('http://localhost:8765/'), 'http://localhost:8765');
  for (const value of ['javascript:alert(1)', 'https://user:secret@example.com', 'https://example.com/?token=s', 'https://example.com/#s', 'https://example.com/api']) assert.throws(() => normalizeOrigin(value));
});

test('REST authentication is in Bearer header, never URLs or cookies', async () => {
  let call;
  const api = create(async (url, init) => { call = { url, init }; return json({ id: 'admin', is_admin: true }); });
  await api.me();
  assert.equal(call.url, 'http://localhost:8765/api/v1/me');
  assert.equal(call.init.headers.Authorization, 'Bearer memory-only-secret');
  assert.equal(call.init.credentials, 'omit'); assert.equal(call.init.cache, 'no-store'); assert.equal(call.init.redirect, 'error');
  assert.equal(JSON.stringify(api).includes('memory-only-secret'), false);
  api.clearCredentials(); await api.me(); assert.equal(call.init.headers.Authorization, 'Bearer ');
});

test('401 unauthorized and 409 conflict surface typed user-facing states', async () => {
  for (const [status, kind] of [[401, 'unauthorized'], [403, 'forbidden'], [404, 'not-found'], [409, 'conflict'], [422, 'invalid'], [429, 'limit'], [503, 'server']]) {
    const api = create(async () => json({ error: kind, detail: `safe error ${status}` }, status));
    await assert.rejects(api.me(), (error) => error instanceof ApiError && error.kind === kind && error.status === status);
  }
});

test('error text is bounded and token-redacted; malformed success is protocol failure', async () => {
  const api = create(async () => json({ detail: 'Do not show memory-only-secret', error: 'error' }, 422));
  await assert.rejects(api.me(), (error) => !error.message.includes('memory-only-secret') && error.message.includes('[凭据已隐藏]'));
  await assert.rejects(create(async () => new Response('<html>proxy</html>')).me(), ProtocolError);
  await assert.rejects(create(async () => json({ id: 'admin' })).me(), ProtocolError);
});

test('network errors do not cause automatic POST retry', async () => {
  let calls = 0;
  const api = create(async () => { calls++; throw new TypeError('Failed to fetch'); });
  await assert.rejects(api.submit('s', { request_id: 'r', text: '你好', idempotency_key: 'k' }), (error) => error.kind === 'network');
  assert.equal(calls, 1);
});

test('same pending submission can be manually retried unchanged', async () => {
  const calls = [];
  const body = { request_id: 'r', text: '你好', idempotency_key: 'k' };
  const api = create(async (url, init) => {
    calls.push(init.body);
    if (calls.length === 1) throw new TypeError('offline');
    return json({ request_id: 'r', session_id: 's', input_id: 'i', run_id: null, status: 'queued' }, 202);
  });
  await assert.rejects(api.submit('s', body));
  const receipt = await api.submit('s', body);
  assert.equal(calls[0], calls[1]); assert.equal(receipt.status, 'queued');
});

test('client validates outgoing DTO and incoming list/snapshot without schema copies', async () => {
  let count = 0;
  const api = create(async () => { count++; return json(fixtures.snapshot); });
  await assert.rejects(api.submit('s', { text: '' }), ProtocolError); assert.equal(count, 0);
  assert.deepEqual(await api.snapshot('session-demo'), fixtures.snapshot);
  const list = create(async () => json({ sessions: [fixtures.snapshot.session] }));
  assert.deepEqual(await list.sessions(), [fixtures.snapshot.session]);
});

test('WS first frame includes token/epoch/watermark, URL does not contain credentials', () => {
  class Socket {
    constructor(url) { this.url = url; }
    send(data) { this.frame = JSON.parse(data); }
  }
  const api = create(null, { WebSocketImpl: Socket });
  const socket = api.subscribe(fixtures.snapshot);
  assert.equal(socket.url, 'ws://localhost:8765/api/v1/sessions/session-demo/events');
  assert.equal(socket.url.includes('secret'), false);
  socket.onopen();
  assert.deepEqual(socket.frame, { type: 'subscribe', token: 'memory-only-secret', epoch: fixtures.snapshot.epoch, after_seq: fixtures.snapshot.seq });
});

test('configuration uses PUT and exact expected_revision plus full settings', async () => {
  let call;
  const api = create(async (url, init) => { call = init; return json({ revision: 6, settings: fixtures.snapshot.settings }); });
  await api.configure('s', { request_id: 'r', expected_revision: 5, settings: fixtures.snapshot.settings });
  assert.equal(call.method, 'PUT');
  assert.deepEqual(JSON.parse(call.body).settings, fixtures.snapshot.settings);
  assert.equal(JSON.parse(call.body).expected_revision, 5);
});

test('native-style fetch is invoked with global binding, not ApiClient as this', async () => {
  const api = create(async function () { assert.equal(this, globalThis); return json({ id: 'admin', is_admin: true }); });
  await api.me();
});

test('interrupted response body is a retryable transport error, not JSON schema failure', async () => {
  const api = create(async () => ({ ok: true, status: 200, text: async () => { throw new TypeError('body interrupted'); } }));
  await assert.rejects(api.me(), (error) => error.kind === 'network');
});
