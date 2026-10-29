/** Native token-count adapter for the official Harness compaction services. */
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { resolve, sep } from 'node:path';
import { Service } from '@deepseek-ai/cordis';
import { Tokenizer } from '@huggingface/tokenizers';
import { Template } from '@huggingface/jinja';
import BasicCompactionEngine from '@deepseek-ai/dsh-compaction-basic';
import ToolResultPruner from '@deepseek-ai/dsh-compaction-tool-result-pruner';
import { BlockAssembler, LlmError, ReasoningEffortId, assembleAssistantStream, createUserMessage } from '@deepseek-ai/dsh-llm';
import { Session, ToolCallRecovery, headerEquals } from '@deepseek-ai/dsh-session';
import { buildPayload, generationAllowance } from './adapter.mjs';

const CONTINUE = 'Continue checking the unresolved code-review items against the available read-only sources, then return the final review report.';
const SUMMARY_DIRECTIVE = 'Condense this same independent code review into a short continuation checkpoint. Preserve concrete findings, evidence paths and ranges, actually delivered source identities and ranges, distinguishing delivery from completed review, unresolved checks and uncertainty. Do not conduct a new review, invent conclusions, copy entire source files, include operational logs, or call tools. Keep any prior checkpoint facts that remain relevant. Output only the checkpoint text.';
const textBlocks = blocks => (blocks || []).filter(block => block.type === 'text' || block.type === 'reasoning').map(block => block.text).join('\n');
const asUser = (text, kind = 'review-progress') => createUserMessage({ content: [{ type: 'text', text }], source: { kind } });
const routeOf = spec => spec.route || spec;
const settingsOf = spec => spec.contextManagement || spec.context_management || {};
const positive = (value, fallback) => Number.isInteger(value) && value > 0 ? value : fallback;
const nonnegative = (value, name) => {
  if (!Number.isSafeInteger(value) || value < 0) throw new LlmError(`${name} must be a non-negative integer`, 'INVALID_CONTEXT_BUDGET');
  return value;
};

// Added tokens are hard tokenizer boundaries, so their surrounding sections
// can be encoded independently without approximating cross-boundary BPE. Keep
// this fast path narrower than the supported asset formats: unfamiliar token
// normalization/whitespace rules fall back to the unchanged complete encoder.
function nativeSectionPatterns(definition, config) {
  const added = definition.added_tokens;
  const normalizer = definition.normalizer;
  const isolated = value => value?.type === 'Split' || value?.type === 'ByteLevel' && value.add_prefix_space === false
    || value?.type === 'Sequence' && Array.isArray(value.pretokenizers) && value.pretokenizers.every(isolated);
  if (!Array.isArray(added) || !added.length || config.remove_space || config.do_lowercase_and_remove_accent
    || normalizer && !(normalizer.type === 'Sequence' && normalizer.normalizers?.length === 0)
    || !isolated(definition.pre_tokenizer) || definition.post_processor?.type !== 'ByteLevel'
    || definition.model?.type !== 'BPE' || definition.model.dropout != null && definition.model.dropout !== 0
    || added.some(token => typeof token.content !== 'string' || !token.content || typeof token.normalized !== 'boolean'
      || token.single_word !== false || token.lstrip !== false || token.rstrip !== false)) return null;
  // The maintained tokenizer isolates raw added tokens first, then normalized
  // ones. Preserve that two-stage priority even for overlapping marker names;
  // the guarded identity normalizer makes both stages exact literal matches.
  const pattern = normalized => {
    const literals = [...new Set(added.filter(token => token.normalized === normalized).map(token => token.content))]
      .sort((a, b) => b.length - a.length).map(text => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
    return literals.length ? new RegExp(literals.join('|'), 'g') : null;
  };
  return { raw: pattern(false), normalized: pattern(true) };
}

/** The same native tokenizer/template estimate as the material host; never a second budget policy. */
export class PayloadTokenCounter {
  constructor(route, tokenizerDirectory = new URL('../assets/deepseek_v4_tokenizer/', import.meta.url)) {
    this.route = route;
    this.native = String(route.model).toLowerCase().startsWith('deepseek');
    if (!this.native) return;
    const directory = tokenizerDirectory instanceof URL ? tokenizerDirectory : pathToFileURL(resolve(tokenizerDirectory) + sep);
    const config = JSON.parse(readFileSync(new URL('tokenizer_config.json', directory), 'utf8'));
    const definition = JSON.parse(readFileSync(new URL('tokenizer.json', directory), 'utf8'));
    this.tokenizer = new Tokenizer(definition, config);
    this.sectionPatterns = nativeSectionPatterns(definition, config);
    this.textCache = new Map();
    this.cachedTextChars = 0;
    // A cache eviction only repeats exact encoding; it never changes counts.
    this.maxCachedTextChars = 8_000_000;
    this.maxCachedTextItems = 4096;
    this.template = new Template(config.chat_template);
    this.bos = typeof config.bos_token === 'string' ? config.bos_token : config.bos_token?.content || '';
  }
  encodeText(text) {
    if (!text) return 0;
    if (this.textCache.has(text)) {
      const tokens = this.textCache.get(text);
      this.textCache.delete(text);
      this.textCache.set(text, tokens);
      return tokens;
    }
    const tokens = this.tokenizer.encode(text, { add_special_tokens: false }).ids.length;
    if (text.length <= this.maxCachedTextChars && this.maxCachedTextItems > 0) {
      while (this.textCache.size && (this.cachedTextChars + text.length > this.maxCachedTextChars
        || this.textCache.size >= this.maxCachedTextItems)) {
        const oldest = this.textCache.keys().next().value;
        this.textCache.delete(oldest);
        this.cachedTextChars -= oldest.length;
      }
      this.textCache.set(text, tokens);
      this.cachedTextChars += text.length;
    }
    return tokens;
  }
  sectionTokens(text, pattern, encode) {
    if (!pattern) return encode(text);
    let tokens = 0, cursor = 0;
    for (const match of text.matchAll(pattern)) {
      tokens += encode(text.slice(cursor, match.index)) + 1;
      cursor = match.index + match[0].length;
    }
    return tokens + encode(text.slice(cursor));
  }
  text(text) {
    if (!this.sectionPatterns) return this.encodeText(text);
    const normalized = value => this.sectionTokens(value, this.sectionPatterns.normalized, section => this.encodeText(section));
    return this.sectionTokens(text, this.sectionPatterns.raw, normalized);
  }
  count(payload) {
    const messages = payload.messages || [], tools = payload.tools || [];
    if (!this.native) return messages.reduce((n, item) => n + Buffer.byteLength(JSON.stringify(item)), 0)
      + (tools.length ? Buffer.byteLength(JSON.stringify(tools)) : 0) + 16 * messages.length + (tools.length ? 16 : 0);
    const normalized = messages.map(message => message.role === 'assistant' && message.content == null ? { ...message, content: '' } : message);
    const rendered = this.template.render({ messages: normalized, bos_token: this.bos, add_generation_prompt: true });
    let tokens = this.text(rendered) + (tools.length ? this.text(JSON.stringify(tools)) : 0);
    for (const message of messages) {
      if (message.role === 'assistant' && message.tool_calls?.length) tokens += this.text(JSON.stringify(message.tool_calls));
      for (const key of ['reasoning_content', 'reasoning']) if (typeof message[key] === 'string' && message[key]) tokens += this.text(message[key]);
      if (!['system', 'user', 'assistant', 'tool'].includes(message.role)) tokens += this.text(JSON.stringify(message));
    }
    return tokens;
  }
  options(options) { return this.count(buildPayload(options, this.route)); }
}

/** Minimal meter seam; pressure policy, range selection and commits remain upstream. */
export class NativeReviewMeter extends Service {
  constructor(ctx, config) {
    super(ctx, 'tokenMeter');
    this.route = config.route;
    this.counter = new PayloadTokenCounter(config.route, config.tokenizerDirectory);
    this.prices = new WeakMap();
    this.anchors = new WeakMap();
    this.rawCache = new WeakMap();
    this.coldChecked = new WeakSet();
    this.failure = null;
    ctx.on('session/event', (session, event) => {
      if (event.type !== 'assistant/message' || !event.data.usage) return;
      try {
        const usage = event.data.usage;
        // Harness usage buckets are disjoint: adapters subtract cached input
        // before exposing inputTokens. Reasoning is already in outputTokens.
        const tokens = usage.inputTokens + (usage.cacheReadTokens || 0) + (usage.cacheWriteTokens || 0) + usage.outputTokens;
        if (!Number.isFinite(tokens) || tokens < 0) throw new Error('Review meter received invalid provider usage');
        const header = session.requestHeader();
        this.anchors.set(session, { header, native: this.rawMeasure(session, header).totalTokens, tokens, usage });
      } catch (error) {
        // Session observers contain exceptions. Expose this failure at the
        // next awaited request/flush boundary rather than silently losing it.
        this.failure = error;
      }
    });
    ctx.on('session/flush', () => this.assertHealthy());
  }
  options(messages, header, session) {
    return { provider: header?.config.provider || 'review', model: header?.config.model || this.route.model,
      messages, tools: header?.tools || [], toolHistory: session?.toolHistory(), maxTokens: header?.config.maxTokens };
  }
  estimateMessage(message) {
    if (this.prices.has(message)) return this.prices.get(message);
    const value = this.counter.options(this.options([message]));
    this.prices.set(message, value);
    return value;
  }
  rawMeasure(session, requestHeader = session.requestHeader()) {
    const cached = this.rawCache.get(session);
    const sameHeader = cached && (cached.header === undefined || requestHeader === undefined
      ? cached.header === requestHeader : headerEquals(cached.header, requestHeader));
    if (sameHeader && cached.generation === session.surface.contentGeneration
      && cached.nodeCount === session.surface.nodes.length && cached.tail === session.surface.nodes.at(-1))
      return { ...cached.measurement, logRevision: session.seq };
    const nodes = [], messages = [];
    for (const seq of session.surface.nodes) {
      const message = session.deriveEventMessage(session.eventAt(seq));
      if (!message) throw new Error('Review meter cannot price a surface event without its projected message');
      const tokens = this.estimateMessage(message);
      nodes.push({ seq, tokens, heuristicTokens: tokens });
      messages.push(message);
    }
    const tokens = this.counter.options(this.options(messages, requestHeader, session));
    const measurement = { logRevision: session.seq, baseline: { kind: 'estimated', tokens }, surfaceDeltaTokens: 0,
      totalTokens: tokens, surfaceTokens: nodes.reduce((sum, node) => sum + node.tokens, 0),
      nodes: Object.freeze(nodes.map(node => Object.freeze(node))) };
    this.rawCache.set(session, { header: requestHeader, generation: session.surface.contentGeneration,
      nodeCount: session.surface.nodes.length, tail: session.surface.nodes.at(-1), measurement });
    return measurement;
  }
  assertHealthy() { if (this.failure) throw this.failure; }
  restoreAnchor(session) {
    if (this.coldChecked.has(session) || this.anchors.has(session)) return;
    this.coldChecked.add(session);
    const events = session.snapshotEvents();
    for (let index = events.length - 1; index >= 0; index -= 1) {
      const event = events[index];
      if (event.type !== 'assistant/message' || !event.data.usage) continue;
      const usage = event.data.usage;
      const tokens = usage.inputTokens + (usage.cacheReadTokens || 0) + (usage.cacheWriteTokens || 0) + usage.outputTokens;
      if (!Number.isFinite(tokens) || tokens < 0) throw new Error('Review meter received invalid persisted usage');
      // Reuse the official Session fold for the historical surface. This is a
      // detached, read-only reconstruction, not a replay of API or tool calls.
      const prefix = Session.create(session.id, events.slice(0, index + 1), session.header);
      const header = prefix.requestHeader();
      this.anchors.set(session, { header, native: this.rawMeasure(prefix, header).totalTokens, tokens, usage });
      return;
    }
  }
  measure(session, requestHeader = session.requestHeader()) {
    this.assertHealthy();
    this.restoreAnchor(session);
    const measured = this.rawMeasure(session, requestHeader), anchor = this.anchors.get(session);
    if (anchor?.header && requestHeader && headerEquals(anchor.header, requestHeader)) {
      const anchored = anchor.tokens + measured.totalTokens - anchor.native;
      if (anchored > measured.totalTokens) return { ...measured, baseline: { kind: 'usage', tokens: anchor.tokens, usage: anchor.usage },
        surfaceDeltaTokens: measured.totalTokens - anchor.native, totalTokens: anchored };
    }
    return measured;
  }
}

function currentText(attempt) {
  const data = attempt?.data || attempt;
  if (data?.message?.content) return textBlocks(data.message.content);
  return data?.stream?.length ? textBlocks(assembleAssistantStream(data.stream).blocks()) : '';
}
function repairLengthTools(session, attempt) {
  if (!attempt || !(attempt.data?.message?.content || []).some(block => block.type === 'tool-call')) return;
  const repair = new ToolCallRecovery();
  repair.observe(attempt);
  for (const event of repair.results()) session.append('tool/result', event.data,
    { surfaceOp: 'append', ...(event.sourceEventSeqs ? { sourceEventSeqs: event.sourceEventSeqs } : {}) });
}

/** Mount the official compactor/pruner over one precise counter backend. */
export async function mountContext(ctx, spec, host = {}) {
  const route = routeOf(spec), settings = settingsOf(spec);
  const summaryCap = Math.min(positive(settings.compaction_max_tokens, 16384), positive(route.max_tokens, Infinity));
  const configuredRatio = settings.compression_trigger_ratio ?? 0.8;
  const declaredWindow = positive(route.context_window_tokens, 1000000);
  const safety = settings.safety_tokens === undefined ? 0 : nonnegative(settings.safety_tokens, 'safety_tokens');
  const baseHeadroom = Math.max(nonnegative(settings.headroom_tokens ?? 65536, 'headroom_tokens'), safety);
  const selectedModel = await ctx.llm.resolveModelInfo('review', route.model, host.signal);
  // An omitted generation cap remains omitted on the wire. Preserve the old
  // planning-only reserve through the official engine's headroom configuration.
  const fallbackReserve = generationAllowance(route) === undefined && selectedModel.defaultMaxTokens === undefined
    ? nonnegative(settings.auto_output_reserve_tokens ?? 393216, 'auto_output_reserve_tokens') : 0;
  const effectiveHeadroom = baseHeadroom + fallbackReserve;
  const declaredInput = positive(route.max_input_tokens, declaredWindow) - safety;
  const thresholdRatio = Math.min(configuredRatio, declaredInput / declaredWindow);
  await ctx.plugin(NativeReviewMeter, { route, tokenizerDirectory: spec.tokenizerDirectory || spec.tokenizer_dir });
  await ctx.plugin(ToolResultPruner, {});
  class ReviewCompaction extends BasicCompactionEngine {
    async summarize(input, agent, signal) {
      const header = agent.session.requestHeader();
      const target = header?.config || agent.options;
      const assembler = new BlockAssembler();
      const options = { provider: target.provider || 'review', model: target.model || route.model,
        messages: [...input.messages, asUser(SUMMARY_DIRECTIVE, 'review-compaction-instruction')],
        ...(input.tools ? { tools: [...input.tools] } : {}), toolHistory: agent.session.toolHistory(),
        maxTokens: summaryCap, purpose: 'compaction',
        reasoningEffort: ReasoningEffortId('none'), sessionId: agent.session.id, ...(signal ? { signal } : {}) };
      const model = await ctx.llm.resolveModelInfo(options.provider, options.model, signal);
      const inputTokens = ctx.tokenMeter.counter.options(options);
      const window = positive(model.context?.contextWindow, declaredWindow);
      if (inputTokens + safety > positive(route.max_input_tokens, window) || inputTokens + summaryCap + safety > window)
        throw new LlmError('Checkpoint input does not fit the selected model limits', 'INVALID_CONTEXT_BUDGET');
      for await (const chunk of ctx.llm.stream(options)) assembler.push(chunk);
      const finish = assembler.finish;
      if (finish?.kind === 'error' || finish?.kind === 'aborted') throw new LlmError(finish.failure.message, finish.failure.code, finish.failure);
      if (finish?.kind !== 'stop') throw new Error(finish?.kind === 'max-tokens' ? 'Review checkpoint was truncated' : 'Review checkpoint request did not complete');
      const rawOutput = assembler.blocks();
      const summary = rawOutput.filter(block => block.type === 'text');
      if (!summary.some(block => block.text.trim())) throw new Error('Review checkpoint contains no usable text');
      return { summary, rawOutput, llmStreamCall: true, provider: options.provider, model: options.model,
        maxTokens: options.maxTokens, ...(assembler.usage ? { usage: assembler.usage } : {}) };
    }
  }
  const requestedRetention = (settings.retain_recent_ratio ?? 0.16)
    * Math.max(0, declaredWindow - fallbackReserve) / declaredWindow;
  // A provider input cap can be much smaller than its total context window.
  // Adapt only the retention configuration to that smaller capacity; the
  // official engine still owns threshold resolution and range selection.
  const retention = Number.isInteger(settings.retain_recent_tokens)
    ? { retainTokens: Math.min(Math.max(0, settings.retain_recent_tokens), Math.max(0, Math.floor(declaredInput / 2))) }
    : { retainRatio: requestedRetention < thresholdRatio ? requestedRetention : thresholdRatio / 2 };
  await ctx.plugin(ReviewCompaction, { thresholdRatio,
    headroomTokens: effectiveHeadroom, maxTokens: summaryCap, ...retention });
  const compaction = ctx.compaction, meter = ctx.tokenMeter;
  async function force(agent, signal = new AbortController().signal) {
    const before = meter.measure(agent.session).totalTokens;
    const generation = agent.session.surface.replaceGeneration;
    const result = await compaction.compactIfNeeded(agent, 'context-overflow', signal);
    return !!result && agent.session.surface.replaceGeneration > generation && meter.measure(agent.session).totalTokens < before;
  }
  return {
    meter, compaction,
    assertHealthy() { meter.assertHealthy(); },
    async ensureRequest(agent, options = {}) {
      meter.assertHealthy();
      if (options.purpose === 'compaction') return false;
      const header = agent.session.requestHeader();
      if (!header) throw new LlmError('Review preflight requires its canonical request header', 'INVALID_CONTEXT_BUDGET');
      const info = await ctx.llm.resolveModelInfo(header.config.provider, header.config.model, options.signal);
      const window = info.context?.contextWindow;
      const reserved = header.config.maxTokens ?? info.defaultMaxTokens ?? 0;
      const headroom = compaction.config.headroomTokens;
      if (!Number.isSafeInteger(window) || window <= 0 || !Number.isSafeInteger(reserved) || reserved < 0
          || reserved + headroom >= window)
        throw new LlmError('Configured review output and headroom leave no input room', 'INVALID_CONTEXT_BUDGET');
      const maxInput = positive(route.max_input_tokens, window) - safety;
      const fits = () => {
        const input = meter.measure(agent.session, header).totalTokens;
        return input < Math.min(maxInput, Math.floor(window * compaction.config.thresholdRatio))
          && input + reserved + headroom <= window;
      };
      const generation = agent.session.surface.replaceGeneration;
      try {
        await compaction.compactIfNeeded(agent, 'pressure', options.signal || new AbortController().signal);
      } catch (error) {
        // Upstream pruning may already have committed enough space even if an
        // optional summary then fails. A real reduction can rebuild safely.
        if (fits() && agent.session.surface.replaceGeneration > generation) return true;
        throw error;
      }
      if (fits()) return agent.session.surface.replaceGeneration > generation;
      // The initial request can consist of one oversized user node, which the
      // normal retained-tail selection cannot compact. Retain a small explicit
      // continuation and let the same upstream overflow transaction condense it.
      agent.session.append('user/message', asUser(CONTINUE, 'review-continuation'), { surfaceOp: 'append' });
      if (!await force(agent, options.signal))
        throw new LlmError('Review context cannot be reduced before sending a model request', 'INVALID_CONTEXT_BUDGET');
      return true;
    },
    async recover(agent, failure, options = {}) {
      const notes = currentText(options.attempt);
      if (failure.code !== 'CONTEXT_WINDOW_EXCEEDED' && !notes.trim()) return false;
      if (notes.trim()) agent.session.append('user/message', asUser('Provisional task notes from this same reviewer; retain their uncertainty:\n' + notes), { surfaceOp: 'append',
        ...(options.attempt?.seq !== undefined ? { sourceEventSeqs: [options.attempt.seq] } : {}) });
      // Upstream zero-retention selection preserves one final node. A useful
      // continuation keeps that small node while the progress itself compacts.
      agent.session.append('user/message', asUser(CONTINUE, 'review-continuation'), { surfaceOp: 'append' });
      return force(agent, options.signal);
    },
    async continueLength(agent, options = {}) {
      repairLengthTools(agent.session, options.attempt);
      agent.session.append('user/message', asUser(CONTINUE, 'review-continuation'), { surfaceOp: 'append' });
      if (!await force(agent, options.signal)) return false;
      agent.followup(asUser(CONTINUE, 'review-continuation'));
      return true;
    },
    async recoverLegacy(agent, progress, options = {}) {
      const signal = options.signal ?? host.signal ?? new AbortController().signal;
      signal.throwIfAborted();
      const allowed = ['prior_checkpoint', 'completed_work', 'internal_notes', 'read_sources', 'inline_delivered_ranges'];
      const notes = typeof progress === 'string' ? progress : JSON.stringify(Object.fromEntries(allowed.filter(key => progress?.[key] !== undefined).map(key => [key, progress[key]])));
      if (!notes?.trim()) return true;
      agent.session.append('user/message', asUser(notes, 'review-imported-progress'), { surfaceOp: 'append' });
      // Already compact task progress is admitted directly, without paying for
      // a larger framed summary. This threshold never truncates the material.
      const noteTokens = meter.counter.native ? meter.counter.text(notes) : Buffer.byteLength(notes);
      if (noteTokens <= summaryCap) return true;
      agent.session.append('user/message', asUser(CONTINUE, 'review-continuation'), { surfaceOp: 'append' });
      const before = meter.measure(agent.session).totalTokens;
      const result = await compaction.compactNow(agent, signal);
      return !!result && meter.measure(agent.session).totalTokens < before;
    },
  };
}
