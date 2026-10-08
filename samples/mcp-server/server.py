"""Minimal Streamable HTTP MCP server for the secure-mcp-gateway-landing-zone demo.

This is a deliberately dependency-light (standard-library-only) reference MCP
server meant to be containerized and registered as an MCP Gateway adapter. It
implements just enough of the Model Context Protocol (JSON-RPC 2.0 over HTTP
POST to ``/mcp``) to demonstrate the pattern end-to-end:

- ``initialize`` -- returns server info/capabilities and mints a session id
  (returned as the ``mcp-session-id`` response header) so MCP Gateway's
  session-affinity layer has something to pin a caller to a specific replica.
- ``notifications/initialized`` -- acknowledged with HTTP 202 and no body.
- ``tools/list`` -- returns two generic, domain-agnostic demo tools.
- ``tools/call`` -- invokes ``echo`` or ``describe_environment``.

``describe_environment`` is the demo's centerpiece: it reports which
pod/process served the call and which forwarded-identity headers it received,
which is exactly how you visibly prove session affinity (same replica across a
session) and end-to-end identity propagation (Identity Mode B: APIM passes the
caller's Entra bearer token straight through, MCP Gateway validates it and
forwards derived identity to the adapter).

Run locally:
    python server.py            # listens on 0.0.0.0:8000 (override with PORT)

This server is intentionally synchronous/threaded (``ThreadingHTTPServer``)
rather than async -- there is no runtime dependency to install, which keeps
the container image small and the demo easy to reason about.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import signal
import socket
import sys
import threading
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import FrameType
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

LOGGER = logging.getLogger("mcp_sample_server")

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "mcp-sample-server"
SERVER_VERSION = "1.0.0"

SESSION_ID_HEADER = "mcp-session-id"
MCP_PATH = "/mcp"
HEALTHZ_PATH = "/healthz"
DEFAULT_PORT = 8000

# JSON-RPC 2.0 reserved error codes used by this server.
PARSE_ERROR = -32700
INVALID_PARAMS = -32602
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603


class UnknownToolError(Exception):
    """Raised when tools/call names a tool this server does not implement."""


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def configure_logging() -> None:
    """Configure structured-ish logging to stdout (container-friendly)."""
    logging.basicConfig(
        level=logging.INFO,
        format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}',
        stream=sys.stdout,
    )


# ---------------------------------------------------------------------------
# JSON-RPC envelope helpers
# ---------------------------------------------------------------------------


def jsonrpc_result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def jsonrpc_error(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


# ---------------------------------------------------------------------------
# Tool catalog
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "echo",
        "title": "Echo",
        "description": "Returns the given text unchanged, proving a basic MCP tool round-trip.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Text to echo back."}},
            "required": ["text"],
        },
    },
    {
        "name": "describe_environment",
        "title": "Describe Environment",
        "description": (
            "Returns non-sensitive runtime facts about the process handling this call "
            "(hostname, pod name, Python version, current UTC timestamp) plus any "
            "forwarded-identity headers (X-Mcp-UserId, X-Mcp-Roles) it received. Call "
            "this repeatedly with the same vs. a different mcp-session-id to observe "
            "session affinity and identity propagation through the governance plane."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _tool_echo(arguments: Mapping[str, Any]) -> dict[str, Any]:
    text = arguments.get("text", "")
    return {"content": [{"type": "text", "text": str(text)}], "isError": False}


def _tool_describe_environment(arguments: Mapping[str, Any], headers: Mapping[str, str]) -> dict[str, Any]:
    facts = {
        "hostname": socket.gethostname(),
        "pod_name": os.environ.get("HOSTNAME", "unknown"),
        "python_version": platform.python_version(),
        "utc_timestamp": datetime.now(timezone.utc).isoformat(),
        "forwarded_user_id": headers.get("x-mcp-userid"),
        "forwarded_roles": headers.get("x-mcp-roles"),
    }
    return {"content": [{"type": "text", "text": json.dumps(facts)}], "isError": False}


_TOOL_HANDLERS: dict[str, Callable[[Mapping[str, Any], Mapping[str, str]], dict[str, Any]]] = {
    "echo": lambda arguments, headers: _tool_echo(arguments),
    "describe_environment": _tool_describe_environment,
}


# ---------------------------------------------------------------------------
# MCP method handlers
# ---------------------------------------------------------------------------


def handle_initialize(params: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    }


def handle_tools_list(params: Mapping[str, Any] | None) -> dict[str, Any]:
    return {"tools": TOOL_DEFINITIONS}


def handle_tools_call(params: Mapping[str, Any] | None, headers: Mapping[str, str]) -> dict[str, Any]:
    params = params or {}
    name = params.get("name")
    arguments = params.get("arguments") or {}
    handler = _TOOL_HANDLERS.get(name)
    if handler is None:
        raise UnknownToolError(f"Unknown tool: {name!r}")
    return handler(arguments, headers)


def dispatch(message: Any, headers: Mapping[str, str]) -> dict[str, Any] | None:
    """Dispatch one parsed JSON-RPC message and return its response (or None).

    Returns ``None`` when no response should be sent -- i.e. the message was a
    valid JSON-RPC *notification* (no ``id``), per the JSON-RPC 2.0 spec.
    """
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "method" not in message:
        request_id = message.get("id") if isinstance(message, dict) else None
        return jsonrpc_error(request_id, INVALID_PARAMS, "Invalid JSON-RPC 2.0 request envelope")

    method = message.get("method")
    is_notification = "id" not in message
    request_id = message.get("id")

    if method == "initialize":
        return jsonrpc_result(request_id, handle_initialize(message.get("params")))

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return jsonrpc_result(request_id, handle_tools_list(message.get("params")))

    if method == "tools/call":
        try:
            result = handle_tools_call(message.get("params"), headers)
        except UnknownToolError as exc:
            return jsonrpc_error(request_id, INVALID_PARAMS, str(exc))
        return jsonrpc_result(request_id, result)

    if is_notification:
        # Unknown notifications get no response at all, per JSON-RPC 2.0.
        return None
    return jsonrpc_error(request_id, METHOD_NOT_FOUND, f"Method not found: {method}")


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


class MCPRequestHandler(BaseHTTPRequestHandler):
    """Handles GET /healthz and POST /mcp (Streamable HTTP, JSON-RPC 2.0)."""

    protocol_version = "HTTP/1.1"
    server_version = f"{SERVER_NAME}/{SERVER_VERSION}"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        # Avoid BaseHTTPRequestHandler's default reverse-DNS lookup on every request.
        LOGGER.info("%s - %s", self.client_address[0], format % args)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming convention
        path = urlsplit(self.path).path
        if path == HEALTHZ_PATH:
            self._write_json(HTTPStatus.OK, {"status": "ok"})
            return
        self._write_json(HTTPStatus.NOT_FOUND, {"error": f"not found: {path}"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming convention
        path = urlsplit(self.path).path
        if path != MCP_PATH:
            self._write_json(HTTPStatus.NOT_FOUND, {"error": f"not found: {path}"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = 0
        raw_body = self.rfile.read(length) if length else b""

        try:
            message: Any = json.loads(raw_body) if raw_body else {}
        except json.JSONDecodeError as exc:
            self._write_jsonrpc(HTTPStatus.OK, jsonrpc_error(None, PARSE_ERROR, f"Parse error: {exc}"))
            return

        forwarded_headers = {key.lower(): value for key, value in self.headers.items()}
        response = dispatch(message, forwarded_headers)

        session_id: str | None = None
        if isinstance(message, dict) and message.get("method") == "initialize":
            session_id = uuid.uuid4().hex

        if response is None:
            # Valid notification: MCP/JSON-RPC expects no body back.
            self.send_response(HTTPStatus.ACCEPTED)
            if session_id:
                self.send_header(SESSION_ID_HEADER, session_id)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self._write_jsonrpc(HTTPStatus.OK, response, session_id=session_id)

    def _write_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_jsonrpc(self, status: HTTPStatus, payload: dict[str, Any], session_id: str | None = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if session_id:
            self.send_header(SESSION_ID_HEADER, session_id)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def create_server(host: str = "0.0.0.0", port: int | None = None) -> ThreadingHTTPServer:
    """Create (but do not start) the HTTP server. ``port=0`` binds an ephemeral port."""
    resolved_port = port if port is not None else int(os.environ.get("PORT", str(DEFAULT_PORT)))
    return ThreadingHTTPServer((host, resolved_port), MCPRequestHandler)


def run_server(host: str = "0.0.0.0", port: int | None = None) -> None:
    """Run the server until SIGTERM/SIGINT, then shut down gracefully."""
    configure_logging()
    server = create_server(host, port)
    bound_port = server.server_address[1]
    LOGGER.info("mcp-sample-server listening on %s:%s", host, bound_port)

    def _handle_shutdown_signal(signum: int, frame: FrameType | None) -> None:
        LOGGER.info("received signal %s, shutting down", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)

    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        LOGGER.info("mcp-sample-server stopped")


if __name__ == "__main__":
    run_server()
