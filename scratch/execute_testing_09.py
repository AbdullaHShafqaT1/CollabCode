"""
Execute one autonomous pass on Testing-0.9 using the Colab OSS model endpoint,
then evaluate with measure_chess.py.
"""
import json
import os
import sys
from pathlib import Path

# Paths
ROOT = Path(r"c:\Users\Acer\Desktop\projects\LEGACYStudios\cloud Bridge Projects\CloudCode")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "NodeCore"))

from launcher_gui import run_autonomous_orchestrator
from node_core.workflow import python_command

WORKSPACE = r"C:\Users\Acer\Desktop\projects\tests\Testing-0.9"
TUNNEL_URL = "https://cement-boutique-birth-provided.trycloudflare.com"
MODEL = "qwen2.5-coder:14b"
CHECKER = str(ROOT / "benchmark_outputs" / "Testing-0.8" / "chess_acceptance.py")
PROMPT_FILE = ROOT / "benchmark_outputs" / "Testing-0.8" / "chess-prompt.txt"

def main():
    os.makedirs(WORKSPACE, exist_ok=True)
    prompt = PROMPT_FILE.read_text(encoding="utf-8")
    
    acceptance = {
        "task": prompt,
        "required_files": [
            "chess_logic.py", "chess_gui.py", "main.py", "test_chess.py",
            "README.md", "RunningGUIDE.txt", "requirements.txt"
        ],
        "checks": [{
            "requirement": "Independent chess engine, launch and browser acceptance",
            "command": python_command(CHECKER, WORKSPACE)
        }]
    }

    print(">>> Starting 1 single-pass autonomous generation run on Testing-0.9...")
    result = run_autonomous_orchestrator(
        workspace_root=WORKSPACE,
        base_url=TUNNEL_URL,
        task_prompt=prompt,
        model=MODEL,
        max_rounds=8,
        acceptance=acceptance,
        compat=False
    )
    
    result_path = Path(WORKSPACE) / ".cloudcode" / "single_run_result.json"
    os.makedirs(result_path.parent, exist_ok=True)
    result_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print("\n>>> Autonomous pass finished with status:", result.get("status"))
    print(">>> Result saved to:", result_path)

if __name__ == "__main__":
    main()
