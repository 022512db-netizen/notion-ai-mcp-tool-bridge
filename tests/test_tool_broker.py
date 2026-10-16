"""Offline checks: .venv/bin/python -B -m unittest discover -s tests -p test_tool_broker.py -v"""

import asyncio
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from app.tool_broker import (
    BridgeError, MAX_ARGUMENT_BYTES, MAX_RESULT_BYTES, MAX_TOOLS_BYTES, ToolBroker,
)


TOOLS = [{
    "type": "function",
    "function": {
        "name": "read_item",
        "description": "Read an item.",
        "parameters": {
            "type": "object",
            "properties": {"key": {"type": "string", "minLength": 1}},
            "required": ["key"],
            "additionalProperties": False,
        },
    },
    "annotations": {"readOnlyHint": True},
}, {
    "type": "function",
    "function": {"name": "write_item", "parameters": {"type": "object"}},
    "annotations": {"readOnlyHint": False, "destructiveHint": True},
}]


def result(event, content="ok"):
    return {"role": "tool", "tool_call_id": event["tool_call"]["id"], "content": content}


class ToolBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.broker = ToolBroker(ttl_seconds=2)
        self.tasks = []
        self.network = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        self.network.start()

    async def asyncTearDown(self):
        await self.broker.shutdown()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.network.stop()

    def session(self, owner="owner", model="model", tools=None, choice="auto"):
        return self.broker.create(owner, model, TOOLS if tools is None else tools, choice)

    def start(self, session, name="read_item", arguments=None):
        task = asyncio.create_task(self.broker.call_tool(
            session.id, name, {"key": "value"} if arguments is None else arguments))
        self.tasks.append(task)
        return task

    async def event(self, session):
        return await asyncio.wait_for(session.events.get(), 0.5)

    async def test_success_and_definition_snapshot(self):
        tools = deepcopy(TOOLS)
        session = self.session(tools=tools)
        tools[0]["function"]["name"] = "changed"
        self.assertEqual(session.tools, TOOLS)
        self.assertFalse(session.tools[1]["annotations"]["readOnlyHint"])
        self.assertIs(self.broker.get(session.id), session)
        task = self.start(session)
        event = await self.event(session)
        call = event["tool_call"]
        self.assertEqual(event["type"], "tool_call")
        self.assertEqual(call["type"], "function")
        self.assertEqual(call["function"]["name"], "read_item")
        self.assertEqual(json.loads(call["function"]["arguments"]), {"key": "value"})
        self.assertTrue(call["id"].startswith("call_"))
        self.assertIn(call["id"], session.pending)
        messages = [result(event, "the result")]
        self.assertIs(self.broker.get_for_results("owner", "model", messages), session)
        self.broker.submit_results(session, messages)
        self.assertEqual(await task, "the result")
        self.assertEqual(session.pending, {})
        self.assertEqual(session.call_count, 1)

    async def test_large_client_catalog_is_not_truncated(self):
        tools = [deepcopy(TOOLS[0]) for _ in range(300)]
        for index, tool in enumerate(tools):
            tool["function"]["name"] = f"client_tool_{index}"
        session = self.session(tools=tools)
        self.assertEqual(session.tools, tools)
        task = self.start(session, "client_tool_299")
        event = await self.event(session)
        self.assertEqual(event["tool_call"]["function"]["name"], "client_tool_299")
        self.broker.submit_results(session, [result(event)], tools=tools, tool_choice="auto")
        self.assertEqual(await task, "ok")
        self.assertEqual(len(session.tools), 300)
        second = self.start(session, "client_tool_128")
        self.broker.submit_results(session, [result(await self.event(session))])
        self.assertEqual(await second, "ok")

    async def test_catalog_byte_limit_still_applies(self):
        tool = deepcopy(TOOLS[0])
        tool["function"]["description"] = "x" * MAX_TOOLS_BYTES
        with self.assertRaises(BridgeError) as exc:
            self.session(tools=[tool])
        self.assertEqual(exc.exception.status_code, 413)
        self.assertFalse(self.broker._sessions)

    async def test_owner_and_model_isolation(self):
        session = self.session()
        task = self.start(session)
        event = await self.event(session)
        for owner, model, status in [("other", "model", 403), ("owner", "other", 400)]:
            with self.subTest(owner=owner, model=model), self.assertRaises(BridgeError) as exc:
                self.broker.get_for_results(owner, model, [result(event)])
            self.assertEqual(exc.exception.status_code, status)
        self.assertFalse(task.done())
        other = self.session()
        with self.assertRaises(BridgeError):
            self.broker.submit_results(other, [result(event)])
        self.assertFalse(task.done())

    async def test_only_trailing_tool_messages_are_used(self):
        session = self.session()
        task = self.start(session)
        event = await self.event(session)
        history = [{"role": "tool", "tool_call_id": "old"}, {"role": "assistant", "content": "next"}]
        self.assertIs(self.broker.get_for_results("owner", "model", history + [result(event)]), session)
        with self.assertRaises(BridgeError):
            self.broker.get_for_results("owner", "model", [result(event)] + [{"role": "user"}])
        self.broker.submit_results(session, history + [result(event)])
        self.assertEqual(await task, "ok")

    async def test_missing_unknown_and_duplicate_results(self):
        for messages, status in [([], 400), ([{"role": "tool"}], 400),
                                 ([{"role": "tool", "tool_call_id": "unknown"}], 404)]:
            with self.subTest(messages=messages), self.assertRaises(BridgeError) as exc:
                self.broker.get_for_results("owner", "model", messages)
            self.assertEqual(exc.exception.status_code, status)
        session = self.session()
        task = self.start(session)
        event = await self.event(session)
        message = result(event)
        with self.assertRaises(BridgeError) as exc:
            self.broker.submit_results(session, [message, message])
        self.assertEqual(exc.exception.status_code, 409)
        self.assertFalse(task.done())
        self.broker.submit_results(session, [message])
        for _ in range(2):
            with self.assertRaises(BridgeError) as exc:
                self.broker.submit_results(session, [message])
            self.assertEqual(exc.exception.status_code, 409)
        self.assertEqual(await task, "ok")

    async def test_unknown_tools_bad_arguments_and_limits(self):
        session = self.session()
        cases = [("unknown", {}, 400), ("read_item", {}, 400),
                 ("read_item", {"key": 42}, 400), ("read_item", {"key": ""}, 400),
                 ("read_item", {"key": "ok", "extra": True}, 400),
                 ("read_item", "{}", 400), ("read_item", [], 400),
                 ("write_item", {1: "bad key"}, 400),
                 ("write_item", {"value": float("nan")}, 400),
                 ("read_item", {"key": "x" * MAX_ARGUMENT_BYTES}, 413)]
        for name, arguments, status in cases:
            with self.subTest(name=name, arguments_type=type(arguments)), self.assertRaises(BridgeError) as exc:
                await self.broker.call_tool(session.id, name, arguments)
            self.assertEqual(exc.exception.status_code, status)
        self.assertTrue(session.events.empty())
        self.assertFalse(session.pending)
        self.assertEqual(session.call_count, 0)

    async def test_invalid_tool_definitions(self):
        invalid = [None, [{"type": "custom"}], [{"type": "function", "function": {"name": "a.b"}}],
                   [TOOLS[0], TOOLS[0]], [{"type": "function", "function": {
                       "name": "bad", "parameters": {"type": "not-a-type"}}}]]
        for tools in invalid:
            with self.subTest(tools=tools), self.assertRaises(BridgeError):
                self.broker.create("owner", "model", tools)
        self.assertFalse(self.broker._sessions)

    async def test_tool_choice(self):
        none = self.session(choice="none")
        with self.assertRaises(BridgeError):
            await self.broker.call_tool(none.id, "read_item", {"key": "x"})
        named = self.session(choice={"type": "function", "function": {"name": "write_item"}})
        with self.assertRaises(BridgeError):
            await self.broker.call_tool(named.id, "read_item", {"key": "x"})
        task = self.start(named, "write_item", {})
        event = await self.event(named)
        self.broker.submit_results(named, [result(event)])
        self.assertEqual(await task, "ok")
        required = self.session(choice="required")
        self.assertEqual(required.call_count, 0)
        for choice in ["invalid", {"type": "function", "function": {"name": "not-provided"}}]:
            with self.assertRaises(BridgeError):
                self.session(choice=choice)
        with self.assertRaises(BridgeError):
            self.session(tools=[], choice="required")

    async def test_local_schema_references_and_no_remote_fetch(self):
        tool = {"type": "function", "function": {"name": "schema_tool", "parameters": {
            "type": "object", "$defs": {"value": {"type": "integer", "minimum": 1}},
            "properties": {"value": {"$ref": "#/$defs/value"}}, "required": ["value"],
        }}}
        session = self.session(tools=[tool])
        with self.assertRaises(BridgeError):
            await self.broker.call_tool(session.id, "schema_tool", {"value": 0})
        task = self.start(session, "schema_tool", {"value": 1})
        self.broker.submit_results(session, [result(await self.event(session))])
        self.assertEqual(await task, "ok")
        tool["function"]["parameters"] = {"$ref": "https://invalid.example/schema"}
        remote = self.session(tools=[tool])
        with self.assertRaises(BridgeError):
            await self.broker.call_tool(remote.id, "schema_tool", {})
        self.assertTrue(remote.events.empty())

    async def test_next_turn_can_release_named_choice_and_revoke_tools(self):
        session = self.session(choice={"type": "function", "function": {"name": "read_item"}})
        first = self.start(session)
        event = await self.event(session)
        self.broker.submit_results(session, [result(event)], tools=TOOLS, tool_choice="auto")
        self.assertEqual(await first, "ok")
        second = self.start(session, "write_item", {})
        event = await self.event(session)
        self.broker.submit_results(session, [result(event)], tools=[TOOLS[0]], tool_choice="none")
        self.assertEqual(await second, "ok")
        for name, arguments in [("write_item", {}), ("read_item", {"key": "x"})]:
            with self.assertRaises(BridgeError):
                await self.broker.call_tool(session.id, name, arguments)
        self.assertTrue(session.events.empty())

    async def test_invalid_next_turn_does_not_consume_result_or_permissions(self):
        session = self.session()
        pending = self.start(session)
        event = await self.event(session)
        with self.assertRaises(BridgeError):
            self.broker.submit_results(session, [result(event)], tools=[], tool_choice="required")
        self.assertFalse(pending.done())
        self.assertEqual(session.tools, TOOLS)
        self.broker.submit_results(session, [result(event)], tools=[], tool_choice="auto")
        self.assertEqual(await pending, "ok")
        with self.assertRaises(BridgeError):
            await self.broker.call_tool(session.id, "read_item", {"key": "x"})

    async def test_result_validation_is_atomic(self):
        session = self.session()
        task = self.start(session)
        event = await self.event(session)
        for content, status in [(None, 400), ({"raw": True}, 400),
                                ([{"type": "image_url"}], 400), ("x" * (MAX_RESULT_BYTES + 1), 413)]:
            with self.subTest(content_type=type(content)), self.assertRaises(BridgeError) as exc:
                self.broker.submit_results(session, [result(event, content)])
            self.assertEqual(exc.exception.status_code, status)
            self.assertFalse(task.done())
        self.broker.submit_results(session, [result(event, [
            {"type": "text", "text": "a"}, {"type": "text", "text": "b"}])])
        self.assertEqual(await task, "ab")

    async def test_session_calls_are_serialized(self):
        session = self.session()
        first = self.start(session)
        first_event = await self.event(session)
        second = self.start(session)
        await asyncio.sleep(0)
        self.assertTrue(session.events.empty())
        self.assertEqual(len(session.pending), 1)
        self.broker.submit_results(session, [result(first_event, "first")])
        self.assertEqual(await first, "first")
        second_event = await self.event(session)
        self.assertNotEqual(first_event["tool_call"]["id"], second_event["tool_call"]["id"])
        self.broker.submit_results(session, [result(second_event, "second")])
        self.assertEqual(await second, "second")

    async def test_concurrent_sessions_and_mixed_results(self):
        one, two = self.session(), self.session()
        first, second = self.start(one), self.start(two)
        event_one, event_two = await self.event(one), await self.event(two)
        with self.assertRaises(BridgeError):
            self.broker.get_for_results("owner", "model", [result(event_one), result(event_two)])
        self.assertFalse(first.done())
        self.assertFalse(second.done())
        self.broker.submit_results(two, [result(event_two, "two")])
        self.broker.submit_results(one, [result(event_one, "one")])
        self.assertEqual(await asyncio.gather(first, second), ["one", "two"])

    async def test_close_cleans_up_and_wakes_reader_and_callers(self):
        session = self.session()
        first = self.start(session)
        event = await self.event(session)
        pending = session.pending[event["tool_call"]["id"]]
        second = self.start(session)
        await asyncio.sleep(0)
        reader = asyncio.create_task(self.event(session))
        self.broker.close(session.id, "closed by API")
        self.assertEqual(await reader, {"type": "error", "message": "closed by API", "status_code": 410})
        self.assertTrue(pending.cancelled())
        self.assertFalse(session.pending)
        self.assertNotIn(session.id, self.broker._sessions)
        self.assertNotIn(event["tool_call"]["id"], self.broker._calls)
        for task in (first, second):
            with self.assertRaises(BridgeError) as exc:
                await task
            self.assertEqual(exc.exception.status_code, 410)
        with self.assertRaises(BridgeError) as exc:
            self.broker.get_for_results("owner", "model", [result(event)])
        self.assertEqual(exc.exception.status_code, 410)
        self.broker.close(session.id, "again")
        self.assertTrue(session.events.empty())

    async def test_timeout_is_explicit_and_automatic(self):
        self.broker.ttl_seconds = 0.03
        session = self.session()
        task = self.start(session)
        event = await self.event(session)
        error = await self.event(session)
        self.assertEqual(error["status_code"], 410)
        self.assertIn("timed out", error["message"])
        with self.assertRaisesRegex(BridgeError, "timed out"):
            await task
        with self.assertRaises(BridgeError) as exc:
            self.broker.get_for_results("owner", "model", [result(event)])
        self.assertEqual(exc.exception.status_code, 410)
        self.assertFalse(self.broker._sessions)
        self.assertFalse(self.broker._calls)

    async def test_keepalive_extends_open_session(self):
        self.broker.ttl_seconds = 0.2
        session = self.session()
        for _ in range(4):
            await asyncio.sleep(0.1)
            self.broker.keepalive(session)
        self.assertFalse(session.closed)
        self.assertIs(self.broker.get(session.id), session)
        await asyncio.sleep(0.3)
        self.assertTrue(session.closed)

    async def test_unconsumed_sessions_expire_and_release_capacity(self):
        self.broker.ttl_seconds = 0.03
        self.broker.max_sessions = 1
        session = self.session()
        with self.assertRaises(BridgeError) as exc:
            self.session()
        self.assertEqual(exc.exception.status_code, 429)
        self.assertEqual((await self.event(session))["type"], "error")
        self.assertTrue(session.closed)
        self.assertFalse(self.broker._sessions)
        self.assertNotEqual(self.session().id, session.id)

    async def test_unconsumed_tool_event_is_replaced_on_close(self):
        session = self.session()
        task = self.start(session)
        await asyncio.sleep(0)
        self.broker.close(session.id, "closed")
        self.assertEqual((await self.event(session))["type"], "error")
        self.assertTrue(session.events.empty())
        with self.assertRaises(BridgeError):
            await task

    async def test_call_cancellation_and_shutdown(self):
        session = self.session()
        task = self.start(session)
        await self.event(session)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(session.closed)
        self.assertEqual((await self.event(session))["status_code"], 410)
        other = self.session()
        waiting = self.start(other)
        await self.event(other)
        await self.broker.shutdown()
        await self.broker.shutdown()
        with self.assertRaises(BridgeError):
            await waiting
        self.assertFalse(self.broker._sessions)
        self.assertFalse(self.broker._calls)
        with self.assertRaises(BridgeError) as exc:
            self.session()
        self.assertEqual(exc.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
