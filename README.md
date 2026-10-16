# notion-ai-mcp-tool-bridge

实验项目：让 Notion AI（Opus 5.5、Sonnet 5.5、Haiku 5.5 等）作为 OpenAI 兼容后端，并通过 MCP 回调执行**客户端本地工具**（Claude Code、Cowork、Codex 等）。

> **状态：已放弃（2026-10-16）。** 工具往返能跑通，但多步任务里 Notion 经常在工具结果之后只思考、不回答，然后断流，根因没有确诊。完整的完成情况、卡点和继续研究的建议见 **[docs/NOTION_MCP_BRIDGE_POSTMORTEM.md](docs/NOTION_MCP_BRIDGE_POSTMORTEM.md)**。公开出来，希望有人能突破。

## 基于的上游项目

本仓库基于 **[maverickxone/notion2api](https://github.com/maverickxone/notion2api)**（MIT License）修改而来。Notion 会话、账号池、流解析、模型注册、Web 前端等基础功能都来自上游，原 README 保留在 [docs/UPSTREAM_README.md](docs/UPSTREAM_README.md)（[English](docs/UPSTREAM_README_EN.md)）。感谢上游作者。

本仓库新增或改动的部分：

| 文件 | 内容 |
| --- | --- |
| `app/tool_bridge.py` | 内置 MCP 服务器；把 Notion Agent 的 `call_client_tool` 转成标准 `tool_calls` 交给客户端，再把结果交回挂起的调用；续传、超时、空结束重试、失败报错 |
| `app/tool_broker.py` | 工具会话、请求 ID、调用 ID、Schema 校验、结果匹配 |
| `app/stream_parser.py` | 去掉 Notion 整段重发造成的重复；最终答案补发 |
| `app/context_trimmer.py` | 发送前裁剪历史 |
| `app/image_uploader.py` | 图片附件上传 |
| `scripts/add_notion_account.py` | 加账号 → 注册个人 MCP 连接 → 启用模块 → 进账号池 |
| `scripts/mcp_tunnel_supervisor.py` 等 | 公网隧道守护、重连、探针 |
| `tests/` | 60 项离线测试，不访问 Notion |
| `docs/notion-mcp-bridge.md` | 部署与配置说明 |

## 快速了解

```
客户端 --/v1/chat/completions(tools)--> 桥接 --runInferenceTranscript--> Notion Agent
Notion Agent --MCP(公网 HTTPS)--> 桥接 /notion-mcp/  (call_client_tool)
桥接 --tool_calls--> 客户端执行 --role=tool--> 桥接 --> Notion 同一轮继续回答
```

运行测试：

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest tests -q
```

部署需要 Notion 账号的 `token_v2`、个人 `space_view_id`、一个公网 HTTPS 地址（Notion 从云端回调），细节见 [docs/notion-mcp-bridge.md](docs/notion-mcp-bridge.md)。不要提交 `accounts.json`、`.env`、`data/`。

## 免责声明

这是逆向接入 Notion 内部接口的研究代码，不是官方 API，可能随时失效，也可能违反 Notion 的服务条款。使用风险自负。

## License

MIT，见 [LICENSE](LICENSE)。上游版权归 notion2api 作者所有。
