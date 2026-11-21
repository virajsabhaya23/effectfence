from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

from . import __version__
from .mcp_client import McpClientError
from .mcp_reports import write_json_report, write_junit_report, write_sarif_report
from .mcp_scan import (
    ScanError,
    build_manifest,
    classify,
    list_server_tools,
    write_manifest,
)
from .mcp_verifier import ManifestError, verify_manifest


def _die(parser: argparse.ArgumentParser, message: str, *, hint: str | None = None) -> None:
    # argparse.error prints usage to stderr and exits 2 – we want a shorter,
    # more actionable line for manifest/io failures.
    text = f"{parser.prog}: error: {message}"
    if hint:
        text += f"\n  hint: {hint}"
    print(text, file=sys.stderr)
    parser.exit(2)


def _parse_env(parser: argparse.ArgumentParser, pairs: list[str]) -> dict[str, str]:
    environment: dict[str, str] = {}
    for pair in pairs:
        key, separator, value = pair.partition("=")
        if not separator or not key:
            _die(parser, f"--env expects KEY=VALUE, got {pair!r}")
        environment[key] = value
    return environment


def _parse_sqlite_queries(
    parser: argparse.ArgumentParser, entries: list[str]
) -> list[tuple[str, str]]:
    queries: list[tuple[str, str]] = []
    for entry in entries:
        # Split on the first '=' only, so SQL containing '=' still works.
        database, separator, query = entry.partition("=")
        if not separator or not database.strip() or not query.strip():
            _die(parser, f"--sqlite-query expects DATABASE=SQL, got {entry!r}")
        queries.append((database.strip(), query.strip()))
    return queries


def _add_scan_options(parser: argparse.ArgumentParser, *, required_out: bool) -> None:
    parser.add_argument(
        "--observer-root", required=True, help="sandbox directory to observe"
    )
    if required_out:
        parser.add_argument("--out", required=True, help="path for the generated manifest")


def build_parser(prog: str = "safesink") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="MCP side-effect conformance verifier (formerly EffectFence)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="cmd", required=True)

    check = commands.add_parser(
        "mcp-check",
        help="scan a server and verify it in one step (no manifest needed)",
    )
    check.add_argument(
        "--sandbox",
        help="directory to observe; defaults to a fresh temporary one",
    )
    check.add_argument("--out", default="safesink-mcp-report.json", help="JSON report path")
    check.add_argument("--junit", help="JUnit XML path")
    check.add_argument("--sarif", help="SARIF path")
    check.add_argument("--manifest-out", help="also keep the generated manifest here")
    check.add_argument(
        "--include-destructive",
        action="store_true",
        help="also call tools declaring destructiveHint (sandbox only)",
    )
    check.add_argument("--min-coverage", type=float, default=0.0)
    check.add_argument("--inherit-env", nargs="+", default=["HOME"])
    check.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="environment variable for the server (repeatable)",
    )
    check.add_argument(
        "--sqlite-query",
        action="append",
        default=[],
        metavar="DATABASE=SQL",
        help=(
            "observe the rows this query returns instead of the sandbox file hash "
            "(repeatable; the database must already exist)"
        ),
    )
    check.add_argument(
        "--sandbox-observer",
        action="store_true",
        help="also hash the sandbox directory when --sqlite-query is used",
    )
    check.add_argument(
        "--allow-unobserved-mutations",
        action="store_true",
        help=(
            "allow --include-destructive to call destructive tools when the only "
            "observed boundary is a real database"
        ),
    )
    check.add_argument(
        "--capture-stderr",
        action="store_true",
        help="include a bounded, redacted server stderr excerpt in the report",
    )
    check.add_argument("--protocol-version", default="2025-11-25")
    check.add_argument("--startup-timeout", type=float, default=60.0)
    check.add_argument(
        "--keep-sandbox",
        action="store_true",
        help="keep the temporary sandbox for inspection",
    )
    check.add_argument(
        "command",
        nargs="+",
        help="stdio command that starts the server; place it after ' -- '",
    )

    mcp = commands.add_parser(
        "mcp-verify", help="verify MCP tool annotations against observed effects"
    )
    mcp.add_argument("manifest", help="path to an effectfence.mcp.v1 manifest")
    mcp.add_argument(
        "--out", default="safesink-mcp-report.json", help="JSON report path"
    )
    mcp.add_argument("--junit", help="JUnit XML path")
    mcp.add_argument("--sarif", help="SARIF path")
    mcp.add_argument("--verbose", action="store_true", help="print per-case verdicts")
    mcp.add_argument(
        "--capture-stderr",
        action="store_true",
        help="include a bounded, redacted server stderr excerpt in the report",
    )

    scan = commands.add_parser(
        "mcp-scan",
        help="generate a conformance manifest from a live server's advertised tools",
    )
    _add_scan_options(scan, required_out=True)
    scan.add_argument("--cwd", default=".")
    scan.add_argument(
        "--include-destructive",
        action="store_true",
        help="also generate cases for tools declaring destructiveHint (sandbox only)",
    )
    scan.add_argument("--setup-command", nargs="+")
    scan.add_argument("--min-coverage", type=float, default=0.0)
    scan.add_argument("--inherit-env", nargs="+", default=["HOME"])
    scan.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="environment variable for the server (repeatable)",
    )
    scan.add_argument("--protocol-version", default="2025-11-25")
    scan.add_argument("--startup-timeout", type=float, default=60.0)
    scan.add_argument(
        "command",
        nargs="+",
        help="stdio command that starts the server; place it after ' -- '",
    )
    return parser


def _exit_code(report: dict) -> int:
    """0 = verified, 3 = nothing failed but nothing could be verified, 2 = finding."""
    return {"pass": 0, "inconclusive": 3}.get(str(report.get("verdict")), 2)


def _print_case_verdicts(report: dict) -> None:
    for case in report["cases"]:
        if case["passed"]:
            status = "PASS"
        elif case.get("violations"):
            status = "FAIL"
        else:
            status = "INCONCL"
        print(f"  {status} {case['id']} ({case['tool']})", file=sys.stderr)
        for item in case.get("violations", []):
            print(f"    - {item['code']}: {item.get('message','')}", file=sys.stderr)
        for item in case.get("inconclusive", []):
            print(f"    ~ {item['code']}: {item.get('message','')}", file=sys.stderr)


def main(argv: list[str] | None = None, prog: str = "safesink") -> int:
    parser = build_parser(prog)
    arguments = parser.parse_args(argv)

    if arguments.cmd == "mcp-check":
        temporary = arguments.sandbox is None
        sandbox = (
            Path(arguments.sandbox)
            if arguments.sandbox
            else Path(tempfile.mkdtemp(prefix="safesink-check-"))
        )
        sandbox.mkdir(parents=True, exist_ok=True)
        manifest_path = Path(arguments.manifest_out or (sandbox / "manifest.json"))
        server_env = _parse_env(parser, arguments.env)
        sqlite_queries = _parse_sqlite_queries(parser, arguments.sqlite_query)
        try:
            tools, server_info = list_server_tools(
                arguments.command,
                cwd=Path.cwd(),
                inherit_env=arguments.inherit_env,
                env=server_env,
                protocol_version=arguments.protocol_version,
                startup_timeout_seconds=arguments.startup_timeout,
            )
            destructive = sorted(
                tool["name"] for tool in tools if classify(tool) == "destructive"
            )
            # A seeded database is real user data. Watching it is not the same as
            # being allowed to mutate it: the sandbox is disposable, orders.db is
            # not. Refuse rather than report a deletion we caused ourselves.
            if (
                destructive
                and arguments.include_destructive
                and sqlite_queries
                and not arguments.sandbox_observer
                and not arguments.allow_unobserved_mutations
            ):
                databases = ", ".join(
                    str(Path(database).expanduser().resolve())
                    for database, _ in sqlite_queries
                )
                _die(
                    parser,
                    "refusing to call destructive tools against a real database",
                    hint=(
                        f"no filesystem sandbox observer is configured, so "
                        f"{', '.join(destructive)} would mutate: {databases}. "
                        "Pass --allow-unobserved-mutations if that is intended, or add "
                        "--sandbox-observer and point the server at a disposable copy."
                    ),
                )
                return 2
            manifest = build_manifest(
                tools,
                command=arguments.command,
                observer_root=str(sandbox),
                cwd=".",
                include_destructive=arguments.include_destructive,
                minimum_tool_coverage=arguments.min_coverage,
                inherit_env=arguments.inherit_env,
                env=server_env,
                protocol_version=arguments.protocol_version,
                sqlite_queries=sqlite_queries,
                include_sandbox_observer=arguments.sandbox_observer,
                capture_stderr=arguments.capture_stderr,
            )
            write_manifest(manifest, manifest_path)
            report = verify_manifest(
                manifest_path, capture_stderr=arguments.capture_stderr
            )
        except ScanError as error:
            _die(parser, str(error))
            return 2
        except McpClientError as error:
            _die(parser, str(error), hint="the server must speak MCP over stdio")
            return 2
        except ManifestError as error:
            _die(parser, str(error))
            return 2
        finally:
            if temporary and not arguments.keep_sandbox:
                shutil.rmtree(sandbox, ignore_errors=True)
        write_json_report(report, arguments.out)
        if arguments.junit:
            write_junit_report(report, arguments.junit)
        if arguments.sarif:
            write_sarif_report(report, arguments.sarif)
        print(
            json.dumps(
                {
                    "server": server_info.get("name"),
                    "version": server_info.get("version"),
                    "verdict": report["verdict"],
                    "cases": len(report["cases"]),
                    "toolCoverage": report["coverage"]["ratio"],
                    "observers": [item["kind"] for item in report.get("observers", [])],
                    "inconclusive": sum(
                        1 for case in report["cases"] if case.get("inconclusive")
                    ),
                    "certificateSha256": report["certificateSha256"],
                    "report": str(Path(arguments.out).resolve()),
                },
                indent=2,
            )
        )
        _print_case_verdicts(report)
        return _exit_code(report)

    if arguments.cmd == "mcp-verify":
        try:
            report = verify_manifest(
                arguments.manifest, capture_stderr=arguments.capture_stderr
            )
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
            _print_case_verdicts(report)
        return _exit_code(report)

    # mcp-scan
    observer_root = Path(arguments.observer_root)
    observer_root.mkdir(parents=True, exist_ok=True)
    server_env = _parse_env(parser, arguments.env)
    try:
        tools, server_info = list_server_tools(
            arguments.command,
            cwd=Path(arguments.cwd).resolve(),
            inherit_env=arguments.inherit_env,
            env=server_env,
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
            env=server_env,
            protocol_version=arguments.protocol_version,
        )
    except (ScanError, McpClientError) as error:
        _die(parser, str(error), hint="the server must speak MCP over stdio")
        return 2
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


if __name__ == "__main__":
    raise SystemExit(main())
