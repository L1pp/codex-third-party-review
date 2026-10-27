# Third-party notices

本项目自身代码与文档按根目录 [LICENSE](LICENSE) 中的 Apache License 2.0 发布。以下列出外部工具、依赖和资产边界；本文件不改变各自许可证。

## 外部官方百炼 CLI

本项目通过官方阿里云百炼 CLI（`bl`）的公开命令复用模型目录与本机配置能力，CLI 在用户机器上单独安装。本仓库未复制或再分发其 TypeScript 源码、打包产物或依赖。

官方上游为 [modelstudioai/cli](https://github.com/modelstudioai/cli)，采用 [Apache-2.0](https://github.com/modelstudioai/cli/blob/main/LICENSE)。SSE 分块边界处理参考其 [core/client/stream.ts](https://github.com/modelstudioai/cli/blob/main/packages/core/src/client/stream.ts) 的换行完整性约束，采用独立 Python 状态机并以 Requests 真实解码器离线验证；未内嵌其 TypeScript 实现。CLI 的供应商服务和账号权限仍由其自身规则决定。项目名称用于说明兼容对象，不表示阿里云为本项目背书。

## Python 包

依赖由用户或 CI 从 Python 包索引单独安装，本仓库未内置这些包。完整及传递依赖的许可证应以所安装发行版的许可文件为准。

| 包 | 用途 | 上游 |
| --- | --- | --- |
| Requests | HTTPS 与 SSE 流读取 | [psf/requests](https://github.com/psf/requests) |
| tzdata | IANA 时区数据 | [python/tzdata](https://github.com/python/tzdata) |
| tokenizers（可选） | 本地分词 | [huggingface/tokenizers](https://github.com/huggingface/tokenizers) |
| Jinja2（可选） | 渲染本地 chat template | [pallets/jinja](https://github.com/pallets/jinja) |

## DeepSeek tokenizer（不随仓库分发）

可选安装器仅从用户选定的本地目录/归档，或显式请求的官方下载中读取 `tokenizer.json` 与 `tokenizer_config.json`。官方来源为 [DeepSeek Token 用量文档](https://api-docs.deepseek.com/zh-cn/quick_start/token_usage/) 链接的归档。本项目尚未核明这些 JSON 的再分发许可，因此仓库和发布内容不包含它们；使用前请核对官方来源、适用条件与所用模型。

安装器不执行归档内的 Python 文件，不复制其他文件，也不进行 hash、指纹或冻结验证。本地资产不是本项目 Apache-2.0 许可的组成部分。

## GitHub Actions

CI 使用 GitHub 的 `actions/checkout` 与 `actions/setup-python`，在 GitHub 运行时按工作流引用获取；它们的源码未在本仓库内分发。上游分别见 [checkout](https://github.com/actions/checkout) 与 [setup-python](https://github.com/actions/setup-python)。
