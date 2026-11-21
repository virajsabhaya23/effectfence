"""Streamable HTTP transport for MCP.

The stdio client can only reach servers a user installs locally. Most servers a
real user or agent meets are remote: hosted endpoints, gateway-fronted servers,
anything behind OAuth. This module implements the streamable HTTP transport with
the same surface the verifier already uses, so the verifier never branches on
transport when running a case.

Deliberate limits, matching the rest of the tool: no shell, an explicit host
allowlist, no redirects, bounded response size, and bearer tokens supplied only
through environment variables so they never reach a manifest or a report.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .mcp_client import McpClientError, McpProtocolError
from .mcp_protocol import (
    SUPPORTED_PROTOCOL_VERSIONS,
    UNSUPPORTED_PROTOCOL_VERSION,
    is_modern_version,
    modern_meta,
    pick_common_version,
    server_info as _server_info,
    wants_modern,
    with_meta,
)


def _client_version() -> str:
    from . import __version__

    return __version__


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class McpHttpClient:
    """Minimal streamable-HTTP MCP client (JSON and SSE responses)."""

    def __init__(
        self,
        url: str,
        *,
        headers_from_env: dict[str, str] | None = None,
        allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "::1"),
        protocol_version: str = "auto",
        timeout_seconds: float = 30.0,
        startup_timeout_seconds: float = 15.0,
        max_response_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        """Create the client; ``start`` performs the initialize handshake."""
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise McpClientError("MCP HTTP url must use http or https")
        if parsed.hostname not in allowed_hosts:
            raise McpClientError(
                f"MCP HTTP host {parsed.hostname!r} is not in allowedHosts"
            )
        if timeout_seconds <= 0 or startup_timeout_seconds <= 0:
            raise McpClientError("MCP HTTP timeouts must be positive")
        self.url = url
        self.headers_from_env = dict(headers_from_env or {})
        self.protocol_version = protocol_version
        self.timeout_seconds = timeout_seconds
        self.startup_timeout_seconds = startup_timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.server_info: dict[str, Any] = {}
        self.server_capabilities: dict[str, Any] = {}
        self.negotiated_protocol_version = ""
        self.session_id: str | None = None
        self.era = "legacy"
        self._header_version = SUPPORTED_PROTOCOL_VERSIONS[-1]
        self._next_id = 1

    # The verifier reads these off every client, including the stdio one.
    @property
    def stderr_tail(self) -> str:
        return ""

    def __enter__(self) -> McpHttpClient:
        self.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _headers(self, method: str = "", tool_name: str = "") -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": (
                self.negotiated_protocol_version or self._header_version
            ),
        }
        if self.era == "modern":
            # 2026-07-28 requires the method, and the tool name for tools/call.
            headers["Mcp-Method"] = method
            if tool_name:
                headers["Mcp-Name"] = tool_name
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        for header, variable in self.headers_from_env.items():
            if variable not in os.environ:
                raise McpClientError(
                    f"MCP HTTP header {header!r} requires environment variable {variable!r}"
                )
            headers[header] = os.environ[variable]
        return headers

    def _post(
        self,
        payload: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
        method: str = "",
        tool_name: str = "",
    ) -> tuple[int, dict[str, str], bytes]:
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers=self._headers(method, tool_name),
            method="POST",
        )
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            with opener.open(
                request, timeout=timeout_seconds or self.timeout_seconds
            ) as response:
                body = response.read(self.max_response_bytes + 1)
                return response.status, dict(response.headers), body
        except urllib.error.HTTPError as error:
            # A 401 is the most useful diagnostic here, so name it.
            if error.code == 401:
                raise McpClientError(
                    "MCP HTTP server returned 401; supply the token through headersFromEnv"
                ) from error
            # 400 is how a legacy server answers a modern probe, so the body has
            # to be inspectable instead of fatal.
            try:
                body = error.read(self.max_response_bytes + 1)
            except OSError:
                body = b""
            return error.code, dict(error.headers or {}), body
        except urllib.error.URLError as error:
            raise McpClientError(
                f"MCP HTTP request failed ({error.__class__.__name__}; details omitted)"
            ) from error

    def start(self) -> None:
        if not self._start_modern():
            self._start_legacy()

    def _start_modern(self) -> bool:
        """Probe with server/discover; a legacy server falls back to initialize."""
        if not wants_modern(self.protocol_version):
            return False
        preferred = (
            self.protocol_version
            if is_modern_version(self.protocol_version)
            else SUPPORTED_PROTOCOL_VERSIONS[0]
        )
        self._header_version = preferred
        try:
            result = self._discover(preferred)
        except McpProtocolError as error:
            corrected = self._retry_discover(error, preferred)
            if corrected is None:
                return False
            result, preferred = corrected
        except McpClientError:
            return False
        if not isinstance(result, dict):
            return False
        chosen = pick_common_version(result.get("supportedVersions"), preferred)
        if chosen is None or not is_modern_version(chosen):
            return False
        if chosen != preferred:
            try:
                result = self._discover(chosen)
            except McpClientError:
                return False
            if not isinstance(result, dict):
                return False
        self.era = "modern"
        self._header_version = chosen
        self.negotiated_protocol_version = chosen
        self.server_info = _server_info(result)
        capabilities = result.get("capabilities")
        self.server_capabilities = capabilities if isinstance(capabilities, dict) else {}
        return True

    def _retry_discover(
        self, error: McpProtocolError, preferred: str
    ) -> tuple[Any, str] | None:
        if error.code != UNSUPPORTED_PROTOCOL_VERSION:
            return None
        chosen = pick_common_version(error.data.get("supported"), preferred)
        if chosen is None or not is_modern_version(chosen):
            return None
        self._header_version = chosen
        try:
            return self._discover(chosen), chosen
        except McpClientError:
            return None

    def _discover(self, version: str) -> Any:
        return self._exchange(
            "server/discover",
            {"_meta": modern_meta(version, _client_version())},
            timeout_seconds=self.startup_timeout_seconds,
        )

    def _start_legacy(self) -> None:
        self.era = "legacy"
        self._header_version = SUPPORTED_PROTOCOL_VERSIONS[-1]
        result = self._exchange(
            "initialize",
            {
                "protocolVersion": SUPPORTED_PROTOCOL_VERSIONS[-1]
                if wants_modern(self.protocol_version)
                else self.protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "safesink", "version": _client_version()},
            },
            timeout_seconds=self.startup_timeout_seconds,
        )
        if not isinstance(result, dict):
            raise McpClientError("MCP HTTP initialize result must be an object")
        self.negotiated_protocol_version = str(result.get("protocolVersion", ""))
        info = result.get("serverInfo")
        self.server_info = info if isinstance(info, dict) else {}
        capabilities = result.get("capabilities")
        self.server_capabilities = capabilities if isinstance(capabilities, dict) else {}
        # A notification has no id and the server normally answers 202.
        try:
            self._post(
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                method="notifications/initialized",
            )
        except McpClientError:
            pass

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> Any:
        if self.era == "modern":
            params = with_meta(
                params, self.negotiated_protocol_version, _client_version()
            )
            tool_name = ""
            if method == "tools/call" and isinstance(params.get("name"), str):
                tool_name = params["name"]
            return self._exchange(
                method, params, timeout_seconds=timeout_seconds, tool_name=tool_name
            )
        return self._exchange(method, params, timeout_seconds=timeout_seconds)

    def _exchange(
        self,
        method: str,
        params: dict[str, Any] | None,
        *,
        timeout_seconds: float | None = None,
        tool_name: str = "",
    ) -> Any:
        request_id = self._next_id
        self._next_id += 1
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        status, headers, body = self._post(
            payload,
            timeout_seconds=timeout_seconds,
            method=method,
            tool_name=tool_name,
        )
        if self.era == "legacy":
            session = {
                key.lower(): value for key, value in headers.items()
            }.get("mcp-session-id")
            if session:
                self.session_id = session
        return _result_from_body(body, headers, expected_id=request_id, status=status)

    def list_tools(self, *, max_pages: int = 100) -> list[dict[str, Any]]:
        cursor: str | None = None
        tools: list[dict[str, Any]] = []
        seen: set[str] = set()
        for _ in range(max_pages):
            params = {"cursor": cursor} if cursor else None
            result = self.request("tools/list", params)
            if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
                raise McpClientError("tools/list result must contain a tools array")
            for tool in result["tools"]:
                if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                    raise McpClientError("tools/list returned an invalid tool definition")
                if tool["name"] in seen:
                    raise McpClientError(f"tools/list returned duplicate tool {tool['name']!r}")
                seen.add(tool["name"])
                tools.append(tool)
            next_cursor = result.get("nextCursor")
            if next_cursor is None:
                return tools
            if not isinstance(next_cursor, str) or not next_cursor:
                raise McpClientError("tools/list returned an invalid nextCursor")
            cursor = next_cursor
        raise McpClientError(f"tools/list exceeded maxPages={max_pages}")

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        if not isinstance(result, dict):
            raise McpClientError("tools/call result must be an object")
        return result

    def close(self) -> None:
        self.session_id = None


def _is_event_stream(headers: dict[str, str]) -> bool:
    content_type = {key.lower(): value for key, value in headers.items()}.get(
        "content-type", ""
    )
    return content_type.split(";")[0].strip().lower() == "text/event-stream"


def _decode_event_stream(body: bytes) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for block in body.decode("utf-8", "replace").split("\n\n"):
        data = "\n".join(
            line[5:].strip()
            for line in block.splitlines()
            if line.startswith("data:")
        ).strip()
        if not data:
            continue
        try:
            message = json.loads(data)
        except json.JSONDecodeError as error:
            raise McpClientError("MCP HTTP event stream carried invalid JSON") from error
        if isinstance(message, dict):
            messages.append(message)
    return messages


def _result_from_body(
    body: bytes,
    headers: dict[str, str],
    *,
    expected_id: int,
    status: int,
) -> Any:
    if len(body) > 8 * 1024 * 1024:
        raise McpClientError("MCP HTTP response exceeded maxResponseBytes")
    if _is_event_stream(headers):
        messages = _decode_event_stream(body)
        message = next(
            (item for item in messages if item.get("id") == expected_id), None
        )
        if message is None:
            raise McpClientError("MCP HTTP event stream did not contain the response")
    else:
        if not body.strip():
            if 200 <= status < 300:
                return None
            raise McpClientError(f"MCP HTTP server returned {status} with no body")
        try:
            message = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise McpClientError("MCP HTTP server returned non-JSON content") from error
    if not isinstance(message, dict):
        raise McpClientError("MCP HTTP message must be a JSON object")
    if message.get("jsonrpc") != "2.0":
        raise McpClientError("MCP HTTP response has invalid jsonrpc version")
    if "error" in message:
        error_value = message["error"]
        if isinstance(error_value, dict):
            code = error_value.get("code", "unknown")
            raise McpProtocolError(
                code,
                error_value.get("data"),
                f"MCP error {code}; server-provided message omitted from report",
            )
        raise McpClientError("MCP error; server-provided value omitted from report")
    if "result" not in message:
        raise McpClientError("MCP HTTP response contains neither result nor error")
    return message["result"]
