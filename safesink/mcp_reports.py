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
    failures = sum(1 for case in cases if case.get("violations"))
    skipped = sum(
        1 for case in cases if not case.get("violations") and case.get("inconclusive")
    )
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
            "name": "safesink-mcp-conformance",
            "tests": str(total_tests),
            "failures": str(failures),
            "errors": "0",
            "skipped": str(skipped),
            "time": f"{suite_time:.3f}",
        },
    )
    # surface the certificate as a property so CI can link JSON <-> JUnit
    props = ET.SubElement(suite, "properties")
    cert = report.get("certificateSha256") or report.get("certificate_sha256") or ""
    if cert:
        ET.SubElement(props, "property", {"name": "certificateSha256", "value": str(cert)})
    baseline = report.get("baseline")
    if isinstance(baseline, dict):
        # A suppressed finding is still visible in the CI suite, even though it
        # no longer fails the run.
        ET.SubElement(
            props,
            "property",
            {
                "name": "baselineSuppressedCount",
                "value": str(baseline.get("suppressedCount", 0)),
            },
        )
        for suppressed in baseline.get("suppressed", []):
            ET.SubElement(
                props,
                "property",
                {
                    "name": "baselineSuppressed",
                    "value": f"{suppressed.get('caseId')}:{suppressed.get('code')}",
                },
            )
    for case in cases:
        node = ET.SubElement(
            suite,
            "testcase",
            {
                "classname": "safesink.mcp",
                "name": str(case.get("id", "unknown")),
                "time": f"{float(case.get('durationMs', 0)) / 1000:.3f}",
            },
        )
        if case.get("violations"):
            failure = ET.SubElement(
                node,
                "failure",
                {"message": _failure_summary(case.get("violations", []))},
            )
            failure.text = json.dumps(case.get("violations", []), indent=2)
        elif case.get("inconclusive"):
            skipping = ET.SubElement(
                node,
                "skipped",
                {
                    "message": ", ".join(
                        str(item.get("code", "inconclusive"))
                        for item in case["inconclusive"]
                    )[:500]
                },
            )
            skipping.text = json.dumps(case["inconclusive"], indent=2)
    infrastructure = []
    if report.get("fatalError"):
        infrastructure.append(
            {"code": "FATAL_ERROR", "message": _fatal_text(report)}
        )
    infrastructure.extend(report.get("policyViolations", []))
    if infrastructure:
        node = ET.SubElement(
            suite,
            "testcase",
            {"classname": "safesink.mcp", "name": "manifest-policy", "time": "0"},
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
                "message": {"text": _fatal_text(report)},
            }
        )
    # keep SARIF minimal but valid – GitHub code scanning expects rules even if results is empty
    rules = [
        {
            "id": rule_id,
            "name": rule_id,
            "shortDescription": {"text": _rule_title(rule_id)},
            "helpUri": "https://github.com/virajsabhaya23/safesink/blob/main/docs/MCP_CONFORMANCE.md",
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
                        "name": "SafeSink MCP Conformance",
                        "informationUri": "https://github.com/virajsabhaya23/safesink",
                        "version": str(
                            report.get("runtime", {}).get("version") or "unknown"
                        ),
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


BADGE_COLORS = {
    "pass": "#4c1",
    "inconclusive": "#dfb317",
    "fail": "#e05d44",
}


def render_badge_svg(
    verdict: str, score: object | None = None, *, label: str = "safesink"
) -> str:
    """A Shields-style SVG so an author can embed their own result.

    It deliberately states the verdict and the score, not a certification: the
    color is the same three-state vocabulary as the report.
    """
    import html

    value = str(verdict)
    if score is not None:
        value = f"{verdict} {score}"
    label_text = html.escape(label)
    value_text = html.escape(value)
    color = BADGE_COLORS.get(str(verdict), "#9f9f9f")
    label_width = 7 * len(label) + 12
    value_width = 7 * len(value) + 12
    total = label_width + value_width
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{total}" height="20" role="img" aria-label="{label_text}: {value_text}">
  <title>{label_text}: {value_text}</title>
  <rect width="{label_width}" height="20" fill="#555"/>
  <rect x="{label_width}" width="{value_width}" height="20" fill="{color}"/>
  <g fill="#fff" font-family="DejaVu Sans, Verdana, sans-serif" font-size="11">
    <text x="{label_width / 2}" y="14" text-anchor="middle">{label_text}</text>
    <text x="{label_width + value_width / 2}" y="14" text-anchor="middle">{value_text}</text>
  </g>
</svg>
"""


def render_leaderboard_html(data: dict[str, Any]) -> str:
    """A static page over `data/leaderboard.json`, with the honesty rules inline."""
    import html

    entries = data.get("entries", [])
    rows = []
    for entry in entries:
        rows.append(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td>"
            "<td>{}</td><td>{}</td><td>{}</td></tr>".format(
                html.escape(str(entry.get("server", ""))),
                html.escape(str(entry.get("version", ""))),
                html.escape(str(entry.get("tools", ""))),
                html.escape(str(entry.get("cases", ""))),
                html.escape(str(entry.get("findings", ""))),
                html.escape(str(entry.get("inconclusive", ""))),
                html.escape(str(entry.get("verdict", ""))),
                html.escape(str(entry.get("score", ""))),
            )
        )
    generated = html.escape(str(data.get("generatedAt") or "never"))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>SafeSink leaderboard</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:60rem;margin:2rem auto;padding:0 1rem}}
table{{border-collapse:collapse;width:100%}}
th,td{{text-align:left;padding:.4rem .6rem;border-bottom:1px solid #ddd}}
.pass{{color:#1a7f37}}.fail{{color:#b42318}}.inconclusive{{color:#8a6100}}
</style></head><body>
<h1>SafeSink leaderboard</h1>
<p>Generated {generated}. Every row is a pinned version scanned with
<code>safesink mcp-check</code>; the raw report is linked from the workflow run.</p>
<p><strong>Zero findings is the expected result for a trusted server.</strong>
Detection is proven by the deliberately dishonest fixture, not by this table. A
pass here is not a security certification.</p>
<table><thead><tr><th>Server</th><th>Version</th><th>Tools</th><th>Cases</th>
<th>Findings</th><th>Inconclusive</th><th>Verdict</th><th>Score</th></tr></thead>
<tbody>{''.join(rows) or '<tr><td colspan="8">No scans recorded yet.</td></tr>'}</tbody>
</table></body></html>
"""


def render_markdown_report(report: dict[str, Any]) -> str:
    """A reviewable summary for a pull request, where nobody opens artifacts."""
    verdict = str(report.get("verdict", "unknown"))
    banner = {
        "pass": "✅ PASS",
        "fail": "❌ FAIL",
        "inconclusive": "⚠️ INCONCLUSIVE",
    }.get(verdict, verdict.upper())
    score = report.get("score") or {}
    cases = report.get("cases", [])
    coverage = report.get("coverage", {})
    coverage_ratio = coverage.get("ratio")
    coverage_text = (
        f"{round(float(coverage_ratio) * 100)}%"
        if isinstance(coverage_ratio, (int, float))
        else "n/a"
    )
    findings = [
        (case, violation)
        for case in cases
        for violation in case.get("violations", [])
    ]
    unverified = [
        (case, item)
        for case in cases
        for item in case.get("inconclusive", [])
    ]
    lines = [
        f"## SafeSink: {banner}",
        "",
        f"**Score:** {score.get('value', 'n/a')}/100 · "
        f"**Cases:** {len(cases)} · "
        f"**Tool coverage:** {coverage_text} · "
        f"**Findings:** {len(findings)} · "
        f"**Inconclusive:** {len(unverified)}",
        "",
    ]
    if report.get("fatalError"):
        lines.append(f"> Fatal error: {_fatal_text(report)}")
        lines.append("")
    if findings:
        lines.extend(
            [
                "| Tool | Case | Finding | Declaration | Observed |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for case, violation in findings:
            lines.append(
                "| {} | {} | `{}` | {} | {} |".format(
                    _cell(case.get("tool")),
                    _cell(case.get("id")),
                    _cell(violation.get("code")),
                    _cell(_declaration_text(case)),
                    _cell(violation.get("message")),
                )
            )
        lines.append("")
    else:
        lines.extend(["No declaration was contradicted.", ""])
    if unverified:
        lines.extend(
            [
                "| Tool | Case | Unverified because |",
                "| --- | --- | --- |",
            ]
        )
        for case, item in unverified:
            lines.append(
                "| {} | {} | `{}`: {} |".format(
                    _cell(case.get("tool")),
                    _cell(case.get("id")),
                    _cell(item.get("code")),
                    _cell(item.get("message")),
                )
            )
        lines.append("")
    observers = report.get("observers", [])
    if observers:
        boundary = ", ".join(
            f"{observer.get('id')} ({observer.get('kind')}, {observer.get('world')})"
            for observer in observers
        )
        lines.extend([f"**Observed boundary:** {boundary}", ""])
    baseline = report.get("baseline")
    if isinstance(baseline, dict):
        lines.append(
            f"**Baseline:** {baseline.get('suppressedCount', 0)} accepted, "
            f"{len(baseline.get('regressions', []))} new, "
            f"{baseline.get('fixedCount', 0)} fixed"
        )
        lines.append("")
    suppressions = report.get("suppressions")
    if isinstance(suppressions, dict) and suppressions.get("suppressedCount"):
        lines.append(
            f"**Suppressions:** {suppressions['suppressedCount']} accepted with a reason"
        )
        lines.append("")
    if report.get("certificateSha256"):
        lines.append(f"**Certificate:** `{report['certificateSha256']}`")
        lines.append("")
    lines.append(
        "A pass means no contradiction was observed inside the declared boundary; "
        "it is not a security certification."
    )
    return "\n".join(lines)


def write_markdown_report(report: dict[str, Any], path: str | Path) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_markdown_report(report) + "\n", encoding="utf-8")


def _cell(value: Any) -> str:
    text = str(value if value is not None else "").replace("|", "\\|")
    return text.replace("\n", " ")[:400]


def _declaration_text(case: dict[str, Any]) -> str:
    declarations = case.get("declarations") or {}
    parts = [
        f"{name}={value.get('value')}"
        for name, value in sorted(declarations.items())
        if isinstance(value, dict)
    ]
    return ", ".join(parts) or "nothing declared"


def _fatal_text(report: dict[str, Any]) -> str:
    fatal = report.get("fatalError")
    if not isinstance(fatal, dict):
        return str(fatal)
    parts = [str(fatal.get("message", "fatal error"))]
    if fatal.get("phase"):
        parts.append(f"phase={fatal['phase']}")
    if fatal.get("case"):
        parts.append(f"case={fatal['case']}")
    return "; ".join(parts)


def _failure_summary(violations: list[dict[str, Any]]) -> str:
    if not violations:
        return "SafeSink conformance failure"
    return ", ".join(str(item.get("code", "failure")) for item in violations)[:500]


def _rule_title(rule_id: str) -> str:
    return rule_id.replace("_", " ").title()
