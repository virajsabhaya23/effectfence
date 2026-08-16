# EffectFence

Crash/retry side-effect safety verifier for event-driven handlers.

At-least-once queues, consumer retries, and transactional outbox patterns still leave application developers with an important question:

> If the process crashes at every durable boundary and the message is redelivered later, can the external effect be lost, duplicated, or accepted by a stale retry?

EffectFence explores those schedules deterministically and verifies the **observable sink effects**.

## Quick start

Python 3.10+, no runtime dependencies. The repository includes a dependency-free PEP 517 build backend, so installation works offline from a clean virtual environment.

```bash
python -m pip install https://github.com/virajsabhaya23/effectfence/releases/download/v0.1.0/effectfence-0.1.0-py3-none-any.whl
effectfence verify examples/safe_fenced_recovery.json
effectfence verify examples/crash_after_effect.json --out out/report.json --junit out/report.xml
effectfence explore examples/crash_after_effect.json --out out/exploration.json
effectfence benchmark benchmark/corpus.json --out benchmark/results.json
```

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

## Adapters

- persistent SQLite effect ledger;
- in-process ledger sink;
- local/remote HTTP sink using idempotency headers;
- DB-API 2.0 sink usable with sqlite3 and PostgreSQL DB-API drivers;
- SQS/Kafka-like delivery semantics are represented by the deterministic schedule engine rather than by a broker emulator.

## Reports

JSON and JUnit include the entire trace, violations, accepted-effect count, and deterministic SHA-256 certificate.

## Benchmark

The 30-case corpus contains 20 unsafe schedules grounded in documented Kafka/SQS/outbox/fencing failure classes and 10 safe controls. Run it with:

```bash
python -m effectfence benchmark benchmark/corpus.json --out benchmark/results.json
```

## Security

EffectFence's default verification path runs only local deterministic fixtures. HTTP adapter destinations are explicit and no credentials are required. The project never mutates production brokers or databases automatically.

## Limitations

The local benchmark validates the protocol and reference strategies; it is not a live Kafka, SQS, or Debezium deployment benchmark. Real integrations should record broker/sink versions and replay the same Effect Safety Protocol against staging infrastructure.
