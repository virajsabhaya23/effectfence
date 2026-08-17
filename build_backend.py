from __future__ import annotations

import base64
import csv
import hashlib
import io
import os
import tarfile
import zipfile
from pathlib import Path


NAME = "effectfence"
VERSION = "0.2.0"
DIST = f"{NAME}-{VERSION}"
WHEEL = f"{DIST}-py3-none-any.whl"


def _metadata() -> str:
    readme = (Path(__file__).resolve().parent / "README.md").read_text(encoding="utf-8")
    headers = f"""Metadata-Version: 2.3
Name: {NAME}
Version: {VERSION}
Summary: Crash/retry and MCP side-effect conformance verifier
Author: Viraj Sabhaya
License: Apache-2.0
Requires-Python: >=3.10
Description-Content-Type: text/markdown
Keywords: mcp,model-context-protocol,idempotency,conformance,crash-recovery
Classifier: Development Status :: 4 - Beta
Classifier: Environment :: Console
Classifier: Intended Audience :: Developers
Classifier: License :: OSI Approved :: Apache Software License
Classifier: Operating System :: OS Independent
Classifier: Programming Language :: Python :: 3
Classifier: Programming Language :: Python :: 3 :: Only
Classifier: Topic :: Software Development :: Quality Assurance
Classifier: Topic :: Software Development :: Testing
Project-URL: Homepage, https://github.com/virajsabhaya23/effectfence
Project-URL: Repository, https://github.com/virajsabhaya23/effectfence
Project-URL: Issues, https://github.com/virajsabhaya23/effectfence/issues
Provides-Extra: live-kafka-postgres
Requires-Dist: confluent-kafka>=2.5,<3; extra == "live-kafka-postgres"
Requires-Dist: psycopg[binary]>=3.2,<4; extra == "live-kafka-postgres"

"""
    return headers + readme + "\n"


def _wheel_metadata() -> str:
    return """Wheel-Version: 1.0
Generator: effectfence-build-backend
Root-Is-Purelib: true
Tag: py3-none-any
"""


def _hash(data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode()
    return f"sha256={digest.rstrip('=')}"


def _package_files(root: Path) -> list[tuple[Path, str]]:
    included: list[tuple[Path, str]] = []
    for pattern in ("*.py", "*.json"):
        for path in (root / "effectfence").rglob(pattern):
            included.append((path, path.relative_to(root).as_posix()))
    return sorted(included, key=lambda item: item[1])


def _zip_timestamp() -> tuple[int, int, int, int, int, int]:
    epoch = max(int(os.environ.get("SOURCE_DATE_EPOCH", "315532800")), 315532800)
    import time

    value = time.gmtime(epoch)
    return (value.tm_year, value.tm_mon, value.tm_mday, value.tm_hour, value.tm_min, value.tm_sec)


def _write_zip(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, _zip_timestamp())
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    archive.writestr(info, data)


def build_wheel(
    wheel_directory: str,
    config_settings: dict | None = None,
    metadata_directory: str | None = None,
) -> str:
    del config_settings, metadata_directory
    root = Path(__file__).resolve().parent
    output = Path(wheel_directory)
    output.mkdir(parents=True, exist_ok=True)
    target = output / WHEEL
    dist_info = f"{DIST}.dist-info"
    entries: list[tuple[str, str, str]] = []
    with zipfile.ZipFile(target, "w") as archive:
        for path, archive_name in _package_files(root):
            data = path.read_bytes()
            _write_zip(archive, archive_name, data)
            entries.append((archive_name, _hash(data), str(len(data))))
        generated = {
            f"{dist_info}/METADATA": _metadata().encode(),
            f"{dist_info}/WHEEL": _wheel_metadata().encode(),
            f"{dist_info}/LICENSE": (root / "LICENSE").read_bytes(),
            f"{dist_info}/entry_points.txt": (
                b"[console_scripts]\neffectfence = effectfence.cli:main\n"
            ),
        }
        for archive_name, data in generated.items():
            _write_zip(archive, archive_name, data)
            entries.append((archive_name, _hash(data), str(len(data))))
        record_name = f"{dist_info}/RECORD"
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerows(entries)
        writer.writerow((record_name, "", ""))
        _write_zip(archive, record_name, buffer.getvalue().encode())
    return WHEEL


def prepare_metadata_for_build_wheel(
    metadata_directory: str, config_settings: dict | None = None
) -> str:
    del config_settings
    directory = Path(metadata_directory) / f"{DIST}.dist-info"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "METADATA").write_text(_metadata(), encoding="utf-8")
    (directory / "WHEEL").write_text(_wheel_metadata(), encoding="utf-8")
    (directory / "entry_points.txt").write_text(
        "[console_scripts]\neffectfence = effectfence.cli:main\n", encoding="utf-8"
    )
    return directory.name


def build_sdist(sdist_directory: str, config_settings: dict | None = None) -> str:
    del config_settings
    root = Path(__file__).resolve().parent
    output = Path(sdist_directory)
    output.mkdir(parents=True, exist_ok=True)
    name = f"{DIST}.tar.gz"
    target = output / name
    excluded = {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "out",
        "dist",
        "build",
    }
    with tarfile.open(target, "w:gz") as archive:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or any(
                part in excluded or part.endswith(".egg-info") for part in path.parts
            ):
                continue
            archive.add(path, arcname=f"{DIST}/{path.relative_to(root)}")
    return name
