"""One-line status of the current CloudCode chess run (for 5-minute updates).

Usage: .venv/bin/python scratch/run_status.py <log file>
"""
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WS = ROOT / "Testing_Chess"
FILES = ["chess_logic.py", "chess_gui.py", "main.py", "test_chess.py", "RunningGUIDE.txt"]

log = Path(sys.argv[1]).read_text(errors="replace") if len(sys.argv) > 1 else ""
replies = log.count("CoderAgent (to UserProxyRunner)")
started = getattr(Path(sys.argv[1]).stat(), "st_birthtime", time.time()) if len(sys.argv) > 1 else time.time()
state = {}
try:
    state = json.loads((WS / ".cloudcode" / "state.json").read_text())
except (OSError, ValueError):
    pass
draft = state.get("draft") or {}
outline = re.findall(r"=== DRAFT OUTLINE (\S+) \((\d+) implemented, (\d+) remaining", log)
draft_info = f"{outline[-1][0]} {outline[-1][1]} done/{outline[-1][2]} left" if draft and outline else "-"
running = subprocess.run(["pgrep", "-f", "run_chess_test.py"], capture_output=True).returncode == 0
waiting = log.rfind("Model endpoint unavailable") > max(log.rfind("Endpoint recovered"), log.rfind("CoderAgent (to"))
final = re.findall(r"\[run\] status=(\S+).*", log)
score = re.findall(r"COMPLETION = (\S+ = \d+%)", log)
present = [f for f in FILES if (WS / f).is_file()]
print(json.dumps({
    "running": running, "minutes": round((time.time() - started) / 60), "replies": replies,
    "phase": state.get("phase"), "files": f"{len(present)}/5 {present}", "draft": draft_info,
    "endpoint_down_waiting": waiting, "final_status": final[-1] if final else None,
    "score": score[-1] if score else None}))
