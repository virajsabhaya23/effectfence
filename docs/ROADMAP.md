# Roadmap

SafeSink checks one thing and refuses to guess at the rest: a tool's declared
effects must match what an observer saw change. The roadmap widens what can be
**observed** and how easily a team can **adopt the gate** - never by weakening
that rule. `inconclusive` stays a distinct verdict, and a passing run stays a
statement about one pinned version of one server.

## Now

Shipped and documented:

| Capability | Where |
| --- | --- |
| One-step discovery and verification (`mcp-check`, `mcp-verify`) | [`docs/MCP_CONFORMANCE.md`](MCP_CONFORMANCE.md) |
| Reviewable scaffolding (`safesink init`) and declaration lint (`safesink lint`) | [`docs/LINT_RULES.md`](LINT_RULES.md) |
| CLI contracts for unannotated servers (`--contract`, `--contract-all`) | [`docs/MCP_CONFORMANCE.md`](MCP_CONFORMANCE.md) |
| Observers: filesystem, SQLite, PostgreSQL, MySQL, HTTP JSON | [`docs/MCP_CONFORMANCE.md`](MCP_CONFORMANCE.md) |
| Remote servers over streamable HTTP with a host allowlist | [`docs/MCP_CONFORMANCE.md`](MCP_CONFORMANCE.md) |
| Baselines, reasoned suppressions with expiry, run budgets, per-case restart | [`docs/MCP_CONFORMANCE.md`](MCP_CONFORMANCE.md) |
| Trust score, Markdown report, job summary, sticky PR comment, one-step Action | [`docs/GITHUB_ACTION.md`](GITHUB_ACTION.md) |
| Signed, offline-verifiable certificates | [`docs/CERTIFICATES.md`](CERTIFICATES.md) |
| `safesink-mcp`: lint inside an agent, execution opt-in | [`docs/MCP_SERVER.md`](MCP_SERVER.md) |
| Badge and a reproducible pinned leaderboard | [`docs/LEADERBOARD.md`](LEADERBOARD.md) |
| Container image, unprivileged, with untrusted-server guidance | [`docs/CONTAINER.md`](CONTAINER.md) |
| Release automation with a post-publish install check | [`docs/RELEASING.md`](RELEASING.md) |

## Next

In rough order:

1. **`pull_request_target`-safe reporting.** Post the sticky comment from a
   separate workflow so fork PRs get a report without giving fork code a
   writable token.
2. **More observers.** Provider APIs (object storage, a hosted queue) and
   MongoDB, each following the same rule as the SQL observers: credentials from
   the environment only, read-only snapshots, optional driver.
3. **Manifest schema v2.** A migration path for case-level contracts,
   transport credentials by reference, and per-case timeouts without breaking
   `safesink.mcp.v1` manifests.
4. **Report comparison across versions.** `--baseline` compares verdicts; the
   next step is a diff of *which* effect changed between two runs of two
   versions.
5. **Agent-native advice.** Have `lint_manifest` return a concrete contract
   suggestion per finding, not only a code.

## Later

- **Gateway mode.** Verify a whole gateway's aggregated tool list, attributing
  each tool to the upstream server it came from.
- **Policy packs.** Shareable contract presets per ecosystem (filesystem, git,
  database) so a team does not start from an empty contract.
- **Continuous evidence.** Periodic pinned scans with a signed certificate per
  run, so "this version passed on this date" is citable.
- **Non-Python SDKs.** The report format is the integration point; a thin
  verifier in another runtime is possible once the schema stabilises.

## Not planned

- Inferring effects without observing them, or treating "no observer changed"
  as proof of a read-only tool.
- Calling tools the server did not annotate, unless a human wrote the contract.
- Reporting a score without saying which version and which observers produced
  it.
- Probabilistic findings. A finding is a contradiction between a declaration
  and an observation, or it is not a finding.
