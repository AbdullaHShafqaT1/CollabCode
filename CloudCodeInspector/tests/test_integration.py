from __future__ import annotations
import copy
import json
from pathlib import Path
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest
from inspector.config import ROOT, load_config
from inspector.evidence import snapshot, changes, freeze_checks, check_integrity
from inspector.loop import InspectionLoop, verdict
from inspector.models import LocalInspector, cloud_preflight, require_local
from inspector.runtime import run_process
from inspector.screen import ScreenTester, select_cases

@pytest.fixture
def cfg(tmp_path):
    path = tmp_path/'config.json'
    path.write_text(json.dumps({'requirements':[{'id':'R1','text':'add(a,b) returns their sum'}],
        'checks':[{'id':'sum','requirements':['R1'],'argv':['{python}','-I','{checks}/accept.py','{workspace}'],'timeout':10}],
        'limits':{'max_cycles':3,'stagnant_cycles':1}}))
    checks = tmp_path/'checks'
    checks.mkdir()
    (checks/'accept.py').write_text("import runpy,sys\nfrom pathlib import Path\nf=runpy.run_path(str(Path(sys.argv[1])/'app.py'))\nassert f['add'](2,3)==5\nassert f['add'](-4,2)==-2\n")
    return load_config(path)

class Coder:
    def __init__(self, repair=True):
        self.calls = []
        self.repair = repair
    def run(self,prompt,workspace,*args):
        self.calls.append(prompt)
        op = '+' if self.repair and len(self.calls)>1 else '-'
        (workspace/'app.py').write_text(f'def add(a,b):\n    return a {op} b\n')
        return {'status':'COMPLETED','commands':[],'verification':[]}

class Inspector:
    def inspect(self,prompt,requirements,evidence,path):
        passed = evidence['checks'][0]['status']=='passed'
        return {'assessments':[{'id':'R1','status':'supported' if passed else 'missing','evidence':['check:sum'],
                               'reason':'sum assertions passed' if passed else 'addition is wrong'}],
                'issues':[] if passed else [{'requirement':'R1','problem':'returns subtraction','action':'use addition and rerun sum check'}],
                'screen_requests':[]}

class NoScreen:
    def run(self,*args):
        raise AssertionError('Screen must not run for ordinary code/tests')

def run_loop(cfg,tmp_path,coder=None,inspector=None,screen=None,**kwargs):
    return InspectionLoop(cfg,coder or Coder(),inspector or Inspector(),screen or NoScreen(),logger=lambda text:None).run(
        'Implement add(a,b) in app.py to return the sum. ORIGINAL_MARKER',tmp_path/'target',preflight=False,**kwargs)

def test_repairs_using_original_prompt_and_real_checks(cfg,tmp_path):
    coder = Coder()
    result = run_loop(cfg,tmp_path,coder)
    assert result['status']=='VERIFIED_FOR_CONFIGURED_REQUIREMENTS'
    assert len(result['cycles'])==2
    assert 'ORIGINAL_MARKER' in coder.calls[1]
    assert 'returns subtraction' in coder.calls[1]
    assert 'assert' in coder.calls[1]
    assert Path(result['run_dir'],'cycle-1/follow-up-prompt.txt').exists()
    assert Path(result['run_dir'],'cycle-2/after.json').exists()

def test_stagnation(cfg,tmp_path):
    result = run_loop(cfg,tmp_path,Coder(False))
    assert result['status']=='STALLED'
    assert len(result['cycles'])==2

def test_cycle_limit(cfg,tmp_path):
    cfg['limits']['max_cycles']=1
    assert run_loop(cfg,tmp_path)['status']=='CYCLE_LIMIT'

def test_missing_colab_never_calls_coder(cfg,tmp_path,monkeypatch):
    import inspector.loop as loop
    monkeypatch.setattr(loop,'require_local',lambda cfg:[])
    coder = Coder()
    result = InspectionLoop(cfg,coder=coder,logger=lambda text:None).run('task',tmp_path/'target')
    assert result['status']=='BLOCKED'
    assert 'cloud.base_url' in result['error']
    assert not coder.calls and not result['cloud_coding_attempted']

def test_inspector_error_preserves_evidence(cfg,tmp_path):
    class Broken:
        def inspect(self,*args): raise ValueError('bad JSON')
    result = run_loop(cfg,tmp_path,inspector=Broken())
    assert result['status']=='BLOCKED'
    assert Path(result['run_dir'],'cycle-1/evidence.json').exists()

def test_cancellation(cfg,tmp_path):
    class Cancel:
        def run(self,prompt,workspace,cycle,stop_file,should_stop):
            stop_file.touch()
            return {'status':'CANCELLED'}
    assert run_loop(cfg,tmp_path,coder=Cancel())['status']=='CANCELLED'

def test_claims_alone_do_not_verify():
    requirements=[{'id':'R1','text':'works'}]
    review={'assessments':[{'id':'R1','status':'supported','reason':'model says so','evidence':['file:app.py']}],'issues':[]}
    evidence={'incomplete_source':False,'checks':[],'screen':[],'coding_phase':{'status':'COMPLETED'}}
    result=verdict(requirements,review,evidence)
    assert not result['verified'] and result['uncovered_requirements']==['R1']

@pytest.mark.parametrize('status',['failed','timeout','cancelled'])
def test_failed_checks_block_completion(status):
    evidence={'incomplete_source':False,'checks':[{'id':'x','requirements':['R1'],'status':status}], 'screen':[],
              'coding_phase':{'status':'COMPLETED'}}
    review={'assessments':[{'id':'R1','status':'supported','reason':'claims'}]}
    assert not verdict([{'id':'R1'}],review,evidence)['verified']

def test_snapshot_excludes_secrets_and_records_truncation(cfg,tmp_path):
    target=tmp_path/'files';target.mkdir()
    (target/'.env').write_text('password=never include')
    (target/'credentials.json').write_text('private')
    (target/'app.py').write_text('a'*15000)
    snap=snapshot(target,cfg)
    assert list(snap['files'])==['app.py'] and snap['incomplete']

def test_actual_deleted_files_are_evidence(cfg,tmp_path):
    p=tmp_path/'app.py';p.write_text('old')
    before=snapshot(tmp_path,cfg);p.unlink()
    assert any(d['kind']=='deleted' for d in changes(before,snapshot(tmp_path,cfg)))

def test_frozen_check_tamper(cfg,tmp_path):
    run=tmp_path/'run';run.mkdir()
    hashes=freeze_checks(cfg,run)
    (run/'checks/accept.py').write_text('pass')
    with pytest.raises(ValueError,match='modified'): check_integrity(run,hashes)

def test_process_timeout_and_cancel(tmp_path):
    command=[sys.executable,'-c','import time;time.sleep(20)']
    assert run_process(command,tmp_path,tmp_path/'timeout.log',1)['status']=='timeout'
    assert run_process(command,tmp_path,tmp_path/'cancel.log',10,lambda:True)['status']=='cancelled'

def test_child_is_stopped_when_parent_exits(tmp_path):
    import time
    child="import time;from pathlib import Path;time.sleep(2);Path('escaped.txt').write_text('bad')"
    parent="import subprocess,sys;subprocess.Popen([sys.executable,'-c',"+repr(child)+"]);print('parent finished')"
    result=run_process([sys.executable,'-c',parent],tmp_path,tmp_path/'parent.log',5)
    assert result['status']=='passed'
    time.sleep(2.5)
    assert not (tmp_path/'escaped.txt').exists()

def test_no_doubt_no_screen_process(cfg,tmp_path):
    assert ScreenTester(cfg).run([],tmp_path,tmp_path,lambda:False)==[]

def test_screen_gate_requires_configured_requirement(cfg):
    cfg['screen']['cases']=[{'id':'button','requirements':['R1'],'goal':'click'}]
    assert select_cases([{'case_id':'button','requirement':'R1','doubt':'button may not respond'}],cfg)
    with pytest.raises(ValueError): select_cases([{'case_id':'button','requirement':'R2','doubt':'wrong'}],cfg)
    with pytest.raises(ValueError): select_cases([{'case_id':'button','requirement':'R1','doubt':''}],cfg)

def test_screen_observations_are_reinspected(cfg,tmp_path):
    cfg['screen'].update(enabled=True,cases=[{'id':'button','requirements':['R1'],'goal':'check UI'}])
    class Review(Inspector):
        calls=0
        def inspect(self,prompt,requirements,evidence,path):
            self.calls+=1
            result=super().inspect(prompt,requirements,evidence,path)
            result['assessments'][0]['status']='supported'
            result['issues']=[]
            result['screen_requests']=[] if evidence['screen'] else [{'case_id':'button','requirement':'R1','doubt':'click behavior unclear'}]
            return result
    class Screen:
        calls=0
        def run(self,requests,*args):
            self.calls+=1
            assert requests[0]['doubt']=='click behavior unclear'
            return [{'case_id':'button','requirements':['R1'],'status':'passed','assertions':[{'passed':True}]}]
    class Good(Coder):
        def run(self,prompt,workspace,*args):
            (workspace/'app.py').write_text('def add(a,b): return a+b')
            return {'status':'COMPLETED'}
    review=Review();screen=Screen()
    result=run_loop(cfg,tmp_path,coder=Good(),inspector=review,screen=screen)
    assert result['status']=='VERIFIED_FOR_CONFIGURED_REQUIREMENTS'
    assert review.calls==2 and screen.calls==1

def test_malformed_inspector_response_rejected(cfg,monkeypatch):
    inspector=LocalInspector(cfg)
    monkeypatch.setattr(inspector,'ask',lambda *args:{'assessments':[]})
    with pytest.raises(ValueError,match='omitted'): inspector.inspect('task',cfg['requirements'],{'evidence_ids':[]},None)
    monkeypatch.setattr(inspector,'ask',lambda *args:{'assessments':[{'id':'R1','status':'supported','evidence':['madeup'],'reason':'ok'}]})
    with pytest.raises(ValueError,match='nonexistent'): inspector.inspect('task',cfg['requirements'],{'evidence_ids':[]},None)

def test_cloud_and_remote_local_models_rejected(cfg,tmp_path,monkeypatch):
    import inspector.models as models
    monkeypatch.setattr(models,'local_inventory',lambda cfg:[{'name':'qwen3:8b','remote_host':'https://cloud'}])
    with pytest.raises(ValueError,match='local model'): require_local(cfg)
    p=tmp_path/'bad.json';p.write_text(json.dumps({'cloud':{'model':'qwen2.5-coder:32b'}}))
    with pytest.raises(ValueError,match='GPT OSS'): load_config(p)

@pytest.mark.parametrize('mode',['good','wrong_model','history_drop','ignores_limit'])
def test_openai_serving_contract(cfg,tmp_path,mode):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self): self.respond({'data':[{'id':'openai/gpt-oss-20b'}]})
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            import re
            marker=re.search(r'INSPECT_[a-f0-9]+',json.dumps(body['messages']))
            content=marker.group() if marker else ''
            if mode=='history_drop' and len(body['messages'])==3: content='unknown'
            tokens=20 if mode=='ignores_limit' else min(1,body['max_tokens'])
            self.respond({'model':'qwen' if mode=='wrong_model' else body['model'],
                          'choices':[{'message':{'content':content},'finish_reason':'stop'}], 'usage':{'completion_tokens':tokens}})
        def respond(self,data):
            raw=json.dumps(data).encode();self.send_response(200);self.end_headers();self.wfile.write(raw)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    cfg['cloud']['base_url']=f'http://127.0.0.1:{server.server_port}'
    try:
        if mode=='good': assert cloud_preflight(cfg,tmp_path/'preflight.json')['status']=='passed'
        else:
            with pytest.raises(ValueError): cloud_preflight(cfg,tmp_path/'preflight.json')
    finally:
        server.shutdown();server.server_close()

def test_protect_original_projects(cfg):
    with pytest.raises(ValueError,match='target projects'):
        InspectionLoop(cfg).run('task',ROOT.parent/'NodeCore',preflight=False)
