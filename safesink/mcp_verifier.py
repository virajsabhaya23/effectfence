from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import time
from pathlib import Path
from typing import Any

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


def verify_manifest(path: str | Path) -> dict[str, Any]:
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
    started = time.time()
    results: list[dict[str, Any]] = []
    fatal_error: str | None = None
    tools: list[dict[str, Any]] = []
    client: McpStdioClient | None = None
    try:
        client = McpStdioClient(
            list(server["command"]),
            cwd=server_cwd,
            environment=environment,
            request_timeout_seconds=float(server.get("requestTimeoutSeconds", 30)),
            startup_timeout_seconds=float(server.get("startupTimeoutSeconds", 15)),
            protocol_version=str(server.get("protocolVersion", "2025-11-25")),
            max_message_bytes=int(server.get("maxMessageBytes", 8 * 1024 * 1024)),
        )
        client.start()
        tools = client.list_tools(max_pages=int(server.get("maxListPages", 100)))
        indexed_tools = {tool["name"]: tool for tool in tools}
        for case in manifest["cases"]:
            results.append(
                _run_case(
                    case,
                    client=client,
                    tool=indexed_tools.get(case["tool"]),
                    observers=observers,
                    base=base,
                    server_environment=environment,
                )
            )
    except (McpClientError, ObserverError, OSError, ValueError) as error:
        fatal_error = _safe_error(error)
    finally:
        stderr_present = bool(client and client.stderr_tail)
        server_metadata = {
            "name": client.server_info.get("name") if client else None,
            "version": client.server_info.get("version") if client else None,
            "requested_protocol_version": server.get("protocolVersion", "2025-11-25"),
            "negotiated_protocol_version": (
                client.negotiated_protocol_version if client else None
            ),
            "stderr_present": stderr_present,
        }
        if client:
            client.close()

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

    passed = (
        fatal_error is None
        and not policy_violations
        and bool(results)
        and all(case["passed"] for case in results)
    )
    report: dict[str, Any] = {
        "schemaVersion": REPORT_SCHEMA,
        "verdict": "pass" if passed else "fail",
        "generatedAt": _utc_timestamp(started),
        "durationMs": round((time.time() - started) * 1000),
        "manifest": {
            "path": manifest_path.name,
            "sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        },
        "runtime": {
            "effectfenceVersion": "0.2.0",
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
        "cases": results,
        "limitations": [
            "Only configured observers define the observable side-effect boundary.",
            "External reads cannot be inferred from state snapshots.",
            "HTTP JSON observers detect state change but cannot classify deletion.",
        ],
    }
    report["certificateSha256"] = canonical_digest(report)
    return report


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
        if tool_error != expect_error:
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
        elif declarations.get("idempotentHint") is not None:
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


def _validate_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schemaVersion") != MANIFEST_SCHEMA:
        raise ManifestError(f"schemaVersion must be {MANIFEST_SCHEMA!r}")
    server = manifest.get("server")
    if not isinstance(server, dict):
        raise ManifestError("server must be an object")
    _command(server.get("command"), "server.command")
    if server.get("transport", "stdio") != "stdio":
        raise ManifestError("server.transport must be 'stdio'")
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
    minimum = manifest.get("policy", {}).get("minimumToolCoverage", 0)
    if not isinstance(minimum, (int, float)) or isinstance(minimum, bool) or not 0 <= minimum <= 1:
        raise ManifestError("policy.minimumToolCoverage must be between 0 and 1")


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
