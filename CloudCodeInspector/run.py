from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
from inspector.config import ROOT, load_config
from inspector.loop import InspectionLoop
from inspector.models import require_local, cloud_preflight

def main():
    parser = argparse.ArgumentParser(description='CloudCode implementation + local evidence inspection')
    parser.add_argument('--config', default=str(ROOT/'config.example.json'))
    parser.add_argument('--prompt-file', help='UTF-8 file containing the ORIGINAL task')
    parser.add_argument('--workspace', help='Separate target project path')
    parser.add_argument('--inspect-only', action='store_true', help='Inspect existing files without calling Colab')
    parser.add_argument('--preflight', action='store_true', help='Check configured local models and Colab contract')
    parser.add_argument('--models', action='store_true', help='List installed local models without downloading')
    parser.add_argument('--gui', action='store_true', help='Open the desktop launcher (default with no task)')
    parser.add_argument('--port', type=int, default=0, help='Local browser launcher port (0 chooses a free port)')
    parser.add_argument('--no-browser', action='store_true', help='Print launcher URL without opening a browser')
    args = parser.parse_args()
    if args.gui or not (args.prompt_file or args.preflight or args.models):
        from inspector.web_ui import launch
        launch(args.config,args.port,not args.no_browser)
        return 0
    try:
        cfg = load_config(args.config)
        if args.models:
            print(json.dumps(require_local(cfg,vision=cfg['screen']['enabled']),indent=2))
            return 0
        if args.preflight:
            print('Installed local models:', ', '.join(m['name'] for m in require_local(cfg)))
            cloud_preflight(cfg,ROOT/'runs/preflight.json')
            print('GPT OSS serving checks passed; identity remains server-reported.')
            return 0
        if not args.workspace:
            parser.error('--workspace is required with --prompt-file')
        result = InspectionLoop(cfg).run(Path(args.prompt_file).read_text(encoding='utf-8-sig'),args.workspace,args.inspect_only)
        return 0 if result['status'] == 'VERIFIED_FOR_CONFIGURED_REQUIREMENTS' else 2
    except Exception as exc:
        print(f'Cannot run: {exc}',file=sys.stderr)
        return 2

if __name__ == '__main__':
    raise SystemExit(main())
