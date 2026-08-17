from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path

from effectfence.citation import citation
from effectfence.mcp_client import restricted_environment
from effectfence.mcp_reports import (
    write_junit_report,
    write_sarif_report,
)
from effectfence.mcp_verifier import ManifestError, load_manifest, verify_manifest
from effectfence.observers import (
    FilesystemObserver,
    HttpJsonObserver,
    ObserverError,
    SqliteQueryObserver,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_SERVER = ROOT / "examples/mcp-conformance/fixture_server.py"
RESET_FIXTURE = ROOT / "examples/mcp-conformance/reset_fixture.py"


class McpConformanceTests(unittest.TestCase):
    def manifest(self, root: Path, cases: list[dict]) -> Path:
        state = root / "state"
        setup = {"command": [sys.executable, str(RESET_FIXTURE), str(state)]}
        normalized = [{**case, "setup": setup} for case in cases]
        manifest = {
            "schemaVersion": "effectfence.mcp.v1",
            "server": {
                "transport": "stdio",
                "command": [sys.executable, str(FIXTURE_SERVER)],
                "cwd": str(root),
                "env": {"EFFECTFENCE_FIXTURE_ROOT": str(state)},
            },
            "observers": [
                {
                    "id": "state",
                    "kind": "filesystem",
                    "root": str(state),
                    "sensitive": False,
                }
            ],
            "cases": normalized,
            "policy": {"minimumToolCoverage": len(cases) / 5},
        }
        path = root / "manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def test_passing_server_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.manifest(
                Path(directory),
                [
                    {"id": "read", "tool": "read_note", "arguments": {}},
                    {
                        "id": "upsert",
                        "tool": "upsert_record",
                        "arguments": {"id": "7", "value": "TOP-SECRET-CUSTOMER"},
                    },
                ],
            )
            report = verify_manifest(path)
            self.assertEqual(report["verdict"], "pass")
            self.assertTrue(all(case["passed"] for case in report["cases"]))
            self.assertNotIn("TOP-SECRET-CUSTOMER", json.dumps(report))
            self.assertEqual(len(report["certificateSha256"]), 64)

    def test_detects_three_false_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.manifest(
                Path(directory),
                [
                    {"id": "read-lie", "tool": "lying_read"},
                    {
                        "id": "retry-lie",
                        "tool": "append_audit",
                        "contract": {"idempotentHint": True},
                    },
                    {"id": "delete-lie", "tool": "delete_note"},
                ],
            )
            report = verify_manifest(path)
            codes = {
                violation["code"]
                for case in report["cases"]
                for violation in case["violations"]
            }
            self.assertEqual(report["verdict"], "fail")
            self.assertIn("READ_ONLY_HINT_MISMATCH", codes)
            self.assertIn("IDEMPOTENT_HINT_MISMATCH", codes)
            self.assertIn("NON_DESTRUCTIVE_HINT_MISMATCH", codes)

    def test_reports_are_valid_xml_and_sarif(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = verify_manifest(
                self.manifest(root, [{"id": "read", "tool": "read_note"}])
            )
            junit_path = root / "junit.xml"
            sarif_path = root / "report.sarif"
            write_junit_report(report, junit_path)
            write_sarif_report(report, sarif_path)
            self.assertEqual(ET.parse(junit_path).getroot().tag, "testsuite")
            self.assertEqual(json.loads(sarif_path.read_text())["version"], "2.1.0")

    def test_ambiguous_result_accepts_idempotent_upsert_across_twenty_trials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.manifest(
                Path(directory),
                [{
                    "id": "ambiguous-upsert",
                    "tool": "upsert_record",
                    "arguments": {"id": "7", "value": "safe"},
                    "ambiguousResultFault": {
                        "mode": "drop-result-after-response",
                        "trials": 20,
                    },
                }],
            )
            case = verify_manifest(path)["cases"][0]
            self.assertTrue(case["passed"])
            self.assertEqual(
                case["ambiguousResult"]["classifications"],
                {"committed-result-lost": 20},
            )

    def test_ambiguous_result_rejects_duplicate_on_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.manifest(
                Path(directory),
                [{
                    "id": "ambiguous-append",
                    "tool": "append_audit",
                    "ambiguousResultFault": {
                        "mode": "drop-result-after-response",
                        "trials": 3,
                    },
                }],
            )
            case = verify_manifest(path)["cases"][0]
            self.assertFalse(case["passed"])
            self.assertEqual(
                case["ambiguousResult"]["classifications"],
                {"duplicate-on-retry": 3},
            )
            self.assertIn(
                "AMBIGUOUS_RESULT_DUPLICATE_ON_RETRY",
                {item["code"] for item in case["violations"]},
            )

    def test_ambiguous_result_unknown_is_inconclusive_not_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.manifest(
                Path(directory),
                [{
                    "id": "ambiguous-read",
                    "tool": "read_note",
                    "ambiguousResultFault": {
                        "mode": "drop-result-after-response",
                        "trials": 2,
                    },
                }],
            )
            case = verify_manifest(path)["cases"][0]
            self.assertFalse(case["passed"])
            self.assertEqual(
                case["ambiguousResult"]["classifications"],
                {"ambiguous-unknown": 2},
            )
            self.assertIn(
                "AMBIGUOUS_RESULT_UNCLASSIFIED",
                {item["code"] for item in case["inconclusive"]},
            )

    def test_timeout_before_send_is_distinct_from_committed_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.manifest(
                Path(directory),
                [{
                    "id": "pre-send-timeout",
                    "tool": "upsert_record",
                    "arguments": {"id": "7", "value": "safe"},
                    "ambiguousResultFault": {
                        "mode": "timeout-before-send",
                        "trials": 2,
                        "timeoutMs": 25,
                    },
                }],
            )
            case = verify_manifest(path)["cases"][0]
            self.assertTrue(case["passed"])
            self.assertEqual(
                case["ambiguousResult"]["classifications"],
                {"no-effect-timeout": 2},
            )

    def test_paginated_tool_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.manifest(Path(directory), [{"id": "read", "tool": "read_note"}])
            value = json.loads(path.read_text())
            value["server"]["env"]["EFFECTFENCE_FIXTURE_PAGINATE"] = "1"
            path.write_text(json.dumps(value))
            report = verify_manifest(path)
            self.assertEqual(report["coverage"]["listedTools"], 5)
            self.assertEqual(report["verdict"], "pass")

    def test_manifest_rejects_unknown_observer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.manifest(Path(directory), [{"id": "read", "tool": "read_note"}])
            value = json.loads(path.read_text())
            value["cases"][0]["observerIds"] = ["missing"]
            path.write_text(json.dumps(value))
            with self.assertRaises(ManifestError):
                load_manifest(path)

    def test_restricted_environment_does_not_copy_credentials(self) -> None:
        environment = restricted_environment(
            ["REQUESTED"], {"EXPLICIT": "yes"}, base={"SECRET": "no", "REQUESTED": "ok"}
        )
        self.assertNotIn("SECRET", environment)
        self.assertEqual(environment["REQUESTED"], "ok")
        self.assertEqual(environment["EXPLICIT"], "yes")

    def test_filesystem_observer_does_not_follow_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside.txt"
            outside.write_text("secret")
            watched = root / "watched"
            watched.mkdir()
            (watched / "link").symlink_to(outside)
            snapshot = FilesystemObserver("fs", watched).snapshot()
            self.assertEqual(snapshot.state["link"]["type"], "symlink")
            self.assertNotIn("secret", json.dumps(snapshot.state))

    def test_sqlite_observer_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.db"
            connection = sqlite3.connect(database)
            connection.execute("create table events(id integer primary key)")
            connection.execute("insert into events values (1)")
            connection.commit()
            connection.close()
            observer = SqliteQueryObserver("db", database, "select id from events")
            self.assertEqual(observer.snapshot().state["rows"], [[1]])
            mutating = SqliteQueryObserver("db", database, "delete from events")
            with self.assertRaises(ObserverError):
                mutating.snapshot()

    def test_http_json_observer_is_allowlisted_and_bounded(self) -> None:
        state = {"count": 1}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                body = json.dumps(state).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            observer = HttpJsonObserver(
                "api", f"http://127.0.0.1:{server.server_port}/state"
            )
            before = observer.snapshot()
            state["count"] = 2
            after = observer.snapshot()
            self.assertTrue(observer.diff(before, after).changed)
            with self.assertRaises(ObserverError):
                HttpJsonObserver("bad", "https://example.com/state")
        finally:
            server.shutdown()
            server.server_close()

    def test_sensitive_observer_digests_are_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.manifest(
                Path(directory),
                [{"id": "upsert", "tool": "upsert_record", "arguments": {"id": "1", "value": "x"}}],
            )
            value = json.loads(path.read_text())
            value["observers"][0]["sensitive"] = True
            path.write_text(json.dumps(value))
            report = verify_manifest(path)
            delta = report["cases"][0]["deltas"]["firstCall"][0]
            self.assertTrue(delta["digestsRedacted"])
            self.assertNotIn("beforeSha256", delta)
            self.assertNotIn("paths", delta)

    def test_manifest_schema_is_packaged(self) -> None:
        schema = json.loads(
            files("effectfence.schemas")
            .joinpath("mcp-manifest-v1.schema.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(schema["properties"]["schemaVersion"]["const"], "effectfence.mcp.v1")

    def test_citations_are_copy_ready(self) -> None:
        self.assertIn("@software", citation("bibtex"))
        self.assertIn("cff-version: 1.2.0", citation("cff"))
        self.assertEqual(json.loads(citation("json"))["type"], "software")


if __name__ == "__main__":
    unittest.main()
