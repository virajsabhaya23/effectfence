from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from safesink.certificates import (
    CertificateError,
    build_certificate,
    generate_key,
    public_key,
    sign,
    sign_certificate,
    verify,
    verify_certificate,
)
from safesink.cli import main as cli_main
from safesink.mcp_verifier import verify_manifest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "mcp-conformance"


class Ed25519Tests(unittest.TestCase):
    def test_a_signature_verifies_and_a_changed_message_does_not(self) -> None:
        seed, key = generate_key()
        signature = sign(b"safesink", bytes.fromhex(seed))
        self.assertTrue(verify(b"safesink", signature, bytes.fromhex(key)))
        self.assertFalse(verify(b"safe-sink", signature, bytes.fromhex(key)))

    def test_a_different_key_does_not_verify(self) -> None:
        seed, _ = generate_key()
        _, other = generate_key()
        signature = sign(b"payload", bytes.fromhex(seed))
        self.assertFalse(verify(b"payload", signature, bytes.fromhex(other)))

    def test_an_invalid_seed_is_rejected(self) -> None:
        with self.assertRaises(CertificateError):
            public_key(b"short")


class CertificateTests(unittest.TestCase):
    def report(self) -> dict:
        return verify_manifest(EXAMPLES / "passing.json")

    def test_the_digest_chain_verifies_a_report(self) -> None:
        report = self.report()
        certificate = build_certificate(report)
        self.assertEqual(certificate["reportSha256"], report["certificateSha256"])
        result = verify_certificate(certificate, report)
        self.assertTrue(result["valid"])
        self.assertFalse(result["signed"])

    def test_a_tampered_report_fails_verification(self) -> None:
        report = self.report()
        certificate = build_certificate(report)
        report["verdict"] = "fail"
        result = verify_certificate(certificate, report)
        self.assertFalse(result["valid"])
        self.assertIn("edited", result["reason"])

    def test_a_signature_binds_the_certificate_to_its_issuer(self) -> None:
        report = self.report()
        seed, key = generate_key()
        certificate = sign_certificate(build_certificate(report), seed)
        self.assertEqual(certificate["publicKey"], key)
        self.assertTrue(verify_certificate(certificate, report)["valid"])
        _, other = generate_key()
        self.assertFalse(
            verify_certificate(certificate, report, public_key_hex=other)["valid"]
        )

    def test_a_changed_certificate_field_breaks_the_signature(self) -> None:
        report = self.report()
        seed, _ = generate_key()
        certificate = sign_certificate(build_certificate(report), seed)
        certificate["verdict"] = "fail"
        result = verify_certificate(certificate, report)
        self.assertFalse(result["valid"])
        self.assertIn("signature", result["reason"])


class CertificateCliTests(unittest.TestCase):
    def write_report(self, root: Path) -> Path:
        report = verify_manifest(EXAMPLES / "passing.json")
        path = root / "report.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        return path

    def test_sign_then_verify_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = self.write_report(root)
            certificate = root / "cert.json"
            seed, _ = generate_key()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    cli_main(
                        [
                            "sign",
                            "--report",
                            str(report),
                            "--out",
                            str(certificate),
                            "--key",
                            seed,
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    cli_main(
                        ["verify-cert", str(certificate), "--report", str(report)]
                    ),
                    0,
                )
            value = json.loads(certificate.read_text())
            self.assertIn("signature", value)

    def test_verify_cert_fails_on_an_edited_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = self.write_report(root)
            certificate = root / "cert.json"
            with contextlib.redirect_stdout(io.StringIO()):
                cli_main(
                    ["sign", "--report", str(report), "--out", str(certificate)]
                )
            value = json.loads(report.read_text())
            value["verdict"] = "fail"
            report.write_text(json.dumps(value), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = cli_main(
                    ["verify-cert", str(certificate), "--report", str(report), "--json"]
                )
            self.assertEqual(exit_code, 2)

    def test_keygen_prints_a_usable_pair(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(cli_main(["sign", "--keygen"]), 0)
        value = json.loads(stdout.getvalue())
        self.assertEqual(len(value["privateKey"]), 64)
        self.assertEqual(len(value["publicKey"]), 64)


if __name__ == "__main__":
    unittest.main()
