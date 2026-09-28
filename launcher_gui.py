"""
NodeCore Orchestrator Interactive Launcher
==========================================
Provides a modern dual-pane CustomTkinter GUI (with CLI fallback):
- Left-Hand Side (LHS): Configuration & Connectivity, Task Directives with Presets,
  Action Control, and Workspace Info.
- Right-Hand Side (RHS): Comprehensive Runtime Workbench (Antigravity/Cursor/VS Code Copilot style)
  with tabs for Live Agent Stream, Workspace Files Explorer & Inspector, Structured Telemetry,
  and Raw Console Output.
"""
from __future__ import annotations

import os
import sys
import json
import queue
import threading
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, Callable, List

# Prevent UnicodeEncodeError on Windows cmd/powershell for characters like ♔ ♕
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    os.environ["PYTHONIOENCODING"] = "utf-8"
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hdesk = user32.OpenInputDesktop(0, False, 0x01FF)
        if hdesk:
            user32.SetThreadDesktop(hdesk)
    except Exception:
        pass

# Ensure CloudCode root and NodeCore are on sys.path
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ROOT / "NodeCore") not in sys.path:
    sys.path.insert(0, str(_ROOT / "NodeCore"))

from node_core.core import NodeCore, create_cloud_llm_config
from node_core.tools import configure_tools, NodeLog


# =====================================================================
# Configuration Manager & Prompt Presets
# =====================================================================

CONFIG_FILENAME = "orchestrator_config.json"

DEFAULT_TASK_PROMPT = """Task: Create and verify a sanity check file `hello.py`.
Requirements:
1. Output a Python code block with `# filename: hello.py` on line 1:
```python
# filename: hello.py
print("Hello from NodeCore autonomous runner!")
```

2. Output a bash block to run it:
```bash
python hello.py
```

3. Conclude with TERMINATE.
"""

LUDO_TASK_PROMPT = """TASK: Build a Complete GUI-Based 2-4 Player Ludo Game with Full Rules & Save System

Objective:
Create a complete, beautifully rendered, fully functional desktop Ludo game in Python.
The game must support 2 to 4 human players (hotseat/local multiplayer with Red, Green, Yellow, Blue). No CPU players needed.
The application entrypoint must be `main.py`.

Requirements & Architecture:
1. `ludo_logic.py`:
   - Full 15x15 Ludo board coordinate and track mapping.
   - Player management (2 to 4 players, active player rotation).
   - 4 tokens per player with states: HOME_BASE, ACTIVE_TRACK, HOME_STRETCH, FINISHED.
   - Dice rolling logic (1 to 6). Rolling a 6 allows bringing a token out of HOME_BASE and grants a bonus roll.
   - Capturing / Cutting: Landing on an opponent's token sends it back to home base and grants a bonus roll.
   - Safe squares (starting squares and star-marked squares) where tokens cannot be captured.
   - Exact roll required to reach FINISHED center.
   - Win condition: First player to move all 4 tokens to FINISHED wins, with 1st, 2nd, 3rd ranking.
   - State serialization: `save_game(filepath)` and `load_game(filepath)` using JSON format.

2. `ludo_gui.py`:
   - Modern Tkinter GUI with responsive Canvas board rendering the classic colorful Ludo layout:
     - 4 colored corner home bases (Red, Green, Yellow, Blue) with token slots.
     - 52-square outer track, 4 colored home columns leading to the center.
     - Star symbols on safe squares.
     - Smoothly drawn circular tokens with player colors, outlines, and token ID numbers.
     - Clear visual indicator of the active player's turn and roll results.
     - Interactive animated/visual dice button.
     - Highlight clickable tokens that have valid moves; clicking a token executes the move.
     - Top control bar: "New Game", "Save Game", "Load Game", "Rules / Help".
     - Player count selection dialog or buttons (2, 3, or 4 players).
     - Victory popup announcement when a player wins.

3. `main.py`:
   - Application launcher initializing the Ludo logic engine and launching the GUI.

4. `test_ludo.py`:
   - Comprehensive headless automated test suite verifying:
     - Board track initialization and token home-to-track transitions.
     - Dice roll constraints and bonus roll logic on 6.
     - Capture mechanics (sending tokens back to base).
     - Safe square immunity.
     - Win condition detection.
     - Save and load round-trip consistency.
     (Run with `python test_ludo.py`).

5. `RunningGUIDE.txt`:
   - Comprehensive user guide explaining:
     - How to launch the game (`python main.py`).
     - How to select 2, 3, or 4 players.
     - Rules of play, dice rolling, safe squares, capturing, and winning.
     - How to save and resume games.

Execution Directives:
- Output EACH file in its own Markdown code block with `# filename: <path>` on line 1.
- Provide 100% complete, fully implemented code without any placeholders or omissions.
- Verify your code headlessly using `python test_ludo.py` and `python -m py_compile main.py`.
- Do not run `python main.py` in bash blocks because GUI mainloops block execution.
- Conclude with TERMINATE on its own line after all files are created and tests pass.
"""

SUDOKU_TASK_PROMPT = """TASK: Build a Complete GUI-Based Sudoku Game with 3 Levels & Save System

Objective:
Create a complete, fully functional Sudoku desktop application in Python.
The application entrypoint must be `main.py`.

Requirements:
1. `sudoku_logic.py`:
   - Board generation for 3 difficulty levels: Easy, Medium, Hard.
   - Backtracking Sudoku solver and validation functions.
   - Save / Load game state serialization to JSON (`sudoku_save.json`).

2. `sudoku_gui.py`:
   - Clean graphical interface with 9x9 grid, 3x3 subgrid bold borders, number keypad, difficulty selector, timer, and save/load buttons.

3. `main.py`:
   - Application entry point launching the game.

4. `test_sudoku.py`:
   - Headless unit tests verifying board generation, validity checks, and save/load.

5. `RunningGUIDE.txt`:
   - Instructions on running and playing the game.

Directives:
- Output each file with `# filename: <path>` on line 1.
- Conclude with TERMINATE after all files are created and verified.
"""

CHESS_TASK_PROMPT = """TASK: Build a Complete GUI-Based 2-Player Chess Game with Full Rules & Save System

Objective:
Create a complete, fully functional, beautiful desktop Chess game in Python using standard `tkinter`.
The application entrypoint must be `main.py`.

Requirements & Architecture:
1. `chess_logic.py`:
   - Represent the 8x8 board as an 8x8 grid: None for empty, strings like 'wP','wR','wN','wB','wQ','wK' / 'bP','bR','bN','bB','bQ','bK'.
   - Class `ChessGame`:
     * `self.board`: 8x8 matrix with standard initial 32 pieces setup (ranks 0-1 for Black, 6-7 for White).
     * `self.current_turn`: 'W' (White moves first).
     * `get_piece(row, col)` -> piece string or None.
     * `get_legal_moves(row, col)` -> list of valid (target_r, target_c) coordinates for the piece at (row, col).
       - Pawns: forward 1 (or 2 from home row), diagonal captures.
       - Knights: 8 possible (dr, dc) L-shaped leaps.
       - Bishops: diagonals ray-cast until blocked.
       - Rooks: orthogonal ray-cast until blocked.
       - Queens: combination of Rook and Bishop ray-casts.
       - Kings: 1 step in all 8 directions.
     * `make_move(from_pos, to_pos)` -> bool: executes move if legal, switches turn ('W' <-> 'B').
     * `is_in_check(color)` -> bool: checks if color's King is under attack.
     * `save_game(filepath)` and `load_game(filepath)`: JSON serialization of board and current_turn.

2. `chess_gui.py`:
   - Modern Tkinter GUI with responsive 8x8 Canvas board:
     * Alternating squares (#EEEED2 light, #769656 dark).
     * Render pieces directly with Unicode glyphs via canvas.create_text(x, y, text=glyph, font=('Arial', 32)):
       UNICODE_PIECES = {'wK':'♔', 'wQ':'♕', 'wR':'♖', 'wB':'♗', 'wN':'♘', 'wP':'♙', 'bK':'♚', 'bQ':'♛', 'bR':'♜', 'bB':'♝', 'bN':'♞', 'bP':'♟'}
       (DO NOT use external image files or PhotoImage).
     * Click a piece to select: highlight selected square and legal destinations.
     * Click a legal destination square to execute move.
     * Header banner with current turn ('White to move' / 'Black to move') and Check warning.
     * Control bar with buttons: [New Game], [Save Game], [Load Game].
     * Winner / Checkmate dialog popup.

3. `main.py`:
   - Application launcher initializing `ChessGame` and starting `ChessGUI`.

4. `test_chess.py`:
   - Headless unit test suite (using standard `unittest`) verifying:
     * Initial board layout (32 pieces correctly placed).
     * Pawn opening move (e.g. from row 6 to 4).
     * Knight leap over pawn.
     * Turn alternating (W -> B -> W).
     * Simple check detection.
     * JSON save and load consistency.

5. `RunningGUIDE.txt`:
   - User guide detailing launch instructions (`python main.py`), controls (click to select and move), and rules.

Execution Directives:
- Generate ONE complete file per turn in order:
  Turn 1: `chess_logic.py`
  Turn 2: `chess_gui.py`
  Turn 3: `main.py`
  Turn 4: `test_chess.py`
  Turn 5: `RunningGUIDE.txt`
- Output EACH file in its own Markdown code block with `# filename: <filename>` on line 1.
- Provide 100% complete source code (no placeholders, no omissions).
- Conclude with TERMINATE on its own line ONLY after all 5 files are created and unit tests pass.
"""

PROMPT_PRESETS: Dict[str, str] = {
    "Chess GUI Game (2-Player)": CHESS_TASK_PROMPT,
    "Ludo GUI Game (2-4 Player)": LUDO_TASK_PROMPT,
    "Sudoku GUI Game (3 Levels)": SUDOKU_TASK_PROMPT,
    "Sanity Check (hello.py)": DEFAULT_TASK_PROMPT,
    "Custom Directives": ""
}

DEFAULT_CONFIG: Dict[str, Any] = {
    "tunnel_url": "https://values-hills-ware-unified.trycloudflare.com",
    "workspace_root": r"C:\Users\Acer\Desktop\projects\TESTING-0.5",
    "model": "qwen2.5-coder:32b",
    "max_rounds": 50,
    "task_prompt": CHESS_TASK_PROMPT,
}


def open_in_file_manager(path: str) -> None:
    """Open a folder in the platform's file manager (os.startfile is Windows-only)."""
    if sys.platform == "win32":
        os.startfile(path)
    else:
        import subprocess
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])


def get_config_path() -> Path:
    return _ROOT / CONFIG_FILENAME


def load_config() -> Dict[str, Any]:
    """Loads configuration from JSON file or falls back to defaults."""
    cfg = dict(DEFAULT_CONFIG)
    p = get_config_path()
    if p.exists():
        try:
            with open(p, "r", encoding="utf-8") as f:
                saved = json.load(f)
                if isinstance(saved, dict):
                    cfg.update(saved)
        except Exception as e:
            print(f"[!] Warning reading {CONFIG_FILENAME}: {e}")

    # Environment variable overrides
    if "TUNNEL_URL" in os.environ and os.environ["TUNNEL_URL"]:
        cfg["tunnel_url"] = os.environ["TUNNEL_URL"]

    return cfg


def save_config(config_data: Dict[str, Any]) -> bool:
    """Persists configuration to JSON file."""
    p = get_config_path()
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=2)
        return True
    except Exception as e:
        print(f"[!] Error writing {CONFIG_FILENAME}: {e}")
        return False


# =====================================================================
# Endpoint Health Prober
# =====================================================================

def probe_endpoint_health(
    base_url: str,
    model: str = "qwen2.5-coder:32b",
    timeout: int = 15,
    logger: Callable[[str], None] = print,
) -> Tuple[bool, str]:
    """
    Probes remote Cloudflare endpoint for health (/v1/models and /v1/chat/completions).
    Returns (success: bool, report_message: str).
    """
    cleaned_url = base_url.strip().rstrip("/")
    if not cleaned_url.startswith("http://") and not cleaned_url.startswith("https://"):
        msg = f"Invalid URL format: '{base_url}'. Must begin with http:// or https://"
        logger(f"[!] {msg}")
        return False, msg

    api_base = cleaned_url if cleaned_url.endswith("/v1") else f"{cleaned_url}/v1"
    models_url = f"{api_base}/models"
    chat_url = f"{api_base}/chat/completions"

    logger(f"[*] Probing LLM endpoint: {cleaned_url} ...")
    models_ok = False

    # 1. Probe /v1/models
    try:
        req = urllib.request.Request(models_url, headers={"User-Agent": "NodeCore/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as res:
            if res.status == 200:
                logger(f"[+] Models endpoint reachable: HTTP {res.status}")
                models_ok = True
    except urllib.error.HTTPError as e:
        logger(f"[!] Models endpoint returned HTTP {e.code}")
    except Exception as e:
        logger(f"[!] Unable to reach {models_url}: {e}")

    # 2. Probe /v1/chat/completions
    try:
        test_payload = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 5
        }).encode("utf-8")
        req = urllib.request.Request(
            chat_url,
            data=test_payload,
            headers={"Content-Type": "application/json", "User-Agent": "NodeCore/1.0"}
        )
        with urllib.request.urlopen(req, timeout=max(timeout * 2, 35)) as res:
            if res.status == 200:
                logger(f"[+] Chat completions endpoint ready: HTTP {res.status}")
                return True, f"Online & Ready (HTTP {res.status})"
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        diag = ""
        if "tokenizer" in err_body:
            diag = " [Diagnosis: remote handler needs 'tokenizer' defined]"
        msg = f"HTTP {e.code}: {err_body[:100]}{diag}"
        logger(f"[!] Warning: Chat completions returned {msg}")
        return False, msg
    except Exception as e:
        err_msg = str(e)
        logger(f"[!] Warning: Chat completions error: {err_msg}")
        status_msg = "Models reachable, completions timed out (busy GPU)" if models_ok else f"Unreachable: {err_msg[:60]}"
        return False, status_msg

    return models_ok, "Models online, completions untested"


# =====================================================================
# Autonomous Orchestrator Task Runner
# =====================================================================

def run_autonomous_orchestrator(
    workspace_root: str,
    base_url: str,
    task_prompt: str,
    model: str = "qwen2.5-coder:32b",
    max_rounds: int = 50,
    logger: Callable[[str], None] = print,
    should_stop: Optional[Callable[[], bool]] = None,
    acceptance: Optional[Dict[str, Any]] = None,
    compat: Optional[bool] = None,
    resume: bool = False,
) -> Dict[str, Any]:
    """
    Executes the autonomous 2-agent AutoGen execution loop (CoderAgent <-> UserProxyRunner)
    with the 7 NodeCore tools dynamically bound to workspace_root.

    compat: None probes the endpoint and enables compatibility mode (rules folded
    into each prompt, method-at-a-time files) when it drops system messages,
    history or max_tokens. resume: continue from the workspace's saved state.
    Endpoint outages pause the run; saving a new tunnel_url to the config while
    it waits switches endpoints.
    """
    abs_ws = os.path.abspath(workspace_root)
    os.makedirs(abs_ws, exist_ok=True)

    logger("=" * 70)
    logger(" NodeCore Autonomous Multi-Agent Orchestrator ")
    logger("=" * 70)
    logger(f"[*] Workspace Root : {abs_ws}")
    logger(f"[*] Remote Endpoint: {base_url}")
    logger(f"[*] Target Model   : {model}")
    logger(f"[*] Max Rounds     : {max_rounds}")

    # 1. Bind local toolchain to target workspace
    logger(f"\n[*] Binding toolchain to workspace: {abs_ws}")
    tools = configure_tools(workspace_root=abs_ws)
    logger(f"[+] Registered {len(tools)} tools: {', '.join(tools.keys())}")

    # 2. Build LLM configuration
    logger(f"\n[*] Initializing NodeCore with LLM config ({base_url})...")
    llm_config = create_cloud_llm_config(
        base_url=base_url,
        model=model,
        api_key="not-needed"
    )

    # 3. Instantiate NodeCore orchestrator
    orchestrator = NodeCore(
        llm_config=llm_config,
        tools=tools,
        workspace_root=abs_ws
    )

    # 4. Detect endpoint limitations and prepare outage recovery
    if compat is None:
        from node_core.endpoint import probe_capabilities
        compat = bool(probe_capabilities(base_url, model, logger=logger).get("compat"))
    # Fit every request inside the model's context: the reply budget plus the prompt.
    from node_core.endpoint import model_limits, prompt_budget_chars
    limits = model_limits(base_url, model)
    if limits.get("max_output_tokens"):
        for entry in llm_config.get("config_list", []):
            entry["max_tokens"] = min(entry.get("max_tokens") or limits["max_output_tokens"], limits["max_output_tokens"])
        llm_config["max_tokens"] = min(llm_config.get("max_tokens") or limits["max_output_tokens"], limits["max_output_tokens"])
    budget = prompt_budget_chars(limits.get("context_window"), llm_config.get("max_tokens"))
    if budget:
        logger(f"[*] Model context {limits['context_window']} tokens, reply budget {llm_config.get('max_tokens')}: "
               f"prompts kept under ~{budget} characters")
    config_url_at_start = load_config().get("tunnel_url")

    def resolve_endpoint() -> str:
        current = load_config().get("tunnel_url")
        return current if current and current != config_url_at_start else base_url

    # 5. Start the autonomous AutoGen task
    logger("\n[*] Starting NodeCore autonomous loop..." + (" (resuming saved state)" if resume else ""))
    # This built-in preset has an exact, independently checkable output contract.
    if acceptance is None and task_prompt.strip() == DEFAULT_TASK_PROMPT.strip():
        from node_core.workflow import python_command
        acceptance = {"required_files": ["hello.py"], "checks": [{
            "requirement": "hello.py prints the requested greeting and exits successfully",
            "command": python_command("-c", "import contextlib,io,runpy; "
                                       "out=io.StringIO(); "
                                       "exec('with contextlib.redirect_stdout(out):\\n runpy.run_path(\"hello.py\", run_name=\"__main__\")'); "
                                       "assert out.getvalue().strip() == 'Hello from NodeCore autonomous runner!'"),
        }]}
    result = orchestrator.start_task(task_prompt, max_rounds=max_rounds,
                                     should_stop=should_stop, acceptance=acceptance,
                                     compat=compat, endpoint_resolver=resolve_endpoint, resume=resume,
                                     prompt_budget_chars=budget)
    logger(f"\n[*] Execution Finished. Result: {result.get('status')}")
    return result


# =====================================================================
# Thread-Safe Stdout / Stderr Streamer
# =====================================================================

class QueueStream:
    """Redirects writes to a thread-safe queue while also echoing to original stream."""
    def __init__(self, target_queue: queue.Queue, original_stream: Any, stream_name: str = "stdout"):
        self.target_queue = target_queue
        self.original_stream = original_stream
        self.stream_name = stream_name

    def write(self, text: str):
        if self.original_stream:
            try:
                from node_core.tools import sanitize_for_console
                safe_text = sanitize_for_console(text)
                self.original_stream.write(safe_text)
                self.original_stream.flush()
            except Exception:
                try:
                    self.original_stream.write(text.encode("ascii", errors="replace").decode("ascii", errors="replace"))
                    self.original_stream.flush()
                except Exception:
                    pass
        if text:
            self.target_queue.put(("stream", self.stream_name, text))

    def flush(self):
        if self.original_stream:
            try:
                self.original_stream.flush()
            except Exception:
                pass


# =====================================================================
# CustomTkinter Modern Dual-Pane Launcher Application
# =====================================================================

class NodeCoreLauncherApp:
    """
    Modern Dual-Pane Interactive Launcher for NodeCore Autonomous Orchestrator.
    - LHS: Configuration, Connectivity, Task Directives, Control Actions, Workspace Stats.
    - RHS: Runtime Workbench (Agent Stream, Workspace File Explorer & Inspector, Live Telemetry, Raw Terminal).
    """

    def __init__(self, root: Any, initial_config: Optional[Dict[str, Any]] = None):
        import tkinter as tk
        from tkinter import filedialog, messagebox
        import customtkinter as ctk

        self.tk = tk
        self.filedialog = filedialog
        self.messagebox = messagebox
        self.ctk = ctk

        self.root = root
        self.config = initial_config or load_config()

        self.root.title("NodeCore Autonomous Orchestrator — CloudCode")
        try:
            self.root.configure(fg_color="#181818")
        except Exception:
            try:
                self.root.configure(bg="#181818")
            except Exception:
                pass

        # Responsive high-productivity window size
        window_width = 1560
        window_height = 920
        self.root.minsize(1240, 750)

        # Center on screen
        self.root.update_idletasks()
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x = max(0, int((screen_width - window_width) / 2))
        y = max(0, int((screen_height - window_height) / 2))
        self.root.geometry(f"{window_width}x{window_height}+{x}+{y}")
        self.root.update_idletasks()

        self.log_queue: queue.Queue[Tuple[str, Any, Any]] = queue.Queue()
        self.is_running = False
        self.stop_requested = False
        self.worker_thread: Optional[threading.Thread] = None
        self.active_round = 0

        self._build_ui()
        self._subscribe_nodelog()
        self._refresh_workspace_files()

        # Start processing message queue
        self.root.after(50, self._process_log_queue)

    def _build_ui(self):
        """Constructs the modern 2-column split workbench layout."""
        # Column 0: LHS Control Center (fixed ~570px)
        # Column 1: RHS Runtime Workbench (expanding weight=1)
        self.root.grid_columnconfigure(0, weight=0, minsize=560)
        self.root.grid_columnconfigure(1, weight=1)
        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_rowconfigure(1, weight=0)

        # =============================================================
        # LHS: Control Center & Directives Frame
        # =============================================================
        self.lhs_frame = self.ctk.CTkFrame(self.root, fg_color="#1E1E1E", corner_radius=0)
        self.lhs_frame.grid(row=0, column=0, sticky="nsew", padx=(12, 6), pady=(12, 6))

        # Top Header Strip
        lhs_header = self.ctk.CTkFrame(self.lhs_frame, fg_color="transparent")
        lhs_header.pack(fill="x", padx=14, pady=(12, 8))

        lhs_hdr_text = self.ctk.CTkFrame(lhs_header, fg_color="transparent")
        lhs_hdr_text.pack(side="left", fill="y")

        title_lbl = self.ctk.CTkLabel(
            lhs_hdr_text,
            text="NodeCore Orchestrator",
            font=self.ctk.CTkFont(family="Segoe UI", size=18, weight="bold"),
            text_color="#ECECEC"
        )
        title_lbl.pack(anchor="w")

        sub_lbl = self.ctk.CTkLabel(
            lhs_hdr_text,
            text="CloudCode Multi-Agent Architecture • Qwen-Coder Dual Loop",
            font=self.ctk.CTkFont(family="Segoe UI", size=11),
            text_color="#9E9E9E"
        )
        sub_lbl.pack(anchor="w", pady=(1, 0))

        badge_frame = self.ctk.CTkFrame(
            lhs_header,
            fg_color="#2A2A2A",
            border_color="#424242",
            border_width=1,
            corner_radius=12
        )
        badge_frame.pack(side="right", anchor="center", padx=2, pady=2)

        self.toolchain_badge = self.ctk.CTkLabel(
            badge_frame,
            text="● Toolchain Ready",
            text_color="#7BC28C",
            font=self.ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            padx=10,
            pady=3
        )
        self.toolchain_badge.pack()

        # -------------------------------------------------------------
        # Card 1: Configuration & Connectivity
        # -------------------------------------------------------------
        cfg_card = self.ctk.CTkFrame(
            self.lhs_frame,
            fg_color="#262626",
            border_color="#3C3C3C",
            border_width=1,
            corner_radius=10
        )
        cfg_card.pack(fill="x", padx=14, pady=(0, 10))

        cfg_inner = self.ctk.CTkFrame(cfg_card, fg_color="transparent")
        cfg_inner.pack(fill="x", padx=14, pady=12)

        cfg_hdr = self.ctk.CTkFrame(cfg_inner, fg_color="transparent")
        cfg_hdr.pack(fill="x", pady=(0, 8))

        self.ctk.CTkLabel(
            cfg_hdr,
            text="CONFIG & ENDPOINT",
            font=self.ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color="#D97757"
        ).pack(side="left")

        # Row 0: Tunnel URL + Probe
        url_lbl = self.ctk.CTkLabel(
            cfg_inner,
            text="Cloudflare Tunnel URL:",
            font=self.ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color="#CCCCCC"
        )
        url_lbl.pack(anchor="w", pady=(2, 2))

        url_row = self.ctk.CTkFrame(cfg_inner, fg_color="transparent")
        url_row.pack(fill="x", pady=(0, 6))

        self.url_var = self.tk.StringVar(value=self.config.get("tunnel_url", ""))
        self.url_entry = self.ctk.CTkEntry(
            url_row,
            textvariable=self.url_var,
            height=34,
            corner_radius=6,
            fg_color="#1E1E1E",
            border_color="#3C3C3C",
            text_color="#ECECEC",
            font=self.ctk.CTkFont(family="Segoe UI", size=11),
            placeholder_text="https://<subdomain>.trycloudflare.com"
        )
        self.url_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))

        self.probe_btn = self.ctk.CTkButton(
            url_row,
            text="Probe",
            command=self._on_probe_click,
            width=70,
            height=34,
            corner_radius=6,
            fg_color="#383838",
            hover_color="#484848",
            text_color="#ECECEC",
            font=self.ctk.CTkFont(family="Segoe UI", size=11, weight="bold")
        )
        self.probe_btn.pack(side="left", padx=(0, 6))

        self.health_badge = self.ctk.CTkLabel(
            url_row,
            text="● Untested",
            text_color="#A0A0A0",
            font=self.ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            width=80,
            anchor="w"
        )
        self.health_badge.pack(side="left")

        # Row 1: Workspace Directory + Browse
        ws_lbl = self.ctk.CTkLabel(
            cfg_inner,
            text="Target Workspace Directory:",
            font=self.ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color="#CCCCCC"
        )
        ws_lbl.pack(anchor="w", pady=(4, 2))

        ws_row = self.ctk.CTkFrame(cfg_inner, fg_color="transparent")
        ws_row.pack(fill="x", pady=(0, 6))

        self.ws_var = self.tk.StringVar(value=self.config.get("workspace_root", ""))
        self.ws_entry = self.ctk.CTkEntry(
            ws_row,
            textvariable=self.ws_var,
            height=34,
            corner_radius=6,
            fg_color="#1E1E1E",
            border_color="#3C3C3C",
            text_color="#ECECEC",
            font=self.ctk.CTkFont(family="Segoe UI", size=11),
            placeholder_text="Target project workspace directory"
        )
        self.ws_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))

        browse_btn = self.ctk.CTkButton(
            ws_row,
            text="Browse...",
            command=self._on_browse_workspace,
            width=80,
            height=34,
            corner_radius=6,
            fg_color="#383838",
            hover_color="#484848",
            text_color="#ECECEC",
            font=self.ctk.CTkFont(family="Segoe UI", size=11)
        )
        browse_btn.pack(side="left")

        # Row 2: Model & Rounds
        meta_row = self.ctk.CTkFrame(cfg_inner, fg_color="transparent")
        meta_row.pack(fill="x", pady=(4, 0))

        self.ctk.CTkLabel(
            meta_row,
            text="Model:",
            font=self.ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color="#CCCCCC"
        ).pack(side="left", padx=(0, 4))

        self.model_var = self.tk.StringVar(value=self.config.get("model", "qwen2.5-coder:32b"))
        self.model_entry = self.ctk.CTkEntry(
            meta_row,
            textvariable=self.model_var,
            width=200,
            height=32,
            corner_radius=6,
            fg_color="#1E1E1E",
            border_color="#3C3C3C",
            text_color="#ECECEC",
            font=self.ctk.CTkFont(family="Segoe UI", size=11)
        )
        self.model_entry.pack(side="left", padx=(0, 14))

        self.ctk.CTkLabel(
            meta_row,
            text="Max Rounds:",
            font=self.ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color="#CCCCCC"
        ).pack(side="left", padx=(0, 4))

        self.rounds_var = self.tk.IntVar(value=int(self.config.get("max_rounds", 50)))
        self.rounds_entry = self.ctk.CTkEntry(
            meta_row,
            textvariable=self.rounds_var,
            width=65,
            height=32,
            corner_radius=6,
            fg_color="#1E1E1E",
            border_color="#3C3C3C",
            text_color="#ECECEC",
            font=self.ctk.CTkFont(family="Segoe UI", size=11)
        )
        self.rounds_entry.pack(side="left")

        # -------------------------------------------------------------
        # Card 2: Autonomous Task Directives (Prompt & Presets)
        # -------------------------------------------------------------
        prompt_card = self.ctk.CTkFrame(
            self.lhs_frame,
            fg_color="#262626",
            border_color="#3C3C3C",
            border_width=1,
            corner_radius=10
        )
        prompt_card.pack(fill="both", expand=True, padx=14, pady=(0, 10))

        prompt_inner = self.ctk.CTkFrame(prompt_card, fg_color="transparent")
        prompt_inner.pack(fill="both", expand=True, padx=14, pady=12)

        prompt_topbar = self.ctk.CTkFrame(prompt_inner, fg_color="transparent")
        prompt_topbar.pack(fill="x", pady=(0, 6))

        self.ctk.CTkLabel(
            prompt_topbar,
            text="AUTONOMOUS TASK DIRECTIVES",
            font=self.ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color="#D97757"
        ).pack(side="left")

        # Presets selector
        self.preset_var = self.tk.StringVar(value="Ludo GUI Game (2-4 Player)")
        preset_menu = self.ctk.CTkOptionMenu(
            prompt_topbar,
            values=list(PROMPT_PRESETS.keys()),
            variable=self.preset_var,
            command=self._on_select_preset,
            width=190,
            height=28,
            corner_radius=6,
            fg_color="#383838",
            button_color="#464646",
            button_hover_color="#555555",
            font=self.ctk.CTkFont(family="Segoe UI", size=11)
        )
        preset_menu.pack(side="right", padx=(6, 0))

        reset_prompt_btn = self.ctk.CTkButton(
            prompt_topbar,
            text="↺ Reset",
            command=self._on_reset_prompt,
            width=60,
            height=28,
            corner_radius=6,
            fg_color="#383838",
            hover_color="#484848",
            text_color="#ECECEC",
            font=self.ctk.CTkFont(family="Segoe UI", size=11)
        )
        reset_prompt_btn.pack(side="right")

        self.prompt_text = self.ctk.CTkTextbox(
            prompt_inner,
            corner_radius=6,
            fg_color="#1A1A1A",
            border_color="#3C3C3C",
            border_width=1,
            text_color="#ECECEC",
            font=self.ctk.CTkFont(family="Consolas", size=11),
            wrap="word"
        )
        self.prompt_text.pack(fill="both", expand=True)
        self.prompt_text.insert("end", self.config.get("task_prompt", LUDO_TASK_PROMPT))

        # -------------------------------------------------------------
        # Card 3: Action Control Bar
        # -------------------------------------------------------------
        action_card = self.ctk.CTkFrame(
            self.lhs_frame,
            fg_color="#262626",
            border_color="#3C3C3C",
            border_width=1,
            corner_radius=10
        )
        action_card.pack(fill="x", padx=14, pady=(0, 10))

        action_inner = self.ctk.CTkFrame(action_card, fg_color="transparent")
        action_inner.pack(fill="x", padx=12, pady=10)

        self.start_btn = self.ctk.CTkButton(
            action_inner,
            text="▶  Start Autonomous Task",
            command=self._on_start_task,
            width=190,
            height=38,
            corner_radius=6,
            fg_color="#D97757",
            hover_color="#B85D3F",
            text_color="#FFFFFF",
            font=self.ctk.CTkFont(family="Segoe UI", size=12, weight="bold")
        )
        self.start_btn.pack(side="left", padx=(0, 8))

        save_btn = self.ctk.CTkButton(
            action_inner,
            text="💾 Save",
            command=self._on_save_config_click,
            width=80,
            height=38,
            corner_radius=6,
            fg_color="#383838",
            hover_color="#484848",
            border_color="#484848",
            border_width=1,
            text_color="#ECECEC",
            font=self.ctk.CTkFont(family="Segoe UI", size=11)
        )
        save_btn.pack(side="left", padx=(0, 8))

        self.stop_btn = self.ctk.CTkButton(
            action_inner,
            text="⏹ Stop",
            command=self._on_stop_task,
            state="disabled",
            width=80,
            height=38,
            corner_radius=6,
            fg_color="#383838",
            hover_color="#A83232",
            border_color="#484848",
            border_width=1,
            text_color="#A0A0A0",
            font=self.ctk.CTkFont(family="Segoe UI", size=11)
        )
        self.stop_btn.pack(side="left", padx=(0, 8))

        clear_btn = self.ctk.CTkButton(
            action_inner,
            text="🧹 Clear",
            command=self._on_clear_streams,
            width=75,
            height=38,
            corner_radius=6,
            fg_color="#383838",
            hover_color="#484848",
            border_color="#484848",
            border_width=1,
            text_color="#ECECEC",
            font=self.ctk.CTkFont(family="Segoe UI", size=11)
        )
        clear_btn.pack(side="left")

        # -------------------------------------------------------------
        # Card 4: Target Workspace Quick Stats
        # -------------------------------------------------------------
        stat_card = self.ctk.CTkFrame(
            self.lhs_frame,
            fg_color="#222222",
            border_color="#363636",
            border_width=1,
            corner_radius=8
        )
        stat_card.pack(fill="x", padx=14, pady=(0, 10))

        stat_inner = self.ctk.CTkFrame(stat_card, fg_color="transparent")
        stat_inner.pack(fill="x", padx=12, pady=8)

        self.stat_ws_lbl = self.ctk.CTkLabel(
            stat_inner,
            text=f"Target: {os.path.basename(self.ws_var.get()) or 'Workspace'}",
            font=self.ctk.CTkFont(family="Segoe UI", size=11),
            text_color="#B0B0B0"
        )
        self.stat_ws_lbl.pack(side="left")

        self.stat_files_lbl = self.ctk.CTkLabel(
            stat_inner,
            text="Files: 0",
            font=self.ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color="#7BC28C"
        )
        self.stat_files_lbl.pack(side="right", padx=(0, 6))

        self.stat_round_lbl = self.ctk.CTkLabel(
            stat_inner,
            text="Round: 0",
            font=self.ctk.CTkFont(family="Segoe UI", size=11),
            text_color="#A0A0A0"
        )
        self.stat_round_lbl.pack(side="right", padx=(0, 14))


        # =============================================================
        # RHS: Runtime Workbench & Telemetry Panel (Antigravity Style)
        # =============================================================
        self.rhs_frame = self.ctk.CTkFrame(self.root, fg_color="#1E1E1E", corner_radius=0)
        self.rhs_frame.grid(row=0, column=1, sticky="nsew", padx=(6, 12), pady=(12, 6))

        # RHS Header Strip
        rhs_header = self.ctk.CTkFrame(self.rhs_frame, fg_color="transparent")
        rhs_header.pack(fill="x", padx=14, pady=(12, 6))

        rhs_title_box = self.ctk.CTkFrame(rhs_header, fg_color="transparent")
        rhs_title_box.pack(side="left", fill="y")

        self.ctk.CTkLabel(
            rhs_title_box,
            text="RUNTIME WORKBENCH & MODEL STREAM",
            font=self.ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color="#D97757"
        ).pack(anchor="w")

        self.ctk.CTkLabel(
            rhs_title_box,
            text="Real-time multi-agent dialogue, active file generation, and execution telemetry",
            font=self.ctk.CTkFont(family="Segoe UI", size=11),
            text_color="#9E9E9E"
        ).pack(anchor="w")

        rhs_ctrls = self.ctk.CTkFrame(rhs_header, fg_color="transparent")
        rhs_ctrls.pack(side="right", anchor="center")

        self.autoscroll_var = self.tk.BooleanVar(value=True)
        self.autoscroll_chk = self.ctk.CTkCheckBox(
            rhs_ctrls,
            text="Auto-scroll",
            variable=self.autoscroll_var,
            text_color="#A0A0A0",
            fg_color="#D97757",
            hover_color="#B85D3F",
            font=self.ctk.CTkFont(family="Segoe UI", size=11),
            corner_radius=4,
            width=90
        )
        self.autoscroll_chk.pack(side="left", padx=(0, 12))

        self.runtime_status_badge = self.ctk.CTkLabel(
            rhs_ctrls,
            text="● IDLE",
            text_color="#A0A0A0",
            font=self.ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            padx=10,
            pady=4
        )
        self.runtime_status_badge.pack(side="left")

        # -------------------------------------------------------------
        # RHS Multi-Tabview (Antigravity / Cursor Workbench)
        # -------------------------------------------------------------
        self.tabview = self.ctk.CTkTabview(
            self.rhs_frame,
            fg_color="#242424",
            segmented_button_fg_color="#1E1E1E",
            segmented_button_selected_color="#D97757",
            segmented_button_selected_hover_color="#B85D3F",
            segmented_button_unselected_color="#2D2D2D",
            segmented_button_unselected_hover_color="#383838",
            text_color="#ECECEC",
            corner_radius=8
        )
        self.tabview.pack(fill="both", expand=True, padx=14, pady=(0, 12))

        # Add 4 Workbench Tabs
        self.tab_stream = self.tabview.add("⚡ Agent Stream")
        self.tab_files = self.tabview.add("📁 Workspace Files")
        self.tab_telemetry = self.tabview.add("📊 Live Telemetry")
        self.tab_console = self.tabview.add("🖥️ Raw Console")

        # -------------------------------------------------------------
        # Tab 1: Agent Stream (Real-Time Dialogue & Working)
        # -------------------------------------------------------------
        self.agent_stream_text = self.ctk.CTkTextbox(
            self.tab_stream,
            corner_radius=6,
            fg_color="#181818",
            border_color="#3C3C3C",
            border_width=1,
            text_color="#ECECEC",
            font=self.ctk.CTkFont(family="Consolas", size=11),
            wrap="word"
        )
        self.agent_stream_text.pack(fill="both", expand=True, padx=8, pady=8)

        # Color tagging for Agent Stream (CTkTextbox supports foreground only)
        self.agent_stream_text.tag_config("tag_agent_coder", foreground="#E8997A")
        self.agent_stream_text.tag_config("tag_agent_user", foreground="#8FA8CF")
        self.agent_stream_text.tag_config("tag_file_created", foreground="#7BC28C")
        self.agent_stream_text.tag_config("tag_command", foreground="#4DD0E1")
        self.agent_stream_text.tag_config("tag_error", foreground="#E06C75")
        self.agent_stream_text.tag_config("tag_success", foreground="#7BC28C")
        self.agent_stream_text.tag_config("tag_info", foreground="#A0A0A0")
        self.agent_stream_text.tag_config("tag_banner", foreground="#D97757")

        self._append_agent_stream("=== NodeCore Runtime Stream Ready ===\n", "tag_banner")
        self._append_agent_stream(f"Target Endpoint : {self.url_var.get()}\n", "tag_info")
        self._append_agent_stream(f"Target Workspace: {self.ws_var.get()}\n\n", "tag_info")

        # -------------------------------------------------------------
        # Tab 2: Workspace Files Explorer & Inspector
        # -------------------------------------------------------------
        files_toolbar = self.ctk.CTkFrame(self.tab_files, fg_color="transparent")
        files_toolbar.pack(fill="x", padx=8, pady=(6, 6))

        self.files_ws_label = self.ctk.CTkLabel(
            files_toolbar,
            text=f"Location: {self.ws_var.get()}",
            font=self.ctk.CTkFont(family="Segoe UI", size=11),
            text_color="#A0A0A0"
        )
        self.files_ws_label.pack(side="left", padx=4)

        refresh_files_btn = self.ctk.CTkButton(
            files_toolbar,
            text="↻ Refresh Files",
            command=self._refresh_workspace_files,
            width=100,
            height=28,
            corner_radius=6,
            fg_color="#383838",
            hover_color="#484848",
            text_color="#ECECEC",
            font=self.ctk.CTkFont(family="Segoe UI", size=11)
        )
        refresh_files_btn.pack(side="right", padx=(6, 0))

        open_folder_btn = self.ctk.CTkButton(
            files_toolbar,
            text="📂 Open Folder",
            command=self._on_open_workspace_folder,
            width=100,
            height=28,
            corner_radius=6,
            fg_color="#383838",
            hover_color="#484848",
            text_color="#ECECEC",
            font=self.ctk.CTkFont(family="Segoe UI", size=11)
        )
        open_folder_btn.pack(side="right")

        # Split pane for Files Tab: Top File List, Bottom File Preview
        files_split = self.ctk.CTkFrame(self.tab_files, fg_color="transparent")
        files_split.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        files_split.grid_rowconfigure(0, weight=1)
        files_split.grid_rowconfigure(1, weight=2)
        files_split.grid_columnconfigure(0, weight=1)

        # File List Box
        self.files_list_frame = self.ctk.CTkScrollableFrame(
            files_split,
            fg_color="#181818",
            border_color="#3C3C3C",
            border_width=1,
            corner_radius=6
        )
        self.files_list_frame.grid(row=0, column=0, sticky="nsew", pady=(0, 6))

        # File Inspector / Preview Box
        inspector_frame = self.ctk.CTkFrame(
            files_split,
            fg_color="#181818",
            border_color="#3C3C3C",
            border_width=1,
            corner_radius=6
        )
        inspector_frame.grid(row=1, column=0, sticky="nsew")

        inspector_hdr = self.ctk.CTkFrame(inspector_frame, fg_color="#222222", corner_radius=0)
        inspector_hdr.pack(fill="x")

        self.inspector_title = self.ctk.CTkLabel(
            inspector_hdr,
            text="File Preview: (Select a file above to inspect)",
            font=self.ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color="#CCCCCC"
        )
        self.inspector_title.pack(side="left", padx=10, pady=4)

        self.file_preview_text = self.ctk.CTkTextbox(
            inspector_frame,
            corner_radius=0,
            fg_color="#161616",
            border_width=0,
            text_color="#ECECEC",
            font=self.ctk.CTkFont(family="Consolas", size=11),
            wrap="none"
        )
        self.file_preview_text.pack(fill="both", expand=True, padx=4, pady=4)

        # -------------------------------------------------------------
        # Tab 3: Live Telemetry (Structured NodeLog Events)
        # -------------------------------------------------------------
        self.telemetry_text = self.ctk.CTkTextbox(
            self.tab_telemetry,
            corner_radius=6,
            fg_color="#181818",
            border_color="#3C3C3C",
            border_width=1,
            text_color="#ECECEC",
            font=self.ctk.CTkFont(family="Consolas", size=11),
            wrap="word"
        )
        self.telemetry_text.pack(fill="both", expand=True, padx=8, pady=8)

        self.telemetry_text.tag_config("tag_tel_success", foreground="#7BC28C")
        self.telemetry_text.tag_config("tag_tel_warning", foreground="#E5B567")
        self.telemetry_text.tag_config("tag_tel_error", foreground="#E06C75")
        self.telemetry_text.tag_config("tag_tel_info", foreground="#8FA8CF")
        self.telemetry_text.tag_config("tag_tel_tool", foreground="#4DD0E1")

        # -------------------------------------------------------------
        # Tab 4: Raw Console (Stdout / Stderr)
        # -------------------------------------------------------------
        self.console_text = self.ctk.CTkTextbox(
            self.tab_console,
            corner_radius=6,
            fg_color="#181818",
            border_color="#3C3C3C",
            border_width=1,
            text_color="#ECECEC",
            font=self.ctk.CTkFont(family="Consolas", size=11),
            wrap="word"
        )
        self.console_text.pack(fill="both", expand=True, padx=8, pady=8)

        self.console_text.tag_config("tag_err", foreground="#E06C75")
        self.console_text.tag_config("tag_norm", foreground="#CCCCCC")

        # =============================================================
        # Bottom Status Bar Strip
        # =============================================================
        self.status_bar_frame = self.ctk.CTkFrame(
            self.root,
            height=32,
            corner_radius=0,
            fg_color="#141414",
            border_color="#2E2E2E",
            border_width=1
        )
        self.status_bar_frame.grid(row=1, column=0, columnspan=2, sticky="ew")

        self.status_bar = self.ctk.CTkLabel(
            self.status_bar_frame,
            text="Ready. Configure parameters and click 'Start Autonomous Task'.",
            text_color="#A0A0A0",
            font=self.ctk.CTkFont(family="Segoe UI", size=11)
        )
        self.status_bar.pack(side="left", padx=14, pady=3)

        self.status_bar_info = self.ctk.CTkLabel(
            self.status_bar_frame,
            text=f"Config: {CONFIG_FILENAME} • Dual-Agent Mode Active",
            text_color="#707070",
            font=self.ctk.CTkFont(family="Segoe UI", size=11)
        )
        self.status_bar_info.pack(side="right", padx=14, pady=3)

    def _subscribe_nodelog(self):
        """Attaches a listener to NodeLog so structured events flow into the GUI queue."""
        def on_nodelog_event(event_type: str, data: dict):
            self.log_queue.put(("nodelog", event_type, data))

        NodeLog.add_listener(on_nodelog_event)

    def _on_browse_workspace(self):
        """Opens a directory selector dialog."""
        initial = self.ws_var.get().strip() or str(_ROOT)
        chosen = self.filedialog.askdirectory(
            initialdir=initial if os.path.exists(initial) else str(_ROOT),
            title="Select Target Workspace Directory"
        )
        if chosen:
            selected_path = os.path.abspath(chosen)
            self.ws_var.set(selected_path)
            self.stat_ws_lbl.configure(text=f"Target: {os.path.basename(selected_path)}")
            self.files_ws_label.configure(text=f"Location: {selected_path}")
            self._refresh_workspace_files()

    def _on_select_preset(self, choice: str):
        """Loads selected prompt template into prompt editor."""
        template = PROMPT_PRESETS.get(choice, "")
        if template:
            self.prompt_text.delete("1.0", "end")
            self.prompt_text.insert("end", template)
            self.status_bar.configure(text=f"Loaded preset: '{choice}'")

    def _on_reset_prompt(self):
        """Resets the prompt textarea to the active preset or default."""
        choice = self.preset_var.get()
        template = PROMPT_PRESETS.get(choice, DEFAULT_TASK_PROMPT) or DEFAULT_TASK_PROMPT
        self.prompt_text.delete("1.0", "end")
        self.prompt_text.insert("end", template)

    def _on_open_workspace_folder(self):
        """Opens the active workspace directory in the OS file explorer."""
        ws = self.ws_var.get().strip()
        if os.path.exists(ws):
            open_in_file_manager(ws)
        else:
            self.messagebox.showwarning("Directory Not Found", f"Workspace does not exist yet:\n{ws}")

    def _refresh_workspace_files(self):
        """Scans the active workspace directory and populates the Files tab and quick stats."""
        ws = self.ws_var.get().strip()
        if not ws:
            return

        # Clear existing file buttons
        for widget in self.files_list_frame.winfo_children():
            widget.destroy()

        if not os.path.exists(ws):
            self.stat_files_lbl.configure(text="Files: 0")
            lbl = self.ctk.CTkLabel(
                self.files_list_frame,
                text="(Workspace directory does not exist yet)",
                text_color="#707070",
                font=self.ctk.CTkFont(family="Segoe UI", size=11)
            )
            lbl.pack(padx=10, pady=10)
            return

        try:
            entries = sorted(os.listdir(ws))
            files = [e for e in entries if os.path.isfile(os.path.join(ws, e))]
            self.stat_files_lbl.configure(text=f"Files: {len(files)}")
            self.stat_ws_lbl.configure(text=f"Target: {os.path.basename(ws)}")
            self.files_ws_label.configure(text=f"Location: {ws}")

            if not files:
                lbl = self.ctk.CTkLabel(
                    self.files_list_frame,
                    text="(No files in workspace yet)",
                    text_color="#707070",
                    font=self.ctk.CTkFont(family="Segoe UI", size=11)
                )
                lbl.pack(padx=10, pady=10)
                return

            for filename in files:
                file_path = os.path.join(ws, filename)
                try:
                    size_bytes = os.path.getsize(file_path)
                    size_str = f"{size_bytes / 1024:.1f} KB" if size_bytes >= 1024 else f"{size_bytes} B"
                    mtime = datetime.fromtimestamp(os.path.getmtime(file_path)).strftime("%H:%M:%S")
                except Exception:
                    size_str = "Unknown"
                    mtime = "--"

                item_frame = self.ctk.CTkFrame(self.files_list_frame, fg_color="#222222", corner_radius=4)
                item_frame.pack(fill="x", pady=2, padx=2)

                btn = self.ctk.CTkButton(
                    item_frame,
                    text=f"📄  {filename}",
                    command=lambda f=filename: self._show_file_preview(f),
                    anchor="w",
                    fg_color="transparent",
                    hover_color="#303030",
                    text_color="#ECECEC",
                    font=self.ctk.CTkFont(family="Consolas", size=11),
                    height=26
                )
                btn.pack(side="left", fill="x", expand=True, padx=(4, 8))

                size_lbl = self.ctk.CTkLabel(
                    item_frame,
                    text=f"{size_str} • {mtime}",
                    font=self.ctk.CTkFont(family="Segoe UI", size=10),
                    text_color="#808080"
                )
                size_lbl.pack(side="right", padx=6)

        except Exception as e:
            lbl = self.ctk.CTkLabel(
                self.files_list_frame,
                text=f"Error scanning workspace: {e}",
                text_color="#E06C75",
                font=self.ctk.CTkFont(family="Segoe UI", size=11)
            )
            lbl.pack(padx=10, pady=10)

    def _show_file_preview(self, filename: str):
        """Displays the selected file content inside the inspector preview box."""
        ws = self.ws_var.get().strip()
        file_path = os.path.join(ws, filename)
        self.inspector_title.configure(text=f"File Preview: {filename}")
        self.file_preview_text.delete("1.0", "end")

        if not os.path.exists(file_path):
            self.file_preview_text.insert("end", f"File not found: {file_path}")
            return

        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            self.file_preview_text.insert("end", content)
        except Exception as e:
            self.file_preview_text.insert("end", f"Error reading file: {e}")

    def _get_current_inputs(self) -> Dict[str, Any]:
        return {
            "tunnel_url": self.url_var.get().strip(),
            "workspace_root": self.ws_var.get().strip(),
            "model": self.model_var.get().strip(),
            "max_rounds": int(self.rounds_var.get() or 50),
            "task_prompt": self.prompt_text.get("1.0", "end").strip(),
        }

    def _on_save_config_click(self):
        """Saves current input fields to orchestrator_config.json."""
        inputs = self._get_current_inputs()
        if save_config(inputs):
            self.status_bar.configure(text=f"Settings saved to {CONFIG_FILENAME}.")
            self._append_agent_stream(f"[+] Saved settings to {CONFIG_FILENAME}\n", "tag_success")
        else:
            self.status_bar.configure(text="Error saving settings.")

    def _on_clear_streams(self):
        """Clears console, stream, and telemetry textareas."""
        self.agent_stream_text.delete("1.0", "end")
        self.telemetry_text.delete("1.0", "end")
        self.console_text.delete("1.0", "end")
        self.runtime_status_badge.configure(text="● IDLE", text_color="#A0A0A0")
        self.stat_round_lbl.configure(text="Round: 0")
        self.active_round = 0

    def _on_clear_log(self):
        """Backward-compatibility alias for test suites."""
        self._on_clear_streams()

    def _append_log_text(self, text: str, tag: Optional[str] = None):
        """Backward-compatibility alias for test suites."""
        self._append_agent_stream(text, tag)
        self._append_console(text, tag)

    @staticmethod
    def _detect_tag(text: str) -> Optional[str]:
        """Heuristic color tagging for streamed stdout lines."""
        stripped = text.strip()
        if stripped.startswith("[+]"):
            return "tag_success"
        if stripped.startswith("[!]") or "Warning" in stripped or "WARN" in stripped:
            return "tag_warning"
        if "Error" in stripped or "Traceback" in stripped or "HTTPError" in stripped:
            return "tag_error"
        if stripped.startswith("="):
            return "tag_task"
        if stripped.startswith("[*]"):
            if "Autonomous" in stripped or "Task" in stripped or "Finished" in stripped:
                return "tag_task"
            return "tag_info"
        if "CoderAgent" in stripped or "UserProxyRunner" in stripped:
            return "tag_agent"
        if any(tool in stripped for tool in ("[NodeForge]", "[NodePulse]", "[NodeInsight]", "[NodeLog]", "Executing tool", "Registered")):
            return "tag_tool"
        return None

    def _on_probe_click(self):
        """Triggers asynchronous endpoint health probe."""
        url = self.url_var.get().strip()
        model = self.model_var.get().strip()

        if not url:
            self.messagebox.showwarning("Missing URL", "Please enter a valid Cloudflare Tunnel URL.")
            return

        self.probe_btn.configure(state="disabled", text="...")
        self.health_badge.configure(text="● Probing...", text_color="#E5B567")
        self.status_bar.configure(text=f"Probing endpoint: {url} ...")
        self._append_agent_stream(f"\n[*] Probing endpoint: {url} ...\n", "tag_info")

        def _worker():
            def _log(msg: str):
                self.log_queue.put(("stream", "stdout", msg + "\n"))
            ok, msg = probe_endpoint_health(url, model=model, timeout=20, logger=_log)
            self.log_queue.put(("probe_result", ok, msg))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_stop_task(self):
        """Signals task interruption."""
        if self.is_running:
            self.stop_requested = True
            self.status_bar.configure(text="Stopping autonomous task...")
            self._append_agent_stream("[!] Abort requested by user.\n", "tag_warning")

    def _on_start_task(self):
        """Validates inputs, saves config, and launches the autonomous task in a worker thread."""
        if self.is_running:
            return

        inputs = self._get_current_inputs()
        url = inputs["tunnel_url"]
        ws = inputs["workspace_root"]
        model = inputs["model"]
        max_rounds = inputs["max_rounds"]
        prompt = inputs["task_prompt"]

        if not url:
            self.messagebox.showwarning("Input Error", "Please provide a Cloudflare Tunnel URL.")
            return
        if not ws:
            self.messagebox.showwarning("Input Error", "Please provide a target workspace directory.")
            return
        if not prompt:
            self.messagebox.showwarning("Input Error", "Task prompt cannot be empty.")
            return

        # Auto-save config
        save_config(inputs)

        # Update UI state
        self.is_running = True
        self.stop_requested = False
        self.active_round = 0
        self.start_btn.configure(state="disabled", text="⏳ Running Task...", fg_color="#2A2A2A", text_color="#606060")
        self.stop_btn.configure(state="normal", text_color="#ECECEC", fg_color="#8A2B2B")
        self.runtime_status_badge.configure(text="● RUNNING", text_color="#E5B567")
        self.status_bar.configure(text="Autonomous multi-agent task running. Streaming output...")

        self._append_agent_stream("\n" + "=" * 70 + "\n", "tag_banner")
        self._append_agent_stream(f"[*] Starting Autonomous Task at {datetime.now().strftime('%H:%M:%S')}\n", "tag_banner")
        self._append_agent_stream(f"[*] Target Workspace: {ws}\n", "tag_info")
        self._append_agent_stream(f"[*] Remote Endpoint : {url}\n", "tag_info")
        self._append_agent_stream(f"[*] Target Model    : {model}\n", "tag_info")
        self._append_agent_stream("=" * 70 + "\n\n", "tag_banner")

        def _worker():
            old_stdout = sys.stdout
            old_stderr = sys.stderr
            q_out = QueueStream(self.log_queue, old_stdout, "stdout")
            q_err = QueueStream(self.log_queue, old_stderr, "stderr")
            sys.stdout = q_out
            sys.stderr = q_err

            try:
                result = run_autonomous_orchestrator(
                    workspace_root=ws,
                    base_url=url,
                    task_prompt=prompt,
                    model=model,
                    max_rounds=max_rounds,
                    should_stop=lambda: self.stop_requested,
                    logger=print
                )
                self.log_queue.put(("task_finished", True, result))
            except Exception as e:
                self.log_queue.put(("task_finished", False, str(e)))
            finally:
                sys.stdout = old_stdout
                sys.stderr = old_stderr

        self.worker_thread = threading.Thread(target=_worker, daemon=True)
        self.worker_thread.start()

    def _append_agent_stream(self, text: str, tag: Optional[str] = None):
        """Appends text to the Agent Stream textbox."""
        self.agent_stream_text.insert("end", text, tag or ())
        if self.autoscroll_var.get():
            self.agent_stream_text.see("end")

    def _append_console(self, text: str, tag: Optional[str] = None):
        """Appends text to the Raw Console textbox."""
        self.console_text.insert("end", text, tag or ())
        if self.autoscroll_var.get():
            self.console_text.see("end")

    def _append_telemetry(self, text: str, tag: Optional[str] = None):
        """Appends text to the Telemetry textbox."""
        self.telemetry_text.insert("end", text, tag or ())
        if self.autoscroll_var.get():
            self.telemetry_text.see("end")

    def _process_log_queue(self):
        """Polls the queue and routes items to the appropriate tab in real time."""
        try:
            while not self.log_queue.empty():
                item = self.log_queue.get_nowait()
                msg_type = item[0]

                if msg_type == "stream":
                    _, stream_name, text = item
                    tag = "tag_err" if stream_name == "stderr" else "tag_norm"
                    self._append_console(text, tag)

                elif msg_type == "nodelog":
                    _, event_type, data = item
                    level = data.get("status_level", "INFO")
                    msg = data.get("message", "")
                    src = data.get("source", "NodeLog")

                    tel_tag = (
                        "tag_tel_success" if level == "SUCCESS"
                        else "tag_tel_warning" if level in ("WARN", "WARNING")
                        else "tag_tel_error" if level in ("ERROR", "CRITICAL")
                        else "tag_tel_tool" if any(k in event_type for k in ("TOOL", "FILE", "COMMAND"))
                        else "tag_tel_info"
                    )
                    ts = datetime.now().strftime("%H:%M:%S")
                    self._append_telemetry(f"[{ts}] [{src}] [{event_type}] {msg}\n", tel_tag)

                    # Route special events into Agent Stream
                    if event_type == "AGENT_MESSAGE":
                        sender = data.get("source", "Agent")
                        recipient = data.get("recipient", "")
                        raw_msg = data.get("message", "")
                        if sender == "CoderAgent":
                            self.active_round += 1
                            self.stat_round_lbl.configure(text=f"Round: {self.active_round}")
                            self._append_agent_stream(f"\n┌── [CoderAgent ➜ {recipient}] (Round {self.active_round})\n", "tag_agent_coder")
                            self._append_agent_stream(f"{raw_msg}\n", "tag_info")
                            self._append_agent_stream("└" + "─" * 50 + "\n", "tag_agent_coder")
                        else:
                            self._append_agent_stream(f"\n┌── [UserProxyRunner ➜ {recipient}]\n", "tag_agent_user")
                            self._append_agent_stream(f"{raw_msg}\n", "tag_info")
                            self._append_agent_stream("└" + "─" * 50 + "\n", "tag_agent_user")

                    elif event_type == "FILE_CREATED":
                        f_name = data.get("file", "")
                        f_bytes = data.get("bytes", 0)
                        self._append_agent_stream(f"  [+] [NodeForge] Created File: '{f_name}' ({f_bytes} bytes)\n", "tag_file_created")
                        # Real-time workspace update
                        self._refresh_workspace_files()

                    elif event_type == "COMMAND_RUN":
                        cmd = data.get("command", "")
                        code = data.get("exit_code", 0)
                        self._append_agent_stream(f"  [>] [NodePulse] Executed: `{cmd}` (exit code {code})\n", "tag_command")

                elif msg_type == "probe_result":
                    _, ok, msg = item
                    self.probe_btn.configure(state="normal", text="Probe")
                    if ok:
                        self.health_badge.configure(text="● Reachable", text_color="#7BC28C")
                        self.status_bar.configure(text=f"Probe Success: {msg}")
                        self._append_agent_stream(f"[+] Endpoint probe successful: {msg}\n", "tag_success")
                    else:
                        if "not loaded" in msg.lower() or "500" in msg:
                            badge_txt = "● Model Unloaded"
                            badge_color = "#E5B567"
                        elif any(c in msg for c in ("502", "503", "530", "timed out")):
                            badge_txt = "● Tunnel Down"
                            badge_color = "#E06C75"
                        else:
                            badge_txt = "● Unreachable"
                            badge_color = "#E06C75"
                        self.health_badge.configure(text=badge_txt, text_color=badge_color)
                        self.status_bar.configure(text=f"Probe: {msg}")
                        self._append_agent_stream(f"[!] Endpoint probe check: {msg}\n", "tag_warning" if "500" in msg else "tag_error")

                elif msg_type == "task_finished":
                    _, success, res = item
                    self.is_running = False
                    self.start_btn.configure(state="normal", text="▶  Start Autonomous Task", fg_color="#D97757", text_color="#FFFFFF")
                    self.stop_btn.configure(state="disabled", fg_color="#383838", text_color="#A0A0A0")
                    self._refresh_workspace_files()

                    if success:
                        status_str = res.get("status", "IN_PROGRESS") if isinstance(res, dict) else str(res)
                        completed = status_str == "COMPLETED"
                        self.runtime_status_badge.configure(text=f"● {status_str}", text_color="#7BC28C" if completed else "#E5B567")
                        self.status_bar.configure(text=f"Task Finished: {status_str}")
                        self._append_agent_stream(f"\n[*] Autonomous Task Finished: {status_str}\n", "tag_success" if completed else "tag_warning")
                    else:
                        self.runtime_status_badge.configure(text="● ERROR", text_color="#E06C75")
                        self.status_bar.configure(text=f"Task Error: {res}")
                        self._append_agent_stream(f"\n[!] Task Execution Error: {res}\n", "tag_error")

        except Exception as e:
            print(f"[!] Log queue processing error: {e}", file=sys.__stderr__)

        self.root.after(50, self._process_log_queue)


# =====================================================================
# CLI Fallback Mode
# =====================================================================

def run_cli_fallback(
    tunnel_url: Optional[str] = None,
    workspace_root: Optional[str] = None,
    model: Optional[str] = None,
    task_prompt: Optional[str] = None,
    non_interactive: bool = False,
    max_rounds: Optional[int] = None,
    compat: Optional[bool] = None,
    resume: bool = False,
) -> Dict[str, Any]:
    """
    Graceful fallback for headless/terminal environments prompting via standard CLI inputs.
    """
    cfg = load_config()

    print("\n" + "=" * 70)
    print(" NodeCore Autonomous Multi-Agent Orchestrator (CLI Mode)")
    print("=" * 70)

    selected_url = tunnel_url or cfg["tunnel_url"]
    selected_ws = workspace_root or cfg["workspace_root"]
    selected_model = model or cfg["model"]
    selected_prompt = task_prompt or cfg["task_prompt"]
    selected_rounds = max_rounds if max_rounds is not None else cfg.get("max_rounds", 50)

    if not non_interactive:
        print("\n[Configuration Settings]")
        val_url = input(f"1. Cloudflare Tunnel URL [{selected_url}]: ").strip()
        selected_url = val_url or selected_url

        val_ws = input(f"2. Target Workspace Directory [{selected_ws}]: ").strip()
        selected_ws = val_ws or selected_ws

        val_model = input(f"3. LLM Model [{selected_model}]: ").strip()
        selected_model = val_model or selected_model

    # Save selected values to orchestrator_config.json
    save_config({
        "tunnel_url": selected_url,
        "workspace_root": selected_ws,
        "model": selected_model,
        "max_rounds": selected_rounds,
        "task_prompt": selected_prompt
    })

    # Probe endpoint
    print(f"\n[*] Probing endpoint health: {selected_url} ...")
    ok, probe_msg = probe_endpoint_health(selected_url, model=selected_model)
    if not ok:
        print(f"[!] Warning: Remote LLM endpoint check reported: {probe_msg}")
        if not non_interactive:
            proceed = input("    Proceed with task anyway? [y/N]: ").strip().lower()
            if proceed not in ("y", "yes"):
                print("[*] Aborted by user.")
                return {"status": "FAILED", "termination_reason": "Endpoint unreachable"}

    # Execute
    return run_autonomous_orchestrator(
        workspace_root=selected_ws,
        base_url=selected_url,
        task_prompt=selected_prompt,
        model=selected_model,
        max_rounds=selected_rounds,
        compat=compat,
        resume=resume,
    )


# =====================================================================
# Main Launcher Dispatcher
# =====================================================================

def launch_orchestrator(
    force_cli: bool = False,
    non_interactive: bool = False,
    tunnel_url: Optional[str] = None,
    workspace_root: Optional[str] = None,
    model: Optional[str] = None,
    task_prompt: Optional[str] = None,
    max_rounds: Optional[int] = None,
    compat: Optional[bool] = None,
    resume: bool = False,
):
    """
    Entrypoint that automatically launches the modern CustomTkinter GUI if possible,
    with seamless fallback to CLI mode if GUI display is unavailable.
    """
    if force_cli or non_interactive:
        return run_cli_fallback(
            tunnel_url=tunnel_url,
            workspace_root=workspace_root,
            model=model,
            task_prompt=task_prompt,
            non_interactive=non_interactive,
            max_rounds=max_rounds,
            compat=compat,
            resume=resume,
        )

    try:
        import customtkinter as ctk
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        root = ctk.CTk()
        cfg = load_config()
        if tunnel_url:
            cfg["tunnel_url"] = tunnel_url
        if workspace_root:
            cfg["workspace_root"] = workspace_root
        if model:
            cfg["model"] = model
        if task_prompt:
            cfg["task_prompt"] = task_prompt
        if max_rounds is not None:
            cfg["max_rounds"] = max_rounds

        app = NodeCoreLauncherApp(root, initial_config=cfg)
        root.mainloop()
        return {"status": "GUI_EXITED"}

    except Exception as e:
        print(f"[*] GUI initialization bypassed ({e}). Falling back to interactive CLI mode.")
        return run_cli_fallback(
            tunnel_url=tunnel_url,
            workspace_root=workspace_root,
            model=model,
            task_prompt=task_prompt,
            non_interactive=False,
            max_rounds=max_rounds,
        )


if __name__ == "__main__":
    launch_orchestrator()
