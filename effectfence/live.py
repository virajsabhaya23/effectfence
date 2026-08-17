from __future__ import annotations

import hashlib
import importlib.metadata
import json
import multiprocessing
import os
import re
import signal
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")


@dataclass(frozen=True)
class LiveConfig:
    bootstrap_servers: str
    postgres_dsn: str
    topic: str
    group_id: str
    run_id: str
    message_id: str
    strategy: str
    expected_outcome: str
    artifact_dir: Path
    timeout_seconds: float = 90.0
    broker_version_label: str = "external/unknown"


def default_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"live-{stamp}-{uuid.uuid4().hex[:8]}"


def validate_config(config: LiveConfig) -> None:
    for label, value in (
        ("run ID", config.run_id),
        ("topic", config.topic),
        ("group ID", config.group_id),
    ):
        if not RUN_ID_PATTERN.fullmatch(value):
            raise ValueError(
                f"{label} must start with a letter or digit and contain only "
                "letters, digits, dot, underscore, or hyphen (maximum 120 characters)"
            )
    if config.strategy not in {"effectfence", "naive"}:
        raise ValueError("strategy must be 'effectfence' or 'naive'")
    if config.expected_outcome not in {"safe", "unsafe"}:
        raise ValueError("expected outcome must be 'safe' or 'unsafe'")
    if config.timeout_seconds <= 0:
        raise ValueError("timeout must be greater than zero")


def require_live_dependencies() -> tuple[Any, Any]:
    try:
        import confluent_kafka
        import confluent_kafka.admin
    except ImportError as error:
        raise RuntimeError(
            "live Kafka support is not installed; run "
            "'python -m pip install \"effectfence[live-kafka-postgres]\"'"
        ) from error
    try:
        import psycopg
    except ImportError as error:
        raise RuntimeError(
            "live PostgreSQL support is not installed; run "
            "'python -m pip install \"effectfence[live-kafka-postgres]\"'"
        ) from error
    return confluent_kafka, psycopg


def append_trace(path: Path, actor: str, event: str, **detail: Any) -> None:
    record = {
        "at": datetime.now(timezone.utc).isoformat(),
        "actor": actor,
        "event": event,
        "detail": detail,
    }
    data = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _wait_for_services(config: LiveConfig, kafka: Any, psycopg: Any) -> str:
    deadline = time.monotonic() + config.timeout_seconds
    kafka_error: Exception | None = None
    postgres_error: Exception | None = None
    postgres_version = "unknown"
    while time.monotonic() < deadline:
        try:
            kafka.admin.AdminClient(
                {"bootstrap.servers": config.bootstrap_servers}
            ).list_topics(timeout=3)
            kafka_error = None
        except kafka.KafkaException as error:
            kafka_error = error
        try:
            with (
                psycopg.connect(
                    config.postgres_dsn, connect_timeout=3
                ) as connection,
                connection.cursor() as cursor,
            ):
                cursor.execute("SHOW server_version")
                row = cursor.fetchone()
                postgres_version = str(row[0]) if row else "unknown"
            postgres_error = None
        except psycopg.Error as error:
            postgres_error = error
        if kafka_error is None and postgres_error is None:
            return postgres_version
        time.sleep(1)
    failures = []
    if kafka_error is not None:
        failures.append(f"Kafka unavailable: {type(kafka_error).__name__}: {kafka_error}")
    if postgres_error is not None:
        failures.append(
            f"PostgreSQL unavailable: {type(postgres_error).__name__}: {postgres_error}"
        )
    raise TimeoutError("; ".join(failures))


def _initialize_database(config: LiveConfig, psycopg: Any) -> None:
    statements = (
        """
        CREATE TABLE IF NOT EXISTS effectfence_live_runs (
            run_id TEXT PRIMARY KEY,
            strategy TEXT NOT NULL,
            topic TEXT NOT NULL,
            group_id TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS effectfence_live_attempts (
            run_id TEXT NOT NULL REFERENCES effectfence_live_runs(run_id),
            worker_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            topic TEXT NOT NULL,
            partition_id INTEGER NOT NULL,
            broker_offset BIGINT NOT NULL,
            attempted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (run_id, worker_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS effectfence_live_effects (
            sequence BIGSERIAL PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES effectfence_live_runs(run_id),
            message_id TEXT NOT NULL,
            effect_key TEXT NOT NULL,
            worker_id TEXT NOT NULL,
            accepted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (run_id, effect_key)
        )
        """,
    )
    with (
        psycopg.connect(config.postgres_dsn) as connection,
        connection.cursor() as cursor,
    ):
        for statement in statements:
            cursor.execute(statement)
        cursor.execute(
            """
            INSERT INTO effectfence_live_runs(run_id, strategy, topic, group_id)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (run_id) DO NOTHING
            """,
            (config.run_id, config.strategy, config.topic, config.group_id),
        )


def _create_topic(config: LiveConfig, kafka: Any) -> None:
    admin = kafka.admin.AdminClient({"bootstrap.servers": config.bootstrap_servers})
    metadata = admin.list_topics(timeout=10)
    if config.topic in metadata.topics:
        return
    futures = admin.create_topics(
        [kafka.admin.NewTopic(config.topic, num_partitions=1, replication_factor=1)]
    )
    try:
        futures[config.topic].result(timeout=15)
    except kafka.KafkaException:
        metadata = admin.list_topics(timeout=10)
        if config.topic not in metadata.topics:
            raise


def _produce_probe(config: LiveConfig, kafka: Any) -> tuple[int, int]:
    delivered: list[tuple[int, int]] = []
    delivery_errors: list[str] = []

    def on_delivery(error: Any, message: Any) -> None:
        if error is not None:
            delivery_errors.append(str(error))
        else:
            delivered.append((message.partition(), message.offset()))

    producer = kafka.Producer({"bootstrap.servers": config.bootstrap_servers})
    payload = json.dumps(
        {
            "effectfence_run_id": config.run_id,
            "message_id": config.message_id,
            "payload": {"operation": "charge", "amount_minor": 1700},
        },
        sort_keys=True,
    )
    producer.produce(
        config.topic,
        key=config.message_id.encode(),
        value=payload.encode(),
        on_delivery=on_delivery,
    )
    remaining = producer.flush(timeout=15)
    if remaining or delivery_errors or len(delivered) != 1:
        raise RuntimeError(
            "probe delivery failed: "
            f"remaining={remaining}, errors={delivery_errors}, delivered={delivered}"
        )
    return delivered[0]


def _record_effect(
    config: LiveConfig,
    psycopg: Any,
    worker_id: str,
    partition: int,
    offset: int,
) -> bool:
    stable_key = f"{config.run_id}:{config.message_id}"
    effect_key = (
        stable_key if config.strategy == "effectfence" else f"{stable_key}:{worker_id}"
    )
    with (
        psycopg.connect(config.postgres_dsn) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute(
            """
            INSERT INTO effectfence_live_attempts(
                run_id, worker_id, message_id, topic, partition_id, broker_offset
            ) VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                config.run_id,
                worker_id,
                config.message_id,
                config.topic,
                partition,
                offset,
            ),
        )
        cursor.execute(
            """
            INSERT INTO effectfence_live_effects(
                run_id, message_id, effect_key, worker_id
            ) VALUES (%s, %s, %s, %s)
            ON CONFLICT (run_id, effect_key) DO NOTHING
            RETURNING sequence
            """,
            (config.run_id, config.message_id, effect_key, worker_id),
        )
        accepted = cursor.fetchone() is not None
    return accepted


def _claim_killpoint(marker: Path) -> bool:
    try:
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    try:
        os.write(descriptor, b"claimed\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return True


def _worker_entry(config: LiveConfig, worker_id: str, crash_after_sink: bool) -> None:
    kafka, psycopg = require_live_dependencies()
    trace_path = config.artifact_dir / "trace.ndjson"
    consumer = kafka.Consumer(
        {
            "bootstrap.servers": config.bootstrap_servers,
            "group.id": config.group_id,
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
            "session.timeout.ms": 6000,
            "heartbeat.interval.ms": 2000,
        }
    )
    append_trace(trace_path, worker_id, "consumer_starting")
    try:
        consumer.subscribe([config.topic])
        deadline = time.monotonic() + config.timeout_seconds
        while time.monotonic() < deadline:
            message = consumer.poll(1.0)
            if message is None:
                continue
            if message.error():
                if message.error().code() == kafka.KafkaError._PARTITION_EOF:
                    continue
                raise RuntimeError(str(message.error()))
            payload = json.loads(message.value().decode())
            if payload.get("effectfence_run_id") != config.run_id:
                consumer.commit(message=message, asynchronous=False)
                continue
            append_trace(
                trace_path,
                worker_id,
                "message_received",
                partition=message.partition(),
                offset=message.offset(),
            )
            accepted = _record_effect(
                config,
                psycopg,
                worker_id,
                message.partition(),
                message.offset(),
            )
            append_trace(
                trace_path,
                worker_id,
                "sink_transaction_committed",
                effect_accepted=accepted,
            )
            marker = config.artifact_dir / "killpoint.claimed"
            if crash_after_sink and _claim_killpoint(marker):
                append_trace(
                    trace_path,
                    worker_id,
                    "sigkill_injected",
                    boundary="after_sink_commit_before_offset_commit",
                )
                os.kill(os.getpid(), signal.SIGKILL)
            committed = consumer.commit(message=message, asynchronous=False)
            append_trace(
                trace_path,
                worker_id,
                "offset_committed",
                committed_offsets=[
                    {
                        "topic": item.topic,
                        "partition": item.partition,
                        "offset": item.offset,
                    }
                    for item in committed
                ],
            )
            return
        raise TimeoutError("worker timed out waiting for the probe message")
    except BaseException as error:
        append_trace(
            trace_path,
            worker_id,
            "worker_error",
            error_type=type(error).__name__,
            message=str(error),
        )
        raise
    finally:
        consumer.close()


def _run_worker(
    context: multiprocessing.context.BaseContext,
    config: LiveConfig,
    worker_id: str,
    crash_after_sink: bool,
) -> int | None:
    process = context.Process(
        target=_worker_entry,
        args=(config, worker_id, crash_after_sink),
        name=f"effectfence-{worker_id}",
    )
    process.start()
    process.join(config.timeout_seconds + 10)
    if process.is_alive():
        process.terminate()
        process.join(5)
    return process.exitcode


def _database_counts(config: LiveConfig, psycopg: Any) -> tuple[int, int]:
    with (
        psycopg.connect(config.postgres_dsn) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT COUNT(*) FROM effectfence_live_attempts WHERE run_id = %s",
            (config.run_id,),
        )
        attempts_row = cursor.fetchone()
        cursor.execute(
            "SELECT COUNT(*) FROM effectfence_live_effects WHERE run_id = %s",
            (config.run_id,),
        )
        effects_row = cursor.fetchone()
    return int(attempts_row[0]), int(effects_row[0])


def _committed_offset(
    config: LiveConfig, kafka: Any, partition: int
) -> int | None:
    consumer = kafka.Consumer(
        {
            "bootstrap.servers": config.bootstrap_servers,
            "group.id": config.group_id,
            "enable.auto.commit": False,
        }
    )
    try:
        offsets = consumer.committed(
            [kafka.TopicPartition(config.topic, partition)], timeout=10
        )
        if not offsets or offsets[0].offset < 0:
            return None
        return int(offsets[0].offset)
    finally:
        consumer.close()


def evaluate_live_evidence(
    *,
    attempts: int,
    effects: int,
    first_exitcode: int | None,
    second_exitcode: int | None,
    committed_offset: int | None,
    expected_committed_offset: int,
    expected_outcome: str,
) -> dict[str, Any]:
    first_killed = first_exitcode == -signal.SIGKILL
    harness_violations = []
    if not first_killed:
        harness_violations.append(
            {
                "kind": "killpoint_not_observed",
                "expected_exitcode": -signal.SIGKILL,
                "actual_exitcode": first_exitcode,
            }
        )
    if second_exitcode != 0:
        harness_violations.append(
            {"kind": "recovery_worker_failed", "exitcode": second_exitcode}
        )
    if attempts < 2:
        harness_violations.append(
            {"kind": "redelivery_not_observed", "attempts": attempts}
        )
    if committed_offset != expected_committed_offset:
        harness_violations.append(
            {
                "kind": "offset_not_committed",
                "expected": expected_committed_offset,
                "actual": committed_offset,
            }
        )
    effect_violations = []
    if effects < 1:
        effect_violations.append(
            {"kind": "lost_effect", "expected": 1, "actual": effects}
        )
    if effects > 1:
        effect_violations.append(
            {"kind": "duplicate_effect", "expected": 1, "actual": effects}
        )
    harness_ok = not harness_violations
    effect_safe = not effect_violations
    observed_outcome = "safe" if effect_safe else "unsafe"
    return {
        "harness_ok": harness_ok,
        "effect_safe": effect_safe,
        "observed_outcome": observed_outcome,
        "expectation_met": harness_ok and observed_outcome == expected_outcome,
        "violations": harness_violations + effect_violations,
        "first_worker_sigkilled": first_killed,
    }


def _sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def run_live_kafka_postgres(config: LiveConfig) -> dict[str, Any]:
    validate_config(config)
    if os.name != "posix":
        raise RuntimeError("the SIGKILL integration currently requires a POSIX host")
    kafka, psycopg = require_live_dependencies()
    config.artifact_dir.mkdir(parents=True, exist_ok=False)
    (config.artifact_dir / "config.json").write_text(
        json.dumps(public_config(config), indent=2, sort_keys=True) + "\n"
    )
    trace_path = config.artifact_dir / "trace.ndjson"
    append_trace(trace_path, "controller", "run_started", run_id=config.run_id)
    postgres_version = _wait_for_services(config, kafka, psycopg)
    append_trace(trace_path, "controller", "services_ready")
    _initialize_database(config, psycopg)
    _create_topic(config, kafka)
    partition, produced_offset = _produce_probe(config, kafka)
    append_trace(
        trace_path,
        "controller",
        "probe_delivered",
        partition=partition,
        offset=produced_offset,
    )

    context = multiprocessing.get_context("spawn")
    first_exitcode = _run_worker(context, config, "worker-1", True)
    append_trace(
        trace_path, "controller", "first_worker_exited", exitcode=first_exitcode
    )
    second_exitcode = _run_worker(context, config, "worker-2", False)
    append_trace(
        trace_path, "controller", "recovery_worker_exited", exitcode=second_exitcode
    )

    attempts, effects = _database_counts(config, psycopg)
    committed_offset = _committed_offset(config, kafka, partition)
    evaluation = evaluate_live_evidence(
        attempts=attempts,
        effects=effects,
        first_exitcode=first_exitcode,
        second_exitcode=second_exitcode,
        committed_offset=committed_offset,
        expected_committed_offset=produced_offset + 1,
        expected_outcome=config.expected_outcome,
    )
    report: dict[str, Any] = {
        "schema_version": "effectfence.live.v1",
        "run_id": config.run_id,
        "strategy": config.strategy,
        "expected_outcome": config.expected_outcome,
        "message_id": config.message_id,
        "broker": {
            "implementation": "Apache Kafka protocol",
            "version_label": config.broker_version_label,
            "topic": config.topic,
            "partition": partition,
            "produced_offset": produced_offset,
            "committed_offset": committed_offset,
        },
        "sink": {
            "implementation": "PostgreSQL",
            "server_version": postgres_version,
            "attempts": attempts,
            "accepted_effects": effects,
        },
        "killpoint": {
            "boundary": "after_sink_commit_before_offset_commit",
            "mechanism": "SIGKILL",
            "first_worker_exitcode": first_exitcode,
            "recovery_worker_exitcode": second_exitcode,
        },
        "client_versions": {
            "confluent-kafka": importlib.metadata.version("confluent-kafka"),
            "psycopg": importlib.metadata.version("psycopg"),
        },
        **evaluation,
    }
    report["evidence_sha256"] = _sha256(report)
    report_path = config.artifact_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    append_trace(
        trace_path,
        "controller",
        "run_finished",
        expectation_met=report["expectation_met"],
        evidence_sha256=report["evidence_sha256"],
    )
    return report


def public_config(config: LiveConfig) -> dict[str, Any]:
    result = asdict(config)
    result.pop("postgres_dsn")
    result["artifact_dir"] = str(config.artifact_dir)
    return result
