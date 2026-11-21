from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from . import __version__
from .certificates import (
    CertificateError,
    build_certificate,
    generate_key,
    sign_certificate,
    verify_certificate,
)
from .mcp_client import McpClientError
from .mcp_lint import lint_manifest, lint_tools
from .mcp_reports import (
    render_markdown_report,
    write_json_report,
    write_junit_report,
    write_markdown_report,
    write_sarif_report,
)
from .mcp_scan import (
    HINT_NAMES,
    ScanError,
    build_manifest,
    classify,
    list_server_tools,
    scaffold_manifest,
    write_manifest,
)
from .mcp_verifier import (
    ManifestError,
    load_manifest,
    load_suppressions,
    verify_manifest,
)


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


def _parse_hint_spec(parser: argparse.ArgumentParser, spec: str) -> dict[str, bool]:
    hints: dict[str, bool] = {}
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        name, separator, raw = item.partition(":")
        name = name.strip()
        raw = raw.strip().lower()
        if not separator or name not in HINT_NAMES:
            _die(
                parser,
                f"unknown effect hint {name!r}",
                hint=(
                    f"valid hints: {', '.join(HINT_NAMES)}; "
                    "expected TOOL=hint:true|false, hint:true|false"
                ),
            )
        if raw not in {"true", "false"}:
            _die(parser, f"hint {name} must be true or false, got {raw!r}")
        hints[name] = raw == "true"
    if not hints:
        _die(parser, f"empty contract: expected hint:true|false, got {spec!r}")
    return hints


def _parse_contracts(
    parser: argparse.ArgumentParser, entries: list[str]
) -> dict[str, dict[str, bool]]:
    contracts: dict[str, dict[str, bool]] = {}
    for entry in entries:
        tool, separator, spec = entry.partition("=")
        if not separator or not tool.strip():
            _die(
                parser,
                f"--contract expects TOOL=hint:value,..., got {entry!r}",
                hint="for example --contract list_tables=readOnlyHint:true",
            )
        contracts[tool.strip()] = _parse_hint_spec(parser, spec)
    return contracts


def _load_baseline(
    parser: argparse.ArgumentParser, path: str | None, new_only: bool
) -> tuple[dict | None, str | None]:
    if new_only and not path:
        _die(parser, "--new-only requires --baseline PREVIOUS-REPORT.json")
    if not path:
        return None, None
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        _die(parser, f"baseline report not found: {path}")
    except (OSError, json.JSONDecodeError) as error:
        _die(parser, f"could not read baseline report: {error}")
    if not isinstance(value, dict):
        _die(parser, "baseline report must be a JSON object")
    return value, path


def _load_suppressions(
    parser: argparse.ArgumentParser, path: str | None
) -> tuple[dict | None, str | None]:
    if not path:
        return None, None
    try:
        return load_suppressions(path), path
    except ManifestError as error:
        _die(parser, str(error), hint="every entry needs a reason and a YYYY-MM-DD expiry")
    return None, None


def _add_suppression_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--suppress",
        metavar="FILE.json",
        help="accept specific findings with a reason and an expiry date",
    )
    parser.add_argument(
        "--fail-on-suppressed",
        action="store_true",
        help="treat any accepted finding as a failure (high-assurance runs)",
    )


def _write_markdown(parser: argparse.ArgumentParser, report: dict, arguments: Any) -> None:
    if getattr(arguments, "markdown", None):
        write_markdown_report(report, arguments.markdown)
    if getattr(arguments, "github_summary", False):
        target = os.environ.get("GITHUB_STEP_SUMMARY")
        if not target:
            _die(
                parser,
                "--github-summary needs GITHUB_STEP_SUMMARY to be set",
                hint="this flag only works inside a GitHub Actions step",
            )
            return
        with open(target, "a", encoding="utf-8") as stream:
            stream.write(render_markdown_report(report) + "\n")


def _add_report_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--markdown", help="Markdown report path for a review comment")
    parser.add_argument(
        "--github-summary",
        action="store_true",
        help="also append the Markdown report to GITHUB_STEP_SUMMARY",
    )


def _add_isolation_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--restart-per-case",
        action="store_true",
        help="restart the server between cases so no state carries over",
    )
    parser.add_argument(
        "--case-order",
        choices=("file", "name", "manifest"),
        help="case execution order: as written, alphabetical, or manifest.caseOrder",
    )


def _add_baseline_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--baseline",
        metavar="REPORT.json",
        help="previous report; findings it already recorded are not new",
    )
    parser.add_argument(
        "--new-only",
        action="store_true",
        help="fail only on findings absent from --baseline",
    )


def _add_contract_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--contract",
        action="append",
        default=[],
        metavar="TOOL=HINT:VALUE",
        help=(
            "declare an effect hint for one tool, for servers that publish no "
            "annotations (repeatable; comma-separate several hints)"
        ),
    )
    parser.add_argument(
        "--contract-all",
        metavar="HINT:VALUE",
        help="declare the same policy for every tool; --contract overrides it per tool",
    )


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
    check.add_argument(
        "--min-score",
        type=float,
        help="fail when the documented trust score is below this value (0-100)",
    )
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
        help="also hash the sandbox directory when a query observer is used",
    )
    check.add_argument(
        "--pgsql-query",
        action="append",
        default=[],
        metavar="ENV_VAR=SQL",
        help=(
            "observe a PostgreSQL query; ENV_VAR names the variable holding the "
            "DSN, which never enters the manifest (repeatable)"
        ),
    )
    check.add_argument(
        "--mysql-query",
        action="append",
        default=[],
        metavar="ENV_VAR=SQL",
        help="observe a MySQL query; ENV_VAR names the variable holding the DSN (repeatable)",
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
    check.add_argument(
        "--url",
        help="streamable-HTTP MCP endpoint, instead of a stdio command",
    )
    check.add_argument(
        "--allow-host",
        action="append",
        default=[],
        help="extra host the --url endpoint may use (repeatable)",
    )
    check.add_argument(
        "--header-env",
        action="append",
        default=[],
        metavar="HEADER=ENV_VAR",
        help="send a header whose value comes from the environment (repeatable)",
    )
    check.add_argument(
        "--dry-run",
        action="store_true",
        help="read tools/list and print the plan without calling any tool",
    )
    check.add_argument(
        "--timeout",
        type=float,
        help="stop issuing calls after this many seconds and report the rest as unverified",
    )
    _add_contract_options(check)
    _add_baseline_options(check)
    _add_suppression_options(check)
    _add_isolation_options(check)
    _add_report_options(check)
    check.add_argument("--protocol-version", default="2025-11-25")
    check.add_argument("--startup-timeout", type=float, default=60.0)
    check.add_argument(
        "--keep-sandbox",
        action="store_true",
        help="keep the temporary sandbox for inspection",
    )
    check.add_argument(
        "command",
        nargs="*",
        help="stdio command that starts the server; place it after ' -- '",
    )

    lint = commands.add_parser(
        "lint",
        help="check a server's annotations statically, without calling any tool",
    )
    lint.add_argument(
        "--manifest",
        help="lint the contracts in a manifest instead of starting a server",
    )
    lint.add_argument("--json", action="store_true", dest="as_json", help="print JSON")
    lint.add_argument(
        "--min-annotation-score",
        type=float,
        help="exit 2 when the annotation score is below this value (0-100)",
    )
    lint.add_argument("--inherit-env", nargs="+", default=["HOME"])
    lint.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="environment variable for the server (repeatable)",
    )
    lint.add_argument("--protocol-version", default="2025-11-25")
    lint.add_argument("--startup-timeout", type=float, default=60.0)
    lint.add_argument(
        "command",
        nargs="*",
        help="stdio command that starts the server; place it after ' -- '",
    )

    init = commands.add_parser(
        "init",
        help="scaffold a reviewable manifest from a live server",
    )
    init.add_argument(
        "--server",
        nargs="+",
        required=True,
        metavar="COMMAND",
        help="stdio command that starts the server",
    )
    init.add_argument("--out", default="safesink.mcp.json", help="manifest to write")
    init.add_argument(
        "--observer-root",
        default="./safesink-sandbox",
        help="sandbox directory the cases observe",
    )
    init.add_argument("--cwd", default=".")
    init.add_argument("--include-destructive", action="store_true")
    init.add_argument("--inherit-env", nargs="+", default=["HOME"])
    init.add_argument(
        "--env", action="append", default=[], metavar="KEY=VALUE",
        help="environment variable for the server (repeatable)",
    )
    init.add_argument("--protocol-version", default="2025-11-25")
    init.add_argument("--startup-timeout", type=float, default=60.0)

    sign = commands.add_parser(
        "sign", help="issue a detached certificate for a report (optionally Ed25519-signed)"
    )
    sign.add_argument("--report", required=True, help="JSON report to certify")
    sign.add_argument(
        "--out", default="safesink-certificate.json", help="certificate path"
    )
    sign.add_argument(
        "--key",
        help="32-byte hex Ed25519 seed; omit for a digest-only certificate",
    )
    sign.add_argument(
        "--keygen", action="store_true", help="print a new keypair and exit"
    )

    verify_cert = commands.add_parser(
        "verify-cert", help="verify a certificate against its report"
    )
    verify_cert.add_argument("certificate", help="certificate JSON path")
    verify_cert.add_argument("--report", required=True, help="JSON report path")
    verify_cert.add_argument(
        "--public-key", help="expected hex public key, overriding the embedded one"
    )
    verify_cert.add_argument(
        "--json", action="store_true", dest="as_json", help="print JSON"
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
    mcp.add_argument(
        "--min-score",
        type=float,
        help="fail when the documented trust score is below this value (0-100)",
    )
    _add_baseline_options(mcp)
    _add_suppression_options(mcp)
    _add_isolation_options(mcp)
    _add_report_options(mcp)
    mcp.add_argument("--verbose", action="store_true", help="print per-case verdicts")
    mcp.add_argument(
        "--capture-stderr",
        action="store_true",
        help="include a bounded, redacted server stderr excerpt in the report",
    )
    mcp.add_argument(
        "--timeout",
        type=float,
        help="stop issuing calls after this many seconds and report the rest as unverified",
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
    _add_contract_options(scan)
    scan.add_argument(
        "--dry-run",
        action="store_true",
        help="print the generated plan without calling any tool",
    )
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


def _below_min_score(report: dict, minimum: float | None) -> bool:
    if minimum is None:
        return False
    score = int(report.get("score", {}).get("value", 0))
    if score >= minimum:
        return False
    print(
        f"safesink: trust score {score} is below --min-score {minimum:g}",
        file=sys.stderr,
    )
    return True


def _exit_code(report: dict) -> int:
    """0 = verified, 3 = nothing failed but nothing could be verified, 2 = finding."""
    return {"pass": 0, "inconclusive": 3}.get(str(report.get("verdict")), 2)


def _print_plan(plan: list[dict]) -> None:
    """Readable review of a dry run: tool, arguments, observers, declarations."""
    print("\nPLAN (no tool is called)", file=sys.stderr)
    header = f"{'TOOL':<28} {'KIND':<12} {'OBSERVERS':<22} DECLARED / ARGUMENTS"
    print(header, file=sys.stderr)
    print("-" * len(header), file=sys.stderr)
    for item in plan:
        declared = ", ".join(
            f"{key}={value}" for key, value in sorted(item.get("contract", {}).items())
        ) or "nothing declared"
        observers = ",".join(item.get("observers", [])) or "none"
        print(
            f"{item['tool']:<28} {item['kind']:<12} {observers:<22} {declared}",
            file=sys.stderr,
        )
        print(
            f"  arguments: {json.dumps(item.get('arguments', {}), sort_keys=True)}",
            file=sys.stderr,
        )


def _print_lint(result: dict) -> None:
    print(
        f"annotation score {result['score']['value']}/{result['score']['max']} "
        f"over {result['toolsAdvertised']} tools",
        file=sys.stderr,
    )
    for finding in result["findings"]:
        print(
            f"  {finding['tool']}: {finding['code']} - {finding['message']}",
            file=sys.stderr,
        )
    if not result["findings"]:
        print("  no annotation problems found", file=sys.stderr)


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
        if bool(arguments.url) == bool(arguments.command):
            _die(
                parser,
                "mcp-check needs exactly one of --url or a stdio command",
                hint="for example --url https://example.test/mcp, or -- npx -y server",
            )
        server_env = _parse_env(parser, arguments.env)
        header_env = _parse_env(parser, arguments.header_env)
        allowed_hosts = tuple(arguments.allow_host) or None
        sqlite_queries = _parse_sqlite_queries(parser, arguments.sqlite_query)
        postgres_queries = _parse_sqlite_queries(parser, arguments.pgsql_query)
        mysql_queries = _parse_sqlite_queries(parser, arguments.mysql_query)
        database_observers = sqlite_queries or postgres_queries or mysql_queries
        contracts = _parse_contracts(parser, arguments.contract)
        contract_all = (
            _parse_hint_spec(parser, arguments.contract_all)
            if arguments.contract_all
            else None
        )
        baseline, baseline_path = _load_baseline(
            parser, arguments.baseline, arguments.new_only
        )
        suppressions, suppressions_path = _load_suppressions(
            parser, arguments.suppress
        )
        try:
            tools, server_info = list_server_tools(
                arguments.command,
                cwd=Path.cwd(),
                inherit_env=arguments.inherit_env,
                env=server_env,
                protocol_version=arguments.protocol_version,
                startup_timeout_seconds=arguments.startup_timeout,
                url=arguments.url,
                headers_from_env=header_env,
                allowed_hosts=allowed_hosts,
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
                and database_observers
                and not arguments.sandbox_observer
                and not arguments.allow_unobserved_mutations
            ):
                databases = ", ".join(
                    [
                        str(Path(database).expanduser().resolve())
                        for database, _ in sqlite_queries
                    ]
                    + [f"${name} (postgres)" for name, _ in postgres_queries]
                    + [f"${name} (mysql)" for name, _ in mysql_queries]
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
                dry_run=arguments.dry_run,
                contracts=contracts,
                contract_all=contract_all,
                url=arguments.url,
                headers_from_env=header_env,
                allowed_hosts=allowed_hosts,
                postgres_queries=postgres_queries,
                mysql_queries=mysql_queries,
            )
            write_manifest(manifest, manifest_path)
            if arguments.dry_run:
                # There is no report to write, so --out holds the reviewable
                # manifest (with the plan) a user can then verify later.
                written = write_manifest(manifest, arguments.out)
                plan = manifest["generated"]["plan"]
                print(
                    json.dumps(
                        {
                            "mode": "dry-run",
                            "server": server_info.get("name"),
                            "version": server_info.get("version"),
                            "toolsAdvertised": len(tools),
                            "casesGenerated": len(manifest["cases"]),
                            "observers": [item["id"] for item in manifest["observers"]],
                            "plan": plan,
                            "manifest": str(written),
                        },
                        indent=2,
                    )
                )
                _print_plan(plan)
                return 0
            report = verify_manifest(
                manifest_path,
                capture_stderr=arguments.capture_stderr,
                timeout_seconds=arguments.timeout,
                baseline=baseline,
                baseline_path=baseline_path,
                new_only=arguments.new_only,
                suppressions=suppressions,
                suppression_path=suppressions_path,
                fail_on_suppressed=arguments.fail_on_suppressed,
                restart_per_case=arguments.restart_per_case or None,
                case_order=arguments.case_order,
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
        _write_markdown(parser, report, arguments)
        print(
            json.dumps(
                {
                    "server": server_info.get("name"),
                    "version": server_info.get("version"),
                    "verdict": report["verdict"],
                    "cases": len(report["cases"]),
                    "toolCoverage": report["coverage"]["ratio"],
                    "observers": [item["kind"] for item in report.get("observers", [])],
                    "declarationSource": (
                        "manifest-contract"
                        if (contracts or contract_all)
                        else "mcp-tool-annotation"
                    ),
                    "inconclusive": sum(
                        1 for case in report["cases"] if case.get("inconclusive")
                    ),
                    "score": report["score"]["value"],
                    "certificateSha256": report["certificateSha256"],
                    "report": str(Path(arguments.out).resolve()),
                },
                indent=2,
            )
        )
        _print_case_verdicts(report)
        if _below_min_score(report, arguments.min_score):
            return 2
        return _exit_code(report)

    if arguments.cmd == "init":
        observer_root = Path(arguments.observer_root)
        observer_root.mkdir(parents=True, exist_ok=True)
        server_env = _parse_env(parser, arguments.env)
        try:
            tools, server_info = list_server_tools(
                arguments.server,
                cwd=Path(arguments.cwd).resolve(),
                inherit_env=arguments.inherit_env,
                env=server_env,
                protocol_version=arguments.protocol_version,
                startup_timeout_seconds=arguments.startup_timeout,
            )
            manifest = scaffold_manifest(
                tools,
                command=arguments.server,
                observer_root=str(observer_root),
                cwd=arguments.cwd,
                include_destructive=arguments.include_destructive,
                inherit_env=arguments.inherit_env,
                env=server_env,
                protocol_version=arguments.protocol_version,
            )
        except (ScanError, McpClientError) as error:
            _die(parser, str(error), hint="the server must speak MCP over stdio")
            return 2
        destination = write_manifest(manifest, arguments.out)
        # Validate before announcing success: a scaffold that cannot be loaded is
        # worse than no scaffold.
        try:
            load_manifest(destination)
        except ManifestError as error:
            _die(parser, f"the generated manifest is not valid: {error}")
            return 2
        print(
            json.dumps(
                {
                    "server": server_info.get("name"),
                    "version": server_info.get("version"),
                    "cases": len(manifest["cases"]),
                    "todo": manifest["generated"]["todo"],
                    "manifest": str(destination),
                    "next": (
                        "review the cases, fill every empty contract, then run "
                        f"safesink mcp-verify {destination}"
                    ),
                },
                indent=2,
            )
        )
        return 0

    if arguments.cmd == "sign":
        if arguments.keygen:
            seed, key = generate_key()
            print(
                json.dumps(
                    {
                        "privateKey": seed,
                        "publicKey": key,
                        "note": "store the private key offline; publish only the public key",
                    },
                    indent=2,
                )
            )
            return 0
        try:
            report = json.loads(Path(arguments.report).read_text(encoding="utf-8"))
        except FileNotFoundError:
            _die(parser, f"report not found: {arguments.report}")
            return 2
        except (OSError, json.JSONDecodeError) as error:
            _die(parser, f"could not read report: {error}")
            return 2
        try:
            certificate = build_certificate(report)
            if arguments.key:
                certificate = sign_certificate(certificate, arguments.key)
        except CertificateError as error:
            _die(parser, str(error), hint="generate one with: safesink sign --keygen")
            return 2
        destination = Path(arguments.out)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(certificate, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    "certificate": str(destination),
                    "signed": bool(arguments.key),
                    "publicKey": certificate.get("publicKey"),
                    "reportSha256": certificate["reportSha256"],
                },
                indent=2,
            )
        )
        return 0

    if arguments.cmd == "verify-cert":
        try:
            certificate = json.loads(
                Path(arguments.certificate).read_text(encoding="utf-8")
            )
            report = json.loads(Path(arguments.report).read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            _die(parser, f"file not found: {error.filename}")
            return 2
        except (OSError, json.JSONDecodeError) as error:
            _die(parser, f"could not read input: {error}")
            return 2
        result = verify_certificate(
            certificate, report, public_key_hex=arguments.public_key
        )
        if arguments.as_json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            state = "VALID" if result["valid"] else "INVALID"
            print(f"{state}: {result['reason']}")
        return 0 if result["valid"] else 2

    if arguments.cmd == "lint":
        if arguments.manifest:
            try:
                manifest, _ = load_manifest(arguments.manifest)
            except ManifestError as error:
                _die(parser, str(error), hint="see docs/MCP_CONFORMANCE.md#manifest")
                return 2
            except FileNotFoundError:
                _die(parser, f"manifest not found: {arguments.manifest}")
                return 2
            result = lint_manifest(manifest)
        else:
            if not arguments.command:
                _die(
                    parser,
                    "lint needs either --manifest FILE or a server command",
                    hint="safesink lint -- npx -y @modelcontextprotocol/server-filesystem .",
                )
                return 2
            server_env = _parse_env(parser, arguments.env)
            try:
                tools, server_info = list_server_tools(
                    arguments.command,
                    cwd=Path.cwd(),
                    inherit_env=arguments.inherit_env,
                    env=server_env,
                    protocol_version=arguments.protocol_version,
                    startup_timeout_seconds=arguments.startup_timeout,
                )
            except (ScanError, McpClientError) as error:
                _die(parser, str(error), hint="the server must speak MCP over stdio")
                return 2
            result = lint_tools(tools)
            result["server"] = server_info.get("name")
            result["version"] = server_info.get("version")
        if arguments.as_json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            _print_lint(result)
        minimum = arguments.min_annotation_score
        if minimum is not None and result["score"]["value"] < minimum:
            print(
                f"safesink: annotation score {result['score']['value']} is below "
                f"--min-annotation-score {minimum:g}",
                file=sys.stderr,
            )
            return 2
        return 0

    if arguments.cmd == "mcp-verify":
        baseline, baseline_path = _load_baseline(
            parser, arguments.baseline, arguments.new_only
        )
        suppressions, suppressions_path = _load_suppressions(
            parser, arguments.suppress
        )
        try:
            report = verify_manifest(
                arguments.manifest,
                capture_stderr=arguments.capture_stderr,
                timeout_seconds=arguments.timeout,
                baseline=baseline,
                baseline_path=baseline_path,
                new_only=arguments.new_only,
                suppressions=suppressions,
                suppression_path=suppressions_path,
                fail_on_suppressed=arguments.fail_on_suppressed,
                restart_per_case=arguments.restart_per_case or None,
                case_order=arguments.case_order,
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
        _write_markdown(parser, report, arguments)
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
        if _below_min_score(report, arguments.min_score):
            return 2
        return _exit_code(report)

    # mcp-scan
    observer_root = Path(arguments.observer_root)
    observer_root.mkdir(parents=True, exist_ok=True)
    server_env = _parse_env(parser, arguments.env)
    contracts = _parse_contracts(parser, arguments.contract)
    contract_all = (
        _parse_hint_spec(parser, arguments.contract_all)
        if arguments.contract_all
        else None
    )
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
            dry_run=arguments.dry_run,
            contracts=contracts,
            contract_all=contract_all,
        )
    except (ScanError, McpClientError) as error:
        _die(parser, str(error), hint="the server must speak MCP over stdio")
        return 2
    destination = write_manifest(manifest, arguments.out)
    summary: dict[str, object] = {
        "server": server_info.get("name"),
        "version": server_info.get("version"),
        "toolsAdvertised": len(tools),
        "casesGenerated": len(manifest["cases"]),
        "skipped": manifest["generated"]["skipped"],
        "manifest": str(destination),
    }
    if arguments.dry_run:
        summary["mode"] = "dry-run"
        summary["plan"] = manifest["generated"]["plan"]
    print(json.dumps(summary, indent=2))
    if arguments.dry_run:
        _print_plan(manifest["generated"]["plan"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
