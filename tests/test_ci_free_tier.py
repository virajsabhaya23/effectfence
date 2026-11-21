from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
# Standard GitHub-hosted Ubuntu runners are free and unmetered on public repos.
# Anything below is either billed or unavailable to a GitHub Free account.
PAID_OR_PAID_ONLY = (
    "macos-",
    "windows-",
    "self-hosted",
    "-cores",
    "merge_group",
    "codeql",
    "github-pages",
    "larger-runner",
)


class FreeTierWorkflowTests(unittest.TestCase):
    def test_workflows_are_discovered(self) -> None:
        self.assertGreaterEqual(len(WORKFLOWS), 4)

    def test_only_standard_ubuntu_runners(self) -> None:
        for path in WORKFLOWS:
            with self.subTest(workflow=path.name):
                labels = re.findall(r"runs-on:\s*(\S+)", path.read_text(encoding="utf-8"))
                self.assertTrue(labels)
                self.assertEqual(set(labels), {"ubuntu-latest"})

    def test_no_billed_or_paid_only_features(self) -> None:
        for path in WORKFLOWS:
            text = path.read_text(encoding="utf-8")
            for feature in PAID_OR_PAID_ONLY:
                with self.subTest(workflow=path.name, feature=feature):
                    self.assertNotIn(feature, text)

    def test_every_workflow_declares_read_only_permissions(self) -> None:
        for path in WORKFLOWS:
            with self.subTest(workflow=path.name):
                self.assertRegex(path.read_text(encoding="utf-8"), r"(?m)^permissions:")

    def test_every_job_has_a_timeout(self) -> None:
        # A hung job holds one of the Free plan's 20 concurrent slots for up to 6h.
        for path in WORKFLOWS:
            text = path.read_text(encoding="utf-8")
            with self.subTest(workflow=path.name):
                self.assertEqual(text.count("runs-on:"), text.count("timeout-minutes:"))

    def test_jobs_stay_within_free_concurrency(self) -> None:
        for path in WORKFLOWS:
            text = path.read_text(encoding="utf-8")
            with self.subTest(workflow=path.name):
                self.assertLessEqual(text.count("runs-on:"), 20)

    def test_artifacts_have_bounded_retention(self) -> None:
        # Free plan includes 500 MB of artifact storage, shared with packages.
        for path in WORKFLOWS:
            text = path.read_text(encoding="utf-8")
            with self.subTest(workflow=path.name):
                uploads = text.count("actions/upload-artifact@")
                self.assertEqual(uploads, text.count("retention-days:"))
                for days in re.findall(r"retention-days:\s*(\d+)", text):
                    self.assertGreaterEqual(int(days), 1)
                    self.assertLessEqual(int(days), 30)


if __name__ == "__main__":
    unittest.main()
