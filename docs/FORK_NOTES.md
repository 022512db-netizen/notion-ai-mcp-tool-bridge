# 本仓库改动说明（自用 fork）

- 上游：<https://github.com/maverickxone/notion2api>，本仓库基于上游 commit `5882102`。
- 用途：把 Notion AI 包装成 OpenAI 兼容接口，再挂到 CPA（CLI Proxy API）上给内部使用。

## 1. 客户端改用能伪装浏览器 TLS 指纹的 HTTP 库（关键修复）

- **现象**：经桥接调用时，Notion 返回 `Something went wrong. Please try again later.`（`temporarily-unavailable`），
  或返回 HTTP 200 但流为空，表现为「上游 502」。账号正常、模型编号正确时也会出现。
- **原因**：Notion 会按 **TLS 指纹** 区分真浏览器和脚本客户端。上游用的 `requests` / `cloudscraper`
  虽然能过 Cloudflare 挑战，但无法伪装 Chrome 的 TLS 指纹，于是被上游静默拒绝。
- **处理**：`app/notion_client.py` 全面换成 `curl_cffi`，并指定 `impersonate="chrome"`；
  另加了一个 `_CffiLineShim`，让 `parse_stream` 继续用 `iter_lines(decode_unicode=True)`（curl_cffi 不支持该参数）。
- **注意**：指纹要对得上，`chrome131` 这类旧指纹同样会被拒；`chrome`、`safari18_0` 实测可用。
- `NOTION_CLIENT_VERSION` 已同步到当前线上版本（Notion 前端升级后可能需要再跟）。

## 2. 模型清单更新

- 上游清单停在 Opus 4.6/4.7/4.8/5、Sonnet 4.6/5、Haiku 4.5，缺 5.5 一代。
- 新增（均已实测可用）：`claude-opus5.5` → `albuquerque-quinn`、`claude-sonnet5.5` → `achira-donut`、
  `claude-haiku5.5` → `amla-jam`、`gpt-6.1-sol` → `omniberry-sundae`。
- 刷新方法与权威快照见 [`docs/notion-model-registry.md`](docs/notion-model-registry.md)，
  刷新脚本：`scripts/refresh_notion_model_registry.py`。

## 3. 部署调整

- `docker-compose.yml`：端口只绑 `127.0.0.1`（上游默认 `0.0.0.0`）。
- `requirements.txt`：新增 `curl_cffi`。

## 重要提醒

- **Notion 对不存在的模型编号不报错，会静默回退到默认模型**（当前 `omniberry-sundae`）。
  改完清单务必用 `docs/notion-model-registry.md` 里的「回显校验法」确认编号真的生效。
- `.env`、`accounts.json` 含密钥与登录态，已在 `.gitignore` 中，不要提交。
- 上游未提供 LICENSE，本仓库默认按私有仓库处理；若要公开，请先确认授权。
