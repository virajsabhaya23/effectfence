from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


def write_json_report(report: dict[str, Any], path: str | Path) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def write_junit_report(report: dict[str, Any], path: str | Path) -> None:
    cases = report.get("cases", [])
    failures = sum(1 for case in cases if not case.get("passed"))
    has_policy_failure = bool(report.get("fatalError") or report.get("policyViolations"))
    if has_policy_failure:
        failures += 1
    total_tests = len(cases) + (1 if has_policy_failure else 0)
    # GitHub/Jenkins expect time in seconds; report stores ms
    try:
        suite_time = float(report.get("durationMs", 0)) / 1000.0
    except Exception:
        suite_time = 0.0
    suite = ET.Element(
        "testsuite",
        {
            "name": "effectfence-mcp-conformance",
            "tests": str(total_tests),
            "failures": str(failures),
            "errors": "0",
            "time": f"{suite_time:.3f}",
        },
    )
    # surface the certificate as a property so CI can link JSON <-> JUnit
    props = ET.SubElement(suite, "properties")
    cert = report.get("certificateSha256") or report.get("certificate_sha256") or ""
    if cert:
        ET.SubElement(props, "property", {"name": "certificateSha256", "value": str(cert)})
    for case in cases:
        node = ET.SubElement(
            suite,
            "testcase",
            {
                "classname": "effectfence.mcp",
                "name": str(case.get("id", "unknown")),
                "time": f"{float(case.get('durationMs', 0)) / 1000:.3f}",
            },
        )
        if not case.get("passed"):
            failure = ET.SubElement(
                node,
                "failure",
                {"message": _failure_summary(case.get("violations", []))},
            )
            failure.text = json.dumps(case.get("violations", []), indent=2)
    infrastructure = []
    if report.get("fatalError"):
        infrastructure.append(
            {"code": "FATAL_ERROR", "message": report["fatalError"]}
        )
    infrastructure.extend(report.get("policyViolations", []))
    if infrastructure:
        node = ET.SubElement(
            suite,
            "testcase",
            {"classname": "effectfence.mcp", "name": "manifest-policy", "time": "0"},
        )
        failure = ET.SubElement(
            node,
            "failure",
            {"message": _failure_summary(infrastructure)},
        )
        failure.text = json.dumps(infrastructure, indent=2)
    tree = ET.ElementTree(suite)
    ET.indent(tree, space="  ")
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tree.write(destination, encoding="utf-8", xml_declaration=True)


def write_sarif_report(report: dict[str, Any], path: str | Path) -> None:
    results: list[dict[str, Any]] = []
    rule_ids: set[str] = set()
    for case in report.get("cases", []):
        case_id = str(case.get("id", "unknown"))
        tool = str(case.get("tool", "unknown"))
        for violation in case.get("violations", []):
            rule_id = str(violation.get("code", "CONFORMANCE_FAILURE"))
            rule_ids.add(rule_id)
            # keep message short – SARIF viewers truncate long text
            msg = violation.get("message") or violation.get("detail") or rule_id
            results.append(
                {
                    "ruleId": rule_id,
                    "level": "error",
                    "message": {"text": f"{case_id} ({tool}): {msg}"},
                    "properties": {"caseId": case_id, "tool": tool},
                }
            )
        for item in case.get("inconclusive", []):
            rule_id = str(item.get("code", "INCONCLUSIVE"))
            rule_ids.add(rule_id)
            msg = item.get("message") or item.get("detail") or rule_id
            results.append(
                {
                    "ruleId": rule_id,
                    "level": "warning",
                    "message": {"text": f"{case_id} ({tool}): {msg}"},
                    "properties": {"caseId": case_id, "tool": tool},
                }
            )
    for violation in report.get("policyViolations", []):
        rule_id = str(violation.get("code", "POLICY_FAILURE"))
        rule_ids.add(rule_id)
        results.append(
            {
                "ruleId": rule_id,
                "level": "error",
                "message": {"text": str(violation.get("message", rule_id))},
            }
        )
    if report.get("fatalError"):
        rule_ids.add("FATAL_ERROR")
        results.append(
            {
                "ruleId": "FATAL_ERROR",
                "level": "error",
                "message": {"text": str(report["fatalError"])},
            }
        )
    # keep SARIF minimal but valid – GitHub code scanning expects rules even if results is empty
    rules = [
        {
            "id": rule_id,
            "name": rule_id,
            "shortDescription": {"text": _rule_title(rule_id)},
            "helpUri": "https://github.com/virajsabhaya23/effectfence/blob/main/docs/MCP_CONFORMANCE.md",
        }
        for rule_id in sorted(rule_ids)
    ]
    sarif = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "EffectFence MCP Conformance",
                        "informationUri": "https://github.com/virajsabhaya23/effectfence",
                        "version": str(report.get("effectfenceVersion", "0.2.0")),
                        "rules": rules,
                    }
                },
                "results": results,
            }
        ],
    }
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(sarif, indent=2) + "\n", encoding="utf-8")


def _failure_summary(violations: list[dict[str, Any]]) -> str:
    if not violations:
        return "EffectFence conformance failure"
    return ", ".join(str(item.get("code", "failure")) for item in violations)[:500]


def _rule_title(rule_id: str) -> str:
    return rule_id.replace("_", " ").title()
