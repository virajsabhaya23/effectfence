from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path

from .benchmark import run as benchmark
from .citation import citation
from .explore import explore, minimize_failure
from .io import load, save
from .live import LiveConfig, default_run_id, run_live_kafka_postgres
from .mcp_reports import write_json_report, write_junit_report, write_sarif_report
from .mcp_verifier import ManifestError, verify_manifest
from .reports import junit
from .simulator import verify_scenario


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="effectfence", description="Crash/retry and MCP side-effect verifier"
    )
    commands = parser.add_subparsers(dest="cmd", required=True)

    verify = commands.add_parser("verify", help="verify a deterministic scenario")
    verify.add_argument("scenario")
    verify.add_argument("--out")
    verify.add_argument("--junit")
    verify.add_argument("--minimized")

    explore_command = commands.add_parser("explore")
    explore_command.add_argument("scenario")
    explore_command.add_argument("--out", required=True)

    benchmark_command = commands.add_parser("benchmark")
    benchmark_command.add_argument("corpus")
    benchmark_command.add_argument("--out", required=True)

    mcp = commands.add_parser(
        "mcp-verify", help="verify MCP tool annotations against observed effects"
    )
    mcp.add_argument("manifest", help="path to an effectfence.mcp.v1 manifest")
    mcp.add_argument("--out", default="effectfence-mcp-report.json")
    mcp.add_argument("--junit")
    mcp.add_argument("--sarif")

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
        scenario = load(arguments.scenario)
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
        for violation in report["violations"]:
            print(violation["kind"], violation)
        print("certificate_sha256=" + report["certificate_sha256"])
        return 0 if report["safe"] else 2

    if arguments.cmd == "explore":
        rows = explore(load(arguments.scenario))
        save({"cases": rows}, arguments.out)
        unsafe = sum(1 for row in rows if not row["result"]["safe"])
        print(f"explored={len(rows)} unsafe={unsafe}")
        return 0

    if arguments.cmd == "benchmark":
        report = benchmark(arguments.corpus)
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
            parser.error(str(error))
        write_json_report(report, arguments.out)
        if arguments.junit:
            write_junit_report(report, arguments.junit)
        if arguments.sarif:
            write_sarif_report(report, arguments.sarif)
        print(
            json.dumps(
                {
                    "verdict": report["verdict"],
                    "cases": len(report["cases"]),
                    "toolCoverage": report["coverage"]["ratio"],
                    "certificateSha256": report["certificateSha256"],
                    "report": str(Path(arguments.out).resolve()),
                },
                indent=2,
            )
        )
        return 0 if report["verdict"] == "pass" else 2

    if arguments.cmd == "citation":
        print(citation(arguments.format))
        return 0

    if not arguments.postgres_dsn:
        parser.error(
            "live-kafka-postgres requires --postgres-dsn or EFFECTFENCE_POSTGRES_DSN"
        )
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
