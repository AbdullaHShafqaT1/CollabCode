from __future__ import annotations
import json
import os
import sys
from .config import ROOT
from .runtime import run_process, save_json

class CloudCoder:
    def __init__(self, cfg):
        self.cfg = cfg

    def run(self, prompt, workspace, cycle_dir, stop_file, should_stop):
        request = cycle_dir/'coding-job.json'
        response = cycle_dir/'coding-result.json'
        save_json(request, {'cloud':self.cfg['cloud'], 'workspace':str(workspace),
                            'prompt':prompt, 'stop_file':str(stop_file)})
        env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1')
        outcome = run_process([sys.executable, '-m', 'inspector.cloud_worker', str(request),str(response)],
                              ROOT, cycle_dir/'coding.log', self.cfg['cloud']['cycle_timeout'], should_stop, env)
        if outcome['status'] != 'passed' or not response.exists():
            return {'status':outcome['status'].upper(), 'termination_reason':outcome['output'][-3000:], 'process':outcome}
        return json.loads(response.read_text(encoding='utf-8'))
