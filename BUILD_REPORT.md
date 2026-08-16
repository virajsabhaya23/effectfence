# BUILD REPORT — OC-011 EffectFence

## Selected idea
OC-011 — EffectFence — Crash/Retry Side-Effect Safety Verifier

## Why selected
OC-010 SchemaWeave was the highest queued build (P0/95), but its mandatory validation requires a real Automerge baseline and the current Automerge package could not be retrieved in this runtime. The build queue therefore advanced to OC-011 (P0/94), whose required naive-retry, idempotency-key, and transactional-outbox reference baselines are executable locally without weakening the gate.

## Validated originality wedge
EffectFence does not claim transactional outbox, idempotency keys, at-least-once delivery, or fencing as new. Its contribution is an executable application-level verifier that explores crash/retry/redelivery/stale-retry schedules across durable checkpoints and external side effects, observes sink-side acceptance evidence, checks no-loss/no-duplicate/stale-writer invariants, and minimizes unsafe traces.

## Architecture
- `effectfence/model.py` — scenario and strategy contracts
- `effectfence/ledger.py` — SQLite durable checkpoints, dedupe evidence, accepted effects, outbox state, and fencing epochs
- `effectfence/sinks.py` — ledger, HTTP, and DB-API side-effect adapters
- `effectfence/simulator.py` — naive retry, idempotency-key, transactional-outbox, and EffectFence strategies
- `effectfence/explore.py` — bounded adverse-schedule exploration and counterexample minimization
- `effectfence/benchmark.py` — required 30-case benchmark plus ordinary happy-path baseline
- `effectfence/reports.py` — JSON/JUnit evidence
- `build_backend.py` — dependency-free offline PEP 517 wheel builder
- `.github/workflows/test.yml`, `Dockerfile`, protocol/prior-art docs, evidence manifests

## Verification environment
- Python: 3.13.5 (main, Jul 15 2026, 20:25:40) [GCC 14.2.0]
- Platform: Linux-6.18.35-x86_64-with-glibc2.41

## Verification results
- format-hygiene gate: PASS
- lint/static syntax gate: PASS
- type-interface gate: PASS
- compilation: PASS
- automated tests: 12/12 PASS
- safe EffectFence recovery E2E: PASS
- unsafe naive crash-after-effect E2E: PASS (expected exit 2)
- bounded schedule exploration/minimization: PASS
- JSON/JUnit output: PASS
- `pip install .` in a fresh virtual environment with no downloaded build dependencies: PASS
- wheel build in the same clean environment: PASS
- 30-case benchmark: PASS

## Measured benchmark
```json
{
  "cases": 30,
  "schedule_verifier": {
    "fn": 0,
    "fp": 0,
    "precision": 1.0,
    "recall": 1.0,
    "tn": 10,
    "tp": 20
  },
  "ordinary_happy_path": {
    "fp": 0,
    "recall": 0.0,
    "tp": 0
  },
  "by_strategy": {
    "effectfence": {
      "safe_cases": 4,
      "safe_flagged": 0,
      "unsafe_cases": 0,
      "unsafe_detected": 0
    },
    "idempotency_key": {
      "safe_cases": 2,
      "safe_flagged": 0,
      "unsafe_cases": 8,
      "unsafe_detected": 8
    },
    "naive_retry": {
      "safe_cases": 2,
      "safe_flagged": 0,
      "unsafe_cases": 8,
      "unsafe_detected": 8
    },
    "transactional_outbox": {
      "safe_cases": 2,
      "safe_flagged": 0,
      "unsafe_cases": 4,
      "unsafe_detected": 4
    }
  }
}
```

## Benchmark interpretation
The 20 unsafe cases cover crash-after-effect/before-progress, lost acknowledgement and redelivery, transactional-outbox relay crash after sink acceptance, stale retry, and finite idempotency-evidence expiry. Ten cases are safe controls. The ordinary happy-path baseline runs the same strategies without adverse crash/retry schedule dimensions and detects 0/20 unsafe schedules. This baseline is not represented as native Kafka, SQS, or Debezium test output.

## Exact commands
Install: `python -m pip install .`

Format: `python scripts/format_check.py`

Lint: `python scripts/lint.py`

Type-interface check: `python scripts/typecheck.py`

Test: `python -m unittest discover -s tests -v`

Run safe control: `effectfence verify examples/safe_fenced_recovery.json`

Run unsafe reproduction: `effectfence verify examples/crash_after_effect.json --out out/report.json --junit out/report.xml`

Explore schedules: `effectfence explore examples/crash_after_effect.json --out out/exploration.json`

Benchmark: `effectfence benchmark benchmark/corpus.json --out benchmark/results.json`

## Limitations
The reference benchmark uses deterministic local delivery/sink models rather than a live Kafka/SQS cluster. HTTP and DB-API sink adapters are runnable, but broker/cloud-native adapters require external infrastructure. Results are coverage-qualified by the explored schedules, declared dedupe/evidence lifetime, and sink acceptance semantics.

## Evidence preserved
The repository stores the versioned Effect Safety Protocol, benchmark corpus/results, source provenance, exact test cases, deterministic certificates, SHA-256 file manifest, environment versions, and prior-art boundary.

## Recommended first public release/adoption steps
Publish the protocol and corpus first; then add native Kafka/SQS/Debezium/Temporal adapters, run the same schedules against staging infrastructure, and submit minimized externally confirmed counterexamples before making any field-level impact or adoption claim.
