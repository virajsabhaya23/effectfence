# Static lint rules

`safesink lint` reads `tools/list` and nothing else. It starts the server just
long enough to list tools, then stops; no `tools/call` is ever made and no
observer is consulted. That makes it the first command to run against an unknown
server, and the one an agent can run safely.

```bash
safesink lint -- npx -y @modelcontextprotocol/server-filesystem "$PWD/sandbox"
safesink lint --json --manifest safesink.mcp.json
safesink lint --min-annotation-score 80 -- npx -y some-server
```

The result is an annotation score out of 100 with a per-rule breakdown. A lint
finding is a statement about **what the declarations say**, never proof of what
the tool does: a lint pass cannot certify a server, and a lint finding is a
reason to run `mcp-check`, not a verdict.

## Rules

| Code | Deduction | What it means |
| --- | --- | --- |
| `NO_DECLARED_ANNOTATIONS` | 5 | The tool publishes none of the four effect hints, so nothing can be verified. |
| `READ_ONLY_NAME_MISMATCH` | 20 | The name implies a write (`create_`, `delete_`, `send_`, `write_`, `update_`, …) while `readOnlyHint: true` promises none. |
| `DESTRUCTIVE_NAME_WITH_NON_DESTRUCTIVE_HINT` | 20 | The name implies deletion or removal while `destructiveHint: false` promises nothing is removed. |
| `CONTRADICTORY_DESTRUCTIVE_READ_ONLY` | 25 | `readOnlyHint: true` and `destructiveHint: true` are both set. |
| `OPEN_WORLD_URL_ARGUMENT_MISMATCH` | 10 | The tool takes a URL-shaped argument (`url`, `uri`, `href`, `endpoint`, `webhook`, `format: uri`) but declares `openWorldHint: false`. |
| `MUTATING_WITHOUT_IDEMPOTENT_HINT` | 10 | A tool that is not read-only does not state whether a retry is safe. |
| `IDEMPOTENT_WITH_VOLATILE_ARGUMENT` | 15 | `idempotentHint: true` together with an argument named random, uuid, nonce, timestamp, now or seed, which changes on every call. |

`NO_DECLARED_ANNOTATIONS` is reported alone for a tool: when there is no claim,
reporting an omission under every other rule would drown the one message that
matters.

## Why each rule is worded as a warning

- **`NO_DECLARED_ANNOTATIONS`** — legitimate for servers written before
  annotations existed. The fix is a contract (`--contract`), not a rewrite.
- **`READ_ONLY_NAME_MISMATCH`** — verb prefixes can mislead. `create_index_if_missing`
  on a cache warm-up is plausibly read-only.
- **`DESTRUCTIVE_NAME_WITH_NON_DESTRUCTIVE_HINT`** — a soft delete or an archive
  tool removes nothing observable and can honestly declare `false`.
- **`CONTRADICTORY_DESTRUCTIVE_READ_ONLY`** — the only rule with no false-positive
  caveat: the two hints cannot both be true.
- **`OPEN_WORLD_URL_ARGUMENT_MISMATCH`** — a URL may be parsed or compared
  without being fetched.
- **`MUTATING_WITHOUT_IDEMPOTENT_HINT`** — the hint is optional in the protocol;
  this is an omission, not a lie.
- **`IDEMPOTENT_WITH_VOLATILE_ARGUMENT`** — the value may be ignored by the tool,
  or used only for tracing.

## What lint deliberately does not do

- It does not call a tool, so it cannot observe an effect.
- It does not start hooks, observers or a sandbox.
- It does not read server source, so a deliberate lie that matches the name is
  invisible to it. `mcp-check` is the tool that compares claims with effects.

## Relationship to `mcp-check`

`lint` answers "is this worth checking?"; `mcp-check` answers "is the claim
true?". The two share the same vocabulary for a reason: a lint finding names the
check that would prove or disprove it, and the annotation score in a lint result
is deliberately not the trust score in a verification report. Only observed
evidence produces a trust score.
