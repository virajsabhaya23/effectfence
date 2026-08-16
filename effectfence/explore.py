from __future__ import annotations
from dataclasses import replace, asdict
from .model import Scenario
from .simulator import verify_scenario, BOUNDARIES

def explore(base: Scenario):
    cases=[]
    variants=[()]
    variants += [(p,) for p in BOUNDARIES]
    variants += [('after_effect','before_ack'),('after_effect','after_checkpoint')]
    seen=set()
    for crashes in variants:
        for ack_loss in (False,True):
            for recoveries in (1,2):
                key=(crashes,ack_loss,recoveries)
                if key in seen: continue
                seen.add(key)
                s=replace(base,crashes=crashes,ack_loss=ack_loss,concurrent_recovery=recoveries)
                r=verify_scenario(s)
                cases.append({'scenario':asdict(s),'result':r})
    return cases

def minimize_failure(s: Scenario):
    r=verify_scenario(s)
    if r['safe']: return s
    current=s
    # Remove crashes greedily while preserving the same safety failure.
    crashes=list(s.crashes)
    i=0
    while i < len(crashes):
        trial=replace(current,crashes=tuple(crashes[:i]+crashes[i+1:]))
        if not verify_scenario(trial)['safe']:
            crashes.pop(i); current=trial
        else: i+=1
    if current.concurrent_recovery>1:
        trial=replace(current,concurrent_recovery=1)
        if not verify_scenario(trial)['safe']: current=trial
    if current.redeliveries>1:
        for n in range(1,current.redeliveries):
            trial=replace(current,redeliveries=n)
            if not verify_scenario(trial)['safe']: current=trial; break
    return current
