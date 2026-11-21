from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from safesink.cli import main as cli_main
from safesink.host_config import (
    HostConfigError,
    load_host_config,
)

# One read-only tool, honest about it: audit should pass it.
HONEST_SERVER = '''
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


for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request.get("method")
    if method == "initialize":
        respond(request["id"], {
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "honest-fixture", "version": "1.0.0"},
        })
    elif method == "tools/list":
        respond(request["id"], {"tools": TOOLS})
    elif method == "tools/call":
        respond(request["id"], {
            "content": [{"type": "text", "text": "note"}],
            "isError": False,
        })
'''

BROKEN_SERVER = "import sys\nsys.exit(3)\n"


class HostConfigTests(unittest.TestCase):
    def write(self, root: Path, document: object) -> Path:
        path = root / "config.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_claude_desktop_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                Path(directory),
                {
                    "mcpServers": {
                        "filesystem": {
                            "command": "npx",
                            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                            "env": {"TOKEN": "x"},
                        }
                    }
                },
            )
            servers = load_host_config(path)
        self.assertEqual(list(servers), ["filesystem"])
        self.assertEqual(servers["filesystem"].command[0], "npx")
        self.assertEqual(servers["filesystem"].kind, "stdio")
        self.assertEqual(servers["filesystem"].env, {"TOKEN": "x"})

    def test_vscode_nested_shape_and_http_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                Path(directory),
                {
                    "mcp": {
                        "servers": {
                            "remote": {"type": "http", "url": "https://example.test/mcp"},
                        }
                    }
                },
            )
            servers = load_host_config(path)
        self.assertEqual(servers["remote"].url, "https://example.test/mcp")
        self.assertEqual(servers["remote"].kind, "streamable-http")
        self.assertEqual(servers["remote"].command, [])

    def test_entries_without_a_command_or_url_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                Path(directory),
                {"servers": {"nothing": {"type": "sse"}, "ok": {"command": "node"}}},
            )
            servers = load_host_config(path)
        self.assertEqual(list(servers), ["ok"])

    def test_a_missing_server_map_is_a_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(Path(directory), {"unrelated": True})
            with self.assertRaises(HostConfigError) as raised:
                load_host_config(path)
        self.assertIn("mcpServers", str(raised.exception))

    def test_invalid_json_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text("{", encoding="utf-8")
            with self.assertRaises(HostConfigError):
                load_host_config(path)


class AuditCommandTests(unittest.TestCase):
    def run_audit(self, root: Path, document: object) -> tuple[int, dict | str, str]:
        config = root / "config.json"
        config.write_text(json.dumps(document), encoding="utf-8")
        out = io.StringIO()
        errors = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(errors):
            exit_code = cli_main(
                [
                    "audit",
                    "--config",
                    str(config),
                    "--out-dir",
                    str(root / "audit"),
                ]
            )
        text = out.getvalue()
        try:
            payload: dict | str = json.loads(text)
        except json.JSONDecodeError:
            payload = text
        return exit_code, payload, errors.getvalue()

    def test_an_honest_configured_server_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "server.py"
            script.write_text(HONEST_SERVER, encoding="utf-8")
            exit_code, payload, errors = self.run_audit(
                root,
                {
                    "mcpServers": {
                        "fixture": {
                            "command": sys.executable,
                            "args": [str(script)],
                        }
                    }
                },
            )
            self.assertEqual(errors, "")
            self.assertIsInstance(payload, dict)
            assert isinstance(payload, dict)
            self.assertEqual(exit_code, 0)
            row = payload["servers"][0]
            self.assertEqual(row["verdict"], "pass")
            self.assertEqual(row["tools"], 1)
            self.assertTrue(Path(row["report"]).is_file())
            self.assertTrue((root / "audit" / "fixture" / "manifest.json").is_file())

    def test_a_broken_server_is_reported_without_hiding_the_others(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            good = root / "good.py"
            good.write_text(HONEST_SERVER, encoding="utf-8")
            bad = root / "bad.py"
            bad.write_text(BROKEN_SERVER, encoding="utf-8")
            exit_code, payload, _ = self.run_audit(
                root,
                {
                    "mcpServers": {
                        "good": {"command": sys.executable, "args": [str(good)]},
                        "bad": {"command": sys.executable, "args": [str(bad)]},
                    }
                },
            )
            self.assertEqual(exit_code, 1)
            assert isinstance(payload, dict)
            verdicts = {row["server"]: row["verdict"] for row in payload["servers"]}
            self.assertEqual(verdicts["good"], "pass")
            self.assertEqual(verdicts["bad"], "error")
            self.assertEqual(payload["needsAttention"], 1)

    def test_an_unreadable_discovered_config_does_not_hide_a_good_one(self) -> None:
        from safesink import cli as cli_module

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "server.py"
            script.write_text(HONEST_SERVER, encoding="utf-8")
            good = root / "good.json"
            good.write_text(
                json.dumps(
                    {"mcpServers": {"ok": {"command": sys.executable, "args": [str(script)]}}}
                ),
                encoding="utf-8",
            )
            other = root / "other.json"
            other.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
            stdout = io.StringIO()
            original = cli_module.existing_config_paths
            cli_module.existing_config_paths = lambda: [other, good]
            try:
                with contextlib.redirect_stdout(stdout):
                    exit_code = cli_main(["audit", "--out-dir", str(root / "audit")])
            finally:
                cli_module.existing_config_paths = original
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual([row["server"] for row in payload["servers"]], ["ok"])
            self.assertEqual(len(payload["skippedConfigs"]), 1)
            self.assertIn("mcpServers", payload["skippedConfigs"][0]["reason"])

    def test_an_explicitly_named_bad_config_fails_loudly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad = root / "bad.json"
            bad.write_text(json.dumps({"unrelated": True}), encoding="utf-8")
            errors = io.StringIO()
            with self.assertRaises(SystemExit) as raised:
                with contextlib.redirect_stderr(errors):
                    cli_main(["audit", "--config", str(bad)])
            self.assertEqual(raised.exception.code, 2)
            self.assertIn("mcpServers", errors.getvalue())

    def test_server_filter_and_markdown_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "server.py"
            script.write_text(HONEST_SERVER, encoding="utf-8")
            config = root / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "one": {"command": sys.executable, "args": [str(script)]},
                            "two": {"command": sys.executable, "args": [str(script)]},
                        }
                    }
                ),
                encoding="utf-8",
            )
            summary = root / "summary.md"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "audit",
                        "--config",
                        str(config),
                        "--server",
                        "one",
                        "--out-dir",
                        str(root / "audit"),
                        "--markdown",
                        str(summary),
                    ]
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual([row["server"] for row in payload["servers"]], ["one"])
            text = summary.read_text(encoding="utf-8")
            self.assertIn("| `one` |", text)
            self.assertIn("not that it passed", text)


if __name__ == "__main__":
    unittest.main()
