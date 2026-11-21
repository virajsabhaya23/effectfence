#!/usr/bin/env python3
"""Scan every pinned server in servers.json and write data/leaderboard.json.

Reproducibility is the point: the command, version and date are recorded, so a
row can be re-derived later. A scan that cannot start is recorded as
``inconclusive`` with its error, never omitted.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def scan(entry: dict, *, timeout: float) -> dict:
    with tempfile.TemporaryDirectory() as directory:
        report_path = Path(directory) / "report.json"
        command = [
            sys.executable,
            "-m",
            "safesink",
            "mcp-check",
            "--sandbox",
            str(Path(directory) / "sandbox"),
            "--out",
            str(report_path),
            "--timeout",
            str(timeout),
            "--",
            *entry["command"],
        ]
        completed = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=timeout + 60,
            check=False,
        )
        row = {
            "server": entry["name"],
            "package": entry.get("package"),
            "version": entry.get("version"),
            "command": entry["command"],
            "scannedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        if not report_path.exists():
            row.update(
                {
                    "verdict": "inconclusive",
                    "score": None,
                    "tools": None,
                    "cases": None,
                    "findings": None,
                    "inconclusive": None,
                    "error": (completed.stderr or "no report was written").strip()[:500],
                }
            )
            return row
        report = json.loads(report_path.read_text(encoding="utf-8"))
        cases = report.get("cases", [])
        row.update(
            {
                "verdict": report.get("verdict"),
                "score": (report.get("score") or {}).get("value"),
                "tools": (report.get("coverage") or {}).get("listedTools"),
                "cases": len(cases),
                "findings": sum(len(case.get("violations", [])) for case in cases),
                "inconclusive": sum(1 for case in cases if case.get("inconclusive")),
                "certificateSha256": report.get("certificateSha256"),
                "manifestSha256": (report.get("manifest") or {}).get("sha256"),
            }
        )
        return row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--servers", default="servers.json")
    parser.add_argument("--out", default="data/leaderboard.json")
    parser.add_argument("--html", default="data/leaderboard.html")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--only", action="append", default=[], help="limit to these names")
    arguments = parser.parse_args(argv)

    registry = json.loads((ROOT / arguments.servers).read_text(encoding="utf-8"))
    entries = [
        entry
        for entry in registry["servers"]
        if not arguments.only or entry["name"] in arguments.only
    ]
    rows = [scan(entry, timeout=arguments.timeout) for entry in entries]
    data = {
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": arguments.servers,
        "entries": rows,
    }
    destination = ROOT / arguments.out
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    # Imported lazily so the script still runs when invoked from a checkout that
    # has not been installed.
    sys.path.insert(0, str(ROOT))
    from safesink.mcp_reports import render_leaderboard_html

    html_path = ROOT / arguments.html
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(render_leaderboard_html(data), encoding="utf-8")
    print(json.dumps({"rows": len(rows), "out": str(destination), "html": str(html_path)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
