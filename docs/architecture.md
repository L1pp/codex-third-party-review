# Runtime architecture and boundaries

The live review driver is the official DeepSeek Harness AgentLoop. Python supplies review materials and observes results; it no longer implements the model/tool loop. The controlling GPT session continues to plan, verify findings, and accept changes.

## Active components

| Component | Responsibility |
| --- | --- |
| `scripts/review.py` | CLI, route selection, local credentials, and backward-compatible single-request API |
| `scripts/harness_runner.py` | Start the Skill-local Node bridge, serve read-only source requests, and import unfinished records |
| `scripts/harness_records.py` | Export raw attempts, progress, report diagnostics, timing, and the SQLite index |
| `scripts/repository_source.py`, `materials.py` | Git/workspace views, captured source versions, initial inline materials, and paginated reads |
| `runtime/engine.mjs` | Compose official AgentLoop, Session, retry, and native tool execution with the review policy |
| `runtime/adapter.mjs` | Bailian/OpenAI wire fields, maintained pi-ai transport, raw-response observation, and secret masking |
| `runtime/context.mjs` | One native-counter adapter under official compaction and pruning services |
| `runtime/persistence.mjs` | No-hash storage adapter for the official SessionPersistence contract |
| `runtime/vendor/dsh-pi-*.mjs` | MIT-attributed upstream message/replay and stream converters |
| `scripts/workflow.py` | Small compatibility entry; no request/tool state machine |

Runtime dependencies are pinned in `runtime/package.json`: DeepSeek Harness packages `0.2.1-rc.2`, Cordis `4.0.5`, and pi-ai `1.3.0`. The published rc.2 tools package contains its native executor; a similarly named separate executor on upstream main is not a required rc.2 package. See the [module reuse inventory](harness-reuse.md) for the inspected upstream paths, adopted seams, and deliberate exclusions.

The old Requests/SSE model client and Python loop are not live alternatives. `review.run_review(...)` preserves a single-request interface through the same Node protocol adapter: it makes one attempt, does not execute tools, and does not start an implicit agent loop or retry cycle.

## Installation and process boundary

Git, Python 3.10+, and Node.js **24.9.0 or newer with npm** are required. `scripts/skill_runtime.py setup` creates the Skill's `.venv`, installs Python host dependencies, checks or installs the local official tokenizer assets, and installs exact Node dependencies under `runtime/node_modules`. npm runs with lifecycle scripts, audit, and funding messages disabled; setup does not call a model or load credentials. It does not install or alter system Node.js, the project environment, or the main Codex model configuration.

The Python entrypoint re-enters its own isolated interpreter. The bridge ignores inherited Node import/options settings and does not use ambient proxy routing for model requests. Credentials travel through the private process protocol, not command-line arguments. Missing Node or dependencies produce an actionable setup diagnostic. Authorized live calls can repair missing Skill-local packages; `--history`, `--route-only`, `--runtime-info`, `--dry-run`, and help do not install packages or call models.

A dry run prepares materials and an initial request in Python; it does not boot the live agent conversation or prove service availability. A dedicated Node runtime is interpreter/dependency isolation, not an OS sandbox.

## Review and tool flow

```text
GPT host -> Python CLI: repository, target, requirements, existing evidence
  -> RepositorySource / MaterialStore: initial material bundle and read-only tools
  -> Node bridge: official Session + AgentLoop
       -> maintained pi-ai OpenAI transport -> configured model endpoint
       -> official native tool executor -> Python source/material functions
       -> official retry and compaction services over the same session
  -> raw attempts + source records + final report/diagnostics
  -> GPT host verifies each finding and records its decision
```

Only `list_files`, `read_file`, `search_code`, and material index/read/search functions are registered. The runtime does not mount upstream shell, PTY, sandbox, filesystem write/edit, browser, desktop, or subagent plugins. Restricting a tool list alone would not prevent activation side effects, so those plugin groups are absent from the composition.

The model cannot execute commands, modify code, or run tests through this interface. Host-side Git reads and file capture still require the host project's and platform's normal permissions. Repository text cannot change the endpoint, credential source, or registered tool set.

## Source views and actual delivery

| Primary target | Diff and source view |
| --- | --- |
| `worktree` | Changes relative to HEAD plus eligible untracked text; reads the working tree |
| `staged` | Staged diff and Git index text |
| `range --base <ref>` | Merge-base-to-HEAD diff and HEAD text |

Additional `--workspace-root` aliases always read disk. Roots may overlap, so the same logical file can have different source versions. `--include-file` exposes only the explicitly selected primary-repository disk files through a `supplement` alias. Deleted files can use the recorded baseline. Packet mode grants access only to supplied material identifiers and never discovers additional repository files.

First reads save `source-0001`-style captures with root, relative path, Git view, capture time, content locator, and ranges. Later reads reuse the recorded version; newly read paths after resume are marked separately. A source record can also result from a search scan with no match, so capture does not imply full delivery to the model. Initial inline and tool-delivered ranges are tracked separately, and `coverage.status` remains `not_verified_by_workflow`.

These are per-file reads at different times, not a frozen or atomic whole-workspace snapshot. No hash, fingerprint, or repository-freezing verification is performed. Known credentials and runtime output paths are excluded; active-key masking is not a general secret scanner.

## One context meter

The initial bundle prefers approximately **100,000 locally estimated material tokens** inline. Remaining material is accessible by index and paginated read tools; exploration remains available even when everything initially fits. Python's native tokenizer is used for initial material selection and offline preparation. It is not a second live pressure engine.

The live Node meter uses the same local DeepSeek tokenizer JSON and chat template through Hugging Face's tokenizer/Jinja implementations, pricing the actual OpenAI payload projection, reasoning replay, tool schemas, and tool calls. Auto remains an explicitly marked conservative estimate. Local counts are not provider `usage` guarantees.

The meter is the sole source for official BasicCompactionEngine pressure, region selection, pruning, and summary replacement. Successful usage may anchor a same-route, same-canonical-header estimate; route/tool-schema changes require repricing. OpenAI/Bailian `prompt_tokens` already includes cache reads, so the adapter splits non-overlapping usage buckets before passing them to Harness.

Default DeepSeek settings remain a **1,000,000-token window**, **256,000 output tokens**, and **max** reasoning effort. Auto retains the configured combined output assumption of **393,216 tokens**. The official pressure policy reserves the complete output plus 65,536 headroom and checks the smaller of 80% of the window or the remaining input room. For the default DeepSeek route this is 678,464 input tokens. Headroom already includes the local safety allowance.

Under pressure, the official pruner can shorten older tool-result text, followed by official region compaction when needed. Replacements preserve balanced tool history and durable session events. The summarizer reuses the conversation prefix and appends a review-specific checkpoint instruction; it retains findings, evidence locations, actual read ranges, uncertainty, and unfinished work. A complete framed summary must reduce the replaced region before commit. Truncated or failed summaries do not replace usable history.

The runtime does not interrupt an active stream to insert compaction. Review-specific extensions retain `length` continuation and received-progress recovery after transient stream/deadline errors. These are task policies over the official services, not a second loop or pressure calculator.

## Failure recovery and resume

The official retry plugin owns backoff, Retry-After handling, cancellable waits, and durable retry events. A review-specific outer policy admits temporary no-progress failures within `api_recovery.retry_window_seconds` (default 120; zero disables those repeats). It stops permanent/configuration/credential failures and cancellation. This window controls admission of a new attempt, not the lifetime of a generating request. No new default global model-call or tool-count cap is added; explicit existing limits remain supported.

A failed attempt is retained as an official `assistant/attempt`, not silently promoted to a successful assistant message. Received task progress can be condensed through the official compaction service before retry; operational errors and host-GPT judgments are not checkpoint material. Incomplete tool batches never execute. A returned report, syntactically valid JSON, or a completed request does not imply review approval.

`--resume <unfinished-review> --output-dir <new-directory>` keeps the recorded route/endpoint and captured source views. New-runtime records restore their official session from `engine-session/session-header.json` and `session.jsonl`. Older records use a bounded task-progress/source importer; old API/tool calls are not replayed as new work or usage. The destination is validated before any directory creation and must be independent of the original run. `--dry-run` prepares restoration material without a model call.

New engine-session cold recovery is exercised at the CLI boundary, including missing final results and initial-compaction-only records; orphan-header fallback agrees with the loader, and the storage adapter rejects a still-active writer. Legacy records without an engine session retain their ended-unfinished-run admission rules; arbitrary historical Harness homes are not imported. The no-hash storage adapter keeps official event validation, replay, and interrupted-turn repair semantics; it does not load the stock JSONL backend's digest, checksum, or Windows hash-based lock paths.

## Records, diagnostics, and decisions

Each actual attempt has its own `calls/` directory with request, response metadata, received SSE/raw text, reasoning where available, usage, and status. `tool_results.jsonl` records original local deliveries before context replacements. `engine-session/` holds the canonical durable agent session, while `checkpoint.txt` and `checkpoints/` are compatible derived exports. `workflow.json`, `result.json`, and `report.md` summarize the run; detailed operational records remain local and never become routine model instructions.

The host observer applies active-key redaction across stream boundaries and keeps safe request IDs/rate headers. Raw evidence can still contain code or private paths and must remain private. A logging or progress warning is distinct from a model/provider failure.

`report_parsing.py` delegates syntax repair to pinned `json-repair`; project code retains top-level report selection, ambiguity/loss checks, location diagnostics, and original findings. `report_diagnostics.py` uses supplied or captured text rather than following model-supplied arbitrary file paths. A location match proves neither delivery nor defect validity. `adjudication.json` starts pending and records the main session's `confirmed`, `dismissed`, or `needs_check` decisions separately from the original report.

The lightweight SQLite ledger stores task/model/times/status and output-directory locators, not code, keys, or full responses. Success, failure, `length`-incomplete time, and retry waits are separate in detailed run timing; compaction time is a subset of model request time. Imported usage is not counted as a fresh call, and unavailable provider usage remains unknown.

## Validation boundary

Offline tests exercise the active Node services and Python host with recorded/fake transport. Setup may download packages and official tokenizer assets; the tests do not call model APIs. Real-provider acceptance and per-release test counts must be reported from their actual records. Reusing a mature runtime does not establish better review accuracy, full source coverage, lower latency, or a higher provider deadline.

A native continuation refreshes the read-only material index and keeps material tools available; it does not re-inline the old bulk source history. Per-call inline ranges come from the actual wire envelope, including after preflight compaction. Compatibility checkpoint text exports are advisory: a Windows reader can prevent their update without invalidating the already-committed official session.
