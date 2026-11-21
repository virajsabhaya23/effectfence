"""Protocol revisions SafeSink speaks, and the metadata each one carries.

The 2026-07-28 revision made MCP stateless: there is no ``initialize``
handshake, and every request carries the protocol version, client identity and
capabilities in ``_meta``. Servers must implement ``server/discover`` and answer
an unknown version with ``UnsupportedProtocolVersionError`` (-32022) listing what
they support.

SafeSink is dual-era. A manifest may pin an era; otherwise the client probes
``server/discover`` and falls back to the 2025-11-25 handshake when the server is
not modern, so older servers keep working unchanged.
"""

from __future__ import annotations

from typing import Any

MODERN_PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = (MODERN_PROTOCOL_VERSION, LEGACY_PROTOCOL_VERSION)
PROTOCOL_VERSION_CHOICES = ("auto", *SUPPORTED_PROTOCOL_VERSIONS)

UNSUPPORTED_PROTOCOL_VERSION = -32022

META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

CLIENT_NAME = "safesink"


def is_modern_version(version: object) -> bool:
    """Protocol revisions sort by date, so the comparison is the feature test."""
    return isinstance(version, str) and version >= MODERN_PROTOCOL_VERSION


def wants_modern(requested: str) -> bool:
    """``auto`` means "probe, then use the newest common version"."""
    return requested in ("", "auto") or is_modern_version(requested)


def modern_meta(version: str, client_version: str) -> dict[str, Any]:
    return {
        META_PROTOCOL_VERSION: version,
        META_CLIENT_INFO: {"name": CLIENT_NAME, "version": client_version},
        META_CLIENT_CAPABILITIES: {},
    }


def with_meta(
    params: dict[str, Any] | None, version: str, client_version: str
) -> dict[str, Any]:
    merged = dict(params or {})
    meta = dict(merged.get("_meta") or {})
    meta.setdefault(META_PROTOCOL_VERSION, version)
    meta.setdefault(META_CLIENT_INFO, {"name": CLIENT_NAME, "version": client_version})
    meta.setdefault(META_CLIENT_CAPABILITIES, {})
    merged["_meta"] = meta
    return merged


def requested_version(params: dict[str, Any] | None) -> str | None:
    if not isinstance(params, dict):
        return None
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        return None
    value = meta.get(META_PROTOCOL_VERSION)
    return value if isinstance(value, str) else None


def server_info(result: dict[str, Any]) -> dict[str, Any]:
    """Modern results name the server under ``_meta``; legacy ones inline it."""
    meta = result.get("_meta")
    if isinstance(meta, dict):
        info = meta.get(META_SERVER_INFO)
        if isinstance(info, dict):
            return info
    inline = result.get("serverInfo")
    return inline if isinstance(inline, dict) else {}


def pick_common_version(
    supported: object, preferred: str
) -> str | None:
    """Choose the newest version both sides speak, preferring a modern one."""
    if not isinstance(supported, list):
        return None
    known = [item for item in supported if item in SUPPORTED_PROTOCOL_VERSIONS]
    for candidate in sorted(known, reverse=True):
        if is_modern_version(candidate):
            return candidate
    if preferred in known:
        return preferred
    return known[0] if known else None
