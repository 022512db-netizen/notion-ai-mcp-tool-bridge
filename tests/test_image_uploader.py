import base64, struct, zlib, unittest
from unittest.mock import MagicMock, patch
from app import image_uploader as iu


def png(w, h):
    raw = b"".join(b"\x00" + b"\x10\x20\x30" * w for _ in range(h))
    ch = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + ch(b"IDAT", zlib.compress(raw)) + ch(b"IEND", b"")


DATA_URL = "data:image/png;base64," + base64.b64encode(png(5, 7)).decode()


class ImageUploaderTests(unittest.TestCase):
    def test_extract_only_user_images(self):
        msgs = [{"role": "system", "content": [{"type": "image_url", "image_url": {"url": "x"}}]},
                {"role": "user", "content": [{"type": "text", "text": "hi"}, {"type": "image_url", "image_url": {"url": DATA_URL}}]},
                {"role": "user", "content": "plain"}]
        self.assertEqual(iu.extract_image_urls(msgs), [DATA_URL])

    def test_dimensions_and_type(self):
        data, ctype = iu._load(DATA_URL)
        self.assertEqual(ctype, "image/png")
        self.assertEqual(iu._dimensions(data, ctype), (5, 7))

    def test_rejects_http_and_non_images(self):
        with self.assertRaises(ValueError):
            iu._load("http://example.com/a.png")
        with self.assertRaises(ValueError):
            iu._load("data:image/png;base64," + base64.b64encode(b"not an image").decode())

    def test_attach_inserts_before_last_user(self):
        client = MagicMock(space_id="s1")
        client._create_thread.return_value = True
        client._scraper.post.return_value = MagicMock(status_code=200, json=lambda: {
            "url": "attachment:abc:image-1.png", "signedUploadPostUrl": "https://s3", "fields": {"k": "v"}})
        transcript = [{"type": "config"}, {"type": "context"}, {"type": "user"}, {"type": "assistant"}, {"type": "user"}]
        with patch.object(iu.requests, "post", return_value=MagicMock(status_code=204)):
            tid = iu.attach_images(client, transcript, [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": DATA_URL}}]}], "workflow")
        self.assertTrue(tid)
        self.assertEqual([s["type"] for s in transcript], ["config", "context", "user", "assistant", "attachment", "user"])
        self.assertEqual(transcript[4]["metadata"]["width"], 5)

    def test_no_images_no_thread(self):
        client = MagicMock()
        self.assertIsNone(iu.attach_images(client, [{"type": "user"}], [{"role": "user", "content": "hi"}], "workflow"))
        client._create_thread.assert_not_called()


if __name__ == "__main__":
    unittest.main()
