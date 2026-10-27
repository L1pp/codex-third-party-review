# 复用与外部依赖

## 官方能力与本项目适配层

本项目保留已有 Python 审阅运行时。百炼模型目录通过官方 `bl model` 命令及其目录/补全数据服务复用，而不是在仓库中复制一套官方模型目录、价格表或登录实现。官方 CLI 负责登录与本机配置；审阅入口可读取配置指定 profile 中的现有 `api_key`，环境变量优先。

官方来源见 [modelstudioai/cli](https://github.com/modelstudioai/cli) 及 [阿里云模型目录命令文档](https://help.aliyun.com/en/model-studio/cli/config)。

官方 CLI 是外部可选工具，不是审阅入口的 Python 依赖，也不会被自动安装或触发登录。目录辅助功能与实际审阅模型调用是两条独立路径；目录结果不保证该账号具备模型权限，价格或限额也不构成服务承诺。具体目录适配接口以仓库中的适配脚本与其说明为准。

本次没有复制或再分发百炼 CLI 的 TypeScript 源文件，没有引入 Node 桥接层。HTTP/SSE、材料工具、预算、日志与台账仍由本仓库 Python 代码实现。参考官方接口、设计边界或测试思路，不等于内置或完整实现官方 CLI。

目录辅助入口是 `scripts/bailian_catalog.py`。只有显式 `refresh` 会启动官方 CLI 并联网读取公共目录，不调用审阅模型、不登录或更新凭据；`lookup` 只读取本地缓存：

```text
python scripts/bailian_catalog.py refresh --family qwen3.8-flash --family qwen3.8-max --family deepseek-v4.1-flash-1017
python scripts/bailian_catalog.py lookup --model deepseek-v4.1-flash-1017
```

`--family` 传给官方 CLI 的 `--model` 查询参数，既可用有效家族 ID，也可用上游支持的具体模型查询；可重复提供多个查询。上例在 CLI 2.5.1 已实际取得三组目录、九个具体模型。DeepSeek 具体 ID 返回的家族为 `group-deepseek`；不要假定简称 `deepseek` 也返回相同 JSON。不确定时先用官方 `bl model list` 查询。缓存默认写到被 Git 忽略的 `assets/bailian_catalog_cache.json`；自定义路径需把 `--cache <本地路径>` 放在 `refresh` / `lookup` 子命令之前。刷新失败保留已有缓存并返回诊断，不退回调用模型。

匹配只使用上游明确提供的模型 ID 或别名；`auto` 不匹配具体模型，家族级限额或相似名称也不会自动当成某个 API 模型的限额。目录数据与真实 Token Plan 服务参数可能存在差异，需要在最终配置中明确确认。

审阅入口仅在启动时读取 `model_catalog_file` 指定的本地缓存，不自动刷新，也不调用 CLI。已精确匹配的具体模型，其上下文、输入和输出上限只能被目录收紧，不能扩大调用者设定的较小上限；来源和实际生效值留在本地路由记录。`auto` 仍使用配置中明确列出的假设，不把某个 Qwen 家族上限当成路由结果。没有缓存、缓存损坏、未知名称或歧义别名时保留显式配置，不阻断审阅。`maxOutputTokens` 映射到配置的 `max_tokens`；是否另计思考、是否发送组合输出参数继续由路由显式配置，不能只凭目录字段自动推断总量。`predictConfig` 仅作为白名单元数据保存，未作为已验证的请求字段发送。

官方项目来源和许可证见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。当上游 CLI 的命令输出变动时，应更新本项目适配层及离线 fixture，不应静默推断缺失字段。

## 为什么保留现有调用层

本次按 CLI 2.5.1 的 [text/chat 实现](https://github.com/modelstudioai/cli/blob/main/packages/commands/src/commands/text/chat.ts) 核查：它适合一般单次模型调用，但流式 JSON 结果聚合的是 `content`，没有给本工作流完整保留推理、工具调用、用量和终止状态。命令参数也不能直接表达当前 Token Plan 使用的全部推理与组合输出配置。因此没有把请求层简单替换成 `bl text chat`，也没有为包装 TypeScript core 额外增加 Node 桥接进程。

可直接复用的是官方公共目录与现有凭据配置；SSE 换行边界处理作为实现参考并加入真实解码器回归。快照读取、90% 压缩续审、完整响应归档和轻量台账继续由 Python 运行时负责。以后官方 CLI 若提供完整机器可读流事件，再评估收敛请求层；这不是对未来版本能力的判断。

## 本次发布包含什么

- Skill 入口、Python 审阅运行时与离线测试。
- 不含秘密的配置示例、可选 tokenizer 安装器及说明。
- 依赖清单和 CI 定义。

不包含 API key、个人 `config.json`、审阅包、请求/响应日志、台账数据库，也不包含 DeepSeek tokenizer JSON。用户本机自行安装的官方 CLI 与可选资产继续适用各自的许可证、服务条款和账户规则；本项目的 Apache-2.0 不会重新许可这些内容。
