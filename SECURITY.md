# Security policy

## Supported versions

Security fixes are provided for the latest released major version.

## Reporting a vulnerability

Please report suspected vulnerabilities privately through the repository's GitHub Security Advisory form. Do not include secrets, production data, or exploit details in a public issue. Include a minimal synthetic reproducer, affected version, expected behavior, and impact. You can expect an acknowledgement within seven days after the public repository is launched.

This project is defensive testing software. Only test systems and data you own or are explicitly authorized to assess. Run the live adapter only against disposable local or staging infrastructure: it creates a Kafka topic and namespaced PostgreSQL tables, and deliberately terminates its own consumer process with `SIGKILL`. It never needs production data.

The MCP verifier starts the exact executable configured in a manifest and may
invoke state-changing tools. Review manifests as code, pin the MCP server under
test, and use disposable fixtures or staging accounts. Commands and hooks are
executed directly without a shell. The server receives only a small baseline
environment plus variables listed in `inheritEnv` or `env`.

MCP report redaction is the default: tool values are omitted, sensitive observer
paths/digests are omitted, and server/hook stderr is not copied into reports.
Manifests themselves can still contain sensitive values, so pass credentials by
environment-variable name and never commit them. HTTP observers require an
explicit host allowlist, block redirects, and enforce time and response-size
limits.
