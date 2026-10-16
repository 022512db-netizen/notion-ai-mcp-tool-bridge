import json, os, time
from unittest.mock import patch

# Match test_tool_bridge's offline settings: app config is cached at first import.
os.environ.setdefault("NOTION_ACCOUNTS", json.dumps([{"token_v2": "offline", "space_id": "offline", "user_id": "offline"}]))
os.environ.setdefault("API_KEY", "offline-api-key")
os.environ.setdefault("APP_MODE", "standard")
from app.api import chat


def slow_stream():
    time.sleep(0.35)
    yield {"type": "content", "text": "done"}


def test_long_silence_emits_thinking_notice_then_resumes():
    with patch.object(chat, "STREAM_HEARTBEAT_SECONDS", 0.05), patch.object(chat, "THINKING_NOTICE_SECONDS", 0.2):
        items = list(chat._iter_stream_items_with_heartbeat(None, slow_stream()))
    assert chat._THINKING_NOTICE in items
    assert chat._HEARTBEAT_LINE in items
    assert items.index(chat._HEARTBEAT_LINE) < items.index(chat._THINKING_NOTICE)
    assert items[-1] == {"type": "content", "text": "done"}


def test_short_silence_only_heartbeats():
    def quick():
        time.sleep(0.1)
        yield {"type": "content", "text": "x"}
    with patch.object(chat, "STREAM_HEARTBEAT_SECONDS", 0.05), patch.object(chat, "THINKING_NOTICE_SECONDS", 5):
        items = list(chat._iter_stream_items_with_heartbeat(None, quick()))
    assert chat._THINKING_NOTICE not in items
