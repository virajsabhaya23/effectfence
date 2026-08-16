from __future__ import annotations
import argparse, json
from dataclasses import asdict
from .io import load, save
from .simulator import verify_scenario
from .explore import explore, minimize_failure
from .benchmark import run as bench
from .reports import junit

def main(argv=None):
    p=argparse.ArgumentParser(prog='effectfence',description='Crash/retry side-effect safety verifier')
    sp=p.add_subparsers(dest='cmd',required=True)
    v=sp.add_parser('verify'); v.add_argument('scenario'); v.add_argument('--out'); v.add_argument('--junit'); v.add_argument('--minimized')
    e=sp.add_parser('explore'); e.add_argument('scenario'); e.add_argument('--out',required=True)
    b=sp.add_parser('benchmark'); b.add_argument('corpus'); b.add_argument('--out',required=True)
    a=p.parse_args(argv)
    if a.cmd=='verify':
        s=load(a.scenario); r=verify_scenario(s)
        if a.out: save(r,a.out)
        if a.junit: junit(r,a.junit)
        if a.minimized and not r['safe']: save(asdict(minimize_failure(s)),a.minimized)
        print(('SAFE' if r['safe'] else 'UNSAFE')+f' {s.id} strategy={s.strategy} effects={r["accepted_effects"]}')
        for x in r['violations']: print(x['kind'],x)
        print('certificate_sha256='+r['certificate_sha256']); return 0 if r['safe'] else 2
    if a.cmd=='explore':
        rows=explore(load(a.scenario)); save({'cases':rows},a.out); bad=sum(1 for x in rows if not x['result']['safe']); print(f'explored={len(rows)} unsafe={bad}'); return 0
    r=bench(a.corpus); save(r,a.out); print(json.dumps({'cases':r['cases'],'schedule_verifier':r['schedule_verifier'],'ordinary_happy_path':r['ordinary_happy_path'],'by_strategy':r['by_strategy']},indent=2)); return 0
if __name__=='__main__': raise SystemExit(main())
