"""End-to-end validator for an MCP endpoint published through the governance plane.

This CLI validates that a Model Context Protocol (MCP) server, registered as an
adapter on MCP Gateway (running on AKS) and published through the Citadel
Governance Hub (Azure API Management sitting in front of it), is reachable and
speaks MCP correctly end-to-end -- through *both* governance layers at once:

    caller --(APIM subscription key + Entra bearer token)--> APIM --(pass-through
    Authorization header, backend authType=none)--> MCP Gateway --(validates the
    Entra token itself, derives end-user identity)--> adapter (Streamable HTTP)

It performs a real MCP handshake (``initialize`` -> ``notifications/initialized``
-> ``tools/list`` -> optional ``tools/call``) over Streamable HTTP, understands
both plain JSON and ``text/event-stream`` (SSE) response bodies, and produces a
pass/fail report that calls out exactly which governance layer rejected the
request when something goes wrong.

Library functions in this module return structured results (dataclasses) and
never print; only the CLI entry point (:func:`main`) renders output. This keeps
the validation logic independently testable without a network connection.

Exit codes:
    0 -- validation passed
    1 -- validation failed (endpoint reachable but one or more checks failed)
    2 -- usage/configuration error (bad arguments, missing token source, etc.)
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import ssl
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Mapping, Sequence
from urllib.parse import urlparse

import httpx

try:  # pragma: no cover - exercised indirectly, optional at import time
    from rich.console import Console
    from rich.table import Table

    _RICH_AVAILABLE = True
except ImportError:  # pragma: no cover - fallback path is what's covered
    _RICH_AVAILABLE = False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_PROTOCOL_VERSION = "2025-06-18"
DEFAULT_CLIENT_NAME = "mcp-endpoint-validator"
DEFAULT_CLIENT_VERSION = "1.0.0"

SUBSCRIPTION_KEY_HEADER_CHOICES: tuple[str, ...] = ("api-key", "Ocp-Apim-Subscription-Key")
SESSION_ID_HEADER = "mcp-session-id"
# Session id validity contract enforced by MCP Gateway's session-affinity layer.
SESSION_ID_PATTERN = re.compile(r"^[a-zA-Z0-9\-]{1,128}$")

EXIT_OK = 0
EXIT_VALIDATION_FAILURE = 1
EXIT_USAGE_ERROR = 2

_RESOURCE_METADATA_RE = re.compile(r'resource_metadata="([^"]+)"')


# ---------------------------------------------------------------------------
# Result data model
# ---------------------------------------------------------------------------


@dataclass
class PhaseResult:
    """One row of the pass/fail summary table."""

    name: str
    passed: bool
    detail: str = ""
    elapsed_ms: float | None = None


@dataclass
class FailureHint:
    """An actionable explanation for why validation failed."""

    code: str
    title: str
    explanation: str
    remediation: str


@dataclass
class ValidationReport:
    """The full result of a validation run."""

    endpoint: str
    phases: list[PhaseResult] = field(default_factory=list)
    tool_count: int | None = None
    tool_names: list[str] = field(default_factory=list)
    session_id: str | None = None
    protocol_version: str | None = None
    failure_hint: FailureHint | None = None

    @property
    def passed(self) -> bool:
        return self.failure_hint is None and all(p.passed for p in self.phases)


@dataclass
class SseEvent:
    """A single parsed Server-Sent Event."""

    data: str
    event: str | None = None
    id: str | None = None


# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------


def mask_secret(value: str | None, visible: int = 4, min_mask_len: int = 4) -> str:
    """Mask a secret so it is safe to print, e.g. ``mask_secret("abcd1234cd12")`` -> ``"********cd12"``.

    Never reveals more than the last ``visible`` characters, and never reveals
    any of the value at all if the value is not longer than ``visible``
    characters (short secrets are masked in full).
    """
    if not value:
        return ""
    visible = max(visible, 0)
    suffix = value[-visible:] if visible and len(value) > visible else ""
    mask_len = max(min_mask_len, len(value) - len(suffix))
    return f"{'*' * mask_len}{suffix}"


# ---------------------------------------------------------------------------
# SSE parsing (streaming, never buffers the full body)
# ---------------------------------------------------------------------------


def iter_sse_events(lines: Iterable[str]) -> Iterator[SseEvent]:
    """Incrementally parse Server-Sent Events from a line iterator.

    Consumes ``lines`` lazily (e.g. from ``httpx.Response.iter_lines()``) so a
    caller never has to buffer the whole SSE body into memory before parsing
    starts. Follows the field-parsing rules MCP relies on:

    - Lines starting with ``:`` are comments/heartbeats and are ignored.
    - ``data:`` lines accumulate (joined with ``\\n``) until a blank line
      terminates the event -- so multi-line JSON payloads split across
      several ``data:`` lines are reassembled correctly.
    - ``event:`` and ``id:`` fields are captured but are not required.
    - Any trailing (unterminated) event at end-of-stream is still yielded.
    """
    data_lines: list[str] = []
    event_name: str | None = None
    event_id: str | None = None

    for raw_line in lines:
        line = raw_line.rstrip("\r\n") if isinstance(raw_line, str) else str(raw_line)

        if line == "":
            if data_lines:
                yield SseEvent(data="\n".join(data_lines), event=event_name, id=event_id)
            data_lines = []
            event_name = None
            event_id = None
            continue

        if line.startswith(":"):
            # Comment / heartbeat line -- ignored entirely.
            continue

        if line.startswith("data:"):
            value = line[len("data:") :]
            if value.startswith(" "):
                value = value[1:]
            data_lines.append(value)
        elif line.startswith("event:"):
            event_name = line[len("event:") :].strip()
        elif line.startswith("id:"):
            event_id = line[len("id:") :].strip()
        # Other fields (e.g. "retry:") are not relevant to MCP and are ignored.

    if data_lines:
        yield SseEvent(data="\n".join(data_lines), event=event_name, id=event_id)


def parse_mcp_message(response: httpx.Response) -> dict[str, Any] | None:
    """Extract the first JSON-RPC message from an MCP HTTP response.

    Handles both a plain ``application/json`` body and a streamed
    ``text/event-stream`` (SSE) body. SSE bodies are consumed line-by-line via
    ``response.iter_lines()`` and parsing stops as soon as the first ``data:``
    event yields valid JSON -- the response is never fully buffered first, and
    no assumption is made that a single SSE event is the only one sent.
    """
    content_type = response.headers.get("content-type", "")

    if "text/event-stream" in content_type:
        for evt in iter_sse_events(response.iter_lines()):
            data = evt.data.strip()
            if not data:
                continue
            try:
                parsed = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
        return None

    # Notifications and some errors legitimately return an empty body.
    body = response.read()
    if not body:
        return None
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


# ---------------------------------------------------------------------------
# JSON-RPC envelope construction
# ---------------------------------------------------------------------------


def build_initialize_request(
    *,
    protocol_version: str = DEFAULT_PROTOCOL_VERSION,
    client_name: str = DEFAULT_CLIENT_NAME,
    client_version: str = DEFAULT_CLIENT_VERSION,
    request_id: int | str = 1,
) -> dict[str, Any]:
    """Build the MCP ``initialize`` JSON-RPC 2.0 request."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "initialize",
        "params": {
            "protocolVersion": protocol_version,
            "capabilities": {},
            "clientInfo": {"name": client_name, "version": client_version},
        },
    }


def build_initialized_notification() -> dict[str, Any]:
    """Build the ``notifications/initialized`` JSON-RPC 2.0 notification (no ``id``)."""
    return {"jsonrpc": "2.0", "method": "notifications/initialized"}


def build_tools_list_request(request_id: int | str = 2) -> dict[str, Any]:
    """Build the ``tools/list`` JSON-RPC 2.0 request."""
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/list"}


def build_tools_call_request(
    name: str, arguments: Mapping[str, Any] | None = None, request_id: int | str = 3
) -> dict[str, Any]:
    """Build a ``tools/call`` JSON-RPC 2.0 request."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": dict(arguments or {})},
    }


# ---------------------------------------------------------------------------
# Header construction
# ---------------------------------------------------------------------------


def build_request_headers(
    subscription_key: str | None,
    subscription_key_header: str,
    bearer_token: str | None,
    session_id: str | None = None,
) -> dict[str, str]:
    """Build request headers, selecting the correct APIM subscription-key header name.

    ``subscription_key_header`` must be one of :data:`SUBSCRIPTION_KEY_HEADER_CHOICES`
    (``api-key`` or ``Ocp-Apim-Subscription-Key``) -- both are valid ways APIM
    accepts a subscription key, and the caller chooses which one the published
    asset expects.
    """
    if subscription_key_header not in SUBSCRIPTION_KEY_HEADER_CHOICES:
        raise ValueError(
            f"subscription_key_header must be one of {SUBSCRIPTION_KEY_HEADER_CHOICES}, "
            f"got {subscription_key_header!r}"
        )

    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if subscription_key:
        headers[subscription_key_header] = subscription_key
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    if session_id:
        headers[SESSION_ID_HEADER] = session_id
    return headers


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------


def _get_header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup that works for plain dicts and httpx.Headers."""
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def _extract_resource_metadata(www_authenticate: str) -> str | None:
    match = _RESOURCE_METADATA_RE.search(www_authenticate)
    return match.group(1) if match else None


def classify_failure(
    status_code: int,
    headers: Mapping[str, str] | None = None,
    attempt_count: int = 1,
) -> FailureHint | None:
    """Classify an HTTP failure status code into an actionable :class:`FailureHint`.

    Returns ``None`` for status codes that need no special-cased guidance.
    """
    headers = headers or {}
    www_auth = _get_header(headers, "WWW-Authenticate")

    if status_code == 401:
        if not www_auth:
            return FailureHint(
                code="401-no-www-authenticate",
                title="401 Unauthorized (no WWW-Authenticate challenge)",
                explanation=(
                    "The gateway rejected the request before any OAuth/Entra challenge "
                    "was issued. This shape is typical of Azure API Management rejecting "
                    "a missing or incorrect subscription key, rather than MCP Gateway "
                    "rejecting a bearer token."
                ),
                remediation=(
                    "Confirm --subscription-key is set and matches "
                    "--subscription-key-header ('api-key' vs. 'Ocp-Apim-Subscription-Key'), "
                    "and that the APIM product/subscription the key belongs to has access "
                    "to this API."
                ),
            )
        if "bearer" in www_auth.lower():
            resource_metadata = _extract_resource_metadata(www_auth)
            remediation = (
                "The subscription key layer was satisfied but the Entra bearer token was "
                "rejected. Verify --token / --use-azure-identity is minting a token for the "
                "correct audience (api://<client-id>) and scope (access), and that the "
                "token has not expired."
            )
            if resource_metadata:
                remediation += (
                    " The server advertised RFC 9728 protected-resource metadata at: "
                    f"{resource_metadata} -- fetch it to confirm the expected issuer/audience."
                )
            return FailureHint(
                code="401-bearer-challenge",
                title="401 Unauthorized (WWW-Authenticate: Bearer)",
                explanation=(
                    "The origin issued an OAuth/Entra bearer challenge, meaning the "
                    "subscription-key layer was satisfied but the caller's Entra token was "
                    "missing, expired, or invalid for this resource."
                ),
                remediation=remediation,
            )
        return FailureHint(
            code="401-unrecognized-challenge",
            title="401 Unauthorized (unrecognized WWW-Authenticate challenge)",
            explanation=f"The server issued an auth challenge that was not recognized: {www_auth!r}",
            remediation="Inspect the WWW-Authenticate header manually and confirm the expected auth scheme.",
        )

    if status_code == 403:
        return FailureHint(
            code="403-forbidden",
            title="403 Forbidden",
            explanation=(
                "The caller authenticated successfully but lacks the required MCP Gateway "
                "app role."
            ),
            remediation=(
                "Confirm the Entra identity (user or service principal) has been assigned "
                "the 'mcp.admin' or 'mcp.engineer' app role on the MCP Gateway application "
                "registration."
            ),
        )

    if status_code == 404:
        return FailureHint(
            code="404-not-found",
            title="404 Not Found",
            explanation="The published path does not match what the governance hub actually exposed.",
            remediation=(
                "Check the Publish Contract asset type: 'mcp-existing' assets publish at "
                "/mcp/<path> (no trailing /mcp); 'mcp-from-api' assets publish at "
                "/mcp/<path>/mcp. Confirm --endpoint matches the asset type that was "
                "actually deployed."
            ),
        )

    if status_code in (503, 504):
        if attempt_count > 1:
            return FailureHint(
                code=f"{status_code}-circuit-breaker",
                title=f"{status_code} after repeated failures",
                explanation=(
                    "Repeated 503/504 responses strongly suggest the APIM backend circuit "
                    "breaker has tripped for the MCP Gateway backend pool."
                ),
                remediation=(
                    "Check APIM diagnostics for backend circuit-breaker trips, confirm the "
                    "AKS-hosted MCP Gateway pods are healthy, and retry after the breaker's "
                    "trip duration elapses."
                ),
            )
        return FailureHint(
            code=f"{status_code}-backend-unavailable",
            title=f"{status_code} Service Unavailable / Gateway Timeout",
            explanation="The backend did not respond successfully.",
            remediation=(
                "Retry the request; if this persists across multiple attempts it likely "
                "indicates the APIM backend circuit breaker is open -- check AKS pod health "
                "for the MCP Gateway deployment."
            ),
        )

    return None


# ---------------------------------------------------------------------------
# Connectivity checks
# ---------------------------------------------------------------------------


def check_connectivity(
    host: str, port: int, *, timeout: float, use_tls: bool, insecure: bool
) -> tuple[PhaseResult, PhaseResult]:
    """Check DNS/TCP reachability and (if applicable) TLS handshake.

    Returns ``(dns_tcp_result, tls_result)``. If the TCP connection fails, the
    TLS phase is reported as skipped rather than attempted.
    """
    start = time.perf_counter()
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        elapsed = (time.perf_counter() - start) * 1000
        return (
            PhaseResult("DNS/TCP reachability", False, str(exc), elapsed),
            PhaseResult("TLS handshake", False, "skipped: TCP connection failed"),
        )

    dns_tcp_elapsed = (time.perf_counter() - start) * 1000
    dns_tcp_result = PhaseResult("DNS/TCP reachability", True, f"connected to {host}:{port}", dns_tcp_elapsed)

    if not use_tls:
        sock.close()
        return dns_tcp_result, PhaseResult("TLS handshake", True, "skipped (plain HTTP endpoint)")

    context = ssl.create_default_context()
    if insecure:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    tls_start = time.perf_counter()
    try:
        with context.wrap_socket(sock, server_hostname=host) as tls_sock:
            tls_elapsed = (time.perf_counter() - tls_start) * 1000
            version = tls_sock.version() or "unknown"
            tls_result = PhaseResult("TLS handshake", True, f"negotiated {version}", tls_elapsed)
    except ssl.SSLError as exc:
        tls_elapsed = (time.perf_counter() - tls_start) * 1000
        tls_result = PhaseResult("TLS handshake", False, str(exc), tls_elapsed)
    finally:
        try:
            sock.close()
        except OSError:
            pass

    return dns_tcp_result, tls_result


# ---------------------------------------------------------------------------
# Azure Identity token acquisition
# ---------------------------------------------------------------------------


def acquire_token_via_azure_identity(scope: str) -> str:
    """Acquire a bearer token for ``scope`` using azure-identity's DefaultAzureCredential.

    Raises ``RuntimeError`` with an actionable message if azure-identity is not
    installed, or if token acquisition fails.
    """
    try:
        from azure.identity import DefaultAzureCredential
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise RuntimeError(
            "azure-identity is not installed. Install it with "
            "'pip install -r scripts/requirements.txt', or supply --token directly."
        ) from exc

    try:
        credential = DefaultAzureCredential()
        token = credential.get_token(scope)
    except Exception as exc:  # noqa: BLE001 - surface any credential failure uniformly
        raise RuntimeError(f"Failed to acquire a token for scope {scope!r}: {exc}") from exc

    return token.token


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def post_json_rpc(
    client: httpx.Client, url: str, message: dict[str, Any], headers: Mapping[str, str]
) -> tuple[httpx.Response, dict[str, Any] | None]:
    """POST a JSON-RPC message and return ``(response, parsed_message_or_none)``.

    Uses a streamed request so an SSE response body is never buffered whole
    into memory (see :func:`parse_mcp_message`); the response is always
    closed before returning.
    """
    request = client.build_request("POST", url, json=message, headers=dict(headers))
    response = client.send(request, stream=True)
    try:
        payload = parse_mcp_message(response)
    finally:
        response.close()
    return response, payload


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_validation(
    *,
    endpoint: str,
    subscription_key: str | None,
    subscription_key_header: str,
    bearer_token: str | None,
    timeout: float,
    insecure: bool,
    call_tool: str | None = None,
    tool_args: dict[str, Any] | None = None,
) -> ValidationReport:
    """Run the full end-to-end MCP endpoint validation and return a report."""
    report = ValidationReport(endpoint=endpoint)
    parsed = urlparse(endpoint)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    dns_tcp_result, tls_result = check_connectivity(
        host, port, timeout=timeout, use_tls=(parsed.scheme == "https"), insecure=insecure
    )
    report.phases.append(dns_tcp_result)
    report.phases.append(tls_result)

    if not dns_tcp_result.passed:
        report.failure_hint = FailureHint(
            code="dns-tcp-unreachable",
            title="Endpoint unreachable",
            explanation=dns_tcp_result.detail,
            remediation=(
                "Confirm the hostname/DNS, network path (VNet / private endpoint / "
                "firewall), and that the APIM gateway is provisioned and running."
            ),
        )
        return report

    headers = build_request_headers(subscription_key, subscription_key_header, bearer_token)

    with httpx.Client(timeout=timeout, verify=not insecure) as client:
        start = time.perf_counter()
        try:
            response, payload = post_json_rpc(client, endpoint, build_initialize_request(), headers)
        except httpx.HTTPError as exc:
            elapsed = (time.perf_counter() - start) * 1000
            report.phases.append(PhaseResult("HTTP status", False, f"request failed: {exc}", elapsed))
            report.failure_hint = FailureHint(
                code="transport-error",
                title="Transport-level failure calling initialize",
                explanation=str(exc),
                remediation="Check TLS trust, proxy settings, and that --endpoint is the full published MCP URL.",
            )
            return report
        elapsed = (time.perf_counter() - start) * 1000

        status_ok = 200 <= response.status_code < 300
        report.phases.append(PhaseResult("HTTP status", status_ok, str(response.status_code), elapsed))

        auth_ok = response.status_code not in (401, 403)
        report.phases.append(
            PhaseResult("Auth accepted", auth_ok, "no auth challenge" if auth_ok else str(response.status_code))
        )

        if not status_ok:
            report.failure_hint = classify_failure(response.status_code, response.headers)
            report.phases.append(PhaseResult("Initialize succeeded", False, "skipped: HTTP failure"))
            report.phases.append(PhaseResult("Protocol version negotiated", False, "skipped: HTTP failure"))
            report.phases.append(PhaseResult("Session id", False, "skipped: HTTP failure"))
            report.phases.append(PhaseResult("tools/list", False, "skipped: HTTP failure"))
            return report

        session_id = response.headers.get(SESSION_ID_HEADER)
        report.session_id = session_id
        report.phases.append(
            PhaseResult(
                "Session id",
                True,
                session_id if session_id else "absent (stateless server, or not yet assigned)",
            )
        )

        error = payload.get("error") if isinstance(payload, dict) else None
        result = payload.get("result") if isinstance(payload, dict) else None
        init_ok = payload is not None and error is None and isinstance(result, dict)
        report.phases.append(
            PhaseResult("Initialize succeeded", init_ok, json.dumps(error) if error else "ok")
        )

        protocol_version = result.get("protocolVersion") if isinstance(result, dict) else None
        report.protocol_version = protocol_version
        report.phases.append(
            PhaseResult(
                "Protocol version negotiated",
                bool(protocol_version),
                protocol_version or "not present in response",
            )
        )

        if not init_ok:
            report.failure_hint = report.failure_hint or FailureHint(
                code="jsonrpc-error",
                title="initialize returned a JSON-RPC error",
                explanation=json.dumps(error) if error else "No result or error returned by the server.",
                remediation="Inspect the MCP Gateway / adapter logs for the initialize request.",
            )
            report.phases.append(PhaseResult("tools/list", False, "skipped: initialize failed"))
            return report

        # notifications/initialized -- fire-and-forget, no id, best-effort ack.
        notify_headers = dict(headers)
        if session_id:
            notify_headers[SESSION_ID_HEADER] = session_id
        try:
            notify_request = client.build_request(
                "POST", endpoint, json=build_initialized_notification(), headers=notify_headers
            )
            notify_response = client.send(notify_request, stream=True)
            notify_response.close()
        except httpx.HTTPError:
            pass  # A notification has no required response; ignore transport hiccups here.

        # tools/list
        list_headers = dict(headers)
        if session_id:
            list_headers[SESSION_ID_HEADER] = session_id
        start = time.perf_counter()
        try:
            list_response, list_payload = post_json_rpc(client, endpoint, build_tools_list_request(), list_headers)
        except httpx.HTTPError as exc:
            elapsed = (time.perf_counter() - start) * 1000
            report.phases.append(PhaseResult("tools/list", False, str(exc), elapsed))
            return report
        elapsed = (time.perf_counter() - start) * 1000

        tools: list[Any] = []
        if isinstance(list_payload, dict):
            list_result = list_payload.get("result")
            if isinstance(list_result, dict):
                tools = list_result.get("tools") or []
        tool_names = [t.get("name", "?") for t in tools if isinstance(t, dict)]
        report.tool_count = len(tool_names)
        report.tool_names = tool_names
        list_ok = 200 <= list_response.status_code < 300 and isinstance(list_payload, dict) and "error" not in list_payload
        report.phases.append(
            PhaseResult(
                "tools/list",
                list_ok,
                f"{len(tool_names)} tool(s): {', '.join(tool_names) if tool_names else 'none'}",
                elapsed,
            )
        )

        if call_tool:
            call_headers = dict(headers)
            if session_id:
                call_headers[SESSION_ID_HEADER] = session_id
            start = time.perf_counter()
            try:
                call_response, call_payload = post_json_rpc(
                    client, endpoint, build_tools_call_request(call_tool, tool_args or {}), call_headers
                )
                elapsed = (time.perf_counter() - start) * 1000
                call_error = call_payload.get("error") if isinstance(call_payload, dict) else None
                call_ok = 200 <= call_response.status_code < 300 and call_error is None
                detail = json.dumps(call_error) if call_error else "ok"
                report.phases.append(PhaseResult(f"tools/call:{call_tool}", call_ok, detail, elapsed))
            except httpx.HTTPError as exc:
                elapsed = (time.perf_counter() - start) * 1000
                report.phases.append(PhaseResult(f"tools/call:{call_tool}", False, str(exc), elapsed))

    return report


def determine_exit_code(report: ValidationReport | None, *, usage_error: bool = False) -> int:
    """Map a validation report (or a usage error) to a process exit code."""
    if usage_error:
        return EXIT_USAGE_ERROR
    if report is None:
        return EXIT_USAGE_ERROR
    return EXIT_OK if report.passed else EXIT_VALIDATION_FAILURE


# ---------------------------------------------------------------------------
# Rendering (dict for --json, text/rich for console)
# ---------------------------------------------------------------------------


def render_report_dict(
    report: ValidationReport, *, subscription_key: str | None, subscription_key_header: str
) -> dict[str, Any]:
    """Serialize a report to a JSON-safe dict, with all secrets redacted."""
    return {
        "endpoint": report.endpoint,
        "passed": report.passed,
        "subscription_key_header": subscription_key_header,
        "subscription_key_masked": mask_secret(subscription_key) if subscription_key else None,
        "session_id": report.session_id,
        "protocol_version": report.protocol_version,
        "tool_count": report.tool_count,
        "tool_names": report.tool_names,
        "phases": [
            {"name": p.name, "passed": p.passed, "detail": p.detail, "elapsed_ms": p.elapsed_ms}
            for p in report.phases
        ],
        "failure_hint": (
            {
                "code": report.failure_hint.code,
                "title": report.failure_hint.title,
                "explanation": report.failure_hint.explanation,
                "remediation": report.failure_hint.remediation,
            }
            if report.failure_hint
            else None
        ),
    }


def _render_plain(
    report: ValidationReport, *, subscription_key: str | None, subscription_key_header: str
) -> None:
    print(f"MCP endpoint validation: {report.endpoint}")
    if subscription_key:
        print(f"  {subscription_key_header}: {mask_secret(subscription_key)}")
    print(f"  overall: {'PASS' if report.passed else 'FAIL'}")
    print("-" * 78)
    for p in report.phases:
        status = "PASS" if p.passed else "FAIL"
        elapsed = f"{p.elapsed_ms:.1f}ms" if p.elapsed_ms is not None else "-"
        print(f"  [{status:4}] {p.name:32} {elapsed:>10}  {p.detail}")
    if report.failure_hint:
        fh = report.failure_hint
        print("-" * 78)
        print(f"  Failure: {fh.title}")
        print(f"    {fh.explanation}")
        print(f"    Hint: {fh.remediation}")


def _render_rich(
    report: ValidationReport, *, subscription_key: str | None, subscription_key_header: str
) -> None:
    console = Console()
    table = Table(title=f"MCP endpoint validation: {report.endpoint}")
    table.add_column("Phase")
    table.add_column("Status")
    table.add_column("Elapsed (ms)", justify="right")
    table.add_column("Detail")
    for p in report.phases:
        status = "[green]PASS[/]" if p.passed else "[red]FAIL[/]"
        elapsed = f"{p.elapsed_ms:.1f}" if p.elapsed_ms is not None else "-"
        table.add_row(p.name, status, elapsed, p.detail)
    console.print(table)
    if subscription_key:
        console.print(f"{subscription_key_header}: {mask_secret(subscription_key)}")
    console.print(f"Overall: {'[bold green]PASS[/]' if report.passed else '[bold red]FAIL[/]'}")
    if report.failure_hint:
        fh = report.failure_hint
        console.print(f"[bold red]Failure:[/] {fh.title}")
        console.print(f"  {fh.explanation}")
        console.print(f"  [bold]Hint:[/] {fh.remediation}")


def render_report_console(
    report: ValidationReport, *, subscription_key: str | None, subscription_key_header: str
) -> None:
    """Print the report to the console, using rich if available, plain text otherwise."""
    if _RICH_AVAILABLE:
        _render_rich(report, subscription_key=subscription_key, subscription_key_header=subscription_key_header)
    else:
        _render_plain(report, subscription_key=subscription_key, subscription_key_header=subscription_key_header)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="validate_mcp_endpoint",
        description=(
            "Validate a published MCP endpoint end-to-end through the governance plane "
            "(Azure API Management in front of MCP Gateway)."
        ),
    )
    parser.add_argument(
        "--endpoint",
        required=True,
        help="The APIM-published MCP URL, e.g. https://<apim-gateway-host>/mcp/<path>",
    )
    parser.add_argument(
        "--subscription-key",
        default=None,
        help="APIM subscription key. Never echoed back in full.",
    )
    parser.add_argument(
        "--subscription-key-header",
        default="api-key",
        choices=list(SUBSCRIPTION_KEY_HEADER_CHOICES),
        help="Header name APIM expects the subscription key in (default: api-key).",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="A raw Entra bearer token to send as 'Authorization: Bearer <token>'.",
    )
    parser.add_argument(
        "--use-azure-identity",
        action="store_true",
        help="Acquire a bearer token via azure-identity's DefaultAzureCredential for --scope.",
    )
    parser.add_argument(
        "--scope",
        default=None,
        help="Token scope/audience to request, e.g. api://<client-id>/.default (used with --use-azure-identity).",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="Per-request timeout in seconds (default: 30).")
    parser.add_argument("--json", dest="json_output", action="store_true", help="Emit machine-readable JSON output.")
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Skip TLS certificate verification. LAB USE ONLY -- never use against a production endpoint.",
    )
    parser.add_argument(
        "--call-tool",
        default=None,
        metavar="NAME",
        help="Optionally invoke tools/call for tool NAME after tools/list succeeds.",
    )
    parser.add_argument(
        "--tool-args",
        default=None,
        metavar="JSON",
        help="JSON object of arguments to pass to --call-tool, e.g. '{\"key\": \"value\"}'.",
    )
    return parser


def _warn_insecure() -> None:
    print(
        "WARNING: --insecure disables TLS certificate verification. "
        "This is for lab/demo use only and must never be used against a production endpoint.",
        file=sys.stderr,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.insecure:
        _warn_insecure()

    tool_args: dict[str, Any] | None = None
    if args.tool_args is not None:
        try:
            parsed_tool_args = json.loads(args.tool_args)
        except json.JSONDecodeError as exc:
            print(f"error: --tool-args is not valid JSON: {exc}", file=sys.stderr)
            return EXIT_USAGE_ERROR
        if not isinstance(parsed_tool_args, dict):
            print("error: --tool-args must be a JSON object", file=sys.stderr)
            return EXIT_USAGE_ERROR
        tool_args = parsed_tool_args
    elif args.call_tool:
        tool_args = {}

    bearer_token = args.token
    if args.use_azure_identity:
        if not args.scope:
            print("error: --scope is required with --use-azure-identity", file=sys.stderr)
            return EXIT_USAGE_ERROR
        try:
            bearer_token = acquire_token_via_azure_identity(args.scope)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_USAGE_ERROR

    try:
        report = run_validation(
            endpoint=args.endpoint,
            subscription_key=args.subscription_key,
            subscription_key_header=args.subscription_key_header,
            bearer_token=bearer_token,
            timeout=args.timeout,
            insecure=args.insecure,
            call_tool=args.call_tool,
            tool_args=tool_args,
        )
    except Exception as exc:  # pragma: no cover - defensive: unexpected runtime failure
        print(f"error: unexpected failure during validation: {exc}", file=sys.stderr)
        return EXIT_VALIDATION_FAILURE

    if args.json_output:
        print(
            json.dumps(
                render_report_dict(
                    report,
                    subscription_key=args.subscription_key,
                    subscription_key_header=args.subscription_key_header,
                ),
                indent=2,
            )
        )
    else:
        render_report_console(
            report, subscription_key=args.subscription_key, subscription_key_header=args.subscription_key_header
        )

    return determine_exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
