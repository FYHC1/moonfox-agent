import test from 'node:test';
import assert from 'node:assert/strict';
import fixtures from '../src/generated/fixtures.json' with { type: 'json' };
import { SessionStream } from '../src/stream.js';
import { ApiError } from '../src/client.js';

const clone = structuredClone;
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const snap = (id = 'session-demo') => ({ ...clone(fixtures.snapshot), session_id: id, session: { ...clone(fixtures.snapshot.session), id } });
const fakeTimers = () => ({ tasks: new Map(), next: 0, setTimeout(fn, delay) { this.tasks.set(++this.next, { fn, delay }); return this.next; }, clearTimeout(id) { this.tasks.delete(id); }, run() { const tasks = [...this.tasks.values()]; this.tasks.clear(); tasks.forEach((task) => task.fn()); } });
const socket = () => ({ closed: false, close() { this.closed = true; } });
const tick = () => new Promise((resolve) => setImmediate(resolve));

test('late snapshot from previous selection is ignored and cannot start old subscription', async () => {
  const first = deferred(); const second = deferred(); const subscribed = []; const states = [];
  const api = { snapshot: (id) => id === 'a' ? first.promise : second.promise, subscribe: (snapshot) => { subscribed.push(snapshot.session_id); return socket(); } };
  const stream = new SessionStream(api, { state: (state) => states.push(state.sessionId) });
  const a = stream.select('a'); const b = stream.select('b');
  second.resolve(snap('b')); await b; first.resolve(snap('a')); await a;
  assert.deepEqual(subscribed, ['b']); assert.deepEqual(states, ['b']); stream.dispose();
});

test('refresh closes stream before fetching snapshot; a stale frame cannot overwrite it', async () => {
  let callbacks; let transport; const later = deferred(); let requests = 0;
  const api = { snapshot: () => ++requests === 1 ? Promise.resolve(snap()) : later.promise,
    subscribe: (snapshot, handlers) => { callbacks = handlers; transport = socket(); return transport; } };
  const stream = new SessionStream(api);
  await stream.select('session-demo'); const oldCallbacks = callbacks; const oldSocket = transport;
  callbacks.event(fixtures.events[0]); assert.equal(stream.state.seq, 1);
  const refreshing = stream.reload(); assert.equal(oldSocket.closed, true);
  oldCallbacks.event(fixtures.events[1]); assert.equal(stream.state.seq, 1);
  const updated = snap(); updated.seq = 20; later.resolve(updated); await refreshing;
  oldCallbacks.event(fixtures.events[2]); assert.equal(stream.state.seq, 20); stream.dispose();
});

test('gap and resync close old transport and only schedule snapshot recovery', async () => {
  const timers = fakeTimers(); let callbacks; let requests = 0; let transport;
  const api = { snapshot: async () => { requests++; return snap(); }, subscribe: (snapshot, handlers) => { callbacks = handlers; transport = socket(); return transport; } };
  const stream = new SessionStream(api, {}, timers);
  await stream.select('session-demo'); callbacks.event(fixtures.events.find((event) => event.seq === 5));
  assert.equal(transport.closed, true); assert.equal(timers.tasks.size, 1); assert.equal(stream.state.seq, 0);
  timers.run(); await tick(); assert.equal(requests, 2); stream.dispose(); assert.equal(timers.tasks.size, 0);
});

test('run_finished refreshes snapshot and list, no input or control replay exists', async () => {
  let callbacks; let refreshes = 0; let requests = 0;
  const api = { snapshot: async () => { requests++; return snap(); }, subscribe: (snapshot, handlers) => { callbacks = handlers; return socket(); } };
  const stream = new SessionStream(api, { refresh: () => refreshes++ });
  await stream.select('session-demo');
  for (const event of fixtures.events.filter((event) => event.seq <= 8 && !['heartbeat', 'resync_required'].includes(event.type))) callbacks.event(event);
  await tick(); assert.equal(refreshes, 1); assert.equal(requests, 2); stream.dispose();
});

test('unauthorized snapshot stops automatic reconnect and requests authentication', async () => {
  const timers = fakeTimers(); let unauthorized = 0;
  const api = { snapshot: async () => { throw new ApiError(401); } };
  const stream = new SessionStream(api, { unauthorized: () => unauthorized++ }, timers);
  await stream.select('s'); assert.equal(unauthorized, 1); assert.equal(timers.tasks.size, 0); stream.dispose();
});

test('blocked WS Origin/permissions surfaces explicit action instead of retry loop', async () => {
  const timers = fakeTimers(); let callbacks; const statuses = [];
  const stream = new SessionStream({ snapshot: async () => snap(), subscribe: (snapshot, handlers) => { callbacks = handlers; return socket(); } }, { status: (status) => statuses.push(status) }, timers);
  await stream.select('session-demo'); callbacks.close({ code: 1008 });
  assert.equal(statuses.at(-1), 'blocked'); assert.equal(timers.tasks.size, 0); stream.dispose();
});

test('switch and dispose close ONLY socket; timers and pending snapshot abort are disposed', async () => {
  const timers = fakeTimers(); let signals = []; let transports = [];
  const api = { snapshot: async (id, signal) => { signals.push(signal); return snap(id); },
    subscribe: () => { const transport = socket(); transports.push(transport); return transport; },
    control: () => assert.fail('must never cancel on navigation') };
  const stream = new SessionStream(api, {}, timers);
  await stream.select('a'); await stream.select('b');
  assert.equal(transports[0].closed, true); assert.equal(signals[0].aborted, true);
  stream.schedule('offline'); assert.equal(timers.tasks.size, 1); stream.dispose();
  assert.equal(timers.tasks.size, 0); assert.equal(transports.at(-1).closed, true); assert.equal(stream.activeId, null);
});

test('connect timeout and missing heartbeat recover only the subscription', async () => {
  const timers = fakeTimers(); let callbacks; let requests = 0;
  const api = { snapshot: async () => { requests++; return snap(); }, subscribe: (snapshot, handlers) => { callbacks = handlers; return socket(); } };
  const stream = new SessionStream(api, {}, timers);
  await stream.select('session-demo');
  assert.equal([...timers.tasks.values()][0].delay, 10000);
  callbacks.open(); assert.equal([...timers.tasks.values()][0].delay, 45000);
  callbacks.event(fixtures.events.find((event) => event.type === 'heartbeat'));
  assert.equal(stream.state.seq, 0); assert.equal(timers.tasks.size, 1);
  timers.run(); assert.equal(stream.socket, null); timers.run(); await tick();
  assert.equal(requests, 2); stream.dispose(); assert.equal(timers.tasks.size, 0);
});
