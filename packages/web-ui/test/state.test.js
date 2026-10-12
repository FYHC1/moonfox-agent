import test from 'node:test';
import assert from 'node:assert/strict';
import fixtures from '../src/generated/fixtures.json' with { type: 'json' };
import { fromSnapshot, projectEvent, pendingSubmission, resumableInput, createDraft, submissionFailure, acceptApproval, pendingInputs, activeInput, executionIdle } from '../src/state.js';
import { text } from '../src/dom.js';

const snapshot = fixtures.snapshot;
const events = fixtures.events.filter((event) => !['heartbeat', 'resync_required'].includes(event.type));
const through = (seq) => events.filter((event) => event.seq <= seq).reduce((state, event) => projectEvent(state, event).state, fromSnapshot(snapshot));

test('snapshot import is independent of source, preserves watermark and public state', () => {
  const state = fromSnapshot(snapshot);
  assert.equal(state.epoch, snapshot.epoch); assert.equal(state.seq, snapshot.seq);
  state.settings.model_id = 'edited';
  assert.equal(snapshot.settings.model_id, 'demo');
  assert.deepEqual(state.streamingIds, []);
});

test('stable message_id receives deltas, then message_end replaces instead of appending', () => {
  let state = through(4);
  for (const event of events.filter((item) => item.type === 'text_delta')) state = projectEvent(state, event).state;
  assert.equal(state.messages.find((message) => message.id === 'message-assistant').text, '你好，世界！');
  const end = events.find((event) => event.type === 'message_end' && event.payload.message.role === 'assistant');
  state = projectEvent(state, end).state;
  assert.equal(state.messages.length, 2);
  assert.equal(state.streamingIds.length, 0);
  assert.equal(projectEvent(state, end).state, state);
  const again = projectEvent(state, { ...end, seq: state.seq + 1 });
  assert.equal(again.state.messages.length, 2);
  assert.equal(again.state.messages.at(-1).text, end.payload.message.text);
});

test('duplicates and old/out-of-order frames never append text twice', () => {
  const state = through(6);
  for (const event of events.filter((event) => event.seq <= 6)) assert.equal(projectEvent(state, event).state, state);
});

test('a sequence gap resyncs without applying later fragments', () => {
  const state = through(4);
  const result = projectEvent(state, events.find((event) => event.seq === 6));
  assert.equal(result.effect, 'resync'); assert.equal(result.state, state);
});

test('epoch mismatch resyncs, foreign-session frames are ignored', () => {
  const state = fromSnapshot(snapshot);
  const event = events[0];
  assert.equal(projectEvent(state, { ...event, epoch: 'new-epoch' }).effect, 'resync');
  assert.equal(projectEvent(state, { ...event, session_id: 'other' }).state, state);
  assert.equal(projectEvent(state, { ...event, session_id: 'other' }).effect, null);
});

test('heartbeat never advances seq; resync_required is honored even at duplicate seq', () => {
  const state = through(8);
  const heartbeat = fixtures.events.find((event) => event.type === 'heartbeat');
  assert.equal(projectEvent(state, { ...heartbeat, seq: 999 }).state, state);
  assert.equal(projectEvent(state, fixtures.events.find((event) => event.type === 'resync_required')).effect, 'resync');
});

test('missing message_start triggers resync instead of constructing a fake message', () => {
  const state = fromSnapshot(snapshot);
  const delta = fixtures.events.find((event) => event.type === 'text_delta');
  const result = projectEvent(state, { ...delta, seq: 1 });
  assert.equal(result.effect, 'resync'); assert.deepEqual(result.state.messages, []);
});

test('public delta does not depend on partial; private reasoning is not projected', () => {
  const state = through(4);
  const delta = fixtures.events.find((event) => event.type === 'text_delta');
  assert.equal('partial' in delta.payload, false);
  const result = projectEvent(state, { ...delta, payload: { ...delta.payload, partial: { text: 'private' }, reasoning: 'secret' } });
  assert.equal(result.state.messages.at(-1).text, delta.payload.delta);
  assert.equal(JSON.stringify(result.state).includes('private'), false);
  assert.equal(JSON.stringify(result.state).includes('secret'), false);
});

test('finished event requests authoritative snapshot/list refresh without inference retry', () => {
  const finished = events.find((event) => event.type === 'run_finished');
  const result = projectEvent(through(7), finished);
  assert.equal(result.effect, 'refresh');
  assert.equal(result.state.inputs.at(-1).status, 'completed');
});

test('only the LAST limited/error/aborted input offers explicit continuation', () => {
  const state = through(8);
  for (const status of ['aborted', 'error', 'limit_reached']) {
    const next = { ...state, inputs: [{ ...state.inputs.at(-1), status }] };
    assert.equal(resumableInput(next).status, status);
  }
  for (const status of ['completed', 'unknown', 'queued', 'running']) {
    assert.equal(resumableInput({ ...state, inputs: [{ ...state.inputs.at(-1), status }] }), null);
  }
});

test('XSS strings remain plain text through reducer and DOM text boundary', () => {
  const xss = '<img src=x onerror=alert(1)><script>steal()</script>';
  const state = through(4);
  const delta = fixtures.events.find((event) => event.type === 'text_delta');
  const projected = projectEvent(state, { ...delta, payload: { ...delta.payload, delta: xss } }).state;
  assert.equal(projected.messages.at(-1).text, xss);
  const node = text(xss, { createTextNode: (value) => ({ nodeType: 3, textContent: value }) });
  assert.deepEqual(node, { nodeType: 3, textContent: xss });
});

test('submission retries retain DTO identity; errors classify manual retry safely', () => {
  let next = 0;
  const pending = pendingSubmission('输入', () => `uuid-${++next}`);
  const before = JSON.stringify(pending);
  assert.equal(submissionFailure({ kind: 'network', message: 'offline' }).retryable, true);
  assert.equal(submissionFailure({ kind: 'unauthorized', message: 'auth' }).retryable, false);
  assert.equal(submissionFailure({ kind: 'conflict', message: 'CAS' }).retryable, false);
  assert.equal(JSON.stringify(pending), before);
  assert.deepEqual(Object.keys(pending).sort(), ['idempotency_key', 'request_id', 'text']);
});

test('settings draft retains original CAS revision and never mutates snapshot', () => {
  const draft = createDraft(snapshot.settings, 5); draft.settings.max_turns_per_run = 2; draft.dirty = true;
  assert.equal(draft.revision, 5); assert.equal(snapshot.settings.max_turns_per_run, 4);
});

test('approval HTTP response only merges into matching session and stable approval id', () => {
  const state = fromSnapshot(snapshot);
  const approval = { id: 'approval-test', session_id: state.sessionId, input_id: 'i', tool_name: 'echo', arguments: { text: '<b>safe</b>' }, status: 'approved', expires_at: 1700000000010 };
  const merged = acceptApproval(state, approval);
  assert.equal(acceptApproval(merged, approval).approvals.length, 1);
  assert.equal(acceptApproval(state, { ...approval, session_id: 'other' }), state);
});

test('malformed JSON event objects cannot silently stall the cursor', () => {
  const state = fromSnapshot(snapshot);
  assert.equal(projectEvent(state, null).effect, 'resync');
  assert.equal(projectEvent(state, {}).effect, 'resync');
});

test('queue/active counters derive from public inputs, not an invented run state machine', () => {
  const state = through(3);
  const active = state.inputs[0];
  const withQueue = { ...state, inputs: [active, { ...active, id: 'next', status: 'pending', requests_used: 0 }] };
  assert.equal(pendingInputs(withQueue).length, 1);
  assert.equal(activeInput(withQueue).id, active.id);
  assert.equal(executionIdle(withQueue), false);
  assert.equal(executionIdle(through(8)), true);
});
