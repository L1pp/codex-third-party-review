# 架构与边界

## 一次审阅如何执行

```text
主 GPT 给出仓库、审阅目标、需求和已有测试证据
  -> review.py：CLI、配置、凭据、路由、HTTPS/SSE、结果解析
  -> repository_source.py：自动 Git 取材、根目录内只读探索、来源版本记录
  -> workflow.py：初始内联、工具循环、预算与压缩续审
      -> materials.py：已捕获材料的索引、分页和字面搜索
      -> context_budget.py：本地估算与输入/输出预算
      -> history_cleanup.py：旧读取结果清理、完整工具往返与近期历史保留
      -> atomic_io.py：本地原子元数据提交与 Windows 短时占用处理
  -> report_parsing.py：调用 json-repair 恢复语法，保留报告选择与完整性判断
  -> 原始报告 + report_diagnostics.json + pending adjudication.json
主 GPT 结合源码和测试核实、修复、验收并记录裁决
```

这是额外独立审阅，不改变主 GPT 的模型、正常 reviewer 或验收流程。主会话指定任务目标，不需要逐文件复制源码。外部模型的工具请求由本地运行器处理和回传，不需要主 GPT 中转每一次文件读取。

`bailian_catalog.py` 是可选元数据适配层。普通审阅只读取本地缓存，不启动 CLI 或联网刷新。匹配到具体模型时只能收紧上限；`auto` 沿用显式参数假设，不据目录猜测实际模型。

## 仓库模式与 packet 模式

仓库模式是日常入口：`--repo <项目目录>`。运行器先收集所选目标的差异和变更文本，再让审阅者按需探索关联调用、契约、配置与测试。

| 主仓库目标 | 差异和内容版本 |
| --- | --- |
| `worktree` | 相对 HEAD 的暂存与未暂存差异，加未忽略的未跟踪文本；源码读取工作区 |
| `staged` | 暂存差异；源码读取 Git index |
| `range --base <ref>` | 指定 ref 与 HEAD 的 merge base 到 HEAD 的差异；源码读取 HEAD |

`--workspace-root` 可重复指定额外的目录，根别名为 `workspace-1` 等，主仓库根为 `repo`。允许根目录重叠，不加拒绝门禁；`repo` 别名遵守所选 `worktree`、`staged` 或 `range` Git 视图，工作区别名始终从磁盘读取。重叠时同一文件可能形成多个来源版本，应按根别名路径和 `source_id` 核实，不能把工作区内容当成主仓库的同一 Git 版本。`--test-evidence` 仅读已有证据文件，不执行测试。改名、删除、不可读取或跳过的材料保留对应状态和原因。已知删除文件在 `list_files.entries` 标为 `deleted/base`，初始预算省略后仍可由 `read_file` 读取所选 baseline；工作区根仍读取磁盘。

兼容的 `--packet` 模式继续接收 `task`、`requirements`、`diff`、`files: [{path, content}]`、`test_results` 等字段，只能访问提供的材料。packet 中的路径是标识，不授予仓库访问权。该模式用于明确限定材料或离线回放，仍要求新的显式输出目录。

两种模式都不意味着已完整检查整个仓库，也不保证更好的审阅质量。

## 只读探索与来源记录

仓库模式开放 `list_files`、`read_file`、`search_code`；现有 `list_materials`、`read_material`、`search_material` 仍可读取捕获材料。所有读取都有返回字符、行数、结果数或分页限制。即使初始材料全都内联，仓库工具也保留，模型仍能发现此前未提供的文件。

路径在所选仓库/工作区根内解析。工具不提供任意 shell、写入或测试执行，仓库文字不能修改运行器的 endpoint、凭据或工具权限。已知凭据位置、运行器自身输出及不适合取材的文件按读取策略排除或记录跳过原因。宿主脚本仍需要权限来读取指定项目、调用外部 API 和写本地日志；这不是操作系统沙箱。

首次读取实际内容时，以 `source-0001` 等顺序 ID 保存到 `sources/`，并在 `sources/index.json` 记录根别名、相对路径、读取时间、内容位置和行数等信息；后续读取同一来源默认复用捕获版本，搜索片段也关联该版本。读取缓存用于追溯和保持该文件的上下文，不以预先选出的文件白名单限制探索。来源记录也包括搜索扫描过但未命中的文件；本地保存扫描全文不等于向模型投递了全文。

文件在不同时间首次读取，不是全仓库原子一致快照。工作区在审阅期间可能变化，主 GPT 验收时应核对报告对应版本；记录不宣称审阅了后来的代码。此流程不做 hash、指纹或冻结验证，不锁定仓库。

## 材料内联、预算与续审

默认优先直接内联约 100,000 个本地估算 token 的初始材料；超出部分可按需读取，仓库中的额外文件也可探索。差异、源文件、任务说明、材料索引、工具定义和工具结果均占上下文。代码没有要求主 GPT 在每次调用前手工制作完整 packet。

模型请求材料后继续同一审阅对话。每次请求前重新计算压力，按以下顺序处理：

1. 仅在达到阈值后，缩减近期保留范围之外的可重读旧工具结果。重复读取须同时匹配工具、根、视图、source ID、路径、行/字符范围和文本，不按路径直接合并。大于 8,192 字符的旧读取结果保留前 4,096 和后 1,024 字符，并显式说明省略区间及原读取参数。
2. 重新计数；已恢复空间就继续审阅，不调用摘要模型。初始材料本身过大时，减少内联部分，完整材料仍可通过读取工具获取。
3. 仍超预算才摘要旧进度，保留已发现问题、依据、旧 checkpoint、实际投递范围和未完成项；近期完整对话原样保留。近期目标为扣除完整输出预留后的窗口的 16%，DeepSeek 默认是 119,040 token。按完整 assistant/tool 往返切分，单个超大往返整体进入摘要，不拆开工具调用及返回。

摘要生成后若请求仍超预算且估算没有减少，保留进度并结束为未完成，避免无变化的重复压缩；这是按实际进展判断，没有新增次数硬上限。输出以 `length` 结束时仍通过 checkpoint 续审，保留部分正文和可用推理，不执行未完成的工具调用。运行器不取消正在生成的流以插入压缩请求。来源版本、旧请求和原文保留，checkpoint 不替代主会话核实。

路由在一轮工作流开始时确定，续审不因时段跨界切模型。压缩可以使用独立的较小输出预算；供应商与用户设定的适用上限仍保留。`automatic_retries=0` 表示底层单次调用不隐式重发请求；工作流按下述分类处理异常恢复，每个真实 attempt 都独立记录，不切换模型。`max_model_calls` / `max_compactions` 可设正整数，`null` 或兼容的 `0` 表示没有固定次数限制。无法完成时保存已有材料和部分报告，明确未完成原因。

## token 估算的含义

安装可选依赖与官方资产后，DeepSeek 路由可使用本地 tokenizer 和 chat template，并计入工具 JSON 与推理字段；仍是本地估算，不等于服务端封装或 `usage`。

其他路由或资产不可用时，整段请求预算使用带方法标记的 UTF-8 字节上界与结构开销。初始材料前缀选择单独估算：可用时借用本地 tokenizer，否则每 4 字节约 1 token。两者用途不同，不能据此识别 `auto` 的实际 tokenizer。保守预算可能较早压缩。

## 原始记录、派生诊断和裁决

输出根目录包括 `materials.json`、`workflow.json`、`result.json`、`report.md`，必要时有最新 `checkpoint.txt` 及逐版 `checkpoints/`；工具执行原文先写入 `tool_results.jsonl`，与后续请求视图清理分开；每次模型调用在独立 `calls/` 子目录保留请求、预算、响应、状态与可用推理日志。仓库模式另有 `sources/`。`response_log.py` 尽力保存收到的 SSE 行和推理文本，针对已知当前 API key 脱敏；这不是任意秘密扫描，记录仍应私有。

`report_parsing.py` 将通用 JSON 语法恢复委托给必装依赖 [json-repair](https://github.com/mangiucugna/json_repair)，不再维护一套引号、逗号或括号修复器。恢复在本地完成，不调用模型；原始响应不改写，恢复报告候选和库返回的修复日志作为派生记录保留。我们自己的代码继续负责顶层报告选择、多报告歧义、可能丢失内容的判断，以及发现和裁决的关联。修复库可能补全结构，因此合法 JSON 不等于完整报告，更不证明发现正确；无法确认的恢复仍保留不完整状态，不把空数组当作通过依据。

`report_diagnostics.py` 只依据已提供或实际捕获的源文本分析，不自行读取模型给出的任意路径。`report_diagnostics.json` 记录字段类型、路径/行号、明确引文的匹配、歧义，以及恢复候选和修复日志。诊断 `matched` 只说明已记录文本中的匹配，不证明该文本已投递给模型或 Bug 成立；定位缺失、类型问题或歧义不会静默删除发现。可恢复输出与可能丢项必须区分。

`adjudication.json` 初始为 pending。主 GPT 核实之后记录 `confirmed`、`dismissed`、`needs_check` 及理由，按顺序 finding ID 关联，并保留更新历史。裁决独立保存，不回灌成第三方审阅输入，也不自动作为合并或验收门禁。批量裁决输入放在已生成输出目录的 `adjudication-input.json`，不放被审源码目录；若确需放在项目内，必须排除取材。

`ledger.py` 的 SQLite 台账仍只保存允许的任务、模型、开始/结束、总耗时、首段正文时间、结果状态和输出目录路径，不存完整源码或响应。详细调查顺着目录查原始记录和派生诊断，不把这些运维日志加入审阅上下文。

`coverage.status` 保持 `not_verified_by_workflow`：索引数量说明本地收集范围，不代表已投递或已审阅；实际内联和工具返回的范围另行记录，也不证明模型理解、覆盖完整或找到了所有问题。

## 可移植性

资源按脚本或已加载 `SKILL.md` 的实际位置定位。仓库不依赖个人用户名、Windows 盘符或宿主专属 Skill 环境变量。路由由本地配置控制，Python 3.10+ 可运行；`tzdata` 为缺少系统 IANA 数据的环境提供时区。仓库探索与诊断继续使用 Python 运行时，不要求额外安装 Go 或 Node 编排框架。

## 独立运行环境与完整输出预留

`skill_runtime.py setup` 在 Skill 自己的 `.venv` 中安装依赖并检查本地官方 tokenizer，`review.py` 与裁决 CLI 从其他 Python 启动时自动以隔离模式重新进入它。保留调用者的仓库工作目录和凭据传递方式，忽略继承的 Python 搜索路径；不修改项目虚拟环境或模型配置。没有显式 `--repo`/`--packet` 时，审阅默认探索当前 Git 仓库。

DeepSeek 使用官方 tokenizer 分词，并以本地模板加工具和推理字段估算请求；估算仍不等于供应商的精确 usage。auto 保留明确标注的保守估算。规划器采用已发布 DeepSeek Harness 的完成额度预留与 headroom 公式。设窗口为 W、完整输出额度为 O、余量为 H、比例阈值为 R，则预计输入压力阈值为 `min(floor(W × R), W − O − H)`，供应商较小的 max-input 仍可收紧它。默认 H 为 65,536，已包含本地 2,048 safety margin，不重复扣减；R 为 0.8，均可配置。DeepSeek 的 W=1,000,000、O=256,000，所以压力阈值是 678,464；auto 继续采用正文加推理的 393,216 本地假设，在同样窗口下阈值为 541,248，仍是保守字节估算，不能与 DeepSeek 原生 token 数直接比较。阈值触发的是分层清理，并不必然触发一次模型摘要。不把审阅输出削到只剩几千 token。真正的 `length` 终止仍保留原有续审机制。未添加模型轮数或工具次数硬上限。

## 统一审阅契约与补充来源

`review_contract.py` 提供直接调用和工作流共用的审阅规则及任务标题选择。输出优先级在诊断中作已知别名映射，原 finding 不变。`--execution-context` 放入现有 scope 数据，不作为可信审阅结论。

显式 `--include-file` 使用 `supplement` 根提供主仓库内选定的当前磁盘文件，可补充被 Git 或输出目录规则排除的临时脚本，不改变主 repo 的 Git 读取视图，也不开放同目录其他文件。来源、首次读取快照、行号和 source ID 复用已有机制。

`review_progress.py` 的 CLI 状态摘要按一分钟间隔写入 stderr，包含计数而不含源码/推理正文；库调用默认不输出。状态输出失败不会改变审阅结果，stdout 的 JSON 契约保持。


## 上游依据与验证边界

本次默认值对照已发布的 `@deepseek-ai/dsh@0.2.1-rc.2` 及其同版依赖，核对时间 2026-10-28。参考 [DeepSeek 适配器](https://unpkg.com/@deepseek-ai/dsh-llm-deepseek@0.2.1-rc.2/lib/index.js) 的 1,000,000 窗口与 256,000 默认输出，以及 [compaction-basic](https://unpkg.com/@deepseek-ai/dsh-compaction-basic@0.2.1-rc.2/lib/index.js) 的 0.8 阈值、65,536 headroom 和 0.16 近期保留比例。旧工具结果缩减参考 [官方 tool-result-pruner](https://github.com/deepseek-ai/deepseek-harness/tree/master/packages/compaction/compaction-tool-result-pruner)。这里只适配本项目的消息、来源和记录契约，没有引入整套 Node CLI。

这些是客户端默认值，不是模型 API 最大能力保证。上游 master 的内置窗口、实时模型元数据和显式覆盖值可能不同，不能把本项目静态阈值当成所有 DSH 实例的实际触发点。配置更大的自定义输出额度仍受完整预留、供应商上限和 headroom 约束。

离线回归和模拟供应商验证覆盖运行器行为，不能据此证明真实长审阅质量或延迟改善。质量、运行成本和边界验证的方法见[验证计划](validation-plan.md)；恢复机制的实际供应商表现须依据单独保存的真实调用记录判断。


## 本地文件提交与诊断

`atomic_io.py` 为审阅、工作流、来源索引和裁决 JSON 提供同目录唯一临时文件与原子替换，使用 Tenacity 处理 Windows WinError 5/32/33 的短时占用。关键替换的重试等待预算为 1 秒、间隔 50ms；它不包裹 HTTP 或模型调用，持续失败仍抛出原异常。进度更新使用零重试预算，失败只记本地告警并继续同一个响应，子调用及根汇总可追溯告警。

关键提交失败时保留未提交的序列化文件，并尽力在同目录 `local_io_errors.jsonl` 追加操作、文件名和错误号等安全定位信息，不抄录材料或异常正文；该文件与模型原始记录一样可能包含源码，应本地保留。未提交文件不代表目标已经更新，也不构成审阅通过。POSIX 下新 JSON sidecar 默认为仅属主可读；更新已有普通文件时保留其权限位，其他原文/材料文件继续遵守各自已有写入方式和进程 umask，不能据此宣称整个输出目录已经统一访问控制。

普通真实审阅可以补齐旧隔离环境缺失的依赖；声明离线的查询、dry-run 和本地诊断只检查并在环境不完整时提示显式 `skill_runtime.py setup`，不隐式联网安装。pip 诊断走 stderr，CLI stdout 保持 JSON。独立的 `bailian_catalog.py` 显式目录刷新仍保留其标准库写入路径，不在上述审阅主链路迁移范围内。


## API 异常恢复与跨进程续跑

`api_recovery.py` 是不访问网络、不读取文件的分类与等待策略模块，`workflow.py` 负责执行。分类依据 HTTP 状态、结构化供应商错误及实际收到的推理、正文或工具片段；仅收到首个 SSE 事件或 usage 不算生成进度。

| 情况 | 工作流行为 |
| --- | --- |
| 尚无生成进度的暂时容量、限流、服务端或传输错误 | 在同一请求的恢复窗口内退避重试；成功的旧工具结果不重新执行 |
| 已有进度后的 deadline、推理引擎中止或断流 | 利用已有推理、可见发现及实际材料投递范围生成 checkpoint，再继续同一审阅 |
| 明确上下文超限 | 压缩进度并减少内联材料，完整来源仍可按需读取；不能形成更小请求时保留未完成 |
| 认证、参数、模型、策略拒绝、未知错误、本地故障或人工取消 | 停止自动恢复并保留原错误 |

默认 `api_recovery.retry_window_seconds=120`，从同一请求第一次可重试失败后开始计算；`0` 禁用这类同请求重试。它约束是否启动下一次 attempt，不是正在生成的请求总时限，也不会在 120 秒时切断正常流。供应商 `Retry-After`（秒或 HTTP 日期，另兼容 `retry-after-ms`）优先；否则使用 Tenacity 的 full-jitter 指数等待，multiplier 为 2 秒、上限 30 秒。要求等待超出剩余窗口时停止，不把供应商指定的等待截短后提前发出请求。没有增加默认模型轮数或工具次数硬上限，已有显式次数设置仍适用。

有进度的异常恢复与普通压力压缩共用 checkpoint 逻辑，但不保留旧工具对话尾部，避免压缩后又带回接近原大小的历史。摘要只整理同一任务的发现、证据位置、已投递材料范围、未完成项和不确定性，不把供应商错误、运维日志或主 GPT 裁决发给审阅模型。压缩器使用现有非思考、小输出路由，正式续审保留原模型、思考强度和配置输出额度。压缩调用若未能返回可用摘要，则保留未完成；不会递归启动另一层异常压缩。半截流中的工具参数不执行，真正的 `finish_reason=length` 仍走原有续审分支。

`review.py --resume <旧运行目录> --output-dir <新目录>` 恢复已结束且未完成的任务，先为原检查进度生成 checkpoint，再开始新的审阅请求。它从原请求保留模型参数，检查当前配置 endpoint 与原服务一致，跨时段也不静默切模型。`--dry-run` 只检查恢复材料与预算，标记 `checkpoint_pending`，不生成摘要或请求模型。

`review_resume.py` 保留旧已捕获文件的 source ID、视图和正文；这些路径再读时继续使用其捕获版本。此前未捕获的路径使用当前所选 Git/磁盘视图，来源标记为 `new_after_resume`，与 `restored_capture` 区分。旧版本与新读版本不可当作同一仓库时间点。当前凭据、输出等排除规则仍适用，不冻结工作区，不重放旧工具，也不将旧调用和 usage 计入新运行。`resume-state.json` 只在本地记录来源运行定位信息，不作为模型材料。

所有真实 attempt 分别保存原请求、响应、状态和台账项，根 `recovery_events` 记录恢复动作。`timing.successful_request_seconds`、`failed_request_seconds` 与 `retry_wait_seconds` 分开汇总；压缩请求耗时另有子集统计，不能重复相加。缺失的供应商 usage 保持未知，恢复成功也不把旧失败改为成功。报告仍须由主会话核实，checkpoint、合法 JSON 或请求完成都不能作为审阅通过依据。

退避及 `Retry-After` 处理参考 [OpenAI Python SDK](https://github.com/openai/openai-python/blob/main/src/openai/_base_client.py)，进度摘要衔接参考 [DeepSeek Harness compaction-basic](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/compaction/compaction-basic/src/index.ts)。本项目独立适配本地来源、审阅消息和记录契约；对 deadline 和跨进程恢复的扩展不表示上游已提供相同机制。

恢复耗时分类把正常的 `length` 终止单列为 `timing.incomplete_request_seconds`，不混入成功或故障耗时；明确收到正常 stop 的生成计入请求成功时间，但不因此提升工作流/报告的完整性状态。传输故障按 Requests 基类记录类别，连接/超时/流中断可恢复；TLS 证书等错误默认停止，保留具体异常类型供诊断。
