#!/usr/bin/env python3
"""Local stdio MCP adapter backed by Neuronz.ai's browser OAuth grant.

Claude Code and omp start this bundled process. It exposes a read-only
auth-status tool before sign-in, forwards the remote Neuronz.ai tool catalog
after sign-in, and shares the same refreshable credential as lifecycle hooks.
Login and logout remain explicit host workflows, never model-callable tools.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core import auth  # noqa: E402


AUTH_TOOLS = [
    {
        "name": "neuronzai_auth_status",
        "description": "Check whether this machine is signed in to Neuronz.ai without exposing credentials.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


def _plugin_version() -> str:
    try:
        return str(json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())["version"])
    except (OSError, ValueError, KeyError, TypeError):
        return "unknown"


def _safe_error(message: str) -> dict:
    return {
        "content": [{"type": "text", "text": message}],
        "isError": True,
    }


def _text_result(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}]}


def _parse_http_body(raw: bytes, content_type: str, request_id):
    text = raw.decode("utf-8", errors="replace")
    if "text/event-stream" not in content_type and not text.lstrip().startswith("event:"):
        return json.loads(text) if text.strip() else None
    events = []
    data_lines = []
    for line in text.splitlines():
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif not line and data_lines:
            with contextlib.suppress(ValueError):
                events.append(json.loads("\n".join(data_lines)))
            data_lines = []
    if data_lines:
        with contextlib.suppress(ValueError):
            events.append(json.loads("\n".join(data_lines)))
    for event in events:
        if request_id is None or event.get("id") == request_id:
            return event
    return events[0] if events else None


class McpProxy:
    def __init__(self):
        self.protocol_version = "2025-06-18"
        self.write_lock = threading.Lock()
        self.initialized = False
        self.marker = auth.marker_revision()

    def write(self, payload: dict) -> None:
        with self.write_lock:
            sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
            sys.stdout.flush()

    def start_auth_monitor(self) -> None:
        def monitor():
            while True:
                time.sleep(1)
                revision = auth.marker_revision()
                if revision == self.marker:
                    continue
                self.marker = revision
                if self.initialized:
                    self.write({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})

        threading.Thread(target=monitor, name="neuronzai-auth-monitor", daemon=True).start()

    def remote(self, message: dict, *, retry=True):
        token = auth.access_token()
        if not token:
            raise auth.AuthError("Sign in to Neuronz.ai before using its remote tools.")
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "mcp-protocol-version": self.protocol_version,
        }
        profile = (os.environ.get("NEURONZAI_PROFILE") or "").strip()
        if profile:
            headers["X-Profile"] = profile
        request = urllib.request.Request(
            f"{auth.base_url()}/mcp",
            data=json.dumps(message).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return _parse_http_body(
                    response.read(), response.headers.get("Content-Type", ""), message.get("id")
                )
        except urllib.error.HTTPError as error:
            if error.code == 401 and retry and not auth.environment_token():
                if auth.access_token(force_refresh=True):
                    return self.remote(message, retry=False)
            raise auth.AuthError(f"Neuronz.ai MCP request failed ({error.code}).") from None
        except (OSError, urllib.error.URLError) as error:
            raise auth.AuthError(f"Could not reach the Neuronz.ai MCP server: {error}.") from None
        except ValueError:
            raise auth.AuthError("The Neuronz.ai MCP server returned an invalid response.") from None

    def auth_call(self, name: str) -> dict:
        if name != "neuronzai_auth_status":
            return _safe_error("Unknown Neuronz.ai authentication tool.")
        state = auth.status(verify=True)
        if state.get("authenticated"):
            if state.get("source") == "environment" and state.get("message"):
                return _text_result(f"{state['message']}\nServer: {state.get('issuer')}")
            return _text_result(
                f"Neuronz.ai is authenticated via {state.get('source')} for {state.get('issuer')}."
            )
        if state.get("reauthentication_required"):
            return _safe_error("The saved Neuronz.ai grant expired; sign in again.")
        return _safe_error("Neuronz.ai is not signed in on this machine.")

    def handle(self, message: dict):
        request_id = message.get("id")
        method = message.get("method")
        if method == "initialize":
            params = message.get("params") or {}
            requested = params.get("protocolVersion")
            if isinstance(requested, str) and requested:
                self.protocol_version = requested
            self.initialized = True
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": self.protocol_version,
                    "capabilities": {"tools": {"listChanged": True}},
                    "serverInfo": {"name": "neuronzai", "version": _plugin_version()},
                },
            }
        if method in {"notifications/initialized", "notifications/cancelled"}:
            return None
        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if method == "tools/list":
            remote_tools = []
            remote_result = {}
            if auth.access_token():
                try:
                    remote_response = self.remote(message)
                    remote_result = ((remote_response or {}).get("result") or {})
                    remote_tools = remote_result.get("tools") or []
                except auth.AuthError:
                    remote_tools = []
            names = {tool.get("name") for tool in AUTH_TOOLS}
            remote_tools = [tool for tool in remote_tools if tool.get("name") not in names]
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {**remote_result, "tools": remote_tools + AUTH_TOOLS},
            }
        if method == "tools/call":
            name = str((message.get("params") or {}).get("name") or "")
            if name in {tool["name"] for tool in AUTH_TOOLS}:
                return {"jsonrpc": "2.0", "id": request_id, "result": self.auth_call(name)}
        try:
            return self.remote(message)
        except auth.AuthError as error:
            if request_id is None:
                return None
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32001, "message": str(error)},
            }


def main() -> int:
    proxy = McpProxy()
    proxy.start_auth_monitor()
    for line in sys.stdin:
        message = None
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError
            response = proxy.handle(message)
            if response is not None:
                proxy.write(response)
        except ValueError:
            proxy.write(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "Parse error"},
                }
            )
        except Exception as error:
            print(f"[neuronzai-mcp] unexpected adapter error: {type(error).__name__}", file=sys.stderr)
            request_id = message.get("id") if isinstance(message, dict) else None
            if request_id is not None:
                proxy.write(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32603, "message": "Internal MCP adapter error"},
                    }
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
