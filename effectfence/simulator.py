from __future__ import annotations
from dataclasses import asdict
import hashlib, json, tempfile
from .ledger import EffectLedger
from .model import Scenario, TraceEvent

BOUNDARIES=('before_effect','after_effect','before_checkpoint','after_checkpoint','before_ack','after_ack')

def _cert(obj): return hashlib.sha256(json.dumps(obj,sort_keys=True,separators=(',',':')).encode()).hexdigest()

class Crash(Exception): pass

class Simulator:
    def __init__(self, scenario: Scenario):
        self.s=scenario; self.t=0; self.trace=[]; self.ledger=EffectLedger(':memory:'); self.crash_budget=list(scenario.crashes)
        self.delivery_attempts=0; self.delivery_skips=0
        self.broker_state={'delivered':0,'in_flight':False,'progress_durable':False,'acked':False,'ownership_epoch':0}

    def ev(self,actor,event,**detail):
        self.trace.append(asdict(TraceEvent(self.t,actor,event,detail))); self.t+=1

    def maybe_crash(self,point,actor):
        if point in self.crash_budget:
            self.crash_budget.remove(point); self.ev(actor,'crash',point=point); raise Crash(point)

    def _checkpoint(self, actor):
        self.ledger.checkpoint(self.s.id,self.t); self.broker_state['progress_durable']=True; self.ev(actor,'checkpoint',durable=True)

    def _ack(self, actor):
        self.ledger.ack(self.s.id,self.t); self.broker_state['acked']=True; self.ev(actor,'ack',durable=True)

    def _complete_replay(self, actor, evidence):
        """A replay that observes durable sink evidence must still advance the broker."""
        self.ev(actor,evidence)
        self._checkpoint(actor)
        self._ack(actor)

    def delivery_eligible(self):
        return (not self.broker_state['progress_durable']) or (
            self.s.strategy=='transactional_outbox' and self.ledger.outbox_pending(self.s.id)
        )

    def deliver(self, actor, attempt, *, forced=False, reason='redelivery-budget'):
        eligible=self.delivery_eligible()
        self.ev('broker','delivery-eligibility',worker=actor,attempt=attempt,eligible=eligible,forced=forced,reason=reason)
        if not eligible and not forced:
            self.delivery_skips+=1; self.ev('broker','delivery-skipped',worker=actor,attempt=attempt,reason='durable-progress')
            return False
        self.delivery_attempts+=1; self.broker_state['delivered']+=1; self.broker_state['in_flight']=True; self.broker_state['ownership_epoch']+=1
        self.ev('broker','delivery-start',worker=actor,attempt=attempt,forced=forced,reason=reason,ownership_epoch=self.broker_state['ownership_epoch'])
        try: self.run_attempt(actor,attempt)
        finally: self.broker_state['in_flight']=False
        return True

    def _naive(self,actor,attempt):
        mid=self.s.id; key=f'{mid}:effect:{attempt}'
        self.maybe_crash('before_effect',actor)
        ok=self.ledger.accept_effect(mid,key,actor,self.t,None); self.ev(actor,'effect',accepted=ok,key=key)
        self.maybe_crash('after_effect',actor)
        self.maybe_crash('before_checkpoint',actor)
        self._checkpoint(actor)
        self.maybe_crash('after_checkpoint',actor)
        self.maybe_crash('before_ack',actor)
        self._ack(actor)
        self.maybe_crash('after_ack',actor)

    def _idempotent(self,actor,attempt):
        mid=self.s.id
        if self.ledger.dedupe_valid(mid,self.t): self._complete_replay(actor,'dedupe-hit'); return
        key=f'{mid}:idempotent'
        self.maybe_crash('before_effect',actor)
        ok=self.ledger.accept_effect(mid,key,actor,self.t,None); self.ev(actor,'effect',accepted=ok,key=key)
        self.maybe_crash('after_effect',actor)
        self.ledger.set_dedupe(mid,self.t,self.s.dedupe_ttl); self.ev(actor,'dedupe-record',expires=self.t+self.s.dedupe_ttl)
        self.maybe_crash('before_checkpoint',actor)
        self._checkpoint(actor)
        self.maybe_crash('after_checkpoint',actor)
        self.maybe_crash('before_ack',actor)
        self._ack(actor)
        self.maybe_crash('after_ack',actor)

    def _outbox(self,actor,attempt):
        mid=self.s.id
        if not self.ledger.has_checkpoint(mid):
            self.ledger.create_outbox(mid,self.t); self._checkpoint(actor); self.ev(actor,'db+outbox-commit')
        if self.ledger.outbox_pending(mid):
            key=f'{mid}:outbox:{attempt}'
            self.maybe_crash('before_effect',actor)
            ok=self.ledger.accept_effect(mid,key,actor,self.t,None); self.ev(actor,'relay-effect',accepted=ok,key=key)
            self.maybe_crash('after_effect',actor)
            self.ledger.mark_outbox_published(mid); self.ev(actor,'outbox-published')
        self.maybe_crash('before_ack',actor)
        self._ack(actor)
        self.maybe_crash('after_ack',actor)

    def _effectfence(self,actor,attempt):
        mid=self.s.id
        token=self.ledger.next_fence(mid); self.ev(actor,'fence-acquired',token=token)
        if self.ledger.dedupe_valid(mid,self.t):
            self.ev(actor,'evidence-hit',token=token); self._checkpoint(actor); self._ack(actor); return
        key=f'{mid}:stable-effect'
        self.maybe_crash('before_effect',actor)
        ok=self.ledger.accept_effect(mid,key,actor,self.t,token,enforce_key_unique=True); self.ev(actor,'effect',accepted=ok,key=key,token=token)
        self.maybe_crash('after_effect',actor)
        # Sink-side durable acceptance evidence is established before progress/ack.
        self.ledger.set_dedupe(mid,self.t,self.s.dedupe_ttl); self.ev(actor,'acceptance-evidence',expires=self.t+self.s.dedupe_ttl,token=token)
        self.maybe_crash('before_checkpoint',actor)
        self._checkpoint(actor)
        self.maybe_crash('after_checkpoint',actor)
        self.maybe_crash('before_ack',actor)
        self._ack(actor)
        self.maybe_crash('after_ack',actor)

    def run_attempt(self,actor,attempt):
        fn={'naive_retry':self._naive,'idempotency_key':self._idempotent,'transactional_outbox':self._outbox,'effectfence':self._effectfence}[self.s.strategy]
        try: fn(actor,attempt)
        except Crash: pass

    def run(self):
        attempts=max(1,self.s.redeliveries)
        for i in range(attempts):
            self.deliver(f'worker-{i+1}',i+1)
            self.t += self.s.retry_delay
        # Concurrent/stale recoveries happen after ordinary redeliveries.
        for i in range(max(0,self.s.concurrent_recovery-1)):
            self.deliver(f'recovery-{i+1}',attempts+i+1,forced=True,reason='concurrent-recovery-fault'); self.t+=1
        if self.s.stale_retry_delay is not None:
            self.t += self.s.stale_retry_delay
            self.deliver('stale-retry',attempts+self.s.concurrent_recovery+1,forced=True,reason='stale-inflight-fault')
        if self.s.ack_loss and self.ledger.is_acked(self.s.id):
            self.ledger.lose_broker_progress(self.s.id); self.broker_state['progress_durable']=False; self.broker_state['acked']=False; self.ev('broker','progress-lost',fault='ack_loss')
            self.deliver('redelivery-after-ack-loss',attempts+99,reason='explicit-progress-loss')
        effects=self.ledger.count_effects(self.s.id)
        violations=[]
        if effects < self.s.expect_effects: violations.append({'kind':'lost_effect','expected':self.s.expect_effects,'actual':effects})
        if effects > self.s.expect_effects: violations.append({'kind':'duplicate_effect','expected':self.s.expect_effects,'actual':effects})
        if self.s.strategy=='effectfence':
            stale=[e for e in self.ledger.effects(self.s.id) if e['fence'] is not None and e['fence'] < self.ledger.current_fence(self.s.id)]
            # Accepted earlier valid effects are not stale violations; stale rejection is observed via accepted=False.
        payload={'scenario_id':self.s.id,'strategy':self.s.strategy,'safe':not violations,'violations':violations,'trace':self.trace,
                 'accepted_effects':effects,'acknowledged':self.ledger.is_acked(self.s.id),'checkpointed':self.ledger.has_checkpoint(self.s.id)}
        payload['broker_delivery']={'attempt_budget':attempts,'attempted':self.delivery_attempts,'skipped':self.delivery_skips,
                                    'eligible_after_run':self.delivery_eligible(),'state':dict(self.broker_state)}
        payload['certificate_sha256']=_cert(payload)
        self.ledger.close(); return payload

def verify_scenario(scenario: Scenario): return Simulator(scenario).run()
