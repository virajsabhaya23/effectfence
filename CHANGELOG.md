# Changelog

All notable changes will be documented here. This project follows Semantic Versioning and the Keep a Changelog structure.

## 0.4.4 - 2026-11-21

- Added `--sqlite-query DATABASE=SQL` (repeatable) to `mcp-check`, so database
  servers are verified against the rows a query returns instead of a sandbox
  file hash that their writes never touch. The flag replaces the sandbox
  observer; `--sandbox-observer` adds it back when both boundaries matter.
  Reports now list the configured observers and note when no filesystem
  boundary was watched, because a pass over rows says nothing about files.
- Synthesized arguments now give URL-shaped properties (`url`, `uri`, `href`,
  `endpoint`, `webhook`, `format: uri`) a real URL, reusing an example URL from
  the description when one is given, instead of an opaque probe string. On
  network tools that turned a rejected call into a real observation.
- SQL-shaped arguments (`sql`, `statement`, or a description mentioning SQL,
  tables, rows, columns, a database or a schema) now get `select 1 as probe`,
  which parses on SQLite, PostgreSQL and MySQL. A search tool's `query` is still
  an opaque probe string.

## 0.4.3 - 2026-11-21

- A tool that publishes no effect annotations and has no case `contract` is now
  reported as `NO_DECLARED_ANNOTATIONS` and is **not called**. Previously such a
  case was reported as `passed` with zero declarations checked: running against
  `duckduckgo-mcp-server` 0.7.0 produced a green report over three completely
  unchecked tools.
- Synthesized string arguments now reuse a quoted example from the property
  description (for example `'/mongodb/docs'` for a `libraryId`). On
  `@upstash/context7-mcp` 4.7.3 this turned a rejected `query-docs` call into a
  verified pass.
- Added `--env KEY=VALUE` (repeatable) to `mcp-scan` and `mcp-check`, so servers
  that require configuration can be checked in one step instead of only through a
  hand-written manifest.
- Documented validation against three servers outside the official set
  (Context7 4.7.3, duckduckgo-mcp-server 0.7.0, mcp-server-sqlite-npx 0.8.0).

## 0.4.2 - 2026-11-21

- Documentation-only release so the PyPI project page is correct: the README now
  documents `pip install safesink` as the install path, since 0.4.0 and 0.4.1 are
  published.

## 0.4.1 - 2026-11-21

- Fixed the shipped conformance examples: adding the `read_missing_file` fixture
  tool raised the advertised tool count to six, so `passing.json` failed its own
  coverage policy (and the `mcp-conformance` workflow) instead of passing. The
  example now also covers `expectToolError`, and `failing.json` fails only on the
  three dishonest tools rather than on coverage. A test now runs both shipped
  examples and asserts their documented verdicts.

## 0.4.0 - 2026-11-21

- Added `safesink mcp-check <command>`: scan and verify an MCP server in one
  step, with a temporary sandbox, probe fixtures, destructive tools skipped, and
  a report. This is now the documented first-run path; `mcp-scan` and
  `mcp-verify` remain for curated manifests and CI.
- `mcp-scan` now seeds the observed sandbox with probe fixtures (a text file, a
  valid 1x1 PNG, and a directory with a nested file) and points synthesized file
  arguments at them, distinguishes source from destination arguments, and marks
  every generated case with `generatedArguments: true`. On
  `@modelcontextprotocol/server-filesystem` 2.4.0 this removed 5 false findings
  out of 13 cases; the remaining case is reported as inconclusive.
- Added a third verdict, `inconclusive`, with exit code 3, for runs where nothing
  was contradicted but at least one case could not be verified. `pass` is 0 and
  `fail` stays 2. Generated cases whose arguments the server rejects are
  reported as `TOOL_ERROR_ON_GENERATED_ARGUMENTS` instead of a
  `TOOL_ERROR_EXPECTATION_MISMATCH` finding, and JUnit writes those cases as
  `skipped` rather than failed.
- `IDEMPOTENCY_NOT_EXERCISED` is now only reported for `idempotentHint: true`,
  instead of also for servers that explicitly declare `false`.
- Removed the citation/adoption scaffolding (`safesink citation`,
  `CITATION.cff`, `docs/CITING.md`, `docs/ADOPTION_EVIDENCE.md`,
  `docs/PRIOR_ART.md`, `BUILD_REPORT.md`) so the repository describes one tool
  instead of an originality record.
- Split the product in two: `safesink` now exposes only the MCP conformance
  verifier (`mcp-check`, `mcp-verify`, `mcp-scan`). The crash/retry simulator,
  SQLite effect ledger, reference sinks, benchmark corpus, and live
  Kafka/PostgreSQL adapter moved to the frozen `safesink.legacy` subpackage and
  are reachable through the separate `safesink-legacy` console script. See
  `docs/FROZEN_CRASH_RETRY.md`.
- Fixed the broken distribution metadata: `pyproject.toml` and the wheel
  metadata point at the repository that actually exists
  (`github.com/virajsabhaya23/effectfence`) instead of the nonexistent
  `virajsabhaya23/safesink`, and the README no longer tells readers to run a
  `pip install safesink` that 404s (the name is not on PyPI yet).
- MCP reports no longer hardcode version `0.2.0`. The JSON report's
  `runtime.version`, the SARIF driver version, the MCP `clientInfo` version, and
  the citation metadata now all read `safesink.__version__`.
- Added `--version` to the CLI (works as `safesink --version`, `python -m safesink
  --version`, and through the deprecated `effectfence` alias, which reports the
  program name it was invoked as).
- `effectfence` is now a thin deprecation shim that re-exports `safesink` instead of
  a second copy of the implementation, so fixes can no longer diverge between the
  two import paths. A compatibility test asserts every shimmed module re-exports
  the same public objects, that versions agree, and that both CLI entry points
  return identical verdict exit codes.
- CI and the local gates (format hygiene, lint, type-interface, compile, benchmark)
  now cover the primary `safesink` package as well as the alias; the type-interface
  gate discovers modules automatically instead of using a hand-maintained list.
- Fixed the installed `effectfence` console script: it was mapped to
  `safesink.cli:main`, so it advertised itself as `safesink` and skipped the
  deprecation warning. It now routes through `effectfence.cli:main`, and the
  packaging tests assert both entry points and that the backend version matches
  `safesink.__version__`.
- The unit suite now imports `safesink` directly; only the compatibility test
  exercises the deprecated `effectfence` alias.
- CI is verified to stay within a free GitHub account: public-repo standard
  `ubuntu-latest` runners only, read-only workflow permissions, per-job timeouts,
  a single benchmark artifact, 7-14 day artifact retention (the Free plan shares
  500 MB of artifact storage), and manual `workflow_dispatch` entry points. A new
  `tests/test_ci_free_tier.py` fails on macOS/Windows/larger runners, merge-queue
  triggers, CodeQL/Pages uploads, missing timeouts, missing permissions, or
  unbounded artifact retention. PyPI release metadata now names `safesink`.
- CI runs the gates and suite on Python 3.10, 3.12, and 3.13, and a separate job
  builds the wheel, installs it into a fresh virtual environment, and runs
  `safesink --version` plus the safe and unsafe fixtures through the installed
  console script. A test also asserts `pyproject.toml`'s version matches
  `safesink.__version__`.

- Added an observer non-interference control schedule. Every MCP case (and every
  ambiguous-result trial) now runs matched no-tool observation rounds using the
  exact same observer sequence before the tool is invoked. A state transition that
  reproduces without the tool is reported as
  `OBSERVER_OR_BACKGROUND_INTERFERENCE`, suppresses annotation-mismatch
  attribution for that window, and makes the case inconclusive instead of a pass
  or a false violation. Configure with `observerControlRounds` (default 1; set 0
  to opt out and forfeit causal attribution evidence).
- A case now passes only when it has neither violations nor inconclusive
  evidence, so unresolved checks can no longer be reported as verified.

- Added deterministic MCP ambiguous-result retry schedules with repeated,
  observer-backed classifications for committed results, no-effect timeouts,
  duplicate retries, and explicitly inconclusive outcomes.

- Reworked deterministic broker delivery so a redelivery count is an attempt
  budget, not a command to invoke a handler after durable progress. Trace
  evidence now records delivery eligibility, skipped deliveries, explicit
  progress-loss faults, and the final broker-delivery state.
- Replays that find durable idempotency or acceptance evidence now checkpoint
  and acknowledge the message, matching production consumer behavior.
- Added an MCP stdio conformance runner that negotiates protocol initialization,
  discovers paginated tools, invokes declared cases, and verifies observable
  effects against MCP tool annotations or manifest-owned contracts.
- Added bounded filesystem, read-only SQLite query, and allowlisted HTTP JSON
  observers with sensitive output redaction enabled by default.
- Added retry/idempotency, read-only, non-destructive, closed-world, tool-error,
  unknown-tool, and minimum-coverage checks.
- Added a versioned JSON Schema, public Python API, CLI, composite GitHub Action,
  and redacted JSON, JUnit, and SARIF reports.
- Added honest and deliberately dishonest MCP fixtures plus fourteen MCP,
  security, and packaging tests, bringing the automated suite to 32 tests.
- Completed the dependency-free PEP 517 backend for reproducible v0.2 wheels,
  packaged schemas, license metadata, and clean source distributions.
- Added copy-ready BibTeX/CFF/CSL-like JSON citation output and transparent
  adoption-evidence guidance. No DOI is claimed before archival publication.
- Added a live Apache Kafka protocol/PostgreSQL adapter for the
  sink-commit-before-offset-commit crash window.
- Added real `SIGKILL`, consumer restart/redelivery, broker-offset inspection,
  and PostgreSQL effect-count evidence.
- Added a positive unsafe naive-retry control and harness-validity gating.
- Added redacted JSON/NDJSON evidence artifacts and a SHA-256 report identifier.
- Added a local Docker Compose environment and a dedicated GitHub Actions proof.
- Added six unit tests for the live evidence oracle, trace, configuration, and
  credential redaction.

## 0.3.0 - 2026-08-29

- Renamed distribution to `safesink` (clean break from `effectfence` per issue #1). `pip install safesink`, `import safesink`, CLI `safesink`. `effectfence` remains as a deprecated alias (`import effectfence` warns, `effectfence` CLI forwards to `safesink`). Strategy `safesink` is an alias for `effectfence`; env vars `SAFESINK_*` preferred with `EFFECTFENCE_*` fallback.
- New package `safesink` mirrors `effectfence` 0.2.x; wheel now contains both for transition. `CITATION.cff` and `pyproject.toml` point at `virajsabhaya23/effectfence`, the repository that hosts this code.

## 0.1.0 - 2026-08-16

- Initial public beta release with the deterministic schedule verifier and
  30-case benchmark corpus.
