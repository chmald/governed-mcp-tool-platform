"""Tests for scripts/validate_mcp_endpoint.py.

No real network calls are made anywhere in this suite. HTTP-shaped behavior
(``classify_failure``, header construction, JSON-RPC envelope shape, SSE
parsing) is exercised against plain data structures and in-memory
``httpx.Response`` objects constructed directly (no sockets involved), never a
live ``httpx.Client``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import validate_mcp_endpoint as vme  # noqa: E402  (import after sys.path patch)


# ---------------------------------------------------------------------------
# SSE `data:` line parsing
# ---------------------------------------------------------------------------


def test_sse_single_event_simple_json() -> None:
    lines = ['data: {"jsonrpc": "2.0", "id": 1, "result": {}}', ""]
    events = list(vme.iter_sse_events(lines))
    assert len(events) == 1
    assert json.loads(events[0].data) == {"jsonrpc": "2.0", "id": 1, "result": {}}


def test_sse_multi_line_data_is_joined_with_newline() -> None:
    lines = [
        'data: {"jsonrpc": "2.0",',
        'data: "id": 1,',
        'data: "result": {"ok": true}}',
        "",
    ]
    events = list(vme.iter_sse_events(lines))
    assert len(events) == 1
    assert json.loads(events[0].data) == {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}


def test_sse_comment_and_heartbeat_lines_are_ignored() -> None:
    lines = [
        ": heartbeat",
        'data: {"jsonrpc": "2.0", "id": 1, "result": {}}',
        ": another comment",
        "",
    ]
    events = list(vme.iter_sse_events(lines))
    assert len(events) == 1
    assert json.loads(events[0].data)["id"] == 1


def test_sse_heartbeat_only_produces_no_event() -> None:
    lines = [": heartbeat", ": still just a heartbeat", ""]
    events = list(vme.iter_sse_events(lines))
    assert events == []


def test_sse_multiple_events_separated_by_blank_lines() -> None:
    lines = [
        "event: message",
        'data: {"n": 1}',
        "",
        ": heartbeat between events",
        'data: {"n": 2}',
        "",
    ]
    events = list(vme.iter_sse_events(lines))
    assert [json.loads(e.data)["n"] for e in events] == [1, 2]
    assert events[0].event == "message"


def test_sse_trailing_event_without_blank_line_is_still_yielded() -> None:
    """A stream that ends mid-event (no final blank line) must still flush."""
    lines = ['data: {"n": 42}']
    events = list(vme.iter_sse_events(lines))
    assert len(events) == 1
    assert json.loads(events[0].data) == {"n": 42}


def test_sse_id_field_is_captured() -> None:
    lines = ["id: abc123", 'data: {"n": 1}', ""]
    events = list(vme.iter_sse_events(lines))
    assert events[0].id == "abc123"


def test_parse_mcp_message_handles_plain_json_body() -> None:
    payload = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
    response = httpx.Response(200, headers={"content-type": "application/json"}, content=json.dumps(payload).encode())
    assert vme.parse_mcp_message(response) == payload


def test_parse_mcp_message_handles_sse_body() -> None:
    payload = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
    body = f"data: {json.dumps(payload)}\n\n"
    response = httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())
    assert vme.parse_mcp_message(response) == payload


def test_parse_mcp_message_skips_heartbeats_in_sse_body() -> None:
    payload = {"jsonrpc": "2.0", "id": 1, "result": {}}
    body = f": heartbeat\n\ndata: {json.dumps(payload)}\n\n"
    response = httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())
    assert vme.parse_mcp_message(response) == payload


def test_parse_mcp_message_returns_none_for_empty_body() -> None:
    response = httpx.Response(202, headers={"content-type": "application/json"}, content=b"")
    assert vme.parse_mcp_message(response) is None


# ---------------------------------------------------------------------------
# JSON-RPC envelope construction
# ---------------------------------------------------------------------------


def test_build_initialize_request_shape() -> None:
    req = vme.build_initialize_request()
    assert req["jsonrpc"] == "2.0"
    assert req["method"] == "initialize"
    assert req["id"] == 1
    assert req["params"]["protocolVersion"] == vme.DEFAULT_PROTOCOL_VERSION
    assert req["params"]["clientInfo"] == {"name": vme.DEFAULT_CLIENT_NAME, "version": vme.DEFAULT_CLIENT_VERSION}
    assert req["params"]["capabilities"] == {}


def test_build_initialized_notification_has_no_id() -> None:
    note = vme.build_initialized_notification()
    assert note == {"jsonrpc": "2.0", "method": "notifications/initialized"}
    assert "id" not in note


def test_build_tools_list_request_shape() -> None:
    req = vme.build_tools_list_request(request_id=99)
    assert req == {"jsonrpc": "2.0", "id": 99, "method": "tools/list"}


def test_build_tools_call_request_shape() -> None:
    req = vme.build_tools_call_request("echo", {"text": "hi"}, request_id=7)
    assert req["jsonrpc"] == "2.0"
    assert req["id"] == 7
    assert req["method"] == "tools/call"
    assert req["params"] == {"name": "echo", "arguments": {"text": "hi"}}


def test_build_tools_call_request_defaults_arguments_to_empty_dict() -> None:
    req = vme.build_tools_call_request("describe_environment")
    assert req["params"]["arguments"] == {}


# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "secret",
    [
        "abcd1234cd12",
        "a-very-long-subscription-key-1234567890abcdef",
        "shortkey",
        "x",
    ],
)
def test_mask_secret_never_leaks_full_value(secret: str) -> None:
    masked = vme.mask_secret(secret)
    assert secret not in masked
    assert masked != secret


def test_mask_secret_shows_only_short_suffix() -> None:
    masked = vme.mask_secret("abcd1234cd12")
    assert masked.endswith("cd12")
    assert masked.count("*") >= 4
    assert set(masked[:-4]) == {"*"}


def test_mask_secret_of_value_no_longer_than_visible_window_reveals_nothing() -> None:
    masked = vme.mask_secret("cd12")  # length == default visible window (4)
    assert masked == "****"
    assert "cd12" not in masked


def test_mask_secret_empty_or_none_returns_empty_string() -> None:
    assert vme.mask_secret(None) == ""
    assert vme.mask_secret("") == ""


def test_render_report_dict_never_leaks_subscription_key() -> None:
    report = vme.ValidationReport(endpoint="https://example.test/mcp/demo")
    report.phases.append(vme.PhaseResult("HTTP status", True, "200"))
    rendered = vme.render_report_dict(
        report, subscription_key="supersecretkeyvalue1234", subscription_key_header="api-key"
    )
    serialized = json.dumps(rendered)
    assert "supersecretkeyvalue1234" not in serialized
    assert rendered["subscription_key_masked"].endswith("1234")


def test_render_report_console_plain_never_leaks_subscription_key(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(vme, "_RICH_AVAILABLE", False)
    report = vme.ValidationReport(endpoint="https://example.test/mcp/demo")
    report.phases.append(vme.PhaseResult("HTTP status", True, "200"))
    vme.render_report_console(report, subscription_key="supersecretkeyvalue1234", subscription_key_header="api-key")
    captured = capsys.readouterr()
    assert "supersecretkeyvalue1234" not in captured.out
    assert "1234" in captured.out


# ---------------------------------------------------------------------------
# Header-name selection logic
# ---------------------------------------------------------------------------


def test_build_request_headers_selects_api_key_header() -> None:
    headers = vme.build_request_headers("secret123", "api-key", None)
    assert headers["api-key"] == "secret123"
    assert "Ocp-Apim-Subscription-Key" not in headers


def test_build_request_headers_selects_ocp_apim_header() -> None:
    headers = vme.build_request_headers("secret123", "Ocp-Apim-Subscription-Key", None)
    assert headers["Ocp-Apim-Subscription-Key"] == "secret123"
    assert "api-key" not in headers


def test_build_request_headers_includes_bearer_and_session_id() -> None:
    headers = vme.build_request_headers(None, "api-key", "tok123", session_id="sess-1")
    assert headers["Authorization"] == "Bearer tok123"
    assert headers[vme.SESSION_ID_HEADER] == "sess-1"


def test_build_request_headers_omits_absent_values() -> None:
    headers = vme.build_request_headers(None, "api-key", None)
    assert "Authorization" not in headers
    assert "api-key" not in headers
    assert vme.SESSION_ID_HEADER not in headers


def test_build_request_headers_rejects_invalid_header_name() -> None:
    with pytest.raises(ValueError):
        vme.build_request_headers("key", "X-Not-A-Real-Header", None)


# ---------------------------------------------------------------------------
# Exit-code mapping
# ---------------------------------------------------------------------------


def _make_report(all_passed: bool) -> vme.ValidationReport:
    report = vme.ValidationReport(endpoint="https://example.test/mcp/demo")
    report.phases.append(vme.PhaseResult("HTTP status", all_passed, "200" if all_passed else "500"))
    if not all_passed:
        report.failure_hint = vme.FailureHint("x", "title", "explain", "fix")
    return report


def test_exit_code_pass() -> None:
    assert vme.determine_exit_code(_make_report(True)) == vme.EXIT_OK == 0


def test_exit_code_validation_failure() -> None:
    assert vme.determine_exit_code(_make_report(False)) == vme.EXIT_VALIDATION_FAILURE == 1


def test_exit_code_usage_error_flag_wins() -> None:
    assert vme.determine_exit_code(_make_report(True), usage_error=True) == vme.EXIT_USAGE_ERROR == 2


def test_exit_code_none_report_is_usage_error() -> None:
    assert vme.determine_exit_code(None) == vme.EXIT_USAGE_ERROR


# ---------------------------------------------------------------------------
# Failure-hint classifier
# ---------------------------------------------------------------------------


def test_classify_failure_401_without_www_authenticate_blames_subscription_key() -> None:
    hint = vme.classify_failure(401, {})
    assert hint is not None
    assert hint.code == "401-no-www-authenticate"
    assert "subscription" in hint.remediation.lower()


def test_classify_failure_401_with_bearer_challenge_blames_entra_token() -> None:
    hint = vme.classify_failure(401, {"WWW-Authenticate": 'Bearer realm="mcp-gateway", error="invalid_token"'})
    assert hint is not None
    assert hint.code == "401-bearer-challenge"
    assert "token" in hint.remediation.lower()


def test_classify_failure_401_bearer_challenge_surfaces_resource_metadata_url() -> None:
    header = (
        'Bearer error="invalid_token", '
        'resource_metadata="https://example.test/.well-known/oauth-protected-resource"'
    )
    hint = vme.classify_failure(401, {"WWW-Authenticate": header})
    assert hint is not None
    assert "https://example.test/.well-known/oauth-protected-resource" in hint.remediation


def test_classify_failure_401_header_lookup_is_case_insensitive() -> None:
    hint = vme.classify_failure(401, {"www-authenticate": "Bearer"})
    assert hint is not None
    assert hint.code == "401-bearer-challenge"


def test_classify_failure_403_blames_missing_app_role() -> None:
    hint = vme.classify_failure(403, {})
    assert hint is not None
    assert hint.code == "403-forbidden"
    assert "mcp.admin" in hint.remediation and "mcp.engineer" in hint.remediation


def test_classify_failure_404_explains_publish_contract_path_shapes() -> None:
    hint = vme.classify_failure(404, {})
    assert hint is not None
    assert hint.code == "404-not-found"
    assert "mcp-existing" in hint.remediation
    assert "mcp-from-api" in hint.remediation


@pytest.mark.parametrize("status_code", [503, 504])
def test_classify_failure_503_504_single_attempt_is_generic(status_code: int) -> None:
    hint = vme.classify_failure(status_code, {}, attempt_count=1)
    assert hint is not None
    assert "circuit-breaker" not in hint.code


@pytest.mark.parametrize("status_code", [503, 504])
def test_classify_failure_503_504_repeated_attempts_flags_circuit_breaker(status_code: int) -> None:
    hint = vme.classify_failure(status_code, {}, attempt_count=3)
    assert hint is not None
    assert "circuit-breaker" in hint.code
    assert "circuit breaker" in hint.explanation.lower()


def test_classify_failure_returns_none_for_unremarkable_status() -> None:
    assert vme.classify_failure(200, {}) is None
    assert vme.classify_failure(500, {}) is None
