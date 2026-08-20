# Changelog

All notable changes will be documented here. This project follows Semantic Versioning and the Keep a Changelog structure.

## Unreleased

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

## 0.1.0 - 2026-08-16

- Initial public beta release with the deterministic schedule verifier and
  30-case benchmark corpus.
