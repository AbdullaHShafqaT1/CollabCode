"""Bounded real-model validation against a deliberately broken, controlled fixture."""
from pathlib import Path
import json
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from inspector.config import load_config
from inspector.loop import InspectionLoop
from inspector.runtime import save_json

def main():
    folder = ROOT/'validation/local-inspection'
    checks = folder/'checks'
    checks.mkdir(parents=True,exist_ok=True)
    (checks/'accept.py').write_text("import runpy,sys\nfrom pathlib import Path\nm=runpy.run_path(str(Path(sys.argv[1])/'app.py'))\nassert m['add'](2,3)==5, 'add(2,3) must return 5'\n",encoding='utf-8')
    config = {'requirements':[{'id':'R1','text':'app.py must implement add(a,b) that returns the sum of both numbers.'}],
              'checks_dir':'checks','checks':[{'id':'addition','requirements':['R1'],
                          'argv':['{python}','-I','{checks}/accept.py','{workspace}']}],
              'local':{'timeout':180,'num_predict':2048}}
    save_json(folder/'config.json',config)
    cfg = load_config(folder/'config.json')
    target = ROOT/'targets/local-validation'
    target.mkdir(parents=True,exist_ok=True)
    prompt = 'Implement add(a,b) in app.py. It must return the sum of a and b, including negative values.'
    outcomes = []
    for label, operator in [('broken','-'),('repaired','+')]:
        (target/'app.py').write_text(f'def add(a,b):\n    return a {operator} b\n',encoding='utf-8')
        start = time.monotonic()
        result = InspectionLoop(cfg).run(prompt,target,inspect_only=True)
        outcomes.append({'case':label,'duration_seconds':round(time.monotonic()-start,2),'result':result})
        save_json(folder/'results.json',outcomes)
    return 0 if (outcomes[0]['result']['status']=='INSPECTION_COMPLETE_WITH_GAPS' and
                 outcomes[1]['result']['status']=='VERIFIED_FOR_CONFIGURED_REQUIREMENTS') else 1

if __name__ == '__main__':
    raise SystemExit(main())
