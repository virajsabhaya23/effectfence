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

from safesink.cli import main as cli_main
import contextlib
import io
from safesink.mcp_client import restricted_environment
from safesink.mcp_reports import (
    write_junit_report,
    write_sarif_report,
)
from safesink.mcp_verifier import (
    ManifestError,
    load_manifest,
    load_suppressions,
    verify_manifest,
)
from safesink.observers import (
    FilesystemObserver,
    HttpJsonObserver,
    ObserverError,
    SqliteQueryObserver,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_SERVER = ROOT / "examples/mcp-conformance/fixture_server.py"
RESET_FIXTURE = ROOT / "examples/mcp-conformance/reset_fixture.py"

# Answers quickly, but every tools/call sleeps past a small run budget.
SLOW_SERVER = '''
import json, sys, time

TOOLS = [
    {
        "name": "slow_note",
        "annotations": {"readOnlyHint": True},
        "inputSchema": {"type": "object", "properties": {}},
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
            "serverInfo": {"name": "slow-fixture", "version": "1.0.0"},
        })
    elif method == "tools/list":
        respond(request["id"], {"tools": TOOLS})
    elif method == "tools/call":
        time.sleep(1.5)
        respond(request["id"], {
            "content": [{"type": "text", "text": "slow"}],
            "isError": False,
        })
'''

# Records its own pid on initialize, so a restart is observable as a new process.
RESTART_SERVER = '''
import json, os, sys

LOG = os.environ["RESTART_LOG"]
TOOLS = [
    {
        "name": "ping",
        "annotations": {"readOnlyHint": True},
        "inputSchema": {"type": "object", "properties": {}},
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
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(str(os.getpid()) + "\\n")
        respond(request["id"], {
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "restart-fixture", "version": "1.0.0"},
        })
    elif method == "tools/list":
        respond(request["id"], {"tools": TOOLS})
    elif method == "tools/call":
        respond(request["id"], {"content": [{"type": "text", "text": "pong"}], "isError": False})
'''

# Exits before MCP initialization, with a diagnostic and a secret on stderr.
DEAD_SERVER = '''
import sys
sys.stderr.write("fatal: missing --db path\\n")
sys.stderr.write("api_key=SUPERSECRET\\n")
sys.exit(3)
'''


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
            "policy": {"minimumToolCoverage": min(1.0, len(cases) / 7)},
        }
        path = root / "manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def dead_manifest(self, root: Path, server: Path) -> Path:
        state = root / "state"
        state.mkdir(exist_ok=True)
        manifest = {
            "schemaVersion": "effectfence.mcp.v1",
            "server": {
                "transport": "stdio",
                "command": [sys.executable, str(server)],
                "cwd": str(root),
                "startupTimeoutSeconds": 2,
                "requestTimeoutSeconds": 2,
            },
            "observers": [
                {
                    "id": "state",
                    "kind": "filesystem",
                    "root": str(state),
                    "sensitive": False,
                }
            ],
            "cases": [{"id": "read", "tool": "read_note", "arguments": {}}],
        }
        path = root / "dead-manifest.json"
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

    def test_rejected_generated_arguments_are_inconclusive_not_findings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = verify_manifest(
                self.manifest(
                    Path(directory),
                    [
                        {
                            "id": "generated",
                            "tool": "read_missing_file",
                            "arguments": {"path": "nope.txt"},
                            "generatedArguments": True,
                        }
                    ],
                )
            )
            case = report["cases"][0]
        self.assertEqual(case["violations"], [])
        self.assertIn(
            "TOOL_ERROR_ON_GENERATED_ARGUMENTS",
            [item["code"] for item in case["inconclusive"]],
        )
        self.assertFalse(case["passed"])
        self.assertEqual(report["verdict"], "inconclusive")

    def test_rejected_hand_written_arguments_are_still_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = verify_manifest(
                self.manifest(
                    Path(directory),
                    [
                        {
                            "id": "hand-written",
                            "tool": "read_missing_file",
                            "arguments": {"path": "nope.txt"},
                        }
                    ],
                )
            )
            case = report["cases"][0]
        self.assertEqual(
            [item["code"] for item in case["violations"]],
            ["TOOL_ERROR_EXPECTATION_MISMATCH"],
        )
        self.assertEqual(report["verdict"], "fail")

    def test_unannotated_tools_are_inconclusive_and_not_called(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = verify_manifest(
                self.manifest(
                    Path(directory), [{"id": "plain", "tool": "unannotated_note"}]
                )
            )
            case = report["cases"][0]
        self.assertEqual(case["calls"], [])
        self.assertEqual(
            [item["code"] for item in case["inconclusive"]],
            ["NO_DECLARED_ANNOTATIONS"],
        )
        self.assertFalse(case["passed"])
        self.assertEqual(report["verdict"], "inconclusive")

    def test_contract_still_verifies_an_unannotated_tool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = verify_manifest(
                self.manifest(
                    Path(directory),
                    [
                        {
                            "id": "plain",
                            "tool": "unannotated_note",
                            "contract": {"readOnlyHint": True},
                        }
                    ],
                )
            )
            case = report["cases"][0]
        self.assertEqual(case["inconclusive"], [])
        self.assertTrue(case["passed"])

    def test_shipped_examples_behave_as_documented(self) -> None:
        examples = ROOT / "examples" / "mcp-conformance"
        passing = verify_manifest(examples / "passing.json")
        self.assertEqual(passing["verdict"], "pass")
        failing = verify_manifest(examples / "failing.json")
        self.assertEqual(failing["verdict"], "fail")
        codes = {
            violation["code"]
            for case in failing["cases"]
            for violation in case["violations"]
        }
        self.assertEqual(
            codes,
            {
                "READ_ONLY_HINT_MISMATCH",
                "IDEMPOTENT_HINT_MISMATCH",
                "NON_DESTRUCTIVE_HINT_MISMATCH",
            },
        )

    def test_shipped_examples_have_documented_scores(self) -> None:
        examples = ROOT / "examples" / "mcp-conformance"
        passing = verify_manifest(examples / "passing.json")
        self.assertEqual(passing["score"]["value"], 100)
        self.assertEqual(passing["score"]["version"], "1")
        self.assertIn("formula", passing["score"])
        failing = verify_manifest(examples / "failing.json")
        # 25 + 25 + 15 for the three dishonest tools, and no other deduction.
        self.assertEqual(failing["score"]["value"], 35)
        self.assertEqual(
            failing["score"]["inputs"]["violations"],
            {
                "READ_ONLY_HINT_MISMATCH": 1,
                "IDEMPOTENT_HINT_MISMATCH": 1,
                "NON_DESTRUCTIVE_HINT_MISMATCH": 1,
            },
        )

    def inconclusive_manifest(self, root: Path) -> Path:
        path = self.manifest(
            root,
            [
                {"id": f"plain-{index}", "tool": "unannotated_note"}
                for index in range(3)
            ],
        )
        value = json.loads(path.read_text())
        value["policy"]["minimumToolCoverage"] = 0
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_a_fully_inconclusive_run_scores_well_below_a_clean_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.inconclusive_manifest(Path(directory))
            report = verify_manifest(path)
            self.assertEqual(report["verdict"], "inconclusive")
            self.assertEqual(report["score"]["value"], 70)
            self.assertEqual(report["score"]["inputs"]["inconclusiveCases"], 3)

    def test_min_score_gate_turns_an_unverifiable_run_into_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.inconclusive_manifest(root)
            report_path = root / "report.json"
            # Without the gate this run exits 3 (inconclusive).
            self.assertEqual(
                cli_main(["mcp-verify", str(path), "--out", str(report_path)]), 3
            )
            exit_code = cli_main(
                [
                    "mcp-verify",
                    str(path),
                    "--out",
                    str(report_path),
                    "--min-score",
                    "95",
                ]
            )
            self.assertEqual(exit_code, 2)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertLess(report["score"]["value"], 95)

    def test_baseline_suppresses_exactly_the_known_findings(self) -> None:
        path = ROOT / "examples" / "mcp-conformance" / "failing.json"
        baseline = verify_manifest(path)
        report = verify_manifest(
            path, baseline=baseline, baseline_path=path, new_only=True
        )
        self.assertEqual(report["baseline"]["suppressedCount"], 3)
        self.assertEqual(report["baseline"]["regressions"], [])
        self.assertEqual(len(report["baseline"]["sha256"]), 64)
        self.assertTrue(all(not case["violations"] for case in report["cases"]))
        self.assertEqual(report["verdict"], "pass")

    def test_a_new_finding_still_fails_under_new_only(self) -> None:
        path = ROOT / "examples" / "mcp-conformance" / "failing.json"
        baseline = json.loads(json.dumps(verify_manifest(path)))
        for case in baseline["cases"]:
            if case["violations"]:
                case["violations"].pop()
                break
        report = verify_manifest(path, baseline=baseline, new_only=True)
        self.assertEqual(report["verdict"], "fail")
        self.assertEqual(report["baseline"]["suppressedCount"], 2)
        self.assertEqual(len(report["baseline"]["regressions"]), 1)

    def test_a_finding_absent_from_the_new_run_is_reported_fixed(self) -> None:
        path = ROOT / "examples" / "mcp-conformance" / "failing.json"
        baseline = json.loads(json.dumps(verify_manifest(path)))
        baseline["cases"].append(
            {"id": "ghost", "tool": "ghost", "violations": [{"code": "GHOST"}]}
        )
        report = verify_manifest(path, baseline=baseline, new_only=True)
        self.assertEqual(report["baseline"]["fixed"], ["ghost:GHOST"])
        self.assertEqual(report["baseline"]["fixedCount"], 1)

    def test_new_only_without_a_baseline_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.manifest(
                Path(directory),
                [{"id": "read", "tool": "read_note", "arguments": {}}],
            )
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as refused:
                    cli_main(["mcp-verify", str(path), "--new-only"])
            self.assertEqual(refused.exception.code, 2)

    def test_a_reasoned_suppression_is_listed_and_keeps_the_run_green(self) -> None:
        path = ROOT / "examples" / "mcp-conformance" / "failing.json"
        suppressions = {
            "suppressions": [
                {
                    "caseId": "detect-false-read-only-claim",
                    "code": "READ_ONLY_HINT_MISMATCH",
                    "reason": "documented compatibility shim, tracked in JIRA-12",
                    "owner": "platform",
                    "expires": "2999-01-01",
                }
            ]
        }
        report = verify_manifest(path, suppressions=suppressions)
        self.assertEqual(report["suppressions"]["suppressedCount"], 1)
        applied = report["suppressions"]["applied"][0]
        self.assertEqual(applied["reason"], "documented compatibility shim, tracked in JIRA-12")
        self.assertEqual(applied["owner"], "platform")
        codes = [
            violation["code"]
            for case in report["cases"]
            for violation in case["violations"]
        ]
        self.assertNotIn("READ_ONLY_HINT_MISMATCH", codes)
        self.assertEqual(len(codes), 2)

    def test_an_expired_suppression_is_itself_a_finding(self) -> None:
        path = ROOT / "examples" / "mcp-conformance" / "failing.json"
        suppressions = {
            "suppressions": [
                {
                    "tool": "lying_read",
                    "code": "READ_ONLY_HINT_MISMATCH",
                    "reason": "temporary",
                    "expires": "2000-01-01",
                }
            ]
        }
        report = verify_manifest(path, suppressions=suppressions)
        self.assertEqual(report["suppressions"]["suppressedCount"], 0)
        self.assertEqual(len(report["suppressions"]["expired"]), 1)
        codes = [
            violation["code"]
            for case in report["cases"]
            for violation in case["violations"]
        ]
        self.assertIn("SUPPRESSION_EXPIRED", codes)
        self.assertEqual(report["verdict"], "fail")

    def test_an_unmatched_suppression_is_reported_stale(self) -> None:
        path = ROOT / "examples" / "mcp-conformance" / "failing.json"
        suppressions = {
            "suppressions": [
                {
                    "tool": "no_such_tool",
                    "code": "READ_ONLY_HINT_MISMATCH",
                    "reason": "stale entry",
                    "expires": "2999-01-01",
                }
            ]
        }
        report = verify_manifest(path, suppressions=suppressions)
        self.assertEqual(report["suppressions"]["suppressedCount"], 0)
        self.assertEqual(
            report["suppressions"]["unmatched"][0]["tool"], "no_such_tool"
        )
        self.assertEqual(report["verdict"], "fail")

    def test_fail_on_suppressed_forbids_accepting_a_finding(self) -> None:
        path = ROOT / "examples" / "mcp-conformance" / "failing.json"
        suppressions = {
            "suppressions": [
                {
                    "caseId": case_id,
                    "code": code,
                    "reason": "accepted",
                    "expires": "2999-01-01",
                }
                for case_id, code in (
                    ("detect-false-read-only-claim", "READ_ONLY_HINT_MISMATCH"),
                    ("detect-non-idempotent-retry", "IDEMPOTENT_HINT_MISMATCH"),
                    (
                        "detect-false-non-destructive-claim",
                        "NON_DESTRUCTIVE_HINT_MISMATCH",
                    ),
                )
            ]
        }
        allowed = verify_manifest(path, suppressions=suppressions)
        self.assertEqual(allowed["verdict"], "pass")
        forbidden = verify_manifest(
            path, suppressions=suppressions, fail_on_suppressed=True
        )
        self.assertEqual(forbidden["verdict"], "fail")
        self.assertIn(
            "SUPPRESSED_FINDINGS_NOT_ALLOWED",
            [item["code"] for item in forbidden["policyViolations"]],
        )

    def test_suppression_entry_without_a_reason_or_expiry_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "suppressions.json"
            path.write_text(
                json.dumps({"suppressions": [{"tool": "x", "code": "Y"}]}),
                encoding="utf-8",
            )
            with self.assertRaises(ManifestError):
                load_suppressions(path)
            path.write_text(
                json.dumps(
                    {
                        "suppressions": [
                            {
                                "tool": "x",
                                "code": "Y",
                                "reason": "because",
                                "expires": "not-a-date",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ManifestError):
                load_suppressions(path)

    def restart_manifest(self, root: Path, cases: list[dict], server: dict | None = None) -> Path:
        state = root / "state"
        state.mkdir(exist_ok=True)
        value = {
            "schemaVersion": "effectfence.mcp.v1",
            "server": {
                "transport": "stdio",
                "command": [sys.executable, str(root / "restart_server.py")],
                "cwd": str(root),
                "env": {"RESTART_LOG": str(root / "pids.log")},
                **(server or {}),
            },
            "observers": [
                {
                    "id": "state",
                    "kind": "filesystem",
                    "root": str(state),
                    "sensitive": False,
                }
            ],
            "cases": cases,
        }
        path = root / "restart-manifest.json"
        (root / "restart_server.py").write_text(RESTART_SERVER, encoding="utf-8")
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_restart_per_case_starts_a_fresh_process_for_every_case(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.restart_manifest(
                root,
                [{"id": f"ping-{index}", "tool": "ping", "arguments": {}} for index in range(3)],
            )
            shared = verify_manifest(path)
            self.assertEqual(shared["server"]["restarts"], 0)
            self.assertEqual(
                [case["serverInstance"] for case in shared["cases"]], [0, 0, 0]
            )
            self.assertEqual(len((root / "pids.log").read_text().split()), 1)
            (root / "pids.log").write_text("", encoding="utf-8")

            restarted = verify_manifest(path, restart_per_case=True)
            self.assertEqual(restarted["server"]["restarts"], 2)
            self.assertEqual(
                [case["serverInstance"] for case in restarted["cases"]], [0, 1, 2]
            )
            pids = (root / "pids.log").read_text().split()
            self.assertEqual(len(pids), 3)
            self.assertEqual(len(set(pids)), 3)

    def test_case_order_can_be_alphabetical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.restart_manifest(
                Path(directory),
                [
                    {"id": "z-case", "tool": "ping", "arguments": {}},
                    {"id": "a-case", "tool": "ping", "arguments": {}},
                ],
            )
            report = verify_manifest(path, case_order="name")
            self.assertEqual(
                [case["id"] for case in report["cases"]], ["a-case", "z-case"]
            )
            self.assertEqual(report["server"]["caseOrder"], "name")

    def test_unknown_case_order_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.restart_manifest(
                Path(directory),
                [{"id": "ping", "tool": "ping", "arguments": {}}],
                {"caseOrder": "random"},
            )
            with self.assertRaises(ManifestError):
                load_manifest(path)

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
                        "trials": 20,
                    },
                }],
            )
            case = verify_manifest(path)["cases"][0]
            self.assertFalse(case["passed"])
            self.assertEqual(
                case["ambiguousResult"]["classifications"],
                {"duplicate-on-retry": 20},
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
                        "trials": 20,
                    },
                }],
            )
            case = verify_manifest(path)["cases"][0]
            self.assertFalse(case["passed"])
            self.assertEqual(
                case["ambiguousResult"]["classifications"],
                {"ambiguous-unknown": 20},
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
                        "trials": 20,
                        "timeoutMs": 25,
                    },
                }],
            )
            case = verify_manifest(path)["cases"][0]
            self.assertTrue(case["passed"])
            self.assertEqual(
                case["ambiguousResult"]["classifications"],
                {"no-effect-timeout": 20},
            )

    def test_observer_control_records_a_stable_no_tool_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.manifest(Path(directory), [{"id": "read", "tool": "read_note"}])
            case = verify_manifest(path)["cases"][0]
            self.assertEqual(
                case["observerControl"],
                {"rounds": 1, "interference": False, "unstableObservers": [], "skipped": False},
            )
            self.assertTrue(case["passed"])

    def test_snapshot_induced_state_change_is_inconclusive_not_a_tool_violation(self) -> None:
        # An observed endpoint that mutates on every read: the change is reproducible
        # without any tool call, so it must never be blamed on the tool.
        counter = {"value": 0}

        class Counting(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                counter["value"] += 1
                body = json.dumps({"reads": counter["value"]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Counting)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = self.manifest(root, [{"id": "read", "tool": "read_note"}])
                value = json.loads(path.read_text())
                value["observers"].append(
                    {
                        "id": "unsafe-endpoint",
                        "kind": "http-json",
                        "url": f"http://127.0.0.1:{server.server_address[1]}/state",
                        "sensitive": False,
                    }
                )
                path.write_text(json.dumps(value))
                report = verify_manifest(path)
                case = report["cases"][0]
                self.assertTrue(case["observerControl"]["interference"])
                self.assertIn("unsafe-endpoint", case["observerControl"]["unstableObservers"])
                self.assertIn(
                    "OBSERVER_OR_BACKGROUND_INTERFERENCE",
                    {item["code"] for item in case["inconclusive"]},
                )
                # read_note declares readOnlyHint=true and the endpoint changed, but
                # the change is not attributable, so no hint violation is raised.
                self.assertEqual(case["violations"], [])
                self.assertFalse(case["passed"])
                self.assertEqual(report["verdict"], "inconclusive")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_unstable_observer_does_not_mask_stable_observer_violations(self) -> None:
        counter = {"value": 0}

        class Counting(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                counter["value"] += 1
                body = json.dumps({"reads": counter["value"]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Counting)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = self.manifest(root, [
                    {"id": "read-lie", "tool": "lying_read"},
                    {"id": "retry-lie", "tool": "append_audit",
                     "contract": {"idempotentHint": True}},
                    {"id": "ambiguous-retry-lie", "tool": "append_audit",
                     "ambiguousResultFault": {
                         "mode": "drop-result-after-response", "trials": 2}},
                ])
                value = json.loads(path.read_text())
                value["observers"].append({
                    "id": "unstable-endpoint",
                    "kind": "http-json",
                    "url": f"http://127.0.0.1:{server.server_address[1]}/state",
                    "sensitive": False,
                })
                path.write_text(json.dumps(value))
                cases = {case["id"]: case for case in verify_manifest(path)["cases"]}
                self.assertIn("READ_ONLY_HINT_MISMATCH",
                              {x["code"] for x in cases["read-lie"]["violations"]})
                self.assertIn("IDEMPOTENT_HINT_MISMATCH",
                              {x["code"] for x in cases["retry-lie"]["violations"]})
                ambiguous = cases["ambiguous-retry-lie"]
                self.assertEqual(ambiguous["ambiguousResult"]["classifications"],
                                 {"duplicate-on-retry": 2})
                self.assertIn("AMBIGUOUS_RESULT_DUPLICATE_ON_RETRY",
                              {x["code"] for x in ambiguous["violations"]})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_observer_control_can_be_disabled_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.manifest(Path(directory), [{"id": "read", "tool": "read_note"}])
            value = json.loads(path.read_text())
            value["cases"][0]["observerControlRounds"] = 0
            path.write_text(json.dumps(value))
            case = verify_manifest(path)["cases"][0]
            self.assertTrue(case["observerControl"]["skipped"])
            self.assertTrue(case["passed"])

    def test_manifest_rejects_invalid_observer_control_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.manifest(Path(directory), [{"id": "read", "tool": "read_note"}])
            value = json.loads(path.read_text())
            value["cases"][0]["observerControlRounds"] = 99
            path.write_text(json.dumps(value))
            with self.assertRaises(ManifestError):
                load_manifest(path)

    def test_paginated_tool_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.manifest(Path(directory), [{"id": "read", "tool": "read_note"}])
            value = json.loads(path.read_text())
            value["server"]["env"]["EFFECTFENCE_FIXTURE_PAGINATE"] = "1"
            path.write_text(json.dumps(value))
            report = verify_manifest(path)
            self.assertEqual(report["coverage"]["listedTools"], 7)
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

    def test_report_records_the_observed_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state.db"
            connection = sqlite3.connect(database)
            connection.execute("create table notes(id integer primary key)")
            connection.commit()
            connection.close()
            path = self.manifest(root, [{"id": "read", "tool": "read_note"}])
            value = json.loads(path.read_text())
            value["observers"] = [
                {
                    "id": "notes",
                    "kind": "sqlite-query",
                    "database": "state.db",
                    "query": "select id from notes",
                }
            ]
            path.write_text(json.dumps(value))
            report = verify_manifest(path)
            self.assertEqual([item["kind"] for item in report["observers"]], ["sqlite-query"])
            # SQLite state is sensitive by default, so the report carries no rows.
            self.assertTrue(report["observers"][0]["sensitive"])
            self.assertTrue(
                any("rows its query returns" in line for line in report["limitations"])
            )
            self.assertTrue(
                any("No filesystem observer" in line for line in report["limitations"])
            )
            self.assertEqual(report["verdict"], "pass")

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

    def slow_manifest(self, root: Path) -> Path:
        server = root / "slow_server.py"
        server.write_text(SLOW_SERVER, encoding="utf-8")
        state = root / "state"
        state.mkdir(exist_ok=True)
        manifest = {
            "schemaVersion": "effectfence.mcp.v1",
            "server": {
                "transport": "stdio",
                "command": [sys.executable, str(server)],
                "cwd": str(root),
                "startupTimeoutSeconds": 5,
                "requestTimeoutSeconds": 10,
            },
            "observers": [
                {
                    "id": "state",
                    "kind": "filesystem",
                    "root": str(state),
                    "sensitive": False,
                }
            ],
            "cases": [
                {"id": f"slow-{index}", "tool": "slow_note", "arguments": {}}
                for index in range(3)
            ],
            "policy": {"runTimeoutSeconds": 1},
        }
        path = root / "slow-manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def test_run_budget_reports_remaining_cases_as_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.slow_manifest(Path(directory))
            report = verify_manifest(path)
            self.assertEqual(report["verdict"], "inconclusive")
            self.assertTrue(report["runBudget"]["exhausted"])
            self.assertEqual(report["runBudget"]["timeoutSeconds"], 1.0)
            self.assertEqual(report["cases"][0]["id"], "slow-0")
            for case in report["cases"][1:]:
                self.assertEqual(
                    [item["code"] for item in case["inconclusive"]],
                    ["RUN_BUDGET_EXHAUSTED"],
                )
            self.assertFalse(any(case["violations"] for case in report["cases"]))

    def test_cli_timeout_exits_three_without_fabricating_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.slow_manifest(root)
            report_path = root / "report.json"
            exit_code = cli_main(
                ["mcp-verify", str(path), "--out", str(report_path), "--timeout", "1"]
            )
            self.assertEqual(exit_code, 3)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["verdict"], "inconclusive")

    def test_non_positive_run_timeout_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.slow_manifest(Path(directory))
            value = json.loads(path.read_text())
            value["policy"]["runTimeoutSeconds"] = 0
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(ManifestError):
                load_manifest(path)

    def test_fatal_error_names_its_phase_without_stderr_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = root / "dead_server.py"
            server.write_text(DEAD_SERVER, encoding="utf-8")
            path = self.dead_manifest(root, server)
            report = verify_manifest(path)
            self.assertEqual(report["verdict"], "fail")
            self.assertEqual(report["fatalError"]["phase"], "startup")
            self.assertIsNone(report["fatalError"]["case"])
            self.assertTrue(report["server"]["stderr_present"])
            self.assertNotIn("stderrExcerpt", report["server"])
            self.assertNotIn("SUPERSECRET", json.dumps(report))

    def test_capture_stderr_includes_a_bounded_redacted_excerpt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = root / "dead_server.py"
            server.write_text(DEAD_SERVER, encoding="utf-8")
            path = self.dead_manifest(root, server)
            report = verify_manifest(path, capture_stderr=True)
            excerpt = report["server"]["stderrExcerpt"]
            self.assertIn("missing --db", excerpt)
            self.assertNotIn("SUPERSECRET", excerpt)
            self.assertIn("[redacted]", excerpt)
            self.assertLessEqual(len(excerpt), 4000)

    def test_manifest_can_request_stderr_capture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = root / "dead_server.py"
            server.write_text(DEAD_SERVER, encoding="utf-8")
            path = self.dead_manifest(root, server)
            value = json.loads(path.read_text())
            value["server"]["captureStderr"] = True
            path.write_text(json.dumps(value), encoding="utf-8")
            report = verify_manifest(path)
            self.assertIn("missing --db", report["server"]["stderrExcerpt"])

    def test_manifest_schema_is_packaged(self) -> None:
        schema = json.loads(
            files("safesink.schemas")
            .joinpath("mcp-manifest-v1.schema.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(schema["properties"]["schemaVersion"]["const"], "effectfence.mcp.v1")



if __name__ == "__main__":
    unittest.main()
