# Certificates

`certificateSha256` inside a report is a digest of the report with a field added
afterwards. It proves nothing to a third party: anyone can produce a report and
hash it. A certificate separates the claim from the artifact so a reader can
check it.

```bash
safesink sign --report out/report.json --out out/certificate.json
safesink verify-cert out/certificate.json --report out/report.json
```

## What a certificate contains

| Field | Meaning |
| --- | --- |
| `reportSha256` | digest of the report, excluding its own `certificateSha256` |
| `manifestSha256` | the manifest the run used |
| `serverName`, `serverVersion` | what the report says was scanned |
| `safesinkVersion` | the tool that produced the report |
| `issuedAt`, `verdict`, `score`, `scoreVersion` | the claim |
| `observerSummary` | the boundary the claim is limited to |
| `publicKey`, `signature` | present only when signed |

## What verification proves

`verify-cert` re-derives the digest chain first. If the report was edited after
the certificate was issued, verification fails, even for an unsigned
certificate. If a signature is present, it is checked over the certificate
itself (everything except the signature), so changing `verdict`, `score` or the
observer list also fails.

It does **not** prove that the server is safe, that the run used the pinned
version, or that the observer boundary was adequate. It proves that this report
is the one that was certified, and, when signed, who certified it.

## Signing

```bash
safesink sign --keygen                      # {privateKey, publicKey}, hex
safesink sign --report r.json --key $SEED   # signs, embeds publicKey + signature
safesink verify-cert cert.json --report r.json --public-key $EXPECTED_KEY
```

Ed25519 is implemented in `safesink/certificates.py` (RFC 8032 reference
arithmetic) so `pip install safesink` keeps zero runtime dependencies. It is not
constant-time: it is for evidence artifacts, not for guarding a live secret from
a local timing attacker.

The private key is a 32-byte hex seed and never belongs in a report, a
certificate, a manifest or a repository. Keep it offline, publish only the public
key, and pass `--public-key` when you want verification to fail unless the
certificate was signed by the key you expect rather than by whichever key it
embeds.
