# Notion AI 当作 OpenAI 兼容后端 + 本地工具调用：实验复盘

> 状态：**已放弃（2026-10-16）**。本文记录做到了哪里、卡在哪里，留给想继续尝试的人参考。
> 代码在本仓库的 `app/tool_bridge.py`、`app/tool_broker.py`、`app/stream_parser.py`、`app/context_trimmer.py`、`app/image_uploader.py` 和 `scripts/` 下。部署细节见 [notion-mcp-bridge.md](notion-mcp-bridge.md)，早期调查见 [notion-mcp-investigation-2026-10-13.md](notion-mcp-investigation-2026-10-13.md)。

## 目标

用 Notion AI 订阅里的模型（Opus 5.5、Sonnet 5.5、Haiku 5.5 等）作为 OpenAI 兼容接口，接入 Claude Code、Cowork、Codex 这类需要**本地工具调用**（读写文件、跑命令）的客户端，并且做到稳定、不重复、不跑偏。

## 为什么需要"工具桥接"

Notion AI 自己有工具，但都跑在 Notion 的云端沙箱里，碰不到用户本机。普通的 notion2api 只能做纯对话。要让模型操作本机，必须让 Notion 的 Agent 调用一个**我们控制的工具**，再把调用转交给客户端执行。

## 架构

```
客户端(Claude Code/Cowork)  --/v1/chat/completions(tools)-->  桥接服务
                                                               |
                                    起一轮 Notion 推理(runInferenceTranscript)
                                    注入指令：用 Sam Local Tools 的 call_client_tool
                                                               |
Notion 云端 Agent  --MCP(HTTPS, 公网隧道)-->  桥接内置 MCP 服务器(/notion-mcp/)
   call_client_tool(request_id, name, args)                    |
                                     挂起这次 MCP 调用，返回给客户端标准 tool_calls
客户端执行工具，下一次请求带 role=tool 结果  -->  桥接把结果交回挂起的 MCP 调用
                                     Notion 同一轮推理继续 -> 流式正文 -> 客户端
```

要点：

- 每个 Notion 账号要在自己工作区注册一个个人 MCP 连接（我们叫 `Sam Local Tools`），并在 Agent 模块里启用。MCP 地址必须是公网 HTTPS（Notion 从云端发起调用，`localhost` 不行），所以用了 Cloudflare 固定隧道。
- MCP 只暴露两个工具：`list_client_tools`（返回本轮客户端工具目录）和 `call_client_tool`（转发调用）。工具目录小时直接内联进指令，省一次往返。
- 每个请求一个随机 `request_id`，每次调用一个随机 `tool_call_id`；只接受本轮声明的工具和符合 Schema 的参数；拒绝过期、重复、跨会话的结果。
- `scripts/add_notion_account.py` 一键完成：加账号 → 注册 MCP 连接 → 启用模块 → 进账号池。

## 已经做成的

1. **原生工具往返可用**：Haiku/Sonnet/Opus 都能真实调用本机工具并拿到结果继续回答；Responses 和 Chat 两种接口经 CPA 转换都跑通过；503 个函数的大工具目录也能用。
2. **流式输出与解析**
   - Notion 会把同一块内容按"整段重发"的方式增量更新，原样拼接会让回答重复 N 遍。`stream_parser.py` 对同一 (segment, path) 只取新增后缀。
   - 最终答案（`final_content`）若比已流出的正文长，只补发剩余部分（修复前长回答会被截在中间）。
3. **续传判定**：Claude Code 会在工具结果后追加一条 system 提醒，原逻辑要求"最后一条是 tool"，导致工具结果被当成新请求。改为在消息里匹配待决调用。
4. **一对一会话**：同一客户端会话复用同一个 Notion 线程（键 = 首条用户消息 + 模型 + 账号）。
5. **上下文裁剪**：发送前按估算 token 裁剪历史（45k 触发，目标 30k），避免超大输入。
6. **失败要说出来**：上游关流时若没有实质内容或有悬空工具调用，返回明确错误，而不是假装成功。
7. **其他**：账号池顺序使用、工具调用超时 50 秒后提示重试并可接回原调用、静默期发"正在思考"占位、图片上传、思考强度不透传、注入指令禁止模型把会话摘要当回答、禁止只给计划不执行。
8. 离线测试 60 项（`.venv/bin/python -m pytest tests -q`），不访问 Notion。

## 最终卡在哪里

### 1. 工具结果回来后，Notion 经常"只思考不回答"然后断流（主要阻塞）

现象（服务器日志实测，多次复现）：

- 工具结果交回后，Notion 这一轮只产出 thinking，没有任何正文，约 60 秒后关流。
- 同一线程里立刻重试，Notion 直接返回**空流**（0 个事件，0.6 秒结束，`Notion upstream returned an empty stream`）。
- Haiku 大多能完成；Sonnet 5.5、Opus 5.5 在多步工具任务里大概率在这一步失败。

我们能确认的：桥接这边收到了工具结果、MCP 调用正常返回，失败发生在 Notion 服务端。**根因没有确诊**。候选：

- 同一线程在工具回调后的状态异常（最可疑，未验证）；
- 输入过长：早期失败请求 68k–72k token，成功的 ≤26k；裁剪后仍有失败，没单独记录失败时的长度；
- 上游限流或账号降级：没有记录上游 HTTP 状态码和响应头，无法排除；
- 我们的注入指令/工具结果格式让模型不知道怎么收尾。

没做完的绕法：空流时**新开 Notion 线程**，把已有工具结果当普通文本放进提示，让模型直接回答。代价是那一轮失去原生工具调用，多步任务受限。

### 2. 模型行为不受控

- 模型有时把工具调用写进正文（`[tool call] Bash({})`、`{"function":"connections.mcpServer_sam_local_tools.runTool",...}`），而不是真正调用。实际编辑有时执行了、有时没有，客户端显示很乱。
- 模型会把 Notion 内部的"上下文压缩/会话记忆"作为正文输出（大段英文 Session Memory）。加了指令约束后仍偶发。
- 续传后的大部分输出常被 Notion 标成 thinking 而不是正文，客户端看不到。
- Notion 有大量内置提示词：用户请求里出现"本地""操作"等字样而又不需要工具时，模型会回复"我是沙箱，不能操作你的本地"。

### 3. 结构性限制

- 走的是 Notion 服务端 Agent，不是裸模型 API：内置指令、配额、使用政策都在，思考强度无法关闭（Opus 最低 low），首字延迟高（工具目录要加载、每轮约 78 个 Notion 自带工具也在上下文里）。
- 每个账号都要手工/脚本注册 MCP 连接，换账号成本高。
- 待决调用保存在进程内存里，重启即丢失，只能单 worker。
- MCP 回调必须公网可达，需要隧道。

## 没采纳的方案

- **文本协议工具调用**（让模型在正文里输出 JSON 调用、桥接解析）：失去 Schema 校验，大参数转义脆弱，每轮多一次推理，工具输出变成可见消息有注入风险。
- **占位结果**（让 Notion 不等工具结果先收尾，下轮再喂回去）：没确诊前不动大手术。

## 如果你想继续

建议先补诊断再改架构：

1. 空流时记录上游 HTTP 状态码、响应头、输入 token 数；
2. 同一失败请求分别在原线程和新线程各跑一次，对比；
3. 统计失败/成功请求的输入长度分布；
4. 研究 Notion 前端在工具调用后是如何继续推理的（是否有我们没发的续传字段），这可能才是根因。

## 安全提示

- 不要提交 `accounts.json`、`.env`、`data/`；`token_v2` 等于登录态。
- MCP 鉴权用独立的 32 字符以上密钥，`MCP_BRIDGE_HOSTS` 不要用通配符。
- 本仓库脚本里的公网地址都改成了环境变量（`MCP_PUBLIC_HOST`、`MCP_PUBLIC_URL`）。
