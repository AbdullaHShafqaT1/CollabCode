"""Conditional, isolated target-page testing. Never attaches to the user's desktop."""
from __future__ import annotations
import json
import sys
from .config import ROOT
from .runtime import run_process, save_json

def select_cases(requests, cfg):
    cases = {c['id']:c for c in cfg['screen']['cases']}
    selected = {}
    for request in requests:
        case = cases.get(request['case_id'])
        if not case or request['requirement'] not in case['requirements']:
            raise ValueError('Screen request does not match a configured case/requirement')
        if not request['doubt'].strip():
            raise ValueError('Screen test requires a concrete doubt')
        selected.setdefault(case['id'], {'case':case, 'triggers':[]})['triggers'].append(request)
    return list(selected.values())

class ScreenTester:
    def __init__(self, cfg):
        self.cfg = cfg

    def run(self, requests, workspace, cycle_dir, should_stop):
        if not requests:
            return []
        if not self.cfg['screen']['enabled']:
            return [{'case_id':r['case_id'], 'status':'not_run', 'requirements':[r['requirement']],
                     'trigger':r['doubt'], 'reason':'Conditional screen testing is disabled in configuration'} for r in requests]
        results = []
        for i, selection in enumerate(select_cases(requests, self.cfg)):
            if should_stop():
                break
            path = cycle_dir/f'screen-{i+1}'
            path.mkdir()
            job = {'local':self.cfg['local'], 'screen':self.cfg['screen'], 'workspace':str(workspace), **selection}
            save_json(path/'job.json',job)
            timeout = self.cfg['screen']['max_steps'] * (self.cfg['local']['timeout']+15)+45
            outcome = run_process([sys.executable,'-m','inspector.screen_worker',str(path/'job.json')],
                                  ROOT,path/'worker.log',timeout,should_stop)
            result = {'case_id':selection['case']['id'], 'requirements':selection['case']['requirements'],
                      'triggers':selection['triggers'], 'status':outcome['status'], 'process':outcome}
            if (path/'result.json').exists():
                result.update(json.loads((path/'result.json').read_text(encoding='utf-8')))
                if outcome['status'] != 'passed':
                    result['status'] = outcome['status']
            elif outcome['status'] == 'passed':
                result.update(status='failed', reason='Screen worker produced no evidence')
            results.append(result)
        return results
