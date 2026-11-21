"""Detached, verifiable certificates.

``certificateSha256`` inside a report is a digest of that report including a field
added afterwards, so on its own it proves nothing to a third party: anyone can
produce a report and hash it. A certificate separates the claim from the report:

* the digest chain proves the report was not edited after the certificate existed;
* the optional Ed25519 signature proves who issued it.

Ed25519 is implemented here (RFC 8032 reference arithmetic) so the package keeps
its zero-runtime-dependency promise. It is not constant-time; it is meant for
issuing and checking evidence artifacts, not for protecting a live secret from a
local timing attacker.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Any

from .observers import canonical_digest

CERTIFICATE_SCHEMA = "effectfence.mcp.certificate.v1"

_P = 2**255 - 19
_Q = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_I = pow(2, (_P - 1) // 4, _P)


class CertificateError(ValueError):
    pass


def _inv(value: int) -> int:
    return pow(value, _P - 2, _P)


def _sha512(value: bytes) -> bytes:
    return hashlib.sha512(value).digest()


def _x_recover(y: int) -> int:
    xx = (y * y - 1) * _inv(_D * y * y + 1)
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P != 0:
        x = (x * _I) % _P
    if x % 2 != 0:
        x = _P - x
    return x


_B_Y = 4 * _inv(5) % _P
_B_X = _x_recover(_B_Y)
_B: tuple[int, int, int, int] = (_B_X % _P, _B_Y % _P, 1, (_B_X * _B_Y) % _P)


def _edwards(point: tuple[int, ...], other: tuple[int, ...]) -> tuple[int, ...]:
    x1, y1, z1, t1 = point
    x2, y2, z2, t2 = other
    a = (y1 - x1) * (y2 - x2) % _P
    b = (y1 + x1) * (y2 + x2) % _P
    c = t1 * 2 * _D * t2 % _P
    d = z1 * 2 * z2 % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _scalar_mult(point: tuple[int, ...], scalar: int) -> tuple[int, ...]:
    result = (0, 1, 1, 0)
    while scalar > 0:
        if scalar & 1:
            result = _edwards(result, point)
        point = _edwards(point, point)
        scalar >>= 1
    return result


def _encode_int(value: int) -> bytes:
    return value.to_bytes(32, "little")


def _encode_point(point: tuple[int, ...]) -> bytes:
    x, y, z, _ = point
    z_inv = _inv(z)
    x, y = x * z_inv % _P, y * z_inv % _P
    return _encode_int(y | ((x & 1) << 255))


def _decode_int(value: bytes) -> int:
    return int.from_bytes(value, "little")


def _decode_point(value: bytes) -> tuple[int, ...]:
    if len(value) != 32:
        raise CertificateError("public key must be 32 bytes")
    y = _decode_int(value) & ((1 << 255) - 1)
    x = _x_recover(y)
    if (x & 1) != (_decode_int(value) >> 255) & 1:
        x = _P - x
    return (x, y, 1, x * y % _P)


def _hash_int(*parts: bytes) -> int:
    return _decode_int(_sha512(b"".join(parts))) % _Q


def public_key(seed: bytes) -> bytes:
    """Derive the 32-byte public key for a 32-byte seed."""
    if len(seed) != 32:
        raise CertificateError("an Ed25519 seed must be exactly 32 bytes")
    hashed = _sha512(seed)
    scalar = _decode_int(hashed[:32])
    scalar &= (1 << 254) - 8
    scalar |= 1 << 254
    return _encode_point(_scalar_mult(_B, scalar))


def sign(message: bytes, seed: bytes) -> bytes:
    """Produce a 64-byte detached Ed25519 signature."""
    if len(seed) != 32:
        raise CertificateError("an Ed25519 seed must be exactly 32 bytes")
    hashed = _sha512(seed)
    scalar = _decode_int(hashed[:32])
    scalar &= (1 << 254) - 8
    scalar |= 1 << 254
    prefix = hashed[32:]
    key = _encode_point(_scalar_mult(_B, scalar))
    r = _hash_int(prefix, message)
    r_point = _scalar_mult(_B, r)
    h = _hash_int(_encode_point(r_point), key, message)
    s = (r + h * scalar) % _Q
    return _encode_point(r_point) + _encode_int(s)


def verify(message: bytes, signature: bytes, key: bytes) -> bool:
    if len(signature) != 64 or len(key) != 32:
        return False
    try:
        r_point = _decode_point(signature[:32])
        public = _decode_point(key)
    except CertificateError:
        return False
    s = _decode_int(signature[32:])
    if s >= _Q:
        return False
    h = _hash_int(signature[:32], key, message)
    left = _scalar_mult(_B, s)
    right = _edwards(r_point, _scalar_mult(public, h))
    return _encode_point(left) == _encode_point(right)


def generate_key() -> tuple[str, str]:
    """A new (seed, publicKey) pair, both hex. Store the seed offline."""
    seed = secrets.token_bytes(32)
    return seed.hex(), public_key(seed).hex()


def report_digest(report: dict[str, Any]) -> str:
    """Digest of the report independent of its own certificate field."""
    payload = {key: value for key, value in report.items() if key != "certificateSha256"}
    return canonical_digest(payload)


def build_certificate(
    report: dict[str, Any], *, manifest_sha256: str | None = None
) -> dict[str, Any]:
    server = report.get("server") or {}
    score = report.get("score") or {}
    certificate: dict[str, Any] = {
        "schemaVersion": CERTIFICATE_SCHEMA,
        "reportSha256": report_digest(report),
        "manifestSha256": manifest_sha256
        or (report.get("manifest") or {}).get("sha256"),
        "serverName": server.get("name"),
        "serverVersion": server.get("version"),
        "safesinkVersion": (report.get("runtime") or {}).get("version"),
        "issuedAt": report.get("generatedAt"),
        "verdict": report.get("verdict"),
        "score": score.get("value"),
        "scoreVersion": score.get("version"),
        "observerSummary": [
            {"id": item.get("id"), "kind": item.get("kind"), "world": item.get("world")}
            for item in report.get("observers", [])
        ],
    }
    return certificate


def _signing_payload(certificate: dict[str, Any]) -> bytes:
    return canonical_digest(
        {key: value for key, value in certificate.items() if key != "signature"}
    ).encode()


def sign_certificate(certificate: dict[str, Any], seed_hex: str) -> dict[str, Any]:
    try:
        seed = bytes.fromhex(seed_hex)
    except ValueError as error:
        raise CertificateError("the signing key must be hex") from error
    signed = dict(certificate)
    signed["publicKey"] = public_key(seed).hex()
    signed["signature"] = sign(_signing_payload(signed), seed).hex()
    return signed


def verify_certificate(
    certificate: dict[str, Any],
    report: dict[str, Any],
    *,
    public_key_hex: str | None = None,
) -> dict[str, Any]:
    """Re-derive the digest chain, then check the signature if there is one."""
    if not isinstance(certificate, dict):
        return {"valid": False, "reason": "certificate must be an object"}
    expected = certificate.get("reportSha256")
    actual = report_digest(report)
    if expected != actual:
        return {
            "valid": False,
            "reason": "report digest does not match the certificate (the report was edited)",
        }
    if report.get("certificateSha256") not in (None, expected):
        return {
            "valid": False,
            "reason": "report.certificateSha256 disagrees with the certificate",
        }
    signature = certificate.get("signature")
    if not signature:
        return {"valid": True, "signed": False, "reason": "digest chain verified"}
    key_hex = public_key_hex or certificate.get("publicKey")
    if not key_hex:
        return {"valid": False, "reason": "signature present but no public key supplied"}
    try:
        key = bytes.fromhex(key_hex)
        signature_bytes = bytes.fromhex(signature)
    except ValueError:
        return {"valid": False, "reason": "public key or signature is not hex"}
    if not verify(_signing_payload(certificate), signature_bytes, key):
        return {"valid": False, "reason": "signature does not verify"}
    return {"valid": True, "signed": True, "reason": "digest chain and signature verified"}
