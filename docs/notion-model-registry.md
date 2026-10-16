# Notion 模型清单（桥接用）

## 为什么要这份表
Notion 只认它内部的模型编号（代号，如 `almond-croissant-low`）。
给一个不存在的编号时，Notion **不报错，会静默回退到默认模型**（当前是 `omniberry-sundae` / GPT-6.1 Sol），
所以"能跑通"不能证明编号正确，必须从官方客户端代码里取权威表。

## 权威表怎么来的（可复现）
1. 取首页 HTML 里的 `/_assets/app-*.js`，从中解析出分包映射（约 2500 条）：`([0-9]{4,6}):"([0-9a-f]{16})"`。
2. 并发拉取所有分包：
   `tr '\n' ' ' < urls.txt | xargs -n 120 curl -s --parallel --parallel-max 40 -w '\n@@@URL %{url_effective}\n' | grep -a -E '^@@@URL|notionName:"'`
   （注意：`--parallel` 下输出会交错，`@@@URL` 标记不可信，直接解析内容本身）
3. 分包里有一段模型注册表：`{notionName:"<编号>",lifecycle:"active",modelFamily:"anthropic",displayName:"...",pricing:{default:{inputRate,outputRate,cachedRate}},maxContextTokens}`。
   用花括号配对解析即可，共 200+ 条，含官方单价（美元/百万 token）。

## 判定某个编号是否真被接受（实测过）
桥接请求后，Notion 的 NDJSON 流里会回显实际使用的模型：`"model":"<编号>"`。
- 回显 == 你发的编号 → 接受
- 回显 == `omniberry-sundae` → 静默回退，说明编号无效

## 当前关键编号（2026-10-12 快照）
| 显示名 | Notion 编号 | 输入$/M | 输出$/M | 缓存读$/M |
|---|---|---|---|---|
| Claude Opus 5.5 | `albuquerque-quinn` | 4 | 20 | 0.2 |
| Claude Sonnet 5.5 | `achira-donut` | 2 | 10 | 0.2 |
| Claude Haiku 5.5 | `amla-jam` | 0.1 | 0.5 | 0.01 |
| Claude Opus 5 | `agave-flan` | 5 | 25 | 0.5 |
| Claude Sonnet 4.6 | `almond-croissant-low/high/...` | 3 | 15 | 0.3 |
| Claude Haiku 4.5 | `anthropic-haiku-4.5` | 1 | 5 | 0.1 |
| GPT-6.1 Sol | `omniberry-sundae` | 2 | 10 | 0.1 |
| GPT-6 Luna | `omodi` | 0.1 | 0.5 | 0.01 |
| Grok 4.7 | `grok-4.7` | 2 | 6 | 0.5 |

完整清单见同目录 `notion-model-registry-2026-10-12.json`。
