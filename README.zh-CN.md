# Codex Third-party Code Review

[English](README.md) | 简体中文

A Codex Skill for supplementary, snapshot-based code review through an external model API.

主 GPT 会话整理代码快照，外部模型提供一次补充的独立只读审阅，再由主会话核实发现、决定修改并验收。适合代码、测试、依赖、迁移和影响运行行为的配置变动；纯文档、规则说明和只读查询默认跳过。它不修改 Codex 主模型配置，报告也不等同于完整覆盖或审阅通过。

## 1. 安装 Skill

需要 **Git、Python 3.10+、可运行本地脚本的 Codex，以及外部模型服务的 API key**。ChatGPT/Codex 的订阅登录不会替代这个外部 API key。本仓库无需先安装百炼 CLI，也无需下载 tokenizer。

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
python -m pip install -r requirements.txt
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
python3 -m pip install -r requirements.txt
cp config.example.json config.json
```

后文用 `python` 表示同一个 Python 解释器；macOS/Linux 如使用 `python3`，请相应替换。**安装依赖与 Codex 执行脚本必须使用同一解释器。** 如果使用虚拟环境，先在其中安装依赖，再让 Codex 用该环境的 Python 绝对路径执行此 Skill；不要假定在另一个终端激活环境就会影响桌面 Codex。系统 Python 不允许 pip 安装时，可先用下面的方式创建独立环境，再在后续命令中用其 Python 路径替代 `python`：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

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

默认上下文为 1,000,000 token，优先内联约 100,000 个本地估算 token 的材料，仅超出部分按需读取；在 90% 阈值压缩续审。本地估算不等于服务端 `usage`。缺少可选 tokenizer 时也可运行，详见 [架构与边界](docs/architecture.md)。

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

在 Skill 根目录新建 `packet.json`，用下面的虚构材料检查请求构造；真实任务应换成相关差异、必要源码和真实测试结果：

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
python scripts/review.py --config config.json --packet packet.json --output-dir outputs/dry-run-001 --dry-run
```

成功时终端包含 `status=dry_run`（JSON 字段），实际模型调用数为 0；输出目录内可查看材料快照与计划请求。这不代表 API key 有效或模型服务可用。`--route-only` 与 `--dry-run` 均不读取凭据；dry-run 会写本地文件，也不会清理材料里误放的秘密。输出目录必须是新的，重复运行请改成 `dry-run-002` 等名称。

## 5. 在 Codex 中调用

安装、配置 key 后，在开发项目的 Codex 会话输入：

```text
使用 $third-party-code-review 对本次代码改动做额外独立审阅，由主会话核实并裁决发现。
```

如使用独立虚拟环境，同时告知 Codex 用该环境的 Python 绝对路径运行 Skill。主会话会按 [SKILL.md](SKILL.md) 整理材料并调用脚本；审阅者只可读取这次快照，没有命令、写文件或任意路径读取权限。命令执行仍遵守宿主项目和平台权限规则。

`allow_implicit_invocation=true` 允许 Codex 按描述自动匹配，但**安装不保证每次开发必定调用**。如需在某个项目默认补审，可自行把下面的约定加入该项目 `AGENTS.md`；安装过程不会修改全局规则：

```text
对代码、测试、依赖、迁移及影响运行行为的配置变动，在正常 GPT 审阅之外由主会话额外运行一次 $third-party-code-review；纯文档和只读查询默认跳过。第三方只读，主会话核实裁决，失败或不完整报告不计作通过。
```

## 6. 不通过 Codex，手动调用

仍在 Skill 根目录、已准备 `packet.json` 且配置好 key 时，以下命令会真正发送材料到外部模型并消耗额度：

```text
python scripts/review.py --config config.json --packet packet.json --output-dir outputs/review-001 --task "整数加法函数补充审阅"
```

真实调用使用新的输出目录，不要复用 dry-run 目录。结果在 `report.md`、`result.json`、`workflow.json`，每次请求的完整记录在 `calls/` 子目录。检查 `workflow_status`、`parse_ok`、`completion_warning`、发现与缺失上下文；有报告、无发现或请求结束，都不能自动等同于审阅通过。供应商失败时保留记录，由主会话决定后续。

查询本地调用摘要，不调用模型：

```text
python scripts/review.py --config config.json --history --outcome success --slowest --limit 10
```

## 日志、更新与开发

真实调用默认本地保存材料、请求、响应流、可用推理文本和结果，可能含源码或私有路径。已知当前 API key 会尽力脱敏，但这不是任意秘密检测；日志不能因此视为可公开。`config.json`、常见输出、packet、日志、台账和 tokenizer JSON 已被 Git 忽略；不要强制添加或分享它们。

更新已克隆的 Skill 时，在该目录执行 `git pull --ff-only`，随后用同一 Python 再执行 `python -m pip install -r requirements.txt`。**不要重新复制示例覆盖个人 `config.json`。** 新版本如增加配置字段，请对照示例手动合并。

离线测试无需 API key、百炼 CLI 或 tokenizer 下载：

```text
python -m unittest discover -s scripts -p "test_*.py"
```

缺少可选资产时仅跳过两个原生 tokenizer 测试，其余预算、材料、SSE、工作流和台账测试仍运行。CI 同样只跑离线测试。可选 tokenizer 的获取与依赖见 [安装说明](assets/deepseek_v4_tokenizer/README.md)，本仓库不再分发其 JSON，尚未核明再分发条件。

本项目按 [Apache-2.0](LICENSE) 发布；外部依赖见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。这是可直接克隆的 Skill 源码，并非插件商店上架包。
