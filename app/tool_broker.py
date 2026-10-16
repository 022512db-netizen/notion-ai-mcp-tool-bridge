"""In-memory client-tool bridge; all methods run on the same asyncio loop.

No tools are executed here. Definitions, including any read/write annotations,
are preserved: callers must not advertise arbitrary client tools as read-only.
TTL is an idle timeout, refreshed on emitted calls and accepted results.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
import json
import math
import re
import secrets
from typing import Any

from jsonschema import Draft202012Validator, SchemaError, ValidationError

from app.logger import logger as _logger
from jsonschema.validators import validator_for
from referencing import Registry
from referencing.exceptions import Unresolvable


MAX_TOOLS_BYTES = 1024 * 1024
MAX_ARGUMENT_BYTES = 64 * 1024
MAX_RESULT_BYTES = 1024 * 1024


# Notion gives up waiting for an MCP tool result after roughly a minute
# (server error -32001). Answer before that with a retry hint instead of
# letting Notion time out; the client execution continues in the background.
import os as _os
CALL_TOOL_TIMEOUT_SECONDS = float(_os.getenv("MCP_CALL_TOOL_TIMEOUT_SECONDS", "50"))


class BridgeError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _json_copy(value: Any, label: str, limit: int) -> tuple[str, Any]:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":"))
        if len(encoded.encode("utf-8")) > limit:
            raise BridgeError(f"{label} exceeds the {limit}-byte limit.", 413)
        copied = json.loads(encoded)
        if copied != value:
            raise BridgeError(f"{label} must contain only JSON values and string keys.")
        return encoded, copied
    except (TypeError, ValueError, RecursionError, OverflowError) as exc:
        raise BridgeError(f"{label} must be valid JSON.") from exc


@dataclass(eq=False)
class ToolSession:
    id: str
    owner: str
    model: str
    tools: list[dict]
    tool_choice: Any = "auto"
    events: asyncio.Queue = field(default_factory=asyncio.Queue)
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    expires_at: float = 0.0
    closed: bool = False
    close_reason: str = ""
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _validators: dict[str, Any] = field(default_factory=dict, repr=False)
    _call_ids: set[str] = field(default_factory=set, repr=False)
    _expiry: Any = field(default=None, repr=False)

    @property
    def call_count(self) -> int:
        """The API integration checks this for tool_choice='required'."""
        return len(self._call_ids)


class ToolBroker:
    def __init__(self, ttl_seconds: float = 90, max_sessions: int = 8):
        if (not isinstance(ttl_seconds, (int, float)) or isinstance(ttl_seconds, bool)
                or not math.isfinite(ttl_seconds) or ttl_seconds <= 0):
            raise ValueError("ttl_seconds must be finite and positive")
        if not isinstance(max_sessions, int) or isinstance(max_sessions, bool) or max_sessions < 1:
            raise ValueError("max_sessions must be a positive integer")
        self.ttl_seconds = ttl_seconds
        self.max_sessions = max_sessions
        self._sessions: dict[str, ToolSession] = {}
        self._calls: dict[str, str] = {}
        # Bounded tombstones distinguish recently expired capabilities from unknown IDs.
        self._closed: OrderedDict[str, tuple[str, str, str]] = OrderedDict()
        self._stopped = False

    @staticmethod
    def _validate_tools(tools: list[dict], tool_choice):
        if not isinstance(tools, list):
            raise BridgeError("tools must be a list of functions.")
        # Client definitions are MCP catalog data, not upstream API tool slots.
        _, copied_tools = _json_copy(tools, "Tool definitions", MAX_TOOLS_BYTES)
        validators = {}
        for tool in copied_tools:
            if not isinstance(tool, dict) or tool.get("type") != "function":
                raise BridgeError("Only type=function tools are supported.")
            fn = tool.get("function")
            if not isinstance(fn, dict):
                raise BridgeError("Each tool needs a function definition.")
            name = fn.get("name")
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
                raise BridgeError("Function names must be 1-64 letters, digits, underscores or hyphens.")
            if name in validators:
                raise BridgeError(f"Duplicate tool name: {name}.")
            schema = fn.get("parameters", {"type": "object", "additionalProperties": False})
            if not isinstance(schema, dict):
                raise BridgeError(f"Parameters for {name} must be a JSON Schema object.")
            try:
                validator_type = validator_for(schema, default=Draft202012Validator)
                validator_type.check_schema(schema)
                # An empty registry disables implicit remote reference fetching.
                validators[name] = validator_type(schema, registry=Registry())
            except (SchemaError, TypeError, ValueError, RecursionError) as exc:
                raise BridgeError(f"Invalid parameter schema for {name}.") from exc
        if isinstance(tool_choice, str):
            if tool_choice not in ("auto", "none", "required"):
                raise BridgeError("Unsupported tool_choice.")
            if tool_choice == "required" and not validators:
                raise BridgeError("tool_choice=required needs at least one tool.")
        elif isinstance(tool_choice, dict):
            fn = tool_choice.get("function")
            name = fn.get("name") if isinstance(fn, dict) else None
            if (not isinstance(name, str) or name not in validators
                    or tool_choice != {"type": "function", "function": {"name": name}}):
                raise BridgeError("Named tool_choice must select one of the provided functions.")
            tool_choice = {"type": "function", "function": {"name": name}}
        else:
            raise BridgeError("Unsupported tool_choice.")
        return copied_tools, validators, tool_choice

    def create(self, owner: str, model: str, tools: list[dict], tool_choice="auto") -> ToolSession:
        if self._stopped:
            raise BridgeError("Tool broker is shut down.", 503)
        if not isinstance(owner, str) or not owner or not isinstance(model, str) or not model:
            raise BridgeError("owner and model must be nonempty strings.")
        copied_tools, validators, tool_choice = self._validate_tools(tools, tool_choice)
        for session in list(self._sessions.values()):
            self._expire_if_due(session)
        if len(self._sessions) >= self.max_sessions:
            raise BridgeError("Too many active tool sessions.", 429)
        session = ToolSession("session_" + secrets.token_urlsafe(24), owner, model,
                              copied_tools, tool_choice, _validators=validators)
        self._sessions[session.id] = session
        self._touch(session)
        return session

    def _touch(self, session: ToolSession) -> None:
        loop = asyncio.get_running_loop()
        if session._expiry is not None:
            session._expiry.cancel()
        session.expires_at = loop.time() + self.ttl_seconds
        session._expiry = loop.call_at(session.expires_at, self._expire_if_due, session)

    def keepalive(self, session: ToolSession) -> None:
        """Extend an open session while Notion is still producing output (thinking can go silent for a minute)."""
        if not session.closed and session.id in self._sessions:
            self._touch(session)

    def _expire_if_due(self, session: ToolSession) -> None:
        if not session.closed and asyncio.get_running_loop().time() >= session.expires_at:
            reason = "Tool result timed out; session expired." if session.pending else "Tool session expired."
            self.close(session.id, reason)

    def get(self, session_id: str) -> ToolSession:
        if not isinstance(session_id, str) or not session_id:
            raise BridgeError("Missing tool session ID.")
        session = self._sessions.get(session_id)
        if session is not None:
            self._expire_if_due(session)
            if not session.closed:
                return session
        if session_id in self._closed:
            _logger.warning("Tool call for a closed session id=...%s: %s",
                            session_id[-6:], self._closed[session_id][2])
            raise BridgeError(self._closed[session_id][2], 410)
        _logger.warning("Tool call for an unknown session id=...%s", session_id[-6:])
        raise BridgeError("Unknown tool session.", 404)

    def live_results(self, owner: str, model: str, messages: list[dict]):
        """Tool results submitted at the end of this request.

        Returns (session or None, trailing tool messages). Clients may append other
        messages after the tool results — Claude Code adds a trailing system reminder —
        so trailing system messages are skipped when locating the submission block. A
        request with no trailing tool results is an ordinary new turn, not an error.
        """
        if not isinstance(messages, list):
            raise BridgeError("messages must be a list.")
        trailing: list[dict] = []
        for message in reversed(messages):
            if not isinstance(message, dict):
                break
            role = message.get("role")
            if role == "tool":
                trailing.append(message)
                continue
            if role == "system":
                # Not a boundary: reminders are appended after the results.
                continue
            break
        trailing.reverse()
        if not trailing:
            return None, []

        session = None
        seen = set()
        for message in trailing:
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or not call_id:
                raise BridgeError("Missing tool_call_id in tool result.")
            if call_id in seen:
                raise BridgeError("Duplicate tool_call_id in tool results.", 409)
            seen.add(call_id)
            session_id = self._calls.get(call_id)
            if session_id is None:
                retired = self._closed.get(call_id)
                if retired is not None:
                    self._check_binding(owner, model, retired[0], retired[1])
                    raise BridgeError(retired[2], 410)
                raise BridgeError("Unknown tool_call_id.", 404)
            found = self.get(session_id)
            self._check_binding(owner, model, found.owner, found.model)
            if session is not None and session is not found:
                raise BridgeError("Tool results must belong to one session.")
            future = found.pending.get(call_id)
            if future is None or future.done():
                raise BridgeError("Duplicate or already completed tool result.", 409)
            session = found
        return session, trailing

    @staticmethod
    def _check_binding(owner: str, model: str, expected_owner: str, expected_model: str) -> None:
        if owner != expected_owner:
            raise BridgeError("Tool session belongs to another owner.", 403)
        if model != expected_model:
            raise BridgeError("Tool result model does not match its session.")

    def get_for_results(self, owner: str, model: str, messages: list[dict]) -> ToolSession:
        session, results = self.live_results(owner, model, messages)
        if session is None:
            raise BridgeError("Missing trailing role=tool results.", 400)
        if not results:
            raise BridgeError("No tool results pending for an open session.", 404)
        return session

    def submit_results(self, session: ToolSession, messages: list[dict], *, tools=None, tool_choice=None) -> None:
        """Resolve the pending calls answered by tool results inside this request.

        Accepts the full request so callers do not have to pre-filter; live_results()
        picks the tool messages that answer calls still pending on this session.
        """
        found, results = self.live_results(session.owner, session.model, messages)
        if found is not session:
            raise BridgeError("Tool results do not belong to this session.")
        if not results:
            raise BridgeError("Missing trailing role=tool results.", 400)
        updated = None
        if tools is not None or tool_choice is not None:
            updated = self._validate_tools(
                session.tools if tools is None else tools,
                session.tool_choice if tool_choice is None else tool_choice,
            )
        prepared = []
        for message in results:
            content = message.get("content")
            if isinstance(content, list):
                if not all(isinstance(part, dict) and part.get("type") == "text"
                           and isinstance(part.get("text"), str) for part in content):
                    raise BridgeError("Tool result content supports text parts only.")
                content = "".join(part["text"] for part in content)
            if not isinstance(content, str):
                raise BridgeError("Tool result content must be a string or text parts.")
            try:
                size = len(content.encode("utf-8"))
            except UnicodeError as exc:
                raise BridgeError("Tool result content must be valid UTF-8.") from exc
            if size > MAX_RESULT_BYTES:
                raise BridgeError("Tool result content is too large.", 413)
            prepared.append((message["tool_call_id"], content))
        # Validate the entire batch before resolving any Future.
        if updated is not None:
            session.tools, session._validators, session.tool_choice = updated
        for call_id, content in prepared:
            session.pending.pop(call_id).set_result(content)
        self._touch(session)

    async def call_tool(self, session_id: str, name: str, arguments: dict) -> str:
        session = self.get(session_id)
        async with session._lock:
            self.get(session_id)
            if not isinstance(name, str) or name not in session._validators:
                raise BridgeError("Unknown or unprovided client tool.")
            if session.tool_choice == "none":
                raise BridgeError("tool_choice=none forbids tool calls.")
            if isinstance(session.tool_choice, dict) and name != session.tool_choice["function"]["name"]:
                raise BridgeError("Tool call does not match named tool_choice.")
            if not isinstance(arguments, dict):
                raise BridgeError("Tool arguments must be a JSON object.")
            encoded, copied = _json_copy(arguments, "Tool arguments", MAX_ARGUMENT_BYTES)
            try:
                session._validators[name].validate(copied)
            except (ValidationError, Unresolvable, RecursionError) as exc:
                raise BridgeError(f"Invalid arguments or unresolved schema for tool {name}.") from exc
            # Retry pickup: if the same tool+arguments is already running, wait on it
            # instead of issuing a duplicate call to the client.
            for waiting_id, waiting in session.pending.items():
                if getattr(waiting, "_tool_key", None) == (name, encoded):
                    try:
                        return await asyncio.wait_for(asyncio.shield(waiting), timeout=CALL_TOOL_TIMEOUT_SECONDS)
                    except TimeoutError:
                        return ("[The client is still executing this tool. Call me again with the same "
                                "request_id, name and arguments to pick up the result.]")
            call_id = "call_" + secrets.token_urlsafe(24)
            future = asyncio.get_running_loop().create_future()
            future._tool_key = (name, encoded)
            session.pending[call_id] = future
            session._call_ids.add(call_id)
            self._calls[call_id] = session.id
            self._touch(session)
            session.events.put_nowait({"type": "tool_call", "tool_call": {
                "id": call_id, "type": "function",
                "function": {"name": name, "arguments": encoded},
            }})
            try:
                return await asyncio.wait_for(asyncio.shield(future), timeout=CALL_TOOL_TIMEOUT_SECONDS)
            except TimeoutError:
                return ("[The client is still executing this tool. Call me again with the same "
                        "request_id, name and arguments to pick up the result.]")
            except asyncio.CancelledError:
                if session.closed:
                    raise BridgeError(session.close_reason, 410) from None
                self.close(session.id, "Tool call cancelled.")
                raise
            finally:
                if future.done():
                    session.pending.pop(call_id, None)

    def close(self, session_id: str, reason: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return
        session.closed = True
        session.close_reason = reason
        if session._expiry is not None:
            session._expiry.cancel()
            session._expiry = None
        for future in session.pending.values():
            future.cancel()
        session.pending.clear()
        for capability in (session.id, *session._call_ids):
            self._calls.pop(capability, None)
            self._closed[capability] = (session.owner, session.model, reason)
        while len(self._closed) > 1024:
            self._closed.popitem(last=False)
        # Do not deliver stale tool calls after the session has closed.
        while not session.events.empty():
            session.events.get_nowait()
        session.events.put_nowait({"type": "error", "message": reason, "status_code": 410})

    async def shutdown(self) -> None:
        self._stopped = True
        for session_id in list(self._sessions):
            self.close(session_id, "Tool broker shut down.")
