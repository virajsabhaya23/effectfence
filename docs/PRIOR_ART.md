# Prior-art boundary

Kafka, SQS, transactional outbox, Debezium, idempotency keys, and workflow engines already provide mature delivery/recovery mechanisms. EffectFence does not reimplement those systems.

The contribution is executable **application-level verification** across the external-effect boundary:
- systematically inject crashes around effect/checkpoint/ack boundaries;
- explore lost acknowledgement, concurrent recovery, stale retries, and evidence expiry;
- observe sink acceptance rather than assuming a retry policy is safe;
- compare concrete reference strategies;
- emit replayable minimized counterexample traces and versioned certificates.

The 2026 machine-checked dual-write recovery work is treated as formal prior art, not as an EffectFence invention.
