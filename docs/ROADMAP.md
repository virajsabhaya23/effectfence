# Roadmap

SafeSink checks one thing and refuses to guess at the rest: a tool's declared
effects must match what an observer saw change. The roadmap widens what can be
**observed** and how easily a team can **adopt the gate** - never by weakening
that rule. `inconclusive` stays a distinct verdict, and a passing run stays a
statement about one pinned version of one server.

## Phases

The work that produced the current state, with the issue behind each item. This
is the reasoning, not a changelog: Phase 1 made the check true for the servers
people already run, Phase 2 made it a gate a team could adopt without a cleanup
sprint, Phase 3 made it spread.

### Phase 1 - trustworthy for real servers

| Item | Issue |
| --- | --- |
| CLI contracts so unannotated servers can be verified | [#36](https://github.com/virajsabhaya23/safesink/issues/36) |
| PostgreSQL and MySQL observers | [#34](https://github.com/virajsabhaya23/safesink/issues/34) |
| Streamable HTTP transport | [#33](https://github.com/virajsabhaya23/safesink/issues/33) |
| `--dry-run` plan preview | [#35](https://github.com/virajsabhaya23/safesink/issues/35) |
| Destructive tools against a real database | [#31](https://github.com/virajsabhaya23/safesink/issues/31) |
| Argument synthesis: prompt paths, write roles, directories | [#27](https://github.com/virajsabhaya23/safesink/issues/27), [#28](https://github.com/virajsabhaya23/safesink/issues/28), [#29](https://github.com/virajsabhaya23/safesink/issues/29) |
| Diffable failures: stderr excerpt, phase and case | [#30](https://github.com/virajsabhaya23/safesink/issues/30) |

Why first: every later claim rests on the check being true for a server someone
actually runs, not only for the fixtures.

### Phase 2 - a gate teams can adopt

| Item | Issue |
| --- | --- |
| Baseline and `--new-only` | [#37](https://github.com/virajsabhaya23/safesink/issues/37) |
| Reasoned suppressions with expiry | [#38](https://github.com/virajsabhaya23/safesink/issues/38) |
| Run budget and per-case restart | [#32](https://github.com/virajsabhaya23/safesink/issues/32), [#40](https://github.com/virajsabhaya23/safesink/issues/40) |
| Trust score and `--min-score` | [#44](https://github.com/virajsabhaya23/safesink/issues/44) |
| Markdown report, job summary, PR comment, one-step action | [#42](https://github.com/virajsabhaya23/safesink/issues/42), [#48](https://github.com/virajsabhaya23/safesink/issues/48) |
| Container image and untrusted-server guidance | [#41](https://github.com/virajsabhaya23/safesink/issues/41) |

Why second: adoption fails on noise and safety, not on missing findings.

### Phase 3 - reach

| Item | Issue |
| --- | --- |
| `safesink lint`: zero-execution first run | [#43](https://github.com/virajsabhaya23/safesink/issues/43) |
| `safesink init`: reviewable scaffold | [#39](https://github.com/virajsabhaya23/safesink/issues/39) |
| Badge and reproducible pinned leaderboard | [#45](https://github.com/virajsabhaya23/safesink/issues/45) |
| Signed certificates anyone can verify | [#46](https://github.com/virajsabhaya23/safesink/issues/46) |
| Lint inside an agent, execution opt-in | [#47](https://github.com/virajsabhaya23/safesink/issues/47) |
| Release automation with a post-publish check | [#50](https://github.com/virajsabhaya23/safesink/issues/50) |
| Naming, badges and public direction | [#49](https://github.com/virajsabhaya23/safesink/issues/49), [#51](https://github.com/virajsabhaya23/safesink/issues/51), [#52](https://github.com/virajsabhaya23/safesink/issues/52) |

Why third: none of it matters until the check is true and the gate is cheap.

## Naming

The distribution is `safesink`; the legacy `effectfence` import package and CLI
alias remain for compatibility only. SafeSink is unrelated to the Rust/npm
project of the same former name - see the "Name" section of the README.

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
