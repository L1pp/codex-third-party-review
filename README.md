# Codex Third-party Code Review

English | [简体中文](README.zh-CN.md)

A Codex Skill for supplementary, snapshot-based code review through an external model API.

Your main GPT session prepares a code snapshot, an external model provides an independent read-only review, and the main session verifies the findings and decides what to change. Use it for code, tests, dependencies, migrations, and configuration that affects runtime behavior. Documentation-only changes and read-only queries are skipped by default.

This Skill does not change Codex's main model configuration. A completed report is not proof of full coverage or an approval to merge.

## 1. Install the Skill

You need **Git, Python 3.10+, Codex with permission to run local scripts, and an API key for the external model service**. Your ChatGPT/Codex subscription sign-in does not supply that external API key. The Bailian CLI and tokenizer assets are optional.

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
python -m pip install -r requirements.txt
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
python3 -m pip install -r requirements.txt
cp config.example.json config.json
```

The rest of this guide uses `python` for the same interpreter; substitute `python3` where appropriate. **Install dependencies into the interpreter that Codex will use to run the script.** Activating a virtual environment in another terminal does not change a running desktop Codex process.

If your system Python disallows pip installs, create a virtual environment and use its Python path for the remaining commands:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Tell Codex to execute this Skill with that environment's **absolute Python path**. You do not need to activate it if you use the executable directly.

The user-level directory `~/.agents/skills/third-party-code-review/` makes the Skill available across projects. For one project only, place the complete repository at that project's `.agents/skills/third-party-code-review/`. Keep the relative layout of `SKILL.md`, `scripts/`, configuration examples, and supporting files; copying only `SKILL.md` is insufficient. See the [official OpenAI Skill documentation](https://learn.chatgpt.com/docs/build-skills). If the Skill does not appear, reopen Codex.

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
| 22:00 inclusive to 08:00 exclusive | `deepseek-v4.1-flash-1017` | `reasoning_effort=max`, `max_tokens=393216` |

The route is selected once at the start of a workflow. Material reads or checkpoint continuations do not switch models if the time crosses a boundary. Checkpoint compaction uses a separate, smaller output budget without switching the selected model. The provider selects the underlying model for `auto`; this project cannot identify it from that name. These limits describe configuration and service-adapter assumptions, not a live capability guarantee for every account or model.

Defaults: a 1,000,000-token context window, approximately 100,000 locally estimated material tokens sent inline, overflow available through snapshot-only read tools, and checkpoint compaction at a 90% threshold. Local estimates are not the provider's `usage`. Missing optional tokenizer assets do not prevent operation. See [architecture and boundaries (Chinese)](docs/architecture.md).

**To use one model all day:** replace the complete `day` and `night` route objects, rather than changing only the model name and retaining another model's reasoning/output parameters. For example, replace both route objects with the following to use DeepSeek throughout the day; retain every other top-level configuration field:

```json
{
  "day": {
    "start": "08:00",
    "end": "22:00",
    "model": "deepseek-v4.1-flash-1017",
    "reasoning_effort": "max",
    "max_tokens": 393216
  },
  "night": {
    "model": "deepseek-v4.1-flash-1017",
    "reasoning_effort": "max",
    "max_tokens": 393216
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
| `automatic_retries` | Keep `0`; provider failures do not trigger automatic retries or model switching. |
| `context_management.max_model_calls` / `max_compactions` | `null` by default, with no fixed count limit. Positive integers limit counts. Legacy `0` also means unlimited, not disabled. |

One review workflow may make multiple material-reading or compaction requests. Real requests consume your external service's quota or credits.

## 4. Verify setup without calling a model

Run these commands from the installed Skill root. No key is required. Inspect the current route, then simulate the two time periods:

```text
python scripts/review.py --config config.json --route-only
python scripts/review.py --config config.json --route-only --at 2026-10-27T08:00:00+08:00
python scripts/review.py --config config.json --route-only --at 2026-10-27T22:00:00+08:00
```

With the defaults, the simulated routes should show `auto` and `deepseek-v4.1-flash-1017`, respectively. `--at` is only for offline routing or dry runs; it cannot force the route of a real request.

Create `packet.json` in the Skill root with this fictional example to check request construction. For real work, replace it with the relevant diff, necessary source snapshots, and actual test evidence:

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
python scripts/review.py --config config.json --packet packet.json --output-dir outputs/dry-run-001 --dry-run
```

On success, the terminal JSON has `status` set to `dry_run`, with zero actual model calls. Inspect the snapshot and planned request in the output directory. This does not validate the API key or service availability. Both `--route-only` and `--dry-run` avoid loading credentials. A dry run writes local files and does not remove secrets accidentally placed in the packet. The output directory must be new; use `dry-run-002`, for example, on the next run.

## 5. Invoke it in Codex

Once the Skill is installed and credentials are configured, enter this in your development project's Codex session:

```text
Use $third-party-code-review for an additional independent review of the current code changes. Have the main session verify and adjudicate the findings.
```

If you use a virtual environment, also tell Codex the absolute path of its Python interpreter. The main session follows [SKILL.md](SKILL.md) to prepare materials and run the script. The reviewer can read only this snapshot; it cannot execute commands, write files, or read arbitrary paths. Commands remain subject to your host project's and platform's permission rules.

`allow_implicit_invocation=true` permits Codex to match the Skill automatically; **installation does not guarantee a review after every development task**. To request a review by default in a project, add a rule such as the following to that project's `AGENTS.md`. Installation does not modify global instructions:

```text
For code, tests, dependencies, migrations, and configuration changes affecting runtime behavior, have the main session run $third-party-code-review once in addition to normal GPT review. Skip documentation-only changes and read-only queries by default. The third-party reviewer is read-only; the main session verifies and adjudicates findings. Failed or incomplete reports do not count as approval.
```

## 6. Run manually without Codex

From the Skill root, with `packet.json` and credentials configured, this command **sends the materials to the external service and consumes quota or credits**:

```text
python scripts/review.py --config config.json --packet packet.json --output-dir outputs/review-001 --task "Additional review of integer addition"
```

Use a new output directory, not the dry-run directory. Results are in `report.md`, `result.json`, and `workflow.json`; individual request records are under `calls/`. Check `workflow_status`, `parse_ok`, `completion_warning`, findings, and missing context. A returned report, an empty findings list, or a finished request does not automatically mean the code passed review. Retain provider failures for the main session to assess.

Inspect local call summaries without calling a model:

```text
python scripts/review.py --config config.json --history --outcome success --slowest --limit 10
```

## Logs, updates, and development

Real calls retain materials, requests, response streams, available reasoning text, and results locally. These can contain source code or private paths. The known current API key is redacted where possible; this is not a general secret scanner and does not make logs safe to publish. Git ignores local configuration, common output directories, packets, logs, the ledger, and tokenizer JSON. Do not force-add or share them.

To update a cloned installation, run `git pull --ff-only` in its directory, then install dependencies again with the same interpreter using `python -m pip install -r requirements.txt`. **Do not overwrite your personal `config.json` by copying the example again.** Compare new configuration fields and merge them manually if needed.

Offline tests need no API key, Bailian CLI, or tokenizer downloads:

```text
python -m unittest discover -s scripts -p "test_*.py"
```

Only two native-tokenizer tests are skipped when optional assets or dependencies are absent; budget, materials, SSE, workflow, and ledger tests still run. CI runs offline tests as well. See [optional tokenizer installation (Chinese)](assets/deepseek_v4_tokenizer/README.md). Tokenizer JSON is not redistributed because its redistribution terms have not been established.

Licensed under [Apache-2.0](LICENSE); external components are described in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). This is a directly cloneable Skill source repository, not a published plugin-store package.
