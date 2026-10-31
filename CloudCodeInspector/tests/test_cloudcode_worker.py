"""Exercise the real vendored CloudCode worker against a deterministic HTTP fixture."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
from inspector.coding import CloudCoder
from inspector.config import load_config

def test_real_cloudcode_toolchain_with_fake_gpt_oss_server(tmp_path):
    calls=[]
    replies=[
        '```python\n# filename: app.py\ndef add(a, b):\n    return a + b\n```',
        '```python\n# filename: test_app.py\nimport unittest\nfrom app import add\nclass AdditionTest(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\nif __name__ == "__main__":\n    unittest.main()\n```',
        '```bash\npython test_app.py\n```',
        'TASK_COMPLETE',
    ]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append(data)
            raw=json.dumps({'id':'fixture','object':'chat.completion','created':1,'model':data['model'],
                'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':replies[min(len(calls)-1,3)]}}],
                'usage':{'prompt_tokens':100,'completion_tokens':80,'total_tokens':180}}).encode()
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(raw)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    config=tmp_path/'config.json'
    config.write_text(json.dumps({'cloud':{'base_url':f'http://127.0.0.1:{server.server_port}',
                                         'max_rounds':6,'timeout':10,'cycle_timeout':90}}))
    cfg=load_config(config)
    work=tmp_path/'project';work.mkdir()
    cycle=tmp_path/'cycle';cycle.mkdir()
    try:
        result=CloudCoder(cfg).run('Create app.py with add(a,b) returning their sum. Create test_app.py with unittest tests. '
                                  'Run python test_app.py. Required files: app.py, test_app.py.',work,cycle,tmp_path/'STOP',lambda:False)
        assert calls, result
        assert all(c['model']=='openai/gpt-oss-20b' for c in calls)
        assert (work/'app.py').exists(),result
        assert (work/'test_app.py').exists(),result
        assert 'return a + b' in (work/'app.py').read_text()
        assert result.get('commands'),result
        assert result['commands'][0]['result']['exit_code']==0,result
        assert result['status']=='IMPLEMENTATION_READY',result
        assert not (work/'app_logic.py').exists()
        assert (cycle/'coding-result.json').exists()
    finally:
        server.shutdown();server.server_close()
