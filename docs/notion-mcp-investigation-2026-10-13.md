# Notion 原生 MCP 与本地工具接入调查

> 后续状态：本调查之后已完成原生 MCP、本机工具执行及 CPA Chat/Responses 往返验证。当前实现、限制及部署见 [notion-mcp-bridge.md](notion-mcp-bridge.md)。下文保留最初只读调查时的证据边界，不代表最终实现状态。

调查日期：2026-10-13。仅检查源码、登录态功能配置及请求 Schema；没有发起模型推理、注册连接、部署、构建或重启服务。

## 结论与验证范围

Notion 的普通个人 Agent 已有原生自定义 MCP 入口，不需要为了接工具专门创建 Custom Agent。当前账号允许该入口，因此值得沿这条路径实现本地工具接入。

**尚未完成真实工具调用。** 当前 notion2api 仍是文本桥接，原生 MCP 可用不等于 CPA 已能对客户端返回工具调用。没有验证指定 Claude 模型的 MCP 调用、额度实际扣账、流式暂停与工具结果续传。

此前“Notion 只能用提示词模拟 function calling”的结论不成立。此前“runInferenceTranscript 是唯一推理入口”的结论也超出了证据范围：当前分包已有独立 Agent Service transport、新建线程、发消息及续传代码。

## 已验证：当前登录账号

在既有 Notion 桌面端的 `/chat` renderer 读取实际加载的前端状态；没有修改功能开关：

| 检查项 | 实际值 |
| --- | --- |
| `script_agent_mcp_server` | `true` |
| `SpaceStore.isCustomMcpAllowed()` | `true` |
| `getMcpConnectionScope().arbitraryCustomAllowed` | `true` |
| `getMcpConnectionScope().allowlistEnforced` | `false` |
| 当前工作区是否位于用户管理的工作区列表 | `true` |
| 个人 `agent_chat_modules` 数量 | `0` |
| 桌面本机中继 `agent_client_mcp_enabled` | `false` |

内部套餐值为 `team`，未将内部枚举直接等同于对外套餐名称。账号的许可与前端 gate 是配置证据，不是服务端 MCP 调用成功的证据。

`getCreditRateLimitStatus` 返回 HTTP 200、`within_limit`：`basic_ai_credits` 的 6 小时窗口已用 `0.03/100`，账期已用 `8.63/100`。这是调查时的快照，不应作为固定额度或所有模型的收费规则。

读取路径：前端模块 `192314` 的 `AppStoreSidebarSpaceViewStore.getState().getSpaceStore()`；个人连接列表来自同一 SpaceView 的 `getSettingsStore().getKeyValue("agent_chat_modules")`。

## 已验证：现有桥接缺口

1. `app/schemas.py` 的 `ChatCompletionRequest` 没有 `tools`、`tool_choice` 字段。
2. 在现有服务器容器内只执行 Schema 解析，无上游网络调用：传入一个 `probe_echo` 函数定义后，`model_dump()` 只保留 `model/messages/stream/temperature/conversation_id`；`tools` 与 `tool_choice` 均丢失。
3. `build_standard_transcript()` 将 `enableScriptAgentMcpServers` 固定为 `False`。`customConnectorInfo` 和 `availableConnectors` 也为空，但这两个数组不是已证实的个人 MCP 模块载体，不能把它们为空单独当成 MCP 不可用的证据。
4. `ChatMessage._normalize_role()` 将历史工具调用和工具返回折成文本，缺少结构化 `tool_call_id` 往返关联。
5. `_build_stream_chunk()` 只输出文本、思考和角色；流结束使用 `finish_reason="stop"`，没有客户端 `tool_calls` 返回分支。
6. 现有 `agent-tool-result` 分类为工具内容，但这不构成 MCP 调用结果到客户端工具请求的协议转换。

所以单独打开一个开关、调整提示词、或宣称上游 Claude 支持工具，均不足以修复整条链路。

## 已定位：个人 MCP 的配置与持久化

静态定位于已下载的官方前端分包；这些接口没有在本次被用于新增或修改连接：

- `94988-73c2b6c8dfe98c46.js` 的 `postWorkflowsMcpServerConnect` 支持个人连接路径，传入 `spaceId`、`integrationId`、`moduleDefinition`，个人路径可省略 `workflowId`。定义包含 `name`、`serverUrl` 和工具自动执行选项；认证走 `credentialId` 或认证字段。
- `38514-903ae008a4533754.js` 的 `addAgentChatModule` 将模块引用保存到 SpaceView 的 `agent_chat_modules`，条目为 `{pointer, defaultEnabled}`。
- 模块 `805492` 从上述设置获取引用，再通过 `AgentModuleStore.createChildStore` 读取定义；检查模块属于当前用户、当前工作区且类型为 `mcpServer`。本次实际连接数为 0。
- `69506-8344f7aafe4ff0ba.js` 的 `getWorkflowAgentConfig` 将 `type:"workflow"` 与 `isCustomAgent` 分开。因此 `workflow` 线程类型本身不能证明请求在使用 Custom Agent 的计费路径。
- 通用连接读取事件为 `listExternalConnections` / `getExternalConnection`，但没有验证它们与个人 MCP 模块列表一一对应，不以这两个 API 替代已观察到的 SpaceView 数据来源。

服务端如何结合模块默认启用设置、会话来源选择与 config 决定实际可调用工具，仍需连接一个受认证的最小 MCP 服务后验证。

## 推荐实现边界

优先使用账号已经允许的个人 Agent 自定义 MCP 入口，保留 Notion 内的模型与对应额度计费路径。

对当前 CPA 使用方式，需要一个 MCP 中转层：

1. 客户端发来的工具定义与请求绑定，形成当前会话可见的工具清单。
2. Notion 通过已认证的 MCP 服务获取清单并调用工具。
3. 中转层将调用转换为客户端认识的结构化 `tool_calls`，执行仍由客户端本机承担。
4. 客户端下一轮提交工具结果时，按调用 ID 找回挂起的 MCP 请求，将结果交还 Notion 后继续生成。

这个设计仍待实现和端到端验证。必须明确处理调用超时、取消、并发隔离、重试去重和进程重启后的失效，防止串会话或把旧工具结果交给新请求。

仅希望在 Notion 界面操作本机工具时，可以由一个受认证的本地 MCP 执行服务直接返回结果；这和经 CPA 驱动客户端工具的方案不同，不能混作同一验收结果。

## 桌面中继为何不作为当前主路径

已有解包文件 `/tmp/notion-asar/clientMcpHost/` 实现了文件与 iMessage 工具。`service.js` 在本机生成 `agentToken`，服务器注册信息提供 `host_token`、中继地址等；此前把两者都描述为服务器下发不准确。

当前账号 `agent_client_mcp_enabled=false`。不应通过强改 gate 声称功能可用。即使桌面端 `hello` 能申报工具列表，也未验证服务器是否接受任意自定义工具名，因此这条路径保留为未验证线索。

CDP 历史抓取失败的一个已查明原因：脚本挑中了 `/blank` renderer。改为实际 `/chat` renderer 后，运行时只读检查立即成功。

## 额度与限制

官方文档区分套餐内使用额度和高级模型使用的 Notion credits。MCP 连接本身不意味着更换到 Anthropic API，但也不能据此承诺所有模型都扣同一个额度桶或不限量。

工具放在本机执行，并不意味着移除了 Notion 服务端 Agent 的默认指令、模型策略或用量限制。本次没有找到、也没有证实可以脱离 Notion 服务端直接使用其 Claude 额度的裸模型接口。

用户约束继续有效：禁止未经明确指示在服务器编译；模型验证只用最小 Haiku 请求，禁止用 Fable 测试。

## 官方资料

- 个人 Agent 的 MCP 接入：https://www.notion.com/help/connect-mcp-servers-to-your-notion-agent
- 个人 Agent 使用与额度：https://www.notion.com/help/notion-agent
- Custom Agent 的独立 MCP 连接：https://www.notion.com/help/mcp-connections-for-custom-agents

官方资料与本机证据均于调查日期核对。所有凭据均未写入此报告。
