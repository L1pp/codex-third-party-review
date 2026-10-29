import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync, mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve, dirname, basename } from 'node:path';
import { Context } from '@deepseek-ai/cordis';
import LlmRuntime, { LlmAdapter, ReasoningEffortId, createUserMessage, createSystemMessage, createAssistantMessage, createToolResultMessage, ToolCallId } from '@deepseek-ai/dsh-llm';
import SessionStore, { Session, SessionId } from '@deepseek-ai/dsh-session';
import { PayloadTokenCounter, mountContext } from './context.mjs';
import { buildPayload } from './adapter.mjs';

const ASSETS = new URL('../assets/deepseek_v4_tokenizer/', import.meta.url);
const HAS_ASSETS = existsSync(new URL('tokenizer.json', ASSETS));
const ROUTE = { model: 'deepseek-v4.1-flash-1017', max_tokens: 256000, reasoning_effort: 'max', enable_thinking: true, context_window_tokens: 1000000 };
const SIGNAL = new AbortController().signal;
const user = text => createUserMessage({ content: [{ type: 'text', text }], source: { kind: 'user' } });
const packed = chunks => chunks.map((chunk, time) => ({ type: 'chunk', time, chunk }));
const assistant = content => createAssistantMessage({ content, source: { provider: 'review', model: ROUTE.model } });

class SummaryAdapter extends LlmAdapter {
  constructor(summary = 'Finding: verify src/calc.py:12 against the caller contract. Checked sources remain available; continue unresolved validation.', window = 1000000) { super(); this.window = window; this.defaultMaxTokens = 256000; this.summary = summary; this.requests = []; this.finish = 'stop'; }
  resolveModel(provider, model) { return Promise.resolve({ provider, id: model, name: model, context: { contextWindow: this.window }, defaultMaxTokens: this.defaultMaxTokens, reasoning: { efforts: ['none','max'].map(id => ({ id: ReasoningEffortId(id), name: id })) } }); }
  async *stream(options) {
    this.requests.push(options);
    const block = { type: 'text', text: this.summary };
    yield { type: 'block-start', index: 0, blockType: 'text' };
    yield { type: 'text-delta', index: 0, text: block.text };
    yield { type: 'block-end', index: 0, block };
    yield { type: 'finish', reason: { kind: this.finish } };
  }
}
async function setup(t, extra = {}) {
  const ctx = new Context();
  t.after(async () => { await ctx.fiber.dispose(); });
  await ctx.plugin(LlmRuntime);
  await ctx.plugin(SessionStore);
  const adapter = new SummaryAdapter(undefined, extra.testWindow || 1000000);
  if ('testDefaultMaxTokens' in extra) adapter.defaultMaxTokens = extra.testDefaultMaxTokens;
  ctx.llm.registerAdapter(['review'], adapter);
  const context = await mountContext(ctx, { route: ROUTE, contextManagement: { compaction_max_tokens: 2048 }, ...extra }, extra.testHost || {});
  const session = ctx.sessions.create(SessionId('context-test-' + Math.random()));
  const followups = [];
  const agent = { session, options: { provider: 'review', model: (extra.route || ROUTE).model }, followup: message => followups.push(message), runMaintenance: fn => fn(SIGNAL) };
  return { ctx, context, session, agent, adapter, followups };
}
function open(session, text = 'Original code-review task.\n'.repeat(100), route = ROUTE) {
  session.append('turn/start', { turn: 1 });
  session.append('system/message', { turn: 1, step: 1, message: createSystemMessage('Independent read-only review.') }, { surfaceOp: 'append' });
  session.append('user/message', user(text), { surfaceOp: 'append' });
  session.append('step/start', { turn: 1, step: 1 });
  session.append('request/header', { header: { config: { provider: 'review', model: route.model, ...(route.max_tokens === undefined ? {} : { maxTokens: route.max_tokens }) } }, reason: 'initial' });
}

test('native tokenizer and Jinja keep the existing precise template estimate', { skip: !HAS_ASSETS }, () => {
  const counter = new PayloadTokenCounter(ROUTE, ASSETS);
  assert.equal(counter.count({ messages: [{ role: 'system', content: '独立审阅' }, { role: 'user', content: 'Hello 你好。' }] }), 10);
  assert.equal(counter.text('代码审阅需要检查真实问题、证据、位置。不要把未读材料说成已检查。'), 21);
  assert.equal(counter.text('🙂👨‍👩‍👧‍👦🚀 e\u0301é \u200d \u00a0\u2028\u2029'), 25);
});

test('auto keeps the UTF-8 upper bound without native tokenizer assets', () => {
  const counter = new PayloadTokenCounter({ model: 'auto' }, '/absent/assets');
  const text = '中文🙂'.repeat(300), payload = { messages: [{ role: 'user', content: text }], tools: [] };
  assert.ok(counter.count(payload) > Buffer.byteLength(text));
});

test('official pruner rewrites large search results and preserves the original event', { skip: !HAS_ASSETS }, async t => {
  const { ctx, session, adapter } = await setup(t);
  open(session);
  const call = { type: 'tool-call', id: ToolCallId('search-1'), name: 'search_code', arguments: '{"query":"value"}' };
  session.append('assistant/message', { turn: 1, step: 1, message: assistant([call]), stream: [] }, { surfaceOp: 'append' });
  const text = '代码🙂value\n'.repeat(3000);
  const original = session.append('tool/result', { turn: 1, step: 1, message: createToolResultMessage({ callId: call.id, content: [{ type: 'text', text }], isError: false }) }, { surfaceOp: 'append' });
  const result = ctx.toolResultPruner.pruneSession(session);
  assert.equal(result.pruned.length, 1);
  assert.equal(session.eventAt(original.seq).data.message.content[0].text, text);
  assert.ok(session.surface.replaceGeneration > 0);
  assert.ok(session.snapshotEvents().some(event => event.type === 'compaction/prune'));
  assert.equal(adapter.requests.length, 0);
});

test('successful disjoint usage calibrates one meter and header changes invalidate it', { skip: !HAS_ASSETS }, async t => {
  const { context, session } = await setup(t);
  open(session, 'Small request.');
  session.append('assistant/message', { turn: 1, step: 1,
    message: assistant([{ type: 'text', text: 'Checked the caller.' }]), stream: [],
    usage: { inputTokens: 900, cacheReadTokens: 100, outputTokens: 50, reasoningTokens: 30 } }, { surfaceOp: 'append' });
  const priced = context.meter.measure(session);
  assert.equal(priced.totalTokens, 1050);
  assert.equal(priced.baseline.kind, 'usage');
  const changed = { config: { provider: 'review', model: ROUTE.model, maxTokens: 128000 } };
  assert.equal(context.meter.measure(session, changed).baseline.kind, 'estimated');
});

test('deadline progress becomes an official smaller checkpoint, without tool execution', { skip: !HAS_ASSETS }, async t => {
  const { context, session, agent, adapter } = await setup(t);
  open(session);
  const block = { type: 'reasoning', text: 'Check src/calc.py:12 and the matching caller before finalizing.\n'.repeat(100) };
  const attempt = session.append('assistant/attempt', { turn: 1, step: 1, stream: packed([
    { type: 'block-start', index: 0, blockType: 'reasoning' },
    { type: 'reasoning-delta', index: 0, text: block.text },
    { type: 'block-end', index: 0, block },
    { type: 'finish', reason: { kind: 'error', failure: { code: 'SERVER', message: 'provider deadline' } } },
  ]) });
  assert.equal(await context.recover(agent, { code: 'SERVER' }, { attempt, signal: SIGNAL }), true);
  assert.equal(adapter.requests.length, 1);
  assert.equal(adapter.requests[0].purpose, 'compaction');
  assert.ok(session.snapshotEvents().some(event => event.type === 'compaction/summary'));
  assert.ok(session.snapshotEvents().some(event => event.type === 'compaction/end'));
  assert.ok(!session.snapshotEvents().some(event => event.type === 'tool/start'));
  assert.ok(session.snapshotEvents().some(event => event.type === 'assistant/attempt'));
});

test('a truncated summary never replaces the original review surface', { skip: !HAS_ASSETS }, async t => {
  const { context, session, agent, adapter } = await setup(t);
  open(session);
  adapter.finish = 'max-tokens';
  const attempt = { stream: packed([{ type: 'block-start', index: 0, blockType: 'reasoning' }, { type: 'reasoning-delta', index: 0, text: 'Unfinished reasoning '.repeat(300) }, { type: 'block-end', index: 0, block: { type: 'reasoning', text: 'Unfinished reasoning '.repeat(300) } }, { type: 'finish', reason: { kind: 'error', failure: { code: 'SERVER', message: 'interrupted' } } }]) };
  const before = session.surface.replaceGeneration;
  await assert.rejects(context.recover(agent, { code: 'SERVER' }, { attempt, signal: SIGNAL }), /truncated/);
  assert.equal(session.surface.replaceGeneration, before);
  assert.ok(session.snapshotEvents().some(event => event.type === 'compaction/end' && event.data.error));
});

test('length repairs unexecuted tool calls, compacts, and schedules a fresh turn', { skip: !HAS_ASSETS }, async t => {
  const { context, session, agent, followups } = await setup(t);
  open(session);
  const call = { type: 'tool-call', id: ToolCallId('unfinished-read'), name: 'read_file', arguments: '{"path":' };
  const attempt = session.append('assistant/message', { turn: 1, step: 1,
    message: assistant([{ type: 'text', text: 'Current evidence from src/calc.py:12.\n'.repeat(200) }, call]),
    stream: [] }, { surfaceOp: 'append' });
  session.append('step/end', { turn: 1, step: 1 });
  assert.equal(await context.continueLength(agent, { attempt, signal: SIGNAL }), true);
  assert.equal(followups.length, 1);
  assert.ok(session.snapshotEvents().some(event => event.type === 'tool/result' && event.data.message.isError));
  assert.ok(!session.snapshotEvents().some(event => event.type === 'tool/start'));
});

test('legacy progress imports through the official idle compaction transaction', { skip: !HAS_ASSETS }, async t => {
  const { context, session, agent, adapter } = await setup(t);
  const notes = 'Existing finding at src/calc.py:12; verify unresolved caller contract.\n'.repeat(300);
  assert.equal(await context.recoverLegacy(agent, { internal_notes: notes, read_sources: [{ source_id: 'source-0001', path: 'src/calc.py', start_line: 1, end_line: 20 }] }), true);
  assert.equal(adapter.requests.length, 1);
  assert.ok(!session.snapshotEvents().some(event => event.type === 'request/header'));
  assert.ok(!session.snapshotEvents().some(event => event.type === 'assistant/message'));
  assert.ok(session.snapshotEvents().some(event => event.type === 'compaction/summary'));
});


test('meter reuses immutable node prices and same-surface measurements without hashes', { skip: !HAS_ASSETS }, async t => {
  const { context, session } = await setup(t);
  open(session);
  let encoded = 0;
  const original = context.meter.counter.count.bind(context.meter.counter);
  context.meter.counter.count = payload => { encoded += 1; return original(payload); };
  const first = context.meter.measure(session), count = encoded;
  const second = context.meter.measure(session);
  assert.equal(encoded, count);
  assert.equal(second.totalTokens, first.totalTokens);
  session.append('user/message', user('A newly admitted source observation.'), { surfaceOp: 'append' });
  assert.ok(context.meter.measure(session).totalTokens > first.totalTokens);
  assert.ok(encoded > count);
});

test('small legacy checkpoints remain directly admitted without an unnecessary model call', { skip: !HAS_ASSETS }, async t => {
  const { context, session, agent, adapter } = await setup(t);
  assert.equal(await context.recoverLegacy(agent, { prior_checkpoint: 'Check caller src/calc.py:12.', logs: 'PRIVATE_OPERATIONAL_SENTINEL' }), true);
  assert.equal(adapter.requests.length, 0);
  const text = JSON.stringify(session.snapshotEvents());
  assert.ok(text.includes('src/calc.py:12'));
  assert.ok(!text.includes('PRIVATE_OPERATIONAL_SENTINEL'));
});

test('auto provider usage cannot reduce the conservative byte-bound pressure', async t => {
  const auto = { model: 'auto', max_tokens: 131072, thinking_budget: 262144, combined_output_limit_parameter: 'max_completion_tokens', context_window_tokens: 1000000 };
  const { context, session } = await setup(t, { route: auto });
  session.append('turn/start', { turn: 1 });
  session.append('user/message', user('中文🙂'.repeat(100)), { surfaceOp: 'append' });
  session.append('step/start', { turn: 1, step: 1 });
  session.append('request/header', { header: { config: { provider: 'review', model: 'auto', maxTokens: 393216 } }, reason: 'initial' });
  session.append('assistant/message', { turn: 1, step: 1, message: createAssistantMessage({ content: [{ type: 'text', text: 'done' }], source: { provider: 'review', model: 'auto' } }), stream: [], usage: { inputTokens: 1, outputTokens: 1 } }, { surfaceOp: 'append' });
  const measured = context.meter.measure(session);
  assert.equal(measured.baseline.kind, 'estimated');
  assert.ok(measured.totalTokens > 1000);
});


test('first admitted request is compacted before HTTP without reducing configured output', { skip: !HAS_ASSETS }, async t => {
  const route = { ...ROUTE, max_tokens: 700, context_window_tokens: 2000 };
  const { context, session, agent, adapter } = await setup(t, { route, testWindow: 2000,
    contextManagement: { headroom_tokens: 128, compaction_max_tokens: 128 } });
  open(session, 'evidence '.repeat(1400), route);
  const before = context.meter.measure(session).totalTokens;
  assert.ok(before + 700 + 128 > 2000);
  assert.ok(before + 128 + 128 < 2000); // The actual summary can fit.
  assert.equal(await context.ensureRequest(agent, { signal: SIGNAL }), true);
  assert.ok(context.meter.measure(session).totalTokens + 700 + 128 <= 2000);
  assert.equal(session.requestHeader().config.maxTokens, 700);
  assert.equal(adapter.requests.length, 1);
  assert.equal(adapter.requests[0].purpose, 'compaction');
});

test('incompatible output reservation stops preflight without any model request', { skip: !HAS_ASSETS }, async t => {
  const { context, session, agent, adapter } = await setup(t, { testWindow: 2000 });
  open(session, 'Small request.');
  await assert.rejects(context.ensureRequest(agent, { signal: SIGNAL }), error => error.code === 'INVALID_CONTEXT_BUDGET');
  assert.equal(adapter.requests.length, 0);
});


test('cold resumed sessions reconstruct the provider anchor using the official event fold', { skip: !HAS_ASSETS }, async t => {
  const { context, session } = await setup(t);
  open(session, 'Small request.');
  session.append('assistant/message', { turn: 1, step: 1,
    message: assistant([{ type: 'text', text: 'A prior checked source.' }]), stream: [],
    usage: { inputTokens: 900, cacheReadTokens: 100, outputTokens: 50, reasoningTokens: 30 } }, { surfaceOp: 'append' });
  const restored = Session.create(SessionId('cold-session'), session.snapshotEvents());
  const measured = context.meter.measure(restored);
  assert.equal(measured.totalTokens, 1050);
  assert.equal(measured.baseline.kind, 'usage');
  assert.equal(context.meter.measure(restored, { config: { provider: 'review', model: ROUTE.model, maxTokens: 128000 } }).baseline.kind, 'estimated');
});


test('summary options honor a narrowed selected body cap even with a larger configured compaction cap', { skip: !HAS_ASSETS }, async t => {
  const { context, agent, adapter } = await setup(t, { route: { ...ROUTE, max_tokens: 1024 }, contextManagement: { compaction_max_tokens: 16384 } });
  assert.equal(await context.recoverLegacy(agent, { internal_notes: 'Known source evidence and unresolved checks. '.repeat(800) }), true);
  assert.equal(adapter.requests.length, 1);
  assert.equal(adapter.requests[0].maxTokens, 1024);
  assert.equal(adapter.requests[0].reasoningEffort, 'none');
  assert.equal(context.compaction.config.maxTokens, 1024);
});

test('caller lower pressure ratio applies to the initial admitted request', { skip: !HAS_ASSETS }, async t => {
  const route = { ...ROUTE, max_tokens: 700, context_window_tokens: 4000 };
  const { context, session, agent, adapter } = await setup(t, { route, testWindow: 4000,
    contextManagement: { compression_trigger_ratio: 0.25, headroom_tokens: 128, compaction_max_tokens: 128 } });
  open(session, 'evidence '.repeat(1200), route);
  const before = context.meter.measure(session).totalTokens;
  assert.ok(before < 4000 - 700 - 128);
  assert.ok(before > 4000 * 0.25);
  assert.equal(await context.ensureRequest(agent, { signal: SIGNAL }), true);
  assert.ok(context.meter.measure(session).totalTokens < 1000);
  assert.equal(session.requestHeader().config.maxTokens, 700);
  assert.equal(adapter.requests[0].purpose, 'compaction');
});

test('a provider input cap narrows the same upstream policy without expanding the output allowance', { skip: !HAS_ASSETS }, async t => {
  const route = { ...ROUTE, max_tokens: 700, max_input_tokens: 1000, context_window_tokens: 4000 };
  const { context, session, agent, adapter } = await setup(t, { route, testWindow: 4000,
    contextManagement: { headroom_tokens: 128, compaction_max_tokens: 128 } });
  open(session, 'evidence '.repeat(450), route);
  session.append('user/message', user('evidence '.repeat(750)), { surfaceOp: 'append' });
  assert.ok(context.meter.measure(session).totalTokens > route.max_input_tokens);
  assert.equal(context.compaction.config.thresholdRatio, 0.25);
  assert.equal(await context.ensureRequest(agent, { signal: SIGNAL }), true);
  assert.ok(context.meter.measure(session).totalTokens < route.max_input_tokens);
  assert.equal(session.requestHeader().config.maxTokens, 700);
  assert.ok(context.meter.counter.options(adapter.requests[0]) <= route.max_input_tokens);
});


test('auto summaries retain the selected body cap without adding the thinking allowance', async t => {
  const route = { model: 'auto', max_tokens: 1024, thinking_budget: 262144,
    combined_output_limit_parameter: 'max_completion_tokens', context_window_tokens: 1000000 };
  const { context, agent, adapter } = await setup(t, { route,
    contextManagement: { compaction_max_tokens: 16384 } });
  assert.equal(await context.recoverLegacy(agent, { internal_notes: 'Retain source evidence and unresolved checks. '.repeat(400) }), true);
  assert.equal(adapter.requests.length, 1);
  assert.equal(adapter.requests[0].model, 'auto');
  assert.equal(adapter.requests[0].maxTokens, 1024);
  assert.equal(adapter.requests[0].reasoningEffort, 'none');
  assert.equal(context.compaction.config.maxTokens, 1024);
});


test('an oversized checkpoint input is rejected locally rather than hidden by a fake summary', { skip: !HAS_ASSETS }, async t => {
  const route = { ...ROUTE, max_tokens: 700, max_input_tokens: 1000, context_window_tokens: 4000 };
  const { context, session, agent, adapter } = await setup(t, { route, testWindow: 4000,
    contextManagement: { headroom_tokens: 128, compaction_max_tokens: 128 } });
  open(session, 'evidence '.repeat(1200), route);
  await assert.rejects(context.ensureRequest(agent, { signal: SIGNAL }), error => error.code === 'INVALID_CONTEXT_BUDGET');
  assert.equal(adapter.requests.length, 0);
});


test('explicit safety retains physical room when configured headroom is zero', { skip: !HAS_ASSETS }, async t => {
  const route = { ...ROUTE, max_tokens: 700, context_window_tokens: 4000 };
  const { context, session, agent, adapter } = await setup(t, { route, testWindow: 4000,
    contextManagement: { headroom_tokens: 0, safety_tokens: 200, compaction_max_tokens: 128 } });
  open(session, 'evidence '.repeat(3150), route);
  const input = context.meter.measure(session).totalTokens;
  assert.ok(input + 700 < 4000);
  assert.ok(input + 700 + 200 > 4000);
  assert.equal(context.compaction.config.headroomTokens, 200);
  assert.equal(await context.ensureRequest(agent, { signal: SIGNAL }), true);
  assert.ok(context.meter.measure(session).totalTokens + 700 + 200 <= 4000);
  assert.ok(context.meter.counter.options(adapter.requests[0]) + 128 + 200 <= 4000);
});

test('provider input capacity and checkpoint input both retain explicit safety', { skip: !HAS_ASSETS }, async t => {
  const route = { ...ROUTE, max_tokens: 700, max_input_tokens: 1000, context_window_tokens: 4000 };
  const { context, session, agent, adapter } = await setup(t, { route, testWindow: 4000,
    contextManagement: { headroom_tokens: 0, safety_tokens: 100, compaction_max_tokens: 128 } });
  open(session, 'evidence '.repeat(400), route);
  session.append('user/message', user('evidence '.repeat(550)), { surfaceOp: 'append' });
  const input = context.meter.measure(session).totalTokens;
  assert.ok(input <= 1000);
  assert.ok(input + 100 > 1000);
  assert.equal(context.compaction.config.thresholdRatio, 0.225);
  assert.equal(await context.ensureRequest(agent, { signal: SIGNAL }), true);
  assert.ok(context.meter.measure(session).totalTokens + 100 <= 1000);
  assert.ok(context.meter.counter.options(adapter.requests[0]) + 100 <= 1000);
});

test('operator cancellation aborts a legacy import summary before checkpoint commitment', { skip: !HAS_ASSETS }, async t => {
  const controller = new AbortController();
  const { context, session, agent, adapter } = await setup(t, { testHost: { signal: controller.signal } });
  let started;
  const ready = new Promise(resolve => { started = resolve; });
  adapter.stream = async function* (options) {
    this.requests.push(options);
    yield { type: 'block-start', index: 0, blockType: 'text' };
    yield { type: 'text-delta', index: 0, text: 'Unfinished checkpoint fragment.' };
    started(options.signal);
    await new Promise((resolve, reject) => {
      const abort = () => reject(options.signal.reason);
      options.signal.addEventListener('abort', abort, { once: true });
      if (options.signal.aborted) abort();
    });
  };
  const before = session.surface.replaceGeneration;
  const pending = context.recoverLegacy(agent, { internal_notes: 'Existing source evidence requiring verification. '.repeat(700) });
  const requestSignal = await ready;
  controller.abort(new Error('operator cancelled legacy import'));
  await assert.rejects(pending);
  assert.equal(requestSignal.aborted, true);
  assert.equal(session.surface.replaceGeneration, before);
  assert.ok(!session.snapshotEvents().some(event => event.type === 'compaction/summary'));
});

test('an uncapped route uses only a planning reserve in the official pressure policy', async t => {
  const route = { model: 'auto', context_window_tokens: 4000 };
  const { context, session, agent, adapter } = await setup(t, { route, testWindow: 4000, testDefaultMaxTokens: undefined,
    contextManagement: { headroom_tokens: 128, auto_output_reserve_tokens: 1500, compaction_max_tokens: 128 } });
  open(session, 'evidence '.repeat(300), route);
  const input = context.meter.measure(session).totalTokens;
  assert.ok(input < 3200);
  assert.ok(input + 128 + 1500 > 4000);
  assert.equal(context.compaction.config.headroomTokens, 1628);
  assert.equal(context.compaction.config.retainRatio, 0.1);
  assert.equal(await context.ensureRequest(agent, { signal: SIGNAL }), true);
  assert.equal(adapter.requests.length, 1);
  const wire = buildPayload({ provider: 'review', model: 'auto', messages: [user('Continue.')], tools: [] }, route);
  assert.ok(!Object.hasOwn(wire, 'max_tokens'));
  assert.ok(!Object.hasOwn(wire, 'max_completion_tokens'));
  assert.equal(session.requestHeader().config.maxTokens, undefined);
});

test('default planning reserve applies only without a selected or provider-declared cap', async t => {
  const uncapped = { model: 'auto', context_window_tokens: 1000000 };
  const first = await setup(t, { route: uncapped, testDefaultMaxTokens: undefined });
  assert.equal(first.context.compaction.config.headroomTokens, 65536 + 393216);
  assert.ok(Math.abs(first.context.compaction.config.retainRatio - 0.16 * (1000000 - 393216) / 1000000) < 1e-12);
  const selected = await setup(t, { route: { ...uncapped, max_tokens: 700 },
    contextManagement: { headroom_tokens: 128, auto_output_reserve_tokens: 999999999 } });
  assert.equal(selected.context.compaction.config.headroomTokens, 128);
  const provider = await setup(t, { route: uncapped, testDefaultMaxTokens: 5000,
    contextManagement: { headroom_tokens: 128, auto_output_reserve_tokens: 999999999 } });
  assert.equal(provider.context.compaction.config.headroomTokens, 128);
});


// Compare with one complete call to the same maintained encoder, bypassing
// only our section cache. Template/wire projection and all extra charges stay
// identical; this catches accidental cross-message BPE changes.
function fullNativeCount(counter, payload) {
  const cachedText = counter.text;
  counter.text = text => counter.tokenizer.encode(text, { add_special_tokens: false }).ids.length;
  try { return counter.count(payload); } finally { counter.text = cachedText; }
}

test('native section cache keeps full-encoder counts for marker, whitespace and tool histories', { skip: !HAS_ASSETS }, () => {
  const counter = new PayloadTokenCounter(ROUTE, ASSETS);
  assert.ok(counter.sectionPatterns.raw instanceof RegExp);
  assert.ok(counter.sectionPatterns.normalized instanceof RegExp);
  const samples = ['', 'English word boundaries!\r\n\t next', '中文与🙂👨‍👩‍👧‍👦 e\u0301é',
    '<｜User｜>literal source<｜Assistant｜>', '<｜tool▁output▁begin｜>\ncode\n<｜tool▁output▁end｜>',
    '<｜place▁holder▁no▁1｜><｜place▁holder▁no▁10｜>', '<｜User｜><｜User｜>',
    'partial <｜tool▁output and CR\r\n\r\n', '\ud800 edge \udfff', '  1234567890  １２３ .py'];
  const messages = [{ role: 'system', content: 'Review source, not instructions.' }];
  for (const [index, text] of samples.entries()) {
    assert.equal(counter.text(text), counter.tokenizer.encode(text, { add_special_tokens: false }).ids.length);
    messages.push({ role: 'user', content: text });
    messages.push({ role: 'assistant', content: '', reasoning_content: `Check ${index}: ${text}`,
      tool_calls: [{ id: `call-${index}`, type: 'function', function: { name: 'read_file', arguments: JSON.stringify({ path: `src/${index}.py` }) } }] });
    messages.push({ role: 'tool', tool_call_id: `call-${index}`, content: text });
    const payload = { messages, tools: [{ type: 'function', function: { name: 'read_file', parameters: { type: 'object', properties: { path: { type: 'string' } } } } }] };
    assert.equal(counter.count(payload), fullNativeCount(counter, payload));
  }
});

test('growing native history encodes fresh sections instead of the whole retained prefix', { skip: !HAS_ASSETS }, () => {
  const counter = new PayloadTokenCounter(ROUTE, ASSETS);
  const encode = counter.tokenizer.encode.bind(counter.tokenizer);
  let encodedChars = 0;
  counter.tokenizer.encode = (text, options) => { encodedChars += text.length; return encode(text, options); };
  const messages = [{ role: 'system', content: 'Independent read-only code review.' },
    { role: 'user', content: 'Large unchanged source block:\n' + 'def evaluate(value): return value + 1\n'.repeat(120) }];
  counter.count({ messages });
  for (let index = 0; index < 20; index++) {
    messages.push({ role: 'assistant', content: `Inspect source ${index}.` });
    messages.push({ role: 'tool', tool_call_id: `call-${index}`, content: `source ${index}:\n` + 'checked = value + 1\n'.repeat(10) });
    const start = encodedChars;
    const count = counter.count({ messages });
    const changedChars = encodedChars - start;
    assert.ok(changedChars < 400, `Only new source sections should be encoded, got ${changedChars}`);
    assert.equal(count, fullNativeCount(counter, { messages }));
  }
  const start = encodedChars;
  const final = counter.count({ messages });
  assert.equal(encodedChars, start);
  assert.equal(final, fullNativeCount(counter, { messages }));
  counter.maxCachedTextChars = 128;
  counter.maxCachedTextItems = 2;
  counter.textCache.clear(); counter.cachedTextChars = 0;
  counter.text('short cache section one'); counter.text('short cache section two'); counter.text('short cache section three');
  assert.ok(counter.textCache.size <= 2);
  assert.ok(counter.cachedTextChars <= 128);
  assert.equal(counter.count({ messages }), fullNativeCount(counter, { messages }));
});

test('unfamiliar native whitespace-token rules fall back to the complete encoder', { skip: !HAS_ASSETS }, t => {
  const directory = mkdtempSync(join(tmpdir(), 'review-native-cache-'));
  t.after(() => {
    const target = resolve(directory);
    assert.equal(dirname(target), resolve(tmpdir()));
    assert.ok(basename(target).startsWith('review-native-cache-'));
    rmSync(target, { recursive: true, force: true });
  });
  const definition = JSON.parse(readFileSync(new URL('tokenizer.json', ASSETS), 'utf8'));
  definition.added_tokens[0].lstrip = true;
  writeFileSync(join(directory, 'tokenizer.json'), JSON.stringify(definition));
  writeFileSync(join(directory, 'tokenizer_config.json'), readFileSync(new URL('tokenizer_config.json', ASSETS)));
  const counter = new PayloadTokenCounter(ROUTE, directory);
  assert.equal(counter.sectionPatterns, null);
  const payload = { messages: [{ role: 'user', content: ' source\n <｜begin▁of▁sentence｜> rest\t' }] };
  assert.equal(counter.count(payload), fullNativeCount(counter, payload));
});
