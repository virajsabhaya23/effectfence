from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from .http_transport import McpHttpClient
from .mcp_client import McpClientError, McpStdioClient, restricted_environment
from .observers import (
    Delta,
    FilesystemObserver,
    HttpJsonObserver,
    Observer,
    ObserverError,
    Snapshot,
    SqliteQueryObserver,
    canonical_digest,
)


REPORT_SCHEMA = "effectfence.mcp.report.v1"
MANIFEST_SCHEMA = "effectfence.mcp.v1"

# The score is part of the contract: badges, leaderboards and agent gates all
# consume one number, so the weights are named here and the formula is versioned.
# A lie about a claim is the worst finding, an error mismatch is next, an
# unverifiable case is never free, and an unobserved tool is a coverage gap.
SCORE_VERSION = "1"
SCORE_VIOLATION_WEIGHTS = {
    "READ_ONLY_HINT_MISMATCH": 25,
    "IDEMPOTENT_HINT_MISMATCH": 25,
    "NON_DESTRUCTIVE_HINT_MISMATCH": 15,
    "CLOSED_WORLD_HINT_MISMATCH": 15,
    "TOOL_ERROR_EXPECTATION_MISMATCH": 10,
    "RETRY_TOOL_ERROR_EXPECTATION_MISMATCH": 10,
    "AMBIGUOUS_RESULT_DUPLICATE_ON_RETRY": 20,
}
SCORE_DEFAULT_VIOLATION_WEIGHT = 10
SCORE_INCONCLUSIVE_WEIGHT = 10
SCORE_COVERAGE_WEIGHT = 20


class ManifestError(ValueError):
    pass


def load_manifest(path: str | Path) -> tuple[dict[str, Any], Path]:
    manifest_path = Path(path).resolve()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ManifestError(f"could not read manifest: {error}") from error
    except json.JSONDecodeError as error:
        raise ManifestError(f"manifest is invalid JSON: {error}") from error
    if not isinstance(manifest, dict):
        raise ManifestError("manifest root must be an object")
    _validate_manifest(manifest)
    return manifest, manifest_path


def verify_manifest(
    path: str | Path,
    *,
    capture_stderr: bool = False,
    timeout_seconds: float | None = None,
    baseline: dict[str, Any] | None = None,
    baseline_path: str | Path | None = None,
    new_only: bool = False,
    suppressions: dict[str, Any] | None = None,
    suppression_path: str | Path | None = None,
    fail_on_suppressed: bool = False,
    restart_per_case: bool | None = None,
    case_order: str | None = None,
) -> dict[str, Any]:
    # Imported lazily: this module is imported while ``safesink/__init__`` runs,
    # so the package version does not exist yet at module-import time.
    from . import __version__ as package_version

    manifest, manifest_path = load_manifest(path)
    base = manifest_path.parent
    server = manifest["server"]
    server_cwd = _resolve_path(base, server.get("cwd", "."))
    environment = restricted_environment(
        _string_list(server.get("inheritEnv", []), "server.inheritEnv"),
        _string_map(server.get("env", {}), "server.env"),
    )
    try:
        observers = _build_observers(manifest.get("observers", []), base)
    except ObserverError as error:
        raise ManifestError(str(error)) from error
    # The report has to say which boundary was watched: a pass over rows says
    # nothing about files, and vice versa.
    observer_summary = [
        {
            "id": item["id"],
            "kind": item["kind"],
            "world": item.get(
                "world", "open" if item["kind"] == "http-json" else "closed"
            ),
            "sensitive": bool(item.get("sensitive", True)),
        }
        for item in manifest.get("observers", [])
    ]
    budget = timeout_seconds
    if budget is None:
        declared_budget = manifest.get("policy", {}).get("runTimeoutSeconds")
        budget = float(declared_budget) if declared_budget is not None else None
    if budget is not None and budget <= 0:
        raise ManifestError("run timeout must be positive")
    restart = (
        restart_per_case
        if restart_per_case is not None
        else bool(server.get("restartPerCase", False))
    )
    ordering = case_order or str(server.get("caseOrder", "file"))
    ordered_cases = _order_cases(manifest, ordering)
    started = time.time()
    budget_exhausted = False
    results: list[dict[str, Any]] = []
    # A fatal error has to say where it happened: a 40-case scan that dies on
    # case 31 should not read like one that never started.
    fatal_error: dict[str, Any] | None = None
    phase = "startup"
    current_case: str | None = None
    tools: list[dict[str, Any]] = []
    client: McpStdioClient | None = None
    restarts = 0
    try:
        client = _new_client(server, server_cwd, environment)
        client.start()
        phase = "tools/list"
        tools = client.list_tools(max_pages=int(server.get("maxListPages", 100)))
        indexed_tools = {tool["name"]: tool for tool in tools}
        phase = "case"
        server_instance = 0
        for index, case in enumerate(ordered_cases):
            current_case = case["id"]
            # One hanging tool must not stall a CI gate for timeout x cases. When
            # the budget is spent, the remaining cases are reported as unverified
            # rather than silently dropped.
            if budget is not None and time.time() - started > budget:
                budget_exhausted = True
                results.extend(
                    _unbudgeted_case(remaining, server_instance)
                    for remaining in ordered_cases[index:]
                )
                break
            if restart and index > 0:
                # A stateful server carries locks, caches and sessions into the
                # next case; a fresh process is the only way to attribute a delta.
                client.close()
                restarts += 1
                server_instance += 1
                client = _new_client(server, server_cwd, environment)
                client.start()
            result = _run_case(
                case,
                client=client,
                tool=indexed_tools.get(case["tool"]),
                observers=observers,
                base=base,
                server_environment=environment,
            )
            result["serverInstance"] = server_instance
            results.append(result)
    except (McpClientError, ObserverError, OSError, ValueError) as error:
        fatal_error = {
            "message": _safe_error(error),
            "phase": phase,
            "case": current_case,
        }
    finally:
        stderr_present = bool(client and client.stderr_tail)
        want_stderr = capture_stderr or bool(server.get("captureStderr", False))
        server_metadata = {
            "name": client.server_info.get("name") if client else None,
            "version": client.server_info.get("version") if client else None,
            "requested_protocol_version": server.get("protocolVersion", "2025-11-25"),
            "negotiated_protocol_version": (
                client.negotiated_protocol_version if client else None
            ),
            "stderr_present": stderr_present,
            "restarts": restarts,
            "restartPerCase": restart,
            "caseOrder": ordering,
        }
        if want_stderr and client and client.stderr_tail:
            server_metadata["stderrExcerpt"] = _stderr_excerpt(client.stderr_tail)
        if client:
            client.close()

    suppression_info: dict[str, Any] | None = None
    if suppressions is not None:
        suppression_info = _apply_suppressions(results, suppressions)
        suppression_info["path"] = (
            str(suppression_path) if suppression_path else None
        )
        if fail_on_suppressed and suppression_info["suppressedCount"]:
            suppression_info["failOnSuppressed"] = True
    baseline_info: dict[str, Any] | None = None
    if new_only:
        if baseline is None:
            raise ManifestError("--new-only requires a baseline report")
        baseline_info = _apply_baseline(results, baseline)
        baseline_info["path"] = str(baseline_path) if baseline_path else None
        if baseline_path:
            try:
                baseline_info["sha256"] = hashlib.sha256(
                    Path(baseline_path).read_bytes()
                ).hexdigest()
            except OSError:
                baseline_info["sha256"] = None

    listed_names = {tool["name"] for tool in tools}
    tested_names = {case["tool"] for case in manifest["cases"]}
    coverage = sorted(listed_names & tested_names)
    missing = sorted(listed_names - tested_names)
    unknown = sorted(tested_names - listed_names)
    required_coverage = float(manifest.get("policy", {}).get("minimumToolCoverage", 0))
    coverage_ratio = len(coverage) / len(listed_names) if listed_names else 0.0
    policy_violations: list[dict[str, Any]] = []
    if coverage_ratio < required_coverage:
        policy_violations.append(
            {
                "code": "TOOL_COVERAGE_BELOW_MINIMUM",
                "message": (
                    f"tool coverage {coverage_ratio:.3f} is below required "
                    f"{required_coverage:.3f}"
                ),
            }
        )
    if unknown:
        policy_violations.append(
            {
                "code": "CASE_REFERENCES_UNKNOWN_TOOL",
                "message": "one or more cases reference tools not listed by the server",
            }
        )

    # Three outcomes, because "could not verify" is neither a pass nor a finding.
    if suppression_info and suppression_info.get("failOnSuppressed"):
        policy_violations.append(
            {
                "code": "SUPPRESSED_FINDINGS_NOT_ALLOWED",
                "message": "--fail-on-suppressed forbids accepting a finding, even with a reason",
            }
        )
    failed = (
        fatal_error is not None
        or bool(policy_violations)
        or not results
        or any(case["violations"] for case in results)
    )
    unverified = any(case["inconclusive"] for case in results)
    verdict = "fail" if failed else ("inconclusive" if unverified else "pass")
    report: dict[str, Any] = {
        "schemaVersion": REPORT_SCHEMA,
        "verdict": verdict,
        "generatedAt": _utc_timestamp(started),
        "durationMs": round((time.time() - started) * 1000),
        "manifest": {
            "path": manifest_path.name,
            "sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        },
        "runtime": {
            "version": package_version,
            "pythonVersion": platform.python_version(),
            "platform": platform.system(),
        },
        "server": server_metadata,
        "coverage": {
            "listedTools": len(listed_names),
            "testedTools": len(coverage),
            "ratio": round(coverage_ratio, 6),
            "tested": coverage,
            "untested": missing,
            "unknown": unknown,
        },
        "policyViolations": policy_violations,
        "fatalError": fatal_error,
        "runBudget": {
            "timeoutSeconds": budget,
            "exhausted": budget_exhausted,
        },
        "baseline": baseline_info,
        "suppressions": suppression_info,
        "observers": observer_summary,
        "cases": results,
        "limitations": _limitations(observer_summary),
    }
    report["score"] = _score(
        results, coverage_ratio, required_coverage, policy_violations, fatal_error
    )
    report["certificateSha256"] = canonical_digest(report)
    return report


def _score(
    results: list[dict[str, Any]],
    coverage_ratio: float,
    required_coverage: float,
    policy_violations: list[dict[str, Any]],
    fatal_error: dict[str, Any] | None,
) -> dict[str, Any]:
    """Derive one comparable number from evidence, never from optimism."""
    violation_counts: dict[str, int] = {}
    for case in results:
        for violation in case.get("violations", []):
            code = str(violation.get("code", "UNKNOWN"))
            violation_counts[code] = violation_counts.get(code, 0) + 1
        for violation in policy_violations:
            code = str(violation.get("code", "UNKNOWN"))
            violation_counts[code] = violation_counts.get(code, 0) + 1
    inconclusive = sum(1 for case in results if case.get("inconclusive"))
    violation_points = sum(
        SCORE_VIOLATION_WEIGHTS.get(code, SCORE_DEFAULT_VIOLATION_WEIGHT) * count
        for code, count in violation_counts.items()
    )
    inconclusive_points = SCORE_INCONCLUSIVE_WEIGHT * inconclusive
    # Only the shortfall against the declared policy is a deduction: a curated
    # manifest that deliberately covers 3 of 7 tools has not failed its contract.
    coverage_shortfall = max(0.0, required_coverage - coverage_ratio)
    coverage_points = round(SCORE_COVERAGE_WEIGHT * coverage_shortfall, 2)
    fatal_points = 25 if fatal_error else 0
    value = max(0, round(100 - violation_points - inconclusive_points - coverage_points - fatal_points))
    return {
        "value": value,
        "version": SCORE_VERSION,
        "formula": (
            "100 - 25*READ_ONLY/IDEMPOTENT mismatch - 15*NON_DESTRUCTIVE/CLOSED_WORLD "
            "mismatch - 10*other violation - 20*AMBIGUOUS_RESULT_DUPLICATE_ON_RETRY "
            "- 10*inconclusive case - 20*coverage shortfall below policy "
            "- 25*fatal error, floored at 0"
        ),
        "inputs": {
            "violations": violation_counts,
            "inconclusiveCases": inconclusive,
            "coverageRatio": round(coverage_ratio, 6),
            "requiredCoverage": required_coverage,
            "requiredCoverage": required_coverage,
            "fatalError": fatal_error is not None,
        },
    }


def _suppression_matches(
    entry: dict[str, Any], case: dict[str, Any], code: str
) -> bool:
    if entry.get("code") != code:
        return False
    if "caseId" in entry:
        return entry["caseId"] == case["id"]
    if "tool" in entry:
        return entry["tool"] == case.get("tool")
    return False


def _suppression_expired(expires: str, today: str) -> bool:
    # ISO dates sort lexicographically, so this needs no date parsing heuristics.
    return expires[:10] < today


def _apply_suppressions(
    results: list[dict[str, Any]], suppressions: dict[str, Any]
) -> dict[str, Any]:
    """Accept a finding only with a reason and an expiry, and never silently.

    An expired entry is itself a finding, an entry that matches nothing is
    reported stale, and every applied suppression is listed with its reason so a
    reader can judge the acceptance rather than just see a green run.
    """
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).date().isoformat()
    entries = suppressions.get("suppressions", [])
    used: set[int] = set()
    applied: list[dict[str, Any]] = []
    expired: list[dict[str, Any]] = []
    for case in results:
        remaining: list[dict[str, Any]] = []
        for violation in case.get("violations", []):
            match_index = next(
                (
                    index
                    for index, entry in enumerate(entries)
                    if _suppression_matches(entry, case, str(violation.get("code")))
                ),
                None,
            )
            if match_index is None:
                remaining.append(violation)
                continue
            entry = entries[match_index]
            if _suppression_expired(str(entry["expires"]), today):
                expired.append(
                    {
                        "caseId": case["id"],
                        "tool": case.get("tool"),
                        "code": violation.get("code"),
                        "expires": entry["expires"],
                    }
                )
                remaining.append(
                    {
                        "code": "SUPPRESSION_EXPIRED",
                        "message": (
                            f"the suppression for {violation.get('code')} expired on "
                            f"{entry['expires']}; renew it with a fresh reason or fix "
                            "the finding"
                        ),
                    }
                )
                continue
            used.add(match_index)
            applied.append(
                {
                    "caseId": case["id"],
                    "tool": case.get("tool"),
                    "code": violation.get("code"),
                    "reason": entry.get("reason"),
                    "owner": entry.get("owner"),
                    "expires": entry.get("expires"),
                }
            )
        case["violations"] = remaining
        case["passed"] = not remaining and not case.get("inconclusive")
    unmatched = [entry for index, entry in enumerate(entries) if index not in used]
    return {
        "suppressedCount": len(applied),
        "applied": applied,
        "expired": expired,
        "unmatched": unmatched,
    }


def _apply_baseline(
    results: list[dict[str, Any]], baseline: dict[str, Any]
) -> dict[str, Any]:
    """Drop findings the baseline already recorded, and report what changed.

    The key is ``(caseId, code)`` rather than the message: a message can change
    while the finding stays the same, and a case whose declaration source moves
    from an annotation to a contract is still the same case. Nothing is hidden:
    every suppressed finding is listed with its reason, and a finding that is not
    in the baseline still fails the run.
    """
    known: set[tuple[str, str]] = set()
    for case in baseline.get("cases", []):
        if not isinstance(case, dict):
            continue
        case_id = str(case.get("id"))
        for violation in case.get("violations", []) or []:
            if isinstance(violation, dict):
                known.add((case_id, str(violation.get("code"))))

    suppressed: list[dict[str, Any]] = []
    regressions: list[dict[str, Any]] = []
    current: set[tuple[str, str]] = set()
    for case in results:
        remaining: list[dict[str, Any]] = []
        for violation in case.get("violations", []):
            key = (case["id"], str(violation.get("code")))
            current.add(key)
            if key in known:
                suppressed.append(
                    {
                        "caseId": case["id"],
                        "tool": case.get("tool"),
                        "code": violation.get("code"),
                        "reason": "recorded in the baseline",
                    }
                )
            else:
                remaining.append(violation)
                regressions.append(
                    {"caseId": case["id"], "code": violation.get("code")}
                )
        case["violations"] = remaining
        case["passed"] = not remaining and not case.get("inconclusive")
    fixed = sorted(f"{case_id}:{code}" for case_id, code in known - current)
    summary: list[str] = []
    if regressions:
        summary.append("BASELINE_REGRESSION")
    if fixed:
        summary.append("BASELINE_FIXED")
    return {
        "summary": summary,
        "suppressedCount": len(suppressed),
        "suppressed": suppressed,
        "regressions": regressions,
        "fixed": fixed,
        "fixedCount": len(fixed),
    }


def _new_client(
    server: dict[str, Any], server_cwd: Path, environment: dict[str, str]
) -> Any:
    """Construct a client without starting it, so a failed start keeps stderr."""
    if server.get("transport", "stdio") == "streamable-http":
        return McpHttpClient(
            str(server["url"]),
            headers_from_env=_string_map(server.get("headersFromEnv", {}), "server.headersFromEnv"),
            allowed_hosts=tuple(
                _string_list(
                    server.get("allowedHosts", ["127.0.0.1", "localhost", "::1"]),
                    "server.allowedHosts",
                )
            ),
            protocol_version=str(server.get("protocolVersion", "2025-11-25")),
            timeout_seconds=float(server.get("requestTimeoutSeconds", 30)),
            startup_timeout_seconds=float(server.get("startupTimeoutSeconds", 15)),
            max_response_bytes=int(server.get("maxMessageBytes", 8 * 1024 * 1024)),
        )
    return McpStdioClient(
        list(server["command"]),
        cwd=server_cwd,
        environment=environment,
        request_timeout_seconds=float(server.get("requestTimeoutSeconds", 30)),
        startup_timeout_seconds=float(server.get("startupTimeoutSeconds", 15)),
        protocol_version=str(server.get("protocolVersion", "2025-11-25")),
        max_message_bytes=int(server.get("maxMessageBytes", 8 * 1024 * 1024)),
    )


def _order_cases(
    manifest: dict[str, Any], ordering: str
) -> list[dict[str, Any]]:
    cases = list(manifest["cases"])
    if ordering == "name":
        return sorted(cases, key=lambda case: case["id"])
    if ordering == "manifest":
        declared = manifest.get("caseOrder")
        if not isinstance(declared, list):
            return cases
        index = {str(case_id): position for position, case_id in enumerate(declared)}
        return sorted(
            cases,
            key=lambda case: (index.get(case["id"], len(index)), cases.index(case)),
        )
    return cases


def _unbudgeted_case(
    case: dict[str, Any], server_instance: int = 0
) -> dict[str, Any]:
    """A case the run never reached because the time budget was spent."""
    return {
        "id": case["id"],
        "tool": case["tool"],
        "argumentShapeSha256": canonical_digest(
            _value_shape(case.get("arguments", {}))
        ),
        "argumentKeys": sorted(case.get("arguments", {})),
        "passed": False,
        "durationMs": 0,
        "declarations": {},
        "calls": [],
        "deltas": {"firstCall": [], "retry": []},
        "serverInstance": server_instance,
        "violations": [],
        "inconclusive": [
            {
                "code": "RUN_BUDGET_EXHAUSTED",
                "message": (
                    "the run-level time budget was spent before this case ran; "
                    "raise --timeout or policy.runTimeoutSeconds to verify it"
                ),
            }
        ],
    }


def _limitations(observers: list[dict[str, Any]]) -> list[str]:
    kinds = {observer["kind"] for observer in observers}
    limitations = [
        "Only configured observers define the observable side-effect boundary.",
        "External reads cannot be inferred from state snapshots.",
    ]
    if "http-json" in kinds:
        limitations.append(
            "HTTP JSON observers detect state change but cannot classify deletion."
        )
    if "sqlite-query" in kinds:
        limitations.append(
            "A SQLite observer compares only the rows its query returns; writes "
            "outside that result set are not observed."
        )
    if "filesystem" not in kinds:
        limitations.append(
            "No filesystem observer was configured, so file writes are outside "
            "the observed boundary."
        )
    return limitations


def _run_case(
    case: dict[str, Any],
    *,
    client: McpStdioClient,
    tool: dict[str, Any] | None,
    observers: dict[str, Observer],
    base: Path,
    server_environment: dict[str, str],
) -> dict[str, Any]:
    started = time.time()
    arguments = case.get("arguments", {})
    public: dict[str, Any] = {
        "id": case["id"],
        "tool": case["tool"],
        "argumentShapeSha256": canonical_digest(_value_shape(arguments)),
        "argumentKeys": sorted(arguments),
        "passed": False,
        "durationMs": 0,
        "declarations": {},
        "calls": [],
        "deltas": {"firstCall": [], "retry": []},
        "violations": [],
        "inconclusive": [],
    }
    if tool is None:
        public["violations"].append(
            {"code": "TOOL_NOT_LISTED", "message": "tool was not returned by tools/list"}
        )
        return _finish_case(public, started)

    annotations = tool.get("annotations") if isinstance(tool.get("annotations"), dict) else {}
    explicit = case.get("contract", {})
    declarations, sources = _declarations(annotations, explicit)
    public["declarations"] = {
        key: {"value": value, "source": sources[key]}
        for key, value in declarations.items()
    }
    if not declarations:
        # Nothing was declared, so nothing can be contradicted. Calling the tool
        # would only risk side effects in exchange for no evidence, and a pass
        # here would report an unchecked tool as verified.
        public["inconclusive"].append(
            {
                "code": "NO_DECLARED_ANNOTATIONS",
                "message": (
                    "the tool publishes no effect annotations and the case declares no "
                    "contract, so there is nothing to verify; the tool was not called"
                ),
            }
        )
        public["passed"] = False
        return _finish_case(public, started)
    selected_ids = case.get("observerIds", list(observers))
    selected = [observers[observer_id] for observer_id in selected_ids]
    control_rounds = int(case.get("observerControlRounds", 1))
    if "ambiguousResultFault" in case:
        return _run_ambiguous_result_case(
            case,
            client=client,
            declarations=declarations,
            selected=selected,
            public=public,
            base=base,
            server_environment=server_environment,
            started=started,
            control_rounds=control_rounds,
        )
    try:
        if "setup" in case:
            _run_hook(case["setup"], base, server_environment)
        before, control = _control_window(selected, control_rounds)
        public["observerControl"] = control
        # A state transition reproducible without the tool cannot be blamed on it.
        unstable_observers = set(control["unstableObservers"])
        if unstable_observers:
            public["inconclusive"].append(
                {
                    "code": "OBSERVER_OR_BACKGROUND_INTERFERENCE",
                    "message": (
                        "observed state changed with no tool call; deltas from these "
                        f"observers cannot be attributed to the tool: {control['unstableObservers']}"
                    ),
                }
            )
        first_result = client.call_tool(case["tool"], arguments)
        after_first = _snapshot_all(selected)
        first_delta = _diff_all(selected, before, after_first)
        public["calls"].append(_public_call(first_result, 1))
        public["deltas"]["firstCall"] = [delta.public() for delta in first_delta]

        expect_error = bool(case.get("expectToolError", False))
        tool_error = bool(first_result.get("isError", False))
        # ``mcp-scan`` synthesizes arguments from a schema it does not own. When
        # those arguments are rejected, the tool's declared effects were never
        # exercised: that is a limit of the harness, not a conformance finding.
        unexercised_arguments = bool(case.get("generatedArguments", False)) and (
            tool_error and not expect_error
        )
        if tool_error != expect_error:
            if unexercised_arguments:
                public["inconclusive"].append(
                    {
                        "code": "TOOL_ERROR_ON_GENERATED_ARGUMENTS",
                        "message": (
                            "synthesized arguments were rejected (tools/call isError=true), "
                            "so declared behavior could not be observed; not counted as a "
                            "conformance finding"
                        ),
                    }
                )
            else:
                public["violations"].append(
                    {
                        "code": "TOOL_ERROR_EXPECTATION_MISMATCH",
                        "message": f"tools/call isError={tool_error}, expected {expect_error}",
                    }
                )

        attributable_first = [
            delta for delta in first_delta
            if delta.observer_id not in unstable_observers
        ]
        if unexercised_arguments:
            public["passed"] = False
            return _finish_case(public, started)
        _check_first_call(declarations, attributable_first, public)

        retry_enabled = bool(case.get("retry", declarations.get("idempotentHint") is True))
        if retry_enabled:
            second_result = client.call_tool(case["tool"], arguments)
            after_second = _snapshot_all(selected)
            retry_delta = _diff_all(selected, after_first, after_second)
            public["calls"].append(_public_call(second_result, 2))
            public["deltas"]["retry"] = [delta.public() for delta in retry_delta]
            second_error = bool(second_result.get("isError", False))
            if second_error != expect_error:
                if second_error and case.get("generatedArguments", False):
                    public["inconclusive"].append(
                        {
                            "code": "RETRY_TOOL_ERROR_ON_GENERATED_ARGUMENTS",
                            "message": (
                                "the repeated call with synthesized arguments was rejected "
                                "(for example because the first call consumed them); not "
                                "counted as a conformance finding"
                            ),
                        }
                    )
                else:
                    public["violations"].append(
                        {
                            "code": "RETRY_TOOL_ERROR_EXPECTATION_MISMATCH",
                            "message": (
                                f"retry tools/call isError={second_error}, expected {expect_error}"
                            ),
                        }
                    )
            attributable_retry = [
                delta for delta in retry_delta
                if delta.observer_id not in unstable_observers
            ]
            if (declarations.get("idempotentHint") is True
                    and any(delta.changed for delta in attributable_retry)):
                public["violations"].append(
                    {
                        "code": "IDEMPOTENT_HINT_MISMATCH",
                        "message": "repeating the same call changed observed state again",
                    }
                )
            _check_first_call(declarations, attributable_retry, public)
        elif declarations.get("idempotentHint") is True:
            # Only a positive idempotency claim needs the retry to be exercised;
            # idempotentHint=false already expects repeated effects.
            public["inconclusive"].append(
                {
                    "code": "IDEMPOTENCY_NOT_EXERCISED",
                    "message": "retry=false prevented dynamic idempotency verification",
                }
            )
    except (
        ManifestError,
        McpClientError,
        ObserverError,
        OSError,
        subprocess.SubprocessError,
    ) as error:
        public["violations"].append(
            {"code": "CASE_EXECUTION_FAILED", "message": _safe_error(error)}
        )
    finally:
        if "cleanup" in case:
            try:
                _run_hook(case["cleanup"], base, server_environment)
            except (OSError, subprocess.SubprocessError, ManifestError) as error:
                public["violations"].append(
                    {"code": "CLEANUP_FAILED", "message": _safe_error(error)}
                )
    public["passed"] = not public["violations"] and not public["inconclusive"]
    return _finish_case(public, started)


def _run_ambiguous_result_case(
    case: dict[str, Any],
    *,
    client: McpStdioClient,
    declarations: dict[str, bool],
    selected: list[Observer],
    public: dict[str, Any],
    base: Path,
    server_environment: dict[str, str],
    started: float,
    control_rounds: int = 1,
) -> dict[str, Any]:
    fault = case["ambiguousResultFault"]
    mode = fault["mode"]
    trials: list[dict[str, Any]] = []
    classifications: dict[str, int] = {}
    interfering_trials: list[int] = []
    try:
        for trial_number in range(1, int(fault.get("trials", 20)) + 1):
            if "setup" in case:
                _run_hook(case["setup"], base, server_environment)
            before, control = _control_window(selected, control_rounds)
            unstable_observers = set(control["unstableObservers"])
            if control["interference"]:
                interfering_trials.append(trial_number)
            first_delta: list[Delta] = []
            first_call: dict[str, Any]
            if mode == "drop-result-after-response":
                first_result = client.call_tool(case["tool"], case.get("arguments", {}))
                after_first = _snapshot_all(selected)
                first_delta = _diff_all(selected, before, after_first)
                first_call = {
                    **_public_call(first_result, 1),
                    "outcome": "response-discarded-at-verifier-boundary",
                }
                _check_first_call(
                    declarations,
                    [delta for delta in first_delta
                     if delta.observer_id not in unstable_observers],
                    public,
                )
            else:
                after_first = before
                first_call = {
                    "number": 1,
                    "outcome": "timeout-before-request-send",
                    "isError": False,
                    "contentTypes": [],
                    "structuredContentPresent": False,
                }

            retry_result = client.call_tool(case["tool"], case.get("arguments", {}))
            after_retry = _snapshot_all(selected)
            retry_delta = _diff_all(selected, after_first, after_retry)
            attributable_retry = [
                delta for delta in retry_delta
                if delta.observer_id not in unstable_observers
            ]
            _check_first_call(declarations, attributable_retry, public)
            attributable_first = [
                delta for delta in first_delta
                if delta.observer_id not in unstable_observers
            ]
            first_changed = any(delta.changed for delta in attributable_first)
            retry_changed = any(delta.changed for delta in attributable_retry)
            if unstable_observers and len(unstable_observers) == len(selected):
                classification = "observer-or-background-interference"
            elif mode == "timeout-before-send" and retry_changed:
                classification = "no-effect-timeout"
            elif first_changed and not retry_changed:
                classification = "committed-result-lost"
            elif first_changed and retry_changed:
                classification = "duplicate-on-retry"
            else:
                classification = "ambiguous-unknown"
            classifications[classification] = classifications.get(classification, 0) + 1
            trials.append(
                {
                    "trial": trial_number,
                    "classification": classification,
                    "observerControl": control,
                    "calls": [first_call, _public_call(retry_result, 2)],
                    "deltas": {
                        "firstCall": [delta.public() for delta in first_delta],
                        "retry": [delta.public() for delta in retry_delta],
                    },
                }
            )
    except (
        ManifestError,
        McpClientError,
        ObserverError,
        OSError,
        subprocess.SubprocessError,
    ) as error:
        public["violations"].append(
            {"code": "CASE_EXECUTION_FAILED", "message": _safe_error(error)}
        )
    finally:
        if "cleanup" in case:
            try:
                _run_hook(case["cleanup"], base, server_environment)
            except (OSError, subprocess.SubprocessError, ManifestError) as error:
                public["violations"].append(
                    {"code": "CLEANUP_FAILED", "message": _safe_error(error)}
                )

    public["ambiguousResult"] = {
        "mode": mode,
        "configuredTrials": int(fault.get("trials", 20)),
        "completedTrials": len(trials),
        "timeoutMs": int(fault.get("timeoutMs", 1000)),
        "observerControlRounds": control_rounds,
        "interferingTrials": interfering_trials,
        "classifications": classifications,
        "trials": trials,
    }
    if interfering_trials:
        public["inconclusive"].append(
            {
                "code": "OBSERVER_OR_BACKGROUND_INTERFERENCE",
                "message": (
                    "observed state changed with no tool call in trials "
                    f"{interfering_trials}; unstable-observer deltas were excluded from attribution"
                ),
            }
        )
    if classifications.get("duplicate-on-retry"):
        public["violations"].append(
            {
                "code": "AMBIGUOUS_RESULT_DUPLICATE_ON_RETRY",
                "message": "retry after an ambiguous result changed observed state a second time",
            }
        )
    if classifications.get("ambiguous-unknown"):
        public["inconclusive"].append(
            {
                "code": "AMBIGUOUS_RESULT_UNCLASSIFIED",
                "message": "observer evidence could not distinguish commit, no-effect, or duplicate behavior",
            }
        )
    public["passed"] = not public["violations"] and not public["inconclusive"]
    return _finish_case(public, started)


def _check_first_call(
    declarations: dict[str, bool], deltas: list[Delta], result: dict[str, Any]
) -> None:
    changed = [delta for delta in deltas if delta.changed]
    if declarations.get("readOnlyHint") is True and changed:
        result["violations"].append(
            {
                "code": "READ_ONLY_HINT_MISMATCH",
                "message": "a tool declared read-only changed observed state",
            }
        )
    if declarations.get("destructiveHint") is False and any(
        delta.destructive for delta in deltas
    ):
        result["violations"].append(
            {
                "code": "NON_DESTRUCTIVE_HINT_MISMATCH",
                "message": "a tool declared non-destructive removed observed state",
            }
        )
    if declarations.get("openWorldHint") is False and any(
        delta.changed and delta.world == "open" for delta in deltas
    ):
        result["violations"].append(
            {
                "code": "CLOSED_WORLD_HINT_MISMATCH",
                "message": "a closed-world tool changed an open-world observer",
            }
        )


def _declarations(
    annotations: dict[str, Any], explicit: dict[str, Any]
) -> tuple[dict[str, bool], dict[str, str]]:
    result: dict[str, bool] = {}
    sources: dict[str, str] = {}
    for name in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
        if name in explicit:
            result[name] = bool(explicit[name])
            sources[name] = "manifest-contract"
        elif isinstance(annotations.get(name), bool):
            result[name] = annotations[name]
            sources[name] = "mcp-tool-annotation"
    return result, sources


def _public_call(result: dict[str, Any], number: int) -> dict[str, Any]:
    content = result.get("content", [])
    content_types = sorted(
        {
            str(item.get("type"))
            for item in content
            if isinstance(item, dict) and item.get("type") is not None
        }
    ) if isinstance(content, list) else []
    return {
        "number": number,
        "isError": bool(result.get("isError", False)),
        "contentTypes": content_types,
        "structuredContentPresent": "structuredContent" in result,
    }


def _snapshot_all(observers: list[Observer]) -> dict[str, Snapshot]:
    return {observer.observer_id: observer.snapshot() for observer in observers}


def _control_window(
    observers: list[Observer], rounds: int
) -> tuple[dict[str, Snapshot], dict[str, Any]]:
    """Run matched no-tool observation rounds before attributing any state change.

    Snapshotting is not guaranteed to be passive (an HTTP observer issues a real
    request) and the observed system may have background writers. Repeating the
    exact observer sequence with no tool call first tells us whether a later delta
    can be attributed to the tool at all. Returns the final baseline snapshot and
    the public control evidence.
    """
    baseline = _snapshot_all(observers)
    if rounds < 1:
        return baseline, {"rounds": 0, "interference": False, "unstableObservers": [], "skipped": True}
    unstable: set[str] = set()
    for _ in range(rounds):
        following = _snapshot_all(observers)
        for delta in _diff_all(observers, baseline, following):
            if delta.changed:
                unstable.add(delta.observer_id)
        baseline = following
    return baseline, {
        "rounds": rounds,
        "interference": bool(unstable),
        "unstableObservers": sorted(unstable),
        "skipped": False,
    }


def _diff_all(
    observers: list[Observer], before: dict[str, Snapshot], after: dict[str, Snapshot]
) -> list[Delta]:
    return [
        observer.diff(before[observer.observer_id], after[observer.observer_id])
        for observer in observers
    ]


def _build_observers(items: list[dict[str, Any]], base: Path) -> dict[str, Observer]:
    result: dict[str, Observer] = {}
    for item in items:
        observer_id = item["id"]
        common = {
            "world": item.get(
                "world", "open" if item["kind"] == "http-json" else "closed"
            ),
            "sensitive": bool(item.get("sensitive", True)),
        }
        if item["kind"] == "filesystem":
            observer: Observer = FilesystemObserver(
                observer_id,
                _resolve_path(base, item["root"]),
                exclude=tuple(_string_list(item.get("exclude", []), "observer.exclude")),
                max_files=int(item.get("maxFiles", 10_000)),
                max_entries=int(item.get("maxEntries", 50_000)),
                max_total_bytes=int(item.get("maxTotalBytes", 100 * 1024 * 1024)),
                **common,
            )
        elif item["kind"] == "sqlite-query":
            observer = SqliteQueryObserver(
                observer_id,
                _resolve_path(base, item["database"]),
                item["query"],
                tuple(item.get("parameters", [])),
                **common,
            )
        elif item["kind"] == "http-json":
            observer = HttpJsonObserver(
                observer_id,
                item["url"],
                allowed_hosts=tuple(
                    _string_list(
                        item.get("allowedHosts", ["127.0.0.1", "localhost", "::1"]),
                        "observer.allowedHosts",
                    )
                ),
                headers_from_env=_string_map(
                    item.get("headersFromEnv", {}), "observer.headersFromEnv"
                ),
                timeout_seconds=float(item.get("timeoutSeconds", 5)),
                max_response_bytes=int(item.get("maxResponseBytes", 1024 * 1024)),
                **common,
            )
        else:
            raise ManifestError(f"unsupported observer kind: {item['kind']!r}")
        result[observer_id] = observer
    return result


def _run_hook(hook: dict[str, Any], base: Path, server_environment: dict[str, str]) -> None:
    command = hook["command"]
    cwd = _resolve_path(base, hook.get("cwd", "."))
    environment = dict(server_environment)
    environment.update(_string_map(hook.get("env", {}), "hook.env"))
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=float(hook.get("timeoutSeconds", 30)),
        check=False,
        shell=False,
    )
    if completed.returncode:
        raise ManifestError(
            f"hook exited with code {completed.returncode}; output omitted from report"
        )


def load_suppressions(path: str | Path) -> dict[str, Any]:
    """Validate a suppressions file. Every entry needs a reason and an expiry."""
    from datetime import date

    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as error:
        raise ManifestError(f"could not read suppressions: {error}") from error
    except json.JSONDecodeError as error:
        raise ManifestError(f"suppressions file is invalid JSON: {error}") from error
    if not isinstance(value, dict) or not isinstance(value.get("suppressions"), list):
        raise ManifestError("suppressions file must be an object with a suppressions array")
    for index, entry in enumerate(value["suppressions"]):
        label = f"suppressions[{index}]"
        if not isinstance(entry, dict):
            raise ManifestError(f"{label} must be an object")
        if not isinstance(entry.get("code"), str) or not entry["code"]:
            raise ManifestError(f"{label} requires a finding code")
        if ("caseId" in entry) == ("tool" in entry):
            raise ManifestError(f"{label} requires exactly one of caseId or tool")
        if not isinstance(entry.get("reason"), str) or not entry["reason"].strip():
            raise ManifestError(f"{label} requires a non-empty reason")
        expires = entry.get("expires")
        if not isinstance(expires, str):
            raise ManifestError(f"{label} requires an ISO expiry date")
        try:
            date.fromisoformat(expires[:10])
        except ValueError as error:
            raise ManifestError(f"{label} expires must be YYYY-MM-DD") from error
        if "owner" in entry and not isinstance(entry["owner"], str):
            raise ManifestError(f"{label} owner must be a string")
    return value


def _validate_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schemaVersion") != MANIFEST_SCHEMA:
        raise ManifestError(f"schemaVersion must be {MANIFEST_SCHEMA!r}")
    server = manifest.get("server")
    if not isinstance(server, dict):
        raise ManifestError("server must be an object")
    transport = server.get("transport", "stdio")
    if transport not in {"stdio", "streamable-http"}:
        raise ManifestError("server.transport must be 'stdio' or 'streamable-http'")
    if transport == "stdio":
        _command(server.get("command"), "server.command")
    else:
        if not isinstance(server.get("url"), str) or not server["url"]:
            raise ManifestError("server.url is required for streamable-http")
        _string_map(server.get("headersFromEnv", {}), "server.headersFromEnv")
        _string_list(
            server.get("allowedHosts", ["127.0.0.1", "localhost", "::1"]),
            "server.allowedHosts",
        )
    if "captureStderr" in server and not isinstance(server["captureStderr"], bool):
        raise ManifestError("server.captureStderr must be a boolean")
    if "restartPerCase" in server and not isinstance(server["restartPerCase"], bool):
        raise ManifestError("server.restartPerCase must be a boolean")
    if server.get("caseOrder", "file") not in {"file", "name", "manifest"}:
        raise ManifestError("server.caseOrder must be file, name or manifest")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ManifestError("cases must be a non-empty array")
    observer_items = manifest.get("observers", [])
    if not isinstance(observer_items, list) or not observer_items:
        raise ManifestError("observers must be a non-empty array")
    observer_ids: set[str] = set()
    for item in observer_items:
        if not isinstance(item, dict):
            raise ManifestError("every observer must be an object")
        observer_id = item.get("id")
        if not isinstance(observer_id, str) or not observer_id:
            raise ManifestError("every observer requires a non-empty id")
        if observer_id in observer_ids:
            raise ManifestError(f"duplicate observer id: {observer_id!r}")
        observer_ids.add(observer_id)
        kind = item.get("kind")
        required = {
            "filesystem": "root",
            "sqlite-query": "database",
            "http-json": "url",
        }
        if kind not in required:
            raise ManifestError(f"unsupported observer kind: {kind!r}")
        if required[kind] not in item:
            raise ManifestError(f"observer {observer_id!r} requires {required[kind]!r}")
        if kind == "sqlite-query" and not isinstance(item.get("query"), str):
            raise ManifestError(f"observer {observer_id!r} requires a query")
        if item.get("world", "closed") not in {"closed", "open"}:
            raise ManifestError("observer world must be 'closed' or 'open'")
    case_ids: set[str] = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ManifestError("every case must be an object")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id:
            raise ManifestError("every case requires a non-empty id")
        if case_id in case_ids:
            raise ManifestError(f"duplicate case id: {case_id!r}")
        case_ids.add(case_id)
        if not isinstance(case.get("tool"), str) or not case["tool"]:
            raise ManifestError(f"case {case_id!r} requires a tool")
        if not isinstance(case.get("arguments", {}), dict):
            raise ManifestError(f"case {case_id!r} arguments must be an object")
        selected = case.get("observerIds", list(observer_ids))
        if not isinstance(selected, list) or not selected:
            raise ManifestError(f"case {case_id!r} observerIds must be non-empty")
        unknown = set(_string_list(selected, f"case {case_id}.observerIds")) - observer_ids
        if unknown:
            raise ManifestError(
                f"case {case_id!r} references unknown observers: {sorted(unknown)}"
            )
        contract = case.get("contract", {})
        if not isinstance(contract, dict):
            raise ManifestError(f"case {case_id!r} contract must be an object")
        rounds = case.get("observerControlRounds", 1)
        if not isinstance(rounds, int) or isinstance(rounds, bool) or not 0 <= rounds <= 10:
            raise ManifestError(
                f"case {case_id!r} observerControlRounds must be an integer 0..10"
            )
        allowed_contract = {
            "readOnlyHint",
            "destructiveHint",
            "idempotentHint",
            "openWorldHint",
        }
        if set(contract) - allowed_contract or not all(
            isinstance(value, bool) for value in contract.values()
        ):
            raise ManifestError(f"case {case_id!r} contract contains invalid hints")
        for hook_name in ("setup", "cleanup"):
            if hook_name in case:
                hook = case[hook_name]
                if not isinstance(hook, dict):
                    raise ManifestError(f"case {case_id!r} {hook_name} must be an object")
                _command(hook.get("command"), f"case {case_id}.{hook_name}.command")
        if "ambiguousResultFault" in case:
            fault = case["ambiguousResultFault"]
            if not isinstance(fault, dict):
                raise ManifestError(
                    f"case {case_id!r} ambiguousResultFault must be an object"
                )
            unknown_fault_fields = set(fault) - {"mode", "trials", "timeoutMs"}
            if unknown_fault_fields:
                raise ManifestError(
                    f"case {case_id!r} ambiguousResultFault contains unknown fields"
                )
            if fault.get("mode") not in {
                "drop-result-after-response",
                "timeout-before-send",
            }:
                raise ManifestError(
                    f"case {case_id!r} ambiguousResultFault.mode is invalid"
                )
            trials = fault.get("trials", 20)
            if not isinstance(trials, int) or isinstance(trials, bool) or not 1 <= trials <= 100:
                raise ManifestError(
                    f"case {case_id!r} ambiguousResultFault.trials must be 1..100"
                )
            timeout_ms = fault.get("timeoutMs", 1000)
            if (
                not isinstance(timeout_ms, int)
                or isinstance(timeout_ms, bool)
                or timeout_ms < 1
            ):
                raise ManifestError(
                    f"case {case_id!r} ambiguousResultFault.timeoutMs must be positive"
                )
    policy = manifest.get("policy", {})
    if not isinstance(policy, dict):
        raise ManifestError("policy must be an object")
    minimum = policy.get("minimumToolCoverage", 0)
    if not isinstance(minimum, (int, float)) or isinstance(minimum, bool) or not 0 <= minimum <= 1:
        raise ManifestError("policy.minimumToolCoverage must be between 0 and 1")
    run_timeout = policy.get("runTimeoutSeconds")
    if run_timeout is not None and (
        not isinstance(run_timeout, (int, float))
        or isinstance(run_timeout, bool)
        or run_timeout <= 0
    ):
        raise ManifestError("policy.runTimeoutSeconds must be a positive number")


def _command(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ManifestError(f"{label} must be a non-empty array of strings")
    return value


def _string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ManifestError(f"{label} must be an array of strings")
    return value


def _string_map(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise ManifestError(f"{label} must be an object of string values")
    return value


def _resolve_path(base: Path, value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (base / path).resolve()


# Bounded, and redacted even when the user opted in: server stderr is where
# credentials leak, and an excerpt is for diagnosis, not for copying secrets into
# an artifact that gets uploaded.
_SECRET_IN_STDERR = re.compile(
    r"(?i)\b(token|secret|password|passwd|api[_-]?key|authorization|bearer)"
    r"(\s*[:=]\s*)(\S+)"
)


def _stderr_excerpt(text: str, limit: int = 4000) -> str:
    redacted = _SECRET_IN_STDERR.sub(lambda match: f"{match.group(1)}{match.group(2)}[redacted]", text)
    return redacted[-limit:]


def _safe_error(error: BaseException) -> str:
    text = str(error).replace("\n", " ").strip()
    return text[:2000] or error.__class__.__name__


def _value_shape(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _value_shape(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [_value_shape(item) for item in value]
    if value is None:
        return "null"
    return type(value).__name__


def _finish_case(result: dict[str, Any], started: float) -> dict[str, Any]:
    result["durationMs"] = round((time.time() - started) * 1000)
    return result


def _utc_timestamp(timestamp: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")
