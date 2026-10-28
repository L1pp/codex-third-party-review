/** One official Harness loop; Python owns repository material and report policy. */
import { join } from 'node:path';
import { performance } from 'node:perf_hooks';
import { Context } from '@deepseek-ai/cordis';
import AgentRegistry from '@deepseek-ai/dsh-agent';
import AgentLoop from '@deepseek-ai/dsh-agent-loop';
import LlmRuntime, {
  assistantStreamFirstTokenTime, assembleAssistantStream, createUserMessage,
  lastAssistantStreamChunk, resolveRetryPolicy, LlmError,
} from '@deepseek-ai/dsh-llm';
import SessionStore, { SessionId } from '@deepseek-ai/dsh-session';
import SessionProjections from '@deepseek-ai/dsh-session-projection';
import SystemPrompt from '@deepseek-ai/dsh-system-prompt';
import ToolRuntime, { validateJsonSchemaValue } from '@deepseek-ai/dsh-tools';
import * as OfficialRetry from '@deepseek-ai/dsh-llm-retry';
import { generationAllowance } from './adapter.mjs';

const PROVIDER = 'review';
const schemaBounds = new Set(['minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum', 'multipleOf',
  'minLength', 'maxLength', 'pattern', 'minItems', 'maxItems', 'uniqueItems', 'minProperties', 'maxProperties']);
const normalCodes = new Set(resolveRetryPolicy({ mode: 'normal' }, 'review.retry').retryableCodes);
normalCodes.add('STREAM_CLOSED');

/** Keep the official supported schema dialect; Python enforces acquisition bounds. */
export function projectToolSchema(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return value;
  const projected = {};
  for (const [name, child] of Object.entries(value)) {
    if (schemaBounds.has(name)) continue;
    if (name === 'properties' && child && typeof child === 'object') {
      projected[name] = Object.fromEntries(Object.entries(child).map(([key, schema]) => [key, projectToolSchema(schema)]));
    } else if (name === 'items') {
      projected[name] = projectToolSchema(child);
    } else if (name === 'oneOf' && Array.isArray(child)) {
      projected[name] = child.map(projectToolSchema);
    } else {
      projected[name] = child;
    }
  }
  return projected;
}

function toolsFrom(spec) {
  return (spec.tools ?? []).map(raw => {
    const tool = raw?.function ?? raw;
    if (typeof tool?.name !== 'string' || !tool.name || typeof tool.description !== 'string') {
      throw new TypeError('Each review tool requires a name and description');
    }
    return { name: tool.name, description: tool.description,
      parameters: projectToolSchema(tool.parameters ?? { type: 'object', properties: {} }) };
  });
}

function retryWindow(spec) {
  const seconds = (spec.apiRecovery ?? spec.api_recovery ?? {}).retry_window_seconds ?? 120;
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) {
    throw new TypeError('apiRecovery.retry_window_seconds must be finite and non-negative');
  }
  return seconds * 1000;
}

function explicitCount(spec, name) {
  const value = (spec.contextManagement ?? spec.context_management ?? {})[name];
  if (value == null || value === 0) return undefined;
  if (!Number.isSafeInteger(value) || value < 0) throw new TypeError(`${name} must be a non-negative integer or null`);
  return value;
}

function outputBudget(route) {
  const budget=generationAllowance(route);
  if (budget===undefined && route.max_tokens!=null && route.max_tokens!==0) throw new TypeError('Review output budget must be a positive integer');
  return budget;
}

function visibleText(event) {
  return (event?.data?.message?.content ?? [])
    .filter(block => block.type === 'text').map(block => block.text).join('');
}

function attemptText(event) {
  if (!event?.data?.stream) return '';
  return assembleAssistantStream(event.data.stream).interruptedBlocks()
    .filter(block => block.type === 'text').map(block => block.text).join('');
}

/**
 * Boot official services once and run the entire tool/request conversation there.
 * The adapter handles provider wire I/O; the persistence plugin owns event storage.
 */
export async function runEngine(spec, host, { adapter, persistencePlugin, contextPlugin } = {}) {
  if (!spec || !host || !adapter || !persistencePlugin) {
    throw new TypeError('Engine requires a specification, host, adapter and persistence plugin');
  }
  if (typeof spec.sessionId !== 'string' || !spec.sessionId || typeof spec.outputDir !== 'string') {
    throw new TypeError('Engine requires an explicit session ID and output directory');
  }
  if (typeof spec.route?.model !== 'string' || !spec.route.model) throw new TypeError('Engine requires a fixed model route');
  if (typeof spec.systemPrompt !== 'string' || !spec.systemPrompt.trim()) throw new TypeError('Engine requires the review contract');
  const toolSchemas = toolsFrom(spec);
  const windowMs = retryWindow(spec);
  const maxTokens = outputBudget(spec.route);
  const maxCalls = explicitCount(spec, 'max_model_calls');
  const maxCompactions = explicitCount(spec, 'max_compactions');
  const now = typeof host.now === 'function' ? host.now : () => performance.now();
  const ctx = new Context();
  const previousBeforeRequest = host.beforeRequest;
  const previousHostRequest = host.request;
  const notifications = [];
  let notificationWarning;
  let context;
  let agent;
  let lastTurn;
  let lastAssistant;
  let lastAttempt;
  let retainedReport;
  let toolCalls = 0;
  let attempts = 0;
  let retryWaitMs = 0;
  let checkpointRecoveries = 0;
  let admittedCalls = 0;
  let compactionCalls = 0;
  let pendingPurpose;
  let configuredLimit;
  let contextChanged = false;
  const retryStarts = new Map();
  const notify = (type, data) => {
    if (typeof host.notify !== 'function') return;
    try {
      const pending = Promise.resolve(host.notify(type, data));
      notifications.push(pending.catch(() => { notificationWarning = 'Host event delivery failed; persisted session remains authoritative'; }));
    } catch {
      notificationWarning = 'Host event delivery failed; persisted session remains authoritative';
    }
  };
  const cancel = () => agent?.cancel({ kind: 'user' });
  if (host.signal?.aborted) return { status: 'interrupted', sessionId: spec.sessionId, finalContent: '', content: '' };
  host.signal?.addEventListener('abort', cancel, { once: true });

  try {
    await ctx.plugin(LlmRuntime);
    await ctx.plugin(SessionStore);
    await ctx.plugin(SessionProjections);
    await ctx.plugin(SystemPrompt, { includeHarnessIdentity: false, includeRuntimeContext: false, personaPrefix: '' });
    ctx.systemPrompt.section({ name: 'independent-review-contract', order: 0, text: spec.systemPrompt, complete: true });
    // rc.2's maintained native executor is built into ToolRuntime; no shell/PTC mounts.
    await ctx.plugin(ToolRuntime, { mode: 'native' });
    await ctx.plugin(AgentRegistry);
    await ctx.plugin(persistencePlugin, {
      root: join(spec.outputDir, 'engine-session'),
      ...(spec.resume ? { seedDirectory: spec.resume.sourceDirectory, sessionId: spec.resume.sessionId ?? spec.sessionId } : {}),
    });
    host.beforeRequest = async options => {
      context?.assertHealthy?.();
      if (maxCalls !== undefined && admittedCalls >= maxCalls) {
        configuredLimit = 'Explicit max_model_calls limit reached';
        throw new LlmError(configuredLimit, 'REVIEW_MODEL_CALL_LIMIT');
      }
      if (options?.purpose === 'compaction' && maxCompactions !== undefined && compactionCalls >= maxCompactions) {
        configuredLimit = 'Explicit max_compactions limit reached';
        throw new LlmError(configuredLimit, 'REVIEW_COMPACTION_LIMIT');
      }
      pendingPurpose = options?.purpose;
      if (agent && options?.purpose !== 'compaction' && context?.ensureRequest) {
        if (await context.ensureRequest(agent, options) === true) {
          contextChanged = true;
          throw new LlmError('Review context changed before provider dispatch', 'REVIEW_CONTEXT_CHANGED');
        }
      }
      if (previousBeforeRequest) await previousBeforeRequest(options);
      await ctx.sessionPersistence.flush();
    };
    if (typeof previousHostRequest === 'function') {
      host.request = async (method, params) => {
        const result = await previousHostRequest.call(host, method, params);
        if (method === 'requestStart') {
          admittedCalls++;
          if (pendingPurpose === 'compaction') compactionCalls++;
        }
        return result;
      };
    }

    const pendingSummaries = new Map();
    ctx.on('session/event', (session, event) => {
      if (session.id !== spec.sessionId) return;
      notify('session.event', { sessionId: session.id, event });
      if (event.type === 'compaction/summary') pendingSummaries.set(event.data.compactionId, event);
      if (event.type === 'compaction/end') {
        const summary = pendingSummaries.get(event.data.compactionId);
        pendingSummaries.delete(event.data.compactionId);
        if (summary && event.data.error === undefined) {
          const text = (summary.data.summary ?? []).filter(block => block.type === 'text').map(block => block.text).join('\n');
          if (text.trim()) notify('context.checkpoint', { text, reason: 'official_compaction',
            sessionId: session.id, summarySeq: summary.seq, endSeq: event.seq });
        }
      }
      if (event.type === 'assistant/attempt') lastAttempt = event;
      if (event.type === 'assistant/message') {
        lastAssistant = event;
        retryStarts.delete(`${event.data.turn}:${event.data.step}`);
        const finish = lastAssistantStreamChunk(event.data.stream, 'finish')?.reason.kind;
        if (finish !== 'tool-calls' && visibleText(event).trim()) retainedReport = event;
      }
      if (event.type === 'turn/end') lastTurn = event;
      if (event.type === 'tool/call') toolCalls++;
    });
    ctx.on('agent/assistant-stream', ({ agent: owner, frame }) => {
      if (owner.id !== spec.sessionId) return;
      if (frame.type === 'start') attempts++;
      notify('assistant.stream', { sessionId: owner.id, frame });
    });

    for (const tool of toolSchemas) {
      ctx.tools.register({
        ...tool,
        output: { schema: {}, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
        async execute(args, exec) {
          if (validateJsonSchemaValue(tool.parameters, args).length) throw new TypeError('Tool arguments do not match their schema');
          exec.signal.throwIfAborted();
          context?.assertHealthy?.();
          await ctx.sessionPersistence.flush();
          return host.executeTool({ name: tool.name, arguments: args, callId: exec.callId, sessionId: spec.sessionId });
        },
      });
    }
    const allowed = new Set(toolSchemas.map(tool => tool.name));
    ctx.tools.guard(exec => allowed.has(exec.name) ? undefined : 'This independent reviewer has only the provided read-only tools');
    ctx.llm.registerAdapter([PROVIDER], adapter);
    await ctx.plugin(OfficialRetry);
    context = contextPlugin ? await contextPlugin(ctx, spec, host) : undefined;

    // This outer guard delegates only eligible no-progress failures to the
    // official retry executor. `always` itself deliberately retries any code.
    ctx.on('agent/request-error', async (payload, next) => {
      const { agent: owner, failure, signal, turn, step } = payload;
      if (signal.aborted || host.signal?.aborted) return;
      const attempt = lastAttempt?.data.turn === turn && lastAttempt?.data.step === step ? lastAttempt : undefined;
      const hasProgress = Boolean(attempt && assistantStreamFirstTokenTime(attempt.data.stream) !== undefined);
      if (failure.code === 'REVIEW_CONTEXT_CHANGED' && contextChanged) {
        contextChanged = false;
        retryStarts.delete(`${turn}:${step}`);
        return { kind: 'retry' };
      }
      if (failure.code === 'CONTEXT_WINDOW_EXCEEDED' || (normalCodes.has(failure.code) && hasProgress)) {
        const recovered = await context?.recover?.(owner, failure, { attempt, signal, turn, step });
        notify('recovery.checkpoint', { sessionId: owner.id, code: failure.code, recovered: recovered === true });
        if (recovered === true && !signal.aborted) {
          retryStarts.delete(`${turn}:${step}`);
          checkpointRecoveries++;
          return { kind: 'retry' };
        }
        return;
      }
      if (!normalCodes.has(failure.code) || windowMs === 0) return;
      const key = `${turn}:${step}`;
      if (!retryStarts.has(key)) retryStarts.set(key, now());
      const remaining = windowMs - (now() - retryStarts.get(key));
      const providerWait = failure.providerRetryAfterMs;
      if (remaining <= 0 || (Number.isFinite(providerWait) && providerWait > 0 && providerWait >= remaining)) {
        notify('recovery.stopped', { code: failure.code, reason: 'transient_retry_window_exhausted' });
        return;
      }
      const started = now();
      const action = await next();
      retryWaitMs += Math.max(0, now() - started);
      if (signal.aborted || host.signal?.aborted || now() - retryStarts.get(key) >= windowMs) {
        notify('recovery.stopped', { code: failure.code, reason: 'transient_retry_window_exhausted' });
        return;
      }
      return action;
    }, { prepend: true });

    ctx.on('agent/turn-stopping', async ({ agent: owner, signal, turn }) => {
      if (owner.id !== spec.sessionId || signal.aborted) return;
      const finish = lastAssistant && lastAssistantStreamChunk(lastAssistant.data.stream, 'finish')?.reason.kind;
      if (finish === 'max-tokens') {
        await context?.continueLength?.(owner, { attempt: lastAssistant, signal, turn });
      }
    });
    await ctx.plugin(AgentLoop, { agents: [], maxParallelToolCalls: 1 });
    const options = {
      provider: PROVIDER, model: spec.route.model,
      ...(spec.route.reasoning_effort ? { reasoningEffort: spec.route.reasoning_effort } : {}),
      ...(maxTokens === undefined ? {} : { maxTokens }),
    };
    if (spec.resume) {
      const resumed = await ctx.agentLoop.resume(ctx, { resumeSessionId: SessionId(spec.resume.sessionId ?? spec.sessionId), agentOptions: options });
      agent = resumed.agent;
    } else {
      agent = await ctx.agentLoop.create(SessionId(spec.sessionId), options,
        { cwd: spec.cwd ?? process.cwd() });
    }
    if (host.signal?.aborted) {
      return { status: 'interrupted', sessionId: spec.sessionId, finalContent: '', content: '',
        modelCalls: admittedCalls, toolsExecuted: toolCalls, sessionDirectory: join(spec.outputDir, 'engine-session') };
    }
    if (spec.legacyProgress !== undefined) {
      if (!context?.recoverLegacy) throw new TypeError('Legacy progress requires the task checkpoint adapter');
      const imported = await context.recoverLegacy(agent, spec.legacyProgress);
      if (imported !== true) throw new Error('Legacy review progress was not admitted for continuation');
      context.assertHealthy?.();
      await ctx.sessionPersistence.flush();
    }
    if (host.signal?.aborted) {
      return { status: 'interrupted', sessionId: spec.sessionId, finalContent: '', content: '',
        modelCalls: admittedCalls, toolsExecuted: toolCalls, sessionDirectory: join(spec.outputDir, 'engine-session') };
    }
    let content = spec.initialUserContent;
    if (spec.resume) {
      const continuation = spec.resume.continuePrompt ?? 'Continue the same independent review from its recorded progress, verify unresolved evidence and return the final report.';
      if (typeof content === 'string' && content.trim()) {
        try {
          const prepared = JSON.parse(content);
          content = JSON.stringify({ ...prepared, instruction: String(prepared.instruction ?? '') + ' ' + continuation });
        } catch {
          content = JSON.stringify({ resumed_task_materials: content, instruction: continuation });
        }
      } else content = continuation;
    }
    if (typeof content !== 'string' || !content.trim()) throw new TypeError('Review task content must be non-empty');
    agent.followup(createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text: content }] }));
    await agent.whenIdle();
    await ctx.sessionPersistence.flush();
    const finishReason = lastTurn?.data.reason ?? { kind: 'error', error: { code: 'NO_TURN_RESULT', message: 'No committed turn result' } };
    const kind = finishReason.kind;
    const status = configuredLimit ? 'incomplete' : kind === 'completed' ? 'completed' : kind === 'aborted' || kind === 'interrupted' ? 'interrupted' : kind === 'error' ? 'error' : 'incomplete';
    const finalContent = visibleText(retainedReport);
    await Promise.all(notifications);
    return {
      status, sessionId: spec.sessionId, finishReason, finalContent, content: finalContent,
      partialContent: attemptText(lastAttempt), modelCalls: admittedCalls, requestAttempts: attempts, toolsExecuted: toolCalls,
      compactionCalls, ...(configuredLimit ? { uncompletedReason: configuredLimit } : {}),
      checkpointRecoveries, retryWaitSeconds: retryWaitMs / 1000,
      sessionDirectory: join(spec.outputDir, 'engine-session'),
      ...(notificationWarning ? { notificationWarning } : {}),
    };
  } finally {
    host.signal?.removeEventListener('abort', cancel);
    host.beforeRequest = previousBeforeRequest;
    host.request = previousHostRequest;
    await ctx.root.fiber.dispose();
    await Promise.all(notifications);
  }
}
