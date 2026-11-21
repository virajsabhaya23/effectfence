# SafeSink in GitHub Actions

The repository ships a composite action, so the shortest useful workflow is:

```yaml
name: mcp-conformance
on: [push, pull_request]

permissions:
  contents: read
  pull-requests: write   # only for the sticky comment

jobs:
  conformance:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v7
        with:
          python-version: "3.12"
      - uses: virajsabhaya23/effectfence@main
        with:
          manifest: examples/mcp-conformance/passing.json
      - uses: actions/upload-artifact@v7
        if: always()
        with:
          name: safesink-evidence
          path: |
            safesink-mcp-report.json
            safesink-mcp-junit.xml
            safesink-mcp.sarif
            safesink-mcp-report.md
          retention-days: 14
```

## What it does

| Input | Default | Meaning |
| --- | --- | --- |
| `manifest` | `safesink.mcp.json` | manifest to check or verify |
| `command` | `auto` | `auto`, `check`, `verify`, `lint` |
| `out` / `junit` / `sarif` / `markdown` | `safesink-mcp*` | report paths |
| `sandbox` | `.safesink-sandbox` | directory observed by `--sqlite-query` |
| `min-coverage` | `0` | required fraction of advertised tools covered |
| `min-score` | empty | fail below this trust score |
| `baseline` / `new-only` | empty / `false` | compare against an earlier report |
| `comment` | `true` | post or update the sticky PR comment |

`auto` runs `mcp-verify` when the manifest contains cases and falls back to
`mcp-check`, which discovers tools from a live server. It fails with a clear
message when no manifest exists, instead of producing an empty pass.

Outputs: `report`, `markdown`, `verdict`, `score`, `findings`.

## The sticky comment

With `comment: true` on a `pull_request` event, the action finds its previous
comment by an HTML marker and updates it instead of adding a new one. The body is
the generated Markdown report: verdict, score, per-case results and findings. If
the report could not be written, the comment says so and points at the artifact
rather than disappearing.

GitHub disallows artifact actions inside composite actions, so upload the files
in the calling workflow - the snippet above does, and the repository's own
`mcp-conformance` workflow keeps the evidence for 14 days.

## Permissions and untrusted forks

- `contents: read` is enough to run checks. Add `pull-requests: write` only if
  you want the comment.
- A `pull_request` from a fork runs with a read-only token, so the comment step
  fails there. Set `comment: false` for fork PRs, or use `pull_request_target`
  only with a checked-out base ref and no execution of fork code. Verification
  starts the server under test, so never run it on untrusted changes with a
  writable token.
- Pin the action to a tag or commit for reproducibility.

## Marketplace listing

The action is marketplace-ready (`name`, `description`, `branding`, documented
inputs and outputs). Listing it is a one-time UI step in the repository's release
page and cannot be automated from the repository; until it is listed, use the
`uses: virajsabhaya23/effectfence@main` form above.
