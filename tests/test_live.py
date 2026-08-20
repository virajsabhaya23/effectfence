import json
import signal
import tempfile
import unittest
from pathlib import Path

from effectfence.live import (
    LiveConfig,
    append_trace,
    evaluate_live_evidence,
    public_config,
    validate_config,
)


def config(artifact_dir: Path) -> LiveConfig:
    return LiveConfig(
        bootstrap_servers="127.0.0.1:9092",
        postgres_dsn="postgresql://user:secret@127.0.0.1/effectfence",
        topic="effectfence-test",
        group_id="effectfence-test",
        run_id="test-run-1",
        message_id="message-1",
        strategy="effectfence",
        expected_outcome="safe",
        artifact_dir=artifact_dir,
    )


class LiveEvidenceTests(unittest.TestCase):
    def test_safe_crash_recovery_meets_expectation(self):
        result = evaluate_live_evidence(
            attempts=2,
            effects=1,
            first_exitcode=-signal.SIGKILL,
            second_exitcode=0,
            committed_offset=1,
            expected_committed_offset=1,
            expected_outcome="safe",
        )
        self.assertTrue(result["harness_ok"])
        self.assertTrue(result["effect_safe"])
        self.assertTrue(result["expectation_met"])
        self.assertEqual(result["violations"], [])

    def test_naive_duplicate_is_an_expected_unsafe_control(self):
        result = evaluate_live_evidence(
            attempts=2,
            effects=2,
            first_exitcode=-signal.SIGKILL,
            second_exitcode=0,
            committed_offset=8,
            expected_committed_offset=8,
            expected_outcome="unsafe",
        )
        self.assertTrue(result["harness_ok"])
        self.assertFalse(result["effect_safe"])
        self.assertTrue(result["expectation_met"])
        self.assertEqual(result["violations"][0]["kind"], "duplicate_effect")

    def test_broken_harness_cannot_satisfy_unsafe_expectation(self):
        result = evaluate_live_evidence(
            attempts=1,
            effects=2,
            first_exitcode=0,
            second_exitcode=1,
            committed_offset=None,
            expected_committed_offset=1,
            expected_outcome="unsafe",
        )
        self.assertFalse(result["harness_ok"])
        self.assertFalse(result["expectation_met"])

    def test_public_config_does_not_expose_postgres_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            result = public_config(config(Path(directory)))
        self.assertNotIn("postgres_dsn", result)
        self.assertNotIn("secret", json.dumps(result))

    def test_trace_is_ndjson_and_records_details(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.ndjson"
            append_trace(path, "worker-1", "sink_committed", accepted=True)
            record = json.loads(path.read_text())
        self.assertEqual(record["actor"], "worker-1")
        self.assertTrue(record["detail"]["accepted"])

    def test_config_rejects_unsafe_identifiers(self):
        with tempfile.TemporaryDirectory() as directory:
            invalid = config(Path(directory))
            invalid = LiveConfig(**{**invalid.__dict__, "run_id": "bad/run"})
            with self.assertRaises(ValueError):
                validate_config(invalid)


if __name__ == "__main__":
    unittest.main()
