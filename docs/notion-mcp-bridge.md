# Notion 原生 MCP 客户端工具桥接

## 实现范围

Notion 使用当前账号的个人 Agent MCP 连接，按当前请求的工具定义发起真实 MCP 调用。桥接返回标准 Chat Completions `tool_calls`；调用端执行本机工具，再提交 `role=tool` 和 `tool_call_id`。桥接把结果交回挂起的 MCP 请求，同一次 Notion 推理继续回答，不另起一个模型请求猜测结果。

桥接不提供服务器 shell，不替客户端执行命令，不解析模型正文来伪造工具调用。客户端原有的工具审批和执行权限仍由客户端控制。

## 配置

1. 账号配置须包含个人 `space_view_id`，不是工作区 `space_id`。
2. 配置 `API_KEY`，另建至少 32 字符的独立 MCP 密钥文件，通过 `MCP_BRIDGE_TOKEN_FILE` 指定。
3. `MCP_BRIDGE_HOSTS` 必须包含真实 HTTP Host。通过隧道重写 Host 时允许该固定来源主机名，不使用全域通配符。
4. 将 HTTPS `/notion-mcp/` 注册为个人 MCP 连接，使用独立 Bearer 密钥，并启用该个人模块。模块需允许自动调用转发工具；实际执行仍在客户端。
5. 仅运行一个 worker。待回传调用保存在进程内，重启不能恢复，客户端会收到明确的过期或未知调用错误。

支持标准 `type=function` 工具。CPA 可以对客户端提供 Responses 转换；桥接本身只提供 `/v1/chat/completions`，不宣称原生实现 Responses。

无工具请求继续使用原有路径。`tool_choice=none` 不开启新工具会话。续传时重新验证本轮工具定义和选择策略；撤销的工具不能继续被调用。客户端若希望工具执行后正常回答，下一轮应使用 `auto` 或 `none`，不能继续强制 `required` 却期待无工具的最终回答。

## 隔离与边界

- 每个请求有随机能力 ID，每次调用有独立随机 `tool_call_id`，并绑定桥接调用凭据和模型。
- 只接受本轮声明的工具名及符合 JSON Schema 的参数；禁止验证器自动读取远程 `$ref`。
- 拒绝未知、过期、重复、跨会话结果；先验证整批结果再完成 Future。
- 同会话调用串行化，最多 8 个在途上游请求。工具目录不设 128 函数数量上限，保留 1 MiB 工具定义、64 KiB 参数、1 MiB 单次工具结果限制；不截断或丢弃目录中的工具。
- 当前空闲超时为 90 秒。真实验证覆盖了 12 秒的工具等待，不代表 Notion 上游支持任意时长任务。进程重启、客户端断开及上游限制仍可能终止一轮调用。
- 工具请求关闭内置网页搜索，并传空的 Notion 搜索范围。但这不是独立租户安全边界。共享同一 Notion 工作区和上游 API 密钥时，不应宣称已经隔离所有 Notion 内建能力或全部工作区数据。
- 没有脱离 Notion 服务端 Agent，也没有移除其默认指令、配额或使用政策，不是裸 Claude API 或无限额度。

## 2026-10-13 验证

**模型身份更正（23:10）：** 下列早期验证中的 Haiku 是客户端请求标签，不是最终上游模型。`build_standard_transcript` 已转换内部代号，`_to_notion_transcript` 又用只识别标准名称的映射器二次转换，静默退回 `almond-croissant-low`（Sonnet 4.6）。因此这些结果只证明工具链路和目录兼容性，不能作为 Haiku 的实际验证结果。模型路由修复与验收边界见下节。

- 原生 MCP 探针：Haiku 4.5 真实调用本地 echo/hash 工具，结果与服务端调用日志一致。
- 本地 Chat：标准 `tool_calls`，本机 `/bin/echo` 生成不可预测标记，等待 12 秒后回填，最终答案包含相同标记。
- CPA Chat：`notion/claude-haiku5.5`，上述往返成功，等待 12 秒后继续回答。
- CPA Responses：`function_call` 和 `function_call_output` 往返成功，流式首轮及非流式续传均通过。
- 最终部署复验：逐轮工具权限更新补丁上线后，再次使用 CPA Haiku 5.5 完成 Responses 往返，本机生成的随机标记与最终答案一致，耗时 15.62 秒。容器内 `server.py`、`tool_broker.py`、`tool_bridge.py` 的 SHA-256 与本地一致。
- 模型路由修复后共 28 项离线测试及 139 个子测试通过。覆盖 MCP SDK/HTTP、流式与非流式、300 函数目录及尾部工具调用、权限撤销、重放、超时、并发会话、Schema、无工具路径、鉴权及实际 HTTP 出站模型参数。
- 只使用 Haiku 测试，没有调用 Fable。未逐个验证其他模型、全部客户端或 custom/freeform 工具。

测试命令：

```sh
.venv/bin/python -m pytest -q tests
```

## 完整客户端工具目录修复

2026-10-13 晚用户真实客户端发送普通问候时被拒绝。失败请求经 CPA 展开后包含 503 个标准函数，紧凑 JSON 为 622,472 字节，函数名称均符合 64 字符限制，所有参数 Schema 校验通过。根因是桥接误把工具目录限制为 128 项；该目录作为 MCP 结果提供，并不是向模型 API 注册 503 个原生工具槽位。

- 移除数量上限，保留定义字节数、Schema、重复名称、鉴权和逐轮权限检查。
- 线上 Haiku 5.5 Responses 携带 300 函数，列表尾部工具真实执行并回填成功，32.21 秒完成。
- 从失败请求中只提取原样工具定义，以完整 503 函数重测普通回复，7.97 秒成功。没有重放用户正文、凭证或原来的 Opus 推理。
- 单工具探针不能代表完整客户端验收。上述测试仍不等于全部客户端工作流或所有工具都逐个验证过。
- 私有工具样本和验证证据位于已忽略的 `data/mcp-bridge/`，不能提交 Git。修复前源码备份为 `/opt/notion-mcp/pre-large-catalog-fix-20261013.tgz`。

## 模型路由修复

用户发现选择 Opus 5.5 后 Notion 显示 Sonnet 4.6。本地和运行容器均离线复现了上述二次转换；CPA/New API 审计确认上游收到 `claude-opus5.5`，错误发生在 notion2api 内部，不是客户端标签或渠道映射。

- `get_notion_model` 现在同时识别标准名称和已知内部代号，重复转换结果不变；未知名称报错，不再自动替换成默认模型。
- `notion_upstream_request` 现有日志补充 `upstream_model`，记录真正写入 HTTP 请求的代号，不记录凭证或正文。
- Standard、Lite、Heavy 三条构造路径的全部 27 个登记模型通过离线检查；另拦截实际 HTTP post，确认 Opus/Sonnet/Haiku 5.5 与 Fable 的出站值各自正确，没有发起网络推理。
- 新容器中已复核：Opus 5.5 为 `albuquerque-quinn`，Sonnet 5.5 为 `achira-donut`，Haiku 5.5 为 `amla-jam`；源文件 SHA-256 与本机一致。
- 本次没有重放用户历史或发出新的模型推理请求，因此不宣称 Opus/Fable 的真实回答和全部客户端工作流已经端到端验收。旧 Notion 会话不被修改，修复作用于新发出的请求。
- 回滚源码备份 `/opt/notion-mcp/pre-model-routing-fix-20261013.tgz` 包含旧错误，不能把回滚当成模型路由正确的保障。保留的 8899 旧服务未打本轮补丁，回退前也须先修正并核验出站模型。

## 当前部署与回退

服务器仅解包本机准备的源码和 Linux 预编译 wheel，不执行服务器构建、编译或 pip 安装。

- 新容器 `notion_mcp_service`，仅绑定 `127.0.0.1:8898`，512 MiB 内存、0.5 CPU 上限。
- 代码及预编译依赖：`/opt/notion-mcp/release`；凭据和连接记录：`/opt/notion-mcp/state`，不进入 Git。
- 原容器 `notion_ai_service` 仍在 `127.0.0.1:8899` 运行，保留回退。
- CPA 的 `notion` provider 保留原有 4 个模型别名及认证配置，只将上游地址改成 `http://127.0.0.1:8898/v1`。没有调整 Sam code 价格或新增重复模型。
- `notion-mcp-tunnel.service` 运行独立隧道，通过 `scripts/reconnect_mcp.py` 更新同一个个人 MCP 模块，不重复创建连接。当前使用自动维护的临时 Cloudflare 地址，不能承诺生产级可用性；具备条件时应替换为固定域名的命名隧道。
- `/health` 的 `client_tool_bridge.active_requests` 用于检查在途工具请求。更新桥接前应等到 0；不要重启 CPA、New API 或现有 Caddy 来部署该 Python 服务。
- 最终验收时新旧容器均健康，在途工具请求为 0。CPA、New API、Caddy 未重启；本地临时 uvicorn、cloudflared 和 SSH 反向隧道均已停止，线上运行不依赖这些测试进程。

回退应使用 CPA 管理 API `PATCH /v0/management/openai-compatibility`，只修改 `name=notion` 的 `value.base-url` 为 `http://127.0.0.1:8899/v1`。管理密钥不得写入命令行或日志。回读运行配置并检查重载日志后再停新服务。

注意：直接原子替换 CPA 配置文件时必须保留 `cliproxy:cliproxy` 属主和权限，否则运行用户无法读取或持久化配置。文件内容更新不等于运行配置生效。此次原配置备份为 `/opt/cli-proxy-api/config.yaml.bak-20261013-notion-mcp`。

最后一次更新前的桥接源码备份为 `/opt/notion-mcp/pre-final-update-20261013.tgz`。恢复代码前同样需要等待在途请求结束；该备份不包含账号和密钥。
