# EffectFence

Crash/retry and MCP side-effect conformance verifier.

At-least-once queues, consumer retries, and transactional outbox patterns still leave application developers with an important question:

> If the process crashes at every durable boundary and the message is redelivered later, can the external effect be lost, duplicated, or accepted by a stale retry?

EffectFence explores those schedules deterministically and verifies the **observable sink effects**.

It also tests whether MCP tools behave like their declared `readOnlyHint`,
`destructiveHint`, `idempotentHint`, and `openWorldHint` annotations within an
explicit filesystem, SQLite, or HTTP JSON observation boundary.

## Deterministic quick start

Python 3.10+, no runtime dependencies. The repository includes a dependency-free PEP 517 build backend, so installation works offline from a clean virtual environment.

```bash
python -m pip install https://github.com/virajsabhaya23/effectfence/releases/download/v0.1.0/effectfence-0.1.0-py3-none-any.whl
effectfence verify examples/safe_fenced_recovery.json
effectfence verify examples/crash_after_effect.json --out out/report.json --junit out/report.xml
effectfence explore examples/crash_after_effect.json --out out/exploration.json
effectfence benchmark benchmark/corpus.json --out benchmark/results.json
```

## MCP conformance quick start

Run the included honest fixture and emit JSON, JUnit, and SARIF evidence:

```bash
effectfence mcp-verify examples/mcp-conformance/passing.json \
  --out out/mcp/report.json \
  --junit out/mcp/junit.xml \
  --sarif out/mcp/report.sarif
```

The deliberately dishonest fixture exits with status 2 and demonstrates the
three primary findings:

```bash
effectfence mcp-verify examples/mcp-conformance/failing.json \
  --out out/mcp/failing.json
```

Companies can invoke the same verifier through `from effectfence import
verify_manifest`, the CLI, or the included composite GitHub Action. See the
[MCP conformance guide](docs/MCP_CONFORMANCE.md) for the versioned manifest,
ambiguous-result retry fault schedules,
observer boundary, security model, and CI example.

## Live Kafka/PostgreSQL proof

Version 0.2 adds a real-process killpoint adapter. It publishes a probe to Apache
Kafka, commits its external effect in PostgreSQL, sends `SIGKILL` to the consumer
before its Kafka offset commit, starts a replacement consumer, and evaluates the
result from broker and sink state.

The included environment uses only local open-source containers; no paid cloud
resource is required.

```bash
python -m pip install ".[live-kafka-postgres]"
docker compose -f docker-compose.live.yml up -d --wait

export EFFECTFENCE_POSTGRES_DSN='postgresql://effectfence:effectfence-local-only@127.0.0.1:5432/effectfence'

# Stable effect identity: two delivery attempts, exactly one accepted effect.
effectfence live-kafka-postgres \
  --strategy effectfence \
  --expect safe \
  --broker-version-label apache/kafka:4.3.1

# Positive unsafe control: the same crash window must create a duplicate.
effectfence live-kafka-postgres \
  --strategy naive \
  --expect unsafe \
  --broker-version-label apache/kafka:4.3.1
```

Each run writes a redacted `config.json`, a durable `trace.ndjson`, and a
content-addressed `report.json` below `out/live/<run-id>/`. The command succeeds
only when the infrastructure/restart harness is healthy **and** the observed
safe/unsafe outcome matches `--expect`.

See [the live adapter guide](docs/LIVE_KAFKA_POSTGRES.md) for the oracle,
security boundary, CI setup, and cleanup.

## Strategies included for comparison

- `naive_retry`
- `idempotency_key`
- `transactional_outbox`
- `effectfence` — sink-side acceptance evidence + stable effect identity + monotonic fences

These are executable **reference strategies**, not claims that a particular Kafka/SQS/Debezium production deployment behaves identically.

## Implemented failure schedules

- crash before/after external effect;
- crash before/after durable checkpoint;
- crash before/after acknowledgement;
- lost acknowledgement + redelivery;
- concurrent recovery workers;
- stale in-flight retry;
- dedupe/evidence expiry;
- outbox relay crash after sink acceptance but before outbox progress update.

Ordinary `redeliveries` are a bounded broker attempt budget. The simulator will
not invoke a handler again after a durable checkpoint; only an explicit
progress-loss, concurrent-recovery, or stale-inflight fault can make another
delivery eligible. Every eligibility decision is preserved in the trace.

## Adapters

- persistent SQLite effect ledger;
- in-process ledger sink;
- local/remote HTTP sink using idempotency headers;
- DB-API 2.0 sink usable with sqlite3 and PostgreSQL DB-API drivers;
- live Apache Kafka protocol + PostgreSQL `SIGKILL` adapter;
- SQS-like delivery semantics remain represented by the deterministic schedule engine.

## Reports

The deterministic schedule verifier emits JSON and JUnit with the entire trace,
violations, accepted-effect count, and SHA-256 certificate. MCP conformance emits
redacted JSON, JUnit, and SARIF; argument/output values and sensitive observer
digests are excluded by default.

## Citation and adoption

Run `effectfence citation --format bibtex` or use [`CITATION.cff`](CITATION.cff).
The [citation guide](docs/CITING.md) explains reproducible citation metadata, and
the [adoption evidence guide](docs/ADOPTION_EVIDENCE.md) separates independent
impact evidence from repository vanity metrics. No archival DOI is claimed yet.

## Benchmark

The 30-case corpus contains 20 unsafe schedules grounded in documented Kafka/SQS/outbox/fencing failure classes and 10 safe controls. Run it with:

```bash
python -m effectfence benchmark benchmark/corpus.json --out benchmark/results.json
```

## Security

EffectFence's default verification path runs only local deterministic fixtures.
MCP commands are executed without a shell and receive a restricted environment;
only explicitly inherited variables are passed. HTTP observer hosts are
allowlisted and redirects are blocked. The live command creates one topic and
three namespaced PostgreSQL tables, and should be pointed only at disposable
local or staging infrastructure. Credentials are never written to evidence
artifacts.

## Limitations

The 30-case benchmark still uses deterministic models. The live adapter currently
covers one high-value Kafka/PostgreSQL boundary—after sink commit and before
offset commit—on a POSIX host. MCP findings are bounded by configured observers:
snapshot comparison cannot detect unobserved side effects or prove that an
external read occurred. SQS, Debezium, Temporal, multi-partition rebalances,
network partitions, and sink failover remain future integration work.
