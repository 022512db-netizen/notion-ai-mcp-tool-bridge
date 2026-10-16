"""Model identity at the HTTP boundary, without contacting Notion."""

from copy import deepcopy
import unittest
from unittest.mock import patch

from app.conversation import ConversationManager, build_lite_transcript, build_standard_transcript
from app.model_registry import MODEL_MAP, get_notion_model, is_gemini_model
from app.notion_client import NotionOpusAPI


class DispatchCaptured(Exception):
    pass


class ModelRoutingTests(unittest.TestCase):
    def test_public_and_internal_names_are_idempotent(self):
        for public, internal in MODEL_MAP.items():
            with self.subTest(model=public):
                self.assertEqual(get_notion_model(public), internal)
                self.assertEqual(get_notion_model(internal), internal)
                self.assertEqual(get_notion_model(get_notion_model(public)), internal)

    def test_unknown_names_fail_instead_of_switching_models(self):
        client = object.__new__(NotionOpusAPI)
        for name in ("", "unknown-model", "notion/claude-opus5.5"):
            with self.subTest(model=name):
                with self.assertRaises(ValueError):
                    get_notion_model(name)
                with self.assertRaises(ValueError):
                    client._to_notion_transcript([
                        {"type": "config", "value": {"model": name}},
                    ])

    def test_standard_and_lite_transcripts_preserve_every_model(self):
        client = object.__new__(NotionOpusAPI)
        manager = object.__new__(ConversationManager)
        for public, internal in MODEL_MAP.items():
            transcripts = {
                "standard": build_standard_transcript(
                    [{"role": "user", "content": "offline"}], public, {}),
                "lite": build_lite_transcript("offline", public),
                "heavy": [manager._build_config_block(public, gemini_mode=is_gemini_model(public))],
            }
            for mode, transcript in transcripts.items():
                with self.subTest(model=public, mode=mode):
                    original = deepcopy(transcript)
                    sent = client._to_notion_transcript(transcript)
                    self.assertEqual(sent[0]["value"]["model"], internal)
                    self.assertEqual(client._to_notion_transcript(sent), sent)
                    self.assertEqual(transcript, original)

    def test_actual_http_payload_preserves_requested_workflow_model(self):
        for public in ("claude-opus5.5", "claude-sonnet5.5", "claude-haiku5.5", "fable-5"):
            with self.subTest(model=public):
                with patch("app.notion_client.cffi_requests.Session") as session_factory:
                    scraper = session_factory.return_value
                    scraper.cookies = {}
                    scraper.post.side_effect = DispatchCaptured
                    client = NotionOpusAPI({"token_v2": "offline", "space_id": "offline", "user_id": "offline"})
                    transcript = build_standard_transcript(
                        [{"role": "user", "content": "offline"}], public, {})
                    with self.assertRaises(DispatchCaptured):
                        next(client.stream_response(transcript))
                    payload = scraper.post.call_args.kwargs["json"]
                    self.assertEqual(payload["transcript"][0]["value"]["model"], MODEL_MAP[public])
                    self.assertTrue(payload["transcript"][0]["value"]["modelFromUser"])
                    self.assertEqual(payload["threadType"], "workflow")
                    self.assertTrue(payload["createThread"])
                    scraper.post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
