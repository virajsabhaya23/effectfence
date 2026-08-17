# Live evidence — 2026-08-16

These redacted artifacts were produced by the version 0.2 Kafka/PostgreSQL
adapter on a local POSIX host using:

- Apache Kafka 4.3.1
- PostgreSQL 18.4
- confluent-kafka 2.15.0
- psycopg 3.3.4

`effectfence-report.json` proves that the stable EffectFence identity accepted
one effect across two delivery attempts. `naive-control-report.json` proves that
the same crash window accepted two effects without that identity. In both runs,
the first consumer exited from `SIGKILL`, the replacement consumer received the
same broker offset, and the replacement committed offset `1`.

The corresponding NDJSON files retain the ordered process events. PostgreSQL
connection strings are not stored.
