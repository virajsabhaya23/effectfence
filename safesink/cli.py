from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .citation import citation
from .mcp_reports import write_json_report, write_junit_report, write_sarif_report
from .mcp_scan import ScanError, build_manifest, list_server_tools, write_manifest
from .mcp_verifier import ManifestError, verify_manifest


def _die(parser: argparse.ArgumentParser, message: str, *, hint: str | None = None) -> None:
    # argparse.error prints usage to stderr and exits 2 – we want a shorter,
    # more actionable line for manifest/io failures.
    text = f"{parser.prog}: error: {message}"
    if hint:
        text += f"\n  hint: {hint}"
    print(text, file=sys.stderr)
    parser.exit(2)


def build_parser(prog: str = "safesink") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="MCP side-effect conformance verifier (formerly EffectFence)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="cmd", required=True)

    mcp = commands.add_parser(
        "mcp-verify", help="verify MCP tool annotations against observed effects"
    )
    mcp.add_argument(
        "manifest", help="path to an effectfence.mcp.v1 manifest"
    )
    mcp.add_argument(
        "--out", default="safesink-mcp-report.json", help="JSON report path"
    )
    mcp.add_argument("--junit", help="JUnit XML path")
    mcp.add_argument("--sarif", help="SARIF path")
    mcp.add_argument("--verbose", action="store_true", help="print per-case verdicts")

    scan = commands.add_parser(
        "mcp-scan",
        help="generate a conformance manifest from a live server's advertised tools",
    )
    scan.add_argument("--observer-root", required=True, help="sandbox directory to observe")
    scan.add_argument("--out", required=True, help="path for the generated manifest")
    scan.add_argument("--cwd", default=".")
    scan.add_argument(
        "--include-destructive",
        action="store_true",
        help="also generate cases for tools declaring destructiveHint (sandbox only)",
    )
    scan.add_argument("--setup-command", nargs="+")
    scan.add_argument("--min-coverage", type=float, default=0.0)
    scan.add_argument("--inherit-env", nargs="+", default=["HOME"])
    scan.add_argument("--protocol-version", default="2025-11-25")
    scan.add_argument("--startup-timeout", type=float, default=60.0)
    scan.add_argument(
        "command",
        nargs="+",
        help="stdio command that starts the server; place it after ' -- '",
    )

    cite = commands.add_parser("citation", help="print copy-ready citation metadata")
    cite.add_argument("--format", choices=("bibtex", "cff", "json"), default="bibtex")
    return parser


def main(argv: list[str] | None = None, prog: str = "safesink") -> int:
    parser = build_parser(prog)
    arguments = parser.parse_args(argv)
    if arguments.cmd == "mcp-verify":
        try:
            report = verify_manifest(arguments.manifest)
        except ManifestError as error:
            _die(parser, str(error), hint="see docs/MCP_CONFORMANCE.md#manifest")
        except FileNotFoundError:
            _die(parser, f"manifest not found: {arguments.manifest}")
            return 2
        write_json_report(report, arguments.out)
        if arguments.junit:
            write_junit_report(report, arguments.junit)
        if arguments.sarif:
            write_sarif_report(report, arguments.sarif)
        summary = {
            "verdict": report["verdict"],
            "cases": len(report["cases"]),
            "toolCoverage": report["coverage"]["ratio"],
            "certificateSha256": report["certificateSha256"],
            "report": str(Path(arguments.out).resolve()),
        }
        print(json.dumps(summary, indent=2))
        if arguments.verbose:
            for case in report["cases"]:
                status = "PASS" if case["passed"] else "FAIL"
                print(f"  {status} {case['id']} ({case['tool']})", file=sys.stderr)
                for v in case.get("violations", []):
                    print(f"    - {v['code']}: {v.get('message','')}", file=sys.stderr)
        return 0 if report["verdict"] == "pass" else 2

    if arguments.cmd == "mcp-scan":
        observer_root = Path(arguments.observer_root)
        observer_root.mkdir(parents=True, exist_ok=True)
        try:
            tools, server_info = list_server_tools(
                arguments.command,
                cwd=Path(arguments.cwd).resolve(),
                inherit_env=arguments.inherit_env,
                protocol_version=arguments.protocol_version,
                startup_timeout_seconds=arguments.startup_timeout,
            )
            manifest = build_manifest(
                tools,
                command=arguments.command,
                observer_root=str(observer_root),
                cwd=arguments.cwd,
                include_destructive=arguments.include_destructive,
                setup_command=arguments.setup_command,
                minimum_tool_coverage=arguments.min_coverage,
                inherit_env=arguments.inherit_env,
                protocol_version=arguments.protocol_version,
            )
        except ScanError as error:
            parser.error(str(error))
        destination = write_manifest(manifest, arguments.out)
        print(
            json.dumps(
                {
                    "server": server_info.get("name"),
                    "version": server_info.get("version"),
                    "toolsAdvertised": len(tools),
                    "casesGenerated": len(manifest["cases"]),
                    "skipped": manifest["generated"]["skipped"],
                    "manifest": str(destination),
                },
                indent=2,
            )
        )
        return 0

    print(citation(arguments.format))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
