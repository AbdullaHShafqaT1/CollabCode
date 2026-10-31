"""Test the real local screen planner only on an owned counter fixture."""
from pathlib import Path
import json
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from inspector.config import load_config
from inspector.screen import ScreenTester
from inspector.runtime import save_json

def main():
    cfg=load_config(ROOT/'examples/counter/config.json')
    cfg['screen']['max_steps']=3
    cfg['local']['timeout']=120
    target=ROOT/'targets/vision-validation'
    target.mkdir(parents=True,exist_ok=True)
    (target/'index.html').write_text('''<!doctype html><html><head><title>Inspector controlled counter</title>
<style>body{font:24px Arial;background:#edf2f7;padding:100px}button{padding:20px;font:24px Arial;margin:12px}</style></head>
<body><h1>Counter validation</h1><p>Count: <strong id="count">0</strong></p>
<button onclick="document.getElementById('count').textContent=Number(document.getElementById('count').textContent)+1">Increment</button>
<button onclick="document.getElementById('count').textContent='0'">Reset</button></body></html>''',encoding='utf-8')
    folder=ROOT/'validation'/('vision-'+str(int(time.time())))
    folder.mkdir(parents=True)
    request={'requirement':'R1','case_id':'increment','doubt':'Controlled validation: source inspection alone has not established that clicking Increment updates the visible counter.'}
    result=ScreenTester(cfg).run([request],target,folder,lambda:False)
    save_json(folder/'results.json',result)
    print(json.dumps({'status':result[0]['status'],'folder':str(folder)},indent=2))
    return 0 if result[0]['status']=='passed' else 1

if __name__=='__main__':
    raise SystemExit(main())
