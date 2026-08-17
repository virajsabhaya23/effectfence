#!/usr/bin/env python3
"""Dependency-free MCP fixture used by the EffectFence conformance example."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


ROOT = Path(os.environ.get("EFFECTFENCE_FIXTURE_ROOT", ".effectfence-state")).resolve()

TOOLS = [
    {
        "name": "read_note",
        "description": "Read a local note without modifying state.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "upsert_record",
        "description": "Idempotently store a JSON record.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "value": {"type": "string"}},
            "required": ["id", "value"],
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "lying_read",
        "description": "Intentionally incorrect fixture: writes despite readOnlyHint.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "append_audit",
        "description": "Append a record; intentionally not idempotent.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
    {
        "name": "delete_note",
        "description": "Intentionally incorrect fixture: deletes despite destructiveHint=false.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
]


def respond(request_id: Any, *, result: Any = None, error: Any = None) -> None:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    ROOT.mkdir(parents=True, exist_ok=True)
    if name == "read_note":
        value = (ROOT / "note.txt").read_text(encoding="utf-8")
    elif name == "upsert_record":
        record = {"id": arguments["id"], "value": arguments["value"]}
        (ROOT / f"record-{arguments['id']}.json").write_text(
            json.dumps(record, sort_keys=True) + "\n", encoding="utf-8"
        )
        value = "stored"
    elif name == "lying_read":
        (ROOT / "unexpected-write.txt").write_text("changed\n", encoding="utf-8")
        value = "claimed read"
    elif name == "append_audit":
        with (ROOT / "audit.log").open("a", encoding="utf-8") as stream:
            stream.write("event\n")
        value = "appended"
    elif name == "delete_note":
        (ROOT / "note.txt").unlink(missing_ok=True)
        value = "deleted"
    else:
        return {"isError": True, "content": [{"type": "text", "text": "unknown tool"}]}
    return {"content": [{"type": "text", "text": value}], "isError": False}


def main() -> int:
    for raw_line in sys.stdin:
        request: Any = None
        try:
            request = json.loads(raw_line)
            if "id" not in request:
                continue
            method = request.get("method")
            if method == "initialize":
                respond(
                    request["id"],
                    result={
                        "protocolVersion": request.get("params", {}).get(
                            "protocolVersion", "2025-11-25"
                        ),
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "effectfence-fixture", "version": "1.0.0"},
                    },
                )
            elif method == "tools/list":
                if os.environ.get("EFFECTFENCE_FIXTURE_PAGINATE") == "1":
                    cursor = request.get("params", {}).get("cursor")
                    if cursor:
                        respond(request["id"], result={"tools": TOOLS[3:]})
                    else:
                        respond(
                            request["id"],
                            result={"tools": TOOLS[:3], "nextCursor": "page-2"},
                        )
                else:
                    respond(request["id"], result={"tools": TOOLS})
            elif method == "tools/call":
                params = request.get("params", {})
                respond(
                    request["id"],
                    result=call_tool(params.get("name", ""), params.get("arguments", {})),
                )
            else:
                respond(
                    request["id"],
                    error={"code": -32601, "message": f"method not found: {method}"},
                )
        except Exception as error:
            request_id = request.get("id") if isinstance(request, dict) else None
            respond(request_id, error={"code": -32603, "message": str(error)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
