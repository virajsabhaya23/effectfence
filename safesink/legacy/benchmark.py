from __future__ import annotations
import json
from dataclasses import replace
from pathlib import Path
from .io import load
from .simulator import verify_scenario


def ordinary_happy_path(s):
    """Typical one-shot test: no crash, no lost ack, no concurrent/stale retry."""
    clean=replace(s,crashes=(),redeliveries=1,ack_loss=False,concurrent_recovery=1,stale_retry_delay=None)
    return verify_scenario(clean)


def run(corpus_path: str):
    corpus=Path(corpus_path); meta=json.loads(corpus.read_text())
    rows=[]; tp=fp=tn=fn=0; ordinary_tp=ordinary_fp=0
    by_strategy={}
    for entry in meta['cases']:
        s=load(str(corpus.parent/entry['file']))
        expected_unsafe=bool(entry['unsafe'])
        explored=verify_scenario(s)
        detected=not explored['safe']
        ordinary=ordinary_happy_path(s)
        ordinary_detected=not ordinary['safe']
        if expected_unsafe and detected: tp+=1
        elif expected_unsafe and not detected: fn+=1
        elif not expected_unsafe and detected: fp+=1
        else: tn+=1
        if expected_unsafe and ordinary_detected: ordinary_tp+=1
        if not expected_unsafe and ordinary_detected: ordinary_fp+=1
        st=by_strategy.setdefault(s.strategy,{'unsafe_cases':0,'unsafe_detected':0,'safe_cases':0,'safe_flagged':0})
        if expected_unsafe:
            st['unsafe_cases']+=1; st['unsafe_detected']+=int(detected)
        else:
            st['safe_cases']+=1; st['safe_flagged']+=int(detected)
        rows.append({'id':entry['id'],'family':entry['family'],'source':entry['source'],'strategy':s.strategy,
                     'unsafe_expected':expected_unsafe,'schedule_verifier_detected':detected,
                     'ordinary_happy_path_detected':ordinary_detected,
                     'certificate_sha256':explored['certificate_sha256'],'accepted_effects':explored['accepted_effects']})
    return {
      'name':meta['name'],'cases':len(rows),
      'schedule_verifier':{'tp':tp,'fp':fp,'tn':tn,'fn':fn,'precision':tp/max(1,tp+fp),'recall':tp/max(1,tp+fn)},
      'ordinary_happy_path':{'tp':ordinary_tp,'fp':ordinary_fp,'recall':ordinary_tp/max(1,tp+fn)},
      'by_strategy':by_strategy,'rows':rows
    }
