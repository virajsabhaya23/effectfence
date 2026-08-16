from pathlib import Path
import sys
errors=[]
for base in ("effectfence","tests","scripts"):
    for p in Path(base).rglob("*.py"):
        text=p.read_text()
        if text and not text.endswith("\n"): errors.append(f"{p}: missing final newline")
        for i,line in enumerate(text.splitlines(),1):
            if line.rstrip()!=line: errors.append(f"{p}:{i}: trailing whitespace")
            if "\t" in line: errors.append(f"{p}:{i}: tab")
if errors: print("\n".join(errors)); sys.exit(1)
print("format-hygiene gate: PASS")
