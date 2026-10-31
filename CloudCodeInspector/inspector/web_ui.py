"""Loopback-only launcher; works with Python distributions that omit Tk/Tcl."""
from __future__ import annotations
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
import webbrowser
from .config import ROOT, load_config
from .loop import InspectionLoop
from .models import local_inventory

class Controller:
    def __init__(self,cfg):
        self.cfg=cfg
        self.lock=threading.Lock()
        self.state={'running':False,'messages':[],'result':None,'run_dir':None,'stop_requested':False}

    def log(self,text):
        with self.lock:
            self.state['messages'].append(text)
            if text.startswith('Evidence folder: '):
                self.state['run_dir']=text.split(': ',1)[1]
                if self.state['stop_requested']:
                    (Path(self.state['run_dir'])/'STOP').touch()

    def start(self,data):
        with self.lock:
            if self.state['running']:
                raise ValueError('A run is already active')
            cfg=copy.deepcopy(self.cfg)
            cfg['cloud'].update(base_url=data['endpoint'].strip(),model=data['cloud_model'].strip())
            cfg['local'].update(inspection_model=data['inspection_model'].strip(),vision_model=data['vision_model'].strip())
            cfg['limits']['max_cycles']=int(data['max_cycles'])
            cfg['checks_dir']=str((Path(cfg['_base'])/cfg.get('checks_dir','checks')).resolve())
            if not data['prompt'].strip() or not data['workspace'].strip():
                raise ValueError('Enter the original task and target project path')
            launch=ROOT/'runs/launcher'
            launch.mkdir(parents=True,exist_ok=True)
            config_file=launch/'web-config.json'
            config_file.write_text(json.dumps(cfg,indent=2),encoding='utf-8')
            cfg=load_config(config_file)
            self.state.update(running=True,messages=[],result=None,run_dir=None,stop_requested=False)
        def work():
            try:
                result=InspectionLoop(cfg,logger=self.log).run(data['prompt'],data['workspace'],data.get('inspect_only',False))
                with self.lock:
                    self.state['result']=result
            except Exception as exc:
                self.log('Cannot run: '+str(exc))
            finally:
                with self.lock:
                    self.state['running']=False
        threading.Thread(target=work,daemon=True).start()

    def stop(self):
        with self.lock:
            self.state['stop_requested']=True
            if self.state['run_dir']:
                (Path(self.state['run_dir'])/'STOP').touch()
            self.state['messages'].append('Stop requested. A current local-model call can take up to its timeout to return.')

def make_server(cfg,port=0):
    control=Controller(cfg)
    token=secrets.token_urlsafe(24)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def respond(self,value,status=200,content_type='application/json'):
            raw=value.encode() if isinstance(value,str) else json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type',content_type)
            self.send_header('Content-Length',str(len(raw)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.end_headers();self.wfile.write(raw)
        def do_GET(self):
            if self.path=='/':
                html=(ROOT/'inspector/launcher.html').read_text(encoding='utf-8')
                return self.respond(html.replace('__TOKEN__',token),200,'text/html; charset=utf-8')
            if self.path=='/api/state':
                with control.lock:
                    state=copy.deepcopy(control.state)
                state['defaults']={'endpoint':cfg['cloud']['base_url'],'cloud_model':cfg['cloud']['model'],
                    'inspection_model':cfg['local']['inspection_model'],'vision_model':cfg['local']['vision_model'],
                    'max_cycles':cfg['limits']['max_cycles'],'workspace':str(ROOT/'targets/my-project'),
                    'screen_enabled':cfg['screen']['enabled'],'checks':len(cfg['checks']), 'cases':len(cfg['screen']['cases'])}
                return self.respond(state)
            self.respond({'error':'Not found'},404)
        def do_POST(self):
            if self.headers.get('X-Inspector-Token')!=token:
                return self.respond({'error':'Invalid launcher token'},403)
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0 <= size <= 200000:
                    raise ValueError('Request too large')
                data=json.loads(self.rfile.read(size) or '{}')
                if self.path=='/api/start': control.start(data)
                elif self.path=='/api/stop': control.stop()
                elif self.path=='/api/models':
                    return self.respond({'models':[m['name'] for m in local_inventory(cfg) if not m.get('remote_host') and ':cloud' not in m['name']]})
                else: return self.respond({'error':'Not found'},404)
                self.respond({'ok':True})
            except Exception as exc:
                self.respond({'error':str(exc)},400)
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    return server,control

def launch(config_path,port=0,open_browser=True):
    server,control=make_server(load_config(config_path),port)
    url=f'http://127.0.0.1:{server.server_port}'
    print('CloudCode Inspector launcher: '+url,flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        control.stop()
    finally:
        from .runtime import cancel_all_children
        cancel_all_children()
        server.server_close()
