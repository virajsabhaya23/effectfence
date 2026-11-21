from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from safesink.cli import main as cli_main
from safesink.mcp_lint import RULES, lint_manifest, lint_tools

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_SERVER = ROOT / "examples/mcp-conformance/fixture_server.py"


def tool(name: str, annotations: dict | None = None, properties: dict | None = None) -> dict:
    return {
        "name": name,
        "annotations": annotations or {},
        "inputSchema": {"type": "object", "properties": properties or {}},
    }


def codes(findings: list[dict]) -> set[str]:
    return {finding["code"] for finding in findings}


class LintRuleTests(unittest.TestCase):
    def test_a_clean_annotated_tool_scores_100(self) -> None:
        result = lint_tools(
            [
                tool(
                    "read_note",
                    {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
                )
            ]
        )
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["score"]["value"], 100)

    def test_missing_annotations_are_reported_once(self) -> None:
        result = lint_tools([tool("whatever")])
        self.assertEqual(codes(result["findings"]), {"NO_DECLARED_ANNOTATIONS"})
        self.assertEqual(result["score"]["value"], 95)

    def test_a_read_only_claim_on_a_write_verb_is_a_lie(self) -> None:
        result = lint_tools([tool("delete_file", {"readOnlyHint": True})])
        self.assertIn("READ_ONLY_NAME_MISMATCH", codes(result["findings"]))

    def test_a_non_destructive_claim_on_a_delete_verb_is_a_lie(self) -> None:
        result = lint_tools([tool("remove_record", {"destructiveHint": False})])
        self.assertIn(
            "DESTRUCTIVE_NAME_WITH_NON_DESTRUCTIVE_HINT", codes(result["findings"])
        )

    def test_contradictory_annotations_are_reported(self) -> None:
        result = lint_tools(
            [tool("purge_all", {"readOnlyHint": True, "destructiveHint": True})]
        )
        self.assertEqual(
            codes(result["findings"]),
            {"CONTRADICTORY_DESTRUCTIVE_READ_ONLY", "READ_ONLY_NAME_MISMATCH"},
        )

    def test_a_url_argument_contradicts_a_closed_world_claim(self) -> None:
        result = lint_tools(
            [
                tool(
                    "fetch_page",
                    {"readOnlyHint": True, "openWorldHint": False},
                    {"url": {"type": "string"}},
                )
            ]
        )
        self.assertIn("OPEN_WORLD_URL_ARGUMENT_MISMATCH", codes(result["findings"]))

    def test_a_mutating_tool_without_a_retry_claim_is_flagged(self) -> None:
        result = lint_tools([tool("upsert_record", {"readOnlyHint": False})])
        self.assertIn("MUTATING_WITHOUT_IDEMPOTENT_HINT", codes(result["findings"]))

    def test_an_idempotent_claim_with_a_volatile_argument_is_flagged(self) -> None:
        result = lint_tools(
            [
                tool(
                    "send_message",
                    {"readOnlyHint": False, "idempotentHint": True},
                    {"request_id": {"type": "string"}, "timestamp": {"type": "string"}},
                )
            ]
        )
        self.assertIn("IDEMPOTENT_WITH_VOLATILE_ARGUMENT", codes(result["findings"]))

    def test_the_score_has_per_rule_breakdown_and_caveats(self) -> None:
        result = lint_tools([tool("whatever")])
        breakdown = result["score"]["breakdown"]
        self.assertEqual(breakdown["NO_DECLARED_ANNOTATIONS"]["count"], 1)
        self.assertIn("caveat", breakdown["NO_DECLARED_ANNOTATIONS"])
        self.assertEqual(
            set(breakdown["NO_DECLARED_ANNOTATIONS"]["rationale"]),
            set(RULES["NO_DECLARED_ANNOTATIONS"]["rationale"]),
        )

    def test_lint_manifest_uses_case_contracts(self) -> None:
        manifest = {
            "cases": [
                {"tool": "delete_note", "contract": {"readOnlyHint": True}},
                {"tool": "read_note", "contract": {"readOnlyHint": True}},
            ]
        }
        result = lint_manifest(manifest)
        self.assertEqual(result["source"], "manifest")
        self.assertEqual(
            [finding["tool"] for finding in result["findings"]], ["delete_note"]
        )


class LintCliTests(unittest.TestCase):
    def test_lint_starts_a_server_and_calls_no_tool(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            exit_code = cli_main(
                ["lint", "--json", "--", sys.executable, str(FIXTURE_SERVER)]
            )
        self.assertEqual(exit_code, 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["mode"], "static-lint")
        self.assertGreaterEqual(result["toolsAdvertised"], 1)
        self.assertIn("score", result)

    def test_lint_manifest_and_min_annotation_score(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schemaVersion": "effectfence.mcp.v1",
                        "server": {"transport": "stdio", "command": ["server"]},
                        "observers": [{"id": "sandbox", "kind": "filesystem", "root": "."}],
                        "cases": [
                            {"id": "a", "tool": "delete_note", "contract": {"readOnlyHint": True}}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                exit_code = cli_main(["lint", "--manifest", str(manifest), "--json"])
            self.assertEqual(exit_code, 0)
            result = json.loads(stdout.getvalue())
            self.assertEqual(result["source"], "manifest")
            with contextlib.redirect_stderr(io.StringIO()):
                gated = cli_main(
                    ["lint", "--manifest", str(manifest), "--min-annotation-score", "100"]
                )
            self.assertEqual(gated, 2)

    def test_lint_requires_a_source(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as refused:
                cli_main(["lint"])
        self.assertEqual(refused.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
