"""Regressions from the first live Step 4 control run."""
from node_core.agents import extract_code_blocks_with_metadata, detect_project_profile
from node_core.workflow import TaskWorkflow


def test_four_backtick_document_preserves_shell_example():
    text = "````markdown\n# filename: README.md\nExample:\n```bash\npython main.py <number>\n```\nDone.\n````"
    blocks = extract_code_blocks_with_metadata(text)
    assert len(blocks) == 1
    assert blocks[0]["filename"] == "README.md"
    assert "```bash\npython main.py <number>\n```" in blocks[0]["code"]
    assert blocks[0].get("complete") is not False


def test_truncated_document_never_executes_embedded_command(tmp_path):
    text = "```markdown\n# README.md\n```shell\necho unsafe > marker.txt\n```\nUnfinished"
    w = TaskWorkflow(tmp_path, "CLI app", detect_project_profile("CLI app"))
    reply = w.process(text)
    assert "CONTINUATION REQUIRED" in reply
    assert not (tmp_path / "marker.txt").exists()
    assert not (tmp_path / "README.md").exists()
    assert not w.commands


def test_equal_width_document_fences_are_not_top_level_commands():
    text = "```markdown\n# README.md\n```shell\necho example\n```\nDone.\n```\n```python\n# filename: main.py\nprint('real')\n```"
    blocks = extract_code_blocks_with_metadata(text)
    assert [b["filename"] for b in blocks] == ["README.md", "main.py"]
    assert "echo example" in blocks[0]["code"]


def test_document_failure_does_not_hide_generated_test_failure(tmp_path):
    w = TaskWorkflow(tmp_path, "CLI app", detect_project_profile("CLI app"))
    for name, code in {"app_logic.py": "def add(a,b): return a-b", "app_gui.py": "from app_logic import add", "main.py": "from app_gui import add", "test_app.py": "import unittest\nfrom app_logic import add\nclass T(unittest.TestCase):\n def test_add(self): self.assertEqual(add(2,3),5)"}.items():
        (tmp_path / name).write_text(code)
    w.phase = 4
    reply = w.process("```markdown\n# README.md\n```shell\necho example\n```\nUnfinished")
    assert "CONTINUATION REQUIRED" in reply and "AssertionError" in reply
    assert w.phase == 4 and w.status != "COMPLETED"
    assert any(c["kind"] == "tests" for c in w.commands)
