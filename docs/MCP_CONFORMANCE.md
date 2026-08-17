# MCP side-effect conformance

EffectFence starts an MCP server over `stdio`, reads its `tools/list` metadata,
calls selected tools, and compares the declared effect hints with state changes
seen by explicitly configured observers. It is intended for MCP server authors,
platform teams, security reviewers, and CI maintainers.

MCP tool annotations are hints and must be treated as untrusted unless the server
itself is trusted. EffectFence supplies dynamic, observer-bounded evidence; it
does not turn those hints into a universal security boundary.

## Quick start

```bash
python -m pip install .
effectfence mcp-verify examples/mcp-conformance/passing.json \
  --out out/mcp/report.json \
  --junit out/mcp/junit.xml \
  --sarif out/mcp/report.sarif
```

The intentionally bad fixture demonstrates the failure rules and exits with
status 2:

```bash
effectfence mcp-verify examples/mcp-conformance/failing.json \
  --out out/mcp/failing.json
```

## Manifest

The versioned schema is
[`effectfence/schemas/mcp-manifest-v1.schema.json`](../effectfence/schemas/mcp-manifest-v1.schema.json).
Commands are arrays and are executed directly; EffectFence never invokes a
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

EffectFence also fails unexpected MCP tool errors, unknown tools, execution
errors, and tool-coverage policy violations.

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
- `sqlite-query`: opens the database read-only and rejects mutating SQL through
  SQLite's authorizer API.
- `http-json`: performs an allowlisted GET, blocks redirects, caps response size,
  and compares canonical JSON state.

Observers are `sensitive: true` by default. Sensitive reports omit paths and
state digests. Tool argument values and tool output values are never written to
reports. Only argument keys/types and MCP content types are retained.

## Python integration

```python
from effectfence import verify_manifest

report = verify_manifest("effectfence.mcp.json")
if report["verdict"] != "pass":
    raise RuntimeError(report["cases"])
```

The API returns the same JSON-serializable report used by the CLI. JUnit and
SARIF writers are available from `effectfence.mcp_reports`.

## CI integration

This repository includes a composite GitHub Action. A consuming repository can
pin a released commit or tag:

```yaml
- uses: virajsabhaya23/effectfence@v0.2.0
  with:
    manifest: effectfence.mcp.json
    out: out/effectfence/report.json
    junit: out/effectfence/junit.xml
    sarif: out/effectfence/report.sarif
```

Pin a full commit SHA in high-assurance environments. Preserve the JSON report
as a CI artifact and use the process exit code as the merge gate.

## Evidence boundary

The result is only as complete as the observer set. For example, a filesystem
observer cannot detect an HTTP call, and snapshot comparison cannot prove an
external read occurred. Add observers for every state boundary relevant to the
tool. A passing report means no mismatch was observed within that declared
boundary; it is not proof that the process made no other system call.

The MCP protocol and annotation semantics are documented by the
[Model Context Protocol project](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
and its [tool-annotations guidance](https://blog.modelcontextprotocol.io/posts/2026-03-16-tool-annotations/).
