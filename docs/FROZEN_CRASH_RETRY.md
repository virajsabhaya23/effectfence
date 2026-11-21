# Frozen: crash/retry schedule simulator

The crash/retry schedule simulator is **frozen**. It is no longer the product,
receives no new strategies, adapters, benchmark cases, or features, and is kept
only so earlier reports and evidence artifacts stay reproducible.

The supported product is the MCP side-effect conformance verifier:
[`safesink mcp-verify` / `safesink mcp-scan`](MCP_CONFORMANCE.md).

## What is frozen

Everything under `safesink/legacy/`, exposed only through the `safesink-legacy`
console script:

| Module | Contents |
| --- | --- |
| `simulator.py` | deterministic crash/retry schedules and the `safesink` reference strategy |
| `model.py` | `Scenario`, `TraceEvent`, `VerificationResult` |
| `ledger.py` | SQLite effect ledger (effects, dedupe, checkpoints, outbox, fences) |
| `io.py` | scenario JSON loading and report writing |
| `reports.py` | JUnit output for scenario verification |
| `sinks.py` | in-process ledger, HTTP idempotency-header, and DB-API 2.0 reference sinks |
| `httpdemo.py` | local HTTP demo sink used by the tests |
| `explore.py` | adverse-schedule exploration and failure minimization |
| `benchmark.py` | corpus runner for the 30-case benchmark |
| `live.py` | live Kafka/PostgreSQL `SIGKILL` killpoint adapter |

`benchmark/`, `examples/crash_after_effect.json`, and
`examples/safe_fenced_recovery.json` remain as the frozen fixtures for this half.
The `effectfence` import path and console script still forward here for the
modules that existed before the split.

## Running it

```bash
safesink-legacy verify examples/safe_fenced_recovery.json
safesink-legacy verify examples/crash_after_effect.json --out out/report.json --junit out/report.xml
safesink-legacy explore examples/crash_after_effect.json --out out/exploration.json
safesink-legacy benchmark benchmark/corpus.json --out benchmark/results.json
safesink-legacy live-kafka-postgres --strategy safesink --expect safe
```

`python -m safesink.legacy.cli <command>` is equivalent. The commands, exit
codes (`0` safe/pass, `2` unsafe/fail), and report fields are unchanged from
0.2.x, so existing pipelines keep working.

## Why it is frozen rather than deleted

- **Reproducibility.** Published 0.1.x/0.2.x reports and the benchmark numbers
  quote certificate hashes from this code; deleting it would orphan them.
- **It was not the differentiator.** The simulator validates its own model: the
  corpus declares each case unsafe or safe up front, and the same engine decides
  the verdict, so its precision/recall measures corpus authoring rather than
  detection of real systems. The MCP conformance checker instead measures a
  server the tool did not write.

## Known limitations (do not cite these as evidence of production safety)

- Deterministic model, not a replay of a real broker; the reference strategies
  are executable descriptions of patterns, not claims about a particular
  deployment.
- The 30-case benchmark is self-graded, as described above.
- The live adapter covers one boundary (after sink commit, before offset commit)
  on one stack (Apache Kafka + PostgreSQL) on a POSIX host.

See [EFFECT_SAFETY_PROTOCOL.md](EFFECT_SAFETY_PROTOCOL.md) and
[PRIOR_ART.md](PRIOR_ART.md) for the original protocol notes and prior-art
record, both retained as historical documents.
