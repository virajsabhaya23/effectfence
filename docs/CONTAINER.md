# Running SafeSink in a container

```bash
docker run --rm -v "$PWD:/work" ghcr.io/virajsabhaya23/effectfence \
  mcp-verify manifest.json --out /out/report.json
```

The image is multi-stage, built on `python:3.12-slim`, runs as the unprivileged
user `safesink` (uid 10001), and carries Node 22 because most MCP servers are
started with `npx`. `safesink` is the entrypoint, so arguments go straight to the
CLI.

Images are published to `ghcr.io/virajsabhaya23/effectfence` for `main` and every
`v*` tag by `.github/workflows/container.yml` (linux/amd64, GitHub Actions cache,
`GITHUB_TOKEN` only). Nothing needs to be built by hand, and no registry secret
exists to rotate.

## Verifying a server you do not trust

Verification starts the server under test, so the container is also the easiest
way to bound what it can reach:

```bash
docker run --rm \
  --network none \
  --read-only \
  --tmpfs /work/sandbox:rw,size=64m \
  --tmpfs /tmp:rw,size=64m \
  -v "$PWD/manifest.json:/work/manifest.json:ro" \
  -v "$PWD/out:/out:rw" \
  ghcr.io/virajsabhaya23/effectfence \
  mcp-verify /work/manifest.json --out /out/report.json
```

- `--network none` removes outbound access. If the server genuinely needs the
  network, add `--allow-host`-style review of its manifest instead of dropping
  this flag silently.
- `--read-only` plus an explicit writable sandbox means a tool that writes
  outside the sandbox fails loudly rather than editing your tree.
- Never pass the Docker socket, the host home directory, or credentials you would
  not hand to an untrusted binary. Environment variables are visible to the
  server process, so prefer a dedicated DSN for observation
  (`docs/MCP_CONFORMANCE.md`).
- `--cap-drop ALL` and `--pids-limit 256` are reasonable additions on hosts that
  support them.

A sandbox mount must be writable for the boundary check to mean anything: a
read-only sandbox turns every write into an error, which the report records as an
inconclusive case instead of a clean pass.

## Air-gapped use

`npx` needs its package cache. Pre-populate it in a derived image:

```dockerfile
FROM ghcr.io/virajsabhaya23/effectfence
USER root
RUN npm install -g @modelcontextprotocol/server-filesystem@2.4.0
USER safesink
```

Then reference the installed executable in the manifest command instead of
`npx -y`.
