from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid
from .config import ROOT, resolve_path
from .coding import CloudCoder
from .evidence import snapshot, changes, fingerprint, freeze_checks, check_integrity, run_checks, bundle
from .models import LocalInspector, cloud_preflight, require_local
from .screen import ScreenTester, select_cases
from .runtime import save_json, workspace_lock

def verdict(requirements, review, evidence):
    reasons = []
    if evidence['incomplete_source']:
        reasons.append('Source evidence was truncated/omitted; increase evidence limits or narrow the target.')
    for item in review['assessments']:
        if item['status'] != 'supported':
            reasons.append(item['id']+': '+item['reason'])
    reasons.extend(i['problem'] for i in review.get('issues',[]))
    covered = set()
    for check in evidence['checks']:
        if check['status'] == 'passed':
            covered.update(check['requirements'])
        else:
            reasons.append('Independent check '+check['id']+' '+check['status'])
    for screen in evidence['screen']:
        if screen['status'] == 'passed':
            covered.update(screen['requirements'])
        else:
            reasons.append('Screen case '+screen['case_id']+' '+screen['status'])
    for request in review.get('screen_requests',[]):
        resolved = any(s['case_id'] == request['case_id'] and s['status'] == 'passed' and
                       request['requirement'] in s['requirements'] for s in evidence['screen'])
        if not resolved:
            reasons.append('Unresolved UI doubt: '+request['doubt'])
    uncovered = sorted({r['id'] for r in requirements}-covered)
    if uncovered:
        reasons.append('No passing independent acceptance evidence for '+', '.join(uncovered))
    if evidence['coding_phase']['status'] in ('CANCELLED','TIMEOUT','FAILED'):
        reasons.append('Coding phase ended '+evidence['coding_phase']['status'])
    return {'verified':not reasons, 'reasons':reasons, 'uncovered_requirements':uncovered}

def followup(original, requirements, review, decision, evidence):
    return ('ORIGINAL USER TASK (unchanged):\n'+original+'\n\nREQUIREMENTS:\n'+json.dumps(requirements,ensure_ascii=False)+
            '\n\nLOCAL INSPECTION FEEDBACK:\n'+json.dumps(review,ensure_ascii=False)+
            '\n\nVERIFICATION GAPS:\n'+json.dumps(decision['reasons'],ensure_ascii=False)+
            '\n\nCURRENT EXECUTION EVIDENCE:\n'+json.dumps({'checks':evidence['checks'],'screen':evidence['screen']},ensure_ascii=False)+
            '\n\nContinue working in the existing target project. Read affected files before editing. '
            'Fix the concrete issues, preserve working behavior, and run relevant tests. '
            'Do not rewrite the original task or modify independent acceptance checks. '
            'Do not claim completion based only on your report. If external input is missing, state exactly what is needed.')

class InspectionLoop:
    def __init__(self, cfg, coder=None, inspector=None, screen=None, logger=print):
        self.cfg = cfg
        self.coder = coder or CloudCoder(cfg)
        self.inspector = inspector or LocalInspector(cfg)
        self.screen = screen or ScreenTester(cfg)
        self.log = logger

    def run(self, original, workspace, inspect_only=False, preflight=True):
        workspace = Path(workspace).resolve()
        # Never let a task target this integration or either original implementation.
        sources = [ROOT/'vendor', ROOT.parent, ROOT.parent.parents[1]/'ScreenControlProjects/Test1']
        if workspace == ROOT or workspace in ROOT.parents or any(workspace == p for p in sources):
            raise ValueError('Choose a separate target project, not the integration/source root.')
        if workspace.is_relative_to(ROOT.parent) and not workspace.is_relative_to(ROOT/'targets'):
            raise ValueError('Inside CloudCode, target projects must be under CloudCodeInspector/targets.')
        if workspace.is_relative_to(ROOT/'vendor') or workspace.is_relative_to(sources[2]):
            raise ValueError('Source implementations are protected; choose another target.')
        if not original.strip():
            raise ValueError('The original task prompt cannot be empty')
        workspace.mkdir(parents=True,exist_ok=True)
        runs_root = ROOT/'runs'
        if runs_root.is_relative_to(workspace) or workspace.is_relative_to(runs_root):
            raise ValueError('Target project and evidence directory must be separate')
        with workspace_lock(workspace):
            return self._run(original,workspace,inspect_only,preflight)

    def _run(self,original,workspace,inspect_only,preflight):
        runs_root = ROOT/'runs'
        run_dir = runs_root/(datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8])
        run_dir.mkdir(parents=True)
        stop_file = run_dir/'STOP'
        should_stop = stop_file.exists
        self.log('Evidence folder: '+str(run_dir))
        (run_dir/'original-prompt.txt').write_text(original,encoding='utf-8')
        save_json(run_dir/'configuration.json',self.cfg)
        state = {'status':'STARTING','workspace':str(workspace),'run_dir':str(run_dir),'cycles':[],
                 'original_prompt_sha256':hashlib.sha256(original.encode()).hexdigest(),
                 'cloud_coding_attempted':False,'scope':'configured requirements; model interpretation is fallible'}
        save_json(run_dir/'summary.json',state)
        try:
            if preflight:
                self.log('Checking local models'+(' and Colab GPT OSS.' if not inspect_only else '.'))
                save_json(run_dir/'local-inventory.json',require_local(self.cfg))
                if not inspect_only:
                    cloud_preflight(self.cfg,run_dir/'cloud-preflight.json')
            requirements = self.cfg['requirements'] or self.inspector.requirements(original,run_dir/'requirements-response.json')
            save_json(run_dir/'requirements.json',{'source':'configured' if self.cfg['requirements'] else 'local model decomposition',
                                                  'requirements':requirements})
            check_hashes = freeze_checks(self.cfg,run_dir)
            current_prompt = original
            stagnant = 0
            previous_signature = None
            for cycle in range(1,self.cfg['limits']['max_cycles']+1):
                if should_stop():
                    state['status'] = 'CANCELLED'
                    break
                folder = run_dir/f'cycle-{cycle}'
                folder.mkdir()
                before = snapshot(workspace,self.cfg)
                save_json(folder/'before.json',before)
                (folder/'coding-prompt.txt').write_text(current_prompt,encoding='utf-8')
                self.log(f'Cycle {cycle}: '+('inspecting existing project' if inspect_only else 'CloudCode implementation'))
                state['cloud_coding_attempted'] = not inspect_only
                coder = {'status':'INSPECT_ONLY'} if inspect_only else self.coder.run(current_prompt,workspace,folder,stop_file,should_stop)
                save_json(folder/'coding-report.json',coder)
                if should_stop() or coder.get('status') == 'CANCELLED':
                    state['status'] = 'CANCELLED'
                    break
                check_integrity(run_dir,check_hashes)
                self.log(f'Cycle {cycle}: gathering files and running configured checks')
                checks = run_checks(self.cfg,workspace,run_dir,folder,should_stop)
                after = snapshot(workspace,self.cfg)
                save_json(folder/'after.json',after)
                delta = changes(before,after)
                evidence = bundle(after,delta,checks,coder,self.cfg)
                save_json(folder/'evidence.json',evidence)
                if should_stop():
                    state['status'] = 'CANCELLED'
                    break
                self.log(f'Cycle {cycle}: local inspection against original task')
                review = self.inspector.inspect(original,requirements,evidence,folder/'inspection-response.json')
                save_json(folder/'inspection-before-screen.json',review)
                requests = review.get('screen_requests',[])
                if requests:
                    select_cases(requests,self.cfg)
                    self.log('UI uncertainty: '+'; '.join(r['doubt'] for r in requests))
                    observations = self.screen.run(requests,workspace,folder,should_stop)
                    evidence = bundle(after,delta,checks,coder,self.cfg,observations)
                    save_json(folder/'evidence.json',evidence)
                    if any(s['status'] != 'not_run' for s in observations) and not should_stop():
                        review = self.inspector.inspect(original,requirements,evidence,folder/'reinspection-response.json')
                check_integrity(run_dir,check_hashes)
                # Tests/UI may modify app data. Never verify a different file state from the one inspected.
                if fingerprint(snapshot(workspace,self.cfg)) != fingerprint(after):
                    review.setdefault('issues',[]).append({'requirement':'all','problem':'Files changed during inspection/UI checks',
                                                           'action':'Inspect and re-run acceptance checks on the resulting file state.'})
                decision = verdict(requirements,review,evidence)
                save_json(folder/'inspection.json',review)
                save_json(folder/'decision.json',decision)
                next_prompt = followup(original,requirements,review,decision,evidence)
                (folder/'follow-up-prompt.txt').write_text(next_prompt,encoding='utf-8')
                state['cycles'].append({'cycle':cycle, 'verified':decision['verified'], 'reasons':decision['reasons'], 'folder':str(folder)})
                if should_stop():
                    state['status'] = 'CANCELLED'
                    break
                if decision['verified']:
                    state['status'] = 'VERIFIED_FOR_CONFIGURED_REQUIREMENTS'
                    break
                if coder.get('status') in ('TIMEOUT','FAILED','BLOCKED'):
                    state['status'] = 'CODING_FAILED'
                    break
                if inspect_only:
                    state['status'] = 'INSPECTION_COMPLETE_WITH_GAPS'
                    break
                if decision['uncovered_requirements'] and not review.get('issues') and all(a['status']=='supported' for a in review['assessments']):
                    state['status'] = 'NEEDS_ACCEPTANCE_CHECKS'
                    break
                if requests and not self.cfg['screen']['enabled']:
                    state['status'] = 'NEEDS_SCREEN_CONFIGURATION'
                    break
                signature = (fingerprint(after), json.dumps([(c['id'],c['status']) for c in checks]))
                stagnant = stagnant+1 if signature == previous_signature else 0
                previous_signature = signature
                if stagnant >= self.cfg['limits']['stagnant_cycles']:
                    state['status'] = 'STALLED'
                    break
                current_prompt = next_prompt
                state['status'] = 'IMPROVING'
                save_json(run_dir/'summary.json',state)
            else:
                state['status'] = 'CYCLE_LIMIT'
        except KeyboardInterrupt:
            state.update(status='CANCELLED',error='Interrupted by user')
        except Exception as exc:
            state.update(status='BLOCKED',error=f'{type(exc).__name__}: {exc}')
        save_json(run_dir/'summary.json',state)
        self.log('Finished: '+state['status'])
        if state.get('error'):
            self.log(state['error'])
        return state
