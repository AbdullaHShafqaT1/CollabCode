"""
NodeCore Autonomous Orchestrator Runner
=======================================
Bootstraps NodeCore with AutoGen multi-agent group chat,
binds the local toolchain (NodeForge, NodePulse, NodeInsight, NodeLog)
to the target workspace, and executes autonomous development tasks.

Supports:
- Interactive GUI Launcher (Tkinter / ttk) by default
- Graceful CLI interactive fallback (--cli or headless environments)
- Headless / non-interactive pipeline automation (--non-interactive)
"""
from __future__ import annotations

import os
import sys
import argparse
from pathlib import Path

# Prevent UnicodeEncodeError on Windows cmd/powershell for characters like ♔ ♕
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    os.environ["PYTHONIOENCODING"] = "utf-8"

# Ensure CloudCode root and NodeCore are on sys.path
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ROOT / "NodeCore") not in sys.path:
    sys.path.insert(0, str(_ROOT / "NodeCore"))

from launcher_gui import (
    launch_orchestrator,
    probe_endpoint_health,
    run_autonomous_orchestrator,
    load_config,
    save_config,
    DEFAULT_CONFIG,
    CONFIG_FILENAME
)


def main():
    parser = argparse.ArgumentParser(
        description="NodeCore Autonomous Multi-Agent Orchestrator",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "--cli",
        action="store_true",
        help="Launch in interactive CLI terminal mode instead of Tkinter GUI"
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Execute immediately using current config/flags without GUI or terminal prompts"
    )
    parser.add_argument(
        "--tunnel-url",
        type=str,
        default=None,
        help="Cloudflare Tunnel URL override (e.g. https://<subdomain>.trycloudflare.com)"
    )
    parser.add_argument(
        "--workspace",
        type=str,
        default=None,
        help="Target workspace directory override"
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="LLM model identifier override (e.g. qwen2.5-coder:32b)"
    )
    parser.add_argument(
        "--prompt",
        type=str,
        default=None,
        help="Custom task prompt override"
    )
    parser.add_argument(
        "--probe-only",
        action="store_true",
        help="Probe the endpoint health and print the status report without executing tasks"
    )

    parser.add_argument("--resume", action="store_true",
                        help="Continue the previous run in the workspace from its saved state")
    parser.add_argument("--compat", choices=["auto", "on", "off"], default="auto",
                        help="Short-reply compatibility mode: detect from the endpoint, or force on/off")
    parser.add_argument("--max-rounds", type=int, default=None,
                        help="Maximum model responses, including processing the last response")
    args = parser.parse_args()
    if args.max_rounds is not None and args.max_rounds < 1:
        parser.error("--max-rounds must be positive")

    # If --probe-only is requested
    if args.probe_only:
        cfg = load_config()
        url = args.tunnel_url or cfg["tunnel_url"]
        mdl = args.model or cfg["model"]
        print(f"[*] Probing endpoint: {url} (model: {mdl}) ...")
        ok, msg = probe_endpoint_health(url, model=mdl)
        print(f"[*] Probe Result: {'READY' if ok else 'FAILED'} - {msg}")
        sys.exit(0 if ok else 1)

    # Launch GUI or CLI fallback
    result = launch_orchestrator(
        force_cli=args.cli,
        non_interactive=args.non_interactive,
        tunnel_url=args.tunnel_url,
        workspace_root=args.workspace,
        model=args.model,
        task_prompt=args.prompt,
        max_rounds=args.max_rounds,
        compat={"auto": None, "on": True, "off": False}[args.compat],
        resume=args.resume,
    )
    if args.non_interactive:
        return 0 if isinstance(result, dict) and result.get("status") == "COMPLETED" else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
