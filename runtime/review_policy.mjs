/** Host-clock review guidance: durable soft notices, never a stopping budget. */
import { performance } from 'node:perf_hooks';
import { createUserMessage } from '@deepseek-ai/dsh-llm';
import { appendPluginRecord, pluginRecordOf } from '@deepseek-ai/dsh-session';

const RECORD = 'plugin:review/timing';
const SOURCE = 'review-runtime-clock';
const stages = ['remind', 'conclude'];
const finite = value => typeof value === 'number' && Number.isFinite(value) && value >= 0;
function seconds(value, fallback, name) {
  const result = value ?? fallback;
  if (!finite(result)) throw new TypeError(`reviewPolicy.${name} must be finite and non-negative`);
  return result;
}
function utc(milliseconds) {
  if (!Number.isFinite(milliseconds)) throw new TypeError('Review wall clock must return epoch milliseconds');
  return new Date(milliseconds).toISOString();
}
function isState(value) {
  return value?.version === 1 && finite(value.elapsed_seconds) && typeof value.started_utc === 'string'
    && Array.isArray(value.notified_stages) && value.notified_stages.every(stage => stages.includes(stage));
}

export class ReviewPolicy {
  constructor(settings = {}, clock = {}) {
    this.profile = settings.profile ?? 'standard';
    if (!['standard', 'deep'].includes(this.profile)) throw new TypeError('reviewPolicy.profile must be standard or deep');
    this.remind = seconds(settings.remind_after_seconds, 600, 'remind_after_seconds');
    this.conclude = seconds(settings.conclude_after_seconds, 1200, 'conclude_after_seconds');
    if (this.remind > 0 && this.conclude > 0 && this.conclude < this.remind)
      throw new TypeError('reviewPolicy.conclude_after_seconds must not precede remind_after_seconds');
    this.timezone = settings.timezone ?? 'Asia/Shanghai';
    this.formatter = new Intl.DateTimeFormat('en-CA', { timeZone: this.timezone, year: 'numeric', month: '2-digit',
      day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' });
    this.monotonic = clock.monotonic ?? (() => performance.now());
    this.wall = clock.wall ?? (() => Date.now());
    this.initial = seconds(settings.initial_elapsed_seconds, 0, 'initial_elapsed_seconds');
    this.resume = seconds(settings.resume_elapsed_seconds, 0, 'resume_elapsed_seconds');
    this.origin = this.monotonic();
    if (!Number.isFinite(this.origin)) throw new TypeError('Review monotonic clock must return milliseconds');
    this.runStarted = utc(this.wall());
    if (settings.started_utc !== undefined && (typeof settings.started_utc !== 'string'
        || !/(?:Z|[+-]\d{2}:\d{2})$/.test(settings.started_utc) || !Number.isFinite(Date.parse(settings.started_utc))))
      throw new TypeError('reviewPolicy.started_utc must be an ISO timestamp with a UTC offset');
    this.started = settings.started_utc === undefined
      ? utc(Date.parse(this.runStarted) - this.initial * 1000) : utc(Date.parse(settings.started_utc));
    this.notified = new Set();
    this.base = this.initial + this.resume;
    this.lastElapsed = this.base;
  }

  restore(events) {
    let persisted = 0;
    for (const event of events) {
      const record = pluginRecordOf(event);
      if (record?.type === RECORD && isState(record.data)) {
        persisted = Math.max(persisted, record.data.elapsed_seconds);
        this.started = record.data.started_utc;
        for (const stage of record.data.notified_stages) this.notified.add(stage);
      }
      this.observe(event);
    }
    // An explicit host recovery duration and a native record describe the same
    // previous active work. They are alternatives, not additive durations.
    this.base = this.initial + Math.max(this.resume, persisted);
    this.lastElapsed = Math.max(this.lastElapsed, this.base);
  }

  observe(event) {
    if (event?.type !== 'user/message') return undefined;
    const source = event.data?.source;
    if (source?.kind !== SOURCE || source.version !== 1 || !stages.includes(source.stage)
        || !finite(source.elapsed_seconds)) return undefined;
    this.notified.add(source.stage);
    return source.stage;
  }

  snapshot() {
    const current = this.monotonic();
    if (!Number.isFinite(current)) throw new TypeError('Review monotonic clock must return milliseconds');
    this.lastElapsed = Math.max(this.lastElapsed, this.base + Math.max(0, current - this.origin) / 1000);
    const wall = this.wall();
    const parts = Object.fromEntries(this.formatter.formatToParts(new Date(wall)).map(part => [part.type, part.value]));
    return { version: 1, profile: this.profile, started_utc: this.started, run_started_utc: this.runStarted,
      current_utc: utc(wall), local_time: `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`,
      timezone: this.timezone, elapsed_seconds: Math.round(this.lastElapsed * 1000) / 1000,
      notified_stages: stages.filter(stage => this.notified.has(stage)),
      remind_after_seconds: this.remind, conclude_after_seconds: this.conclude };
  }

  checkpoint(session) {
    const state = this.snapshot();
    // Official ignorable plugin records are log-only: no model context and no
    // parsing of untrusted file/tool/message text to restore timing authority.
    appendPluginRecord(session, RECORD, state);
    return state;
  }

  notice() {
    const state = this.snapshot();
    let stage;
    if (this.conclude > 0 && state.elapsed_seconds >= this.conclude && !this.notified.has('conclude')) stage = 'conclude';
    else if (this.remind > 0 && state.elapsed_seconds >= this.remind && !this.notified.has('remind') && !this.notified.has('conclude')) stage = 'remind';
    if (!stage) return undefined;
    const scope = this.profile === 'deep'
      ? 'Keep deeper checks tied to concrete unresolved contracts or data-flow evidence.'
      : 'Focus on the changed behavior, direct callers/dependencies and relevant tests; extend further for a concrete unresolved concern.';
    const guidance = stage === 'conclude'
      ? 'Consolidate the evidence already collected and work toward the final report. Continue any useful tools or checks you judge necessary; unresolved evidence can be stated in context_gaps.'
      : 'Review the evidence collected so far, prioritize unresolved consequential concerns and work toward a report once those checks are sufficient.';
    const text = `Runtime timing notice from the local host clock: ${Math.floor(state.elapsed_seconds)} seconds of active review have elapsed. `
      + `Local time: ${state.local_time} (${state.timezone}). ${guidance} ${scope} `
      + 'This is a soft reminder only, with no time, tool-call or round limit. The runtime calculates elapsed time; do not estimate time yourself.';
    return createUserMessage({ source: { kind: SOURCE, version: 1, stage, elapsed_seconds: state.elapsed_seconds },
      content: [{ type: 'text', text }] });
  }
}
