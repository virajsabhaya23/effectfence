# SafeSink

**SafeSink checks whether an MCP server's tools do what their annotations claim.**

MCP tool annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`,
`openWorldHint`) are untrusted hints: nothing stops a server from declaring a
tool read-only while it writes files, or idempotent while every retry charges
the customer twice. SafeSink calls the tool, observes the state it actually
changed inside an explicit filesystem, SQLite, or HTTP JSON boundary, and reports
where the declaration and the observed effect disagree.

There is one product: the MCP conformance verifier (`safesink mcp-verify`,
`safesink mcp-scan`). The older crash/retry schedule simulator is frozen and
available only as `safesink-legacy` — see [Frozen crash/retry simulator](#frozen-crashretry-simulator).

## Install

`safesink` is not on PyPI yet, so `pip install safesink` fails. Install from the
repository instead:

```bash
python -m pip install "git+https://github.com/virajsabhaya23/effectfence.git"
```

or, from a clone:

```bash
git clone https://github.com/virajsabhaya23/effectfence.git
cd effectfence
python -m pip install .
```

Python 3.10+; no runtime dependencies. The repository ships a dependency-free
PEP 517 build backend, so the build works offline.

## Quick start

Verify the honest fixture and emit JSON, JUnit, and SARIF evidence:

```bash
safesink mcp-verify examples/mcp-conformance/passing.json \
  --out out/mcp/report.json \
  --junit out/mcp/junit.xml \
  --sarif out/mcp/report.sarif
```

The deliberately dishonest fixture exits with status 2 and demonstrates the
three primary findings:

```bash
safesink mcp-verify examples/mcp-conformance/failing.json \
  --out out/mcp/failing.json
```

Use the same verifier from Python:

```python
from safesink import verify_manifest

report = verify_manifest("manifest.json")
print(report["verdict"], report["certificateSha256"])
```

## Scan a server you have no manifest for

`mcp-scan` connects to a server, reads `tools/list`, and synthesizes a manifest
from each tool's advertised `inputSchema`, so auditing an unfamiliar server does
not require hand-authoring a case per tool.

```bash
mkdir -p sandbox
safesink mcp-scan \
  --observer-root sandbox \
  --out out/mcp/generated.json \
  -- npx -y @modelcontextprotocol/server-filesystem "$PWD/sandbox"

safesink mcp-verify out/mcp/generated.json --out out/mcp/report.json
```

The server command goes after `--` so its own flags are not parsed by `safesink`.

Tools declaring `destructiveHint: true` are **skipped by default** and listed
under `generated.skipped`; pass `--include-destructive` to audit them, and only
against a disposable sandbox. Declared annotations are copied into each case
`contract`, so the generated manifest checks the server against its own claims.

Argument synthesis honours `const`, `enum`, `default`, numeric minimums, array
`minItems`, nullable unions, and nested objects, and it distinguishes file paths
from directory paths using the property description and tool name. Generated
arguments are still a heuristic: triage `TOOL_ERROR_EXPECTATION_MISMATCH`
results before reporting them, because an unsatisfiable generated argument can
look like a conformance finding.

## What it checks

| Finding | Meaning |
| --- | --- |
| `READ_ONLY_HINT_MISMATCH` | a tool declared `readOnlyHint: true` changed observed state |
| `NON_DESTRUCTIVE_HINT_MISMATCH` | a tool declared `destructiveHint: false` removed state |
| `CLOSED_WORLD_HINT_MISMATCH` | a tool declared `openWorldHint: false` changed an open-world observer |
| `IDEMPOTENT_HINT_MISMATCH` | repeating the identical call changed observed state again |
| `TOOL_ERROR_EXPECTATION_MISMATCH` | `tools/call` returned `isError` contrary to the case |
| `TOOL_NOT_LISTED` | a case targets a tool that `tools/list` never advertised |
| `TOOL_COVERAGE_BELOW_MINIMUM` | too few advertised tools are covered by cases |

Two behaviours keep false positives out of reports:

- **Observer control rounds.** Every case (and every ambiguous-result trial) runs
  matched no-tool observation rounds first. A delta that reproduces without the
  tool is reported as `OBSERVER_OR_BACKGROUND_INTERFERENCE` and is not blamed on
  the tool; the case becomes inconclusive rather than passing or failing.
- **Inconclusive is not a pass.** A case passes only with no violations *and* no
  unresolved evidence, so checks that could not be completed are never reported
  as verified.

Ambiguous-result schedules (`ambiguousResultFault`) repeat a call under
`drop-result-after-response` or `timeout-before-send` and classify the outcome as
committed-result-lost, no-effect-timeout, duplicate-on-retry, or explicitly
unclassified.

## CI

A composite action is included:

```yaml
- uses: actions/checkout@v7
- uses: virajsabhaya23/effectfence@main
  with:
    manifest: examples/mcp-conformance/passing.json
    out: out/mcp/report.json
    junit: out/mcp/junit.xml
    sarif: out/mcp/report.sarif
```

The command exits `0` on pass and `2` on fail, so it works as a plain gate too.

## Security model

- Commands are executed without a shell, as arrays, with a restricted
  environment; only variables named in `server.inheritEnv` are passed through.
- HTTP observers are host-allowlisted and follow no redirects.
- Argument and output values are redacted by default, and sensitive observer
  digests are excluded from reports.
- Only configured observers define the observable boundary. Snapshot comparison
  cannot detect unobserved side effects, and it cannot prove that an external
  read occurred.
- Annotations come from the server under test, so a dishonest server can lie to
  the checker itself; SafeSink reports what it observed, within that boundary.

See [the MCP conformance guide](docs/MCP_CONFORMANCE.md) for the versioned
manifest, the observer boundary, and the full security model.

## Frozen crash/retry simulator

Earlier versions also shipped a deterministic crash/retry schedule simulator with
a SQLite effect ledger, reference sinks, a 30-case benchmark, and a live
Kafka/PostgreSQL killpoint adapter. It is **frozen**: kept for reproducibility of
existing artifacts, with no new features, adapters, or benchmark cases.

```bash
safesink-legacy verify examples/crash_after_effect.json --out out/report.json
safesink-legacy explore examples/crash_after_effect.json --out out/exploration.json
safesink-legacy benchmark benchmark/corpus.json --out benchmark/results.json
```

The implementation lives in `safesink/legacy/`; `python -m safesink.legacy.cli`
is equivalent. See [docs/FROZEN_CRASH_RETRY.md](docs/FROZEN_CRASH_RETRY.md) for
its status, scope, and known limitations (notably: its benchmark is graded by
the same model that generates the schedules, so its precision/recall figures
measure corpus authoring, not detection of real systems).

The deprecated `effectfence` import path and console script still work and
forward to `safesink`.

## Citation

```bash
safesink citation --format bibtex
```

or use [`CITATION.cff`](CITATION.cff). No archival DOI is claimed.
