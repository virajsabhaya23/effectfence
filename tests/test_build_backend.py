from __future__ import annotations

import hashlib
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

import build_backend
import safesink

DIST = f"safesink-{safesink.__version__}"


class BuildBackendTests(unittest.TestCase):
    def test_backend_version_matches_the_package(self) -> None:
        self.assertEqual(build_backend.VERSION, safesink.__version__)

    def test_console_scripts_point_at_their_own_module(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            wheel = Path(directory) / build_backend.build_wheel(directory)
            with zipfile.ZipFile(wheel) as archive:
                entry_points = archive.read(
                    f"{DIST}.dist-info/entry_points.txt"
                ).decode()
        self.assertIn("safesink = safesink.cli:main", entry_points)
        self.assertIn("safesink-legacy = safesink.legacy.cli:main", entry_points)
        self.assertIn("effectfence = effectfence.cli:main", entry_points)

    def test_metadata_points_at_the_real_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            wheel = Path(directory) / build_backend.build_wheel(directory)
            with zipfile.ZipFile(wheel) as archive:
                metadata = archive.read(f"{DIST}.dist-info/METADATA").decode()
        self.assertIn(
            "Project-URL: Repository, https://github.com/virajsabhaya23/effectfence",
            metadata,
        )
        self.assertNotIn("virajsabhaya23/safesink", metadata)

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
                # new primary package
                self.assertIn(
                    "safesink/schemas/mcp-manifest-v1.schema.json", names
                )
                self.assertIn("safesink/legacy/simulator.py", names)
                self.assertIn(f"{DIST}.dist-info/LICENSE", names)
                metadata = archive.read(f"{DIST}.dist-info/METADATA")
                self.assertIn(f"Version: {safesink.__version__}".encode(), metadata)
                self.assertIn(b"Description-Content-Type: text/markdown", metadata)
                # legacy shim still shipped
                self.assertIn(
                    "effectfence/schemas/mcp-manifest-v1.schema.json", names
                )

    def test_sdist_contains_offline_build_backend(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / build_backend.build_sdist(directory)
            with tarfile.open(source) as archive:
                names = archive.getnames()
            self.assertIn(f"{DIST}/build_backend.py", names)
            self.assertIn(f"{DIST}/pyproject.toml", names)
            self.assertFalse(any(".egg-info/" in name for name in names))
            # ``python -m build`` refuses an sdist without PKG-INFO, which would
            # break the PyPI release job before it ever uploads anything.
            with tarfile.open(source) as archive:
                metadata = archive.extractfile(f"{DIST}/PKG-INFO")
                self.assertIsNotNone(metadata)
                self.assertIn(
                    f"Version: {safesink.__version__}".encode(), metadata.read()
                )


if __name__ == "__main__":
    unittest.main()
