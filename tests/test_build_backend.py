from __future__ import annotations

import hashlib
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

import build_backend


class BuildBackendTests(unittest.TestCase):
    def test_wheel_is_reproducible_and_contains_schema_and_license(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            first_wheel = Path(first) / build_backend.build_wheel(first)
            second_wheel = Path(second) / build_backend.build_wheel(second)
            self.assertEqual(
                hashlib.sha256(first_wheel.read_bytes()).digest(),
                hashlib.sha256(second_wheel.read_bytes()).digest(),
            )
            with zipfile.ZipFile(first_wheel) as archive:
                names = archive.namelist()
                self.assertIn(
                    "effectfence/schemas/mcp-manifest-v1.schema.json", names
                )
                self.assertIn("effectfence-0.2.0.dist-info/LICENSE", names)
                metadata = archive.read("effectfence-0.2.0.dist-info/METADATA")
                self.assertIn(b"Version: 0.2.0", metadata)
                self.assertIn(b"Description-Content-Type: text/markdown", metadata)

    def test_sdist_contains_offline_build_backend(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / build_backend.build_sdist(directory)
            with tarfile.open(source) as archive:
                names = archive.getnames()
            self.assertIn("effectfence-0.2.0/build_backend.py", names)
            self.assertIn("effectfence-0.2.0/pyproject.toml", names)
            self.assertFalse(any(".egg-info/" in name for name in names))


if __name__ == "__main__":
    unittest.main()
