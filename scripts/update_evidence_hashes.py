from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path("evidence/SHA256SUMS.json")


def repository_files() -> list[Path]:
    output = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
    )
    paths = [Path(item.decode()) for item in output.split(b"\0") if item]
    return sorted(path for path in paths if path != MANIFEST)


def main() -> None:
    hashes = {
        path.as_posix(): hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
        for path in repository_files()
    }
    (ROOT / MANIFEST).write_text(json.dumps(hashes, indent=2, sort_keys=True) + "\n")
    print(f"wrote {len(hashes)} hashes to {MANIFEST}")


if __name__ == "__main__":
    main()
