"""Reconnect an existing personal MCP module after its HTTPS endpoint changes."""

import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.notion_client import NotionOpusAPI, NOTION_URL, NOTION_CLIENT_VERSION


def reconnect(url):
    endpoint = urlsplit(url)
    if endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password:
        raise ValueError("An HTTPS MCP endpoint is required")
    state = Path(os.getenv("MCP_BRIDGE_STATE_DIR", "data/mcp-bridge"))
    connection = json.loads((state / "connection.json").read_text())
    accounts = json.loads(Path(os.getenv("MCP_BRIDGE_ACCOUNTS_FILE", "accounts.json")).read_text())
    account = next(a for a in accounts if a["user_id"] == connection["userId"]
                   and a["space_id"] == connection["spaceId"])
    token = Path(os.environ["MCP_BRIDGE_TOKEN_FILE"]).read_text().strip()
    client = NotionOpusAPI(account)
    headers = client._build_thread_headers()
    headers.update({"notion-client-version": NOTION_CLIENT_VERSION, "origin": NOTION_URL,
                    "referer": NOTION_URL + "/chat"})
    auth_headers = [{"name": "Authorization", "value": "Bearer " + token}]

    def post(event, payload):
        response = client._scraper.post(NOTION_URL + "/api/v3/" + event, headers=headers,
                                        json=payload, timeout=30, allow_redirects=False)
        if response.status_code != 200:
            raise RuntimeError(f"Notion {event} returned HTTP {response.status_code}")
        result = response.json()
        if not result.get("success"):
            raise RuntimeError(f"Notion {event} did not confirm success")
        return result

    try:
        validated = post("validateMcpConnection", {"spaceId": connection["spaceId"], "serverUrl": url,
                         "authHeaders": auth_headers, "initiationContext": "reconnect"})
        tools = {tool["name"] for tool in validated.get("tools", [])}
        if not {"list_client_tools", "call_client_tool"}.issubset(tools):
            raise RuntimeError("MCP endpoint does not expose the client tool bridge")
        post("postWorkflowsMcpServerConnect", {
            "spaceId": connection["spaceId"], "integrationId": connection["moduleId"],
            "authHeaders": auth_headers, "initiationContext": "reconnect",
            "moduleDefinition": {"name": "Sam Local Tools", "serverUrl": url,
                                 "runReadToolsAutomatically": True, "runWriteToolsAutomatically": True},
        })
        published = {"url": url, "moduleId": connection["moduleId"], "updated_at": int(time.time())}
        target = state / "published.json"
        temporary = state / "published.tmp"
        temporary.write_text(json.dumps(published))
        temporary.chmod(0o600)
        temporary.replace(target)
        print("Personal MCP endpoint verified and reconnected", flush=True)
    finally:
        client._scraper.close()


if __name__ == "__main__":
    try:
        reconnect(sys.argv[1])
    except Exception as exc:
        # Do not print HTTP bodies, headers or credentials on failure.
        print(f"MCP reconnect failed ({type(exc).__name__})", file=sys.stderr)
        sys.exit(1)
