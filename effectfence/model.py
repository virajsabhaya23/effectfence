from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any

@dataclass(frozen=True)
class Message:
    id: str
    payload: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class Scenario:
    id: str
    family: str
    strategy: str
    crashes: tuple[str, ...] = ()
    redeliveries: int = 1
    ack_loss: bool = False
    concurrent_recovery: int = 1
    dedupe_ttl: int = 100
    retry_delay: int = 1
    stale_retry_delay: int | None = None
    expected_safe: bool = True
    expect_effects: int = 1
    notes: str = ""

@dataclass
class TraceEvent:
    t: int
    actor: str
    event: str
    detail: dict[str, Any] = field(default_factory=dict)

@dataclass
class VerificationResult:
    scenario_id: str
    strategy: str
    safe: bool
    violations: list[dict[str, Any]]
    trace: list[dict[str, Any]]
    accepted_effects: int
    acknowledged: bool
    checkpointed: bool
    certificate_sha256: str
