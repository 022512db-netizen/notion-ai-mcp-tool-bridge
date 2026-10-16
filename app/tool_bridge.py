"""Native Notion MCP calls translated into client-executed Chat tool calls."""

import asyncio
import hashlib
import hmac
import json
import os
import threading
import time
import uuid

from fastapi.responses import JSONResponse, StreamingResponse
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from app.conversation import build_standard_transcript
from app.context_trimmer import trim_messages
from app.image_uploader import attach_images
from app.logger import logger
from app.model_registry import is_supported_model, get_thread_type
from app.notion_client import NotionOpusAPI
from app.schemas import ChatMessage
from app.tool_broker import BridgeError, ToolBroker


class BearerOnly:
    def __init__(self, app, token):
        self.app = app
        self.expected = ("Bearer " + token).encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            supplied = dict(scope.get("headers", [])).get(b"authorization", b"")
            if not hmac.compare_digest(supplied, self.expected):
                return await JSONResponse(
                    {"error": "Unauthorized"}, status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )(scope, receive, send)
        await self.app(scope, receive, send)


INLINE_CATALOG_LIMIT = int(os.getenv("MCP_INLINE_CATALOG_LIMIT", "6000"))
# Seconds of Notion silence before an SSE comment keeps the client connection alive.
HEARTBEAT_SECONDS = int(os.getenv("MCP_STREAM_HEARTBEAT_SECONDS", "8"))
# After this much silence, send a visible reasoning notice so proxies see real output.
THINKING_NOTICE_SECONDS = int(os.getenv("MCP_THINKING_NOTICE_SECONDS", "55"))
# Silence this long before the upstream stream closes means Notion gave up on the
# connection rather than finishing the turn.
SILENT_END_SECONDS = int(os.getenv("MCP_SILENT_END_SECONDS", "90"))
# One client conversation keeps one Notion thread, instead of a new thread per turn.
THREAD_TTL_SECONDS = int(os.getenv("MCP_THREAD_TTL_SECONDS", "21600"))
MAX_CONVERSATION_THREADS = int(os.getenv("MCP_MAX_CONVERSATION_THREADS", "200"))


def session_tag(session) -> str:
    """Log-safe session label: the random part after the fixed 'session_' prefix."""
    raw = session.id
    return raw.split("_", 1)[-1][:8]


def classify_stream_end(*, stopped: bool, idle_seconds: float, substantive: bool,
                        pending_calls: int) -> str | None:
    """Describe an abnormal upstream stream end, or return None for a normal one.

    Two closes are not really "the turn finished":
      * tool calls the client asked for were never answered, so Notion is left waiting;
      * nothing was delivered to the client at all.
    A long silence before a close with no answer is reported as such rather than as a
    clean finish. When an answer was already delivered it stays a normal end, so working
    turns are not turned into errors.
    """
    if stopped:
        return None
    if pending_calls:
        return f"{pending_calls} client tool call(s) were still waiting for a result"
    if not substantive:
        if idle_seconds >= SILENT_END_SECONDS:
            return f"the stream went silent for {idle_seconds:.0f}s and closed with no answer"
        return "the turn ended without producing any answer content"
    return None



RETRY_NUDGE = ("The client tool results above are complete. Write the final answer to the user's "
               "latest request now, using those results. Do not stop without an answer.")


def catalog_listing(tools):
    catalog = json.dumps(tools, ensure_ascii=False, separators=(",", ":"))
    if len(catalog) <= INLINE_CATALOG_LIMIT:
        # Small catalogs ride in the prompt so the agent can skip the list_client_tools round trip.
        return ("The available client tools and their JSON schemas are: " + catalog + ". "
                "Do not call list_client_tools; call call_client_tool directly. ")
    return "Call list_client_tools with that request_id to obtain names and JSON schemas. "


def redact_tool_history(messages):
    """Strip stale tool payloads/history so the model cannot copy them.

    Every request rebuilds a fresh Notion thread with the full client history.
    Old assistant tool_calls carry their full arguments (e.g. a whole HTML file),
    and a lazy model will re-emit them instead of generating new content for the
    latest user turn. Calls echoed by trailing tool results (the live resume turn)
    keep their arguments; all other tool_calls keep names but lose arguments, and
    old tool results are truncated. Returns a new list; the original is untouched.
    """
    trimmed = []
    # Call IDs echoed back by trailing tool results identify the live turn being
    # resumed; their arguments must survive. Everything older is just history.
    live_ids = set()
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "tool":
            break
        call_id = message.get("tool_call_id")
        if isinstance(call_id, str) and call_id:
            live_ids.add(call_id)
    for message in messages:
        if not isinstance(message, dict):
            trimmed.append(message)
            continue
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            calls = []
            for call in message["tool_calls"]:
                if not isinstance(call, dict):
                    continue
                if call.get("id") in live_ids:
                    calls.append(call)
                    continue
                fn = dict((call.get("function") or {}))
                fn["arguments"] = "{}"
                calls.append({**call, "function": fn})
            message = {**message, "tool_calls": calls}
        elif role == "tool":
            content = message.get("content")
            if isinstance(content, str) and len(content) > 2000:
                message = {**message, "content": content[:2000] + "\n[...older tool result truncated...]"}
        trimmed.append(message)
    return trimmed


class ClientToolBridge:
    def __init__(self, token, allowed_hosts):
        if len(token) < 32:
            raise ValueError("MCP bridge token must contain at least 32 characters")
        self.broker = ToolBroker(ttl_seconds=float(os.getenv("MCP_SESSION_TTL_SECONDS", "300")), max_sessions=8)
        self.jobs = {}
        self.consumers = set()
        # conversation key -> (last used, notion thread id); touch from the event loop only
        self.threads = {}
        self.mcp = FastMCP(
            "Sam Local Tools", streamable_http_path="/", stateless_http=True,
            json_response=True,
            instructions=("Client tools are scoped to a request_id supplied in the current request. "
                          "List its tools, then call them using that same request_id. "
                          "Never invent a request_id or report execution without a tool result."),
            transport_security=TransportSecuritySettings(
                allowed_hosts=allowed_hosts, allowed_origins=[],
            ),
        )

        @self.mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
        async def list_client_tools(request_id: str) -> dict:
            """Get the exact tools and argument schemas of the active client request."""
            session = self.broker.get(request_id)
            return {"tools": session.tools}

        @self.mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
        async def call_client_tool(request_id: str, name: str, arguments: dict) -> str:
            """Ask the client to execute one of its tools and await its actual result.

            Tools can read or modify local resources; execution and approval belong to
            the client. Only the names and schemas from list_client_tools are accepted.
            """
            return await self.broker.call_tool(request_id, name, arguments)

        self.asgi = BearerOnly(self.mcp.streamable_http_app(), token)

    async def shutdown(self):
        await self.broker.shutdown()
        for stop, _ in list(self.jobs.values()):
            stop.set()
        if self.jobs:
            await asyncio.gather(*(job for _, job in list(self.jobs.values())), return_exceptions=True)

    @staticmethod
    def _error(message, status=400):
        return JSONResponse(status_code=status, content={"error": {
            "message": message, "type": "tool_bridge_error", "code": "tool_bridge_error",
        }})

    @staticmethod
    def _conversation_key(owner: str, account_key: str, model: str, messages: list) -> str:
        """Stable per-conversation key: opening user message + model + account.

        The client resends the whole history every turn, so its first user message
        identifies the conversation without needing an id from the caller.
        """
        opening = next((str(m.get("content") or "") for m in messages
                        if isinstance(m, dict) and m.get("role") == "user"), "")
        digest = hashlib.sha256(opening.encode("utf-8")).hexdigest()[:16]
        return f"{owner[:16]}|{account_key}|{model}|{digest}"

    def _thread_for(self, key: str):
        entry = self.threads.get(key)
        if entry is None:
            return None
        stamp, thread_id = entry
        if time.time() - stamp > THREAD_TTL_SECONDS:
            self.threads.pop(key, None)
            return None
        return thread_id

    def _remember_thread(self, key: str, thread_id) -> None:
        if not key or not thread_id:
            return
        self.threads[key] = (time.time(), thread_id)
        while len(self.threads) > MAX_CONVERSATION_THREADS:
            oldest = min(self.threads, key=lambda item: self.threads[item][0])
            self.threads.pop(oldest, None)

    def _start(self, session, raw, pool):
        original = pool.get_client(wait_if_cooling=False)
        if not original.space_view_id:
            raise ValueError("Notion account requires its personal space_view_id for MCP")
        account = {
            "token_v2": original.token_v2, "space_id": original.space_id,
            "user_id": original.user_id, "space_view_id": original.space_view_id,
            "user_name": original.user_name, "user_email": original.user_email,
            "cookies": dict(original.cookies),
        }
        client = NotionOpusAPI(account)
        # Trim before the tool directive is appended so that directive always survives.
        messages = trim_messages([ChatMessage.model_validate(m).model_dump()
                                  for m in redact_tool_history(raw["messages"])])
        conversation_key = self._conversation_key(
            session.owner, original.user_id, raw["model"], messages)
        existing_thread = self._thread_for(conversation_key)
        if existing_thread:
            logger.info("Reusing Notion thread session=%s conversation=%s thread=%s",
                        session_tag(session), conversation_key[-16:], existing_thread[:8])
        listing = catalog_listing(session.tools)
        directive = (
            "The current API client provides external tools through Sam Local Tools MCP. "
            f"The request_id for this request only is {session.id}. "
            + listing +
            "Use call_client_tool with that request_id, name and arguments to execute them. "
            "Use these client tools for local files and commands, not the Notion sandbox. "
            "Wait for the actual returned result. Never simulate a tool result or emit "
            "tool-call JSON as answer text. Only use this request_id for this request. "
            "History may contain earlier tool calls; treat them as read-only records. Never "
            "copy or re-emit them; generate every tool call fresh for the latest user turn. "
            "When the latest user turn asks for a concrete change and client tools are available, "
            "carry it out with the tools directly. Do not write a plan and ask the user to confirm "
            "unless the request is ambiguous or destructive. "
            "Never write session summaries, memory notes, compaction records or hand-off documents "
            "as your answer, unless the user explicitly asks for one. Answer only the latest user request. "
            "If the latest user turn needs no tool, answer it directly from context. Never "
            "mention sandbox limits, your environment, or inability to act; just answer. "
            "If a tool result says the client is still executing, call the same tool again "
            "with identical arguments to pick up the result instead of giving up."
        )
        choice = raw.get("tool_choice", "auto")
        if choice == "required":
            directive += " You must call at least one supplied client tool before answering."
        elif isinstance(choice, dict):
            directive += " Call this supplied tool: " + choice["function"]["name"] + "."
        messages.append({"role": "system", "content": directive})
        transcript = build_standard_transcript(messages, raw["model"], account)
        transcript[0]["value"]["enableScriptAgentMcpServers"] = True
        transcript[0]["value"].update(useWebSearch=False, searchScopes=[])
        transcript[1]["value"].update(
            spaceViewId=original.space_view_id, surface="full_page_chat",
        )
        loop = asyncio.get_running_loop()
        stop = threading.Event()

        def produce():
            started = time.monotonic()
            last = started
            count = 0
            substantive = False
            failure = None
            recorded = False
            try:
                try:
                    image_thread = attach_images(client, transcript, raw["messages"],
                                                 get_thread_type(raw["model"]),
                                                 existing_thread_id=existing_thread)
                except Exception as exc:
                    logger.warning("Notion image attachment failed session=%s: %s", session_tag(session), exc)
                    loop.call_soon_threadsafe(session.events.put_nowait, {
                        "type": "error", "message": f"Image attachment failed: {exc}", "status_code": 400,
                    })
                    return
                # Reuse this conversation's thread; a thread opened for image attachments
                # takes precedence when the conversation is new.
                thread = image_thread or existing_thread
                turn = transcript
                for attempt in range(2):
                    for item in client.stream_response(turn, thread_id=thread):
                        if not recorded:
                            recorded = True
                            loop.call_soon_threadsafe(
                                self._remember_thread, conversation_key,
                                getattr(client, "current_thread_id", None) or thread)
                        now = time.monotonic()
                        count += 1
                        if now - last >= 15:
                            logger.warning("Notion tool stream gap session=%s gap=%.1fs type=%s",
                                           session_tag(session), now - last, item.get("type"))
                        last = now
                        if stop.is_set():
                            break
                        if item.get("type") in ("content", "thinking", "final_content"):
                            if item.get("type") in ("content", "final_content") and str(item.get("text") or "").strip():
                                substantive = True
                            loop.call_soon_threadsafe(session.events.put_nowait, item)
                    # Retry only when nothing was answered and no client tool result is awaited,
                    # so the client never sees duplicated or partial answers.
                    if stop.is_set() or substantive or session.pending or attempt == 1:
                        break
                    logger.warning("Notion turn ended without an answer, retrying once session=%s",
                                   session_tag(session))
                    thread = getattr(client, "current_thread_id", None) or thread
                    turn = build_standard_transcript(
                        messages + [{"role": "system", "content": RETRY_NUDGE}], raw["model"], account)
                    turn[0]["value"]["enableScriptAgentMcpServers"] = True
                    turn[0]["value"].update(useWebSearch=False, searchScopes=[])
                    turn[1]["value"].update(spaceViewId=original.space_view_id, surface="full_page_chat")
                if not stop.is_set():
                    failure = classify_stream_end(
                        stopped=False, idle_seconds=time.monotonic() - last,
                        substantive=substantive, pending_calls=len(session.pending))
            except Exception as exc:
                failure = f"upstream error: {type(exc).__name__}"
                logger.warning("Notion tool stream failed session=%s: %s: %s",
                               session_tag(session), type(exc).__name__, exc)
            finally:
                logger.info(
                    "Notion tool stream finished session=%s events=%d substantive=%s total=%.1fs "
                    "idle_since_last=%.1fs stopped=%s failure=%s",
                    session_tag(session), count, substantive, time.monotonic() - started,
                    time.monotonic() - last, stop.is_set(), failure or "none")
                client._scraper.close()
            if failure and not stop.is_set():
                # Upstream closed the stream before the turn finished. Reporting a normal end
                # would look like success to the client while the task keeps running on Notion's
                # side, so fail loudly and let the client retry. Hop back onto the event loop:
                # produce() runs in a worker thread and broker state is loop-owned.
                loop.call_soon_threadsafe(
                    self._close, session,
                    f"Notion 上游提前结束（{failure}），该任务可能仍在 Notion 侧继续，请重试。")
            elif not stop.is_set():
                loop.call_soon_threadsafe(session.events.put_nowait, {"type": "end"})

        async def run():
            try:
                await asyncio.to_thread(produce)
            finally:
                self.jobs.pop(session.id, None)

        job = asyncio.create_task(run())
        self.jobs[session.id] = (stop, job)

    def _close(self, session, reason):
        if session.id in self.jobs:
            self.jobs[session.id][0].set()
        self.broker.close(session.id, reason)

    async def handle(self, request, raw):
        model = raw.get("model", "")
        if not is_supported_model(model) or get_thread_type(model) != "workflow":
            return self._error("This model does not support the Notion workflow MCP bridge.")
        owner = hashlib.sha256(request.headers.get("authorization", "").encode()).hexdigest()
        messages = raw.get("messages", [])
        session = None
        try:
            # Tool results may not be the last messages: Claude Code appends its own
            # system reminder after them, so match results against pending calls instead
            # of trusting the trailing role.
            session, live_results = self.broker.live_results(owner, model, messages)
            logger.info(
                "Client tool request mode=%s messages=%d trailing=%s tools=%d tool_results=%d live=%d",
                "resume" if session is not None else "new", len(messages),
                messages[-1].get("role") if messages else None,
                len(raw.get("tools") or []),
                sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "tool"),
                len(live_results))
            if session is not None:
                if session.id in self.consumers:
                    return self._error("This tool session already has an active response.", 409)
                self.broker.submit_results(session, messages, tools=raw.get("tools") or [],
                                           tool_choice=raw.get("tool_choice", "auto"))
                session.bridge_called = False
                session.bridge_required = session.tool_choice not in ("auto", "none")
                logger.info("Client tool results accepted session=%s pending=%d",
                            session_tag(session), len(session.pending))
            else:
                if len(self.jobs) >= self.broker.max_sessions:
                    return self._error("Too many active upstream tool requests.", 429)
                session = self.broker.create(owner, model, raw.get("tools", []), raw.get("tool_choice", "auto"))
                session.bridge_called = False
                session.bridge_required = raw.get("tool_choice", "auto") not in ("auto", "none", None)
                self._start(session, raw, request.app.state.account_pool)
            self.consumers.add(session.id)
        except BridgeError as exc:
            logger.warning("Client tool request rejected: %s (status %s)", exc, exc.status_code)
            return self._error(str(exc), exc.status_code)
        except (ValueError, RuntimeError) as exc:
            if session is not None:
                self._close(session, "Unable to start tool inference")
            return self._error(str(exc), 503)

        response_id = "chatcmpl-" + uuid.uuid4().hex
        if raw.get("stream"):
            return StreamingResponse(
                self._stream(request, session, response_id, model), media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        return await self._complete(request, session, response_id, model)

    async def _events(self, request, session):
        quiet = 0
        silent = 0
        while True:
            if await request.is_disconnected():
                raise asyncio.CancelledError()
            try:
                event = await asyncio.wait_for(session.events.get(), timeout=1)
            except TimeoutError:
                quiet += 1
                silent += 1
                if silent >= THINKING_NOTICE_SECONDS:
                    quiet = silent = 0
                    self.broker.keepalive(session)
                    yield {"type": "thinking_notice"}
                elif quiet >= HEARTBEAT_SECONDS:
                    quiet = 0
                    self.broker.keepalive(session)
                    yield {"type": "heartbeat"}
                continue
            quiet = silent = 0
            self.broker.keepalive(session)
            kind = event.get("type")
            if kind == "tool_call":
                session.bridge_called = True
            if kind == "end" and session.bridge_required and not session.bridge_called:
                event = {"type": "error", "message": "Notion did not invoke the required client tool.", "status_code": 502}
            yield event
            if event.get("type") in ("tool_call", "end", "error"):
                return

    @staticmethod
    def _chunk(response_id, model, delta, finish_reason=None):
        payload = {"id": response_id, "object": "chat.completion.chunk", "created": int(time.time()),
                   "model": model, "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}
        return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"

    async def _stream(self, request, session, response_id, model):
        keep = False
        text = ""
        sent_chars = sent_calls = 0
        final_chars = -1
        ended = "disconnected"
        try:
            yield self._chunk(response_id, model, {"role": "assistant"})
            async for event in self._events(request, session):
                kind, value = event.get("type"), event.get("text", "")
                if kind == "heartbeat":
                    yield ": ping\n\n"
                elif kind == "thinking_notice":
                    yield self._chunk(response_id, model, {"reasoning_content": "[正在思考...]"})
                elif kind == "content":
                    text += value
                    sent_chars += len(value)
                    yield self._chunk(response_id, model, {"content": value})
                elif kind == "thinking":
                    yield self._chunk(response_id, model, {"reasoning_content": value})
                elif kind == "final_content":
                    if final_chars < 0:
                        final_chars = len(value)
                    if not text:
                        text = value
                        sent_chars += len(value)
                        yield self._chunk(response_id, model, {"content": value})
                    elif value.startswith(text) and len(value) > len(text):
                        # Streamed content was a prefix of the final answer: send only the rest.
                        extra = value[len(text):]
                        text = value
                        sent_chars += len(extra)
                        yield self._chunk(response_id, model, {"content": extra})
                    elif value != text:
                        logger.warning("Final answer diverges from streamed content session=%s streamed=%d final=%d",
                                       session_tag(session), len(text), len(value))
                elif kind == "tool_call":
                    logger.info("Delivering tool call to client session=%s tool=%s call_id=%s",
                                session_tag(session), event["tool_call"]["function"]["name"],
                                event["tool_call"]["id"][-8:])
                    yield self._chunk(response_id, model, {"tool_calls": [{"index": 0, **event["tool_call"]}]})
                    yield self._chunk(response_id, model, {}, "tool_calls")
                    yield "data: [DONE]\n\n"
                    sent_calls += 1
                    ended = "tool_calls"
                    keep = True
                elif kind == "end":
                    ended = "stop"
                    yield self._chunk(response_id, model, {}, "stop")
                    yield "data: [DONE]\n\n"
                elif kind == "error":
                    ended = "error:" + str(event.get("message", ""))[:80]
                    yield "data: " + json.dumps({"error": {"message": event["message"], "type": "tool_bridge_error"}}) + "\n\n"
                    yield "data: [DONE]\n\n"
        finally:
            logger.info("Bridge stream summary session=%s content_chars=%d tool_calls=%d ended=%s final_chars=%d",
                        session_tag(session), sent_chars, sent_calls, ended, final_chars)
            self.consumers.discard(session.id)
            if not keep:
                self._close(session, "Client response closed")

    async def _complete(self, request, session, response_id, model):
        keep = False
        text, thinking, calls, final = [], [], [], ""
        try:
            async for event in self._events(request, session):
                kind = event.get("type")
                if kind in ("heartbeat", "thinking_notice"):
                    continue
                if kind == "content":
                    text.append(event.get("text", ""))
                elif kind == "thinking":
                    thinking.append(event.get("text", ""))
                elif kind == "final_content":
                    final = event.get("text", "")
                elif kind == "tool_call":
                    logger.info("Delivering tool call to client session=%s tool=%s call_id=%s",
                                session_tag(session), event["tool_call"]["function"]["name"],
                                event["tool_call"]["id"][-8:])
                    calls.append(event["tool_call"])
                    keep = True
                elif kind == "error":
                    return self._error(event["message"], event.get("status_code", 502))
            message = {"role": "assistant", "content": final or "".join(text) or None}
            if thinking:
                message["reasoning_content"] = "".join(thinking)
            if calls:
                message["tool_calls"] = calls
            return JSONResponse({"id": response_id, "object": "chat.completion", "created": int(time.time()),
                                 "model": model, "choices": [{"index": 0, "message": message,
                                 "finish_reason": "tool_calls" if calls else "stop"}]})
        finally:
            self.consumers.discard(session.id)
            if not keep:
                self._close(session, "Client response closed")
