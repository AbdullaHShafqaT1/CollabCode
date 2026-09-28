"""Compatibility with endpoints that cap replies, drop system/history or go offline."""
import json
import tempfile
from pathlib import Path

import pytest

from node_core import incremental as inc
from node_core.agents import fold_rules_into_last, make_resilient_reply, detect_project_profile
from node_core.workflow import TaskWorkflow

TASK = "Build a Chess game with chess_logic.py, chess_gui.py, main.py, test_chess.py and RunningGUIDE.txt"


def _has(module):
    import importlib.util
    return importlib.util.find_spec(module) is not None


def fence(name, code, closed=True):
    return f"```python\n# filename: {name}\n{code}\n" + ("```" if closed else "")


SKELETON = '''import json

class ChessGame:
    def __init__(self):
        self.turn = "W"

    def flip(self):
        """Switch turn and return the new colour."""
        raise NotImplementedError

    def save(self, path):
        """Write state as JSON."""
        raise NotImplementedError
'''


def workflow(tmp_path, **kw):
    return TaskWorkflow(tmp_path, TASK, detect_project_profile(TASK), max_rounds=40, incremental=True, **kw)


def test_find_stubs_and_splice_with_helper():
    assert inc.find_stubs(SKELETON) == ["ChessGame.flip", "ChessGame.save"]
    reply = ("def flip(self):\n    self.turn = self._other()\n    return self.turn\n\n"
             "def _other(self):\n    \"\"\"Opposite colour.\"\"\"\n    raise NotImplementedError\n")
    code, changed = inc.splice(SKELETON, reply, target="ChessGame.flip")
    assert inc.find_stubs(code) == ["ChessGame._other", "ChessGame.save"]
    assert "    def _other(self):" in code and changed[0] == "ChessGame.flip"


def test_splice_accepts_class_wrapped_method_and_adds_imports():
    reply = "import os\n\nclass ChessGame:\n    def save(self, path):\n        json.dump({'t': self.turn}, open(path, 'w'))\n"
    code, _ = inc.splice(SKELETON, reply, target="ChessGame.save")
    assert "import os" in code and "json.dump" in code and inc.find_stubs(code) == ["ChessGame.flip"]


def test_splice_rejects_wrong_or_truncated_reply():
    with pytest.raises(ValueError):
        inc.splice(SKELETON, "def other(self):\n    return 1\n", target="ChessGame.flip")
    with pytest.raises(SyntaxError):
        inc.splice(SKELETON, "def flip(self):\n    if (", target="ChessGame.flip")


def test_incremental_build_assembles_file_from_method_replies(tmp_path):
    w = workflow(tmp_path)
    first = w.initial_prompt()
    assert "SKELETON" in first
    reply = w.process(fence("chess_logic.py", SKELETON))
    assert "implement `ChessGame.flip`" in reply and not (tmp_path / "chess_logic.py").exists()
    reply = w.process(fence("chess_logic.py", "    def flip(self):\n        self.turn = 'B' if self.turn == 'W' else 'W'\n        return self.turn"))
    assert "implement `ChessGame.save`" in reply
    # A cut-off method is first split into a dispatcher plus helper stubs...
    reply = w.process(fence("chess_logic.py", "def save(self, path):\n    with open(path", closed=False))
    assert "SHORT dispatcher" in reply and "`ChessGame.save`" in reply
    # ...and a still cut-off definition is continued from its saved lines.
    reply = w.process(fence("chess_logic.py", "def save(self, path):\n    data = self._state()\n    with open(path, 'w') as f:\n        json.du", closed=False))
    assert "continue `ChessGame.save`" in reply and "data = self._state()" in reply
    reply = w.process(fence("chess_logic.py", "        json.dump(data, f)\n\ndef _state(self):\n    \"\"\"State dict.\"\"\"\n    raise NotImplementedError"))
    assert "implement `ChessGame._state`" in reply
    w.process(fence("chess_logic.py", "def _state(self):\n    return {'turn': self.turn}"))
    code = (tmp_path / "chess_logic.py").read_text()
    compile(code, "chess_logic.py", "exec")
    assert "NotImplementedError" not in code and w.phase >= 3
    # The next file is started step by step as soon as the logic is saved.
    assert w.draft["name"] == "chess_gui.py" and w.draft["code"] is None


def test_draft_abandoned_after_repeated_failures(tmp_path):
    w = workflow(tmp_path)
    w.initial_prompt()
    for _ in range(3):
        reply = w.process("no code here")
    assert w.draft is None and "chess_logic.py" in w.incremental_disabled
    assert "SKELETON" not in reply


def test_method_patch_repairs_existing_file(tmp_path):
    w = workflow(tmp_path)
    body = SKELETON.replace('"""Switch turn and return the new colour."""\n        raise NotImplementedError',
                            "return self.turn").replace('"""Write state as JSON."""\n        raise NotImplementedError',
                                                         "json.dump({}, open(path, 'w'))")
    body += "".join(f"\n    def extra{i}(self):\n        return {i}\n" for i in range(8))
    (tmp_path / "chess_logic.py").write_text(body)
    w.phase = 4
    w.process(fence("chess_logic.py", "def flip(self):\n    self.turn = 'B'\n    return self.turn"))
    code = (tmp_path / "chess_logic.py").read_text()
    assert "self.turn = 'B'" in code and "def extra7" in code and "json.dump" in code


def test_state_is_saved_and_resumed(tmp_path):
    w = workflow(tmp_path)
    w.initial_prompt()
    w.process(fence("chess_logic.py", SKELETON))
    state = json.loads((tmp_path / ".cloudcode" / "state.json").read_text())
    assert state["draft"]["name"] == "chess_logic.py" and state["draft"]["code"]
    resumed = workflow(tmp_path, resume=True)
    assert resumed.resumed and "implement `ChessGame.flip`" in resumed.initial_prompt()


def test_behaviour_trace_reports_calls(tmp_path):
    (tmp_path / "logic.py").write_text("class G:\n    def ok(self, n):\n        return self.inner(n)\n"
                                       "    def inner(self, n):\n        return n - 1\n")
    (tmp_path / "test_logic.py").write_text("import unittest\nfrom logic import G\nclass T(unittest.TestCase):\n"
                                            "    def test_ok(self):\n        self.assertEqual(G().ok(3), 3)\n")
    w = workflow(tmp_path)
    out = w.behavior_trace("test_logic")
    assert "G.ok(3) -> 2" in out and "G.inner(3) -> 2" in out


def test_rules_folded_into_last_message_only():
    msgs = [{"role": "user", "content": "task"}, {"role": "assistant", "content": "x"}, {"role": "user", "content": "go"}]
    out = fold_rules_into_last(msgs, "RULE A")
    assert out[-1]["content"].startswith("OPERATING RULES") and "RULE A" in out[-1]["content"]
    assert out[-1]["content"].endswith("go") and msgs[-1]["content"] == "go" and out[0] is msgs[0]


def test_resilient_reply_waits_and_switches_endpoint():
    import openai
    httpx = pytest.importorskip("httpx2") if not _has("httpx") else __import__("httpx")

    class Agent:
        def __init__(self):
            self.llm_config = {"config_list": [{"model": "m", "base_url": "http://old/v1", "api_key": "x"}]}
            self.calls = 0

        def generate_oai_reply(self, messages, sender, config):
            self.calls += 1
            if self.calls == 1:
                raise openai.APIConnectionError(request=httpx.Request("POST", "http://old/v1"))
            return True, "ok"

    agent = Agent()
    waited = []
    reply = make_resilient_reply(lambda: "http://new", wait=lambda resolve, model, **kw: waited.append(resolve()) or "http://new",
                                 logger=lambda *a: None)
    assert reply(agent, [], None, None) == (True, "ok")
    assert waited == ["http://new"] and agent.llm_config["config_list"][0]["base_url"] == "http://new/v1"


def test_non_outage_errors_are_not_swallowed():
    class Agent:
        llm_config = {"config_list": [{"model": "m", "base_url": "http://x/v1"}]}

        def generate_oai_reply(self, *a):
            raise ValueError("bad request")

    with pytest.raises(ValueError):
        make_resilient_reply(lambda: "http://x", wait=lambda *a, **k: "http://x")(Agent(), [], None, None)


def test_undefined_helpers_get_stubs_and_can_be_patched_in():
    code = ("class G:\n    def moves(self, r, c):\n        return self._knight(r, c) + self._rook(r, c=c)\n\n"
            "    def other(self):\n        return 1\n")
    new, added = inc.add_missing_stubs(code)
    assert added == ["G._knight", "G._rook"]
    assert inc.find_stubs(new) == ["G._knight", "G._rook"] and "def _knight(self, r, c):" in new
    patched, placed = inc.splice(code, "def _knight(self, r, c):\n    return [(r, c)]\n", replace_implemented=True)
    assert placed == ["G._knight"] and [c[1] for c in inc.undefined_self_calls(patched)] == ["_rook"]


def test_outline_hides_implemented_bodies_and_harvest_reads_echoes():
    code = SKELETON.replace('"""Switch turn and return the new colour."""\n        raise NotImplementedError',
                            '"""Switch turn."""\n        self.turn = "B"\n        return self.turn')
    view = inc.outline(code)
    assert 'self.turn = "B"' not in view and "...  # implemented" in view and "raise NotImplementedError" in view
    echo = code.replace('"""Write state as JSON."""\n        raise NotImplementedError',
                        '"""Write state as JSON."""\n        json.dump({"t": self.turn}, open(path, "w"))') + \
        "\n    def later(self):\n        x = ["
    names = [n for n, _ in inc.harvest(echo, complete=False)]
    assert "save" in names and "flip" in names and "later" not in names


def test_echoed_draft_reply_still_makes_progress(tmp_path):
    w = workflow(tmp_path)
    w.initial_prompt()
    w.process(fence("chess_logic.py", SKELETON))
    echo = SKELETON.replace('"""Switch turn and return the new colour."""\n        raise NotImplementedError',
                            'self.turn = "B" if self.turn == "W" else "W"\n        return self.turn')
    echo = echo.replace('"""Write state as JSON."""\n        raise NotImplementedError',
                        'json.dump({"turn": self.turn}, open(path, "w"))') + "\n    def extra(self):\n        return ["
    w.process(fence("chess_logic.py", echo, closed=False))
    assert (tmp_path / "chess_logic.py").is_file()


def test_stubborn_stub_is_deferred_not_abandoned(tmp_path):
    w = workflow(tmp_path)
    w.initial_prompt()
    w.process(fence("chess_logic.py", SKELETON))
    for _ in range(2):  # the model keeps sending the wrong function
        reply = w.process(fence("chess_logic.py", "def unrelated(self):\n    return 1"))
    assert w.draft is not None and "implement `ChessGame.save`" in reply
    w.process(fence("chess_logic.py", "def save(self, path):\n    json.dump({}, open(path, 'w'))"))
    reply = w.process(fence("chess_logic.py", "def flip(self):\n    return self.turn"))
    assert (tmp_path / "chess_logic.py").is_file()


def test_split_helpers_are_continued_not_split_again(tmp_path):
    w = workflow(tmp_path)
    w.initial_prompt()
    w.process(fence("chess_logic.py", SKELETON))
    w.process(fence("chess_logic.py", "def flip(self):\n    x = (", closed=False))  # cut off -> split
    w.process(fence("chess_logic.py", "def flip(self):\n    return self._a()\n\ndef _a(self):\n    \"\"\"A.\"\"\"\n    raise NotImplementedError"))
    reply = w.process(fence("chess_logic.py", "def _a(self):\n    y = 1\n    z = (", closed=False))
    assert "continue `ChessGame._a`" in reply and "SHORT dispatcher" not in reply


def test_slow_method_cycle_is_not_reported_as_stalled(tmp_path):
    w = TaskWorkflow(tmp_path, TASK, detect_project_profile(TASK), max_rounds=40, incremental=True, stall_limit=4)
    w.initial_prompt()
    w.process(fence("chess_logic.py", SKELETON))
    w.process(fence("chess_logic.py", "def flip(self):\n    a = 1\n    b = (", closed=False))       # -> split
    w.process(fence("chess_logic.py", "def flip(self):\n    a = 1\n    b = 2\n    c = (", closed=False))  # -> continue
    w.process(fence("chess_logic.py", "    c = 3\n    d = 4\n    e = (", closed=False))           # partial grows
    w.process(fence("chess_logic.py", "def unrelated(self):\n    return 1"))                        # failed attempt
    assert w.status == "IN_PROGRESS" and w.draft is not None


def test_mostly_finished_draft_is_saved_with_stubs_not_discarded(tmp_path):
    w = workflow(tmp_path)
    w.initial_prompt()
    w.process(fence("chess_logic.py", SKELETON))
    w.process(fence("chess_logic.py", "def flip(self):\n    return self.turn"))
    for _ in range(3):  # the last stub is never supplied
        w.process(fence("chess_logic.py", "def unrelated(self):\n    return 1"))
    code = (tmp_path / "chess_logic.py").read_text()
    assert "return self.turn" in code and inc.find_stubs(code) == ["ChessGame.save"]
    assert any("NotImplementedError" in e for e in w.verify())


def test_autogen_timeout_counts_as_outage():
    from node_core.agents import is_endpoint_outage
    assert is_endpoint_outage(TimeoutError("OpenAI API call timed out."))
    assert not is_endpoint_outage(ValueError("bad"))


def test_whole_file_rewrite_patches_changed_functions_only(tmp_path):
    w = workflow(tmp_path)
    good = SKELETON.replace('"""Switch turn and return the new colour."""\n        raise NotImplementedError', "return self.turn")
    good = good.replace('"""Write state as JSON."""\n        raise NotImplementedError', "json.dump({}, open(path, 'w'))")
    (tmp_path / "chess_logic.py").write_text(good)
    w.phase = 4
    # An outline copy with one real fix, cut off at the end.
    reply = good.replace("return self.turn", "self.turn = 'B'\n        return self.turn").replace(
        "json.dump({}, open(path, 'w'))", "...  # implemented") + "\n    def later(self):\n        x = ("
    w.process(fence("chess_logic.py", reply, closed=False))
    code = (tmp_path / "chess_logic.py").read_text()
    assert "self.turn = 'B'" in code and "json.dump" in code and "# implemented" not in code
    assert w.partial is None


def test_identical_rewrite_is_reported_and_temperature_rises(tmp_path):
    w = TaskWorkflow(tmp_path, TASK, detect_project_profile(TASK), max_rounds=40)
    (tmp_path / "chess_logic.py").write_text(SKELETON)
    w.phase = 4
    replies = [w.process(fence("chess_logic.py", SKELETON.rstrip("\n"))) for _ in range(3)]
    assert any(m in replies[0] for m in ("IDENTICAL to the existing file", "without a usable change"))
    assert any(m in replies[-1] for m in ("IDENTICAL to the existing file", "without a usable change"))
    assert w.temperature_hint == 0.6


def test_temperature_hint_rebuilds_client():
    class WF:
        temperature_hint = 0.9

    class Agent:
        llm_config = {"config_list": [{"model": "m", "base_url": "http://x/v1", "api_key": "k"}], "temperature": 0.2}
        workflow_ref = WF()

        def generate_oai_reply(self, *a):
            return True, self.llm_config["temperature"]

    agent = Agent()
    assert make_resilient_reply(lambda: "http://x")(agent, [], None, None) == (True, 0.9)
    WF.temperature_hint = None
    assert make_resilient_reply(lambda: "http://x")(agent, [], None, None) == (True, 0.2)


RUN10 = Path(__file__).with_name("fixtures") / "run10"


def run10_workspace(tmp_path):
    """Run 10's real buggy logic ('w' compared with 'W') and the model's own tests."""
    import shutil
    for name in ("chess_logic.py", "test_chess.py"):
        shutil.copy(RUN10 / (name + ".fixture"), tmp_path / name)
    (tmp_path / "chess_gui.py").write_text("class ChessGUI:\n    pass\n")
    (tmp_path / "main.py").write_text("import chess_logic, chess_gui\n")
    w = TaskWorkflow(tmp_path, TASK, detect_project_profile(TASK), max_rounds=40)
    w.phase = 4
    return w


def test_resampling_targets_failing_function_without_showing_its_body(tmp_path):
    w = run10_workspace(tmp_path)
    buggy = "if piece and piece[0] == self.current_turn:"
    assert buggy in (tmp_path / "chess_logic.py").read_text()
    prompts = [w.process("No code this time.") for _ in range(2)]
    assert "FRESH REWRITE" in prompts[-1] and "`ChessGame.make_move`" in prompts[-1]
    assert buggy not in prompts[-1].split("Observed facts")[0]  # old body hidden
    assert "self.current_turn='W'" in prompts[-1]                 # concrete values shown
    assert w.temperature_hint == 0.8


def test_gate_keeps_improvement_and_reverts_regression(tmp_path):
    w = run10_workspace(tmp_path)
    w.process("No code this time.")
    baseline = w.best["passes"]
    # Breaks a test that currently passes (JSON save/load), so passing tests drop.
    worse = "def save_game(self, filepath):\n    raise RuntimeError('broken')\n"
    reply = w.process(fence("chess_logic.py", worse))
    assert "REVERTED" in reply and "raise RuntimeError" not in (tmp_path / "chess_logic.py").read_text()
    assert w.verification[-1]["reverted"] and not w.verification[-1]["passed"]
    code = (tmp_path / "chess_logic.py").read_text()
    fixed = [("    def " + name + code.split("    def " + name)[1].split("\n    def ")[0])
             .replace("piece[0] == self.current_turn", "piece[0].upper() == self.current_turn")
             .replace("piece[0] != self.current_turn", "piece[0].upper() != self.current_turn")
             for name in ("get_legal_moves", "make_move")]
    w.resample, w.assertion_rounds = None, 0  # an ordinary repair (not a fresh rewrite) fixing two functions
    w.process(fence("chess_logic.py", "\n\n".join(fixed)))
    assert "piece[0].upper()" in (tmp_path / "chess_logic.py").read_text()
    assert w.best["passes"] > baseline


def test_repair_reply_budget_is_small():
    w = TaskWorkflow(tempfile.mkdtemp(), TASK, detect_project_profile(TASK), max_rounds=5)
    w.phase = 4
    w.process("nothing")
    assert w.max_tokens_hint == 800


def test_resampling_moves_on_to_callees_after_the_direct_function(tmp_path):
    w = run10_workspace(tmp_path)
    for _ in range(2):
        w.process("No code this time.")
    assert w.resample["target"] == "ChessGame.make_move"
    w.resample["attempts"] = w.max_resamples  # make_move's candidates are used up...
    w.resample["improved"] = True             # ...and helped, so no test audit is needed
    w.process("No code this time.")
    assert w.resample and w.resample["target"] != "ChessGame.make_move"
    assert "ChessGame.make_move" in w.resample_tried


def test_trace_explains_the_condition_behind_an_early_return(tmp_path):
    w = run10_workspace(tmp_path)
    out = w.behavior_trace("test_chess")
    assert "WHY: `piece[0] == self.current_turn` evaluated as 'w' == 'W' -> False" in out or \
           "WHY: `piece[0] != self.current_turn` evaluated as 'w' != 'W' -> True" in out


def test_resample_prompt_is_compact_and_explained(tmp_path):
    w = run10_workspace(tmp_path)
    prompts = [w.process("No code this time.") for _ in range(2)]
    prompt = prompts[-1]
    assert "FRESH REWRITE" in prompt and "WHY:" in prompt and "Failing tests:" in prompt
    assert "Traceback (most recent call last)" not in prompt


def test_case_only_mismatch_is_named_explicitly(tmp_path):
    w = run10_workspace(tmp_path)
    assert "differ only in letter case, and == is case-sensitive" in w.behavior_trace("test_chess")


def test_fresh_rewrite_only_replaces_its_target(tmp_path):
    w = run10_workspace(tmp_path)
    for _ in range(2):
        w.process("No code this time.")
    assert w.resample["target"] == "ChessGame.make_move"
    before = (tmp_path / "chess_logic.py").read_text()
    reply = ("def make_move(self, from_pos, to_pos):\n    return 'NEW'\n\n"
             "def get_piece(self, row, col):\n    return 'CHANGED'\n")
    w.process(fence("chess_logic.py", reply))
    after = (tmp_path / "chess_logic.py").read_text()
    assert "return 'CHANGED'" not in after
    assert before.split("def get_piece")[1].split("def ")[0] in after


def test_reasoning_models_keep_room_to_think():
    class WF:
        temperature_hint, max_tokens_hint = None, 800

    class Agent:
        llm_config = {"config_list": [{"model": "gpt-oss:20b", "base_url": "http://x/v1", "api_key": "k"}],
                      "temperature": 0.2, "max_tokens": 16384}
        workflow_ref = WF()

        def generate_oai_reply(self, *a):
            return True, self.llm_config["max_tokens"]

    assert make_resilient_reply(lambda: "http://x")(Agent(), [], None, None) == (True, 4096)


def test_native_tool_call_becomes_a_shell_block():
    from node_core.agents import tool_calls_to_blocks
    reply = {"content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {
        "name": "container_exec", "arguments": '{"cmd":["bash","-lc","sed -n \'1,200p\' chess_logic.py"]}'}}]}
    assert tool_calls_to_blocks(reply) == "```bash\nsed -n '1,200p' chess_logic.py\n```"
    code_call = {"content": "", "tool_calls": [{"function": {"name": "python", "arguments": '{"code": "print(1)"}'}}]}
    assert "print(1)" in tool_calls_to_blocks(code_call)
    assert tool_calls_to_blocks("plain text") == "plain text"


RUN13 = Path(__file__).with_name("fixtures") / "run13"


def run13_workspace(tmp_path):
    """Run 13's real, correct logic with a wrong model-written test (2 queens per side)."""
    import shutil
    for name in ("chess_logic.py", "test_chess.py"):
        shutil.copy(RUN13 / (name + ".fixture"), tmp_path / name)
    (tmp_path / "chess_gui.py").write_text("class ChessGUI:\n    pass\n")
    (tmp_path / "main.py").write_text("import chess_logic, chess_gui\n")
    w = TaskWorkflow(tmp_path, TASK, detect_project_profile(TASK), max_rounds=60)
    w.phase = 4
    return w


def test_tried_functions_are_remembered_across_rounds(tmp_path):
    w = run10_workspace(tmp_path)
    for _ in range(2):
        w.process("No code this time.")
    first = w.resample["target"]
    w.resample["attempts"], w.resample["improved"] = w.max_resamples, True
    w.process("No code this time.")
    w._update_resample([])          # a round without assertion failures...
    assert first in w.resample_tried  # ...does not forget what was already tried


def test_unhelpful_rewrites_lead_to_a_test_audit_and_test_fix(tmp_path):
    w = run13_workspace(tmp_path)
    for _ in range(2):
        w.process("No code this time.")
    assert w.resample
    w.resample["attempts"] = w.max_resamples  # rewrites used up without improvement
    prompt = w.process("No code this time.")
    assert "TEST AUDIT" in prompt and "test_initial_layout" in prompt
    prompt = w.process("test_initial_layout: TEST_WRONG - chess starts with one queen per side\n")
    assert "rewrite ONLY those test methods" in prompt
    fixed = ("class TestChessLogic(unittest.TestCase):\n"
             "    def test_initial_layout(self):\n"
             "        counts = {}\n"
             "        for r in range(8):\n"
             "            for c in range(8):\n"
             "                p = self.game.get_piece(r, c)\n"
             "                if p:\n"
             "                    counts[p] = counts.get(p, 0) + 1\n"
             "        for p, n in counts.items():\n"
             "            self.assertEqual(n, 8 if p.endswith('P') else 1 if p[-1] in 'QK' else 2)\n")
    w.process(fence("test_chess.py", fixed))
    tests = (tmp_path / "test_chess.py").read_text()
    assert "1 if p[-1] in 'QK'" in tests and "def test_knight_leap_over_pawn" in tests


def test_test_fix_that_removes_tests_is_rejected(tmp_path):
    w = run13_workspace(tmp_path)
    w.test_audit = {"stage": "fix", "tests": ["test_initial_layout"], "wrong": ["test_initial_layout"]}
    before = (tmp_path / "test_chess.py").read_text()
    reply = w.process(fence("test_chess.py", "class TestChessLogic(unittest.TestCase):\n"
                                             "    @unittest.skip('x')\n    def test_initial_layout(self):\n        pass\n"))
    assert "Test fix rejected" in reply and (tmp_path / "test_chess.py").read_text() == before
