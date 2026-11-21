"""Read the MCP client configs people already have.

Almost every user meets MCP through a host config file - Claude Desktop,
VS Code, Cursor - before they ever write a SafeSink manifest. Reading those files
is the shortest path from "I have five servers configured" to "here is which of
them contradict their own annotations".

Only the server definitions are read. Nothing is executed here, and no other key
in the file (tokens, prompts, model settings) is touched or reported.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class HostConfigError(ValueError):
    """Raised when a config file cannot be read or has no usable servers."""


@dataclass(frozen=True)
class ConfiguredServer:
    name: str
    command: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None

    @property
    def kind(self) -> str:
        return "streamable-http" if self.url else "stdio"


def _servers_table(document: Any) -> dict[str, Any]:
    """Find the server map in the shapes hosts actually use."""
    if not isinstance(document, dict):
        raise HostConfigError("config must be a JSON object")
    for key in ("mcpServers", "servers"):
        table = document.get(key)
        if isinstance(table, dict):
            return table
    mcp = document.get("mcp")
    if isinstance(mcp, dict):
        for key in ("mcpServers", "servers"):
            table = mcp.get(key)
            if isinstance(table, dict):
                return table
    raise HostConfigError(
        "no mcpServers or servers map found; expected a Claude Desktop, VS Code or Cursor config"
    )


def load_host_config(path: str | Path) -> dict[str, ConfiguredServer]:
    source = Path(path).expanduser()
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise HostConfigError(f"config not found: {source}") from error
    except OSError as error:
        raise HostConfigError(f"could not read config: {error}") from error
    except json.JSONDecodeError as error:
        raise HostConfigError(f"config is not valid JSON: {error}") from error

    servers: dict[str, ConfiguredServer] = {}
    for name, entry in _servers_table(document).items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            continue
        url = entry.get("url")
        if isinstance(url, str) and url.strip():
            servers[name] = ConfiguredServer(name=name, url=url.strip())
            continue
        command = entry.get("command")
        args = entry.get("args")
        if not isinstance(command, str) or not command.strip():
            continue
        argv = [command]
        if isinstance(args, list):
            argv.extend(str(item) for item in args if isinstance(item, (str, int, float)))
        env = {
            key: str(value)
            for key, value in (entry.get("env") or {}).items()
            if isinstance(key, str)
        } if isinstance(entry.get("env"), dict) else {}
        cwd = entry.get("cwd") if isinstance(entry.get("cwd"), str) else None
        servers[name] = ConfiguredServer(
            name=name, command=argv, env=env, cwd=cwd
        )
    if not servers:
        raise HostConfigError("config defines no servers with a command or url")
    return servers


def default_config_paths() -> list[Path]:
    """Hosts usually keep one config per machine; try the usual places."""
    home = Path.home()
    return [
        Path(".mcp.json"),
        Path(".vscode/mcp.json"),
        Path(".cursor/mcp.json"),
        home / "Library/Application Support/Claude/claude_desktop_config.json",
        home / ".config/Claude/claude_desktop_config.json",
        home / ".config/Code/User/mcp.json",
    ]


def existing_config_paths() -> list[Path]:
    return [path for path in default_config_paths() if path.is_file()]
