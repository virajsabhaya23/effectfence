from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from safesink.mcp_server import SafeSinkMcpServer, serve

GOOD_MANIFEST = {
    "schemaVersion": "effectfence.mcp.v1",
    "server": {"transport": "stdio", "command": ["python", "-c", "pass"]},
    "observers": [
        {"id": "sandbox", "kind": "filesystem", "root": "./sandbox", "sensitive": False}
    ],
    "cases": [
        {
            "id": "read-note",
            "tool": "read_note",
            "arguments": {"path": "note.txt"},
            "contract": {"readOnlyHint": True},
        }
    ],
}


class McpServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text(json.dumps(GOOD_MANIFEST), encoding="utf-8")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def call(self, server: SafeSinkMcpServer, name: str, **arguments: object) -> dict:
        response = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        assert response is not None
        return response["result"]

    def test_initialize_reports_capabilities(self) -> None:
        server = SafeSinkMcpServer(root=self.root)
        response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert response is not None
        self.assertEqual(response["result"]["serverInfo"]["name"], "safesink")
        self.assertIn("tools", response["result"]["capabilities"])

    def test_notifications_are_not_answered(self) -> None:
        server = SafeSinkMcpServer(root=self.root)
        self.assertIsNone(server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_verification_is_not_advertised_or_callable_by_default(self) -> None:
        server = SafeSinkMcpServer(root=self.root)
        listing = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert listing is not None
        names = [tool["name"] for tool in listing["result"]["tools"]]
        self.assertNotIn("verify_manifest", names)

        result = self.call(server, "verify_manifest", path="manifest.json")
        self.assertTrue(result["isError"])
        self.assertIn("--allow-exec", result["content"][0]["text"])

    def test_the_allow_exec_flag_advertises_verification(self) -> None:
        server = SafeSinkMcpServer(root=self.root, allow_exec=True)
        listing = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        assert listing is not None
        names = [tool["name"] for tool in listing["result"]["tools"]]
        self.assertIn("verify_manifest", names)

    def test_lint_reads_a_manifest_without_executing_it(self) -> None:
        server = SafeSinkMcpServer(root=self.root)
        result = self.call(server, "lint_manifest", path="manifest.json")
        self.assertFalse(result["isError"])
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload["score"]["value"], payload["score"]["max"])

    def test_lint_reports_missing_annotations(self) -> None:
        unannotated = json.loads(json.dumps(GOOD_MANIFEST))
        unannotated["cases"][0]["contract"] = {}
        path = self.root / "unannotated.json"
        path.write_text(json.dumps(unannotated), encoding="utf-8")
        server = SafeSinkMcpServer(root=self.root)
        payload = json.loads(self.call(server, "lint_manifest", path="unannotated.json")["content"][0]["text"])
        self.assertTrue(payload["findings"])

    def test_paths_cannot_escape_the_root(self) -> None:
        server = SafeSinkMcpServer(root=self.root)
        result = self.call(server, "lint_manifest", path="../../etc/passwd")
        self.assertTrue(result["isError"])
        self.assertIn("inside the server root", result["content"][0]["text"])

    def test_an_invalid_manifest_is_a_tool_error(self) -> None:
        bad = self.root / "bad.json"
        bad.write_text("{", encoding="utf-8")
        server = SafeSinkMcpServer(root=self.root)
        result = self.call(server, "lint_manifest", path="bad.json")
        self.assertTrue(result["isError"])

    def test_rules_are_described(self) -> None:
        server = SafeSinkMcpServer(root=self.root)
        payload = json.loads(self.call(server, "describe_rules")["content"][0]["text"])
        self.assertTrue(any(rule["code"] == "MISSING_READONLY" for rule in payload["rules"]))

    def test_the_transport_answers_a_batch_of_lines(self) -> None:
        server = SafeSinkMcpServer(root=self.root)
        stdin = io.StringIO(
            "\n".join(
                [
                    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
                    "not json",
                    json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
                    "",
                ]
            )
        )
        stdout = io.StringIO()
        serve(stdin, stdout, server=server)
        lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0]["id"], 1)
        self.assertEqual(lines[1]["error"]["code"], -32700)
        self.assertEqual(lines[2]["id"], 2)


if __name__ == "__main__":
    unittest.main()
