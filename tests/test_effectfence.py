import json, sqlite3, tempfile, unittest
from dataclasses import replace
from pathlib import Path
from effectfence.io import load
from effectfence.simulator import verify_scenario
from effectfence.explore import explore, minimize_failure
from effectfence.benchmark import run
from effectfence.ledger import EffectLedger
from effectfence.sinks import DbApiSink
from effectfence.httpdemo import start, Handler
from effectfence.sinks import HttpSink
ROOT=Path(__file__).parents[1]
class T(unittest.TestCase):
 def test_effectfence_crash_safe(self): self.assertTrue(verify_scenario(load(str(ROOT/'examples/safe_fenced_recovery.json')))['safe'])
 def test_naive_crash_unsafe(self):
  s=load(str(ROOT/'examples/crash_after_effect.json')); self.assertFalse(verify_scenario(s)['safe'])
 def test_idempotency_crash_window_unsafe(self):
  s=load(str(ROOT/'benchmark/cases/IDEM-CRASH-01.json')); self.assertFalse(verify_scenario(s)['safe'])
 def test_outbox_relay_crash_unsafe(self):
  s=load(str(ROOT/'benchmark/cases/OUTBOX-CRASH-01.json')); self.assertFalse(verify_scenario(s)['safe'])
 def test_explore(self): self.assertGreater(len(explore(load(str(ROOT/'examples/crash_after_effect.json')))),10)
 def test_minimizer(self):
  s=load(str(ROOT/'examples/crash_after_effect.json')); m=minimize_failure(s); self.assertFalse(verify_scenario(m)['safe'])
 def test_certificate_stable(self):
  s=load(str(ROOT/'examples/safe_fenced_recovery.json')); self.assertEqual(verify_scenario(s)['certificate_sha256'],verify_scenario(s)['certificate_sha256'])
 def test_ordinary_redelivery_budget_stops_after_durable_progress(self):
  s=replace(load(str(ROOT/'examples/crash_after_effect.json')),crashes=(),redeliveries=5,ack_loss=False,concurrent_recovery=1)
  result=verify_scenario(s)
  self.assertEqual({k:result['broker_delivery'][k] for k in ('attempt_budget','attempted','skipped','eligible_after_run')},
                   {'attempt_budget':5,'attempted':1,'skipped':4,'eligible_after_run':False})
  self.assertEqual(result['broker_delivery']['state'],{'delivered':1,'in_flight':False,'progress_durable':True,'acked':True,'ownership_epoch':1})
  self.assertEqual(result['accepted_effects'],1)
 def test_crash_before_checkpoint_remains_delivery_eligible(self):
  s=replace(load(str(ROOT/'examples/crash_after_effect.json')),crashes=('before_checkpoint',),redeliveries=2,ack_loss=False,concurrent_recovery=1)
  result=verify_scenario(s)
  self.assertEqual(result['broker_delivery']['attempted'],2)
  self.assertTrue(any(e['event']=='delivery-start' and e['detail']['attempt']==2 for e in result['trace']))
 def test_crash_after_checkpoint_blocks_ordinary_redelivery(self):
  s=replace(load(str(ROOT/'examples/crash_after_effect.json')),crashes=('after_checkpoint',),redeliveries=2,ack_loss=False,concurrent_recovery=1)
  result=verify_scenario(s)
  self.assertEqual(result['broker_delivery']['attempted'],1)
  self.assertEqual(result['broker_delivery']['skipped'],1)
 def test_explicit_progress_loss_reenables_delivery(self):
  s=replace(load(str(ROOT/'examples/crash_after_effect.json')),crashes=(),redeliveries=2,ack_loss=True,concurrent_recovery=1)
  result=verify_scenario(s)
  self.assertEqual(result['broker_delivery']['attempted'],2)
  self.assertTrue(any(e['event']=='progress-lost' for e in result['trace']))
 def test_sqlite_sink(self):
  db=sqlite3.connect(':memory:'); sink=DbApiSink(db); self.assertTrue(sink.emit('m','k','a',1)); self.assertFalse(sink.emit('m','k','a',2))
 def test_http_sink(self):
  Handler.state.keys.clear(); Handler.state.events.clear(); srv=start(0)
  try:
   sink=HttpSink(f'http://127.0.0.1:{srv.server_port}/effect'); self.assertTrue(sink.emit('m','k','a',1))
  finally: srv.shutdown(); srv.server_close()
 def test_benchmark_count(self): self.assertEqual(run(str(ROOT/'benchmark/corpus.json'))['cases'],30)
 def test_benchmark_safe_fp(self): self.assertEqual(run(str(ROOT/'benchmark/corpus.json'))['schedule_verifier']['fp'],0)
 def test_benchmark_effectfence(self): self.assertEqual(run(str(ROOT/'benchmark/corpus.json'))['schedule_verifier']['fn'],0)
if __name__=='__main__': unittest.main()
