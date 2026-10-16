"""Supervise a temporary Cloudflare endpoint and reconnect the existing MCP module.

Use a named tunnel with a fixed hostname when available. This fallback is not an SLA.
"""

import os
import re
import signal
import subprocess
import sys
import time


def main():
    binary = os.getenv("CLOUDFLARED_BIN", "/opt/notion-mcp/cloudflared")
    token_file = os.getenv("CF_TUNNEL_TOKEN_FILE", "/opt/notion-mcp/state/cf-tunnel-token")
    fixed_url = os.getenv("MCP_PUBLIC_URL", "https://mcp.example.com/notion-mcp/")
    env = dict(os.environ)
    if os.path.exists(token_file):
        # Named tunnel with a fixed hostname; the token stays in the environment, not argv.
        env["TUNNEL_TOKEN"] = open(token_file).read().strip()
        command = [binary, "tunnel", "--no-autoupdate", "--protocol", "http2", "run"]
    else:
        fixed_url = None
        command = [binary, "tunnel", "--no-autoupdate",
                   "--url", "http://127.0.0.1:8898", "--http-host-header", "localhost:8898", "--protocol", "http2"]
    tunnel = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)

    def stop(signum, frame):
        tunnel.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    registered = False
    try:
        for line in tunnel.stdout:
            match = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com\b", line)
            ready = "Registered tunnel connection" in line if fixed_url else bool(match)
            if not registered and ready:
                endpoint = fixed_url or match.group(0) + "/notion-mcp/"
                time.sleep(3)
                for attempt in range(6):
                    if tunnel.poll() is not None:
                        break
                    result = subprocess.run([
                        "docker", "exec", "notion_mcp_service", "python", "scripts/reconnect_mcp.py", endpoint,
                    ], timeout=85, capture_output=True)
                    if result.returncode == 0:
                        registered = True
                        print("MCP tunnel connected and personal module updated", flush=True)
                        break
                    print(f"MCP registration attempt {attempt + 1} failed", flush=True)
                    time.sleep(5)
                if not registered:
                    raise RuntimeError("MCP tunnel could not be registered")
        return tunnel.wait() or 1
    finally:
        if tunnel.poll() is None:
            tunnel.terminate()
        try:
            tunnel.wait(timeout=10)
        except subprocess.TimeoutExpired:
            tunnel.kill()
            tunnel.wait()


if __name__ == "__main__":
    sys.exit(main())
