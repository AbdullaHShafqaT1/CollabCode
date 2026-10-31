import json
from pathlib import Path
import threading
import urllib.error
import urllib.request
import pytest
from inspector.config import load_config
from inspector.runtime import workspace_lock
from inspector.web_ui import make_server
from inspector.screen_worker import apply_action, TargetPlanner
from vendor.screencontrol.local_planner import AgentDecision, AgentObservation

def test_workspace_lock_releases_and_rejects_overlap(tmp_path):
    with workspace_lock(tmp_path):
        with pytest.raises(ValueError,match='Another inspector'):
            with workspace_lock(tmp_path): pass
    with workspace_lock(tmp_path): pass

@pytest.mark.parametrize('action,params',[
    ('open_app',{'app_name':'edge'}),('navigate_url',{'url':'https://example.com'}),
    ('press_key',{'combo':'ctrl+l'}),('click_coords',{'x':1200,'y':20})])
def test_screen_adapter_blocks_unrelated_actions(action,params):
    with pytest.raises(ValueError): apply_action(None,AgentDecision('',action,params))

def test_screen_prompt_removes_unrelated_source_task():
    planner=TargetPlanner()
    prompt=planner._build_prompt_text('Check target counter',[],AgentObservation(None,[],[]))
    assert 'YouTube' not in prompt
    assert 'Check target counter' in prompt
    assert 'No open_app' in prompt

def test_pixel_click_alias_is_supported_and_bounded():
    class Mouse:
        calls=[]
        def click(self,x,y):self.calls.append((x,y))
    class Page:mouse=Mouse()
    page=Page()
    apply_action(page,AgentDecision('','click',{'coordinate':[193,322]}))
    assert page.mouse.calls==[(193,322)]
    with pytest.raises(ValueError):apply_action(page,AgentDecision('','click',{'coordinate':[1101,322]}))

def test_loopback_launcher_and_csrf(tmp_path):
    path=tmp_path/'config.json';path.write_text('{}')
    server,_=make_server(load_config(path))
    threading.Thread(target=server.serve_forever,daemon=True).start()
    url=f'http://127.0.0.1:{server.server_port}'
    try:
        with urllib.request.urlopen(url) as response:
            assert b'Build, inspect, improve.' in response.read()
        with urllib.request.urlopen(url+'/api/state') as response:
            state=json.load(response)
            assert not state['running'] and state['defaults']['endpoint']==''
        request=urllib.request.Request(url+'/api/stop',data=b'{}')
        with pytest.raises(urllib.error.HTTPError) as err: urllib.request.urlopen(request)
        assert err.value.code==403
    finally:
        server.shutdown();server.server_close()
