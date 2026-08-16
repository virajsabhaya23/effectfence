from __future__ import annotations
import importlib, inspect, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dataclasses import is_dataclass
from typing import get_type_hints
mods=["effectfence.model","effectfence.ledger","effectfence.sinks","effectfence.simulator",
      "effectfence.explore","effectfence.io","effectfence.reports","effectfence.benchmark","effectfence.cli"]
errors=[]
for name in mods:
    try: mod=importlib.import_module(name)
    except Exception as e: errors.append(f"{name}: import failed: {e}"); continue
    for n,o in vars(mod).items():
        if inspect.isclass(o) and is_dataclass(o):
            try: get_type_hints(o)
            except Exception as e: errors.append(f"{name}.{n}: unresolved type hints: {e}")
if errors: print("\n".join(errors)); sys.exit(1)
print("type-interface gate: PASS")
