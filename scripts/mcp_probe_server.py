"""Authenticated, side-effect-free MCP probe for Notion connection validation."""

import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import secrets

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.responses import JSONResponse


STATE_DIR = Path(__file__).resolve().parents[1] / "data" / "mcp-bridge"
STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
TOKEN_FILE = STATE_DIR / "mcp-token"
if not TOKEN_FILE.exists():
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as token_file:
        token_file.write(secrets.token_urlsafe(32))
TOKEN = TOKEN_FILE.read_text().strip()
if len(TOKEN) < 32:
    raise ValueError("MCP token must contain at least 32 characters")

PUBLIC_HOST = os.getenv("MCP_PUBLIC_HOST", "")

mcp = FastMCP(
    "Sam Local Tools",
    instructions="Use bridge_probe to check the local tool connection.",
    streamable_http_path="/notion-mcp",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        allowed_hosts=["127.0.0.1:*", "localhost:*"] + ([PUBLIC_HOST] if PUBLIC_HOST else []),
        allowed_origins=[f"https://{PUBLIC_HOST}"] if PUBLIC_HOST else [],
    ),
)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def bridge_probe(text: str) -> dict:
    """Echo a short test string and its SHA-256 hash; no external side effects."""
    if len(text) > 256:
        raise ValueError("Test text is limited to 256 characters")
    digest = hashlib.sha256(text.encode()).hexdigest()
    logging.getLogger("mcp-probe").info("bridge_probe sha256=%s", digest)
    return {"text": text, "sha256": digest}


class BearerOnly:
    def __init__(self, wrapped):
        self.wrapped = wrapped

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            expected = f"Bearer {TOKEN}".encode()
            if not hmac.compare_digest(headers.get(b"authorization", b""), expected):
                response = JSONResponse(
                    {"error": "Unauthorized"}, status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                return await response(scope, receive, send)
        await self.wrapped(scope, receive, send)


app = BearerOnly(mcp.streamable_http_app())
