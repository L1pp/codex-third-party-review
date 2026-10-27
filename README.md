# Codex Third-party Code Review

A Codex Skill for supplementary, snapshot-based code review through an external model API.

这是一个可独立安装的 Codex Skill：主会话把相关代码与测试证据整理成快照，由外部模型提出补充发现，再由主会话逐项核实、决定是否修改并完成验收。它不替换 Codex 的主模型、开发子 Agent 或正常审阅流程，也不修改全局 GPT 配置。

适合代码、测试、依赖、迁移和影响运行行为的配置变动；纯文档、规则说明和只读查询默认跳过。也可以明确指定一组代码进行审阅。报告提供待核实的发现，不能证明完整覆盖或代码安全。

## 准备环境

需要 Python 3.10+。在本仓库根目录安装运行依赖：

```text
python -m pip install -r requirements.txt
```

核心依赖是 `requests`，`tzdata` 为 Windows 等环境提供可配置时区数据。tokenizer 为可选功能，默认无需安装；缺少依赖或资产时运行时使用带方法标记的 UTF-8 估算。

将本仓库作为完整 Skill 文件夹放到当前 Codex 支持的本地发现目录，文件夹名建议为 `third-party-code-review`，例如用户目录 `~/.agents/skills/third-party-code-review/` 或项目 `.agents/skills/third-party-code-review/`。保留 `SKILL.md`、`scripts/`、配置示例及支持文档之间的相对位置。支持符号链接的环境也可以链接整个文件夹。具体目录以 [OpenAI 官方 Skill 文档](https://learn.chatgpt.com/docs/build-skills) 和所用 Codex 版本为准。

不要把已有个人安装目录里的 `config.json`、`logs/` 或审阅输出整体复制到公共仓库。此仓库是 Skill 源码，未包含插件目录上架或远程安装发布配置。

## 配置模型与凭据

将 `config.example.json` 复制为同目录 `config.json`，后者已被 Git 忽略。按自己的账户与服务调整：

| 字段 | 用途 |
| --- | --- |
| `base_url` | HTTPS OpenAI 兼容服务的基础 URL，脚本追加 `/chat/completions` |
| `api_key_env` | 从该环境变量读取 API key；优先于本地 profile |
| `credential_file` / `credential_profile` | 环境变量为空时读取本机 JSON 中 `<profile>.api_key` |
| `timezone`、`day.start/end` | 按配置时区划分 `day` / `night` 路由 |
| `day` / `night` | 各路由的模型、输出参数和预算 |
| `context_management` | 材料内联、上下文预算、压缩和调用次数限制 |
| `model_catalog_file`（可选） | 官方 CLI 元数据的本地缓存；相对路径以配置文件目录为基准 |
| `ledger_file`（可选） | 本地 SQLite 台账路径；相对路径以命令当前目录为基准，建议绝对路径；默认在 Skill 的 `logs/` 下 |

示例配置使用阿里云百炼 Token Plan、本机 `~/.bailian/config.json` 中的 `token-plan` profile，以及 `Asia/Shanghai` 的日夜时段。这是一个可修改的配置样例，不要求其他用户采用该服务、时区、模型或个人习惯。真实模型名称、额度、参数和可用性需要结合自己的服务确认；示例中的上下文/输出上限不是实时服务承诺。服务兼容性不限于基础 URL，推理字段、SSE、工具调用和输出限制也须匹配。

凭据通过配置指定的环境变量或现有本机 profile 提供，无需写进审阅包。若使用百炼，登录和凭据配置交给官方 `bl` CLI 管理；本项目不会执行登录、复制凭据或把 CLI 当作审阅模型。官方目录复用边界见 [docs/reuse.md](docs/reuse.md)。

`automatic_retries` 必须保持 `0`。它禁止 HTTP 自动重试；一次审阅工作流仍可为了读取材料或压缩上下文发出多次请求。示例里的 `max_model_calls` 和 `max_compactions` 为 `null`，表示未设置次数上限；希望控制消耗时应设置正整数，并保留处理不完整结果的流程。`0` 兼容旧配置，也表示不限制，不能用来禁止调用；推荐用 `null` 明示无限制。

## 先做离线检查

以下命令以仓库或已安装的 Skill 根目录为当前目录。已安装后也可使用脚本与配置的实际绝对路径。

```text
python scripts/review.py --config config.json --route-only
python -m unittest discover -s scripts -p "test_*.py"
```

`--route-only` 仅读取本地配置并显示当前路由，不读取凭据或发起模型调用。需要复核时段时可在离线命令中添加 `--at 2026-10-26T12:00:00+08:00`。

将下面的虚构材料保存到私有任务目录的 `packet.json`：

```json
{
  "task": "复核一个整数加法函数",
  "requirements": ["确认行为符合给定函数契约；只报告有证据的问题"],
  "diff": "",
  "files": [{"path": "example.py", "content": "def add(a: int, b: int) -> int:\n    return a + b\n"}],
  "test_results": "示例字段：请替换为实际测试命令和结果"
}
```

使用新的输出目录预览请求与预算，不调用模型：

```text
python scripts/review.py --config config.json --packet "<私有任务目录>/packet.json" --output-dir outputs/dry-run-001 --dry-run
```

`--dry-run` 会写入本地材料快照、计划请求和工作流记录，并非只在终端显示。输出目录必须尚不存在。它不加载凭据，因此也不会替你清理材料中误放的秘密。

## 在 Codex 中使用

安装与配置后，向 Codex 明确指定：

```text
使用 $third-party-code-review 对本次代码改动做额外独立审阅，由主会话核实并裁决发现。
```

主会话按 [SKILL.md](SKILL.md) 准备材料，再通过已加载 Skill 的实际目录执行 `scripts/review.py`。命令权限遵守宿主项目和平台规则；公共 Skill 不改变这些权限。

手工发起真实调用时，在同一命令中去掉 `--dry-run`，保留新的 `--output-dir`。这会把审阅材料发送到配置的外部服务，并可能消耗其额度。任务授权必须覆盖所发送材料。不要在 `--task` 短标题中放源码、密钥或私有内容。

结果在输出目录的 `report.md`、`result.json` 和 `workflow.json`。主会话需要确认结果状态、未完成原因、发现的来源位置与实际行为；材料已投递或被工具读取不证明模型完整审阅，也不证明报告正确。

本地调用摘要可通过以下命令查询，不调用模型：

```text
python scripts/review.py --config config.json --history --outcome success --slowest --limit 10
```

## 日志、数据与发布边界

真实调用默认在本地保留材料、请求、原始响应流、可用的推理文本、解析结果和工作流记录。这些文件可能包含源码、差异、测试信息和私有路径。已知当前 API key 会尽力脱敏；这不是对任意秘密的通用检测，不能把“脱敏”理解成日志可公开。

`config.json`、常见输出目录、材料文件和本地 tokenizer 资产已被 `.gitignore` 排除。Git 忽略规则不能阻止强制添加，也不会删除已跟踪文件；发布者仍应检查实际将发布的文件。不要提交凭据、原始材料或调用日志。

本项目默认不再分发 DeepSeek tokenizer JSON；未核明其再分发条件。需要时按 [可选 tokenizer 安装说明](assets/deepseek_v4_tokenizer/README.md) 从官方来源自行获取，仅安装两个 JSON。运行时的本地估算始终不等于服务端 `usage`。

## 离线开发与 CI

```text
python -m unittest discover -s scripts -p "test_*.py"
```

测试用 mock 替代服务调用，不需要 API key、官方 `bl` 登录或模型额度。CI 安装 Python 依赖后执行同样的离线测试，不下载 tokenizer，不调用模型 API。默认缺少可选资产时，仅两个原生 tokenizer 专属测试被明确跳过，其余预算、fallback、材料工具、HTTP/SSE、工作流和台账测试仍执行。

源码流程、估算方式和限制见 [docs/architecture.md](docs/architecture.md)。本项目按 [Apache-2.0](LICENSE) 发布；外部依赖与未再分发资产见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
