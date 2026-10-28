# Third-party notices

Project-authored code and documentation are licensed under the root [Apache License 2.0](LICENSE). The components below retain their own terms. This document does not relicense upstream code, dependencies, downloaded assets, or provider services.

## DeepSeek Harness

The active runtime directly depends on official packages from [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness), licensed under [MIT](https://github.com/deepseek-ai/deepseek-harness/blob/master/LICENSE), copyright (c) 2026 DeepSeek. Exact package versions are declared in [runtime/package.json](runtime/package.json); the current integration uses Harness `0.2.1-rc.2` and Cordis `4.0.5`. Installed packages remain under the Skill's ignored `runtime/node_modules` directory and are not vendored into this repository.

The following focused source extracts are redistributed with their complete MIT copyright and permission notice in each file:

| Local file | Upstream source | Adaptation |
| --- | --- | --- |
| `runtime/vendor/dsh-pi-replay.mjs` | Published `@deepseek-ai/dsh-llm-pi-ai@0.2.1-rc.2` bundle; upstream `packages/llm/llm-pi-ai/src/replay.ts` and message projection helpers | Retained as the Harness/pi-ai message replay conversion seam |
| `runtime/vendor/dsh-pi-stream.mjs` | Published `@deepseek-ai/dsh-llm-pi-ai@0.2.1-rc.2` bundle; upstream `packages/llm/llm-pi-ai/src/stream.ts` | Includes the upstream-main malformed-tool-argument validation backport; stream conversion is upstream-derived |

The project's Apache-2.0 license does not replace these MIT notices. Bailian wire-field mapping, local no-hash storage, source/range capture, report completeness and adjudication, and the host-process bridge are project-specific adapters. The ordinary agent/session/retry/prune/compaction algorithms are provided by the declared upstream packages rather than a second Python loop. See [the reuse inventory](docs/harness-reuse.md) for ownership and deliberate exclusions.

## Node dependencies

Direct versions are fixed in `runtime/package.json`. Runtime setup installs them without npm lifecycle scripts, audit, or funding operations. Their full and transitive license terms are those of the installed distributions.

| Package family | Use | Upstream |
| --- | --- | --- |
| DeepSeek Harness and Cordis | Agent loop, session/events, tool execution, retry, timeout, compaction and pruning | [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness) |
| `@earendil-works/pi-ai` | Maintained OpenAI-compatible protocol and tool/reasoning stream handling | [earendil-works/pi](https://github.com/earendil-works/pi) |
| `eventsource-parser` | SSE framing in the maintained transport stack | [rexxars/eventsource-parser](https://github.com/rexxars/eventsource-parser) |
| `@huggingface/tokenizers`, `@huggingface/jinja` | Local native-tokenizer and chat-template estimation | [huggingface/tokenizers.js](https://github.com/huggingface/tokenizers.js), [huggingface/huggingface.js](https://github.com/huggingface/huggingface.js) |

Provider SDKs pulled transitively by pi-ai retain their own licenses. Installing a dependency does not enable unrelated provider accounts, shell tools, telemetry exporters, or model routes.

## Python dependencies

Python dependencies are installed separately from the Python package index; their source is not embedded here.

| Package | Use | Upstream |
| --- | --- | --- |
| Requests | Host downloads/catalog and compatible offline helpers; not the live model transport | [psf/requests](https://github.com/psf/requests) |
| tzdata | IANA time zones | [python/tzdata](https://github.com/python/tzdata) |
| json-repair `0.64.2` (MIT) | Local model-report syntax recovery without a model call | [mangiucugna/json_repair](https://github.com/mangiucugna/json_repair) |
| tenacity `9.2.1` (Apache-2.0) | Local Windows atomic-file-replacement retry policy | [jd/tenacity](https://github.com/jd/tenacity) |
| tokenizers and Jinja2 | Initial material-prefix selection and local preparation with the official assets | [huggingface/tokenizers](https://github.com/huggingface/tokenizers), [pallets/jinja](https://github.com/pallets/jinja) |

The project uses json-repair's installed parser and retains its own report selection, possible-loss checks and host adjudication. It does not copy the library's source. Tenacity remains a local file-I/O dependency; live API retry execution is supplied by Harness.

## Alibaba Cloud Bailian CLI

The optional [official CLI](https://github.com/modelstudioai/cli) supplies public model-catalog commands and an existing local profile format. It is installed separately and is not required for ordinary reviews. This repository does not copy or redistribute its TypeScript source or bundled dependencies. The upstream [Apache-2.0 license](https://github.com/modelstudioai/cli/blob/main/LICENSE) and service/account rules continue to apply. Compatibility descriptions do not imply vendor endorsement.

## DeepSeek tokenizer assets

`tokenizer.json` and `tokenizer_config.json` are installed locally from a user-supplied directory/archive or the explicitly requested official archive linked by [DeepSeek's token-usage documentation](https://api-docs.deepseek.com/zh-cn/quick_start/token_usage/). Their redistribution terms have not been established here, so this repository and its releases exclude them.

The installer reads only the selected JSON assets; it does not execute Python files from the archive, copy unrelated files, or perform hash/fingerprint/freeze verification. The assets are not relicensed under this project's Apache-2.0 license.

## GitHub Actions

CI obtains [actions/checkout](https://github.com/actions/checkout), [actions/setup-python](https://github.com/actions/setup-python), and [actions/setup-node](https://github.com/actions/setup-node) at the versions named in the workflow. Their source is not redistributed here. CI prepares package/assets separately and runs offline fixtures without model credentials.
