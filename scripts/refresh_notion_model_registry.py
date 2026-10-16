#!/usr/bin/env python3
"""刷新 Notion 模型注册表（编号 / 显示名 / 官方单价）。

原理：Notion 前端把模型注册表打进分包里，结构形如
    {notionName:"...", lifecycle:"active", modelFamily:"anthropic",
     displayName:"...", pricing:{default:{inputRate,outputRate,cachedRate}}, maxContextTokens:...}
本脚本从 /_assets/app-*.js 解析分包清单，并发拉取全部分包，抽出注册表对象并去重输出。

用法：
    python3 scripts/refresh_notion_model_registry.py --out docs/notion-model-registry.json
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.request

BASE = "https://app.notion.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")


def fetch(path: str) -> str:
    req = urllib.request.Request(BASE + path, headers={"user-agent": UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", "replace")


def chunk_urls() -> list[str]:
    html = fetch("/ai")
    app_js = re.search(r'src="(/_assets/app-[^"]+\.js)"', html)
    if not app_js:
        raise SystemExit("没找到 app-*.js，Notion 前端结构可能变了")
    src = fetch(app_js.group(1))
    pairs = re.findall(r'([0-9]{4,6}):"([0-9a-f]{16})"', src)
    if len(pairs) < 100:
        raise SystemExit(f"分包映射只解析到 {len(pairs)} 条，结构可能变了")
    return [f"{BASE}/_assets/{cid}-{digest}.js" for cid, digest in pairs]


def scan(urls: list[str], batch: int = 120) -> str:
    """用 curl --parallel 批量拉取（带 URL 标记），返回合并文本。"""
    out = []
    for i in range(0, len(urls), batch):
        proc = subprocess.run(
            ["curl", "-s", "--parallel", "--parallel-max", "40", "--max-time", "30",
             "-w", "\n@@@URL %{url_effective}\n", *urls[i:i + batch]],
            capture_output=True, text=True,
        )
        out.append(proc.stdout)
        print(f"  已扫描 {min(i + batch, len(urls))}/{len(urls)}", file=sys.stderr)
    return "\n".join(out)


def parse_registry(text: str) -> list[dict]:
    rows: dict[str, dict] = {}
    for match in re.finditer(r'notionName:"', text):
        start = text.rfind("{", 0, match.start())
        if start < 0:
            continue
        depth, end = 0, start
        while end < len(text):
            if text[end] == "{":
                depth += 1
            elif text[end] == "}":
                depth -= 1
                if depth == 0:
                    break
            end += 1
        frag = text[start:end + 1]

        def grab(pattern: str, cast=str):
            m = re.search(pattern, frag)
            if not m:
                return None
            try:
                return cast(m.group(1))
            except Exception:
                return m.group(1)

        name = grab(r'notionName:"([^"]+)"')
        if not name:
            continue
        rows[name] = {
            "notionName": name,
            "lifecycle": grab(r'lifecycle:"([^"]+)"'),
            "modelFamily": grab(r'modelFamily:"([^"]+)"'),
            "displayName": grab(r'displayName:"([^"]+)"'),
            "maxContextTokens": grab(r'maxContextTokens:([\d.e]+)'),
            "inputRateUSD": grab(r'inputRate:([\d.]+)', float),
            "outputRateUSD": grab(r'outputRate:([\d.]+)', float),
            "cachedRateUSD": grab(r'cachedRate:([\d.]+)', float),
        }
    return sorted(rows.values(), key=lambda r: (r["modelFamily"] or "", r["notionName"]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="docs/notion-model-registry.json")
    ap.add_argument("--show", default="", help="只打印显示名匹配该正则的条目")
    args = ap.parse_args()

    urls = chunk_urls()
    print(f"分包数：{len(urls)}", file=sys.stderr)
    rows = parse_registry(scan(urls))
    print(f"解析到模型条目：{len(rows)}", file=sys.stderr)

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=1)
    print(f"已写入 {args.out}", file=sys.stderr)

    pattern = args.show
    for r in rows:
        if r["lifecycle"] != "active":
            continue
        if pattern and not re.search(pattern, r["displayName"] or "", re.I):
            continue
        print(f'  {r["notionName"]:<34} {str(r["displayName"]):<26} '
              f'in=${r["inputRateUSD"]}/M out=${r["outputRateUSD"]}/M cached=${r["cachedRateUSD"]}/M')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
