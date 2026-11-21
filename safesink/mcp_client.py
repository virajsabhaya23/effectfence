from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

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

# One probe per server command per process: the era is a property of the server,
# not of a single request, and repeating a 1s timeout per case would be waste.
_ERA_CACHE: dict[tuple[str, ...], str] = {}


def _client_version() -> str:
    # Imported lazily: this module is imported while ``safesink/__init__`` runs.
    from . import __version__

    return __version__


class McpClientError(RuntimeError):
    """Raised when an MCP server cannot complete the requested protocol operation."""


class McpProtocolError(McpClientError):
    """A JSON-RPC error, kept structured so version negotiation can react to it."""

    def __init__(self, code: object, data: object = None, message: str = "") -> None:
        self.code = code
        self.data = data if isinstance(data, dict) else {}
        super().__init__(message or f"MCP error {code}")


def _negotiate_probe_version(error: McpProtocolError) -> str | None:
    if error.code != UNSUPPORTED_PROTOCOL_VERSION:
        return None
    supported = error.data.get("supported")
    return pick_common_version(supported, SUPPORTED_PROTOCOL_VERSIONS[0])


class McpStdioClient:
    """Small, dependency-free MCP stdio client used by the conformance runner.

    MCP stdio is newline-delimited JSON-RPC. The client deliberately never invokes a
    shell and keeps stderr separate from protocol stdout.
    """

    def __init__(
        self,
        command: list[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        request_timeout_seconds: float = 30.0,
        startup_timeout_seconds: float = 15.0,
        protocol_version: str = "auto",
        max_message_bytes: int = 8 * 1024 * 1024,
        probe_timeout_seconds: float = 1.0,
    ) -> None:
        if not command or not all(isinstance(item, str) and item for item in command):
            raise ValueError("MCP command must be a non-empty array of strings")
        if request_timeout_seconds <= 0 or startup_timeout_seconds <= 0:
            raise ValueError("MCP timeouts must be positive")
        if max_message_bytes < 1024:
            raise ValueError("MCP maxMessageBytes must be at least 1024")
        self.command = list(command)
        self.cwd = cwd.resolve()
        self.environment = dict(environment)
        self.request_timeout_seconds = request_timeout_seconds
        self.startup_timeout_seconds = startup_timeout_seconds
        self.protocol_version = protocol_version
        self.max_message_bytes = max_message_bytes
        self.probe_timeout_seconds = probe_timeout_seconds
        self.era = "legacy"
        self.process: subprocess.Popen[str] | None = None
        self.server_info: dict[str, Any] = {}
        self.server_capabilities: dict[str, Any] = {}
        self.negotiated_protocol_version = ""
        self._next_id = 1
        self._stdout: queue.Queue[dict[str, Any] | BaseException] = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=200)
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None

    def __enter__(self) -> McpStdioClient:
        self.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    @property
    def stderr_tail(self) -> str:
        return "".join(self._stderr)[-16_384:]

    def start(self) -> None:
        if self.process is not None:
            raise McpClientError("MCP client is already started")
        if not self.cwd.is_dir():
            raise McpClientError(f"MCP server cwd does not exist: {self.cwd}")
        try:
            self.process = subprocess.Popen(
                self.command,
                cwd=self.cwd,
                env=self.environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                shell=False,
            )
        except OSError as error:
            raise McpClientError(f"could not start MCP server: {error}") from error
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._reader.start()
        self._stderr_reader.start()
        try:
            if self._start_modern():
                return
            self._start_legacy()
        except BaseException:
            self.close()
            raise

    # -- era detection -----------------------------------------------------

    def _start_modern(self) -> bool:
        """Probe ``server/discover``; return False when the server is legacy.

        On stdio there is no status code to inspect, so a recognised modern error
        means modern and anything else (an error, silence, a timeout) means the
        server predates stateless MCP.
        """
        if not wants_modern(self.protocol_version):
            return False
        cached = _ERA_CACHE.get(tuple(self.command))
        if cached == "legacy":
            return False
        preferred = (
            self.protocol_version
            if is_modern_version(self.protocol_version)
            else SUPPORTED_PROTOCOL_VERSIONS[0]
        )
        try:
            result = self._discover(preferred)
        except McpProtocolError as error:
            negotiated = _negotiate_probe_version(error)
            if negotiated is None or not is_modern_version(negotiated):
                _ERA_CACHE[tuple(self.command)] = "legacy"
                return False
            try:
                result = self._discover(negotiated)
            except McpClientError:
                return False
            preferred = negotiated
        except McpClientError:
            _ERA_CACHE[tuple(self.command)] = "legacy"
            return False
        if not isinstance(result, dict):
            _ERA_CACHE[tuple(self.command)] = "legacy"
            return False
        supported = result.get("supportedVersions")
        chosen = pick_common_version(supported, preferred)
        if chosen is None or not is_modern_version(chosen):
            _ERA_CACHE[tuple(self.command)] = "legacy"
            return False
        if chosen != preferred:
            try:
                result = self._discover(chosen)
            except McpClientError:
                return False
        _ERA_CACHE[tuple(self.command)] = "modern"
        self.era = "modern"
        self.negotiated_protocol_version = chosen
        self.server_info = _server_info(result if isinstance(result, dict) else {})
        self.server_capabilities = _object(
            (result or {}).get("capabilities") if isinstance(result, dict) else {}
        )
        return True

    def _discover(self, version: str) -> Any:
        return self.request(
            "server/discover",
            {"_meta": modern_meta(version, _client_version())},
            timeout_seconds=self.probe_timeout_seconds,
        )

    def _start_legacy(self) -> None:
        result = self.request(
            "initialize",
            {
                "protocolVersion": self.protocol_version
                if not wants_modern(self.protocol_version)
                else SUPPORTED_PROTOCOL_VERSIONS[-1],
                "capabilities": {},
                "clientInfo": {"name": "safesink", "version": _client_version()},
            },
            timeout_seconds=self.startup_timeout_seconds,
        )
        if not isinstance(result, dict):
            raise McpClientError("MCP initialize result must be an object")
        self.era = "legacy"
        self.negotiated_protocol_version = str(result.get("protocolVersion", ""))
        self.server_info = _object(result.get("serverInfo"))
        self.server_capabilities = _object(result.get("capabilities"))
        self.notify("notifications/initialized")

    def _read_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while True:
                line = self.process.stdout.readline(self.max_message_bytes + 1)
                if not line:
                    break
                if len(line.encode("utf-8")) > self.max_message_bytes:
                    raise McpClientError("MCP server response exceeded maxMessageBytes")
                if not line.endswith("\n"):
                    raise McpClientError(
                        "MCP server response exceeded maxMessageBytes or lacked a newline"
                    )
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as error:
                    raise McpClientError(
                        "MCP server wrote non-JSON content to protocol stdout"
                    ) from error
                if not isinstance(message, dict):
                    raise McpClientError("MCP server message must be a JSON object")
                self._stdout.put(message)
            self._stdout.put(
                McpClientError(
                    "MCP server closed protocol stdout before completing a request"
                )
            )
        except BaseException as error:
            self._stdout.put(error)

    def _read_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        while True:
            chunk = self.process.stderr.readline(4097)
            if not chunk:
                break
            self._stderr.append(chunk)

    def _write(self, message: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise McpClientError("MCP client is not started")
        if self.process.poll() is not None:
            raise McpClientError(self._exit_message())
        encoded = json.dumps(message, separators=(",", ":"), ensure_ascii=False)
        if len(encoded.encode("utf-8")) > self.max_message_bytes:
            raise McpClientError("MCP client request exceeded maxMessageBytes")
        try:
            self.process.stdin.write(encoded + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise McpClientError(self._exit_message()) from error

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._write(message)

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> Any:
        request_id = self._next_id
        self._next_id += 1
        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None or self.era == "modern":
            message["params"] = self._params(params)
        self._write(message)
        deadline = time.monotonic() + (
            timeout_seconds
            if timeout_seconds is not None
            else self.request_timeout_seconds
        )
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpClientError(f"MCP request {method!r} timed out")
            try:
                response = self._stdout.get(timeout=remaining)
            except queue.Empty as error:
                raise McpClientError(f"MCP request {method!r} timed out") from error
            if isinstance(response, BaseException):
                raise McpClientError(str(response)) from response
            if "id" not in response:
                # Server notifications and progress messages do not complete a request.
                continue
            if "method" in response:
                # The client advertises no server-request capabilities.
                self._write(
                    {
                        "jsonrpc": "2.0",
                        "id": response["id"],
                        "error": {
                            "code": -32601,
                            "message": "EffectFence client capability not available",
                        },
                    }
                )
                continue
            if response.get("id") != request_id:
                raise McpClientError(
                    f"MCP server returned unexpected response id {response.get('id')!r}"
                )
            if response.get("jsonrpc") != "2.0":
                raise McpClientError("MCP server response has invalid jsonrpc version")
            if "error" in response:
                error_value = response["error"]
                if isinstance(error_value, dict):
                    code = error_value.get("code", "unknown")
                    raise McpProtocolError(
                        code,
                        error_value.get("data"),
                        f"MCP error {code}; server-provided message omitted from report",
                    )
                raise McpClientError("MCP error; server-provided value omitted from report")
            if "result" not in response:
                raise McpClientError("MCP response contains neither result nor error")
            return response["result"]

    def _params(self, params: dict[str, Any] | None) -> dict[str, Any] | None:
        if self.era != "modern":
            return params
        return with_meta(
            params, self.negotiated_protocol_version or SUPPORTED_PROTOCOL_VERSIONS[0],
            _client_version(),
        )

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

    def _exit_message(self) -> str:
        code = self.process.poll() if self.process else None
        suffix = "; server stderr omitted from report" if self.stderr_tail else ""
        return f"MCP server exited unexpectedly with code {code}{suffix}"

    def close(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def restricted_environment(
    inherited_names: list[str], explicit: dict[str, str], *, base: dict[str, str] | None = None
) -> dict[str, str]:
    """Build a small subprocess environment without copying arbitrary credentials."""

    source = os.environ if base is None else base
    always = ("PATH", "SystemRoot", "WINDIR", "TMPDIR", "TEMP", "TMP")
    environment = {
        name: source[name]
        for name in dict.fromkeys((*always, *inherited_names))
        if name in source
    }
    environment.update(explicit)
    environment.setdefault("PYTHONUNBUFFERED", "1")
    return environment
