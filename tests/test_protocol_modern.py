"""Dual-era behavior: stateless 2026-07-28 servers and the legacy fallback."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from safesink.mcp_client import McpStdioClient, restricted_environment
from safesink.mcp_server import SafeSinkMcpServer
from safesink.mcp_verifier import verify_manifest

# A stateless server: no initialize, every request carries _meta, and
# server/discover is how it introduces itself.
MODERN_SERVER = '''
import json, sys

TOOLS = [
    {
        "name": "read_note",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    }
]


def respond(request_id, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}) + "\\n")
    sys.stdout.flush()


def error(request_id, code, message, data=None):
    payload = {"code": code, "message": message}
    if data is not None:
        payload["data"] = data
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "error": payload}) + "\\n")
    sys.stdout.flush()


for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request.get("method")
    params = request.get("params") or {}
    version = (params.get("_meta") or {}).get("io.modelcontextprotocol/protocolVersion")
    if version != "2026-07-28":
        error(request["id"], -32022, "Unsupported protocol version",
              {"supported": ["2026-07-28"], "requested": version})
        continue
    if method == "server/discover":
        respond(request["id"], {
            "resultType": "complete",
            "supportedVersions": ["2026-07-28"],
            "capabilities": {"tools": {}},
            "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "modern-fixture", "version": "9.0.0"}},
            "ttlMs": 1000,
            "cacheScope": "public",
        })
    elif method == "tools/list":
        respond(request["id"], {"resultType": "complete", "tools": TOOLS, "ttlMs": 1000, "cacheScope": "public"})
    elif method == "tools/call":
        respond(request["id"], {
            "resultType": "complete",
            "content": [{"type": "text", "text": "ok"}],
            "structuredContent": {"ok": True},
            "isError": False,
        })
    else:
        error(request["id"], -32601, "unknown method")
'''

# Answers initialize but ignores anything it does not know, like older SDK-built
# servers that silently drop an unfamiliar method.
LEGACY_SILENT_SERVER = '''
import json, sys

for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    if request.get("method") == "initialize":
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "legacy-fixture", "version": "1.0.0"},
        }}) + "\\n")
        sys.stdout.flush()
'''


class ModernProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def start(self, source: str) -> McpStdioClient:
        script = self.root / f"server-{abs(hash(source))}.py"
        script.write_text(source, encoding="utf-8")
        client = McpStdioClient(
            [__import__("sys").executable, str(script)],
            cwd=self.root,
            environment=restricted_environment([], {}),
            startup_timeout_seconds=10,
            probe_timeout_seconds=1.0,
        )
        client.start()
        self.addCleanup(client.close)
        return client

    def test_a_stateless_server_is_detected_without_initialize(self) -> None:
        client = self.start(MODERN_SERVER)
        self.assertEqual(client.era, "modern")
        self.assertEqual(client.negotiated_protocol_version, "2026-07-28")
        self.assertEqual(client.server_info["name"], "modern-fixture")
        tools = client.list_tools()
        self.assertEqual([tool["name"] for tool in tools], ["read_note"])

    def test_every_modern_request_carries_the_version_metadata(self) -> None:
        client = self.start(MODERN_SERVER)
        result = client.call_tool("read_note", {})
        self.assertEqual(result["structuredContent"], {"ok": True})

    def test_a_silent_legacy_server_falls_back_to_the_handshake(self) -> None:
        client = self.start(LEGACY_SILENT_SERVER)
        self.assertEqual(client.era, "legacy")
        self.assertEqual(client.negotiated_protocol_version, "2025-11-25")

    def test_verification_runs_against_a_modern_server(self) -> None:
        script = self.root / "modern.py"
        script.write_text(MODERN_SERVER, encoding="utf-8")
        (self.root / "sandbox").mkdir()
        manifest_path = self.root / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "schemaVersion": "effectfence.mcp.v1",
                    "server": {
                        "transport": "stdio",
                        "command": [__import__("sys").executable, str(script)],
                    },
                    "observers": [
                        {
                            "id": "sandbox",
                            "kind": "filesystem",
                            "root": "./sandbox",
                            "sensitive": False,
                        }
                    ],
                    "cases": [{"id": "read", "tool": "read_note", "arguments": {}}],
                }
            ),
            encoding="utf-8",
        )
        report = verify_manifest(manifest_path)
        self.assertEqual(report["verdict"], "pass")
        self.assertEqual(report["server"]["negotiated_protocol_version"], "2026-07-28")


class ModernServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.server = SafeSinkMcpServer(root=self.root)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def modern(self, method: str, params: dict | None = None) -> dict:
        merged = dict(params or {})
        merged["_meta"] = {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}
        response = self.server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": merged}
        )
        assert response is not None
        return response

    def test_discover_advertises_versions_and_identity(self) -> None:
        result = self.modern("server/discover")["result"]
        self.assertEqual(result["supportedVersions"][0], "2026-07-28")
        self.assertIn("2025-11-25", result["supportedVersions"])
        self.assertEqual(
            result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"], "safesink"
        )

    def test_modern_results_are_typed_and_cacheable(self) -> None:
        result = self.modern("tools/list")["result"]
        self.assertEqual(result["resultType"], "complete")
        self.assertIn("ttlMs", result)
        self.assertEqual(result["cacheScope"], "public")
        self.assertTrue(all("outputSchema" in tool for tool in result["tools"]))

    def test_a_tool_call_returns_structured_content(self) -> None:
        manifest = self.root / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "schemaVersion": "effectfence.mcp.v1",
                    "server": {"transport": "stdio", "command": ["true"]},
                    "observers": [
                        {"id": "s", "kind": "filesystem", "root": ".", "sensitive": False}
                    ],
                    "cases": [{"id": "x", "tool": "read_note", "arguments": {}}],
                }
            ),
            encoding="utf-8",
        )
        result = self.modern(
            "tools/call", {"name": "lint_manifest", "arguments": {"path": "manifest.json"}}
        )["result"]
        self.assertEqual(result["resultType"], "complete")
        self.assertIn("score", result["structuredContent"])

    def test_an_unknown_version_gets_the_error_with_supported_versions(self) -> None:
        response = self.server.handle(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/list",
                "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "1900-01-01"}},
            }
        )
        assert response is not None
        self.assertEqual(response["error"]["code"], -32022)
        self.assertIn("2026-07-28", response["error"]["data"]["supported"])

    def test_a_legacy_client_still_gets_the_old_shapes(self) -> None:
        init = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert init is not None
        self.assertEqual(init["result"]["protocolVersion"], "2025-11-25")
        listing = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert listing is not None
        self.assertNotIn("resultType", listing["result"])


if __name__ == "__main__":
    unittest.main()
