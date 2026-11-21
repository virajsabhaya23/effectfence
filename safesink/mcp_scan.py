"""Generate an ``effectfence.mcp.v1`` manifest from a live server's own tool schemas.

Hand-authoring a case per tool is the main barrier to running conformance checks
across many servers. This module connects to a server, reads ``tools/list``, and
synthesizes minimal valid arguments from each tool's advertised ``inputSchema``.

Synthesized arguments are a best effort: a case whose call fails with a tool
error is reported as inconclusive by the verifier rather than as a conformance
finding, so argument bugs cannot masquerade as violations.
"""

from __future__ import annotations

import json
import re
import struct
import zlib
from pathlib import Path
from typing import Any

from .mcp_client import McpStdioClient, restricted_environment

PROBE_STRING = "safesink-probe"
PROBE_FILE_NAME = "safesink-probe.txt"
PROBE_MEDIA_NAME = "safesink-probe.png"
PROBE_COPY_NAME = "safesink-probe-copy.txt"
PROBE_DIR_NAME = "safesink-probe-dir"
PROBE_TEXT = "safesink probe fixture\n"
# A reachable, stable document: URL arguments need a real URL, not a placeholder
# string, or every network tool answers with an argument error.
PROBE_URL = "https://example.com/"
# Valid on SQLite, PostgreSQL and MySQL without knowing any schema. Database
# tools that require a particular table still fail, but harmlessly and
# observably, instead of on a syntax error.
PROBE_SQL = "select 1 as probe"

# Property-name tokens that name a filesystem location on their own. Matching is
# per token, not per substring: ``profile_id`` contains "file" but names an
# identifier, and synthesizing it as a path makes the server reject the call.
_PATH_NAME_TOKENS = {
    "path",
    "paths",
    "file",
    "files",
    "filename",
    "filenames",
    "filepath",
    "filepaths",
    "dir",
    "dirs",
    "directory",
    "directories",
    "folder",
    "folders",
    "location",
    "locations",
}
# Tokens that describe a location only when they are the entire name ("source",
# "destination") or sit beside a path noun ("source_path"). Inside a longer
# identifier they usually mean something else: ``source_language`` and
# ``target_audience`` are not files.
_WEAK_PATH_TOKENS = {
    "source",
    "src",
    "destination",
    "dest",
    "dst",
    "target",
    "from",
    "to",
}

# Signals that a path argument must be a directory; pointing a file at these
# yields tool errors that look like conformance findings but are argument bugs.
# Whole words only: "dir" as a substring matches "directories" in the allowed-path
# boilerplate that file tools carry, which then sends a directory to read_file.
_DIRECTORY_HINTS = ("directory", "directories", "folder", "folders", "tree", "walk")
_DIRECTORY_NAMES = {"dir", "directory", "folder", "dir_path", "directory_path"}
_DIRECTORY_TOOL_HINTS = ("list_", "tree", "search", "glob", "walk", "ls_")

# Signals that a path argument names media rather than text.
_MEDIA_HINTS = ("media", "image", "photo", "picture", "png", "jpeg", "jpg")

# Roles: a destination must be writable (and may not exist yet); a source must
# already exist, otherwise the tool errors for reasons unrelated to conformance.
# Matched per token, because "to" inside "token" and "out" inside "timeout"
# are not destination words.
_WRITE_ROLE_TOKENS = {"destination", "dest", "dst", "target", "output", "out", "to"}

# Many servers document a required identifier format with a quoted example, for
# example "Exact library ID (e.g. '/mongodb/docs')". Reusing that example is the
# difference between a rejected call and a real observation.
_EXAMPLE_PATTERN = re.compile(r"['\"](/[^'\"\s,]+)['\"]")
_URL_EXAMPLE_PATTERN = re.compile(r"https?://[^\s'\"<>),;]+")
# "for example https://..." is the description telling us what shape it wants.
_URL_HINT_PATTERN = re.compile(
    r"(?:e\.g\.|for example|example:)[^.]{0,40}?(https?://[^\s'\"<>),;]+)",
    re.IGNORECASE,
)

# Property names that carry a URL rather than an opaque string.
_URL_NAME_TOKENS = {"url", "uri", "href", "endpoint", "webhook"}
_URL_FORMATS = {"uri", "url", "iri"}

# "query" alone is a search box as often as it is SQL, so SQL detection needs a
# database word as well.
_SQL_NAME_TOKENS = {"sql", "statement"}
_SQL_TEXT_PATTERN = re.compile(
    r"\b(sql|select|table|column|rows?|database|schema)\b", re.IGNORECASE
)


class ScanError(ValueError):
    pass


def classify(tool: dict[str, Any]) -> str:
    annotations = tool.get("annotations") or {}
    if annotations.get("readOnlyHint") is True:
        return "read-only"
    if annotations.get("destructiveHint") is True:
        return "destructive"
    return "mutating"


def _tiny_png() -> bytes:
    """A valid 1x1 transparent PNG, so image tools get a real file to read."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    header = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    idat = zlib.compress(b"\x00\x00\x00\x00\x00")
    return header + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def seed_sandbox(root: str | Path) -> bool:
    """Create the probe fixtures synthesized arguments point at.

    Returns ``False`` when the sandbox cannot be written (the scan still runs, but
    path arguments then reference files that do not exist).
    """

    base = Path(root)
    try:
        base.mkdir(parents=True, exist_ok=True)
        (base / PROBE_FILE_NAME).write_text(PROBE_TEXT, encoding="utf-8")
        (base / PROBE_MEDIA_NAME).write_bytes(_tiny_png())
        directory = base / PROBE_DIR_NAME
        directory.mkdir(exist_ok=True)
        (directory / "nested.txt").write_text(PROBE_TEXT, encoding="utf-8")
    except OSError:
        return False
    return True


def _looks_like_path(name: str) -> bool:
    tokens = _name_tokens(name)
    if tokens & _PATH_NAME_TOKENS:
        return True
    return len(tokens) == 1 and tokens <= _WEAK_PATH_TOKENS


def _role(name: str) -> str:
    tokens = _name_tokens(name)
    if tokens & _WRITE_ROLE_TOKENS:
        return "write"
    # Defaults to read: a missing source file surfaces as a clear tool error,
    # while a destination the server was told about but never seeded is silent.
    return "read"


def _wants_directory(name: str, description: str, tool_name: str) -> bool:
    if name.lower() in _DIRECTORY_NAMES:
        return True
    property_text = f"{name} {description}".lower()
    # A file noun wins: file tools mention "allowed directories" in their
    # boilerplate, so testing for directory words first sends them a directory.
    if "file" in property_text or any(hint in property_text for hint in _MEDIA_HINTS):
        return False
    if any(hint in property_text for hint in _DIRECTORY_HINTS):
        return True
    lowered_tool = tool_name.lower()
    return any(hint in lowered_tool for hint in _DIRECTORY_TOOL_HINTS)


def _wants_media(name: str, description: str, tool_name: str) -> bool:
    haystack = f"{name} {description} {tool_name}".lower()
    return any(hint in haystack for hint in _MEDIA_HINTS)


def _description_example(description: str) -> str | None:
    match = _EXAMPLE_PATTERN.search(description)
    if match is None:
        return None
    example = match.group(1)
    return example if len(example) > 2 else None


def _name_tokens(name: str) -> set[str]:
    # Split camelCase too, so ``fileUrl`` yields ``file`` and ``url`` rather than
    # one opaque token that matches neither list.
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
    return set(re.findall(r"[a-z0-9]+", spaced.lower()))


def _looks_like_url(name: str, schema: dict[str, Any]) -> bool:
    if _name_tokens(name) & _URL_NAME_TOKENS:
        return True
    declared_format = schema.get("format")
    if isinstance(declared_format, str) and declared_format.lower() in _URL_FORMATS:
        return True
    description = schema.get("description")
    return isinstance(description, str) and _URL_HINT_PATTERN.search(description) is not None


def _url_value(schema: dict[str, Any]) -> str:
    description = schema.get("description")
    if isinstance(description, str):
        hinted = _URL_HINT_PATTERN.search(description)
        match = _URL_EXAMPLE_PATTERN.search(description)
        if hinted is not None:
            return hinted.group(1).rstrip(".,;:")
        if match is not None:
            return match.group(0).rstrip(".,;:")
    return PROBE_URL


def _looks_like_sql(name: str, description: str, tool_name: str) -> bool:
    if _name_tokens(name) & _SQL_NAME_TOKENS:
        return True
    text = f"{description} {tool_name}"
    return _SQL_TEXT_PATTERN.search(text) is not None


def _path_value(
    name: str,
    schema: dict[str, Any],
    sandbox: str,
    tool_name: str,
    role: str = "read",
) -> str:
    description = schema.get("description") if isinstance(schema.get("description"), str) else ""
    base = Path(sandbox)
    if _wants_directory(name, description, tool_name):
        return str(base)
    if _wants_media(name, description, tool_name):
        return str(base / PROBE_MEDIA_NAME)
    if role == "write":
        return str(base / PROBE_COPY_NAME)
    return str(base / PROBE_FILE_NAME)


def _scalar_for(
    schema: dict[str, Any],
    name: str,
    sandbox: str | None,
    tool_name: str = "",
    role: str = "read",
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
        return [
            _scalar_for(items, name, sandbox, tool_name, role) for _ in range(count)
        ]
    if declared == "object":
        return synthesize_arguments(schema, sandbox=sandbox, tool_name=tool_name)

    description = schema.get("description") if isinstance(schema.get("description"), str) else ""
    # URL detection runs first: a property named ``file_url`` is a URL, not a
    # sandbox path, even though it contains a path hint.
    is_url = _looks_like_url(name, schema)
    if sandbox and _looks_like_path(name) and not is_url:
        return _path_value(name, schema, sandbox, tool_name, role)
    if is_url:
        return _url_value(schema)
    if _looks_like_sql(name, description, tool_name):
        return PROBE_SQL
    example = _description_example(description)
    if example is not None:
        return example
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
        arguments[name] = _scalar_for(
            definition, name, sandbox, tool_name, _role(name)
        )
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


def _database_observer_id(database: Path, used: set[str]) -> str:
    stem = re.sub(r"[^a-z0-9]+", "-", database.stem.lower()).strip("-")
    base = f"sqlite-{stem or 'db'}"
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}-{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


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
    sqlite_queries: list[tuple[str, str]] | None = None,
    include_sandbox_observer: bool = False,
) -> dict[str, Any]:
    """Turn advertised tools into a manifest, skipping tools we must not auto-call.

    ``sqlite_queries`` adds one ``sqlite-query`` observer per ``(database, query)``
    pair. Because a database server's observable state is its rows, supplying a
    query replaces the sandbox file observer unless ``include_sandbox_observer``
    asks for both.
    """

    fixtures = seed_sandbox(observer_root)
    observers: list[dict[str, Any]] = []
    used_observer_ids: set[str] = set()
    database_queries = list(sqlite_queries or [])
    if include_sandbox_observer or not database_queries:
        observers.append(
            {"id": "sandbox", "kind": "filesystem", "root": observer_root, "sensitive": False}
        )
        used_observer_ids.add("sandbox")
    for database, query in database_queries:
        resolved = Path(database).expanduser().resolve()
        if not resolved.is_file():
            raise ScanError(
                f"SQLite database to observe does not exist: {resolved}; create and seed it "
                "before scanning"
            )
        observers.append(
            {
                "id": _database_observer_id(resolved, used_observer_ids),
                "kind": "sqlite-query",
                "database": str(resolved),
                "query": query,
                "sensitive": False,
            }
        )
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

        case: dict[str, Any] = {
            "id": case_id,
            "tool": name,
            "arguments": arguments,
            # Tells the verifier that a tool error here may be an argument bug,
            # not a conformance finding.
            "generatedArguments": True,
        }
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
        "observers": observers,
        "cases": cases,
        "policy": {"minimumToolCoverage": minimum_tool_coverage},
        "generated": {
            "by": "safesink mcp-scan",
            "skipped": skipped,
            "toolsAdvertised": len(tools),
            "probeFixtures": fixtures,
        },
    }


def write_manifest(manifest: dict[str, Any], destination: str | Path) -> Path:
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path.resolve()
