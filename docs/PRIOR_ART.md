# Prior-art boundary

Kafka, SQS, transactional outbox, Debezium, idempotency keys, and workflow engines already provide mature delivery/recovery mechanisms. EffectFence does not reimplement those systems.

The original contribution boundary is executable **application-level
verification** across the external-effect boundary:
- systematically inject crashes around effect/checkpoint/ack boundaries;
- explore lost acknowledgement, concurrent recovery, stale retries, and evidence expiry;
- observe sink acceptance rather than assuming a retry policy is safe;
- compare concrete reference strategies;
- emit replayable minimized counterexample traces and versioned certificates.

The 2026 machine-checked dual-write recovery work is treated as formal prior art, not as an EffectFence invention.

MCP tool annotations, policy gateways, and taint tracking are also prior art.
EffectFence does not claim those mechanisms. The MCP-specific wedge is a
transport-level test runner that compares a server's declared tool-effect hints
or an organization's explicit contract with observer-bounded state before the
call, after the call, and after an identical retry. The output is portable
conformance evidence for CI rather than a runtime authorization decision.

This boundary remains deliberately narrow: a snapshot observer cannot detect an
unobserved system call or prove that an external read occurred. Any field-level
originality or significance claim requires independent review, adoption, and
corroboration beyond this repository.
