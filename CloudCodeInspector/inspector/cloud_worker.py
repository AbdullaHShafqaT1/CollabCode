"""A fresh process per coding cycle prevents tool globals leaking between runs."""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

def main():
    job_path, result_path = map(Path, sys.argv[1:])
    job = json.loads(job_path.read_text(encoding='utf-8'))
    root = Path(__file__).resolve().parents[1]/'vendor/cloudcode'
    sys.path.insert(0,str(root))
    sys.path.insert(0,str(root/'NodeCore'))
    from node_core.core import NodeCore, create_cloud_llm_config
    from node_core import tools
    # Never claim an integrated run if CloudCode silently selected mock file/shell tools.
    required = ('_HAS_INSIGHT','_HAS_FORGE','_HAS_PULSE','_HAS_LOG')
    missing = [name for name in required if not getattr(tools,name,False)]
    if missing:
        raise RuntimeError('Missing live CloudCode tool dependencies: '+', '.join(missing))
    def remote_unavailable(*args,**kwargs):
        raise RuntimeError('Remote job dispatch is not configured in this integration; no mock success is allowed.')
    tools.NodeLink.dispatch_remote = staticmethod(remote_unavailable)
    cloud = job['cloud']
    config = create_cloud_llm_config(cloud['base_url'], cloud['model'], os.environ.get(cloud['api_key_env'],'not-needed'))
    config.update(timeout=cloud['timeout'], max_tokens=cloud['max_tokens'], cache_seed=None)
    config['config_list'][0].update(max_tokens=cloud['max_tokens'], max_retries=0)
    from .general_coder import run
    result = run(job,config,tools)
    result_path.write_text(json.dumps(result, indent=2, default=str), encoding='utf-8')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
