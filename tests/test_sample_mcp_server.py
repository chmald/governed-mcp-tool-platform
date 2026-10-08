"""Tests for samples/mcp-server/server.py.

Starts the real ``ThreadingHTTPServer`` on an ephemeral port (``port=0``) in a
background thread for each test via the ``running_server`` fixture, so
assertions cover actual HTTP-level behavior (status codes, response headers)
rather than only the internal ``dispatch()`` function. The server thread is
always torn down in the fixture's ``finally`` block.
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from typing import Any, Iterator

import pytest

_SAMPLE_SERVER_DIR = Path(__file__).resolve().parent.parent / "samples" / "mcp-server"
if str(_SAMPLE_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SAMPLE_SERVER_DIR))

import server as mcp_server  # noqa: E402  (import after sys.path patch)


@pytest.fixture()
def running_server() -> Iterator[str]:
    httpd = mcp_server.create_server(host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield base_url
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def _post_jsonrpc(
    base_url: str, message: dict[str, Any], extra_headers: dict[str, str] | None = None
) -> tuple[int, Message, bytes]:
    return _post_raw(base_url, json.dumps(message).encode("utf-8"), extra_headers)


def _post_raw(
    base_url: str, raw_body: bytes, extra_headers: dict[str, str] | None = None
) -> tuple[int, Message, bytes]:
    request = urllib.request.Request(
        f"{base_url}/mcp",
        data=raw_body,
        method="POST",
        headers={"Content-Type": "application/json", **(extra_headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()
        return exc.code, exc.headers, body


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


def test_healthz_returns_200(running_server: str) -> None:
    with urllib.request.urlopen(f"{running_server}/healthz", timeout=5) as response:
        assert response.status == 200
        payload = json.loads(response.read())
    assert payload["status"] == "ok"


# ---------------------------------------------------------------------------
# initialize
# ---------------------------------------------------------------------------


def test_initialize_returns_result_and_session_header(running_server: str) -> None:
    status, headers, body = _post_jsonrpc(
        running_server, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    assert status == 200
    payload = json.loads(body)
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == 1
    assert "result" in payload
    assert payload["result"]["protocolVersion"] == mcp_server.PROTOCOL_VERSION
    assert payload["result"]["serverInfo"]["name"] == mcp_server.SERVER_NAME

    session_id = headers.get(mcp_server.SESSION_ID_HEADER)
    assert session_id
    assert len(session_id) == 32  # uuid4().hex


def test_each_initialize_call_gets_a_fresh_session_id(running_server: str) -> None:
    _, headers1, _ = _post_jsonrpc(running_server, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    _, headers2, _ = _post_jsonrpc(running_server, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    session_1 = headers1.get(mcp_server.SESSION_ID_HEADER)
    session_2 = headers2.get(mcp_server.SESSION_ID_HEADER)
    assert session_1 and session_2
    assert session_1 != session_2


# ---------------------------------------------------------------------------
# notifications/initialized
# ---------------------------------------------------------------------------


def test_notifications_initialized_returns_202_with_empty_body(running_server: str) -> None:
    status, _, body = _post_jsonrpc(running_server, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert status == 202
    assert body == b""


# ---------------------------------------------------------------------------
# tools/list
# ---------------------------------------------------------------------------


def test_tools_list_returns_both_tools_with_valid_json_schema(running_server: str) -> None:
    status, _, body = _post_jsonrpc(running_server, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert status == 200
    payload = json.loads(body)
    tools = payload["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert names == {"echo", "describe_environment"}
    for tool in tools:
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert "properties" in schema
        assert isinstance(tool["description"], str) and tool["description"]


# ---------------------------------------------------------------------------
# tools/call
# ---------------------------------------------------------------------------


def test_tools_call_echo_round_trips(running_server: str) -> None:
    status, _, body = _post_jsonrpc(
        running_server,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "echo", "arguments": {"text": "hello-mcp"}},
        },
    )
    assert status == 200
    payload = json.loads(body)
    assert payload["result"]["isError"] is False
    assert payload["result"]["content"][0]["text"] == "hello-mcp"


def test_tools_call_describe_environment_reports_forwarded_identity(running_server: str) -> None:
    status, _, body = _post_jsonrpc(
        running_server,
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "describe_environment", "arguments": {}},
        },
        extra_headers={"X-Mcp-UserId": "user-123", "X-Mcp-Roles": "mcp.engineer"},
    )
    assert status == 200
    payload = json.loads(body)
    facts = json.loads(payload["result"]["content"][0]["text"])
    assert facts["forwarded_user_id"] == "user-123"
    assert facts["forwarded_roles"] == "mcp.engineer"
    assert facts["python_version"]
    assert facts["utc_timestamp"]
    assert facts["hostname"]


def test_tools_call_describe_environment_without_identity_headers_reports_none(running_server: str) -> None:
    status, _, body = _post_jsonrpc(
        running_server,
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "describe_environment"}},
    )
    assert status == 200
    payload = json.loads(body)
    facts = json.loads(payload["result"]["content"][0]["text"])
    assert facts["forwarded_user_id"] is None
    assert facts["forwarded_roles"] is None


def test_tools_call_unknown_tool_returns_jsonrpc_error(running_server: str) -> None:
    status, _, body = _post_jsonrpc(
        running_server,
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "does-not-exist", "arguments": {}}},
    )
    payload = json.loads(body)
    assert "result" not in payload
    assert payload["error"]["code"] == mcp_server.INVALID_PARAMS


# ---------------------------------------------------------------------------
# Protocol-error handling
# ---------------------------------------------------------------------------


def test_unknown_method_returns_method_not_found(running_server: str) -> None:
    status, _, body = _post_jsonrpc(running_server, {"jsonrpc": "2.0", "id": 6, "method": "not/a/real/method"})
    payload = json.loads(body)
    assert payload["error"]["code"] == mcp_server.METHOD_NOT_FOUND


def test_malformed_json_returns_parse_error(running_server: str) -> None:
    status, _, body = _post_raw(running_server, b"{not valid json")
    payload = json.loads(body)
    assert payload["error"]["code"] == mcp_server.PARSE_ERROR


def test_unknown_path_returns_404(running_server: str) -> None:
    request = urllib.request.Request(f"{running_server}/not-mcp", data=b"{}", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    assert status == 404


# ---------------------------------------------------------------------------
# dispatch() unit tests (direct, no HTTP layer)
# ---------------------------------------------------------------------------


def test_dispatch_directly_for_initialize() -> None:
    response = mcp_server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, headers={})
    assert response is not None
    assert response["result"]["protocolVersion"] == mcp_server.PROTOCOL_VERSION


def test_dispatch_directly_for_notification_returns_none() -> None:
    assert mcp_server.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"}, headers={}) is None


def test_dispatch_directly_for_unknown_notification_returns_none() -> None:
    assert mcp_server.dispatch({"jsonrpc": "2.0", "method": "notifications/whatever"}, headers={}) is None
