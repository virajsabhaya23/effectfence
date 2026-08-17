#!/usr/bin/env python3
from __future__ import annotations

import shutil
import sys
from pathlib import Path


root = Path(sys.argv[1]).resolve()
shutil.rmtree(root, ignore_errors=True)
root.mkdir(parents=True)
(root / "note.txt").write_text("hello\n", encoding="utf-8")
