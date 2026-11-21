# Badge and leaderboard

Both surfaces are only worth having if they stay honest. The rules below are
enforced by the code, not by good intentions.

## Your own badge

```bash
safesink mcp-check --out out/mcp/report.json -- npx -y your-server
safesink badge --report out/mcp/report.json --out badge.svg
```

The SVG states the verdict (`pass`, `inconclusive`, `fail`) and the score, in the
same three-state vocabulary as the report. It is a statement about one pinned
version of one server, not a certificate: the certificate is the signed
attestation produced by `safesink sign` (`docs/CERTIFICATES.md`).

## The leaderboard

`servers.json` is the registry. Every entry pins a package version and the exact
command, so a row can be re-derived later:

```json
{
  "name": "filesystem",
  "package": "@modelcontextprotocol/server-filesystem",
  "version": "2.4.0",
  "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem@2.4.0", "./sandbox"]
}
```

`scripts/scan_servers.py` runs `safesink mcp-check` for each entry, records the
verdict, score, tool and case counts, finding counts, and report hashes into
`data/leaderboard.json`, and renders `data/leaderboard.html` from it. The
nightly workflow runs the same script and uploads both files as a build
artifact; nothing is committed to a branch by CI, and the workflow keeps
read-only permissions.

A scan that cannot start is recorded as `inconclusive` with its error rather
than dropped - a table that silently loses the servers it could not scan is
worse than one that admits the gap.

### Honesty rules

- **Zero findings is the expected result for a trusted server.** The table
  demonstrates that the tool does not manufacture failures; detection is proven
  separately by the deliberately dishonest fixture in the test suite.
- A pass is not a security certification, an audit, or a statement about
  versions other than the pinned one.
- Versions are bumped in their own commit, so a row always points at something
  reproducible.
- Rows are never edited by hand. Re-run the script.

`docs/leaderboard.html` in a local checkout is generated output; `data/` is the
source of truth.
