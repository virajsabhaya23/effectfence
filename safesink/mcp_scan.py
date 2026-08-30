"""Generate an ``effectfence.mcp.v1`` manifest from a live server's own tool schemas.

Hand-authoring a case per tool is the main barrier to running conformance checks
across many servers. This module connects to a server, reads ``tools/list``, and
synthesizes minimal valid arguments from each tool's advertised ``inputSchema``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .mcp_client import McpStdioClient, restricted_environment

PROBE_STRING = "effectfence-probe"
PROBE_PATH_STEM = "effectfence-probe"

# Property names that should resolve to a sandbox path rather than a bare string.
_PATH_HINTS = ("path", "file", "filename", "directory", "dir", "folder", "location")

# Signals that a path argument must be a directory; pointing a file at these
# yields tool errors that look like conformance findings but are argument bugs.
_DIRECTORY_HINTS = ("directory", "dir", "folder", "tree", "list", "search", "walk")


class ScanError(ValueError):
    pass


def classify(tool: dict[str, Any]) -> str:
    annotations = tool.get("annotations") or {}
    if annotations.get("readOnlyHint") is True:
        return "read-only"
    if annotations.get("destructiveHint") is True:
        return "destructive"
    return "mutating"


def _looks_like_path(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in _PATH_HINTS)


def _wants_directory(name: str, description: str, tool_name: str) -> bool:
    haystack = f"{name} {description} {tool_name}".lower()
    return any(hint in haystack for hint in _DIRECTORY_HINTS)


def _path_value(
    name: str, schema: dict[str, Any], sandbox: str, tool_name: str
) -> str:
    description = schema.get("description") if isinstance(schema.get("description"), str) else ""
    if _wants_directory(name, description, tool_name):
        return sandbox
    return str(Path(sandbox) / f"{PROBE_PATH_STEM}.txt")


def _scalar_for(
    schema: dict[str, Any], name: str, sandbox: str | None, tool_name: str = ""
) -> Any:
    if "const" in schema:
        return schema["const"]
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        return schema["enum"][0]
    if "default" in schema:
        return schema["default"]

    declared = schema.get("type")
    if isinstance(declared, list):
        declared = next((item for item in declared if item != "null"), None)

    if declared == "boolean":
        return False
    if declared in {"integer", "number"}:
        minimum = schema.get("minimum", schema.get("exclusiveMinimum"))
        if isinstance(minimum, (int, float)) and not isinstance(minimum, bool):
            value = minimum + 1 if "exclusiveMinimum" in schema else minimum
            return int(value) if declared == "integer" else float(value)
        return 1 if declared == "integer" else 1.0
    if declared == "array":
        items = schema.get("items")
        count = schema.get("minItems", 0)
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
            count = 1
        if not isinstance(items, dict):
            return [PROBE_STRING] * count
        return [_scalar_for(items, name, sandbox, tool_name) for _ in range(count)]
    if declared == "object":
        return synthesize_arguments(schema, sandbox=sandbox, tool_name=tool_name)

    if sandbox and _looks_like_path(name):
        return _path_value(name, schema, sandbox, tool_name)
    return PROBE_STRING


def synthesize_arguments(
    schema: dict[str, Any] | None, *, sandbox: str | None = None, tool_name: str = ""
) -> dict[str, Any]:
    """Build the smallest argument object that satisfies a tool's input schema."""

    if not isinstance(schema, dict):
        return {}
    for combinator in ("anyOf", "oneOf", "allOf"):
        if isinstance(schema.get(combinator), list) and schema[combinator]:
            branch = schema[combinator][0]
            if isinstance(branch, dict):
                merged = {key: value for key, value in schema.items() if key != combinator}
                merged.update(branch)
                schema = merged
            break

    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return {}
    required = schema.get("required")
    names = required if isinstance(required, list) else list(properties)

    arguments: dict[str, Any] = {}
    for name in names:
        if not isinstance(name, str):
            continue
        definition = properties.get(name)
        if not isinstance(definition, dict):
            continue
        arguments[name] = _scalar_for(definition, name, sandbox, tool_name)
    return arguments


def list_server_tools(
    command: list[str],
    *,
    cwd: Path,
    inherit_env: list[str] | None = None,
    env: dict[str, str] | None = None,
    protocol_version: str = "2025-11-25",
    startup_timeout_seconds: float = 60.0,
    request_timeout_seconds: float = 60.0,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    environment = restricted_environment(
        list(inherit_env or ["HOME"]), dict(env or {})
    )
    with McpStdioClient(
        command,
        cwd=cwd,
        environment=environment,
        protocol_version=protocol_version,
        startup_timeout_seconds=startup_timeout_seconds,
        request_timeout_seconds=request_timeout_seconds,
    ) as client:
        return client.list_tools(), dict(client.server_info)


def build_manifest(
    tools: list[dict[str, Any]],
    *,
    command: list[str],
    observer_root: str,
    cwd: str = ".",
    include_destructive: bool = False,
    setup_command: list[str] | None = None,
    minimum_tool_coverage: float = 0.0,
    inherit_env: list[str] | None = None,
    env: dict[str, str] | None = None,
    protocol_version: str = "2025-11-25",
) -> dict[str, Any]:
    """Turn advertised tools into a manifest, skipping tools we must not auto-call."""

    cases: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    used_ids: set[str] = set()

    for tool in tools:
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            continue
        kind = classify(tool)
        if kind == "destructive" and not include_destructive:
            skipped.append({"tool": name, "reason": "declared destructive"})
            continue

        arguments = synthesize_arguments(
            tool.get("inputSchema"), sandbox=observer_root, tool_name=name
        )
        case_id = f"{name}-{kind}"
        suffix = 2
        while case_id in used_ids:
            case_id = f"{name}-{kind}-{suffix}"
            suffix += 1
        used_ids.add(case_id)

        case: dict[str, Any] = {"id": case_id, "tool": name, "arguments": arguments}
        annotations = tool.get("annotations") or {}
        contract = {
            hint: annotations[hint]
            for hint in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
            if isinstance(annotations.get(hint), bool)
        }
        if contract:
            case["contract"] = contract
        if setup_command:
            case["setup"] = {"command": list(setup_command)}
        cases.append(case)

    if not cases:
        raise ScanError(
            "no testable tools were generated; the server advertised no tools, or "
            "every tool is declared destructive (use include_destructive to test them)"
        )

    server: dict[str, Any] = {
        "transport": "stdio",
        "command": list(command),
        "cwd": cwd,
        "protocolVersion": protocol_version,
    }
    if inherit_env:
        server["inheritEnv"] = list(inherit_env)
    if env:
        server["env"] = dict(env)

    return {
        "schemaVersion": "effectfence.mcp.v1",
        "server": server,
        "observers": [
            {"id": "sandbox", "kind": "filesystem", "root": observer_root, "sensitive": False}
        ],
        "cases": cases,
        "policy": {"minimumToolCoverage": minimum_tool_coverage},
        "generated": {
            "by": "effectfence mcp-scan",
            "skipped": skipped,
            "toolsAdvertised": len(tools),
        },
    }


def write_manifest(manifest: dict[str, Any], destination: str | Path) -> Path:
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path.resolve()
