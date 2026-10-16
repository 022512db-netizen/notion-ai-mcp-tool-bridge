"""Upload OpenAI-style image_url parts to Notion and attach them to a transcript."""

import base64
import os
import struct
import uuid
from typing import Any, Optional

import requests

from app.logger import logger
from app.notion_client import NOTION_CLIENT_VERSION, NOTION_URL

MAX_IMAGE_BYTES = int(os.getenv("NOTION_MAX_IMAGE_BYTES", str(20 * 1024 * 1024)))
MAX_IMAGES = int(os.getenv("NOTION_MAX_IMAGES", "6"))
ALLOWED_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}


def extract_image_urls(raw_messages: list) -> list[str]:
    """Image URLs from user messages, oldest first, capped to the most recent MAX_IMAGES."""
    urls = []
    for message in raw_messages or []:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "image_url":
                continue
            value = part.get("image_url")
            url = value.get("url") if isinstance(value, dict) else value
            if isinstance(url, str) and url:
                urls.append(url)
    return urls[-MAX_IMAGES:]


def _sniff_type(data: bytes) -> Optional[str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _dimensions(data: bytes, content_type: str) -> tuple[int, int]:
    try:
        if content_type == "image/png":
            return struct.unpack(">II", data[16:24])
        if content_type == "image/gif":
            return struct.unpack("<HH", data[6:10])
        if content_type == "image/jpeg":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                length = struct.unpack(">H", data[i + 2:i + 4])[0]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                i += 2 + length
        if content_type == "image/webp":
            chunk = data[12:16]
            if chunk == b"VP8X":
                return (int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1)
            if chunk == b"VP8 ":
                w, h = struct.unpack("<HH", data[26:30])
                return w & 0x3FFF, h & 0x3FFF
            if chunk == b"VP8L":
                bits = int.from_bytes(data[21:25], "little")
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    except (struct.error, IndexError):
        pass
    return 0, 0


def _load(url: str) -> tuple[bytes, str]:
    if url.startswith("data:"):
        header, _, payload = url.partition(",")
        if ";base64" not in header:
            raise ValueError("Only base64 data URLs are supported")
        data = base64.b64decode(payload, validate=False)
    elif url.startswith("https://"):
        with requests.get(url, timeout=30, stream=True, headers={"User-Agent": "Mozilla/5.0"}) as resp:
            resp.raise_for_status()
            chunks, size = [], 0
            for chunk in resp.iter_content(65536):
                size += len(chunk)
                if size > MAX_IMAGE_BYTES:
                    raise ValueError("Image exceeds size limit")
                chunks.append(chunk)
            data = b"".join(chunks)
    else:
        raise ValueError("Only data: and https:// image URLs are supported")
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Image is empty or exceeds size limit")
    content_type = _sniff_type(data)
    if content_type not in ALLOWED_TYPES:
        raise ValueError("Unsupported image format")
    return data, content_type


def upload_image(client: Any, thread_id: str, url: str, index: int) -> dict:
    """Upload one image into the thread and return a Notion attachment transcript step."""
    data, content_type = _load(url)
    space_id = client.space_id
    filename = f"image-{index + 1}.{EXTENSIONS[content_type]}"
    record = {"table": "thread", "id": thread_id, "spaceId": space_id}
    headers = client._build_thread_headers()
    headers.update({"notion-client-version": NOTION_CLIENT_VERSION, "origin": NOTION_URL})
    resp = client._scraper.post(NOTION_URL + "/api/v3/getUploadFileUrl", headers=headers, timeout=30,
                                json={"bucket": "secure", "name": filename, "contentType": content_type, "record": record})
    if resp.status_code != 200:
        raise RuntimeError(f"getUploadFileUrl returned HTTP {resp.status_code}")
    target = resp.json()
    upload = requests.post(target["signedUploadPostUrl"], data=target["fields"],
                           files={"file": (filename, data, content_type)}, timeout=60)
    if upload.status_code not in (200, 201, 204):
        raise RuntimeError(f"File upload returned HTTP {upload.status_code}")
    width, height = _dimensions(data, content_type)
    return {
        "id": str(uuid.uuid4()),
        "type": "attachment",
        "fileUrl": target["url"],
        "fileName": filename,
        "contentType": content_type,
        "permissionRecord": record,
        "metadata": {
            "attachmentRisk": "skipped", "contentType": content_type, "fileSizeBytes": len(data),
            "width": width, "height": height, "moderation": {"status": "skipped"},
            "attachmentSource": "user_upload", "guardrail": {"attachmentRisk": "skipped"},
        },
    }


def attach_images(client: Any, transcript: list, raw_messages: list, thread_type: str, existing_thread_id: Optional[str] = None) -> Optional[str]:
    """Create a thread, upload images and insert attachment steps before the last user step.

    Returns the thread id to pass to stream_response, or None when there are no images.
    Raises ValueError/RuntimeError so callers can return a clear error instead of silently
    answering without the image.
    """
    urls = extract_image_urls(raw_messages)
    if not urls:
        return None
    thread_id = existing_thread_id or str(uuid.uuid4())
    if not existing_thread_id:
        if not client._create_thread(thread_id, thread_type):
            raise RuntimeError("Unable to create a Notion thread for image attachments")
    steps = [upload_image(client, thread_id, url, i) for i, url in enumerate(urls)]
    user_positions = [i for i, step in enumerate(transcript) if step.get("type") == "user"]
    position = user_positions[-1] if user_positions else len(transcript)
    transcript[position:position] = steps
    logger.info("Attached %d image(s) to Notion thread %s", len(steps), thread_id[:8])
    return thread_id
