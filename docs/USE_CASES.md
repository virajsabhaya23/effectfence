# Who this is for, and what each person actually does

SafeSink is only useful if it fits a job someone already has. These are the five
that it is built for, with the command each one runs and the limit each one has to
keep in mind.

## 1. An MCP server author shipping a release

You declare `readOnlyHint: true` on `search_notes` and ship. Then someone's data
changes and your annotation is the reason their agent thought it was safe.

```bash
safesink init --server npx -y your-server ./sandbox --out safesink.mcp.json
# review the cases, fill the contracts of unannotated tools
safesink mcp-verify safesink.mcp.json --out out/report.json --markdown out/report.md
safesink sign --report out/report.json --key "$SEED" --out out/report.cert.json
```

Ship the report and the certificate next to the release. **What it proves:** for
the pinned version and the cases you wrote, the declaration matched the observed
state. **What it does not prove:** that the cases cover every path, or that a
future version behaves the same.

## 2. A platform or security team with servers already in use

Your developers have six servers configured in Claude Desktop, VS Code and
Cursor. Nobody knows which of them write where they claim to read.

```bash
safesink audit --out-dir out/audit --markdown out/audit.md
# or a specific host config, and only the servers you care about
safesink audit --config ~/.config/Claude/claude_desktop_config.json --server filesystem
```

One row per server: verdict, score, findings, inconclusive count, and a report
path. A server that cannot start is `error`, not a pass - and one broken server
does not hide the others.

**What it proves:** which configured servers contradicted their own declarations
when called the way the generated cases call them. **What it does not prove:**
that an unannotated server is safe; those cases stay inconclusive by design, and
the fix is a contract, not a guess.

## 3. An AI agent checking its own work

An agent writing an MCP server or a manifest can lint it without being handed a
shell.

```json
{
  "mcpServers": {
    "safesink": {"command": "uvx", "args": ["safesink-mcp", "--root", "."]}
  }
}
```

The agent calls `lint_manifest`, reads the structured result (findings, codes,
score) and fixes the declaration. `verify_manifest` - which starts the server
under test - is not advertised and refuses calls unless the human started the
server with `--allow-exec`.

**What it proves:** the declaration half, at zero risk. **What it does not
prove:** anything about runtime behaviour, until a human opts into execution.

## 4. A team putting a gate in CI

You cannot fix twelve servers before turning on a check, and a check that fails
on day one gets disabled by Friday.

```yaml
- uses: virajsabhaya23/safesink@main
  with:
    manifest: safesink.mcp.json
    baseline: out/baseline.json
    new-only: "true"
    min-score: "70"
```

The action writes JSON/JUnit/SARIF/Markdown, adds a job summary, keeps one sticky
PR comment, and exposes `verdict`/`score`/`findings` as outputs. Suppressions
must carry a reason and an expiry, so "accepted for now" becomes a date someone
has to revisit.

**What it proves:** nothing got worse than the baseline. **What it does not
prove:** the baseline was good; that is what `--new-only` is honest about.

## 5. A registry, marketplace or directory

A directory that lists MCP servers wants a reproducible signal, not a screenshot.

```bash
python scripts/scan_servers.py      # pinned versions in servers.json
```

Writes `data/leaderboard.json` and the HTML page: verdict, score, counts, hashes
and scan time per pinned version. A server that cannot start is recorded as
inconclusive with its error rather than dropped.

**What it proves:** these exact versions, on this date, gave these results.
**What it does not prove:** that a passing row is a security certification - the
page says so, and the detection proof stays the deliberately dishonest fixture.

## Where SafeSink is the wrong tool

- You want a runtime sandbox or a proxy in front of a server at execution time.
  SafeSink is a check you run, not a fence you deploy.
- You want to test a server you have no permission to start. Use `safesink lint`
  instead; verification executes the server under test.
- You want a score for every server on the internet without running them. The
  score comes from observations; without observations it would be a guess.

## The shortest path for each

| You have | Run |
| --- | --- |
| A server command | `safesink mcp-check -- npx -y your-server` |
| A host config | `safesink audit` |
| A manifest | `safesink mcp-verify safesink.mcp.json` |
| Nothing installed | `safesink lint -- npx -y your-server` |
| An agent | `safesink-mcp` |
| A repo | the action, with `baseline` + `new-only` |
