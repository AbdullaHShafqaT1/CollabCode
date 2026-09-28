"""Fresh-folder CloudCode chess run + independent completion score.

Usage: .venv/bin/python scratch/run_chess_test.py [--score-only] [--rounds N]
Wipes Testing_Chess/ before every run so each attempt is verifiable on its own.
"""
import json, shutil, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WS = Path(sys.argv[sys.argv.index("--dir") + 1]).resolve() if "--dir" in sys.argv else ROOT / "Testing_Chess"
sys.path[:0] = [str(ROOT), str(ROOT / "NodeCore")]
PY = sys.executable


def run(rounds, resume=False):
    from launcher_gui import run_autonomous_orchestrator, load_config
    cfg = load_config()
    if WS.exists() and not resume:
        shutil.rmtree(WS)
    (WS / ".cloudcode").mkdir(parents=True, exist_ok=True)
    acceptance = {
        "required_files": ["chess_logic.py", "chess_gui.py", "main.py", "test_chess.py", "RunningGUIDE.txt"],
        "checks": [
            {"requirement": "All modules import without starting a GUI mainloop",
             "command": f'"{PY}" -c "import chess_logic, chess_gui, main"'},
            {"requirement": "The unittest suite test_chess.py passes",
             "command": f'"{PY}" -m unittest -v test_chess'},
        ],
    }
    (WS / ".cloudcode" / "acceptance.json").write_text(json.dumps(acceptance, indent=1))
    start = time.time()
    result = run_autonomous_orchestrator(str(WS), cfg["tunnel_url"], cfg["task_prompt"],
                                         model=cfg.get("model", "qwen2.5-coder:32b"), max_rounds=rounds,
                                         acceptance=acceptance, resume=resume)
    print(f"\n[run] status={result.get('status')} rounds={result.get('rounds')} "
          f"phases={result.get('completed_phases')} elapsed={time.time() - start:.0f}s")
    print("[run] reason:", result.get("termination_reason"))
    print("[run] remaining:", json.dumps(result.get("remaining_requirements", []))[:1500])


PROBE = r'''
import json, os, sys, tempfile, unittest, io
sys.path.insert(0, os.getcwd())
res = {}
def chk(name, fn):
    try: res[name] = bool(fn())
    except Exception as e: res[name] = False; res[name + "_err"] = repr(e)[:200]
import chess_logic as L
G = L.ChessGame
def fresh(): return G()
chk("logic: 32 pieces initial", lambda: sum(p is not None for r in fresh().board for p in r) == 32)
chk("logic: back ranks correct", lambda: fresh().board[0] == ['bR','bN','bB','bQ','bK','bB','bN','bR'] and fresh().board[7] == ['wR','wN','wB','wQ','wK','wB','wN','wR'] and fresh().board[1] == ['bP']*8 and fresh().board[6] == ['wP']*8)
chk("logic: current_turn W", lambda: fresh().current_turn == 'W')
chk("logic: get_piece", lambda: fresh().get_piece(7, 4) == 'wK' and fresh().get_piece(4, 4) is None)
chk("logic: pawn 1/2 step", lambda: set(map(tuple, fresh().get_legal_moves(6, 4))) == {(5, 4), (4, 4)})
chk("logic: knight leap", lambda: set(map(tuple, fresh().get_legal_moves(7, 6))) == {(5, 5), (5, 7)})
chk("logic: blocked rook/bishop/queen/king", lambda: all(fresh().get_legal_moves(7, c) == [] for c in (0, 2, 3, 4)))
def turns():
    g = fresh(); a = g.make_move((6, 4), (4, 4)); t1 = g.current_turn
    b = g.make_move((1, 4), (3, 4)); return a and b and t1 == 'B' and g.current_turn == 'W'
chk("logic: make_move + turn alternation", turns)
def illegal():
    g = fresh(); return g.make_move((6, 4), (3, 4)) is False and g.current_turn == 'W' and g.board[6][4] == 'wP'
chk("logic: illegal move rejected", illegal)
def wrong_turn():
    g = fresh(); return not g.make_move((1, 4), (3, 4))
chk("logic: cannot move opponent piece", wrong_turn)
def empty():
    g = fresh(); g.board = [[None]*8 for _ in range(8)]; return g
def slide():
    # Kings placed off every queen line, so an open-board queen has exactly 27 moves.
    g = empty(); g.board[4][4] = 'wQ'; g.board[0][1] = 'bK'; g.board[7][0] = 'wK'
    return len(g.get_legal_moves(4, 4)) == 27
chk("logic: queen ray-cast (27 moves open board)", slide)
def capture():
    g = empty(); g.board[4][4] = 'wP'; g.board[3][5] = 'bN'; g.board[3][3] = 'wN'; g.board[0][0]='bK'; g.board[7][7]='wK'
    m = set(map(tuple, g.get_legal_moves(4, 4))); return (3, 5) in m and (3, 3) not in m
chk("logic: pawn diagonal capture only enemy", capture)
def check():
    g = empty(); g.board[7][4] = 'wK'; g.board[0][4] = 'bR'; g.board[0][0] = 'bK'
    return g.is_in_check('W') is True and g.is_in_check('B') is False
chk("logic: is_in_check", check)
def saveload():
    g = fresh(); g.make_move((6, 4), (4, 4)); p = os.path.join(tempfile.mkdtemp(), 's.json')
    g.save_game(p); json.load(open(p)); h = fresh(); h.load_game(p)
    return h.board == g.board and h.current_turn == 'B'
chk("logic: JSON save/load", saveload)
def tests():
    suite = unittest.defaultTestLoader.loadTestsFromName('test_chess')
    r = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
    res["tests_run"] = r.testsRun
    return r.wasSuccessful() and r.testsRun >= 6
chk("tests: test_chess passes (>=6 tests)", tests)
src = open('chess_gui.py').read() if os.path.exists('chess_gui.py') else ''
chk("gui: imports", lambda: __import__('chess_gui') is not None)
chk("gui: colors #EEEED2/#769656", lambda: '#eeeed2' in src.lower() and '#769656' in src.lower())
chk("gui: unicode glyphs via create_text", lambda: 'create_text' in src and ('♚' in src or '\\u265a' in src.lower()))
chk("gui: no PhotoImage", lambda: 'PhotoImage' not in src and src != '')
chk("gui: New/Save/Load buttons", lambda: all(s in src for s in ('New Game', 'Save Game', 'Load Game')))
chk("gui: turn banner + check warning", lambda: 'White to move' in src and 'Black to move' in src and 'check' in src.lower())
chk("gui: checkmate/winner dialog", lambda: 'messagebox' in src and ('mate' in src.lower() or 'winner' in src.lower()))
def gui_live():
    import tkinter as tk, chess_gui
    root = tk.Tk(); root.withdraw()
    cls = getattr(chess_gui, 'ChessGUI')
    try: gui = cls(root, G())
    except TypeError: gui = cls(root)
    root.update(); ok = any(isinstance(w, tk.Canvas) for w in root.winfo_children() + [x for f in root.winfo_children() for x in f.winfo_children()])
    root.destroy(); return ok
chk("gui: ChessGUI builds a Canvas (headless)", gui_live)
msrc = open('main.py').read() if os.path.exists('main.py') else ''
chk("main: imports ChessGame + ChessGUI with __main__ guard", lambda: 'ChessGame' in msrc and 'ChessGUI' in msrc and '__main__' in msrc)
gsrc = open('RunningGUIDE.txt').read() if os.path.exists('RunningGUIDE.txt') else ''
chk("guide: launch + controls + rules", lambda: 'main.py' in gsrc and 'click' in gsrc.lower() and len(gsrc) > 300)
print("__RESULT__" + json.dumps(res))
'''


def score():
    files = ["chess_logic.py", "chess_gui.py", "main.py", "test_chess.py", "RunningGUIDE.txt"]
    present = {f: (WS / f).is_file() for f in files}
    out = subprocess.run([PY, "-c", PROBE], cwd=WS, capture_output=True, text=True, timeout=120)
    line = next((l for l in out.stdout.splitlines() if l.startswith("__RESULT__")), None)
    res = json.loads(line[10:]) if line else {}
    checks = {k: v for k, v in res.items() if isinstance(v, bool)}
    if not checks:
        print("[score] probe crashed:", out.stderr[-1500:])
    total = 30  # 5 files + 25 probe checks
    passed = sum(present.values()) + sum(checks.values())
    print("\n[score] files:", present)
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}" + (f"   ({res.get(k + '_err')})" if res.get(k + '_err') else ""))
    print(f"[score] COMPLETION = {passed}/{total} = {100 * passed / total:.0f}%")


if __name__ == "__main__":
    rounds = int(sys.argv[sys.argv.index("--rounds") + 1]) if "--rounds" in sys.argv else 90
    if "--score-only" not in sys.argv:
        run(rounds, resume="--resume" in sys.argv)
    score()
