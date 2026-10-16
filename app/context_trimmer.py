"""Shrink oversized chat histories before they are sent to Notion.

Measured on the live bridge: turns with 62-72 messages (68-72k tokens) came back with
thinking only and no answer, while turns up to ~26k tokens answered normally. Once the
estimated size passes TRIGGER_TOKENS we keep the system prompts plus the newest messages
that fit TARGET_TOKENS, cut the middle out, and clip any single oversized message.
"""

import os
import re

from app.logger import logger

TRIGGER_TOKENS = int(os.getenv("NOTION_CONTEXT_TRIGGER_TOKENS", "45000"))
TARGET_TOKENS = int(os.getenv("NOTION_CONTEXT_TARGET_TOKENS", "30000"))
MESSAGE_CAP_TOKENS = int(os.getenv("NOTION_MESSAGE_CAP_TOKENS", "8000"))
# History beyond the newest message is kept only as a short recap: a long tail of old
# turns buries the current question, and the model then continues the old thread.
HISTORY_MESSAGE_CAP_TOKENS = int(os.getenv("NOTION_HISTORY_MESSAGE_CAP_TOKENS", "1500"))
MAX_HISTORY_MESSAGES = int(os.getenv("NOTION_MAX_HISTORY_MESSAGES", "24"))

_WIDE = re.compile(r"[\u2e80-\u9fff\uac00-\ud7af\uf900-\ufaff\uff00-\uffef]")


def estimate_tokens(text: str) -> int:
    """Rough count: one token per CJK character, one per four other characters."""
    if not text:
        return 0
    wide = len(_WIDE.findall(text))
    return wide + (len(text) - wide + 3) // 4


def _cost(message: dict) -> int:
    return estimate_tokens(str(message.get("content") or "")) + 4


def _clip(text: str, budget: int) -> str:
    """Keep the head and tail of text so it fits roughly within budget tokens."""
    if estimate_tokens(text) <= budget:
        return text
    ratio = budget / max(estimate_tokens(text), 1)
    keep = max(int(len(text) * ratio) - 40, 200)
    head, tail = keep * 2 // 3, keep // 3
    omitted = len(text) - head - tail
    return f"{text[:head]}\n\n[...中间省略约 {omitted} 个字符...]\n\n{text[-tail:]}"


def trim_messages(messages: list[dict]) -> list[dict]:
    """Return messages unchanged when small; otherwise a trimmed copy within TARGET_TOKENS."""
    total = sum(_cost(m) for m in messages)
    if total <= TRIGGER_TOKENS or not messages:
        return messages

    systems = [dict(m) for m in messages if m.get("role") == "system"]
    others = [dict(m) for m in messages if m.get("role") != "system"]
    for m in systems:
        m["content"] = _clip(str(m.get("content") or ""), MESSAGE_CAP_TOKENS)
    budget = TARGET_TOKENS - sum(_cost(m) for m in systems)

    kept: list[dict] = []
    for index, message in enumerate(reversed(others)):
        newest = index == 0
        # The newest turn keeps a generous share so it stays dominant; older turns are
        # reduced to a short recap and capped in number.
        if newest:
            cap = max(budget - 200, 2000)
        else:
            if len(kept) - 1 >= MAX_HISTORY_MESSAGES:
                break
            cap = min(HISTORY_MESSAGE_CAP_TOKENS, max(budget - 200, 200))
        message["content"] = _clip(str(message.get("content") or ""), cap)
        cost = _cost(message)
        if not newest and cost > budget:
            break
        kept.append(message)
        budget -= cost
        if budget <= 0:
            break
    kept.reverse()

    dropped = len(others) - len(kept)
    note = []
    if dropped:
        note = [{"role": "system", "content": f"[为控制上下文长度，已省略更早的 {dropped} 条消息。]"}]
    result = systems + note + kept
    logger.warning("Trimmed Notion context: %d -> %d estimated tokens, %d of %d messages dropped",
                   total, sum(_cost(m) for m in result), dropped, len(messages))
    return result
