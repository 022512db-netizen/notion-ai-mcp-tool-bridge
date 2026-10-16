import json
import os

os.environ.setdefault("NOTION_ACCOUNTS", '[{"token_v2":"offline","space_id":"offline","user_id":"offline"}]')
os.environ.setdefault("API_KEY", "offline-api-key")
os.environ.setdefault("APP_MODE", "standard")

from unittest.mock import patch

from app.context_trimmer import (MESSAGE_CAP_TOKENS, TARGET_TOKENS, estimate_tokens,
                                 trim_messages)


def test_small_history_is_untouched():
    msgs = [{"role": "user", "content": "hi"}] * 5
    assert trim_messages(msgs) == msgs


def test_huge_single_message_is_clipped():
    huge = "x" * 400000
    result = trim_messages([{"role": "user", "content": huge}])
    assert len(result) == 1
    assert "[...中间省略" in result[0]["content"]
    assert estimate_tokens(result[0]["content"]) < TARGET_TOKENS


def test_systems_kept_and_newest_kept():
    msgs = [{"role": "system", "content": "sys"}] + [
        {"role": "user", "content": f"q{i}"} for i in range(40)
    ]
    for i, m in enumerate(msgs[1:]):
        m["content"] = m["content"] + "x" * 40000
    msgs.append({"role": "user", "content": "最后的问题"})
    result = trim_messages(msgs)
    assert result[0]["content"] == "sys"
    assert result[-1]["content"] == "最后的问题"
    assert any("已省略更早" in str(m.get("content")) for m in result)
    assert sum(estimate_tokens(str(m.get("content") or "")) for m in result) <= TARGET_TOKENS + 1000


def test_estimate_counts_cjk_per_character():
    assert estimate_tokens("你好") == 2
    assert estimate_tokens("hello") == 2
    assert estimate_tokens("你好hello世界") == 6


def test_realistic_large_conversation_shrinks_below_trigger():
    """A 62-message tool conversation like the failing ones must come out small."""
    msgs = [{"role": "system", "content": "system rules"}]
    for i in range(30):
        msgs.append({"role": "user", "content": f"task {i} " + "内容" * 400})
        msgs.append({"role": "assistant", "content": "y" * 3000})
    msgs.append({"role": "user", "content": "现在继续"})
    before = sum(estimate_tokens(str(m["content"])) for m in msgs)
    assert before > 45000, before  # this input is in the failing size class
    result = trim_messages(msgs)
    after = sum(estimate_tokens(str(m.get("content") or "")) for m in result)
    assert after <= TARGET_TOKENS + 1000, after
    assert result[-1]["content"] == "现在继续"


def test_bridge_trims_before_adding_directive():
    """The tool directive must be appended after trimming so it is never dropped."""
    import asyncio
    from unittest.mock import patch as _patch
    from fastapi import FastAPI, Request
    import httpx
    from app import tool_bridge as tb
    from app.tool_bridge import ClientToolBridge

    seen = {}

    class FakeScraper:
        def close(self):
            pass

    class FakeClient:
        space_id, user_id, space_view_id = "s", "u", "sv"
        token_v2, user_name, user_email, cookies = "t", "n", "e", {}

        def __init__(self, account=None):
            self._scraper = FakeScraper()

        def stream_response(self, transcript, thread_id=None):
            self.current_thread_id = "th"
            seen["transcript"] = transcript
            yield {"type": "content", "text": "ok"}

    class FakePool:
        def get_client(self, wait_if_cooling=True):
            return FakeClient()

    async def run():
        bridge = ClientToolBridge("offline-mcp-test-token-" + "x" * 32, ["test"])
        app = FastAPI()
        app.state.account_pool = FakePool()

        @app.post("/v1/chat/completions")
        async def chat(request: Request):
            return await bridge.handle(request, await request.json())

        body = {"model": "claude-haiku4.5", "stream": False,
                "messages": [{"role": "system", "content": "rules"}] + [
                    {"role": "user", "content": "x" * 60000} for _ in range(3)] +
                    [{"role": "user", "content": "current question"}],
                "tools": [{"type": "function", "function": {"name": "echo", "description": "d",
                           "parameters": {"type": "object", "properties": {}}}}]}
        with _patch.object(tb, "NotionOpusAPI", FakeClient), \
             _patch.object(tb, "attach_images", lambda *a, **k: None):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
                response = await asyncio.wait_for(http.post("/v1/chat/completions", json=body), 5)
                assert response.status_code == 200, response.text
        await bridge.shutdown()

    asyncio.run(run())
    users = [s for s in seen["transcript"] if s.get("type") == "user"]
    assert users, seen["transcript"]
    joined = json.dumps(seen["transcript"], ensure_ascii=False)
    assert "current question" in joined
    assert "request_id" in joined, "tool directive missing"
    assert estimate_tokens(joined) < 60000, estimate_tokens(joined)
