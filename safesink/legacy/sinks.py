from __future__ import annotations
import json, sqlite3, urllib.request
from dataclasses import dataclass
from .ledger import EffectLedger

class Sink:
    def emit(self, message_id: str, effect_key: str, actor: str, now: int, fence: int | None = None) -> bool:
        raise NotImplementedError

class LedgerSink(Sink):
    def __init__(self, ledger: EffectLedger): self.ledger=ledger
    def emit(self, message_id, effect_key, actor, now, fence=None):
        return self.ledger.accept_effect(message_id,effect_key,actor,now,fence)

class HttpSink(Sink):
    def __init__(self, endpoint: str, timeout: float=3.0): self.endpoint=endpoint; self.timeout=timeout
    def emit(self, message_id, effect_key, actor, now, fence=None):
        body=json.dumps({'message_id':message_id,'effect_key':effect_key,'actor':actor,'time':now,'fence':fence}).encode()
        req=urllib.request.Request(self.endpoint,data=body,headers={'Content-Type':'application/json','Idempotency-Key':effect_key},method='POST')
        with urllib.request.urlopen(req,timeout=self.timeout) as r: return 200 <= r.status < 300

class DbApiSink(Sink):
    """Portable DB-API 2.0 sink; works with sqlite3 and PostgreSQL drivers exposing DB-API."""
    def __init__(self, connection):
        self.db=connection
        self.db.execute('CREATE TABLE IF NOT EXISTS effectfence_effects(effect_key TEXT PRIMARY KEY, message_id TEXT, actor TEXT, accepted_at INTEGER, fence INTEGER)')
        self.db.commit()
    def emit(self,message_id,effect_key,actor,now,fence=None):
        try:
            self.db.execute('INSERT INTO effectfence_effects(effect_key,message_id,actor,accepted_at,fence) VALUES (?,?,?,?,?)',(effect_key,message_id,actor,now,fence)); self.db.commit(); return True
        except Exception:
            self.db.rollback(); return False
