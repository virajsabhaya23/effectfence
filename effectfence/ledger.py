from __future__ import annotations
import sqlite3
from pathlib import Path

class EffectLedger:
    def __init__(self, path: str = ':memory:'):
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS effects(
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT NOT NULL,
            effect_key TEXT NOT NULL,
            actor TEXT NOT NULL,
            accepted_at INTEGER NOT NULL,
            fence INTEGER
        );
        CREATE TABLE IF NOT EXISTS dedupe(
            message_id TEXT PRIMARY KEY,
            expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS checkpoints(
            message_id TEXT PRIMARY KEY,
            committed_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS acknowledgements(
            message_id TEXT PRIMARY KEY,
            acked_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS outbox(
            message_id TEXT PRIMARY KEY,
            created_at INTEGER NOT NULL,
            published INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS fences(
            message_id TEXT PRIMARY KEY,
            token INTEGER NOT NULL
        );
        ''')
        self.db.commit()

    def close(self): self.db.close()
    def has_checkpoint(self, mid: str) -> bool:
        return self.db.execute('SELECT 1 FROM checkpoints WHERE message_id=?',(mid,)).fetchone() is not None
    def checkpoint(self, mid: str, now: int):
        self.db.execute('INSERT OR REPLACE INTO checkpoints VALUES (?,?)',(mid,now)); self.db.commit()
    def ack(self, mid: str, now: int):
        self.db.execute('INSERT OR REPLACE INTO acknowledgements VALUES (?,?)',(mid,now)); self.db.commit()
    def is_acked(self, mid: str) -> bool:
        return self.db.execute('SELECT 1 FROM acknowledgements WHERE message_id=?',(mid,)).fetchone() is not None
    def dedupe_valid(self, mid: str, now: int) -> bool:
        row=self.db.execute('SELECT expires_at FROM dedupe WHERE message_id=?',(mid,)).fetchone()
        return bool(row and row[0] >= now)
    def set_dedupe(self, mid: str, now: int, ttl: int):
        self.db.execute('INSERT OR REPLACE INTO dedupe VALUES (?,?)',(mid,now+ttl)); self.db.commit()
    def create_outbox(self, mid: str, now: int):
        self.db.execute('INSERT OR IGNORE INTO outbox(message_id,created_at,published) VALUES (?,?,0)',(mid,now)); self.db.commit()
    def mark_outbox_published(self, mid: str):
        self.db.execute('UPDATE outbox SET published=1 WHERE message_id=?',(mid,)); self.db.commit()
    def outbox_pending(self, mid: str) -> bool:
        row=self.db.execute('SELECT published FROM outbox WHERE message_id=?',(mid,)).fetchone(); return bool(row and row[0]==0)
    def next_fence(self, mid: str) -> int:
        row=self.db.execute('SELECT token FROM fences WHERE message_id=?',(mid,)).fetchone()
        token=(row[0]+1) if row else 1
        self.db.execute('INSERT OR REPLACE INTO fences VALUES (?,?)',(mid,token)); self.db.commit(); return token
    def current_fence(self, mid: str) -> int:
        row=self.db.execute('SELECT token FROM fences WHERE message_id=?',(mid,)).fetchone(); return row[0] if row else 0
    def accept_effect(self, mid: str, key: str, actor: str, now: int, fence: int | None=None, enforce_key_unique: bool=False) -> bool:
        if fence is not None and fence < self.current_fence(mid): return False
        if enforce_key_unique and self.db.execute('SELECT 1 FROM effects WHERE effect_key=?',(key,)).fetchone(): return False
        self.db.execute('INSERT INTO effects(message_id,effect_key,actor,accepted_at,fence) VALUES (?,?,?,?,?)',(mid,key,actor,now,fence)); self.db.commit(); return True
    def count_effects(self, mid: str) -> int:
        return self.db.execute('SELECT COUNT(*) FROM effects WHERE message_id=?',(mid,)).fetchone()[0]
    def effects(self, mid: str):
        return [dict(zip(('seq','message_id','effect_key','actor','accepted_at','fence'),r)) for r in self.db.execute('SELECT seq,message_id,effect_key,actor,accepted_at,fence FROM effects WHERE message_id=? ORDER BY seq',(mid,))]
