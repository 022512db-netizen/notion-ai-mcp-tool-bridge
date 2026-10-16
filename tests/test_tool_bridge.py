"""Offline HTTP/MCP round trips. These tests never contact Notion."""

import asyncio
from copy import deepcopy
import json

from fastapi import FastAPI, Request
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from app.tool_bridge import ClientToolBridge


TOKEN = "offline-mcp-test-token-" + "x" * 32
TOOL = {"type": "function", "function": {"name": "echo", "description": "Echo a test value",
        "parameters": {"type": "object", "properties": {"value": {"type": "string"}},
                       "required": ["value"], "additionalProperties": False}}}


async def exercise(stream=False, tool_count=1):
    bridge = ClientToolBridge(TOKEN, ["test"])
    app = FastAPI()
    app.mount("/notion-mcp", bridge.asgi)
    app.state.account_pool = object()
    tasks = []
    tools = [deepcopy(TOOL) for _ in range(tool_count - 1)]
    for index, tool in enumerate(tools):
        tool["function"]["name"] = f"unused_{index}"
    tools.append(TOOL)

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        return await bridge.handle(request, await request.json())

    async def fake_notion(session):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                headers={"Authorization": "Bearer " + TOKEN}) as mcp_http:
            async with streamable_http_client("http://test/notion-mcp/", http_client=mcp_http) as (read, write, _):
                async with ClientSession(read, write) as mcp:
                    await mcp.initialize()
                    listed = await mcp.call_tool("list_client_tools", {"request_id": session.id})
                    assert not listed.isError
                    assert json.loads(listed.content[0].text)["tools"] == tools
                    result = await mcp.call_tool("call_client_tool", {
                        "request_id": session.id, "name": "echo", "arguments": {"value": "offline-value"}})
                    assert not result.isError
                    await session.events.put({"type": "content", "text": result.content[0].text})
                    await session.events.put({"type": "end"})

    def start(session, raw, pool):
        tasks.append(asyncio.create_task(fake_notion(session)))

    bridge._start = start
    async with bridge.mcp.session_manager.run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                headers={"Authorization": "Bearer offline-client-key"}) as http:
            unauthorized = await http.post("/notion-mcp/", json={})
            assert unauthorized.status_code == 401
            body = {"model": "claude-haiku4.5", "messages": [{"role": "user", "content": "Echo"}],
                    "tools": tools, "tool_choice": "required", "stream": stream}
            response = await asyncio.wait_for(http.post("/v1/chat/completions", json=body), 5)
            assert response.status_code == 200, response.text
            if stream:
                chunks = [json.loads(line[6:]) for line in response.text.splitlines()
                          if line.startswith("data: ") and line != "data: [DONE]"]
                calls = [chunk["choices"][0]["delta"]["tool_calls"][0]
                         for chunk in chunks if "tool_calls" in chunk["choices"][0]["delta"]]
                assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"
                call = dict(calls[0])
                call.pop("index")
                message = {"role": "assistant", "content": None, "tool_calls": [call]}
            else:
                result = response.json()
                assert result["choices"][0]["finish_reason"] == "tool_calls"
                message = result["choices"][0]["message"]
                call = message["tool_calls"][0]
            assert call["function"]["name"] == "echo"
            assert json.loads(call["function"]["arguments"]) == {"value": "offline-value"}
            body["messages"] += [message, {"role": "tool", "tool_call_id": call["id"], "content": "actual-local-output"}]
            body["tool_choice"] = "auto"
            wrong_owner = await http.post("/v1/chat/completions", json=body,
                                         headers={"Authorization": "Bearer different-client"})
            assert wrong_owner.status_code in (403, 404, 409, 410)
            resumed = await asyncio.wait_for(http.post("/v1/chat/completions", json=body), 5)
            assert resumed.status_code == 200, resumed.text
            if stream:
                assert "actual-local-output" in resumed.text
                assert '"finish_reason": "stop"' in resumed.text
            else:
                final = resumed.json()["choices"][0]
                assert final["message"]["content"] == "actual-local-output"
                assert final["finish_reason"] == "stop"
            replay = await http.post("/v1/chat/completions", json=body)
            assert replay.status_code in (404, 409, 410)
        await asyncio.gather(*tasks)
        await bridge.shutdown()


def test_nonstream_mcp_round_trip():
    asyncio.run(exercise())


def test_stream_mcp_round_trip():
    asyncio.run(exercise(stream=True))


def test_large_catalog_mcp_round_trip():
    for stream in (False, True):
        asyncio.run(exercise(stream=stream, tool_count=300))


def test_legacy_routing_and_auth_without_bridge(monkeypatch):
    monkeypatch.setenv("NOTION_ACCOUNTS", json.dumps([{"token_v2": "offline", "space_id": "offline", "user_id": "offline"}]))
    monkeypatch.setenv("API_KEY", "offline-api-key")
    monkeypatch.setenv("APP_MODE", "standard")
    monkeypatch.delenv("MCP_BRIDGE_TOKEN_FILE", raising=False)
    from fastapi.testclient import TestClient
    from fastapi.responses import JSONResponse
    from app import server
    from app.api import chat

    async def legacy(request, req_body, response, raw_messages=None):
        return JSONResponse({"legacy": True})

    monkeypatch.setattr(chat, "_handle_standard_request", legacy)
    with TestClient(server.app) as http:
        body = {"model": "claude-haiku4.5", "messages": [{"role": "user", "content": "hello"}]}
        assert http.post("/v1/chat/completions", json=body).status_code == 401
        headers = {"Authorization": "Bearer offline-api-key"}
        assert http.post("/v1/chat/completions", json=body, headers=headers).json() == {"legacy": True}
        body["tools"] = [TOOL]
        assert http.post("/v1/chat/completions", json=body, headers=headers).status_code == 503
        body["tool_choice"] = "none"
        assert http.post("/v1/chat/completions", json=body, headers=headers).json() == {"legacy": True}


def test_catalog_inlined_only_when_small():
    from app.tool_bridge import catalog_listing
    small = catalog_listing([TOOL])
    assert '"name":"echo"' in small and "Do not call list_client_tools" in small
    large = catalog_listing([deepcopy(TOOL) for _ in range(300)])
    assert large.startswith("Call list_client_tools")


def test_heartbeat_comment_when_notion_is_silent(monkeypatch):
    import app.tool_bridge as tb
    monkeypatch.setattr(tb, "HEARTBEAT_SECONDS", 1)

    async def run():
        bridge = ClientToolBridge(TOKEN, ["test"])
        app = FastAPI()
        app.state.account_pool = object()

        async def late(session):
            await asyncio.sleep(2.6)
            await session.events.put({"type": "content", "text": "done"})
            await session.events.put({"type": "end"})

        bridge._start = lambda session, raw, pool: asyncio.create_task(late(session))

        @app.post("/v1/chat/completions")
        async def chat(request: Request):
            return await bridge.handle(request, await request.json())

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                headers={"Authorization": "Bearer k"}) as http:
            body = {"model": "claude-haiku4.5", "messages": [{"role": "user", "content": "hi"}],
                    "tools": [TOOL], "stream": True}
            response = await asyncio.wait_for(http.post("/v1/chat/completions", json=body), 10)
            assert response.status_code == 200, response.text
            assert response.text.count(": ping") >= 1
            assert '"content": "done"' in response.text
        await bridge.shutdown()

    asyncio.run(run())


def test_redact_tool_history_strips_old_arguments_but_keeps_latest():
    import os
    os.environ.setdefault("NOTION_ACCOUNTS", '[{"token_v2":"x","space_id":"x","user_id":"x"}]')
    os.environ.setdefault("API_KEY", "x")
    os.environ.setdefault("APP_MODE", "standard")
    from app.tool_bridge import redact_tool_history
    big = "x" * 5000
    messages = [
        {"role": "user", "content": "draw a pelican"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "old", "type": "function", "function": {"name": "write", "arguments": big}}]},
        {"role": "tool", "tool_call_id": "old", "content": "wrote it: " + big},
        {"role": "user", "content": "draw a crocodile instead"},
    ]
    out = redact_tool_history(messages)
    assert out[1]["tool_calls"][0]["function"]["arguments"] == "{}"
    assert out[1]["tool_calls"][0]["function"]["name"] == "write"
    assert "truncated" in out[2]["content"]
    assert len(out[2]["content"]) < len(messages[2]["content"])
    assert out[3] == messages[3]
    # originals untouched
    assert messages[1]["tool_calls"][0]["function"]["arguments"] == big


def test_redact_tool_history_keeps_live_resume_call():
    import os
    os.environ.setdefault("NOTION_ACCOUNTS", '[{"token_v2":"x","space_id":"x","user_id":"x"}]')
    os.environ.setdefault("API_KEY", "x")
    os.environ.setdefault("APP_MODE", "standard")
    from app.tool_bridge import redact_tool_history
    args = '{"a":1}'
    messages = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "stale", "type": "function", "function": {"name": "write", "arguments": "y" * 9000}}]},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "live", "type": "function", "function": {"name": "echo", "arguments": args}}]},
        {"role": "tool", "tool_call_id": "live", "content": "ok"},
    ]
    out = redact_tool_history(messages)
    assert out[1]["tool_calls"][0]["function"]["arguments"] == "{}"
    assert out[2]["tool_calls"][0]["function"]["arguments"] == args


def test_reasoning_effort_is_not_forwarded():
    """The client's thinking-depth hint must never reach Notion's config."""
    import os
    os.environ.setdefault("NOTION_ACCOUNTS", '[{"token_v2":"x","space_id":"x","user_id":"x"}]')
    os.environ.setdefault("API_KEY", "x")
    os.environ.setdefault("APP_MODE", "standard")
    from app.conversation import build_standard_transcript
    acc = {"user_id": "u", "space_id": "s"}
    msgs = [{"role": "user", "content": "hi"}]
    t = build_standard_transcript(msgs, "claude-haiku5.5", acc)
    assert "reasoningEffort" not in t[0]["value"]
    assert "reasoning_effort" not in t[0]["value"]


def test_call_tool_timeout_and_retry_pickup():
    import asyncio
    import os
    os.environ.setdefault("NOTION_ACCOUNTS", '[{"token_v2":"x","space_id":"x","user_id":"x"}]')
    os.environ.setdefault("API_KEY", "x")
    os.environ.setdefault("APP_MODE", "standard")
    from unittest.mock import patch
    from app import tool_broker as tb

    async def run():
        broker = tb.ToolBroker(ttl_seconds=300)
        session = broker.create("owner", "m", [TOOL])
        with patch.object(tb, "CALL_TOOL_TIMEOUT_SECONDS", 0.05):
            # first call times out, client still "executing"
            r1 = await broker.call_tool(session.id, "echo", {"value": "v"})
            assert "still executing" in r1
            assert len(session.pending) == 1
            # retry with same args waits on the same future, no duplicate event
            events_before = session.events.qsize()
            task = asyncio.create_task(broker.call_tool(session.id, "echo", {"value": "v"}))
            await asyncio.sleep(0.02)
            assert session.events.qsize() == events_before
            # client result arrives, retry gets it
            call_id = next(iter(session.pending))
            session.pending[call_id].set_result("done-value")
            assert await task == "done-value"
        await broker.shutdown()

    asyncio.run(run())


def test_stream_end_classification():
    from app.tool_bridge import classify_stream_end

    # Client disconnected first: not our business.
    assert classify_stream_end(stopped=True, idle_seconds=0, substantive=False, pending_calls=2) is None
    # A delivered answer stays a normal end, so working turns are never turned into errors.
    assert classify_stream_end(stopped=False, idle_seconds=600, substantive=True, pending_calls=0) is None
    assert classify_stream_end(stopped=False, idle_seconds=1, substantive=True, pending_calls=0) is None
    # Nothing to show and long silence: the upstream gave up.
    assert "silent" in classify_stream_end(stopped=False, idle_seconds=111, substantive=False, pending_calls=0)
    # Nothing to show at all.
    assert classify_stream_end(stopped=False, idle_seconds=3, substantive=False, pending_calls=0)
    # Tool calls we can never answer.
    reason = classify_stream_end(stopped=False, idle_seconds=3, substantive=True, pending_calls=1)
    assert "waiting for a result" in reason


def test_aborted_turn_reports_error_instead_of_silent_end():
    """Upstream closing with no answer must reach the client as an error, not 'stop'."""
    from app.tool_bridge import ClientToolBridge
    from app.tool_broker import ToolBroker

    async def run():
        bridge = ClientToolBridge(TOKEN, ["test"])
        app = FastAPI()
        app.mount("/notion-mcp", bridge.asgi)
        app.state.account_pool = object()

        @app.post("/v1/chat/completions")
        async def chat(request: Request):
            return await bridge.handle(request, await request.json())

        def start(session, raw, pool):
            async def die():
                await session.events.put({"type": "thinking", "text": "hmm"})
                await session.events.put({"type": "error", "message": "Notion 上游提前结束（stream went silent for 111s and closed with no answer），该任务可能仍在 Notion 侧继续，请重试。", "status_code": 410})
            asyncio.create_task(die())

        bridge._start = start
        async with bridge.mcp.session_manager.run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                    headers={"Authorization": "Bearer offline-client-key"}) as http:
                body = {"model": "claude-haiku4.5", "messages": [{"role": "user", "content": "go"}],
                        "tools": [deepcopy(TOOL)], "stream": True}
                response = await asyncio.wait_for(http.post("/v1/chat/completions", json=body), 5)
                assert response.status_code == 200
                payloads = [json.loads(line[6:]) for line in response.text.splitlines()
                            if line.startswith("data: ") and line != "data: [DONE]"]
                errors = [p for p in payloads if "error" in p]
                assert errors, response.text
                assert "提前结束" in errors[0]["error"]["message"]
                assert all(p["choices"][0]["finish_reason"] != "stop" for p in payloads if "choices" in p)
        await bridge.shutdown()

    asyncio.run(run())


def _run_start_with(fake_stream):
    """Drive the real _start() with a fake Notion client and return the client-visible SSE."""
    from unittest.mock import patch
    from app import tool_bridge as tb

    class FakeScraper:
        def close(self):
            pass

    class FakeClient:
        space_id = "s"
        user_id = "u"
        space_view_id = "sv"
        token_v2 = "t"
        user_name = "n"
        user_email = "e"
        cookies = {}

        def __init__(self, account=None):
            self._scraper = FakeScraper()

        def stream_response(self, transcript, thread_id=None):
            yield from fake_stream(self)

    class FakePool:
        def get_client(self, wait_if_cooling=True):
            return FakeClient()

    async def run():
        bridge = ClientToolBridge(TOKEN, ["test"])
        app = FastAPI()
        app.mount("/notion-mcp", bridge.asgi)
        app.state.account_pool = FakePool()

        @app.post("/v1/chat/completions")
        async def chat(request: Request):
            return await bridge.handle(request, await request.json())

        with patch.object(tb, "NotionOpusAPI", FakeClient), \
             patch.object(tb, "attach_images", lambda *a, **k: None):
            async with bridge.mcp.session_manager.run():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                        headers={"Authorization": "Bearer offline-client-key"}) as http:
                    body = {"model": "claude-haiku4.5", "messages": [{"role": "user", "content": "go"}],
                            "tools": [deepcopy(TOOL)], "stream": True}
                    return await asyncio.wait_for(http.post("/v1/chat/completions", json=body), 5)

    return asyncio.run(run())


def _sse_payloads(response):
    return [json.loads(l[6:]) for l in response.text.splitlines()
            if l.startswith("data: ") and l != "data: [DONE]"]


def test_start_reports_error_when_upstream_closes_with_no_answer():
    def silent_then_close(client):
        yield {"type": "thinking", "text": "hmm"}

    response = _run_start_with(silent_then_close)
    payloads = _sse_payloads(response)
    errors = [p for p in payloads if "error" in p]
    assert errors, response.text
    assert "提前结束" in errors[0]["error"]["message"]
    assert all(p["choices"][0]["finish_reason"] != "stop" for p in payloads if "choices" in p)


def test_start_keeps_normal_end_when_answer_delivered():
    def answer_then_close(client):
        yield {"type": "content", "text": "here is the answer"}

    response = _run_start_with(answer_then_close)
    payloads = _sse_payloads(response)
    assert not [p for p in payloads if "error" in p], response.text
    assert any(p["choices"][0].get("finish_reason") == "stop" for p in payloads)
    assert "here is the answer" in response.text


def test_conversation_keeps_one_notion_thread():
    """Two turns of the same conversation must reuse one Notion thread."""
    from unittest.mock import patch
    from app import tool_bridge as tb

    seen = []
    calls = {"n": 0}

    class FakeScraper:
        def close(self):
            pass

    class FakeClient:
        space_id, user_id, space_view_id = "s", "u", "sv"
        token_v2, user_name, user_email, cookies = "t", "n", "e", {}

        def __init__(self, account=None):
            self._scraper = FakeScraper()

        def stream_response(self, transcript, thread_id=None):
            calls["n"] += 1
            self.current_thread_id = thread_id or f"generated-thread-{calls['n']}"
            seen.append(self.current_thread_id)
            yield {"type": "content", "text": "ok"}

    class FakePool:
        def get_client(self, wait_if_cooling=True):
            return FakeClient()

    async def run():
        bridge = ClientToolBridge(TOKEN, ["test"])
        app = FastAPI()
        app.mount("/notion-mcp", bridge.asgi)
        app.state.account_pool = FakePool()

        @app.post("/v1/chat/completions")
        async def chat(request: Request):
            return await bridge.handle(request, await request.json())

        with patch.object(tb, "NotionOpusAPI", FakeClient), \
             patch.object(tb, "attach_images", lambda *a, **k: None):
            async with bridge.mcp.session_manager.run():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                        headers={"Authorization": "Bearer offline-client-key"}) as http:
                    for _ in range(2):
                        body = {"model": "claude-haiku4.5",
                                "messages": [{"role": "user", "content": "same opening question"}],
                                "tools": [deepcopy(TOOL)], "stream": False}
                        response = await asyncio.wait_for(http.post("/v1/chat/completions", json=body), 5)
                        assert response.status_code == 200, response.text
                        await asyncio.sleep(0)
        await bridge.shutdown()

    asyncio.run(run())
    assert len(seen) == 2
    assert seen[1] == seen[0], f"second turn opened a new thread: {seen}"


def test_different_conversations_use_different_threads():
    from app.tool_bridge import ClientToolBridge
    bridge = ClientToolBridge(TOKEN, ["test"])
    a = bridge._conversation_key("owner", "acct", "m", [{"role": "user", "content": "task A"}])
    b = bridge._conversation_key("owner", "acct", "m", [{"role": "user", "content": "task B"}])
    b2 = bridge._conversation_key("owner", "acct", "m", [{"role": "user", "content": "task B"},
                                                        {"role": "assistant", "content": "x"}])
    assert a != b
    assert b == b2, "same conversation must keep the same key as history grows"


def test_tool_result_followed_by_system_message_still_resumes():
    """Claude Code appends a system reminder after tool results; that must still resume."""
    asyncio.run(_resume_with_trailing_system())


async def _resume_with_trailing_system():
    bridge = ClientToolBridge(TOKEN, ["test"])
    app = FastAPI()
    app.mount("/notion-mcp", bridge.asgi)
    app.state.account_pool = object()
    started = []

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        return await bridge.handle(request, await request.json())

    async def fake_notion(session):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                headers={"Authorization": "Bearer " + TOKEN}) as mcp_http:
            async with streamable_http_client("http://test/notion-mcp/", http_client=mcp_http) as (read, write, _):
                async with ClientSession(read, write) as mcp:
                    await mcp.initialize()
                    result = await mcp.call_tool("call_client_tool", {
                        "request_id": session.id, "name": "echo", "arguments": {"value": "v"}})
                    assert not result.isError
                    await session.events.put({"type": "content", "text": "after-tools " + result.content[0].text})
                    await session.events.put({"type": "end"})

    def start(session, raw, pool):
        started.append(session.id)
        asyncio.create_task(fake_notion(session))

    bridge._start = start
    async with bridge.mcp.session_manager.run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                headers={"Authorization": "Bearer offline-client-key"}) as http:
            body = {"model": "claude-haiku4.5", "messages": [{"role": "user", "content": "go"}],
                    "tools": [deepcopy(TOOL)], "stream": False}
            first = await asyncio.wait_for(http.post("/v1/chat/completions", json=body), 5)
            call = first.json()["choices"][0]["message"]["tool_calls"][0]
            # The tool result is followed by a system reminder, exactly as Claude Code sends it.
            body["messages"] += [
                {"role": "assistant", "content": None, "tool_calls": [call]},
                {"role": "tool", "tool_call_id": call["id"], "content": "local-output"},
                {"role": "system", "content": "<system-reminder>attribution rules</system-reminder>"},
            ]
            second = await asyncio.wait_for(http.post("/v1/chat/completions", json=body), 5)
            assert second.status_code == 200, second.text
            assert len(started) == 1, f"a second session was started: {started}"
            assert "local-output" in second.text and "after-tools" in second.text
    await bridge.shutdown()
