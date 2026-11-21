"""Static annotation lint: honest checks that need no tool call.

Every other SafeSink path executes the server. That is the right way to gather
evidence and the wrong way to answer "should I trust this server?" in thirty
seconds, or to vet a server from an agent that must not launch processes.

``tools/list`` alone supports the checks below. They are warnings about what the
declarations say, not findings about what the tools do: a lint result can never
prove a tool is safe, and it is deliberately conservative because a linter that
cries wolf loses trust on its first run.
"""

from __future__ import annotations

from typing import Any

from .mcp_scan import HINT_NAMES, _name_tokens

# Name tokens that promise an effect the annotations may deny.
_WRITE_VERBS = {
    "add",
    "append",
    "create",
    "delete",
    "destroy",
    "drop",
    "erase",
    "insert",
    "move",
    "patch",
    "post",
    "purge",
    "put",
    "remove",
    "rename",
    "replace",
    "send",
    "set",
    "truncate",
    "unlink",
    "update",
    "upload",
    "write",
}
_DESTRUCTIVE_VERBS = {
    "delete",
    "destroy",
    "drop",
    "erase",
    "purge",
    "remove",
    "truncate",
    "unlink",
}
_VOLATILE_NAME_TOKENS = {
    "random",
    "uuid",
    "nonce",
    "timestamp",
    "timenow",
    "now",
}
_URL_NAME_TOKENS = {"url", "uri", "href", "endpoint", "webhook"}
_URL_FORMATS = {"uri", "url", "iri"}

# code -> (deduction, rationale, false-positive caveat)
RULES: dict[str, dict[str, Any]] = {
    "NO_DECLARED_ANNOTATIONS": {
        "deduction": 5,
        "rationale": "A tool with no annotations makes no claim, so no conformance check can exist for it.",
        "caveat": "Legitimate for servers that predate annotations; declare a CLI contract to verify one.",
    },
    "READ_ONLY_NAME_MISMATCH": {
        "deduction": 20,
        "rationale": "The name promises a write while readOnlyHint: true promises none.",
        "caveat": "Verb prefixes can be misleading, e.g. `create_index_if_missing` on a cache warm-up.",
    },
    "DESTRUCTIVE_NAME_WITH_NON_DESTRUCTIVE_HINT": {
        "deduction": 20,
        "rationale": "The name promises deletion while destructiveHint: false promises nothing is removed.",
        "caveat": "Soft deletes and archive tools remove nothing observable and can be honest.",
    },
    "CONTRADICTORY_DESTRUCTIVE_READ_ONLY": {
        "deduction": 25,
        "rationale": "readOnlyHint: true and destructiveHint: true cannot both be true.",
        "caveat": "None: this is a contradiction in the declaration itself.",
    },
    "OPEN_WORLD_URL_ARGUMENT_MISMATCH": {
        "deduction": 10,
        "rationale": "A tool that accepts a URL can reach the open world, so openWorldHint: false is suspect.",
        "caveat": "A URL may be parsed or compared without being fetched.",
    },
    "MUTATING_WITHOUT_IDEMPOTENT_HINT": {
        "deduction": 10,
        "rationale": "A mutating tool that does not state retry safety leaves the caller to guess.",
        "caveat": "The hint is optional in the protocol; this is an omission, not a lie.",
    },
    "IDEMPOTENT_WITH_VOLATILE_ARGUMENT": {
        "deduction": 15,
        "rationale": "An argument named random/uuid/nonce/timestamp changes on every retry, which cannot be idempotent.",
        "caveat": "The value may be ignored by the tool, or used only for tracing.",
    },
}

ANNOTATION_SCORE_MAX = 100


def _findings_for(tool: dict[str, Any]) -> list[dict[str, Any]]:
    name = tool.get("name")
    if not isinstance(name, str) or not name:
        return []
    annotations = tool.get("annotations") if isinstance(tool.get("annotations"), dict) else {}
    tokens = _name_tokens(name)
    schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {}
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    findings: list[dict[str, Any]] = []

    def add(code: str, message: str, evidence: dict[str, Any] | None = None) -> None:
        findings.append(
            {
                "tool": name,
                "code": code,
                "message": message,
                "evidence": evidence or {},
            }
        )

    if not any(isinstance(annotations.get(hint), bool) for hint in HINT_NAMES):
        add(
            "NO_DECLARED_ANNOTATIONS",
            "the tool publishes no effect annotations",
        )
        # Every other rule compares a claim against something. With no claim there
        # is nothing to contradict, and reporting an omission seven times would
        # drown the one message that matters.
        return findings

    read_only = annotations.get("readOnlyHint")
    destructive = annotations.get("destructiveHint")
    idempotent = annotations.get("idempotentHint")
    open_world = annotations.get("openWorldHint")

    if read_only is True and destructive is True:
        add(
            "CONTRADICTORY_DESTRUCTIVE_READ_ONLY",
            "readOnlyHint and destructiveHint are both true",
            {"readOnlyHint": True, "destructiveHint": True},
        )
    if read_only is True and tokens & _WRITE_VERBS:
        add(
            "READ_ONLY_NAME_MISMATCH",
            "the name implies a write but the tool declares readOnlyHint: true",
            {"nameTokens": sorted(tokens & _WRITE_VERBS)},
        )
    if destructive is False and tokens & _DESTRUCTIVE_VERBS:
        add(
            "DESTRUCTIVE_NAME_WITH_NON_DESTRUCTIVE_HINT",
            "the name implies deletion but the tool declares destructiveHint: false",
            {"nameTokens": sorted(tokens & _DESTRUCTIVE_VERBS)},
        )
    if open_world is False and _has_url_argument(properties):
        add(
            "OPEN_WORLD_URL_ARGUMENT_MISMATCH",
            "the tool accepts a URL but declares openWorldHint: false",
        )
    if read_only is not True and not isinstance(idempotent, bool):
        add(
            "MUTATING_WITHOUT_IDEMPOTENT_HINT",
            "a mutating tool does not state whether a retry is safe",
        )
    volatile = _volatile_arguments(properties)
    if idempotent is True and volatile:
        add(
            "IDEMPOTENT_WITH_VOLATILE_ARGUMENT",
            "the tool declares idempotentHint: true but takes a value that changes per call",
            {"arguments": volatile},
        )
    return findings


def _volatile_arguments(properties: dict[str, Any]) -> list[str]:
    return sorted(
        name
        for name in properties
        if isinstance(name, str) and _name_tokens(name) & _VOLATILE_NAME_TOKENS
    )


def _has_url_argument(properties: dict[str, Any]) -> bool:
    for name, schema in properties.items():
        if not isinstance(name, str):
            continue
        if _name_tokens(name) & _URL_NAME_TOKENS:
            return True
        if isinstance(schema, dict):
            declared = schema.get("format")
            if isinstance(declared, str) and declared.lower() in _URL_FORMATS:
                return True
    return False


def lint_tools(tools: list[dict[str, Any]]) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    for tool in tools:
        if isinstance(tool, dict):
            findings.extend(_findings_for(tool))
    counts: dict[str, int] = {}
    deduction = 0
    for finding in findings:
        code = finding["code"]
        counts[code] = counts.get(code, 0) + 1
        deduction += int(RULES[code]["deduction"])
    return {
        "mode": "static-lint",
        "toolsAdvertised": len(tools),
        "findings": findings,
        "counts": counts,
        "score": {
            "value": max(0, ANNOTATION_SCORE_MAX - deduction),
            "max": ANNOTATION_SCORE_MAX,
            "breakdown": {
                code: {
                    "count": count,
                    "deductionEach": RULES[code]["deduction"],
                    "rationale": RULES[code]["rationale"],
                    "caveat": RULES[code]["caveat"],
                }
                for code, count in sorted(counts.items())
            },
        },
    }


def lint_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Lint the declarations a manifest owns, without starting the server."""

    tools: list[dict[str, Any]] = []
    for case in manifest.get("cases", []):
        if not isinstance(case, dict):
            continue
        tools.append(
            {
                "name": str(case.get("tool", "")),
                "annotations": case.get("contract", {}),
            }
        )
    result = lint_tools(tools)
    result["source"] = "manifest"
    return result
