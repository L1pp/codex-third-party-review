# 架构与边界

## 一次审阅如何执行

```text
主会话整理 packet.json
  -> review.py：配置、凭据、路由、HTTPS/SSE、结果解析
  -> workflow.py：材料投递、只读工具循环、预算与压缩续审
      -> materials.py：内存快照索引、分页读取、字面搜索
      -> context_budget.py：本地估算与输入/输出预算
  -> report.md + result.json + workflow.json
主会话核实发现、决定修改、运行正常验收
```

`scripts/review.py` 是 CLI 入口。可选的 `bailian_catalog.py` 缓存提供具体模型的供应商上限，正常审阅只读本地元数据；显式配置较小时保持较小值。生产运行时调用配置的外部 API；模型侧可请求的工具只读取当前 `MaterialStore` 中的快照。工具不按模型给出的路径打开仓库文件，不执行命令、不写代码。主机脚本仍需读取调用者指定的配置与 packet、写本地输出，并在真实审阅时发送材料。

## 材料投递与上下文

packet 常用字段是 `task`、`requirements`、`diff`、`files: [{path, content}]` 和 `test_results`。路径作为材料标识，而不是授予模型的文件系统权限。不同内容的同路径快照会保留为可区分材料。

默认优先直接内联约 100,000 本地估算 token 的材料，超出部分保留在内存索引内，可通过有限的 `list_materials`、`read_material`、`search_material` 按需读取。工具有返回字符、行数与分页游标限制。任务描述、材料索引、部分材料内联和工具结果都占上下文。

这是一次工作流，而不保证只有一次 HTTP 请求。模型请求材料时会继续对话；接近配置阈值时，运行时用模型生成检查点并续审。示例配置在估算上下文的 90% 触发压缩，具体值可调整。源材料仍保留在快照存储中；压缩摘要不能替代主会话的最终核实。

`automatic_retries=0` 禁止网络失败后自动重试。`max_model_calls` / `max_compactions` 可设为正整数，`null` 表示未限制次数。达到配置限制、无法压缩到预算或服务失败时，工作流返回不完整/失败状态，已有文本可能只是部分报告。

## token 估算的含义

安装可选依赖与官方资产后，DeepSeek 路由可使用本地 tokenizer 和 chat template，再分别计入工具 JSON 与推理字段。结果记录 `method`，它仍是本地估算，不等于服务端的真实请求封装或 `usage`。

整段请求预算在其他模型路由，或 DeepSeek 依赖/资产不可用时，使用明确标注的 UTF-8 字节上界加结构开销。材料前缀选择单独估算：有本地 tokenizer 时，非 DeepSeek 路由也会借用它作为带警告的近似值；没有可用资产时按每 4 字节约 1 token 选择前缀。两者用途不同，均不表示已识别 `auto` 背后的模型 tokenizer。保守预算可能导致较早压缩；输出能力和服务限制须由配置和真实服务另行确认。

## 本地记录与核实

输出根目录保存 `materials.json`、`workflow.json`、`result.json`、`report.md`，必要时有 `checkpoint.txt`；每次模型请求使用独立子目录，保存请求、预算、响应、状态及可用日志。`response_log.py` 尽力保存 SSE 事件和推理文本，对当前已知 API key 做脱敏；失败会记录 warning。

`ledger.py` 以 SQLite 记录允许的调用摘要和输出目录路径，不在台账内保存完整源码或响应正文。台账与原始日志用途不同；台账仍可能包含任务短标题和本地路径，应本地保留。

`coverage.status` 明确为 `not_verified_by_workflow`。已索引、已内联或已通过工具投递的范围仅说明传输过程，不能证明模型看懂、覆盖完整或找到所有问题。主会话需要结合源代码、契约和真实测试逐项裁决。

## 可移植性

所有资源按脚本或已加载 `SKILL.md` 的实际位置定位。公共仓库不依赖个人用户名、Windows 盘符、Claude 专属环境变量或 Skill 级模型字段。路由通过独立本地配置控制，不改 Codex 全局模型设置。Python 3.10+ 可运行；任意时区在缺少系统 IANA 数据时由 `tzdata` 提供。
