from __future__ import annotations

import importlib
import inspect
import pkgutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dataclasses import is_dataclass
from typing import get_type_hints

import safesink

mods = [
    f"safesink.{module.name}"
    for module in pkgutil.iter_modules(safesink.__path__)
    if module.name != "__main__"
]
errors = []
for name in mods:
    try:
        mod = importlib.import_module(name)
    except Exception as e:
        errors.append(f"{name}: import failed: {e}")
        continue
    for n, o in vars(mod).items():
        if inspect.isclass(o) and is_dataclass(o):
            try:
                get_type_hints(o)
            except Exception as e:
                errors.append(f"{name}.{n}: unresolved type hints: {e}")
if errors:
    print("\n".join(errors))
    sys.exit(1)
print(f"type-interface gate: PASS ({len(mods)} modules)")
