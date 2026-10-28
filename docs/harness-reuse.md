# DeepSeek Harness reuse inventory

This map records the official components actually wired into the review runtime and the adaptations retained for this Skill. Source integration is implemented; release-level offline, real-provider, independent-review, and installation results are recorded separately and are not asserted by this inventory.

## Sources and versions

The official [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness) repository was cloned and inspected on 2026-10-28, including `AGENTS.md`, `LICENSE`, `docs/architecture.md`, and the relevant package sources. The inspected mainline declares `0.2.2-alpha.2`. The active runtime uses the separately checked published **`0.2.1-rc.2`** packages, Cordis **`4.0.5`**, and pi-ai **`1.3.0`**, fixed in [runtime/package.json](../runtime/package.json). A mainline package/export is not assumed available in rc.2; in that release native tool execution is part of `dsh-tools`.

Harness is MIT, copyright (c) 2026 DeepSeek. The two focused extracts under `runtime/vendor/` retain the complete MIT notice. Direct and transitive dependencies retain their own terms; see [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md). This project does not redistribute the full checkout, installed `node_modules`, or tokenizer assets.

## One owner for each generic mechanism

New reviews use the official **AgentLoop + Session + Retry + native Tools** path in `runtime/engine.mjs`. Official **BasicCompactionEngine + ToolResultPruner** operate over one native-counter adapter in `runtime/context.mjs`. `scripts/workflow.py` is a compatibility entry, not another running request/tool loop. The single-request `review.run_review(...)` interface uses the same maintained protocol stack while remaining one attempt without implicit tool execution or retries.

Python retains source acquisition, routing/credentials, initial material selection, local raw exports, report diagnostics/adjudication, and the lightweight directory-index ledger. These are task adapters around the maintained runtime, not replacements for its generic loop. No failure-handling instructions are added to every model's Skill context.

## Implemented replacement map

Upstream paths in this table are relative to the official repository; local paths are relative to this repository. "Adapted" identifies a deliberate seam, not a claim of unmodified end-to-end upstream behavior.

| Capability | Official implementation inspected | Active local integration / decision |
| --- | --- | --- |
| Model/stream/failure vocabulary | `packages/llm/llm/src/` | `runtime/adapter.mjs`, `engine.mjs`, and `context.mjs` use the official LLM contract and structured failures. |
| OpenAI protocol and SSE/tool aggregation | `packages/llm/llm-pi-ai/src/{adapter,context,stream,replay}.ts`; its maintained pi-ai dependency | `runtime/adapter.mjs` directly uses pi-ai's OpenAI Completions provider and message projection. The former Python Requests/SSE model client is retired. |
| Stream/replay conversion | Released `dsh-llm-pi-ai@0.2.1-rc.2` bundle, upstream `stream.ts`/`replay.ts` | `runtime/vendor/dsh-pi-stream.mjs` and `dsh-pi-replay.mjs` are attributed extracts. Malformed completed tool-batch rejection is backported from upstream main. |
| Provider error mapping | `llm-deepseek/src/transport.ts`, `llm-pi-ai/src/stream.ts`, `llm/src/error.ts` | `runtime/adapter.mjs` adapts official categories to Bailian HTTP-200 errors, capacity/deadline details, and safe retry headers; raw evidence remains available. |
| Backoff, Retry-After, cancellation, durable retry events | `llm/src/retry-policy.ts`, `packages/llm/llm-retry/src/index.ts` | `runtime/engine.mjs` mounts official Retry. Its review guard admits eligible failures within the configured recovery window and rejects permanent errors/cancellation. No duplicate Python API retry executor is active. |
| Stream timeout ownership | `packages/util/timeout/src/index.ts`, provider watchdogs | `runtime/adapter.mjs` uses the official timeout/watchdog seam; timeout/cancellation map into official failures. Continuous slow output remains different from idle timeout. |
| Model/tool lifecycle | `packages/core/agent-loop/src/{agent,index,assistant-stream}.ts` | `runtime/engine.mjs` directly mounts official AgentLoop; review-only length/progress continuation is layered on its task boundary. |
| Append-only events and visible history | `packages/core/session/src/{index,surface,request-header,repair}.ts`, `session/session-projection/` | Official Session and projections are the canonical state; Python receives events and derives compatible artifacts rather than rebuilding a second conversation. |
| Failed output versus successful assistant messages | `core/agent-loop/src/agent.ts`: `assistant/attempt` / `assistant/message` | Official failed attempts remain failed. The review extension can compact received task progress; it does not treat a failed attempt as report approval. |
| Session resume and interrupted turn/tool repair | `core/agent-loop/src/index.ts`, `core/session/src/repair.ts` | New-format `engine-session/` records use official reconstruction/repair through the no-hash storage seam. Legacy Python records use the bounded progress/source importer in `scripts/harness_runner.py`. |
| Tool registration, ordering, validation, dispatch | `packages/core/tools/`; mainline `core/tool-executor-native/` | Rc.2 native tool execution in `dsh-tools` is mounted by `runtime/engine.mjs`. Only the host's read-only functions are registered; Python retains source-specific argument and acquisition bounds. |
| Exact active request counting | `packages/llm/token-meter/src/{index,estimate,surface-fold,route-pricing}.ts` and public `estimate` subpath | `runtime/context.mjs` supplies the native tokenizer/template meter contract. It prices the same projected payload as transport, uses valid same-header usage anchors, and is the sole pressure source. Python counting is only initial material/offline preparation. |
| Output reservation, threshold, retained-tail policy | `packages/compaction/compaction-basic/src/config.ts` | The official BasicCompactionEngine resolves the policy. Route/window/output fields are mapped by the adapter rather than independently reimplemented in the Python loop. |
| Unicode-safe old tool-text pruning | `compaction/compaction-tool-result-pruner/src/{index,config}.ts` | Official ToolResultPruner handles durable old-result replacements in `runtime/context.mjs`. Source identities and reread locators remain in the source adapter. |
| Balanced region selection, shrinking summary, cancellation-safe commit | `compaction-basic/src/{region,index}.ts` | Official compaction selects/replaces session regions and checks the framed replacement. `runtime/context.mjs` does not own another region transaction algorithm. |
| Cache-friendly checkpoint request | `compaction-basic/src/summarizer.ts` and its supported `summarize` hook | The local subclass uses the same conversation prefix and a review-specific summary instruction. Incomplete summaries fail before replacement. |
| Context overflow, deadline progress, max-token continuation | `compaction-basic/src/index.ts`, AgentLoop max-token outcome | Official pressure/overflow transactions are reused. The review extension admits failed task progress and continues length-ended reviews, which are not generic upstream defaults. |
| Whole-run observation and first-token facts | `session/session-stats/`, LLM stream helpers, `session/session-telemetry/` | Official stream/session event facts are observed by `engine.mjs` and `scripts/harness_records.py`. The existing per-attempt directory/timing ledger remains; UI statistics and remote telemetry exporters are not mounted. |

## Retained adapters and deliberately unmounted alternatives

| Area inspected | Why it is not a drop-in replacement | Local boundary |
| --- | --- | --- |
| Native `llm-deepseek` Messages transport | Current Token Plan endpoint uses OpenAI-compatible Chat Completions; changing only base URL would change the protocol incorrectly. | Maintained pi-ai OpenAI transport plus a narrow Bailian field/error adapter. |
| Stock `session-persistence-jsonl` | Its checksum, migration/digest, and Windows lock-name paths include explicit hashing and native backend dependencies. | `runtime/persistence.mjs` implements the official persistence contract with no-hash local files/ownership. Official validation, replay, and repair remain reused. |
| `spill-policy`, `spill-local`, `output-retention` | The source tools already return bounded pages with full captured-source locators; another spill layer is not currently required. `spill-local` hashes session-directory names. | Retain source-aware paging, long-line `char_offset`, and the approximately 100k-token preferred initial inline bundle. |
| `fs/tool-fs/src/read-render.ts` | Generic byte/line windows do not supply this project's Git views, source IDs, recorded versions, and resumable long-line cursor contract. | `repository_source.py` / `materials.py` remain the read-only acquisition seam. |
| `util/atomic-write` | Its lock-takeover path hashes a record; its temporary-file disposition differs from critical-payload retention required by local diagnostics. | Python's existing atomic JSON/retained-failure adapter remains; Node session storage uses the explicit no-hash backend. |
| `credentials` / authorization stores | A second account/key store is unnecessary for the existing environment or Bailian profile lookup. | Host credentials stay local and are passed privately to the bridge; no login/account configuration is changed. |
| SDK/profile bundles | Official SDKs and profiles were inspected, but generic profiles activate unrelated services, including shell/PTY/sandbox rows. | A narrow composition mounts only required services; it is a project integration using public packages, not an unmodified official application profile. |
| JSON report repair, location checks, host decisions | The generic agent runtime does not define this code-review report/adjudication contract. | Pinned `json-repair`, project completeness/ambiguity checks, and main-session decisions remain. |
| Model routing, catalog and packaging | They express this installation's service/account/time policy rather than a generic agent loop. | Existing route config and optional official `bl model` catalog adapter remain; `skill_runtime.py setup` prepares both language environments. |

## Non-negotiable compatibility boundaries

- **One retry owner.** The underlying SDK retries remain disabled; official Harness Retry owns live workflow retries and every actual attempt has a raw record. The default official ordinary policy has five retries, 500ms initial/10s maximum delay and 10% jitter, but this integration's outer review admission policy uses its configured time window. Neither creates a new default global model/tool-count cap.
- **Wire failures remain explicit.** Native Messages EOF can raise `STREAM_CLOSED`, which is not in the ordinary upstream retry list; this integration deliberately admits that supported stream category. Malformed completed tool arguments fail the batch. Length-ended partial arguments do not turn into empty tool calls.
- **One meter.** Upstream heuristics are not mislabeled as the official tokenizer. The active Node native counter uses local assets and template semantics; auto remains a conservative marked estimate. Same-route/header usage anchoring must not double count cached input: OpenAI `prompt_tokens` already includes it, while Harness expects non-overlapping buckets.
- **One context transaction.** Official pressure/prune/summary/replacement code decides whether a complete, smaller checkpoint can land. Review-specific instructions preserve evidence, actual read scope, uncertainty and unfinished work while excluding operational logs and host judgments.
- **Actual reads remain distinguishable.** A captured file is not proof of delivery or comprehension. Old source IDs/views survive resume; new reads are separately marked. No whole-workspace snapshot, fingerprint, or freeze verification is added.
- **Records stay private.** Session events, failed attempts, raw request/response streams, usable reasoning, and adjudication remain local. The active key is masked across stream boundaries; this is not a general secret scanner. Unknown provider usage remains unknown, and imported calls are not counted as new API work.

## Capability groups intentionally excluded

The following upstream systems were considered and are not activated: shell/terminal/PTY, sandbox, subprocess/SSH execution, write/edit tools, PTC code execution, browser/computer use, Electron/GUI, arbitrary web/network tools, multimodal attachment/image-offload paths, subagent orchestration, schedules/goals/todo/workflow product features, new identity/login stores, remote telemetry exporters, and historical Harness-home migration/UI.

The reviewer needs one independent read-only code-review task. These groups do not replace a required behavior and could add execution, account, state, or exposure beyond that task. In particular, tool restrictions are not a substitute for leaving unwanted plugins unmounted; their activation can have side effects before any model tool call.

## Validation boundary

Source wiring above is implemented. Release acceptance must exercise the **active** Node services and Python host: transport/reasoning/tool fragments, raw capture, failures and cancellation, retry admission, compaction convergence, new/legacy resume, source views, and isolated installation. Root-level test counts and real-provider/independent-review results must come from the actual release records; release acceptance is recorded below.

Cold recovery for new engine sessions is covered by the active host/official-session fixtures, including missing final results, initial-compaction-only records, orphan-header fallback and rejection of a still-active writer. Legacy records without an engine session retain their ended-unfinished-run rules. Passing old mocks of the retired Python loop is not evidence for the new path. A completed engineering migration does not establish higher review quality, complete defect recall, lower latency, or a larger vendor deadline.

## Release acceptance — 2026-10-28

- Source regression: 342 Python tests, 338 passed and four platform-specific skips; 97 Node tests passed. The retired Python loop/parser mocks were mapped to active-service and host contracts, rather than retained as a second runtime.
- Real serial gateway checks: auto and DeepSeek each completed four requests and six read-only tool calls, returning valid reports. These are protocol/lifecycle checks on a small fixture, not model-ranking evidence.
- One independent review of the integration completed. Five configuration/cancellation/recovery findings were addressed with offline regression; no automatic second independent review was opened.
- Configuration, keys, tokenizer assets and original review records remain local. The main Codex model configuration was not changed.
