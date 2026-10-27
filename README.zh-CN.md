# Codex Third-party Code Review

[English](README.md) | 简体中文

A Codex Skill for supplementary, repository-aware code review through an external model API.

主 GPT 只需提供项目、审阅目标、需求和已有测试证据。运行器自动收集初始改动，外部模型通过只读工具探索关联文件并提出发现，再由主会话核实、决定修改并验收。适合代码、测试、依赖、迁移和影响运行行为的配置变动；纯文档、规则说明和只读查询默认跳过。它不修改 Codex 主模型配置，报告也不等同于完整覆盖或审阅通过。

## 1. 安装 Skill

需要 **Git、Python 3.10+、可运行本地脚本的 Codex，以及外部模型服务的 API key**。ChatGPT/Codex 的订阅登录不会替代这个外部 API key。无需先安装百炼 CLI；tokenizer 由环境准备命令下载，也可使用已有本地资产。

以下按首次安装编写，Windows 与 macOS/Linux 选一组。目标目录应尚不存在；已有安装请看文末更新说明，执行某一步报错后先处理错误。

**Windows PowerShell：**

```powershell
$skillDir = Join-Path $HOME ".agents/skills/third-party-code-review"
New-Item -ItemType Directory -Force (Split-Path $skillDir) | Out-Null
git clone https://github.com/L1pp/codex-third-party-review.git $skillDir
```

克隆成功后继续：

```powershell
Set-Location $skillDir
python --version
python scripts/skill_runtime.py setup --download-official
Copy-Item config.example.json config.json
```

**macOS / Linux（Bash）：**

```bash
mkdir -p "$HOME/.agents/skills"
git clone https://github.com/L1pp/codex-third-party-review.git "$HOME/.agents/skills/third-party-code-review"
```

克隆成功后继续：

```bash
cd "$HOME/.agents/skills/third-party-code-review"
python3 --version
python3 scripts/skill_runtime.py setup --download-official
cp config.example.json config.json
```

安装会在 Skill 自己的 `.venv` 中准备 `json-repair`、原生 `tokenizers`、Jinja2 等依赖，并检查本地官方 tokenizer；不会调用模型或读取密钥。入口会自动切换到这套独立环境，保留项目工作目录，项目虚拟环境无需另装依赖。已有配置、审阅日志和完整 tokenizer 资产保留。

已下载官方 tokenizer 时可以使用本地目录：

```text
python scripts/skill_runtime.py setup --source-dir "<本地 tokenizer 目录>"
```

不加载凭据、不调用模型即可查看实际解释器和计数能力：

```text
python scripts/review.py --runtime-info
```

隔离解释器会忽略继承的 Python 搜索路径和用户级包；这不是操作系统沙箱。

用户级目录 `~/.agents/skills/third-party-code-review/` 可供多个项目使用。只想在一个项目使用，也可把完整仓库放到该项目的 `.agents/skills/third-party-code-review/`。必须保留 `SKILL.md`、`scripts/`、配置示例及支持文件的相对位置，不能只复制 `SKILL.md`。具体发现方式见 [OpenAI 官方 Skill 文档](https://learn.chatgpt.com/docs/build-skills)。未出现在 Skill 列表时，重新打开 Codex。

## 2. 配置 API key

`config.json` 是本地配置，已被 Git 忽略。默认服务是阿里云百炼 **Token Plan**：

```text
https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
```

请使用与该服务地址及账户对应的 API key。默认先读环境变量 `BAILIAN_TOKEN_PLAN_API_KEY`，为空时才读配置指定的本地 JSON profile。不会自动读取其他名字（例如 `DASHSCOPE_API_KEY`）；如需更名，修改 `config.json` 的 `api_key_env`。以下方式选一种即可。

### 方式 A：环境变量

PowerShell 中隐藏输入，仅给当前终端及其后启动的子进程使用：

```powershell
$reviewKey = Read-Host "Token Plan API key" -AsSecureString
$env:BAILIAN_TOKEN_PLAN_API_KEY = [System.Net.NetworkCredential]::new("", $reviewKey).Password
Remove-Variable reviewKey
```

Bash 中隐藏输入：

```bash
read -r -s -p "Token Plan API key: " BAILIAN_TOKEN_PLAN_API_KEY
printf '\n'
export BAILIAN_TOKEN_PLAN_API_KEY
```

**这不会更新已经运行的桌面 Codex 的环境。** 在同一终端运行手动 CLI，或从这个终端启动 Codex CLI，才能继承该变量。图形界面启动的桌面 Codex 推荐使用下面的本地凭据文件方式。不要把真实 key 写进命令示例、提示词、审阅包或 Git 文件。

### 方式 B：本地凭据文件（适合桌面 Codex，无需百炼 CLI）

先创建本地凭据目录，Windows 与 macOS/Linux 选一条：

```powershell
New-Item -ItemType Directory -Force (Join-Path $HOME ".config/third-party-code-review") | Out-Null
```

```bash
mkdir -p "$HOME/.config/third-party-code-review"
```

再在本机编辑器中创建 `~/.config/third-party-code-review/credentials.json`，内容如下，并将占位文字替换为自己的 key：

```json
{
  "review": {
    "api_key": "替换为自己的 API key，仅保留在本机"
  }
}
```

在已安装 Skill 的 `config.json` 中修改下面两个字段，其余配置保留：

```json
{
  "credential_file": "~/.config/third-party-code-review/credentials.json",
  "credential_profile": "review"
}
```

上面仅展示要改的字段，**不要用这个片段覆盖整个配置**。此凭据文件应只供本机当前用户读取，不要提交到项目。若环境变量已存在，它仍优先于文件。

已有百炼 CLI 的 `~/.bailian/config.json`、且其中配置了 `token-plan.api_key` 时，可以直接保留默认的 `credential_file` / `credential_profile`；本项目不会替你创建或登录该 profile。CLI 及公共模型目录均是可选能力，见 [复用说明](docs/reuse.md)。

## 3. 确认模型路由

复制的默认配置按 **Asia/Shanghai（北京时间）** 路由，不受电脑显示时区影响：

| 工作流开始时的北京时间 | 请求模型 | 审阅阶段思考与输出配置 |
| --- | --- | --- |
| 08:00（含）至 22:00（不含） | `auto` | 正文 131,072、思考 262,144 的本地假设，合并发送 `max_completion_tokens=393216` |
| 22:00（含）至次日 08:00（不含） | `deepseek-v4.1-flash-1017` | `reasoning_effort=max`，`max_tokens=393216` |

一次工作流开始时选择路由；同一轮取材或压缩续审跨过边界也不会中途换模型。checkpoint 压缩使用独立的较小输出预算，但不切换已选模型。`auto` 由服务端决定实际路由，本项目不能据此确认背后的模型。表中上限是配置与已知服务适配假设，不是任意账号、任意模型的实时能力保证。

默认上下文为 1,000,000 token，优先内联约 100,000 个本地估算 token 的材料，在预计输入、完整输出预留和安全余量达到 90% 阈值前压缩续审。即使初始材料全部内联，仓库探索工具仍可使用，审阅者可以自行查找调用方、契约、配置和相关测试，无需主会话逐文件复制。本地估算不等于服务端 `usage`。DeepSeek 原生计数依赖由独立运行环境准备，auto 仍明确使用保守估算，详见 [架构与边界](docs/architecture.md)。

**如果不想按时间切换：**修改 `config.json` 中的完整 `day` / `night` 路由，不要只改模型名、却留下另一个模型的思考和输出参数。例如全天使用 DeepSeek 时，用下面的两段替换原有 `day` / `night`，保留其他顶层字段：

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

全天使用 auto 时，把默认 `day` 中除 `start` / `end` 外的整组模型参数复制到 `night`，并保留 `day.start/end`。换其他服务商时，还要配套修改 `base_url`、凭据和两组完整路由；只改 URL 不能保证推理字段、工具调用或输出参数兼容。

常用其他字段：

| 字段 | 说明 |
| --- | --- |
| `base_url` | HTTPS 基础地址，脚本追加 `/chat/completions`，不要重复填写该后缀 |
| `model_catalog_file` | 可选公共目录缓存；相对路径以配置文件目录为基准；缺少时仍可运行，不会隐式联网刷新 |
| `ledger_file` | 可选 SQLite 台账路径；相对路径以命令当前目录为基准，建议绝对路径；默认在 Skill 的 `logs/` 下 |
| `automatic_retries` | 保持 `0`；供应商失败不自动重试或切换模型 |
| `context_management.max_model_calls` / `max_compactions` | 默认 `null`，没有固定次数上限；正整数可限制。`0` 是兼容的无限制别名，不能用于禁止调用 |

一次审阅工作流可能包含多次材料读取与压缩请求，真实调用会消耗外部服务额度。

## 4. 首先验证，不调用模型

以下命令在已安装的 Skill 根目录执行，无需 key。检查当前路由，再模拟两个时段：

```text
python scripts/review.py --config config.json --route-only
python scripts/review.py --config config.json --route-only --at 2026-10-27T08:00:00+08:00
python scripts/review.py --config config.json --route-only --at 2026-10-27T22:00:00+08:00
```

默认应分别看到 `auto` 与 `deepseek-v4.1-flash-1017`。`--at` 只用于离线路由或 dry-run，不能用于强制切换真实调用。

把项目路径换成自己的本地 Git 项目，预览自动取材和初始请求：

```text
python scripts/review.py --config config.json --repo "/path/to/project" --target worktree --task "审阅当前改动" --dry-run
```

不需要准备 `packet.json`。成功时终端 JSON 的 `status` 为 `dry_run`，实际模型调用数为 0，并返回绝对 `output_dir`。仓库模式省略输出目录时，会自动在已安装 Skill 的 `logs/reviews/` 下创建带 UTC 时间戳的新目录；也可显式指定新的 `--output-dir`。

`--route-only` 与 `--dry-run` 均不读取凭据。dry-run 会读取项目材料并写本地快照和计划请求，不能证明 API key 有效或模型服务可用，也不能清理源码中误放的任意秘密。输出应本地保留。

## 5. 在 Codex 中调用

安装、配置 key 后，在开发项目的 Codex 会话输入：

```text
使用 $third-party-code-review 审阅本项目当前改动。自动收集差异和源码上下文，让审阅者只读探索关联文件，最后由主会话核实并裁决发现。
```

主会话给运行器项目目录、审阅目标、简短需求和已有测试证据文件，不再手工复制源码到审阅包。入口自动进入 Skill 自己的独立虚拟环境，不需要调用方指定项目 Python 路径。

审阅者可在指定仓库和额外工作区根内列文件、搜索代码、分页读取文本。即使小改动的材料全部内联，这些工具也保留。运行器把实际读到的内容保存为顺序版本，后续读取复用该版本。审阅工具不执行命令、不修改代码、不运行测试；主会话继续按宿主项目与平台权限规则负责修改和验收。

`allow_implicit_invocation=true` 允许 Codex 按描述自动匹配，但**安装不保证每次开发必定调用**。如需在某个项目默认补审，可自行把下面的约定加入该项目 `AGENTS.md`；安装过程不会修改全局规则：

```text
对代码、测试、依赖、迁移及影响运行行为的配置变动，在正常 GPT 审阅之外由主会话额外运行一次 $third-party-code-review；纯文档和只读查询默认跳过。主会话给出仓库、审阅目标、需求和已有测试证据，由运行器取材、审阅者只读探索；主会话核实裁决。失败或不完整报告不计作通过。
```

## 6. 不通过 Codex，手动调用

仍在 Skill 根目录、已配置好 key 时，以下命令会真正发送项目材料到外部模型并消耗额度：

```text
python scripts/review.py --config config.json --repo "/path/to/project" --target worktree --task "审阅当前改动" --requirement "保持已公开的行为契约"
```

仓库审阅目标：

| 目标 | 初始差异与主仓库内容读取方式 |
| --- | --- |
| `worktree`（默认） | 相对 HEAD 的差异，含暂存和未暂存改动；另外收集未忽略的未跟踪文本文件。读取当前工作区内容。 |
| `staged` | 暂存差异，从 Git index 读取内容，不拿未暂存内容冒充暂存版本。 |
| `range --base <ref>` | 比较指定 ref 与 HEAD 的 merge base 到 HEAD 的差异，从 HEAD 读取内容。基准 ref 须在本地存在。 |

已知删除路径会在 `list_files` 的 entries 中标为 `deleted/base`；即使初始材料预算省略了它，`read_file` 仍返回所选 baseline。额外工作区根继续读取磁盘视图。

示例：

```text
python scripts/review.py --config config.json --repo "/path/to/project" --target staged --task "审阅暂存改动"
python scripts/review.py --config config.json --repo "/path/to/project" --target range --base origin/main --task "审阅本分支"
python scripts/review.py --config config.json --repo "/path/to/project" --workspace-root "/path/to/shared-library" --test-evidence "/path/to/test-results.txt" --task "审阅集成改动"
```

多个额外工作区可重复提供 `--workspace-root`，多条简短要求可重复提供 `--requirement`。允许根目录重叠，不因重叠拒绝运行。`repo` 别名遵守所选 Git 视图，`workspace-*` 别名始终读磁盘；同一文件可能因此有多个版本，应结合根别名路径和 `source_id` 裁决，不能把额外工作区读到的内容当成主仓库的同一 Git 版本。`--test-evidence` 仅读已有文件，不执行其中描述的测试。已知凭据位置和运行器自身输出会排除，初始取材跳过项带原因；工具使用根目录内的相对路径，不授予任意文件系统或 shell 访问权限。

文件在首次读取时记录，不同文件可能来自不同读取时刻。这是实际看到材料的记录，**不是全仓库原子一致快照**。代码后续又修改时，不能未经复核便声称发现适用于新版本。

### 检查报告并记录主会话裁决

终端返回绝对输出目录，其中包括：

| 文件 | 用途 |
| --- | --- |
| `report.md`、`result.json`、`workflow.json` | 原始可见报告、解析结果、完成状态与上下文记录 |
| `calls/` | 每次请求、响应流、可用推理文本和耗时记录 |
| `sources/index.json`、`sources/source-0001.txt` 等 | 本地源码顺序记录，包括搜索扫描过但未命中的文件 |
| `report_diagnostics.json` | 派生字段、定位、证据诊断，以及恢复报告候选和修复日志；原始报告单独保留 |
| `adjudication.json` | 主会话裁决，初始为 pending，不自动代表通过 |

本地来源记录与模型收到的内容不同：搜索可能在本地保留扫描文件全文，却不向模型返回该文件的任何文本。`coverage` 中的索引数量不证明模型已看到或审阅这些文件；诊断 `matched` 只核验实际记录文本，不证明文本已投递或 Bug 成立。

检查 `workflow_status`、`parse_ok`、`completion_warning`、发现、缺失上下文和诊断。通用 JSON 语法恢复交给 [json-repair](https://github.com/mangiucugna/json_repair)，在本地执行，不为格式修复再次请求模型。原始响应、恢复候选和修复日志分开保留；恢复为合法 JSON 不证明报告完整或发现正确。运行器仍负责报告选择、歧义、可能丢失内容的判断和主会话裁决。定位匹配不证明 Bug 成立，定位缺失或歧义也不会静默删除发现。有报告、无发现或请求结束，都不能自动等同于审阅通过；失败与部分报告保留给主会话判断，也不保证所有格式损坏都能恢复。

结合源码和测试证据核实后，可在本地记录裁决，无需另一次模型调用：

```text
python scripts/report_diagnostics.py --review-dir "/path/to/review-output" --finding finding-0001 --verdict confirmed --reason "已核对触发输入和受影响调用链"
```

裁决值可用 `confirmed`、`dismissed`、`needs_check`，finding ID 取自对应报告，不要自行编造。裁决及其更新历史独立于原始模型报告，不提前回灌给独立审阅输入。

多项发现可把裁决对象（`id`、`verdict`、`reason`，可选 `evidence`）的 JSON 数组放在已生成审阅输出目录的 `adjudication-input.json` 中，一次记录：

```text
python scripts/report_diagnostics.py --review-dir "/path/to/review-output" --decisions-file "/path/to/review-output/adjudication-input.json"
```

裁决输入不要放在被审源码目录，避免成为独立审阅材料；若确需放在项目内，必须明确排除取材。

`--note "..."` 可记录整轮核查说明，包括报告无发现的情况；记录了裁决或说明不自动代表允许合并。

查询本地调用摘要，不调用模型：

```text
python scripts/review.py --config config.json --history --outcome success --slowest --limit 10
```

### Packet 模式：兼容与回放

需要明确限定材料集合或离线回放时，可准备私有 `packet.json`：

```json
{
  "task": "复核一个整数加法函数",
  "requirements": ["确认行为符合给定函数契约；只报告有证据的问题"],
  "diff": "",
  "files": [{"path": "example.py", "content": "def add(a: int, b: int) -> int:\n    return a + b\n"}],
  "test_results": "此示例未运行函数测试；仅用于验证离线请求构造"
}
```

```text
python scripts/review.py --config config.json --packet packet.json --output-dir outputs/packet-dry-run-001 --dry-run
```

Packet 模式仅使用已提供的材料，需要时通过受限快照工具读/搜，不发现额外仓库文件。它仍要求新的显式 `--output-dir`，重复运行请更换目录。去掉 `--dry-run` 即为真实 API 调用，需要凭据；日常使用仓库模式；省略 `--repo` 和 `--packet` 时自动探索当前工作目录的 Git 仓库。

## 日志、更新与开发

真实调用默认本地保存初始材料、探索读到的源码版本、请求、响应流、可用推理文本、诊断、裁决和结果，可能含源码或私有路径。已知当前 API key 会尽力脱敏，但这不是任意秘密检测；日志不能因此视为可公开。`config.json`、常见输出、packet、日志、台账和 tokenizer JSON 已被 Git 忽略；自定义输出目录中的源码记录和裁决也应保持私有，不要强制添加或分享。

更新已克隆的 Skill 时，在该目录执行 `git pull --ff-only`，随后用同一 Python 再执行 `python scripts/skill_runtime.py setup --download-official`。**不要重新复制示例覆盖个人 `config.json`。** 新版本如增加配置字段，请对照示例手动合并。

离线测试无需 API key、百炼 CLI 或 tokenizer 下载：

```text
python -m unittest discover -s scripts -p "test_*.py"
```

标准安装会执行原生 tokenizer 检查；仅平台专属测试按环境跳过。CI 在独立环境中准备依赖和官方资产，然后只运行离线测试，不调用模型。tokenizer 的获取与依赖见 [安装说明](assets/deepseek_v4_tokenizer/README.md)，本仓库不再分发其 JSON，尚未核明再分发条件。

本项目按 [Apache-2.0](LICENSE) 发布；外部依赖见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。这是可直接克隆的 Skill 源码，并非插件商店上架包。
