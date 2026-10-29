# Codex Third-party Code Review

English | [简体中文](README.zh-CN.md)

A Codex Skill for supplementary, repository-aware code review through an external model API.

Your main GPT session supplies the project, review target, requirements, and existing test evidence. The runtime collects the initial changes, and an external model can explore the repository through read-only tools before returning findings. The main session verifies those findings and decides what to change. Use it for code, tests, dependencies, migrations, and configuration that affects runtime behavior. Documentation-only changes and read-only queries are skipped by default.

This Skill does not change Codex's main model configuration. A completed report is not proof of full coverage or an approval to merge.

## 1. Install the Skill

You need **Git, Python 3.10+, Node.js 24.9.0+ with npm, Codex with permission to run local scripts, and an API key for the external model service**. Your ChatGPT/Codex subscription sign-in does not supply that external API key. The Bailian CLI is optional. Setup prepares this Skill's Python host, official Harness Node dependencies, and the two official tokenizer JSON assets locally. Install a suitable Node.js version first; setup reports a missing/old Node and does not change system Node for you.

Choose the instructions for your platform. These commands are for a **new installation**: the destination must not already exist. If a step fails, resolve the error before continuing. For an existing installation, see the update instructions at the end.

**Windows PowerShell:**

```powershell
$skillDir = Join-Path $HOME ".agents/skills/third-party-code-review"
New-Item -ItemType Directory -Force (Split-Path $skillDir) | Out-Null
git clone https://github.com/L1pp/codex-third-party-review.git $skillDir
```

After cloning successfully:

```powershell
Set-Location $skillDir
python --version
node --version
npm --version
python scripts/skill_runtime.py setup --download-official
Copy-Item config.example.json config.json
```

**macOS / Linux (Bash):**

```bash
mkdir -p "$HOME/.agents/skills"
git clone https://github.com/L1pp/codex-third-party-review.git "$HOME/.agents/skills/third-party-code-review"
```

After cloning successfully:

```bash
cd "$HOME/.agents/skills/third-party-code-review"
python3 --version
node --version
npm --version
python3 scripts/skill_runtime.py setup --download-official
cp config.example.json config.json
```

The rest of this guide uses `python`; substitute `python3` where appropriate. Setup creates the Skill's `.venv`, installs Python host dependencies, checks the local official tokenizer, and installs the exact packages in `runtime/package.json` under `runtime/node_modules`. npm lifecycle scripts, audit, and funding operations are disabled. Setup does not call a model, read API credentials, overwrite personal configuration, or modify individual project environments.

If you already downloaded the official assets, setup can use them without downloading the archive again:

```text
python scripts/skill_runtime.py setup --source-dir "<local tokenizer directory>"
```

The `review.py` and adjudication CLI automatically re-enter the Skill's isolated Python, even when Codex starts them with a project's Python. Live reviews then use the Skill-local Node bridge: the official Harness AgentLoop owns requests, read-only tool continuations, retry, session history, and compaction. Python supplies source material and records the result. No activation or dependency installation in individual projects is needed. Offline queries and dry runs do not automatically install missing packages. This is interpreter/dependency isolation, not an operating-system sandbox.

Check the active runtime without loading credentials or calling a model:

```text
python scripts/review.py --runtime-info
```

The user-level directory `~/.agents/skills/third-party-code-review/` makes the Skill available across projects. For one project only, place the complete repository at that project's `.agents/skills/third-party-code-review/`. Keep the relative layout of `SKILL.md`, `scripts/`, `runtime/` (including its package manifest and attributed vendor files), configuration examples, and supporting files; copying only `SKILL.md` or `scripts/` is insufficient. See the [official OpenAI Skill documentation](https://learn.chatgpt.com/docs/build-skills). If the Skill does not appear, reopen Codex.

## 2. Configure the API key

`config.json` is your local configuration and is ignored by Git. The example uses Alibaba Cloud Bailian **Token Plan** at:

```text
https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
```

Use an API key for the corresponding service and account. By default, the script reads `BAILIAN_TOKEN_PLAN_API_KEY` first; only when it is empty does it read the configured local JSON profile. Other names, such as `DASHSCOPE_API_KEY`, are not detected automatically. To use a different variable name, change `api_key_env` in `config.json`.

Choose one of these methods.

### Option A: Environment variable

PowerShell, with hidden input, for this terminal and child processes started from it:

```powershell
$reviewKey = Read-Host "Token Plan API key" -AsSecureString
$env:BAILIAN_TOKEN_PLAN_API_KEY = [System.Net.NetworkCredential]::new("", $reviewKey).Password
Remove-Variable reviewKey
```

Bash, with hidden input:

```bash
read -r -s -p "Token Plan API key: " BAILIAN_TOKEN_PLAN_API_KEY
printf '\n'
export BAILIAN_TOKEN_PLAN_API_KEY
```

**This does not update the environment of an already-running desktop Codex app.** Manual commands or Codex CLI launched from the same terminal can inherit the variable. For a desktop app launched through the GUI, use the local credentials file below. Do not paste a real key into prompts, review packets, tracked files, or literal command examples.

### Option B: Local credentials file (suitable for desktop Codex; no Bailian CLI required)

Create the parent directory using the command for your platform:

```powershell
New-Item -ItemType Directory -Force (Join-Path $HOME ".config/third-party-code-review") | Out-Null
```

```bash
mkdir -p "$HOME/.config/third-party-code-review"
```

In a local editor, create `~/.config/third-party-code-review/credentials.json`, replacing the placeholder with your key:

```json
{
  "review": {
    "api_key": "REPLACE_WITH_YOUR_API_KEY_KEEP_LOCAL"
  }
}
```

Change these two fields in the installed Skill's `config.json`, leaving all other fields intact:

```json
{
  "credential_file": "~/.config/third-party-code-review/credentials.json",
  "credential_profile": "review"
}
```

This is a **partial configuration**, not a replacement for the whole file. Keep the credentials file private to your local user and out of your projects. A nonempty environment variable still takes precedence.

If you already have a Bailian CLI profile containing `token-plan.api_key` in `~/.bailian/config.json`, you can retain the example's default `credential_file` and `credential_profile`. This project does not create or log in to that profile. The CLI and its public model catalog are optional; see [reuse notes (Chinese)](docs/reuse.md).

## 3. Check model routing

The default configuration uses **Asia/Shanghai (Beijing time)**, independently of your computer's displayed time zone:

| Beijing time when the workflow starts | Requested model | Review reasoning and output configuration |
| --- | --- | --- |
| 08:00 inclusive to 22:00 exclusive | `auto` | Local assumptions of 131,072 answer tokens and 262,144 reasoning tokens; sends combined `max_completion_tokens=393216` |
| 22:00 inclusive to 08:00 exclusive | `deepseek-v4.1-flash-1017` | `reasoning_effort=max`, `max_tokens=256000` |

The route is selected once at the start of a workflow. Material reads or checkpoint continuations do not switch models if the time crosses a boundary. Checkpoint compaction uses a separate, smaller output budget without switching the selected model. The provider selects the underlying model for `auto`; this project cannot identify it from that name. These limits describe configuration and service-adapter assumptions, not a live capability guarantee for every account or model.

Defaults remain a **1,000,000-token context window**, approximately **100,000 locally estimated material tokens inline**, and DeepSeek **max** effort with **256,000 output tokens**. The live runtime uses one native-tokenizer meter beneath official Harness compaction/pruning services. It reserves the complete output plus 65,536 headroom and checks pressure at the smaller of 80% of the window or the remaining input room (678,464 input tokens for the default DeepSeek route). It prunes older tool text before summarizing balanced history and commits only a complete, smaller checkpoint. Initial Python estimates select the material prefix; they are not a second live pressure engine. No active stream is interrupted to compact. Repository exploration remains available even when the initial bundle fits. Local estimates are not provider `usage`; see [architecture](docs/architecture.md) and [the upstream reuse map](docs/harness-reuse.md).

**To use one model all day:** replace the complete `day` and `night` route objects, rather than changing only the model name and retaining another model's reasoning/output parameters. For example, replace both route objects with the following to use DeepSeek throughout the day; retain every other top-level configuration field:

```json
{
  "day": {
    "start": "08:00",
    "end": "22:00",
    "model": "deepseek-v4.1-flash-1017",
    "reasoning_effort": "max",
    "max_tokens": 256000
  },
  "night": {
    "model": "deepseek-v4.1-flash-1017",
    "reasoning_effort": "max",
    "max_tokens": 256000
  }
}
```

To use `auto` all day, copy the default `day` model parameters, except `start` and `end`, into `night`, retaining `day.start/end`. For another provider, configure its HTTPS `base_url`, credentials, and both complete routes together. Changing only the URL does not establish compatibility with reasoning fields, tools, or output parameters.

Other useful settings:

| Setting | Meaning |
| --- | --- |
| `base_url` | HTTPS base URL; the script appends `/chat/completions`. Do not include that suffix twice. |
| `model_catalog_file` | Optional public-catalog cache; relative to the configuration file's directory. Missing cache is allowed; reviews never refresh it implicitly. |
| `ledger_file` | Optional SQLite ledger path; relative paths use the command's working directory. Prefer an absolute path. Defaults to the Skill's `logs/` directory. |
| `node_executable` | Optional Node executable path/name; otherwise use `node` on PATH. Requires version 24.9.0 or newer. |
| `automatic_retries` | Keep `0`; a direct low-level call stays one attempt. The official Harness retry service owns live workflow recovery. |
| `api_recovery.retry_window_seconds` | Defaults to `120`; elapsed window for starting retries of a temporary failure before usable output. `0` disables those retries. |
| `context_management.max_model_calls` / `max_compactions` | `null` by default, with no fixed count limit. Positive integers limit counts. Legacy `0` also means unlimited, not disabled. |

One review workflow may make multiple material-reading or compaction requests. Real requests consume your external service's quota or credits.

### Review scope and clock reminders

The default `standard` profile reviews changed behavior, direct callers/dependencies and relevant tests. It explores further when a concrete contract or data-flow concern needs evidence, and returns a report once the important checks and open concerns are resolved. Missing evidence belongs in `context_gaps`; reading every repository file is not a completion requirement. Use `--review-profile deep` for broader related checks. Both profiles keep repository tools and the configured model/reasoning strength.

```json
"review_policy": {
  "profile": "standard",
  "remind_after_seconds": 600,
  "conclude_after_seconds": 1200
}
```

The runtime calculates elapsed time from the local monotonic clock. Local wall-clock timestamps use the configured timezone, `Asia/Shanghai` by default. At a safe boundary before the next review step it sends one reminder after 10 minutes and one consolidation notice after 20 minutes. It does not interrupt an active response or split a tool batch. If both thresholds pass during one response, only the consolidation notice is needed. The model does not estimate time itself.

**These notices are advisory:** they neither stop tools nor cancel a request, impose a round limit, or mark a long review failed. The reviewer may continue after 20 minutes. A disabled threshold is `0`; changing profiles does not change the two default thresholds. Native resume retains issued notices and elapsed active work up to the last persisted step checkpoint, excluding the shutdown/offline interval. An abrupt crash can lose uncheckpointed time from the in-flight response; these notices do not promise exact crash-time accounting. No per-round time message is added to model context. `review_timing.jsonl` and `review_timing` in the final result provide the host-clock record for observing real behavior.

## 4. Verify setup without calling a model

Run these commands from the installed Skill root. No key is required. Inspect the current route, then simulate the two time periods:

```text
python scripts/review.py --config config.json --route-only
python scripts/review.py --config config.json --route-only --at 2026-10-27T08:00:00+08:00
python scripts/review.py --config config.json --route-only --at 2026-10-27T22:00:00+08:00
```

With the defaults, the simulated routes should show `auto` and `deepseek-v4.1-flash-1017`, respectively. `--at` is only for offline routing or dry runs; it cannot force the route of a real request.

Point the runtime at a local Git project to preview automatic collection and the initial request. Replace the project path with your own:

```text
python scripts/review.py --config config.json --repo "/path/to/project" --target worktree --task "Review the current changes" --dry-run
```

No `packet.json` is needed. On success, terminal JSON has `status` set to `dry_run`, zero actual model calls, and an absolute `output_dir`. If omitted in repository mode, the output directory is generated under the installed Skill's `logs/reviews/` with a UTC timestamp. You can instead provide a new `--output-dir` explicitly.

Both `--route-only` and `--dry-run` avoid loading credentials. A dry run reads project materials and writes local snapshots and planned requests; it does not validate the API key or service availability and cannot remove arbitrary secrets accidentally included in source material. Keep its output private.

## 5. Invoke it in Codex

Once the Skill is installed and credentials are configured, enter this in your development project's Codex session:

```text
Use $third-party-code-review to review this project's current changes. Automatically collect the diff and source context, let the reviewer inspect related files, and have the main session verify and adjudicate its findings.
```

The main session gives the runtime the repository, target, short requirements, and any existing test-evidence file. It no longer needs to copy source into a review packet. The entry point automatically uses the Skill runtime; individual project interpreters need no extra dependencies.

The reviewer can list files, search code, and read paginated text within the selected repository and any supplied workspace roots. These tools remain available even for a small change that was fully inlined. `batch_read` and `batch_search` combine related requests under the same per-response acquisition bounds. Initial diffs use normal three-line context; complete functions remain available on demand. The runtime records the content actually read as numbered source versions; subsequent reads reuse that version. Review-time reads do not execute commands, modify code, or run tests. The main session remains responsible for changes and testing under the host project's and platform's permission rules.

`allow_implicit_invocation=true` permits Codex to match the Skill automatically; **installation does not guarantee a review after every development task**. To request a review by default in a project, add a rule such as the following to that project's `AGENTS.md`. Installation does not modify global instructions:

```text
For code, tests, dependencies, migrations, and configuration changes affecting runtime behavior, have the main session run $third-party-code-review once in addition to normal GPT review. Skip documentation-only changes and read-only queries by default. Supply the repository, review target, requirements, and existing test evidence; let the runtime collect source and the reviewer explore it read-only. The main session verifies and adjudicates findings. Failed or incomplete reports do not count as approval.
```

## 6. Run manually without Codex

From the Skill root, with credentials configured, the following command **sends project materials to the external service and consumes quota or credits**:

```text
python scripts/review.py --config config.json --repo "/path/to/project" --target worktree --task "Review the current changes" --requirement "Preserve the documented public behavior"
```

Repository targets:

| Target | Initial changes and primary repository reads |
| --- | --- |
| `worktree` (default) | Changes against HEAD, including staged and unstaged edits; non-ignored untracked text files are collected separately. Reads use the current working tree. |
| `staged` | Staged diff; reads use the Git index, so unstaged edits are not substituted for the staged version. |
| `range --base <ref>` | Changes from the merge base of the specified ref and HEAD to HEAD; reads use HEAD. The base ref must exist locally. |

Known deleted repository files remain discoverable in `list_files` entries as `deleted/base`; `read_file` returns their selected baseline even when they were omitted from the initial materials. Extra workspace roots retain their working-tree view.

Examples:

```text
python scripts/review.py --config config.json --repo "/path/to/project" --target staged --task "Review staged changes"
python scripts/review.py --config config.json --repo "/path/to/project" --target range --base origin/main --task "Review this branch"
python scripts/review.py --config config.json --repo "/path/to/project" --workspace-root "/path/to/shared-library" --test-evidence "/path/to/test-results.txt" --task "Review the integration"
```

Repeat `--workspace-root` for multiple additional roots, or `--requirement` for multiple short requirements. Roots may overlap; overlap is not rejected. The `repo` alias follows the selected Git view, while `workspace-*` aliases always read from disk. Overlapping roots can therefore expose different versions of the same file; use the root-qualified path and `source_id` when checking a finding, rather than treating a workspace read as the primary Git version. `--test-evidence` reads an existing file; it does not run the described tests. Known credential locations and the runtime's own output are excluded from exploration; skipped initial materials carry reasons. The tools use root-relative paths and do not grant arbitrary filesystem or shell access.

Each file is captured when first read, so different files can reflect different read times. This is a record of the materials observed, **not an atomic snapshot of the whole repository**. Do not claim a finding was checked against a later edit without verifying that edit.

### Inspect the report and record the main session's decisions

The terminal prints the absolute output directory. It contains:

| File | Purpose |
| --- | --- |
| `report.md`, `result.json`, `workflow.json` | Compact visible report with links to full request records, parsed result, workflow completion and context records |
| `calls/`, `call_history.jsonl` | Individual complete requests, response streams, available reasoning, compact call history and timing records |
| `material_delivery_index.json`, `review_timing.jsonl` | Shared delivery index and advisory host-clock events; private local records |
| `sources/index.json`, `sources/source-0001.txt`, ... | Numbered local source records, including files scanned by a search that returned no match |
| `report_diagnostics.json` | Derived field, location, and evidence checks, plus the recovered report candidate and repair log; the original report is preserved |
| `adjudication.json` | Main-session decisions, initially pending; not an automatic approval |
| `engine-session/` | Official session header/event log in the local no-hash backend; used for new-format resume |

Local source capture is broader than model delivery: a search can retain a scanned file locally while returning no text from it to the model. Indexed source counts in `coverage` do not prove that the model saw or reviewed those files. A diagnostic `matched` result only checks the recorded text, not whether that text was delivered or the bug is real.

Check `workflow_status`, `parse_ok`, `completion_warning`, findings, missing context, and diagnostics. Report syntax recovery uses [json-repair](https://github.com/mangiucugna/json_repair) locally without another model request. The original response, recovered candidate, and repair log remain separate; a syntactically valid recovery does not establish completeness or correctness. The runtime retains report selection, ambiguity, possible-loss checks, and main-session adjudication. A location match does not prove a bug; a missing or ambiguous match does not silently delete the finding. A returned report, an empty findings list, or a finished request does not automatically mean the code passed review. Retain failures and partial reports for the main session to assess; not every malformed response can be recovered.

After checking a finding against the source and test evidence, record a decision locally without another model call:

```text
python scripts/report_diagnostics.py --review-dir "/path/to/review-output" --finding finding-0001 --verdict confirmed --reason "Verified the triggering input and the affected call path"
```

Use `confirmed`, `dismissed`, or `needs_check` as the verdict. Use the finding IDs from that report, not invented IDs. Decisions and their history are separate from the original model report and are not sent back as input to the independent review.

For several findings, place a JSON array of decision objects (`id`, `verdict`, `reason`, and optional `evidence`) in `adjudication-input.json` inside the generated review output directory, then record them in one call:

```text
python scripts/report_diagnostics.py --review-dir "/path/to/review-output" --decisions-file "/path/to/review-output/adjudication-input.json"
```

Keep decision input files out of the reviewed source tree so they do not become independent-review material. If a project location is unavoidable, explicitly exclude it from review collection.

`--note "..."` can record an overall verification note, including when the report has no findings. A recorded decision or note is not an automatic merge approval.

Inspect local call summaries without calling a model:

```text
python scripts/review.py --config config.json --history --outcome success --slowest --limit 10
```

### Recovery and resuming an unfinished review

The official Harness retry service handles eligible capacity/transport failures and cancellable backoff, with the review policy admitting new attempts within the configured recovery window. Once reasoning or text has arrived, a deadline or interrupted stream can produce a checkpoint and continue the review. Authentication, parameter, policy, local I/O errors and operator cancellation do not trigger automatic recovery. The model is retained throughout.

To continue an unfinished recorded review in a new directory:

```text
python scripts/review.py --config config.json --resume "/path/to/unfinished-review" --output-dir "/path/to/new-review"
```

Resume keeps the recorded model settings, endpoint, and captured file versions. New-format records restore the official session; older records import saved review progress and sources without replaying old API/tool calls. Newly explored files use the current selected repository view and are marked separately. Add `--dry-run` to prepare restoration material without a model call; `checkpoint_pending` means live continuation has not started.

Every actual attempt retains its own directory and ledger entry. Failed-request time and retry waits are separated from successful-request time; the original run and its usage are retained without being charged again as new calls. Recovery can consume additional service quota and does not guarantee a completed report. See [recovery details](docs/architecture.md#failure-recovery-and-resume) for the classification and recording boundaries.

### Packet mode: compatibility and replay

For an explicitly selected material set or offline replay, prepare a private `packet.json` with the following shape:

```json
{
  "task": "Review an integer addition function",
  "requirements": ["Check the stated function contract; report only evidence-backed issues"],
  "diff": "",
  "files": [{"path": "example.py", "content": "def add(a: int, b: int) -> int:\n    return a + b\n"}],
  "test_results": "No function tests were run; this example only checks offline request construction"
}
```

```text
python scripts/review.py --config config.json --packet packet.json --output-dir outputs/packet-dry-run-001 --dry-run
```

Packet mode uses only the supplied materials, with bounded snapshot read/search tools when needed. It does not discover additional repository files. It still requires a new explicit `--output-dir`; use a different directory on each run. Removing `--dry-run` makes a real API request and requires credentials. Repository mode is the normal entry point; omitting both `--repo` and `--packet` explores the current Git working directory.

## Logs, updates, and development

Real calls retain collected materials, explored source versions, requests, response streams, available reasoning text, diagnostics, decisions, and results locally. These can contain source code or private paths. The known current API key is redacted where possible; this is not a general secret scanner and does not make logs safe to publish. Git ignores local configuration, common output directories, packets, logs, the ledger, and tokenizer JSON. Keep all review output directories, including source captures and adjudication records, private even if you choose a custom location. Do not force-add or share them.

To update a cloned installation, run `git pull --ff-only` in its directory, then refresh the dedicated runtime using `python scripts/skill_runtime.py setup --download-official`. **Do not overwrite your personal `config.json` by copying the example again.** Compare new configuration fields and merge them manually if needed.

After preparing the runtime above, run both offline suites; neither needs an API key, the Bailian CLI, or a live model:

```text
node --test runtime/test_*.mjs
python scripts/skill_runtime.py test
```

Setup may download missing packages and tokenizer assets. Tests use local fixtures; platform-specific cases may be skipped. CI prepares Node 24, the isolated Python host and official assets, then exercises both the active Node runtime and Python host without model calls. See [tokenizer installation (Chinese)](assets/deepseek_v4_tokenizer/README.md). Tokenizer JSON is not redistributed because its redistribution terms have not been established.

Project-authored code is licensed under [Apache-2.0](LICENSE). Official Harness dependencies and the two extracted converters retain their upstream licenses, including their MIT notices; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). This is a directly cloneable Skill source repository, not a published plugin-store package.

## Supplemental files, execution facts and progress

For a temporary script excluded by Git or stored under `artifacts/`, supply the selected file while keeping repository exploration enabled:

```text
python scripts/review.py --repo "/path/to/project" --include-file "artifacts/deploy.py" --execution-context "Preparation runs on Windows; SCP transfers the file before Linux deployment; external acceptance runs separately."
```

Repeat either option as needed. Supplemental paths must identify files inside the primary repository, not whole directories. They are recorded current-disk snapshots under the `supplement` root, independent of `staged` or `range` Git views. Only those files are exposed through that root; normal credential and output-path exclusions still apply. Execution context is supplied background to verify, not a host review verdict. No packet builder is required.

Direct and orchestrated reviews use the same short review contract. Diagnostics preserve each original finding and add `normalized_priority` for known `high/medium/low` or Chinese aliases; uncertain or invalid findings remain available for host adjudication.

Live CLI calls print a brief status to **stderr** about once per minute, with the current call, phase and character counts, then a final completion line. **stdout remains one JSON result.** The summary contains no source or reasoning text. Use `--quiet-progress` to suppress it; local progress and full logs are retained. Ledger titles fall back to the packet task or repository title when `--task` is omitted.
