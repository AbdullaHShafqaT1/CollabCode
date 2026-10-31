"""Explicit, allowlisted source import. Never copies runtimes, secrets or run data."""
from pathlib import Path
import ast
import hashlib
import json
import shutil

DEST = Path(__file__).resolve().parents[1]
CLOUD = DEST.parent
SCREEN = CLOUD.parents[1] / 'ScreenControlProjects' / 'Test1'

def main():
    if (DEST / 'vendor').exists():
        raise SystemExit('vendor already exists; refusing to overwrite the integrated snapshot')
    manifest = []
    def copy(src, target, text=None):
        target.parent.mkdir(parents=True, exist_ok=True)
        if text is None:
            shutil.copyfile(src, target)
        else:
            target.write_text(text, encoding='utf-8')
        manifest.append({'source': str(src), 'destination': str(target.relative_to(DEST)),
                         'source_sha256': hashlib.sha256(src.read_bytes()).hexdigest(),
                         'copied_sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
                         'adapted': text is not None})
    for component in ('NodeCore/node_core', 'NodeForge/tools', 'NodeInsight/file_reader_tool',
                      'NodePulse', 'NodeLog/nodelog', 'NodeLink/nodelink'):
        for src in (CLOUD / component).rglob('*.py'):
            if any(p.startswith('.') or p in ('__pycache__', 'tests') for p in src.relative_to(CLOUD).parts):
                continue
            if src.name.startswith(('test_', 'demo')):
                continue
            copy(src, DEST / 'vendor/cloudcode' / src.relative_to(CLOUD))
    # Keep the complete original planner as reference, but import only its local
    # provider and data types. Desktop initialization and cloud providers are unnecessary.
    source = SCREEN / 'core/agent_planner.py'
    copy(source, DEST / 'vendor/screencontrol/reference/agent_planner.py')
    copy(SCREEN / 'core/models.py', DEST / 'vendor/screencontrol/models.py')
    code = source.read_text(encoding='utf-8')
    tree = ast.parse(code)
    names = {'_clean_json_text', 'parse_agent_decision_json', 'AgentObservation',
             'AgentDecision', 'OllamaLlavaAgentPlanner'}
    chunks = []
    for node in tree.body:
        if getattr(node, 'name', None) in names:
            start = min([node.lineno] + [d.lineno for d in getattr(node, 'decorator_list', [])])
            chunks.append('\n'.join(code.splitlines()[start-1:node.end_lineno]))
    header = '''from __future__ import annotations
import os, re, json, base64, logging, time
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field
import requests
from .models import WindowInfo, UIElement, ActionReport, Rect
logger = logging.getLogger("Inspector.LocalVision")
SYSTEM_PROMPT = "Inspect only the supplied target page. Follow the task's action restrictions. Return one JSON action."
'''
    copy(source, DEST / 'vendor/screencontrol/local_planner.py', header + '\n\n'.join(chunks) + '\n')
    (DEST / 'vendor/screencontrol/__init__.py').write_text('', encoding='utf-8')
    (DEST / 'vendor/__init__.py').write_text('', encoding='utf-8')
    (DEST / 'SOURCE_MANIFEST.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(f'Imported {len(manifest)} source files; source projects unchanged.')

if __name__ == '__main__':
    main()
