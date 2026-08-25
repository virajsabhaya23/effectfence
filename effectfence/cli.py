from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from .benchmark import run as benchmark
from .citation import citation
from .explore import explore, minimize_failure
from .io import load, save
from .live import LiveConfig, default_run_id, run_live_kafka_postgres
from .mcp_reports import write_json_report, write_junit_report, write_sarif_report
from .mcp_scan import ScanError, build_manifest, list_server_tools, write_manifest
from .mcp_verifier import ManifestError, verify_manifest
from .reports import junit
from .simulator import verify_scenario


def _die(parser: argparse.ArgumentParser, message: str, *, hint: str | None = None) -> None:
    # argparse.error prints usage to stderr and exits 2 – we want a shorter,
    # more actionable line for manifest/io failures.
    text = f"{parser.prog}: error: {message}"
    if hint:
        text += f"\n  hint: {hint}"
    print(text, file=sys.stderr)
    parser.exit(2)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="effectfence", description="Crash/retry and MCP side-effect verifier"
    )
    commands = parser.add_subparsers(dest="cmd", required=True)

    verify = commands.add_parser("verify", help="verify a deterministic scenario")
    verify.add_argument("scenario", help="path to scenario JSON")
    verify.add_argument("--out", help="write JSON report to this path")
    verify.add_argument("--junit", help="write JUnit XML to this path")
    verify.add_argument("--minimized", help="write minimized failing scenario when unsafe")
    verify.add_argument("--verbose", action="store_true", help="print trace on failure")

    explore_command = commands.add_parser("explore", help="explore adverse schedules")
    explore_command.add_argument("scenario", help="path to scenario JSON")
    explore_command.add_argument("--out", required=True, help="write exploration JSON")
    explore_command.add_argument("--verbose", action="store_true")

    benchmark_command = commands.add_parser("benchmark", help="run 30-case benchmark")
    benchmark_command.add_argument("corpus", help="path to corpus.json")
    benchmark_command.add_argument("--out", required=True, help="write benchmark results")

    mcp = commands.add_parser(
        "mcp-verify", help="verify MCP tool annotations against observed effects"
    )
    mcp.add_argument("manifest", help="path to an effectfence.mcp.v1 manifest")
    mcp.add_argument("--out", default="effectfence-mcp-report.json", help="JSON report path")
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

    live = commands.add_parser(
        "live-kafka-postgres", help="run the real Kafka/PostgreSQL SIGKILL proof"
    )
    live.add_argument(
        "--bootstrap-servers",
        default=os.environ.get("EFFECTFENCE_KAFKA_BOOTSTRAP", "127.0.0.1:9092"),
    )
    live.add_argument(
        "--postgres-dsn",
        default=os.environ.get("EFFECTFENCE_POSTGRES_DSN"),
        help="PostgreSQL DSN; defaults to EFFECTFENCE_POSTGRES_DSN",
    )
    live.add_argument("--strategy", choices=("effectfence", "naive"), default="effectfence")
    live.add_argument("--expect", choices=("safe", "unsafe"), default="safe")
    live.add_argument("--run-id")
    live.add_argument("--message-id")
    live.add_argument("--topic")
    live.add_argument("--group-id")
    live.add_argument("--artifact-dir", default="out/live")
    live.add_argument("--timeout", type=float, default=90.0)
    live.add_argument("--broker-version-label", default="external/unknown")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.cmd == "verify":
        try:
            scenario = load(arguments.scenario)
        except FileNotFoundError:
            _die(parser, f"scenario not found: {arguments.scenario}", hint="check path and try again")
            return 2
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            _die(parser, f"could not load scenario: {exc}")
            return 2
        report = verify_scenario(scenario)
        if arguments.out:
            save(report, arguments.out)
        if arguments.junit:
            junit(report, arguments.junit)
        if arguments.minimized and not report["safe"]:
            save(asdict(minimize_failure(scenario)), arguments.minimized)
        print(
            ("SAFE" if report["safe"] else "UNSAFE")
            + f" {scenario.id} strategy={scenario.strategy} "
            + f'effects={report["accepted_effects"]}'
        )
        if arguments.verbose or not report["safe"]:
            for violation in report["violations"]:
                print(f"  - {violation['kind']}: {violation.get('detail','')}", file=sys.stderr if report["safe"] else sys.stdout)
        print("certificate_sha256=" + report["certificate_sha256"])
        return 0 if report["safe"] else 2

    if arguments.cmd == "explore":
        try:
            scenario = load(arguments.scenario)
        except FileNotFoundError:
            _die(parser, f"scenario not found: {arguments.scenario}")
            return 2
        except (OSError, ValueError) as exc:
            _die(parser, f"could not load scenario: {exc}")
            return 2
        rows = explore(scenario)
        save({"cases": rows}, arguments.out)
        unsafe = sum(1 for row in rows if not row["result"]["safe"])
        print(f"explored={len(rows)} unsafe={unsafe} -> {arguments.out}")
        if arguments.verbose:
            for row in rows:
                if not row["result"]["safe"]:
                    print(f"  unsafe: {row['scenario']['id']} {row['result']['violations']}")
        return 0

    if arguments.cmd == "benchmark":
        try:
            report = benchmark(arguments.corpus)
        except FileNotFoundError:
            _die(parser, f"corpus not found: {arguments.corpus}")
            return 2
        except (OSError, ValueError) as exc:
            _die(parser, f"could not load corpus: {exc}")
            return 2
        save(report, arguments.out)
        print(
            json.dumps(
                {
                    "cases": report["cases"],
                    "schedule_verifier": report["schedule_verifier"],
                    "ordinary_happy_path": report["ordinary_happy_path"],
                    "by_strategy": report["by_strategy"],
                },
                indent=2,
            )
        )
        return 0

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

    if arguments.cmd == "citation":
        print(citation(arguments.format))
        return 0

    if not arguments.postgres_dsn:
        _die(parser, "live-kafka-postgres requires --postgres-dsn or EFFECTFENCE_POSTGRES_DSN", hint="export EFFECTFENCE_POSTGRES_DSN or pass --postgres-dsn")
        return 2
    if arguments.timeout is not None and arguments.timeout <= 0:
        _die(parser, "--timeout must be > 0")
        return 2
    run_id = arguments.run_id or default_run_id()
    config = LiveConfig(
        bootstrap_servers=arguments.bootstrap_servers,
        postgres_dsn=arguments.postgres_dsn,
        topic=arguments.topic or f"effectfence-{run_id}",
        group_id=arguments.group_id or f"effectfence-{run_id}",
        run_id=run_id,
        message_id=arguments.message_id or f"message-{run_id}",
        strategy=arguments.strategy,
        expected_outcome=arguments.expect,
        artifact_dir=Path(arguments.artifact_dir) / run_id,
        timeout_seconds=arguments.timeout,
        broker_version_label=arguments.broker_version_label,
    )
    report = run_live_kafka_postgres(config)
    print(
        json.dumps(
            {
                "run_id": report["run_id"],
                "observed_outcome": report["observed_outcome"],
                "expectation_met": report["expectation_met"],
                "attempts": report["sink"]["attempts"],
                "accepted_effects": report["sink"]["accepted_effects"],
                "evidence_sha256": report["evidence_sha256"],
                "report": str(config.artifact_dir / "report.json"),
            },
            indent=2,
        )
    )
    return 0 if report["expectation_met"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
