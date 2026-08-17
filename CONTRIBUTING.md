# Contributing

Thank you for helping improve this project.

1. Open an issue describing the problem, expected behavior, and a minimal synthetic example.
2. Create a focused change with tests covering success, failure, and malformed-input behavior.
3. Run `python scripts/format_check.py`, `python scripts/lint.py`,
   `python scripts/typecheck.py`, and `python -m unittest discover -s tests -v`.
   Changes to the live adapter must also run both the safe strategy and the
   expected-unsafe naive control described in `docs/LIVE_KAFKA_POSTGRES.md`.
   MCP changes must also run both manifests in `examples/mcp-conformance/` and
   confirm that the passing fixture exits 0 while the dishonest fixture exits 2.
4. Submit a pull request explaining the behavior change and any compatibility implications.

Never submit credentials, customer data, or undisclosed third-party vulnerabilities. Security-sensitive findings should follow `SECURITY.md`. By contributing, you agree that your contribution is licensed under Apache-2.0.
