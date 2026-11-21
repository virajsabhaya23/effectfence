from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from safesink.cli import main as cli_main
from safesink.mcp_reports import render_badge_svg, render_leaderboard_html


class BadgeTests(unittest.TestCase):
    def test_the_verdict_and_score_are_visible(self) -> None:
        svg = render_badge_svg("pass", 100)
        root = ET.fromstring(svg)
        self.assertIn("pass 100", svg)
        self.assertEqual(root.get("role"), "img")
        self.assertIn("#4c1", svg)

    def test_failures_are_red_and_unknown_states_are_neutral(self) -> None:
        self.assertIn("#e05d44", render_badge_svg("fail", 35))
        self.assertIn("#9f9f9f", render_badge_svg("unknown"))
        self.assertIn("#dfb317", render_badge_svg("inconclusive"))

    def test_a_label_is_xml_escaped(self) -> None:
        svg = render_badge_svg("pass", 100, label='a<b"c')
        ET.fromstring(svg)
        self.assertNotIn('a<b"c', svg)

    def test_the_cli_writes_the_badge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            report.write_text(
                json.dumps({"verdict": "pass", "score": {"value": 100}}), encoding="utf-8"
            )
            out = Path(directory) / "nested" / "badge.svg"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = cli_main(["badge", "--report", str(report), "--out", str(out)])
            self.assertEqual(exit_code, 0)
            self.assertIn("pass 100", out.read_text(encoding="utf-8"))
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["verdict"], "pass")
            self.assertIn("badge.svg", payload["markdown"])


class LeaderboardTests(unittest.TestCase):
    def test_entries_are_escaped_and_the_empty_state_is_explicit(self) -> None:
        html = render_leaderboard_html({"generatedAt": None, "entries": []})
        self.assertIn("No scans recorded yet.", html)
        self.assertIn("never", html)

        html = render_leaderboard_html(
            {
                "generatedAt": "2026-01-01T00:00:00Z",
                "entries": [
                    {
                        "server": "<script>alert(1)</script>",
                        "version": "1.0.0",
                        "tools": 3,
                        "cases": 3,
                        "findings": 0,
                        "inconclusive": 0,
                        "verdict": "pass",
                        "score": 100,
                    }
                ],
            }
        )
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("2026-01-01", html)

    def test_the_page_states_the_limits_of_a_pass(self) -> None:
        html = render_leaderboard_html({"entries": []})
        self.assertIn("not a security certification", html)
        self.assertIn("deliberately dishonest fixture", html)


if __name__ == "__main__":
    unittest.main()
