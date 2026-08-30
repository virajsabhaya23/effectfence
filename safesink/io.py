from __future__ import annotations
import json
from pathlib import Path
from .model import Scenario

def load(path: str) -> Scenario:
    d=json.loads(Path(path).read_text())
    allowed=set(Scenario.__dataclass_fields__)
    extra=set(d)-allowed
    if extra: raise ValueError(f'unknown scenario keys: {sorted(extra)}')
    if d.get('strategy') not in {'naive_retry','idempotency_key','transactional_outbox','effectfence'}: raise ValueError('invalid strategy')
    d['crashes']=tuple(d.get('crashes',()))
    return Scenario(**d)

def save(obj,path: str):
    p=Path(path); p.parent.mkdir(parents=True,exist_ok=True); p.write_text(json.dumps(obj,indent=2,sort_keys=True)+'\n')
