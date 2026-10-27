# 可选 DeepSeek tokenizer

本目录在公开仓库中只保留本说明。`tokenizer.json` 与 `tokenizer_config.json` 的再分发许可尚未核明，安装后的 JSON 及其他本地资产均被 Git 忽略。

缺少文件、`tokenizers` 或 `Jinja2` 时，审阅流程仍可使用带方法标记的 UTF-8 fallback，不要求先下载资产。本地分词结果也不等于服务端 `usage`，更不保证 `auto` 路由背后的实际模型采用相同 tokenizer。

## 从官方来源自行获取

先查看 [DeepSeek 官方 Token 用量文档](https://api-docs.deepseek.com/zh-cn/quick_start/token_usage/)，确认当前下载和适用条件。该页面目前链接 [deepseek_v4_tokenizer.zip](https://cdn.deepseek.com/api-docs/deepseek_v4_tokenizer.zip)。官方链接可能更新；安装器使用此固定来源，不会推断其他模型资源地址。

在 Skill 根目录手动选择一种安装方式：

```text
# 复制你已下载并解压的目录中的两个 JSON
python scripts/install_tokenizer.py --source-dir <本地官方tokenizer目录>

# 从你已下载的官方 ZIP 中只取两个 JSON
python scripts/install_tokenizer.py --source-zip <本地官方tokenizer.zip>

# 明确选择下载当前官方 ZIP；需要网络，不会自动重试
python scripts/install_tokenizer.py --download-official
```

安装器仅使用 Python 标准库；网络下载只在显式使用 `--download-official` 时发生。它先校验两个 JSON 的基本结构，再写入此目录。它不会解压/执行 Python 代码，不复制归档中的其他文件，不做 hash、指纹或冻结验证。已有文件默认拒绝覆盖；确认需要替换时添加 `--force`。

随后安装可选运行依赖：

```text
python -m pip install -r requirements-tokenizer.txt
python -m unittest discover -s scripts -p "test_context_primitives.py"
```

两个原生 tokenizer 专属测试仅在依赖或资产缺失时 skip；已存在但损坏/不兼容的资产会使这两个测试失败，以便发现问题。fallback 和预算测试不因缺少资产跳过。CI 默认不下载、不安装本目录中的 JSON。
