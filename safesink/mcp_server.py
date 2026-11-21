"""A Model Context Protocol server that puts conformance checks in an agent's hands.

It is lint-only by default: ``lint_manifest`` reads declarations and never starts
a process, so an agent can check its own manifest without the tool becoming a way
to run arbitrary commands. Verifying a manifest executes the server under test,
so that tool stays unavailable until the operator passes ``--allow-exec``.

Both protocol eras are served: a legacy client's ``initialize`` handshake and a
stateless 2026-07-28 client's per-request ``_meta``. ``server/discover`` is
implemented as the modern revision requires, and tool results carry
``structuredContent`` next to the text so an agent can consume them directly.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, TextIO

from . import __version__
from .mcp_lint import lint_manifest
from .mcp_protocol import (
    LEGACY_PROTOCOL_VERSION,
    META_SERVER_INFO,
    MODERN_PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    UNSUPPORTED_PROTOCOL_VERSION,
    requested_version,
)
from .mcp_verifier import ManifestError, load_manifest, verify_manifest

LIST_TTL_MS = 3_600_000

IMPORT_META = "_meta"
QUERY_SCHEMA = {"type": "object", "additionalProperties": True}

LINT_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["findings", "score", "toolsAdvertised"],
    "properties": {
        "findings": {"type": "array"},
        "score": {
            "type": "object",
            "required": ["value", "max"],
            "properties": {"value": {"type": "number"}, "max": {"type": "number"}},
        },
        "toolsAdvertised": {"type": "integer"},
    },
}

RULES_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["rules"],
    "properties": {"rules": {"type": "array"}},
}

VERIFY_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["verdict"],
    "properties": {
        "verdict": {"enum": ["pass", "inconclusive", "fail"]},
        "score": {"type": "object"},
        "cases": {"type": "array"},
    },
}

LINT_TOOL: dict[str, Any] = {
    "name": "lint_manifest",
    "title": "Lint a SafeSink manifest",
    "description": (
        "Lint a SafeSink manifest's declared tool annotations without starting the "
        "server. Returns findings, a code for each, and an annotation score."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Manifest path, relative to the server's root.",
            }
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    "outputSchema": LINT_OUTPUT_SCHEMA,
    "annotations": {"readOnlyHint": True, "openWorldHint": False},
}

RULES_TOOL: dict[str, Any] = {
    "name": "describe_rules",
    "title": "List SafeSink lint rules",
    "description": "List the lint rules and what each one asks of a tool declaration.",
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    "outputSchema": RULES_OUTPUT_SCHEMA,
    "annotations": {"readOnlyHint": True, "openWorldHint": False},
}

VERIFY_TOOL: dict[str, Any] = {
    "name": "verify_manifest",
    "title": "Verify a manifest's declared effects",
    "description": (
        "Run the conformance suite for a manifest. This starts the server under "
        "test, so it is only available when the operator passed --allow-exec."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Manifest path, relative to the root."},
            "timeoutSeconds": {"type": "number", "description": "Whole-run budget."},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    "outputSchema": VERIFY_OUTPUT_SCHEMA,
    "annotations": {
        "readOnlyHint": False,
        "destructiveHint": True,
        "openWorldHint": True,
    },
}

RULE_CATALOG = [
    {
        "code": "MISSING_READONLY",
        "ask": "declare readOnlyHint so a caller can tell reads from writes",
    },
    {"code": "MISSING_DESTRUCTIVE", "ask": "declare destructiveHint for deleting tools"},
    {"code": "MISSING_IDEMPOTENT", "ask": "declare idempotentHint where repeats are safe"},
    {"code": "MISSING_OPEN_WORLD", "ask": "declare openWorldHint for network-facing tools"},
    {
        "code": "DESTRUCTIVE_WITHOUT_CONFIRMATION",
        "ask": "document the confirmation a destructive call needs",
    },
    {"code": "VOLATILE_ARGUMENT", "ask": "accept a caller-supplied seed instead of a timestamp"},
    {
        "code": "NETWORK_TOOL_WITHOUT_HOST_ALLOWLIST",
        "ask": "accept a URL argument only with an allowlist",
    },
]


class ServerError(Exception):
    """A request the server refuses; answered as a tool error, never a crash."""


class SafeSinkMcpServer:
    def __init__(
        self,
        *,
        root: Path,
        allow_exec: bool = False,
        max_bytes: int = 1_000_000,
    ) -> None:
        self.root = root.resolve()
        self.allow_exec = allow_exec
        self.max_bytes = max_bytes

    # -- protocol ---------------------------------------------------------

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method = request.get("method")
        request_id = request.get("id")
        if request_id is None:
            # A notification (initialized, cancelled) needs no answer.
            return None
        params = request.get("params") or {}
        version = requested_version(params)
        modern = version is not None
        try:
            if modern and version not in SUPPORTED_PROTOCOL_VERSIONS:
                return _protocol_version_error(request_id, str(version))
            if method == "initialize":
                result: Any = {
                    "protocolVersion": LEGACY_PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": _identity(),
                }
            elif method == "server/discover":
                if not modern:
                    return _error(
                        request_id, -32601, "server/discover requires a protocol version in _meta"
                    )
                result = {
                    "resultType": "complete",
                    "supportedVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
                    "capabilities": {"tools": {}},
                    "_meta": {META_SERVER_INFO: _identity()},
                    "instructions": (
                        "Lint manifests with lint_manifest; verification that starts the "
                        "server under test requires --allow-exec."
                    ),
                    "ttlMs": LIST_TTL_MS,
                    "cacheScope": "public",
                }
            elif method == "ping" and not modern:
                result = {}
            elif method == "tools/list":
                tools = [LINT_TOOL, RULES_TOOL]
                if self.allow_exec:
                    tools.append(VERIFY_TOOL)
                result = _modern({"tools": tools}, modern)
            elif method == "tools/call":
                result = self._call(params, modern=modern)
            else:
                return _error(request_id, -32601, f"unknown method: {method}")
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except ServerError as error:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": _modern(
                    {
                        "content": [{"type": "text", "text": str(error)}],
                        "isError": True,
                    },
                    modern,
                ),
            }
        except Exception as error:  # never kill the session on one bad call
            return _error(request_id, -32603, f"{error.__class__.__name__}: {error}")

    def _call(self, params: dict[str, Any], *, modern: bool) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name == "describe_rules":
            return _text({"rules": RULE_CATALOG}, modern=modern)
        if name == "lint_manifest":
            manifest = self._manifest(arguments)
            return _text(lint_manifest(manifest), modern=modern)
        if name == "verify_manifest":
            if not self.allow_exec:
                raise ServerError(
                    "verify_manifest starts the server under test and is disabled: "
                    "start safesink-mcp with --allow-exec to enable it"
                )
            manifest_path = self._path(arguments)
            report = verify_manifest(
                manifest_path,
                timeout_seconds=_optional_number(arguments.get("timeoutSeconds")),
            )
            return _text(report, modern=modern)
        raise ServerError(f"unknown tool: {name!r}")

    # -- filesystem -------------------------------------------------------

    def _path(self, arguments: dict[str, Any]) -> Path:
        raw = arguments.get("path")
        if not isinstance(raw, str) or not raw:
            raise ServerError("path is required")
        candidate = (self.root / raw).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ServerError("path must stay inside the server root")
        if not candidate.is_file():
            raise ServerError(f"not a file: {raw}")
        if candidate.stat().st_size > self.max_bytes:
            raise ServerError(f"manifest is larger than {self.max_bytes} bytes")
        return candidate

    def _manifest(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = self._path(arguments)
        try:
            manifest, _ = load_manifest(path)
        except ManifestError as error:
            raise ServerError(f"invalid manifest: {error}") from error
        return manifest


def _identity() -> dict[str, Any]:
    return {"name": "safesink", "version": __version__}


def _modern(result: dict[str, Any], modern: bool) -> dict[str, Any]:
    """Modern results are typed and cacheable; legacy clients get the old shape."""
    if modern:
        result = {"resultType": "complete", **result}
        result.setdefault("ttlMs", LIST_TTL_MS)
        result.setdefault("cacheScope", "public")
    return result


def _optional_number(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value > 0:
            return float(value)
    raise ServerError("timeoutSeconds must be a positive number")


def _text(payload: Any, *, modern: bool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
        "isError": False,
    }
    if modern:
        # Structured content is what an agent consumes; the text block keeps
        # older clients working with the same payload.
        result["structuredContent"] = (
            payload if isinstance(payload, dict) else {"value": payload}
        )
    return _modern(result, modern)


def _protocol_version_error(request_id: object, requested: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": UNSUPPORTED_PROTOCOL_VERSION,
            "message": "Unsupported protocol version",
            "data": {
                "supported": list(SUPPORTED_PROTOCOL_VERSIONS),
                "requested": requested,
            },
        },
    }


def _error(request_id: object, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def serve(stdin: TextIO, stdout: TextIO, *, server: SafeSinkMcpServer) -> None:
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            response: dict[str, Any] | None = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "invalid JSON"},
            }
        else:
            if not isinstance(request, dict):
                response = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "request must be an object"},
                }
            else:
                response = server.handle(request)
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="safesink-mcp",
        description=(
            "Expose SafeSink's manifest lint (and, with --allow-exec, verification) "
            "as MCP tools. Lint-only by default so an agent cannot run commands."
        ),
    )
    parser.add_argument(
        "--allow-exec",
        action="store_true",
        help="enable verify_manifest, which starts the server under test",
    )
    parser.add_argument("--root", default=".", help="directory paths must stay inside")
    arguments = parser.parse_args(argv)
    server = SafeSinkMcpServer(root=Path(arguments.root), allow_exec=arguments.allow_exec)
    serve(sys.stdin, sys.stdout, server=server)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
