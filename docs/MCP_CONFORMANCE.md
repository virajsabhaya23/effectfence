# MCP side-effect conformance

SafeSink starts an MCP server over `stdio`, reads its `tools/list` metadata,
calls selected tools, and compares the declared effect hints with state changes
seen by explicitly configured observers. It is intended for MCP server authors,
platform teams, security reviewers, and CI maintainers.

MCP tool annotations are hints and must be treated as untrusted unless the server
itself is trusted. SafeSink supplies dynamic, observer-bounded evidence; it
does not turn those hints into a universal security boundary.

## Quick start

No manifest, no server changes:

```bash
mkdir -p sandbox
safesink mcp-check --sandbox sandbox --out out/mcp/report.json \
  -- npx -y @modelcontextprotocol/server-filesystem "$PWD/sandbox"
```

`mcp-check` starts the server, reads `tools/list`, synthesizes arguments from
each `inputSchema` (including any quoted example the description provides), seeds
the sandbox with probe fixtures, skips tools that declare `destructiveHint: true`,
verifies, and writes the report. Pass `--manifest-out` to keep the generated
manifest, `--include-destructive` to call destructive tools against a disposable
sandbox, and `--env KEY=VALUE` (repeatable) for servers that need configuration.

With `--sqlite-query DATABASE=SQL` the observed boundary is the rows that query
returns rather than the sandbox file hash; repeat the flag for several
observers, and add `--sandbox-observer` to watch both. The database must already
exist and be seeded before the run, because a scan does not create your schema.
Observer ids come from the file name (`state.db` becomes `sqlite-state`), and the
manifest stores an absolute database path so it survives being written into a
temporary sandbox.

Because that database is real user data, `--include-destructive` is refused when
`--sqlite-query` replaces the sandbox observer: mutating it would destroy what
the run is supposed to measure. The error names every database that would be
touched. Pass `--allow-unobserved-mutations` to accept that, or copy the database
and point the server at the copy.

With a hand-written manifest:

```bash
python -m pip install .
safesink mcp-verify examples/mcp-conformance/passing.json \
  --out out/mcp/report.json \
  --junit out/mcp/junit.xml \
  --sarif out/mcp/report.sarif
```

The intentionally bad fixture demonstrates the failure rules and exits with
status 2:

```bash
safesink mcp-verify examples/mcp-conformance/failing.json \
  --out out/mcp/failing.json
```

## Manifest

The versioned schema is
[`safesink/schemas/mcp-manifest-v1.schema.json`](../safesink/schemas/mcp-manifest-v1.schema.json).
Commands are arrays and are executed directly; SafeSink never invokes a
shell.

```json
{
  "schemaVersion": "effectfence.mcp.v1",
  "server": {
    "transport": "stdio",
    "command": ["node", "dist/server.js"],
    "cwd": ".",
    "inheritEnv": ["DATABASE_URL"]
  },
  "observers": [
    {
      "id": "orders",
      "kind": "sqlite-query",
      "database": "test-state/orders.db",
      "query": "select id, status from orders order by id",
      "sensitive": true
    }
  ],
  "cases": [
    {
      "id": "create-order-retry",
      "tool": "create_order",
      "arguments": {"id": "order-7"},
      "observerIds": ["orders"],
      "contract": {"idempotentHint": true},
      "retry": true,
      "setup": {"command": ["python", "tests/reset_orders.py"]}
    }
  ],
  "policy": {"minimumToolCoverage": 1.0}
}
```

`contract` is useful when a company requires a stronger policy than the server's
own annotations or when an older server does not publish them. The report marks
each declaration source as `mcp-tool-annotation` or `manifest-contract`.

## Checks

| Declaration | Dynamic failure condition |
| --- | --- |
| `readOnlyHint: true` | Any configured observer changes after the call |
| `destructiveHint: false` | A filesystem path disappears or a SQLite query returns fewer rows |
| `idempotentHint: true` | The same tool and arguments change observed state again on retry |
| `openWorldHint: false` | A configured open-world observer changes |

SafeSink also fails unexpected MCP tool errors, unknown tools, execution
errors, and tool-coverage policy violations.

### Verdicts

A report has one of three verdicts, and the CLI maps them to exit codes:

| Verdict | Exit | Meaning |
| --- | --- | --- |
| `pass` | 0 | every case ran and no declaration was contradicted |
| `inconclusive` | 3 | nothing was contradicted, but at least one case was not verified |
| `fail` | 2 | a declaration was contradicted, or the run could not be trusted |

`inconclusive` covers observer/background interference, ambiguous results the
boundary cannot classify, and rejected synthesized arguments. JUnit records these
cases as `skipped`. A case passes only with no violations and no unresolved
evidence, so a CI gate never treats unverified behavior as verified.

### Tools without annotations

A tool that publishes none of the four hints and has no `contract` in its case is
reported as `NO_DECLARED_ANNOTATIONS` and is **not called**. Nothing can be
contradicted, so a call would only risk side effects, and a pass would claim a
check that never happened. Supply a `contract` to verify such a server against
an internal policy instead.

### Generated arguments

`mcp-scan` (and therefore `mcp-check`) marks each case it synthesized with
`generatedArguments: true`. If the server rejects those arguments, the case is
reported as `TOOL_ERROR_ON_GENERATED_ARGUMENTS` and the verdict is
`inconclusive`: the declared behavior was never exercised, which is a limit of
the harness rather than a finding about the server. A rejection in a
hand-written case is still `TOOL_ERROR_EXPECTATION_MISMATCH`. Triage generated
cases before reporting them.

String arguments are synthesized from the property itself: a URL-shaped name
(`url`, `uri`, `href`, `endpoint`, `webhook`) or `format: uri` gets a real URL,
reusing a URL the description gives as an example; an argument that names SQL
(`sql`, `statement`, or a description mentioning SQL, tables, rows, columns, a
database or a schema) gets `select 1 as probe`, which parses on SQLite,
PostgreSQL and MySQL instead of failing on a syntax error. A plain `query` on a
search tool still gets an opaque probe string.

### Ambiguous result retries

Set `ambiguousResultFault` on a case to test the failure window where the
caller cannot rely on receiving a tool result and therefore retries:

```json
"ambiguousResultFault": {
  "mode": "drop-result-after-response",
  "trials": 20,
  "timeoutMs": 1000
}
```

`drop-result-after-response` completes the first MCP call, deliberately discards
its result at the verifier boundary, snapshots state, and issues the same call
again. This is a deterministic client-knowledge fault, not packet-level network
emulation. `timeout-before-send` is the negative control: no first request is
sent, so only the retry may change state. Each trial is reset independently and
classified as `committed-result-lost`, `no-effect-timeout`,
`duplicate-on-retry`, or `ambiguous-unknown`. Duplicates fail the case; unknown
evidence is explicitly inconclusive and can never produce a passing verdict.

## Observers

- `filesystem`: hashes regular files, records symlinks without following them,
  and enforces file-count and byte limits.
- `sqlite-query`: opens the database read-only, rejects mutating SQL through
  SQLite's authorizer API, and compares the rows the query returns as a
  multiset. A removed row is reported as destructive; an insert, update or
  delete outside the result set is invisible to this observer.
- `http-json`: performs an allowlisted GET, blocks redirects, caps response size,
  and compares canonical JSON state.

Observers are `sensitive: true` by default. Sensitive reports omit paths and
state digests. Tool argument values and tool output values are never written to
reports. Only argument keys/types and MCP content types are retained.

## Python integration

```python
from safesink import verify_manifest

report = verify_manifest("safesink.mcp.json")
if report["verdict"] == "fail":
    raise RuntimeError(report["cases"])
if report["verdict"] == "inconclusive":
    print("nothing contradicted, but some cases were not verified")
```

The API returns the same JSON-serializable report used by the CLI. JUnit and
SARIF writers are available from `safesink.mcp_reports`.

## CI integration

This repository includes a composite GitHub Action. A consuming repository can
pin a released commit or tag:

```yaml
- uses: virajsabhaya23/effectfence@main
  with:
    manifest: safesink.mcp.json
    out: out/safesink/report.json
    junit: out/safesink/junit.xml
    sarif: out/safesink/report.sarif
```

Pin a full commit SHA in high-assurance environments. Preserve the JSON report
as a CI artifact and use the process exit code as the merge gate.

## Diagnosing a fatal error

A run that cannot start the server, list its tools, or keep it alive records
`fatalError` as `{message, phase, case}`. `phase` is `startup`, `tools/list` or
`case`, and `case` names the case that was running, so a scan that dies on case
31 does not read like one that never started.

Server stderr is the usual explanation and is the usual place a credential
leaks, so it is summarised as `server.stderr_present` by default. Pass
`--capture-stderr` (or set `server.captureStderr` in the manifest) to add a
bounded `server.stderrExcerpt`; it is truncated to 4 KB and values beside
`token`, `secret`, `password`, `api_key`, `authorization` and `bearer` are
replaced with `[redacted]` before it reaches the report.

## Evidence boundary

The result is only as complete as the observer set. For example, a filesystem
observer cannot detect an HTTP call, a SQLite observer sees only the rows its
query selects, and snapshot comparison cannot prove an external read occurred. Add observers for every state boundary relevant to the
tool. A passing report means no mismatch was observed within that declared
boundary; it is not proof that the process made no other system call.

The MCP protocol and annotation semantics are documented by the
[Model Context Protocol project](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
and its [tool-annotations guidance](https://blog.modelcontextprotocol.io/posts/2026-03-16-tool-annotations/).
