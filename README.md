# SafeSink

**SafeSink checks whether an MCP server's tools do what their annotations claim.**

MCP tool annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`,
`openWorldHint`) are untrusted hints: nothing stops a server from declaring a
tool read-only while it writes files, or idempotent while every retry charges the
customer twice. SafeSink calls the tool, observes the state it actually changed
inside an explicit filesystem, SQLite, or HTTP JSON boundary, and reports where
the declaration and the observed effect disagree.

One command, no manifest, no server changes:

```bash
safesink mcp-check -- npx -y @modelcontextprotocol/server-filesystem "$PWD/sandbox"
```

Nothing to install? Lint it instead. `safesink lint` reads `tools/list` and
stops: no `tools/call`, no observer, no sandbox.

```bash
safesink lint -- npx -y @modelcontextprotocol/server-filesystem "$PWD/sandbox"
```

It reports annotation problems (a write verb under `readOnlyHint: true`, a
delete verb under `destructiveHint: false`, contradictory hints, an idempotent
claim with a timestamp argument) with an annotation score. Every rule, its
rationale and its false-positive caveat are in
[docs/LINT_RULES.md](docs/LINT_RULES.md). A lint pass is not a certification;
it tells you whether a server is worth verifying.

## Install

```bash
python -m pip install safesink
```

Python 3.10+; no runtime dependencies. To work on it, from a clone:

```bash
git clone https://github.com/virajsabhaya23/effectfence.git
cd effectfence
python -m pip install -e .
```

The repository ships a dependency-free PEP 517 build backend, so the build works
offline.

## Quick start

Point it at any stdio MCP server:

```bash
mkdir -p sandbox
safesink mcp-check \
  --sandbox sandbox \
  --out out/mcp/report.json \
  --junit out/mcp/junit.xml \
  --sarif out/mcp/report.sarif \
  -- npx -y @modelcontextprotocol/server-filesystem "$PWD/sandbox"
```

Review what a run would do before any side effect exists:

```bash
safesink mcp-check --dry-run --out out/plan.json -- \
  npx -y @modelcontextprotocol/server-filesystem "$PWD/sandbox"
```

`--dry-run` still starts the server and reads `tools/list`, because that is the
only read-only way to learn the schemas, then stops. It prints a table of tool,
synthesized arguments, observers and declarations, calls no tool, and writes the
manifest (with the plan under `generated.plan`) to `--out`. It does not require
an `--sqlite-query` database to exist, since nothing will be read.

`mcp-check` starts the server, reads `tools/list`, synthesizes minimal arguments
from each tool's `inputSchema` (reusing any quoted example the description gives,
such as `'/mongodb/docs'`), seeds the sandbox with probe fixtures those arguments
can refer to, skips tools declaring `destructiveHint: true`, and verifies what
changed. Servers that need configuration take `--env KEY=VALUE`, repeatable. The
command goes after `--` so its own flags are not parsed by `safesink`.

A database server's effects are rows, not files, so point `mcp-check` at a query
instead of the sandbox hash:

```bash
safesink mcp-check \
  --sqlite-query "./state.db=select id, status from orders order by id" \
  --sandbox-observer \
  -- npx -y mcp-server-sqlite --db "$PWD/state.db"
```

The database must already exist and be seeded; a scan does not create your
schema. `--sqlite-query` is repeatable, replaces the sandbox file observer, and
adds `--sandbox-observer` back only when you want both boundaries watched.

For a server that needs a curated argument per tool, write a manifest and use
`mcp-verify` instead:

```bash
safesink mcp-verify my-manifest.json --out out/mcp/report.json
```

Build that manifest from the live server, then edit it:

```bash
safesink init --server npx -y @modelcontextprotocol/server-filesystem "$PWD/sandbox" \
  --observer-root sandbox --out safesink.mcp.json
```

`init` writes one case per tool, strips the "generated arguments" flag so a
rejection is a real finding rather than a harness limitation, and gives every
unannotated tool an empty `contract` placeholder plus a `generated.todo` entry.
The scaffold is validated before it is written, so it cannot be a manifest that
silently passes an unchecked tool.

The honest fixture and the deliberately dishonest one show both outcomes:

```bash
safesink mcp-verify examples/mcp-conformance/passing.json   # exit 0
safesink mcp-verify examples/mcp-conformance/failing.json   # exit 2
```

From Python:

```python
from safesink import verify_manifest

report = verify_manifest("manifest.json")
print(report["verdict"], report["certificateSha256"])
```

## Verdicts and exit codes

| Verdict | Exit | Meaning |
| --- | --- | --- |
| `pass` | 0 | every case was exercised and no declared effect was contradicted |
| `inconclusive` | 3 | nothing was contradicted, but at least one case could not be verified |
| `fail` | 2 | a declared effect was contradicted, or the run could not be trusted |

`inconclusive` is a first-class outcome, not a soft pass. It covers rejected
synthesized arguments, observers whose state changes on its own, and ambiguous
results the observation boundary cannot classify. A case is only `pass` when it
has no violations **and** no unresolved evidence, so CI never reports an
unverified case as verified.

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

A tool that publishes **no annotations at all** has nothing to contradict, so it
is reported as `NO_DECLARED_ANNOTATIONS` and is never called: a pass there would
claim the tool was verified when no check existed. Declare your own policy
instead, without hand-writing a manifest:

```bash
safesink mcp-check \
  --contract-all readOnlyHint:false \
  --contract list_tables=readOnlyHint:true,idempotentHint:true \
  -- npx -y mcp-server-sqlite-npx "$PWD/state.db"
```

`--contract-all` sets the policy for every tool and `--contract` overrides it per
tool. The report marks every such declaration with
`source: "manifest-contract"`, never as the server's own annotation, so a pass is
never mistaken for the server telling the truth.

Two behaviours keep false findings out of reports:

- **Observer control rounds.** Every case (and every ambiguous-result trial) runs
  matched no-tool observation rounds first. A delta that reproduces without the
  tool is reported as `OBSERVER_OR_BACKGROUND_INTERFERENCE` and is not blamed on
  the tool.
- **Argument-synthesis limits are inconclusive.** When `mcp-scan` generated the
  arguments and the server rejects them, the result is
  `TOOL_ERROR_ON_GENERATED_ARGUMENTS`, not a conformance finding. Hand-written
  cases that are rejected still fail.

Ambiguous-result schedules (`ambiguousResultFault`) repeat a call under
`drop-result-after-response` or `timeout-before-send` and classify the outcome as
committed-result-lost, no-effect-timeout, duplicate-on-retry, or explicitly
unclassified.

## Adopting it on an existing server

Record today, gate tomorrow:

```bash
safesink mcp-verify safesink.mcp.json --out baseline.json
safesink mcp-verify safesink.mcp.json --baseline baseline.json --new-only
```

`--new-only` fails only on `(caseId, code)` pairs the baseline did not record.
Every suppressed finding is still listed with its case and code, and the JUnit
report counts them, so adoption never hides what was accepted.

## Accepted findings, with a reason and an expiry

A baseline covers "not fixed yet". For an accepted risk, write a suppressions
file - every entry needs a non-empty `reason` and an ISO `expires`:

```json
{"suppressions": [
  {"tool": "delete_note", "code": "NON_DESTRUCTIVE_HINT_MISMATCH",
   "reason": "soft delete kept for API compatibility", "owner": "platform",
   "expires": "2026-06-30"}
]}
```

```bash
safesink mcp-verify safesink.mcp.json --suppress safesink.suppressions.json
```

An expired entry is itself a finding (`SUPPRESSION_EXPIRED`), an entry that
matches nothing is reported stale, and every applied suppression is listed with
its reason in the report. `--fail-on-suppressed` turns any acceptance into a
failure for high-assurance runs.

## Certificates

A report's digest proves nothing on its own. Issue a detached certificate and
anyone can check it against the report:

```bash
safesink sign --report out/mcp/report.json --out out/certificate.json
safesink verify-cert out/certificate.json --report out/mcp/report.json
```

Verification re-derives the digest chain, so an edited report fails. Add
`--key $ED25519_SEED` to sign, and `--public-key $EXPECTED` when verifying to
require a specific issuer. See [docs/CERTIFICATES.md](docs/CERTIFICATES.md) for
what a certificate does and does not prove.

## Trust score

Every report also carries `score: {value, version, formula, inputs}`: 100 minus
weighted deductions for mismatches, inconclusive cases, coverage shortfall below
the declared policy, and a fatal error (floored at 0). The weights and the
versioned formula are in
[the conformance guide](docs/MCP_CONFORMANCE.md#trust-score). `--min-score N`
fails the run when the score is below `N`, so a single number can gate CI or feed
a badge.

## Validation against real servers

Measured on 2026-11-21 with the released upstream servers, `safesink mcp-check`,
default settings (destructive tools skipped):

| Server | Version | Tools | Cases | Findings | Inconclusive | Verdict |
| --- | --- | --- | --- | --- | --- | --- |
| `@modelcontextprotocol/server-filesystem` | 2.4.0 | 16 | 13 | 0 | 1 | inconclusive (exit 3) |
| `@modelcontextprotocol/server-memory` | 2.2.1 | 9 | 6 | 0 | 0 | pass (exit 0) |
| `@modelcontextprotocol/server-sequential-thinking` | 2.2.0 | 1 | 1 | 0 | 0 | pass (exit 0) |

The single inconclusive case is `copy_file`, whose second identical call is
rejected because the first call created the destination; the tool's
`idempotentHint: true` therefore remains unverified rather than violated.

Servers outside the official set, same date, same defaults:

| Server | Version | Tools | Cases | Findings | Inconclusive | Verdict |
| --- | --- | --- | --- | --- | --- | --- |
| `@upstash/context7-mcp` | 4.7.3 | 2 | 2 | 0 | 0 | pass (exit 0) |
| `duckduckgo-mcp-server` (PyPI) | 0.7.0 | 3 | 3 | 0 | 3 | inconclusive (exit 3) |
| `mcp-server-sqlite-npx` | 0.8.0 | 5 | 5 | 0 | 5 | inconclusive (exit 3) |

Context7 publishes full annotations on both tools; its `readOnlyHint`,
`idempotentHint` and `openWorldHint` claims held. The other two publish no
annotations on any tool, so every case is `NO_DECLARED_ANNOTATIONS` and no tool is
called: there is nothing to compare against. That is the intended outcome, and it
is why an unannotated tool is not reported as a pass.

These are trusted servers, so zero findings is the expected result: the table
shows that the scanner does not manufacture findings, not that it can catch a
liar. Detection is covered by the deliberately dishonest fixture, where the
suite asserts `READ_ONLY_HINT_MISMATCH`, `IDEMPOTENT_HINT_MISMATCH`, and
`NON_DESTRUCTIVE_HINT_MISMATCH`.

An earlier revision reported 5 of these 13 cases as conformance failures because
synthesized arguments pointed at directories and files that did not exist. That
false-positive rate is why generated arguments are now seeded from probe fixtures
and why a rejected generated call is inconclusive.

## CI

A reviewable summary needs no extra tooling:

```bash
safesink mcp-verify safesink.mcp.json --markdown out/report.md --github-summary
```

`--markdown` writes the verdict banner, score, findings and inconclusive tables
plus the observed boundary; `--github-summary` appends it to
`GITHUB_STEP_SUMMARY` so the reason for a red build is on the run page.

```yaml
- uses: actions/checkout@v7
- uses: virajsabhaya23/effectfence@main
  with:
    manifest: examples/mcp-conformance/passing.json
    out: out/mcp/report.json
    junit: out/mcp/junit.xml
    sarif: out/mcp/report.sarif
```

Pin a full commit SHA in high-assurance environments. The action verifies a
manifest; use `mcp-check` in the job directly if you have no manifest yet. JUnit
marks unverifiable cases as `skipped`, not failed, matching the verdicts above.

## Security model

- Commands are executed without a shell, as arrays, with a restricted
  environment (`PATH` plus explicitly inherited variables).
- HTTP observers are host-allowlisted and follow no redirects.
- Argument and output values are redacted by default, and sensitive observer
  digests are excluded from reports.
- `mcp-check` only ever mutates the sandbox directory you give it, and skips
  tools that declare themselves destructive unless you pass
  `--include-destructive`.
- The exception is a database observed with `--sqlite-query`: without a
  filesystem sandbox observer, that database is your real data, so
  `--include-destructive` refuses to run until you also pass
  `--allow-unobserved-mutations`. The refusal names every database that would be
  mutated. Point the server at a disposable copy instead when you can.
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
