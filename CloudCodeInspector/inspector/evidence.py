from __future__ import annotations
import difflib
import hashlib
import json
import os
import sys
from pathlib import Path
from .runtime import run_process, save_json

SKIP = {'.git','.venv','venv','node_modules','__pycache__','.cache','.pytest_cache',
        '.cloudcode','nodelog_data','logs','dist','build','.aws','.ssh','.codex','vendor'}
TEXT = {'.py','.js','.ts','.tsx','.jsx','.html','.css','.json','.md','.txt','.toml','.yaml','.yml','.sql','.sh','.ps1','.svg'}

def safe_file(path):
    name = path.name.lower()
    return (path.suffix.lower() in TEXT and not name.startswith('.env') and
            not any(word in name for word in ('credential','secret','private_key','token.json')))

def snapshot(workspace, cfg):
    root = Path(workspace).resolve()
    files, omitted = {}, []
    total_chars = 0
    limits = cfg['limits']
    for folder, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP and not d.startswith('.') and
                         not (Path(folder)/d).is_symlink() and (Path(folder)/d).resolve().is_relative_to(root))
        for name in sorted(names):
            path = Path(folder)/name
            if path.is_symlink() or not path.resolve().is_relative_to(root) or not safe_file(path):
                continue
            rel = path.relative_to(root).as_posix()
            if len(files) >= limits['max_files']:
                omitted.append(rel)
                continue
            if path.stat().st_size > 2_000_000:
                omitted.append(rel)
                continue
            raw = path.read_bytes()
            try:
                content = raw.decode('utf-8-sig')
            except UnicodeDecodeError:
                omitted.append(rel)
                continue
            budget = max(0, min(limits['max_file_chars'], limits['max_evidence_chars']-total_chars))
            excerpt = content[:budget]
            total_chars += len(excerpt)
            files[rel] = {'sha256':hashlib.sha256(raw).hexdigest(), 'size':len(raw),
                          'content':excerpt, 'truncated':len(excerpt)<len(content)}
    return {'files':files, 'omitted':omitted, 'incomplete':bool(omitted or any(f['truncated'] for f in files.values()))}

def changes(before, after):
    rows = []
    for path in sorted(before['files'].keys() | after['files'].keys()):
        old, new = before['files'].get(path), after['files'].get(path)
        if old and new and old['sha256'] == new['sha256']:
            continue
        diff = ''.join(difflib.unified_diff((old or {}).get('content','').splitlines(True),
                       (new or {}).get('content','').splitlines(True), fromfile=path+' (before)', tofile=path+' (after)'))
        rows.append({'path':path, 'kind':'added' if old is None else 'deleted' if new is None else 'changed',
                     'before_sha256':(old or {}).get('sha256'), 'after_sha256':(new or {}).get('sha256'),
                     'diff':diff[:12000], 'diff_truncated':len(diff)>12000})
    return rows

def fingerprint(snap):
    return hashlib.sha256(json.dumps({p:f['sha256'] for p,f in snap['files'].items()}, sort_keys=True).encode()).hexdigest()

def freeze_checks(cfg, run_dir):
    """Copy user-supplied independent checks outside the coding workspace before coding."""
    root = Path(cfg['_base']) / cfg.get('checks_dir','checks')
    destination = Path(run_dir)/'checks'
    destination.mkdir()
    hashes = {}
    if root.exists():
        for src in sorted(root.rglob('*')):
            if src.is_symlink() or not src.is_file() or any(p.startswith('.') or p == '__pycache__' for p in src.relative_to(root).parts):
                continue
            if not safe_file(src):
                continue
            target = destination/src.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            data = src.read_bytes()
            target.write_bytes(data)
            hashes[target.relative_to(destination).as_posix()] = hashlib.sha256(data).hexdigest()
    save_json(Path(run_dir)/'check-hashes.json', hashes)
    return hashes

def check_integrity(run_dir, hashes):
    for name, expected in hashes.items():
        path = Path(run_dir)/'checks'/name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('Independent check was modified: '+name)

def run_checks(cfg, workspace, run_dir, cycle_dir, should_stop):
    results = []
    for i, check in enumerate(cfg['checks']):
        if should_stop():
            break
        argv = [arg.replace('{workspace}',str(workspace)).replace('{python}',sys.executable)
                .replace('{checks}',str(Path(run_dir)/'checks')) for arg in check['argv']]
        result = run_process(argv, workspace, Path(cycle_dir)/f'check-{i+1}.log', check.get('timeout',60), should_stop)
        result.update(id=check['id'], requirements=check['requirements'], provenance='configured independent check')
        results.append(result)
    return results

def bundle(snap, delta, checks, coder, cfg, screen=None):
    screen = screen or []
    ids = ['file:'+p for p,f in snap['files'].items() if f['content']]
    ids += ['check:'+c['id'] for c in checks]
    ids += ['screen:'+s['case_id'] for s in screen]
    # Keep context growth bounded without silently dropping file coverage.
    delta_budget = 6000
    compact_delta = []
    for change in delta:
        excerpt = change['diff'][:delta_budget]
        delta_budget -= len(excerpt)
        compact_delta.append({**change,'diff':excerpt,'diff_truncated':len(excerpt)<len(change['diff']) or change['diff_truncated']})
    def compact_records(records, budget):
        text = json.dumps(records,ensure_ascii=False,default=str)
        return records if len(text) <= budget else {'excerpt':text[:budget], 'truncated':True}
    return {'files':snap['files'], 'omitted':snap['omitted'], 'incomplete_source':snap['incomplete'],
            'changes':compact_delta, 'checks':checks, 'screen':screen,
            'coding_phase':{'status':coder.get('status'), 'termination_reason':coder.get('termination_reason'),
                            'claims_are_not_verification':True,
                            'commands':compact_records(coder.get('commands',[])[-12:],3000),
                            'verification':compact_records(coder.get('verification',[]),3000)},
            'screen_cases':[{'id':c['id'],'requirements':c['requirements'],'goal':c['goal']} for c in cfg['screen']['cases']],
            'evidence_ids':ids}
