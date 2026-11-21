from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
RELEASE = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")


class ReleaseWorkflowTests(unittest.TestCase):
    def test_it_can_be_started_by_hand_with_a_version(self) -> None:
        self.assertIn("workflow_dispatch:", RELEASE)
        self.assertIn("types: [published]", RELEASE)
        self.assertIn("inputs:", RELEASE)

    def test_it_checks_every_version_source(self) -> None:
        for source in ("pyproject.toml", "safesink/__init__.py", "build_backend.py"):
            self.assertIn(source, RELEASE)

    def test_it_requires_changelog_notes_and_runs_the_gates_first(self) -> None:
        self.assertIn("CHANGELOG.md", RELEASE)
        for gate in ("unittest discover", "format_check.py", "lint.py", "typecheck.py"):
            self.assertIn(gate, RELEASE)

    def test_it_verifies_the_published_distribution(self) -> None:
        self.assertIn("verify-published:", RELEASE)
        self.assertIn("needs: publish", RELEASE)
        self.assertIn("safesink==${version}", RELEASE)

    def test_it_publishes_with_trusted_publishing_only(self) -> None:
        self.assertIn("id-token: write", RELEASE)
        self.assertIn("pypa/gh-action-pypi-publish", RELEASE)
        self.assertNotIn("PYPI_API_TOKEN", RELEASE)
        self.assertNotIn("password:", RELEASE)


if __name__ == "__main__":
    unittest.main()
