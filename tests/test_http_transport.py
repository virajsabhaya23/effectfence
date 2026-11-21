from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from safesink.cli import main as cli_main
from safesink.http_transport import _decode_event_stream
from safesink.mcp_client import McpClientError
from safesink.mcp_scan import list_server_tools

TOOLS = [
    {
        "name": "write_note",
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "File to write"}},
            "required": ["path"],
        },
    }
]


class _Handler(BaseHTTPRequestHandler):
    require_token = True
    sse = False

    def log_message(self, *_: object) -> None:  # keep test output clean
        return

    def _send(self, payload: dict, *, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header(
            "Content-Type", "text/event-stream" if self.sse else "application/json"
        )
        self.send_header("Mcp-Session-Id", "session-1")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        if self.require_token and self.headers.get("X-Token") != "expected-token":
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length) or b"{}")
        if "id" not in request:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        method = request.get("method")
        if method == "initialize":
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "http-fixture", "version": "1.0.0"},
                    },
                }
            )
        elif method == "tools/list":
            self._send({"jsonrpc": "2.0", "id": request["id"], "result": {"tools": TOOLS}})
        elif method == "tools/call":
            arguments = request["params"].get("arguments", {})
            path = arguments.get("path")
            if isinstance(path, str):
                Path(path).write_text("written over http\n", encoding="utf-8")
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {"content": [{"type": "text", "text": "ok"}], "isError": False},
                }
            )
        else:
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "error": {"code": -32601, "message": "unknown method"},
                }
            )


class HttpTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/mcp"
        os.environ["HTTP_MCP_TOKEN"] = "expected-token"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        os.environ.pop("HTTP_MCP_TOKEN", None)

    def test_tools_can_be_listed_over_streamable_http(self) -> None:
        tools, info = list_server_tools(
            [],
            cwd=Path.cwd(),
            url=self.url,
            headers_from_env={"X-Token": "HTTP_MCP_TOKEN"},
        )
        self.assertEqual([tool["name"] for tool in tools], ["write_note"])
        self.assertEqual(info["name"], "http-fixture")

    def test_a_missing_token_is_reported_as_401(self) -> None:
        os.environ.pop("HTTP_MCP_TOKEN", None)
        with self.assertRaises(McpClientError):
            list_server_tools(
                [],
                cwd=Path.cwd(),
                url=self.url,
                headers_from_env={"X-Token": "HTTP_MCP_TOKEN"},
            )

    def test_a_host_outside_the_allowlist_is_refused(self) -> None:
        with self.assertRaises(McpClientError):
            list_server_tools([], cwd=Path.cwd(), url=self.url, allowed_hosts=("example.test",))

    def test_mcp_check_verifies_a_server_over_http_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_path = root / "report.json"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                exit_code = cli_main(
                    [
                        "mcp-check",
                        "--url",
                        self.url,
                        "--allow-host",
                        "127.0.0.1",
                        "--header-env",
                        "X-Token=HTTP_MCP_TOKEN",
                        "--sandbox",
                        str(root / "sandbox"),
                        "--out",
                        str(report_path),
                    ]
                )
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 2)
            self.assertEqual(report["verdict"], "fail")
            self.assertIn(
                "READ_ONLY_HINT_MISMATCH",
                [
                    violation["code"]
                    for case in report["cases"]
                    for violation in case["violations"]
                ],
            )

    def test_event_stream_frames_are_parsed(self) -> None:
        body = (
            b'event: message\ndata: {"jsonrpc":"2.0","id":2,"result":{"tools":[]}}\n\n'
            b'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
        )
        messages = _decode_event_stream(body)
        self.assertEqual(messages[0]["id"], 2)
        self.assertEqual(messages[1]["method"], "notifications/progress")


if __name__ == "__main__":
    unittest.main()
