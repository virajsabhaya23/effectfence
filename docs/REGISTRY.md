# Listing SafeSink in the MCP Registry

`server.json` at the repository root is the entry for the official MCP Registry
(`registry.modelcontextprotocol.io`). Agents and hosts discover servers there, so
being listed is the difference between "available if you already know the name"
and "findable by an agent looking for a verifier".

```json
{
  "name": "io.github.virajsabhaya23/safesink",
  "packages": [
    {
      "registryType": "pypi",
      "identifier": "safesink",
      "version": "0.5.0",
      "runtimeHint": "uvx",
      "transport": {"type": "stdio"}
    }
  ]
}
```

## Publishing (owner step)

The registry verifies the namespace and requires the package version to exist on
PyPI, so this happens **after** the release workflow has published the same
version.

```bash
# once per machine
brew install mcp-publisher        # or download the release binary

# in the repository root, after `Publish to PyPI` succeeded for this version
mcp-publisher login github        # proves ownership of io.github.virajsabhaya23
mcp-publisher validate
mcp-publisher publish
```

Rules the registry enforces, and how this entry respects them:

- **The name is a namespace.** `io.github.virajsabhaya23/safesink` must match the
  GitHub account that authenticates; it is not free text.
- **Versions are immutable.** Each `server.json` version is published once. Bump
  `version` here in the same commit as `pyproject.toml`,
  `safesink/__init__.py` and `build_backend.py` - the release workflow already
  refuses to publish when those disagree, so update all five places together.
- **The package must resolve.** `safesink==0.5.0` has to install from PyPI. The
  release workflow verifies exactly that in its post-publish job.
- **The description says what runs.** SafeSink's MCP server is lint-only by
  default; `--allow-exec` is a human decision, and the description says so rather
  than implying every tool starts servers.

## Keeping it honest

Listing is not a certification, and the registry entry should not read like one.
The description names the tool's limits; the same limits are in
`docs/MCP_SERVER.md`. If the default changes (for example if `verify_manifest`
were ever enabled by default), the registry description has to change with it -
otherwise the listing is the misleading artifact.
