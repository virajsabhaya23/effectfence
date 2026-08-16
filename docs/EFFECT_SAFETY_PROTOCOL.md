# Effect Safety Protocol v0.1

EffectFence models an input message, durable progress, an external effect sink, acknowledgement, idempotency evidence, and fencing lifetime as separate state machines.

## Safety invariants

1. **No lost owed work** — every input that reaches the handler must produce the declared effect unless the contract marks it intentionally discarded.
2. **No duplicate accepted effect** — retries/redeliveries may repeat execution, but the external sink may accept the logical effect at most once.
3. **Stale retry rejection** — a recovery attempt using an older fence may not supersede or duplicate a newer accepted attempt.
4. **Evidence lifetime is explicit** — idempotency/dedupe evidence is valid only through its declared expiry.
5. **Progress ordering is observable** — checkpoint/ack/effect boundaries are represented separately so crash windows can be explored.

The verifier is coverage-qualified by the schedules explored. It does not claim universal distributed exactly-once delivery.
