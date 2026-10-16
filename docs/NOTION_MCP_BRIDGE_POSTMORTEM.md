# Notion AI 当作 OpenAI 兼容后端 + 本地工具调用：完整实验记录与复盘

> **状态：已放弃（2026-10-16）。**
> 工具往返本身跑通了，Haiku 多数情况下能完成多步任务；但 Sonnet 5.5 / Opus 5.5 在"工具结果回来之后"经常只思考、不回答，然后断流，同线程重试拿到空流。根因没有确诊。
> 这份文档尽量把**做过什么、为什么这么做、证据是什么、卡在哪里**都写下来。如果你突破了，欢迎开 issue 告诉我们。

- 本仓库基于 [maverickxone/notion2api](https://github.com/maverickxone/notion2api)，本文只记录我们新增的"工具桥接"部分。
- 部署和配置细节：[notion-mcp-bridge.md](notion-mcp-bridge.md)
- 最早的只读调查：[notion-mcp-investigation-2026-10-13.md](notion-mcp-investigation-2026-10-13.md)
- 模型代号表与刷新方法：[notion-model-registry.md](notion-model-registry.md)

## 目录

1. [目标与约束](#1-目标与约束)
2. [最终架构](#2-最终架构)
3. [实验时间线](#3-实验时间线)
4. [实现清单（问题 → 原因 → 改法 → 验证）](#4-实现清单)
5. [最终卡点（详细证据）](#5-最终卡点)
6. [试过但放弃 / 没采纳的路线](#6-试过但放弃--没采纳的路线)
7. [诊断方法和日志字段](#7-诊断方法和日志字段)
8. [给后来者：怎么复现、怎么判断"突破了"](#8-给后来者)
9. [配置项一览](#9-配置项一览)
10. [安全与合规提醒](#10-安全与合规提醒)

---

## 1. 目标与约束

**目标**：把 Notion AI 订阅里的模型（Claude Opus 5.5 / Sonnet 5.5 / Haiku 5.5 / Fable 5 等）包装成 OpenAI 兼容接口，挂到 CLIProxyAPI（下称 CPA）和自建 New API 网关后面，给 Claude Code、Cowork、Codex、DeepSeek Harness 这类**需要本地工具调用**的客户端使用。要求：

- 能真实读写本机文件、跑命令（不是 Notion 云端沙箱里的文件）；
- 流式输出稳定，不重复、不截断、不夹带 Notion 内部内容；
- 失败时明确报错，不能假装成功；
- 尽量省自己的 API 额度（这条路线的价值是"用 Notion 订阅额度"，不是更快）。

**实验过程中的约束**（解释了为什么有些验证没做）：

- 只用 Haiku 小请求做真实推理测试，不用 Fable/Opus 烧额度；
- 服务器上不编译、不 `pip install`：本机准备好源码和预编译 wheel，上传后 `docker restart`；
- 每次改线上文件前先备份；破坏性操作先确认。

## 2. 最终架构

```
┌────────────┐  /v1/chat/completions (tools)  ┌──────────────────────────────┐
│  客户端     │ ─────────────────────────────▶ │ 桥接服务 (FastAPI, 单 worker)  │
│ ClaudeCode │                                 │  app/tool_bridge.py           │
│ Cowork ... │ ◀── SSE: content / tool_calls ─ │  app/tool_broker.py           │
└────────────┘                                 └──────┬───────────────▲────────┘
      ▲   │ role=tool 结果(下一次 HTTP 请求)            │ runInferenceTranscript │ MCP: call_client_tool
      │   ▼                                             ▼ (workflow 线程)        │ (公网 HTTPS 回调)
  本机执行工具                                    ┌────────────────────────────┴───┐
                                                 │ Notion 云端 Agent (Opus/Sonnet…) │
                                                 │ 个人 MCP 连接 "Sam Local Tools"  │
                                                 └────────────────────────────────┘
```

一次带工具请求的生命周期：

1. 客户端发 `/v1/chat/completions`，带 `tools`。桥接用 `ToolBroker.create()` 建一个**工具会话**，生成随机 `request_id`。
2. 桥接裁剪上下文、脱掉历史里旧工具调用的大参数，在消息末尾追加一条 system 指令：告诉 Notion 用 `Sam Local Tools` 的 `call_client_tool`、本轮 `request_id` 是什么；工具目录 ≤6000 字符时直接内联进指令。
3. 用 `runInferenceTranscript` 发起 Notion 推理（`enableScriptAgentMcpServers=true`、关闭网页搜索、`surface=full_page_chat`），在后台线程消费 NDJSON 流，把 content/thinking 事件推进会话队列。
4. Notion Agent 决定调工具时，从 Notion 云端通过 **公网 HTTPS** 调我们内置的 MCP 服务器 `/notion-mcp/` 的 `call_client_tool(request_id, name, arguments)`。
5. 桥接校验工具名和参数 Schema，生成 `tool_call_id`，**挂起这个 MCP 调用**，同时给客户端发标准 `tool_calls` 分片 + `finish_reason=tool_calls` + `[DONE]`，结束这次 HTTP 响应（但 Notion 推理线程保持运行）。
6. 客户端执行工具，发下一次请求，消息里带 `role=tool`。桥接识别出这是**续传**（匹配待决调用），把结果交给挂起的 MCP 调用，MCP 返回给 Notion。
7. Notion 在**同一轮推理**里继续，可能再调工具（回到第 4 步），最后输出正文，桥接流给客户端，`finish_reason=stop`。

为什么是这个形状：

- Notion 自带工具都跑在它的云端沙箱，碰不到本机；唯一能把调用"引出来"的官方机制是**个人 Agent 的自定义 MCP 连接**（官方文档 *Connect MCP servers to your Notion Agent*）。
- MCP 由 Notion 服务端发起，所以地址必须公网可达（我们用 Cloudflare 命名隧道），`localhost` 不行。
- 桌面端有一个"本机 MCP 中继"开关 `agent_client_mcp_enabled`，在我们账号上是 `false`，没有强开。

## 3. 实验时间线

### 10-12：先把纯对话接上

- CPA 插件商店里没有 Notion 插件，决定用 notion2api 做 OpenAI 兼容桥接，CPA 用 `openai-compatibility` 接入。
- **Notion 静默拒绝脚本客户端**：返回 `temporarily-unavailable` 或 HTTP 200 空流。原因是 TLS 指纹，`requests`/`cloudscraper` 过得了 Cloudflare 但过不了 Notion。改用 `curl_cffi` + `impersonate="chrome"`（`chrome131` 这类旧指纹同样被拒）。
- 补齐 5.5 一代模型代号（Opus 5.5 → `albuquerque-quinn`，Sonnet 5.5 → `achira-donut`，Haiku 5.5 → `amla-jam`）。注意：**Notion 对不存在的模型代号不报错，会静默回退默认模型**。
- 大输入"静默掐流"：对齐官方前端完整的 config/context/payload 字段后，25 万字中文的 Haiku 请求能正常返回；多轮 user 消息合并为一块并标注"当前问题"，避免长历史淹没短问题。

### 10-13：只读调查 → 实现工具桥接 → 上线

- 只读调查（桌面端 CDP，注意要选 `/chat` renderer，不是 `/blank`）确认：普通个人 Agent **原生支持自定义 MCP**；账号 `script_agent_mcp_server=true`、`isCustomMcpAllowed=true`、无 URL 白名单。推翻了"Notion 只能用提示词模拟 function calling"的旧结论。
- 前端静态分析定位：个人 MCP 注册走 `postWorkflowsMcpServerConnect`（个人路径可省 `workflowId`），模块引用存在 SpaceView 的 `agent_chat_modules`（新增时 `defaultEnabled:false`）。普通 workflow 请求只带 `spaceViewId`，**没有把 MCP pointer 塞进 config 的字段**——不要凭空加 `enabledMcpServers`。
- 实现 `tool_broker.py`（会话、调用 ID、Schema 校验、结果匹配）和 `tool_bridge.py`（MCP 服务器 + HTTP 接入 + 流）。原生 MCP 探针、本地 Chat、CPA Chat、CPA Responses 都完成了真实往返：本机 `/bin/echo` 生成随机标记，回填后 Notion 返回相同标记。
- **客户端真实工具目录有 503 个函数**（622 KB），我们自设的 128 个上限把它拒了。去掉数量上限（保留总字节、Schema、重名检查）后，300 函数尾部调用 32 秒往返成功，完整 503 函数普通回复 8 秒成功。
- **模型静默降级**：用户选 Opus 5.5，Notion 界面显示 Sonnet 4.6。原因是模型名被转换了两次：第一次标准名 → 内部代号，第二次的映射器不认识内部代号，默默退回 `almond-croissant-low`（Sonnet 4.6）。**所以 10-13 下午所有"Haiku 验证"实际都跑在 Sonnet 4.6 上。** 修成幂等映射、未知名称直接报错，并在出站日志记录真正的 `upstream_model`。
- 部署：新容器只绑本机端口，旧容器保留做回退；CPA 只改 notion provider 的 base-url。

### 10-14：周边兼容问题

- Opus/Sonnet 5.5 上游**不支持强制 `tool_choice`**（`required` → 400 `type "tool" and "any" are not supported`），Haiku 正常。
- 客户端发 `reasoning.effort=none` 时 CPA 会转成 `thinking.type=disabled`，新一代 Claude 不接受；在 CPA payload 层做了兜底（与桥接无关，但影响同一批客户端）。

### 10-15：提速与稳定性

- **实测"走 Notion 更快"不成立**：短请求只比直连慢 0.2–0.4 秒（Opus 例外 2.4–5.9 秒，因为思考最低只能 `low`）；带工具两步流程才是瓶颈，Haiku 15.6–17.7 秒、Sonnet 11.7–11.9 秒，慢在"发起工具调用"的跨公网往返 + Notion Agent 调度。
- **目录内联**：≤6000 字符的工具目录直接写进指令，免掉一次 `list_client_tools`，Sonnet 两步从 ~12 秒降到 6–7 秒。
- **固定隧道**：临时 Cloudflare 隧道每次重启地址都变，要调 `reconnect_mcp.py` 去改 Notion 里的 MCP 地址；换成命名隧道固定域名。
- **会话保活**：thinking 阶段实测会连续沉默 27–64 秒，撞穿了 90 秒空闲超时，Notion 回填时拿到 404/410 → "工具通道断了"。每收到一个 Notion 事件（含 thinking）都刷新有效期，TTL 改为 300 秒。
- **SSE 心跳**：沉默 8 秒发 `: ping` 注释，防中间层断开；沉默 55 秒发一个 `reasoning_content` 占位"[正在思考...]"，避免客户端判定超时。
- **第二个账号**：每个 Notion 账号都要在自己的工作区单独注册 MCP 连接并启用模块，否则 Notion 不认；写了 `scripts/add_notion_account.py` 一条龙完成。
- 让 Notion 界面里的 Custom Agent 手动调本地工具：不通，也没必要——`request_id` 是会话隔离令牌，不能变成可选。

### 10-16：长任务稳定性攻坚 → 放弃

按"先确诊再动大手术"的原则一层层加诊断：

1. 工具调用 50 秒没结果先回"还在执行，请用相同参数重试"，重试时接回原调用。
2. 账号池从轮询改为顺序使用。
3. 会话号日志修正（原来截成了固定前缀，所有会话看起来是同一个）；每条流结束记录 `substantive / failure`。
4. 上游关流时若没有实质内容或有悬空工具调用，返回明确错误，不再假装 `stop`。
5. 取消思考强度透传；同一客户端会话一对一复用同一个 Notion 线程。
6. 发送前上下文裁剪（实测 144,622 → 29,999 估算 token）。
7. **续传判定修复**：Claude Code 在工具结果后会追加 system 提醒，原逻辑要求"最后一条是 tool"，结果工具结果被当成新请求、原会话永远等不到。改为在消息里匹配待决调用。日志确认 `mode=resume live=1` 生效。
8. **重复输出修复**：Notion 会把同一块内容整段重发，原样拼接导致回答重复 9 遍；改为同一 (segment, path) 只取新增后缀。
9. **"吐两个字就断"**：加了只记数量的汇总日志，定位到续传那轮 Notion 产出 183 条 thinking、只有 2 条 content，桥接把 thinking 发成 `reasoning_content`，客户端不显示。
10. **最终答案被截断**：某次 `final_chars=1026` 但只发出 `content_chars=304`，`final_content` 因为前面已发过正文被整段跳过；改为前缀匹配时只补发剩余部分。
11. 续传后空结束**自动重试一次**；指令里禁止输出会话摘要 / 只给计划不执行。
12. 用 Haiku 实测：多步编辑任务能完成，文件真的被改了，但正文里仍夹带 JSON 形式的"假工具调用"文本；Sonnet/Opus 多步任务基本失败在第 5 节描述的卡点。

当天下午决定放弃，撤掉所有线上 Notion 配置。

## 4. 实现清单

| # | 问题现象 | 原因 | 改法 | 验证 |
| --- | --- | --- | --- | --- |
| 1 | 空流 / temporarily-unavailable | TLS 指纹 | `curl_cffi` impersonate chrome | 实测可用 |
| 2 | 选的模型和实际跑的不一致 | 模型名二次转换，未知代号静默回退 | 幂等映射，未知名报错，出站记 `upstream_model` | 27 个模型离线 + 拦截 HTTP post |
| 3 | 工具定义被丢弃 | 原 Schema 没有 `tools`/`tool_choice` | 新增字段，`role=tool`/`tool_calls` 结构化往返 | 测试 |
| 4 | Notion 碰不到本机 | 只有云端沙箱工具 | 个人 MCP 连接 + 内置 MCP 服务器 + 挂起调用 | 本机 echo 随机标记往返 |
| 5 | 真实客户端被拒 | 自设 128 函数上限 | 去掉数量上限，保留 1 MiB 总量/64 KiB 参数/1 MiB 结果 | 503 函数 |
| 6 | 跨会话串调用风险 | — | 随机 `request_id` + `tool_call_id`，绑定 owner/model，拒绝过期/重复/跨会话结果 | 测试 |
| 7 | 两步工具流程慢 | 多一次 `list_client_tools` | 小目录内联 | Sonnet 12s → 6–7s |
| 8 | 隧道地址变 | 临时隧道 | 命名隧道 + `reconnect_mcp.py` | — |
| 9 | "工具通道断了" | 长思考撞穿 90s 空闲超时 | 事件保活 + TTL 300s | 日志 |
| 10 | 客户端/中间层断开 | 长沉默 | 8s `: ping`，55s 思考占位 | 测试 |
| 11 | 长输入零输出 | 68k–72k token 输入 | 发送前裁剪（45k 触发，30k 目标） | 144k → 30k |
| 12 | 工具结果后会话"失联" | Claude Code 追加 system 提醒，续传判定失败 | 匹配待决调用，不看末尾角色 | 回归测试 + 日志 |
| 13 | 回答重复 N 遍 | Notion 整段重发增量 | 同 (segment, path) 只取后缀 | 真实请求 |
| 14 | 回答截断 | `final_content` 被跳过 | 前缀匹配补发剩余 | `final_chars` vs `content_chars` |
| 15 | 静默"成功" | 上游关流按 stop 处理 | 无实质内容/悬空调用 → 明确报错 | 测试 |
| 16 | 续传后空结束 | Notion 侧 | 同线程自动重试一次 | **重试拿到空流，无效** |
| 17 | 模型输出会话摘要/只给计划 | Notion 内置行为 | 指令约束 | 仍偶发 |
| 18 | 多账号 | 每账号要单独注册 MCP | `add_notion_account.py` | 两账号可用 |
| 19 | 看不到图片 | — | `image_uploader.py` 上传附件 | 测试 |

离线测试 60 项 + 139 子测试，覆盖 MCP SDK/HTTP、流式与非流式、300 函数目录、权限撤销、重放、超时、并发、Schema、续传、去重、裁剪、思考占位等。

## 5. 最终卡点

### 5.1 主要卡点：工具结果回来后，Notion "只思考不回答"然后断流

**典型日志**（一次失败的多步任务，字段含义见第 7 节）：

```
Client tool request mode=new  messages=12 tools=110 tool_results=2 live=0
Delivering tool call to client session=… tool=Read
Bridge stream summary content_chars=0 tool_calls=1 ended=tool_calls
Client tool request mode=resume messages=15 tools=110 tool_results=3 live=1   ← 结果已交回
Parser stream summary events={'thinking': 19} text_chars=514                  ← 只有思考
Notion turn ended without an answer, retrying once
Parser stream summary events={} text_chars=0                                  ← 重试：空
Notion tool stream failed: NotionUpstreamError: Notion upstream returned an empty stream.
Notion tool stream finished events=19 substantive=False total=68.2s idle_since_last=64.1s
```

**已经确认的事实**

- 桥接收到了工具结果，MCP 调用正常返回给 Notion（回调链路本身是通的：同一天有 12 次工具结果成功回流、10 轮连续续传成功）。
- 失败那轮 Notion 只产出 thinking，约 60 秒沉默后关流。
- 立刻在同一线程重试，Notion 0.6 秒返回空流（0 事件）。
- 之后几次新请求也连续 `NotionUpstreamError`，看起来像一段时间内这个账号/线程都不可用。
- 模型差异明显：Haiku 5.5 多数能完成多步编辑；Sonnet 5.5、Opus 5.5 的多步工具任务大概率失败在这一步。
- 失败时 Notion 网页端那条对话里，任务"看起来还在跑"或者后来补全了答案——也就是说**我们这边断了，Notion 那边不一定断了**。

**没有确认的候选原因**（按我们的怀疑程度排序）

1. **线程状态**：MCP 回调之后同一线程进入某种异常状态。我们的续传方式（同一个 `runInferenceTranscript` 流里等 MCP 返回）可能和 Notion 前端的真实做法不一样——前端可能在工具完成后用另一种请求继续推理。**这是我们认为最值得查的方向，但没查到证据。**
2. **输入长度**：早期失败请求 68k–72k token，成功的 ≤26k。加了裁剪后仍有失败，但没有单独记录失败时的输入长度。
3. **限流 / 账号降级**：重试瞬间空流很像限流。我们没有记录上游 HTTP 状态码和响应头，无法排除。
4. **我们的注入指令或工具结果格式**让模型不知道怎么收尾。没有做对照实验。

### 5.2 模型行为不受控

- **假工具调用**：模型有时把调用写进正文，比如 `[tool call] Bash({})`，或者 `{"function": "connections.mcpServer_sam_local_tools.runTool", "args": {...}}`。后者暴露出 Notion Agent 内部其实是在一个"脚本 agent"里用 `connections.<连接名>.runTool` 调 MCP。有时真实调用也执行了，有时没有。
- **内部内容泄漏**：模型会把 Notion 自己的上下文压缩记录（大段英文 *Session Memory / User Intent / Pending Tasks*）当成正文输出。加指令约束后仍偶发。
- **thinking/content 归类**：续传后大部分输出常被标成 thinking，客户端不显示，看起来像"断了"。
- **内置提示词干扰**：请求里出现"本地""操作"之类字样但其实不需要工具时，模型会回复"我是沙箱，不能操作你的本地"。
- **只给计划不执行**：用户已经明确要求修改，模型仍先写计划问"要不要改"。

### 5.3 结构性限制（不太可能靠桥接解决）

- 走的是 **Notion 服务端 Agent**，不是裸模型 API：内置指令、配额、使用政策都在；Opus 思考强度最低 `low`，关不掉；每轮上下文里还有约 78 个 Notion 自带工具 + 约 18 个电脑/MCP 操作工具。
- 首字延迟高；带工具两步流程 6–18 秒。
- Opus/Sonnet 5.5 不支持强制 `tool_choice`。
- 每个账号都要注册 MCP 连接；待决调用在进程内存里，只能单 worker，重启即丢；MCP 回调必须公网可达。
- 不能自动换账号重试（账号失效直接 401 失败）。

## 6. 试过但放弃 / 没采纳的路线

| 路线 | 结果 |
| --- | --- |
| 只靠提示词让模型"模拟" function calling | 早期结论，后来证明 Notion 原生支持 MCP，没必要 |
| 换别的 Notion 反代项目（如 Notion2API-Tutorial 一类） | 同样只做对话，不解决工具问题 |
| 桌面端本机 MCP 中继 | 账号上 `agent_client_mcp_enabled=false`，没有强开 gate，未验证服务端是否接受任意工具 |
| 绕开 Notion 沙箱、用 Notion 额度直接请求模型 | 没有找到、也没有证实存在这样的裸模型接口 |
| 在 Notion UI 的 Custom Agent 里调本地工具 | Custom Agent 的 MCP 连接是另一套，且 `request_id` 不能省略 |
| 思考强度透传 | 做过，后来按需求取消 |
| 账号池轮询 | 改为顺序使用 |
| **方案 B：文本协议工具调用**（让模型在正文输出 JSON，桥接解析） | 没采纳：丢 Schema 校验、大参数转义脆弱、每轮多一次推理、工具输出变可见消息有注入风险、和 Notion 内置 agent 行为抢方向盘 |
| **方案 C：占位结果**（Notion 不等结果先收尾，下轮再喂） | 没采纳：未确诊前不动大手术 |
| **新开线程续传**（空流时换新线程，把工具结果当文本塞进去） | 讨论过，没实现；代价是那一轮失去原生工具调用 |

## 7. 诊断方法和日志字段

最有用的方法是：**先加只记数量、不记内容的汇总日志，把问题定位到具体某一层，再改逻辑。**不要靠猜。

| 日志行 | 字段含义 |
| --- | --- |
| `Client tool request mode=new/resume messages= trailing= tools= tool_results= live=` | 新请求还是续传；`trailing` 是最后一条消息的角色；`live` 是本次请求里能匹配上的待决工具结果数 |
| `Delivering tool call to client session= tool= call_id=` | 桥接把 Notion 的调用转给客户端 |
| `Client tool results accepted session= pending=` | 结果被接受，剩余待决数 |
| `Parser stream summary events={...} text_chars=` | 解析层产出的事件类型计数（content / thinking / final_content / search） |
| `Bridge stream summary content_chars= tool_calls= ended= final_chars=` | 实际发给客户端的正文字数、工具调用数、结束方式；`final_chars` 是 Notion 最终答案长度，`-1` 表示没收到 |
| `Notion tool stream finished events= substantive= total= idle_since_last= failure=` | 上游流结束；`substantive` 是否有实质正文 |
| `Notion tool stream gap gap=` | 单次沉默超过 15 秒 |
| `Notion turn ended without an answer, retrying once` | 触发了空结束重试 |
| `Final answer diverges from streamed content` | 最终答案和已发正文对不上 |

判断方法举例：

- parser 的 content 很多，但 bridge 的 `content_chars` 很少 → 桥接发送层的问题；
- parser 本身 thinking 多、content 少 → Notion 把输出归成了思考；
- `final_chars` 远大于 `content_chars` → 最终答案没补发；
- `substantive=False` 且 `idle_since_last≈60s` → 第 5.1 节的卡点。

## 8. 给后来者

### 复现

1. 按 [notion-mcp-bridge.md](notion-mcp-bridge.md) 部署：需要 `token_v2`、`space_id`、个人 `space_view_id`、一个公网 HTTPS 地址、32 字符以上的 MCP 鉴权密钥。
2. 用 `scripts/add_notion_account.py` 注册个人 MCP 连接并启用模块（也可以在 Notion UI 手动添加）。
3. 用 Claude Code 或任意 OpenAI 兼容客户端，选 Sonnet 5.5，发一个需要"先读文件、再改文件、再读回核对"的任务（至少 3 次工具调用）。
4. 看第 7 节的日志。

### 建议先做的实验

1. 空流时记录上游 HTTP 状态码、响应头、输入 token 数。
2. 同一个失败请求，分别在**原线程**和**新线程**各重放一次，对比。
3. 统计失败 / 成功请求的输入长度分布，看是否有阈值。
4. 抓 Notion 网页端在 MCP 工具调用完成后的真实网络请求：它是继续读同一个流，还是另发了一个续传请求（可能带我们没发的字段）？**这很可能就是根因所在。**
5. 对照实验：去掉我们注入的 system 指令（只保留最少的 `request_id` 说明），看失败率是否变化。

### 怎么判断"突破了"

至少满足：

- Sonnet 5.5 和 Opus 5.5 连续完成 10 次"读 → 改 → 读回核对"的多步任务，每次 ≥3 次工具调用；
- 日志里没有 `substantive=False` 的续传、没有 `empty stream`；
- 每轮 `content_chars` 与 `final_chars` 一致；
- 正文里不出现 `[tool call]`、`connections.…runTool` 文本，也不出现 Session Memory 之类的内部内容；
- 文件确实被修改（看修改时间或 diff）。

## 9. 配置项一览

| 环境变量 | 默认值 | 作用 |
| --- | --- | --- |
| `MCP_BRIDGE_TOKEN_FILE` | 空 | MCP 鉴权密钥文件（≥32 字符），为空则不启用桥接 |
| `MCP_BRIDGE_HOSTS` | `127.0.0.1:*,localhost:*` | 允许的 Host，需包含公网域名，不要通配 |
| `MCP_SESSION_TTL_SECONDS` | 300 | 工具会话空闲有效期（收到任何 Notion 事件都刷新） |
| `MCP_CALL_TOOL_TIMEOUT_SECONDS` | 50 | MCP 调用等待客户端结果的时长，超时后让 Notion 用相同参数重试 |
| `MCP_INLINE_CATALOG_LIMIT` | 6000 | 工具目录小于此字符数时内联进指令 |
| `MCP_STREAM_HEARTBEAT_SECONDS` | 8 | SSE `: ping` 心跳间隔 |
| `MCP_THINKING_NOTICE_SECONDS` | 55 | 长沉默时发"[正在思考...]"占位 |
| `MCP_SILENT_END_SECONDS` | 90 | 沉默后无回答关流时的判定阈值 |
| `MCP_THREAD_TTL_SECONDS` | 21600 | 客户端会话 ↔ Notion 线程映射的有效期 |
| `MCP_MAX_CONVERSATION_THREADS` | 200 | 映射表上限 |
| `NOTION_CONTEXT_TRIGGER_TOKENS` | 45000 | 超过此估算 token 才裁剪 |
| `NOTION_CONTEXT_TARGET_TOKENS` | 30000 | 裁剪目标 |
| `NOTION_MESSAGE_CAP_TOKENS` | 8000 | 最新一轮单条消息上限 |
| `NOTION_HISTORY_MESSAGE_CAP_TOKENS` | 1500 | 历史单条上限 |
| `NOTION_MAX_HISTORY_MESSAGES` | 24 | 保留的历史条数 |
| `NOTION_MAX_IMAGES` | 6 | 单次请求图片数 |
| `MCP_PUBLIC_HOST` / `MCP_PUBLIC_URL` | — | 探针脚本 / 隧道守护使用的公网地址 |

硬限制（代码常量）：工具定义总量 1 MiB、单次参数 64 KiB、单次结果 1 MiB、最多 8 个在途上游请求。

## 10. 安全与合规提醒

- `token_v2` 等于登录态，不要提交 `accounts.json`、`.env`、`data/`，不要贴到聊天或 issue 里。
- MCP 端点用独立密钥鉴权，`MCP_BRIDGE_HOSTS` 不要用通配符。桥接**不提供服务器 shell**，所有工具都在客户端本机按客户端自己的审批执行。
- 多个最终用户共享同一个 Notion 账号/工作区，不等于彼此隔离；Notion 内建能力和工作区数据仍可能被模型访问。
- 这是逆向 Notion 内部接口的研究代码，接口随时可能变，也可能违反 Notion 服务条款。
