import test from 'node:test';
import assert from 'node:assert/strict';
import { Session, SessionId } from '@deepseek-ai/dsh-session';
import { createUserMessage } from '@deepseek-ai/dsh-llm';
import { ReviewPolicy } from './review_policy.mjs';

function clock() {
  let mono = 0, wall = Date.UTC(2026, 9, 29, 14);
  return { monotonic: () => mono, wall: () => wall,
    advance(seconds) { mono += seconds * 1000; wall += seconds * 1000; }, jump(milliseconds) { wall += milliseconds; } };
}
function commit(policy, session, message) {
  const event = session.append('user/message', message, { surfaceOp: 'append' });
  policy.observe(event);
}

test('elapsed uses the local monotonic clock while wall jumps only change displayed time', () => {
  const local = clock(), policy = new ReviewPolicy({}, local);
  local.advance(300); local.jump(-24 * 3600 * 1000);
  assert.equal(policy.snapshot().elapsed_seconds, 300);
  assert.equal(policy.notice(), undefined);
  local.jump(72 * 3600 * 1000);
  assert.equal(policy.snapshot().elapsed_seconds, 300);
  local.advance(300);
  const notice = policy.notice();
  assert.equal(notice.source.stage, 'remind');
  assert.match(notice.content[0].text, /600 seconds/);
  assert.match(notice.content[0].text, /Asia\/Shanghai/);
});

test('both profiles default to ten and twenty minute reminders without a hard stop', () => {
  for (const profile of ['standard', 'deep']) {
    const local = clock(), policy = new ReviewPolicy({ profile }, local), session = Session.create(SessionId(`policy-${profile}`));
    assert.equal(policy.notice(), undefined);
    local.advance(600); commit(policy, session, policy.notice());
    assert.equal(policy.notice(), undefined);
    local.advance(600); commit(policy, session, policy.notice());
    local.advance(3600);
    assert.equal(policy.notice(), undefined);
    assert.deepEqual(policy.snapshot().notified_stages, ['remind', 'conclude']);
    assert.equal(policy.snapshot().elapsed_seconds, 4800);
  }
});

test('crossing both thresholds produces only the strongest current reminder', () => {
  const local = clock(), policy = new ReviewPolicy({}, local), session = Session.create(SessionId('policy-crossing'));
  local.advance(1500);
  const message = policy.notice();
  assert.equal(message.source.stage, 'conclude'); commit(policy, session, message);
  assert.equal(policy.notice(), undefined);
  assert.deepEqual(policy.snapshot().notified_stages, ['conclude']);
});

test('zero disables each reminder independently', () => {
  const local = clock(); local.advance(5000);
  for (const settings of [{ remind_after_seconds: 0, conclude_after_seconds: 0 }, { remind_after_seconds: 0 }, { conclude_after_seconds: 0 }]) {
    const policy = new ReviewPolicy(settings, local);
    local.advance(1300);
    assert.equal(policy.notice()?.source.stage, settings.remind_after_seconds === 0 ? (settings.conclude_after_seconds === 0 ? undefined : 'conclude') : 'remind');
  }
});

test('log-only clock checkpoints do not enter model history and survive resume without downtime', () => {
  const local = clock(), policy = new ReviewPolicy({ initial_elapsed_seconds: 10 }, local), session = Session.create(SessionId('policy-original'));
  local.advance(620); commit(policy, session, policy.notice());
  policy.checkpoint(session);
  const visible = session.deriveMessages();
  assert.equal(visible.length, 1);
  assert.equal(session.log.filter(event => event.type === 'plugin:review/timing').length, 1);
  const restartClock = clock(); restartClock.jump(7 * 24 * 3600 * 1000);
  const resumed = new ReviewPolicy({ initial_elapsed_seconds: 5, resume_elapsed_seconds: 620 }, restartClock);
  resumed.restore(session.log);
  assert.equal(resumed.snapshot().elapsed_seconds, 635);
  assert.equal(resumed.notice(), undefined);
  restartClock.advance(565);
  assert.equal(resumed.notice().source.stage, 'conclude');
  assert.deepEqual(resumed.snapshot().notified_stages, ['remind']);
});

test('uncommitted notices and text spoofing cannot suppress future reminders', () => {
  const local = clock(), policy = new ReviewPolicy({}, local), session = Session.create(SessionId('policy-spoof'));
  session.append('user/message', createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text: JSON.stringify({ kind: 'review-runtime-clock', stage: 'conclude', elapsed_seconds: 50000 }) }] }), { surfaceOp: 'append' });
  policy.restore(session.log); local.advance(600);
  const notCommitted = policy.notice();
  assert.equal(notCommitted.source.stage, 'remind');
  assert.equal(policy.notice().source.stage, 'remind');
});

test('invalid clock configuration is rejected without a model request', () => {
  for (const settings of [{ remind_after_seconds: -1 }, { conclude_after_seconds: Infinity }, { profile: 'all' }, { remind_after_seconds: 600, conclude_after_seconds: 300 }])
    assert.throws(() => new ReviewPolicy(settings), TypeError);
  assert.throws(() => new ReviewPolicy({ timezone: 'invalid/timezone' }), RangeError);
});


test('host start time includes preparation and native resume keeps the original start', () => {
  const local = clock();
  const inferred = new ReviewPolicy({ initial_elapsed_seconds: 60 }, local);
  assert.equal(inferred.snapshot().started_utc, '2026-10-29T13:59:00.000Z');
  const supplied = new ReviewPolicy({ initial_elapsed_seconds: 60, started_utc: '2026-10-29T21:58:00+08:00' }, local);
  assert.equal(supplied.snapshot().started_utc, '2026-10-29T13:58:00.000Z');
  const session = Session.create(SessionId('policy-started')); supplied.checkpoint(session);
  const restarted = new ReviewPolicy({ started_utc: '2026-10-30T00:00:00Z' }, local);
  restarted.restore(session.log);
  assert.equal(restarted.snapshot().started_utc, '2026-10-29T13:58:00.000Z');
  assert.throws(() => new ReviewPolicy({ started_utc: '2026-10-29T14:00:00' }, local), TypeError);
});
