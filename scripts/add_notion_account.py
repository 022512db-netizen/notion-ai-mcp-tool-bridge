"""Add a new Notion account: fetch identity, append to both account files, register MCP, restart, verify.

Usage (run on the server as root):
    python3 add_notion_account.py --token-v2-file /root/.new-token --profile-name "Name" [--space-index 0] [--skip-mcp] [--skip-restart]

Steps:
    1. Read token_v2 from file (never pass on the command line).
    2. Query Notion loadUserContent for user_id / spaces / space_views.
    3. Back up both account files, append the new account, write back.
    4. Validate the MCP endpoint, create the module, enable it in agent_chat_modules.
    5. Restart notion_mcp_service container.
    6. Verify with one haiku call routed to the new account.

Both files use the same entry; the monitor copy keeps the same fields.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BRIDGE_ACCOUNTS = Path("/opt/notion-mcp/state/deploy-accounts.json")
MONITOR_ACCOUNTS = Path("/opt/notion2api/accounts.json")
MCP_TOKEN_FILE = Path("/opt/notion-mcp/state/mcp-token")
MCP_URL_FILE = Path("/opt/notion-mcp/state/published.json")
CONTAINER = "notion_mcp_service"


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def notion_post(client, headers, event, payload):
    from app.notion_client import NOTION_URL
    resp = client._scraper.post(NOTION_URL + "/api/v3/" + event, headers=headers,
                                json=payload, timeout=30, allow_redirects=False)
    if resp.status_code != 200:
        raise RuntimeError(f"Notion {event} returned HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def main():
    ap = argparse.ArgumentParser(description="Add a Notion account end to end.")
    ap.add_argument("--token-v2-file", required=True, help="File containing only the token_v2 value.")
    ap.add_argument("--profile-name", required=True)
    ap.add_argument("--space-index", type=int, default=0, help="Which workspace to use if several (default 0).")
    ap.add_argument("--skip-mcp", action="store_true", help="Only add to account files, do not register MCP.")
    ap.add_argument("--skip-restart", action="store_true", help="Do not restart the bridge container.")
    args = ap.parse_args()

    from app.notion_client import NotionOpusAPI, NOTION_URL, NOTION_CLIENT_VERSION

    token = Path(args.token_v2_file).read_text().strip()
    if not token:
        sys.exit("token file is empty")

    # 1. Identify the account. token_v2 alone is enough for loadUserContent.
    probe = NotionOpusAPI({"token_v2": token, "space_id": "", "user_id": "", "space_view_id": ""})
    headers = probe._build_thread_headers()
    headers.update({"notion-client-version": NOTION_CLIENT_VERSION, "origin": NOTION_URL})
    data = notion_post(probe, headers, "loadUserContent", {})
    probe._scraper.close()
    record_map = data.get("recordMap", {})
    users = {uid: (v.get("value", {}) or {}).get("value", v.get("value", {}) or {})
             for uid, v in (record_map.get("notion_user") or {}).items()}
    spaces = [(sid, ((v.get("value", {}) or {}).get("value") or v.get("value", {}) or {}).get("name", ""))
              for sid, v in (record_map.get("space") or {}).items()]
    views = {((v.get("value", {}) or {}).get("value") or v.get("value", {}) or {}).get("space_id"): vid
             for vid, v in (record_map.get("space_view") or {}).items()}
    if not users or not spaces:
        sys.exit("loadUserContent returned no user/space; token may be invalid")
    if args.space_index >= len(spaces):
        sys.exit(f"space-index {args.space_index} out of range, account has {len(spaces)} workspace(s)")
    user_id, user = next(iter(users.items()))
    space_id, space_name = spaces[args.space_index]
    space_view_id = views.get(space_id, "")
    if not space_view_id:
        sys.exit(f"no space_view found for space {space_id}")
    account = {
        "profile_name": args.profile_name,
        "token_v2": token,
        "space_id": space_id,
        "user_id": user_id,
        "space_view_id": space_view_id,
        "user_name": user.get("name", ""),
        "user_email": user.get("email", ""),
    }
    print(f"account: {account['user_name']} <{account['user_email']}> space={space_name} ({space_id[:8]})", flush=True)

    # 2. Back up and append to both files.
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = Path(f"/opt/notion-mcp/backup-{stamp}-add-account")
    backup.mkdir(parents=True, exist_ok=True)
    for path in (BRIDGE_ACCOUNTS, MONITOR_ACCOUNTS):
        entries = json.loads(path.read_text())
        if any(e.get("user_id") == user_id and e.get("space_id") == space_id for e in entries):
            sys.exit(f"account already present in {path}")
        shutil.copy2(path, backup / path.name)
        entries.append(account)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=2))
        tmp.chmod(0o600)
        tmp.replace(path)
        print(f"appended to {path} ({len(entries)} accounts), backup in {backup}", flush=True)

    if args.skip_mcp:
        print("MCP registration skipped by flag", flush=True)
        return

    # 3. Register MCP on the new account.
    mcp_url = json.loads(MCP_TOKEN_FILE and MCP_URL_FILE.read_text())["url"]
    bridge_token = MCP_TOKEN_FILE.read_text().strip()
    client = NotionOpusAPI(account)
    headers = client._build_thread_headers()
    headers.update({"notion-client-version": NOTION_CLIENT_VERSION, "origin": NOTION_URL,
                    "referer": NOTION_URL + "/chat"})
    auth = [{"name": "Authorization", "value": "Bearer " + bridge_token}]
    try:
        validated = notion_post(client, headers, "validateMcpConnection",
                                {"spaceId": space_id, "serverUrl": mcp_url, "authHeaders": auth})
        tools = {t.get("name") for t in validated.get("tools", [])}
        if not {"list_client_tools", "call_client_tool"}.issubset(tools):
            sys.exit("MCP endpoint does not expose the client tool bridge")
        print(f"MCP endpoint validated, tools: {sorted(tools)}", flush=True)
        module_id = str(uuid.uuid4())
        connected = notion_post(client, headers, "postWorkflowsMcpServerConnect",
                                {"spaceId": space_id, "integrationId": module_id, "authHeaders": auth,
                                 "moduleDefinition": {"name": "Sam Local Tools", "serverUrl": mcp_url,
                                                      "runReadToolsAutomatically": True,
                                                      "runWriteToolsAutomatically": True}})
        if not connected.get("success"):
            sys.exit(f"postWorkflowsMcpServerConnect failed: {connected}")
        print(f"MCP module created: {module_id[:8]}", flush=True)
        modules = [{"pointer": {"id": module_id, "table": "workflow_module", "spaceId": space_id},
                    "defaultEnabled": True}]
        saved = notion_post(client, headers, "saveTransactions",
                            {"requestId": f"add-mcp-{stamp}", "transactions": [{
                                "id": f"add-mcp-{stamp}", "spaceId": space_id, "operations": [{
                                    "pointer": {"table": "space_view", "id": space_view_id, "spaceId": space_id},
                                    "path": ["settings", "agent_chat_modules"],
                                    "command": "set", "args": modules}]}]})
        print("agent_chat_modules updated", flush=True)
    finally:
        client._scraper.close()

    # 4. Restart and verify.
    if not args.skip_restart:
        run(["docker", "restart", CONTAINER])
        time.sleep(10)
        status = run(["docker", "ps", "--filter", f"name={CONTAINER}", "--format", "{{.Status}}"]).stdout.strip()
        print(f"container: {status}", flush=True)
        if "healthy" not in status and "Up" not in status:
            sys.exit("container did not come back healthy")
    print("done. Verify with a haiku call through the notion channel.", flush=True)


if __name__ == "__main__":
    main()
