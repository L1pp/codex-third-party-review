import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { LlmAdapter, createUserMessage, resolveRetryPolicy } from '@deepseek-ai/dsh-llm';
import Persistence from './persistence.mjs';
import { projectToolSchema, runEngine } from './engine.mjs';
import { mountContext } from './context.mjs';

const tool = { type: 'function', function: {
  name: 'read_material', description: 'Read one supplied snapshot, within its line bounds.',
  parameters: { type: 'object', properties: { path: { type: 'string', minLength: 1 }, line_count: { type: 'integer', minimum: 1, maximum: 250 } }, required: ['path'], additionalProperties: false },
} };
function report(text = '{"findings":[],"context_gaps":[]}') {
  return [{ type: 'block-start', index: 0, blockType: 'text' },
    { type: 'text-delta', index: 0, text }, { type: 'block-end', index: 0, block: { type: 'text', text } },
    { type: 'usage', usage: { inputTokens: 10, outputTokens: 5 } }, { type: 'finish', reason: { kind: 'stop' } }];
}
function failure(code, { reasoning = '', providerRetryAfterMs } = {}) {
  return [
    ...(reasoning ? [{ type: 'block-start', index: 0, blockType: 'reasoning' }, { type: 'reasoning-delta', index: 0, text: reasoning }] : []),
    { type: 'finish', reason: { kind: 'error', failure: { code, message: 'Offline fixture failure', ...(providerRetryAfterMs ? { providerRetryAfterMs } : {}) } } },
  ];
}
function readTool(id = 'read-one') {
  const block = { type: 'tool-call', id, name: 'read_material', arguments: '{"path":"sample.py","line_count":1}' };
  return [{ type: 'block-start', index: 0, blockType: 'tool-call' },
    { type: 'tool-call-delta', index: 0, id: block.id, name: block.name, argumentsDelta: block.arguments },
    { type: 'block-end', index: 0, block }, { type: 'finish', reason: { kind: 'tool-calls' } }];
}
class Fixture extends LlmAdapter {
  constructor(host, response) { super(); this.host = host; this.response = response; this.requests = []; }
  resolveModel(provider, model) { return Promise.resolve({ provider, id: model, name: model, context: { contextWindow: this.contextWindow ?? 1000000 }, defaultMaxTokens: 256000, reasoning: { efforts: ['none', 'max'].map(id => ({ id, name: id })) } }); }
  providerRetryPolicy() { return resolveRetryPolicy({ mode: 'always', backoff: { initialDelayMs: 1, maxDelayMs: 120000, jitterRatio: 0 } }, 'engine.fixture'); }
  async * stream(options) {
    await this.host.beforeRequest?.(options);
    await this.host.request?.('requestStart', { purpose: options.purpose });
    this.requests.push(structuredClone({ messages: options.messages, tools: options.tools, maxTokens: options.maxTokens }));
    yield* await this.response(options, this.requests.length);
  }
}
async function fixture(t, response, extra = {}) {
  const directory = await mkdtemp(join(tmpdir(), 'review-engine-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const notifications = [], calls = [];
  const host = { async request() { return { recorded: true }; }, ...extra.host, notify(type, data) { notifications.push({ type, data }); },
    async executeTool(call) { calls.push(call); return { path: 'repo/sample.py', text: 'value = 1\n', source_id: 'source-0001' }; } };
  const spec = { sessionId: 'engine-fixture', outputDir: join(directory, 'new'), route: { model: 'fixture', max_tokens: 256000 },
    systemPrompt: 'Independently review the code. Use only the supplied read-only tools.',
    initialUserContent: 'Review sample.py and return the final JSON report.', tools: [tool],
    apiRecovery: { retry_window_seconds: 120 }, ...extra.spec };
  const adapter = new Fixture(host, response);
  const run = async additions => runEngine({ ...spec, ...additions }, host,
    { adapter, persistencePlugin: extra.persistencePlugin ?? Persistence, contextPlugin: extra.contextPlugin });
  return { directory, spec, host, adapter, notifications, calls, run };
}

test('official loop executes a read-only tool and commits the final report', async t => {
  const f = await fixture(t, (_options, call) => call === 1 ? readTool() : report());
  const result = await f.run();
  assert.equal(result.status, 'completed', JSON.stringify({ status: result.status, finishReason: result.finishReason, modelCalls: result.modelCalls, compactionCalls: result.compactionCalls, uncompletedReason: result.uncompletedReason })); assert.equal(result.modelCalls, 2); assert.equal(result.toolsExecuted, 1);
  assert.equal(f.calls.length, 1); assert.equal(f.calls[0].arguments.path, 'sample.py');
  assert.ok(JSON.stringify(f.adapter.requests[1].messages).includes('source-0001'));
  const events = (await readFile(join(result.sessionDirectory, 'session.jsonl'), 'utf8')).trim().split('\n').map(JSON.parse);
  assert.equal(events.filter(event => event.type === 'tool/call').length, 1);
  assert.equal(events.filter(event => event.type === 'assistant/message').length, 2);
  assert.equal(f.host.beforeRequest, undefined);
});

test('official always retry has no fixed five-attempt ceiling and isolates failed streams', async t => {
  const f = await fixture(t, (_options, call) => call <= 6 ? failure('RATE_LIMIT') : report());
  const result = await f.run();
  assert.equal(result.status, 'completed', JSON.stringify({ status: result.status, finishReason: result.finishReason, modelCalls: result.modelCalls, compactionCalls: result.compactionCalls, uncompletedReason: result.uncompletedReason })); assert.equal(f.adapter.requests.length, 7);
  const events = f.notifications.filter(row => row.type === 'session.event').map(row => row.data.event);
  assert.equal(events.filter(event => event.type === 'assistant/attempt').length, 6);
  assert.equal(events.filter(event => event.type === 'llm/retry').length, 6);
  assert.equal(events.filter(event => event.type === 'assistant/message').length, 1);
});

test('progress interruption checkpoints through the task hook before retrying', async t => {
  let checkpoints = 0;
  const f = await fixture(t, (_options, call) => call === 1 ? failure('SERVER', { reasoning: 'FAILED_PRIVATE_REVIEW_PROGRESS' }) : report(), {
    contextPlugin: async () => ({ async recover(agent, code, { attempt }) {
      assert.equal(code.code, 'SERVER'); assert.equal(attempt.type, 'assistant/attempt'); checkpoints++;
      agent.session.append('user/message', createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text: 'CHECKPOINT: verify unresolved source evidence.' }] }), { surfaceOp: 'append' });
      return true;
    } }),
  });
  const result = await f.run(); assert.equal(result.status, 'completed', JSON.stringify({ status: result.status, finishReason: result.finishReason, modelCalls: result.modelCalls, compactionCalls: result.compactionCalls, uncompletedReason: result.uncompletedReason })); assert.equal(checkpoints, 1);
  assert.equal(result.checkpointRecoveries, 1); assert.equal(f.adapter.requests.length, 2);
  const history = JSON.stringify(f.adapter.requests[1].messages);
  assert.ok(history.includes('CHECKPOINT')); assert.ok(!history.includes('FAILED_PRIVATE_REVIEW_PROGRESS'));
});

test('failed checkpoint, permanent errors and host I/O failure do not enter always retry', async t => {
  for (const [code, reasoning] of [['AUTH', 'some partial text'], ['HOST_IO_ERROR', ''], ['SERVER', 'partial progress']]) {
    await t.test(code, async t => {
      let recoveries = 0;
      const f = await fixture(t, () => failure(code, { reasoning }), { contextPlugin: async () => ({ async recover() { recoveries++; return false; } }) });
      const result = await f.run(); assert.equal(result.status, 'error'); assert.equal(f.adapter.requests.length, 1);
      assert.equal(recoveries, code === 'SERVER' ? 1 : 0);
    });
  }
});

test('Retry-After beyond the recovery window stops without an early request', async t => {
  const f = await fixture(t, () => failure('RATE_LIMIT', { providerRetryAfterMs: 121000 }));
  const result = await f.run(); assert.equal(result.status, 'error'); assert.equal(f.adapter.requests.length, 1);
  assert.ok(f.notifications.some(row => row.type === 'recovery.stopped'));
});

test('oversleep outside the time window cannot start another model attempt', async t => {
  let clock = 0;
  const f = await fixture(t, () => { clock = 125000; return failure('RATE_LIMIT'); }, { host: { now: () => clock } });
  // Set the initial failure time to zero, then make the post-wait read observe suspension.
  let reads = 0; f.host.now = () => ++reads < 4 ? 0 : 125000;
  const result = await f.run(); assert.equal(result.status, 'error'); assert.equal(f.adapter.requests.length, 1);
});

test('length continuation opens a new official turn and does not execute partial tools', async t => {
  let continuations = 0;
  const f = await fixture(t, (_options, call) => call === 1 ? [
    { type: 'block-start', index: 0, blockType: 'text' }, { type: 'text-delta', index: 0, text: 'PARTIAL_REPORT' },
    { type: 'block-start', index: 1, blockType: 'tool-call' },
    { type: 'tool-call-delta', index: 1, id: 'incomplete-call', name: 'read_material', argumentsDelta: '{"path":' },
    { type: 'finish', reason: { kind: 'max-tokens' } },
  ] : report(), { contextPlugin: async () => ({ async continueLength(agent) {
    continuations++;
    agent.followup(createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text: 'Continue from the verified checkpoint.' }] }));
  } }) });
  const result = await f.run(); assert.equal(result.status, 'completed', JSON.stringify({ status: result.status, finishReason: result.finishReason, modelCalls: result.modelCalls, compactionCalls: result.compactionCalls, uncompletedReason: result.uncompletedReason })); assert.equal(f.adapter.requests.length, 2);
  assert.equal(continuations, 1); assert.equal(f.calls.length, 0);
  const ends = f.notifications.filter(row => row.type === 'session.event' && row.data.event.type === 'turn/end').map(row => row.data.event.data.reason.kind);
  assert.deepEqual(ends, ['max-tokens', 'completed']);
});

test('official resume restores tool history in a separate record directory without replay', async t => {
  const first = await fixture(t, (_options, call) => call === 1 ? readTool() : report());
  const previous = await first.run();
  const oldHeader = await readFile(join(previous.sessionDirectory, 'session-header.json'));
  const oldEvents = await readFile(join(previous.sessionDirectory, 'session.jsonl'));
  const next = await fixture(t, () => report('RESUMED_REPORT'));
  const result = await next.run({ resume: { sourceDirectory: previous.sessionDirectory, sessionId: first.spec.sessionId } });
  assert.equal(result.status, 'completed', JSON.stringify({ status: result.status, finishReason: result.finishReason, modelCalls: result.modelCalls, compactionCalls: result.compactionCalls, uncompletedReason: result.uncompletedReason })); assert.equal(result.finalContent, 'RESUMED_REPORT');
  assert.equal(next.adapter.requests.length, 1); assert.equal(next.calls.length, 0);
  assert.ok(next.adapter.requests[0].messages.some(message => message.role === 'tool'));
  assert.deepEqual(await readFile(join(previous.sessionDirectory, 'session-header.json')), oldHeader);
  assert.deepEqual(await readFile(join(previous.sessionDirectory, 'session.jsonl')), oldEvents);
});

test('auto reserves combined body and thinking budget in the official loop envelope', async t => {
  const f = await fixture(t, () => report(), { spec: { route: { model: 'auto', max_tokens: 131072, thinking_budget: 262144 } } });
  await f.run(); assert.equal(f.adapter.requests[0].maxTokens, 393216);
});

test('schema adaptation preserves property names and literal enum data', () => {
  const schema = { type: 'object', properties: { minimum: { type: 'integer', minimum: 1 }, payload: { type: 'object', enum: [{ maximum: 10 }] } }, required: ['minimum'], additionalProperties: false };
  const projected = projectToolSchema(schema);
  assert.deepEqual(projected.properties.minimum, { type: 'integer' });
  assert.deepEqual(projected.properties.payload.enum, [{ maximum: 10 }]);
  assert.deepEqual(schema.properties.minimum, { type: 'integer', minimum: 1 });
});
test('existing positive model-call limit is opt-in and stops before another request', async t => {
  const f = await fixture(t, (_options, call) => call === 1 ? readTool() : report(), {
    spec: { contextManagement: { max_model_calls: 1, max_compactions: null } },
  });
  const result = await f.run();
  assert.equal(result.status, 'incomplete', JSON.stringify({ status: result.status, finishReason: result.finishReason, modelCalls: result.modelCalls, compactionCalls: result.compactionCalls, uncompletedReason: result.uncompletedReason })); assert.equal(result.modelCalls, 1);
  assert.equal(f.adapter.requests.length, 1); assert.equal(f.calls.length, 1);
  assert.match(result.uncompletedReason, /max_model_calls/);
});

test('zero and null legacy count settings do not impose a request limit', async t => {
  const f = await fixture(t, (_options, call) => call === 1 ? readTool() : report(), {
    spec: { contextManagement: { max_model_calls: 0, max_compactions: null } },
  });
  const result = await f.run(); assert.equal(result.status, 'completed', JSON.stringify({ status: result.status, finishReason: result.finishReason, modelCalls: result.modelCalls, compactionCalls: result.compactionCalls, uncompletedReason: result.uncompletedReason })); assert.equal(result.modelCalls, 2);
});

test('only acknowledged request starts count as admitted provider attempts', async t => {
  const f = await fixture(t, () => report(), { host: {
    async request() { const error = new Error('Local request record unavailable'); error.code = 'HOST_IO_ERROR'; throw error; },
  } });
  const result = await f.run(); assert.equal(result.status, 'error'); assert.equal(result.modelCalls, 0);
  assert.equal(f.adapter.requests.length, 0);
});

test('cancellation during service mounting cannot wake a later model turn', async t => {
  const controller = new AbortController();
  class AbortOnMount extends Persistence {
    constructor(...args) { super(...args); controller.abort(); }
  }
  const f = await fixture(t, () => report(), { host: { signal: controller.signal }, persistencePlugin: AbortOnMount });
  const result = await f.run(); assert.equal(result.status, 'interrupted'); assert.equal(result.modelCalls, 0);
  assert.equal(f.adapter.requests.length, 0);
});

test('legacy progress is admitted while idle before the first new task prompt', async t => {
  let imported = 0;
  const progress = { prior_checkpoint: 'Verified earlier evidence', completed_work: [], internal_notes: '', read_sources: [], inline_delivered_ranges: [] };
  const f = await fixture(t, () => report(), {
    spec: { legacyProgress: progress },
    contextPlugin: async () => ({ async recoverLegacy(agent, value) {
      assert.equal(agent.status, 'idle'); assert.equal(agent.inbox.nextTurn.length, 0); imported++;
      assert.deepEqual(value, progress);
      agent.session.append('user/message', createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text: 'IMPORTED_CHECKPOINT' }] }), { surfaceOp: 'append' });
      return true;
    } }),
  });
  const result = await f.run(); assert.equal(result.status, 'completed', JSON.stringify({ status: result.status, finishReason: result.finishReason, modelCalls: result.modelCalls, compactionCalls: result.compactionCalls, uncompletedReason: result.uncompletedReason })); assert.equal(imported, 1);
  assert.ok(JSON.stringify(f.adapter.requests[0].messages).includes('IMPORTED_CHECKPOINT'));
});

test('real official context compactor checkpoints a failed streamed review before continuing', async t => {
  let reviews = 0;
  const f = await fixture(t, options => {
    if (options.purpose === 'compaction') return report('CHECKPOINT: source-0001 was checked; verify the remaining caller boundary.');
    reviews++;
    return reviews === 1 ? failure('SERVER', { reasoning: 'FAILED_REVIEW_EVIDENCE: inspect repo/sample.py source-0001 and the caller.\n'.repeat(400) }) : report();
  }, { contextPlugin: mountContext, spec: { contextManagement: { compaction_max_tokens: 2048 }, route: { model: 'auto', max_tokens: 131072, thinking_budget: 262144, context_window_tokens: 1000000 } } });
  const result = await f.run();
  assert.equal(result.status, 'completed', JSON.stringify({ status: result.status, finishReason: result.finishReason, modelCalls: result.modelCalls, compactionCalls: result.compactionCalls, uncompletedReason: result.uncompletedReason })); assert.equal(result.modelCalls, 3); assert.equal(result.compactionCalls, 1);
  assert.equal(result.checkpointRecoveries, 1); assert.equal(reviews, 2);
  assert.ok(JSON.stringify(f.adapter.requests.at(-1).messages).includes('CHECKPOINT'));
  assert.ok(!JSON.stringify(f.adapter.requests.at(-1).messages).includes('FAILED_REVIEW_EVIDENCE'));
  assert.ok(f.notifications.some(row => row.type === 'session.event' && row.data.event.type === 'compaction/end'));
});

test('real length compaction keeps the incomplete turn and finishes a later turn', async t => {
  let reviews = 0;
  const f = await fixture(t, options => {
    if (options.purpose === 'compaction') return report('CHECKPOINT: preserve the partial finding and finish remaining source checks.');
    reviews++;
    return reviews === 1 ? [
      { type: 'block-start', index: 0, blockType: 'text' },
      { type: 'text-delta', index: 0, text: 'PARTIAL_FINDING: review repo/sample.py.\n'.repeat(400) },
      { type: 'finish', reason: { kind: 'max-tokens' } },
    ] : report();
  }, { contextPlugin: mountContext, spec: { contextManagement: { compaction_max_tokens: 2048 }, route: { model: 'auto', max_tokens: 131072, thinking_budget: 262144, context_window_tokens: 1000000 } } });
  const result = await f.run(); assert.equal(result.status, 'completed', JSON.stringify({ status: result.status, finishReason: result.finishReason, modelCalls: result.modelCalls, compactionCalls: result.compactionCalls, uncompletedReason: result.uncompletedReason })); assert.equal(result.modelCalls, 3);
  assert.equal(result.compactionCalls, 1); assert.equal(f.calls.length, 0);
  const turns = f.notifications.filter(row => row.type === 'session.event' && row.data.event.type === 'turn/end').map(row => row.data.event.data.reason.kind);
  assert.deepEqual(turns, ['max-tokens', 'completed']);
});

test('real context admits a short legacy checkpoint before the first official model step', async t => {
  const f = await fixture(t, () => report(), { contextPlugin: mountContext, spec: {
    route: { model: 'auto', max_tokens: 131072, thinking_budget: 262144, context_window_tokens: 1000000 },
    legacyProgress: { prior_checkpoint: 'LEGACY_CHECKPOINT: src/calc.py:12 remains to verify.', read_sources: [] },
  } });
  const result = await f.run(); assert.equal(result.status, 'completed', JSON.stringify({ status: result.status, finishReason: result.finishReason, modelCalls: result.modelCalls, compactionCalls: result.compactionCalls, uncompletedReason: result.uncompletedReason })); assert.equal(result.modelCalls, 1);
  assert.equal(result.compactionCalls, 0); assert.ok(JSON.stringify(f.adapter.requests[0].messages).includes('LEGACY_CHECKPOINT'));
});

test('an explicit compaction count limit stops a second summary without another API attempt', async t => {
  let reviews = 0;
  const f = await fixture(t, options => {
    if (options.purpose === 'compaction') return report('CHECKPOINT: preserve the confirmed evidence.');
    reviews++;
    return failure('SERVER', { reasoning: 'PROGRESS_WITH_EVIDENCE source-0001 repo/sample.py.\n'.repeat(400) });
  }, { contextPlugin: mountContext, spec: { contextManagement: { max_compactions: 1, compaction_max_tokens: 2048 },
    route: { model: 'auto', max_tokens: 131072, thinking_budget: 262144, context_window_tokens: 1000000 } } });
  const result = await f.run(); assert.equal(result.status, 'incomplete', JSON.stringify({ status: result.status, finishReason: result.finishReason, modelCalls: result.modelCalls, compactionCalls: result.compactionCalls, uncompletedReason: result.uncompletedReason })); assert.equal(result.modelCalls, 3);
  assert.equal(result.compactionCalls, 1); assert.equal(reviews, 2);
  assert.match(result.uncompletedReason, /max_compactions/);
});

test('first oversized canonical request compacts before its API attempt and rebuilds from the official surface', async t => {
  const f = await fixture(t, options => options.purpose === 'compaction'
    ? report('CHECKPOINT: verify source-0001 against its caller and finish the review.') : report(), {
    contextPlugin: mountContext,
    spec: { initialUserContent: 'LARGE_INITIAL_REVIEW_MATERIAL source-0001 file.py\n'.repeat(250),
      route: { model: 'auto', max_tokens: 8000, context_window_tokens: 20000 },
      contextManagement: { compaction_max_tokens: 512, compression_trigger_ratio: 0.8, headroom_tokens: 1000, retain_recent_tokens: 0 } },
  });
  f.adapter.contextWindow = 20000;
  const result = await f.run();
  assert.equal(result.status, 'completed', JSON.stringify(result.finishReason));
  assert.equal(result.modelCalls, 2); assert.equal(result.compactionCalls, 1);
  assert.equal(f.adapter.requests.length, 2);
  assert.ok(JSON.stringify(f.adapter.requests[0].messages).includes('LARGE_INITIAL_REVIEW_MATERIAL'));
  assert.ok(!JSON.stringify(f.adapter.requests[1].messages).includes('LARGE_INITIAL_REVIEW_MATERIAL'));
  assert.ok(JSON.stringify(f.adapter.requests[1].messages).includes('CHECKPOINT'));
  assert.equal(f.adapter.requests[1].maxTokens, 8000);
  assert.ok(f.notifications.some(row => row.type === 'session.event' && row.data.event.type === 'assistant/attempt'
    && JSON.stringify(row.data.event.data.stream).includes('REVIEW_CONTEXT_CHANGED')));
});

test('default null and zero count settings allow more than sixteen official tool steps', async t => {
  for (const value of [null, 0]) {
    await t.test(String(value), async t => {
      const f = await fixture(t, (_options, call) => call <= 18 ? readTool(`read-step-${call}`) : report(), {
        spec: { contextManagement: { max_model_calls: value, max_compactions: value } },
      });
      const result = await f.run();
      assert.equal(result.status, 'completed'); assert.equal(result.modelCalls, 19);
      assert.equal(result.toolsExecuted, 18); assert.equal(f.calls.length, 18);
      const events = f.notifications.filter(row => row.type === 'session.event').map(row => row.data.event);
      assert.equal(events.filter(event => event.type === 'tool/call').length, 18);
      assert.equal(events.filter(event => event.type === 'tool/result').length, 18);
      assert.equal(events.filter(event => event.type === 'step/end').length, 19);
      assert.equal(new Set(f.calls.map(call => call.callId)).size, 18);
      assert.ok(f.adapter.requests.at(-1).messages.filter(message => message.role === 'tool').length >= 18);
    });
  }
});

test('real official checkpoint recovery permits more than three successful compactions by default', async t => {
  let reviews = 0;
  let summaries = 0;
  const f = await fixture(t, options => {
    if (options.purpose === 'compaction') {
      summaries++;
      if (summaries > 1) assert.ok(JSON.stringify(options.messages).includes('CHECKPOINT_STAGE_'));
      return report(`CHECKPOINT_STAGE_${summaries}: retain the earlier checked source evidence and continue unresolved caller validation.`);
    }
    reviews++;
    return reviews <= 4 ? failure('SERVER', {
      reasoning: `NEW_PROGRESS_STAGE_${reviews}: source-0001 repo/sample.py has concrete evidence to preserve.\n`.repeat(300),
    }) : report();
  }, { contextPlugin: mountContext, spec: {
    contextManagement: { max_model_calls: null, max_compactions: 0, compaction_max_tokens: 2048 },
    route: { model: 'auto', max_tokens: 131072, thinking_budget: 262144, context_window_tokens: 1000000 },
  } });
  const result = await f.run();
  assert.equal(result.status, 'completed', JSON.stringify(result.finishReason));
  assert.equal(result.modelCalls, 9); assert.equal(result.compactionCalls, 4);
  assert.equal(result.checkpointRecoveries, 4); assert.equal(reviews, 5); assert.equal(summaries, 4);
  assert.equal(f.calls.length, 0);
  const events = f.notifications.filter(row => row.type === 'session.event').map(row => row.data.event);
  assert.equal(events.filter(event => event.type === 'assistant/attempt').length, 4);
  assert.equal(events.filter(event => event.type === 'compaction/end').length, 4);
  assert.ok(JSON.stringify(f.adapter.requests.at(-1).messages).includes('CHECKPOINT_STAGE_4'));
  assert.ok(!JSON.stringify(f.adapter.requests.at(-1).messages).includes('NEW_PROGRESS_STAGE_'));
});

test('auto with thinking disabled reserves only the actual body allowance',async t=>{
  const f=await fixture(t,()=>report(),{spec:{route:{model:'auto',max_tokens:131072,thinking_budget:262144,combined_output_limit_parameter:'max_completion_tokens',enable_thinking:false,context_window_tokens:1000000}}});
  const result=await f.run();assert.equal(result.status,'completed');assert.equal(f.adapter.requests[0].maxTokens,131072);
});
