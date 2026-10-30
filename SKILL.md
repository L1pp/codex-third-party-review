---
name: third-party-code-review
description: 对代码、测试、依赖、迁移和影响运行行为的配置变动，或用户指定代码范围，自动取材并补充独立只读第三方审阅，由主会话核实裁决。纯文档、规则说明和只读查询默认不触发。
---

# 额外独立代码审阅

保留正常 GPT 开发、审阅和验收流程；同一主任务由主会话额外运行一次，开发子 Agent 不重复调用。第三方优先补充发现问题与有依据的疑点，主会话负责复核裁决。用户明确指定或禁止时遵从用户要求。

1. 以本 `SKILL.md` 实际所在目录定位 `scripts/review.py` 和本地 `config.json`；入口自动使用本 Skill 的独立 `.venv` 与 `runtime/` 中的官方 Harness 依赖，安装与配置见 [README.md](README.md)。不要猜测安装位置。
2. 给出任务仓库、审阅目标、简短需求和已有测试证据，无需逐文件复制源码或准备 packet。默认围绕变更及直接影响收敛；明确需要深入核查时加 `--review-profile deep`，两种模式均保留问题驱动的仓库探索。省略 `--repo` 时自动使用当前仓库，默认 `worktree`；仅暂存改动用 `staged`，分支范围用 `range --base <本地ref>`。涉及额外工作区时通过 `--workspace-root` 指定，允许根重叠；`repo` 按目标 Git 视图读取，工作区别名始终读磁盘，核查时区分路径和来源版本。
3. 遵守宿主项目和平台的命令权限执行入口，入口负责切换独立 Python：

```text
python "<Skill根目录>/scripts/review.py" --repo "<项目目录>" --target worktree --task "<任务短标题>" --requirement "<必要需求>"
```

可用 `--include-file "<主仓库内临时脚本>"` 补充选中的磁盘文件，保留仓库探索；跨平台/执行阶段事实可用 `--execution-context "<简短背景>"`，不放入主 GPT 审阅结论。可加 `--test-evidence "<已有测试结果文件>"`；脚本自动取材，模型可只读列目录、搜索和分页读取指定仓库/工作区。省略输出目录时自动保存到 Skill 的 `logs/reviews/` 并返回绝对路径；显式 `--output-dir` 必须是新目录。凭据、无关私有资料和主 GPT 结论不作为独立审阅材料，审阅工具不执行命令、修改代码或运行测试。

4. 读取输出目录的 `report.md`、`result.json`、`report_diagnostics.json`，核实报告中的发现与有依据的未决疑点后完成正常修复与验收。用 `scripts/report_diagnostics.py --review-dir "<输出目录>" --finding finding-0001 --verdict confirmed --reason "<依据>"` 记录裁决；值可用 `confirmed`、`dismissed`、`needs_check`，多项可用 `--decisions-file "<输出目录>/adjudication-input.json"` 批量记录，裁决输入不要放入被审源码目录，确需放入时必须排除取材；报告无发现时可用 `--note` 写核查说明。来源记录可能包含搜索未命中的本地文件，不等于模型已看过全文；定位诊断不替代投递事实或缺陷判断，保留疑点和原始发现。失败、不完整报告或无发现不自动代表通过；修复后不自动再开独立复审。

只有明确要求限定材料集合或回放时，才使用 `--packet "<packet.json>" --output-dir "<新目录>"`，此模式只访问已给材料。源码读取版本、请求、响应与可用推理日志本地保留，可能含源码，不随仓库发布。预算、路由、来源记录和裁决格式见 [架构与边界](docs/architecture.md)。
