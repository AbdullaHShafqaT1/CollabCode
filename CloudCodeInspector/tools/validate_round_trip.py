"""Real CloudCode tools + real local inspection; coding responses are deterministic fixtures."""
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sys
import threading
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from inspector.config import load_config
from inspector.loop import InspectionLoop
from inspector.runtime import save_json

def main():
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append(request)
            index=len(calls)
            content=('```python\n# filename: app.py\ndef add(a,b):\n    return a '+('-' if index==1 else '+')+' b\n```') if index%2 else 'TASK_COMPLETE'
            body=json.dumps({'id':'controlled-fixture','object':'chat.completion','created':1,'model':request['model'],
                'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':content}}],
                'usage':{'prompt_tokens':100,'completion_tokens':40,'total_tokens':140}}).encode()
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(body)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    folder=ROOT/'validation/round-trip'
    (folder/'checks').mkdir(parents=True,exist_ok=True)
    (folder/'checks/accept.py').write_text("import runpy,sys\nfrom pathlib import Path\nf=runpy.run_path(str(Path(sys.argv[1])/'app.py'))['add']\nassert f(2,3)==5\nassert f(-4,2)==-2\n",encoding='utf-8')
    config={'cloud':{'base_url':f'http://127.0.0.1:{server.server_port}','max_rounds':4,'cycle_timeout':90},
        'local':{'timeout':180,'num_predict':2048}, 'limits':{'max_cycles':2},
        'requirements':[{'id':'R1','text':'add(a,b) in app.py returns the sum, including negative inputs.'}],
        'checks_dir':'checks','checks':[{'id':'addition','requirements':['R1'],'argv':['{python}','-I','{checks}/accept.py','{workspace}']}]}
    save_json(folder/'config.json',config)
    try:
        outcome=InspectionLoop(load_config(folder/'config.json')).run(
            'Implement add(a,b) in app.py returning their sum, including negative values.',ROOT/'targets/round-trip-validation',preflight=False)
        result={'coding_backend':'deterministic HTTP fixture, NOT live GPT OSS',
                'inspection_backend':'real local qwen3:8b','outcome':outcome,
                'coding_requests':len(calls),'followup_received':len(calls)>2 and 'LOCAL INSPECTION FEEDBACK' in calls[2]['messages'][1]['content']}
        save_json(folder/'result.json',result)
        save_json(folder/'coding-fixture-requests.json',calls)
        print(json.dumps(result,indent=2))
        return 0 if outcome['status']=='VERIFIED_FOR_CONFIGURED_REQUIREMENTS' and len(outcome['cycles'])==2 and result['followup_received'] else 1
    finally:
        server.shutdown();server.server_close()

if __name__=='__main__':raise SystemExit(main())
