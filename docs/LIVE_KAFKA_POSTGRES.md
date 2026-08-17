# Live Kafka/PostgreSQL killpoint proof

This adapter turns EffectFence's most important modeled counterexample into a
repeatable integration test against real infrastructure.

## What it proves

The controller performs the following sequence:

1. Create a one-partition topic and publish a uniquely identified probe.
2. Start a consumer with Kafka auto-commit disabled.
3. Commit an attempted effect and its accepted sink record in PostgreSQL.
4. Append and `fsync` the sink-commit trace event.
5. Send `SIGKILL` to that consumer before it can commit its Kafka offset.
6. Start a new consumer in the same group and observe redelivery.
7. Commit the recovered offset and query both Kafka and PostgreSQL.

The first process cannot run cleanup code after `SIGKILL`. Kafka therefore
replays from the last committed group offset. The PostgreSQL transaction remains
durable and is visible to the replacement consumer.

## Oracle

A run is accepted only if the harness proves all of these facts:

- the first consumer exited due to `SIGKILL`;
- PostgreSQL recorded at least two delivery attempts;
- the replacement consumer exited successfully;
- Kafka's committed group offset is exactly the produced offset plus one.

After those checks pass, the effect oracle is:

- `effectfence`: exactly one accepted effect is **safe**;
- `naive`: two accepted effects are the required **unsafe control**.

An unsafe expectation never passes because of a broken container, missing
redelivery, or failed recovery worker. This distinction prevents infrastructure
failures from being misreported as product findings.

## Local run

Requirements are Python 3.10+, Docker with Compose, and a POSIX host.

```bash
python -m pip install ".[live-kafka-postgres]"
docker compose -f docker-compose.live.yml up -d --wait
export EFFECTFENCE_POSTGRES_DSN='postgresql://effectfence:effectfence-local-only@127.0.0.1:5432/effectfence'

effectfence live-kafka-postgres \
  --strategy effectfence \
  --expect safe \
  --run-id local-safe \
  --broker-version-label apache/kafka:4.3.1

effectfence live-kafka-postgres \
  --strategy naive \
  --expect unsafe \
  --run-id local-naive-control \
  --broker-version-label apache/kafka:4.3.1
```

When finished, `docker compose -f docker-compose.live.yml down -v` removes the
two local containers, network, and disposable PostgreSQL volume.

## Evidence artifacts

Every run creates `out/live/<run-id>/` containing:

- `config.json`: non-secret inputs; the PostgreSQL DSN is deliberately omitted;
- `trace.ndjson`: append-only, `fsync`-backed controller and worker events;
- `killpoint.claimed`: durable proof that the single-use killpoint was claimed;
- `report.json`: broker offset, PostgreSQL counts, process exit codes, client and
  server versions, violations, and a SHA-256 evidence digest.

The digest covers the report payload before the digest field is added. It is an
integrity identifier for that live observation, not a claim that timestamps or
broker-assigned offsets are deterministic across runs.

## Infrastructure safety

Do not target a production broker or database. The adapter creates a topic and
the following tables in the configured database:

- `effectfence_live_runs`
- `effectfence_live_attempts`
- `effectfence_live_effects`

Rows are isolated by `run_id`; existing rows and topics are not deleted. The
controller sends `SIGKILL` only to its own child consumer process. It does not
stop Kafka or PostgreSQL.

The bundled GitHub Actions job uses standard public-repository runners and
ephemeral service containers. It uploads both safe and unsafe-control evidence
for 30 days.

## Semantics and references

Kafka documents that a replacement consumer resumes at the last committed group
offset after a crash, and recommends disabling auto-commit for manual offset
control. Psycopg documents that a successful connection context commits its
transaction when the block exits. The adapter makes the sink commit happen
before the manual Kafka commit, then kills the process in that gap.

- [Apache Kafka Docker image](https://kafka.apache.org/quickstart/)
- [Confluent Python client overview](https://docs.confluent.io/kafka-clients/python/current/overview.html)
- [Kafka consumer offset management](https://docs.confluent.io/platform/current/clients/consumer.html)
- [Psycopg transaction management](https://www.psycopg.org/psycopg3/docs/basic/transactions.html)
