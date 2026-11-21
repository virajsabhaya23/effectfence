"""FROZEN: deterministic crash/retry schedule simulator.

This subpackage is the decommissioned half of SafeSink: the crash/retry schedule
simulator (``simulator``), its SQLite effect ledger (``ledger``), reference sinks
(``sinks``), exploration/minimization (``explore``), the self-graded 30-case
benchmark (``benchmark``), and the live Kafka/PostgreSQL killpoint adapter
(``live``).

It is kept so earlier reports, corpus entries, and evidence artifacts stay
reproducible. It is frozen: no new strategies, adapters, benchmark cases, or
features are accepted here. The supported product is the MCP side-effect
conformance verifier in :mod:`safesink` (``safesink mcp-verify`` /
``safesink mcp-scan``).

Import from the modules directly (``from safesink.legacy.simulator import
verify_scenario``), or run the frozen commands through the ``safesink-legacy``
console script / ``python -m safesink.legacy.cli``.
"""

__all__: list[str] = []
