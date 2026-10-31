from __future__ import annotations
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]

def load_config(path):
    path = Path(path).resolve()
    cfg = json.loads(path.read_text(encoding='utf-8-sig'))
    cfg['_base'] = str(path.parent)
    cfg.setdefault('cloud', {})
    cfg.setdefault('local', {})
    cfg.setdefault('limits', {})
    cfg.setdefault('screen', {})
    cfg.setdefault('checks', [])
    cfg.setdefault('requirements', [])
    defaults = {
        'cloud': {'base_url': '', 'model': 'openai/gpt-oss-20b', 'api_key_env': 'COLAB_API_KEY',
                  'timeout': 180, 'max_tokens': 8192, 'max_rounds': 24, 'cycle_timeout': 1800},
        'local': {'base_url': 'http://127.0.0.1:11434', 'inspection_model': 'qwen3:8b',
                  'vision_model': 'qwen2.5vl:3b', 'timeout': 300, 'num_ctx': 16384, 'num_predict': 4096},
        'limits': {'max_cycles': 3, 'stagnant_cycles': 2, 'max_files': 160,
                   'max_file_chars': 12000, 'max_evidence_chars': 24000},
        'screen': {'enabled': False, 'entry': 'index.html', 'max_steps': 6, 'cases': []},
    }
    for group, values in defaults.items():
        for key, value in values.items():
            cfg[group].setdefault(key, value)
    if not re.fullmatch(r'(?:openai/)?gpt-oss(?:-|:)(?:20b|120b)', cfg['cloud']['model']):
        raise ValueError('Cloud model must explicitly be GPT OSS: openai/gpt-oss-20b, openai/gpt-oss-120b, or Ollama gpt-oss:20b/120b.')
    parsed = urlparse(cfg['local']['base_url'])
    if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1'):
        raise ValueError('Inspection and vision must use local Ollama on a loopback address.')
    for group, keys in {'cloud': ['timeout', 'max_tokens', 'max_rounds', 'cycle_timeout'],
                        'local': ['timeout', 'num_ctx', 'num_predict'],
                        'limits': list(defaults['limits']), 'screen': ['max_steps']}.items():
        for key in keys:
            if type(cfg[group][key]) is not int or cfg[group][key] < 1:
                raise ValueError(f'{group}.{key} must be a positive integer')
    if not isinstance(cfg['screen']['enabled'], bool):
        raise ValueError('screen.enabled must be a boolean')
    ids = set()
    for requirement in cfg['requirements']:
        if not re.fullmatch(r'R[1-9][0-9]*', requirement.get('id', '')) or not requirement.get('text', '').strip():
            raise ValueError('Each requirement needs id R1, R2, ... and nonempty text.')
        if requirement['id'] in ids:
            raise ValueError('Duplicate requirement ID')
        ids.add(requirement['id'])
    for check in cfg['checks']:
        if not check.get('id') or not isinstance(check.get('argv'), list) or not check['argv'] or not all(isinstance(a,str) for a in check['argv']):
            raise ValueError('Checks need an id and a nonempty argv array (no shell command string).')
        if not check.get('requirements') or not set(check['requirements']) <= ids:
            raise ValueError('Checks must map to configured requirement IDs.')
        if type(check.get('timeout', 60)) is not int or check.get('timeout',60) < 1:
            raise ValueError('Check timeout must be positive.')
    check_ids = [c['id'] for c in cfg['checks']]
    if len(set(check_ids)) != len(check_ids):
        raise ValueError('Duplicate check ID')
    case_ids = []
    for case in cfg['screen']['cases']:
        case_ids.append(case.get('id'))
        if not case.get('id') or not case.get('goal') or not case.get('requirements') or not set(case['requirements']) <= ids:
            raise ValueError('Screen cases need id, goal and configured requirement IDs.')
        if not case.get('assertions'):
            raise ValueError('Screen cases need independent assertions; model completion alone is insufficient.')
        for assertion in case['assertions']:
            if not assertion.get('selector') or assertion.get('kind') not in ('text', 'visible', 'value'):
                raise ValueError('Screen assertions need a selector and kind text/visible/value.')
            if assertion['kind'] != 'visible' and 'expected' not in assertion:
                raise ValueError('Text/value assertions need expected content.')
        for checkpoint in case.get('checkpoints',[]):
            if not checkpoint.get('selector') or 'expected' not in checkpoint:
                raise ValueError('Screen checkpoints need a selector and expected text.')
    if len(set(case_ids)) != len(case_ids):
        raise ValueError('Duplicate screen case ID')
    return cfg

def api_key(cfg):
    return os.environ.get(cfg['cloud']['api_key_env'], 'not-needed')

def resolve_path(cfg, value):
    return (Path(cfg['_base']) / value).resolve()
