"""Evidence and bounded phase control for the existing two-agent coding runner.

Acceptance checks are supplied by the caller (or loaded before generation), never
inferred from the coder's completion message. Structural checks alone are not a
claim that an arbitrary natural-language requirement has been implemented.
"""
from __future__ import annotations

import hashlib
import ast
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

from node_core.tools import NodeForge, NodeInsight, NodeLog, NodePulse
from node_core import incremental as inc
from node_core import conventions

STATE_FILE = "state.json"  # resumable workflow state inside .cloudcode/
PY_COMMANDS = {"read", "bash", "sh", "shell", "cmd", "powershell"}


def command_output(result):
    if not isinstance(result, dict):
        return str(result)
    return "\n".join(str(result[k]) for k in ("output", "stdout", "stderr", "error", "error_message")
                     if result.get(k))


def python_command(*args):
    parts = [sys.executable, *args]
    return subprocess.list2cmdline(parts) if sys.platform == "win32" else shlex.join(parts)


class TaskWorkflow:
    def __init__(self, workspace, task, profile, max_rounds=30, acceptance=None,
                 stall_limit=4, should_stop=None, incremental=False, resume=False):
        if isinstance(max_rounds, bool) or not isinstance(max_rounds, int) or max_rounds < 1:
            raise ValueError("max_rounds must be a positive integer")
        if stall_limit < 1:
            raise ValueError("stall_limit must be positive")
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.task = task
        self.profile = profile
        self.max_rounds = max_rounds
        self.stall_limit = stall_limit
        self.should_stop = should_stop or (lambda: False)
        self.phase = 1
        self.completed_phases = []
        self.rounds = 0
        self.status = "IN_PROGRESS"
        self.reason = "Execution has not finished"
        self.contract = ""
        self.files_created = set()
        self.files_modified = set()
        self.files_read = set()
        self.read_evidence = set()
        self.responses_received = 0
        self._last_feedback = None
        self.commands = []
        self.verification = []
        self.remaining = []
        self.no_progress = 0
        self.seen_states = {}
        # FIX 4: Track error history for adaptive feedback
        self.error_history = []  # list of sorted error tuples per round
        self.consecutive_identical_errors = 0
        # FIX 5: Track whether stall recovery has been attempted
        self._recovery_attempted = False
        self.recovery_evidence = []
        # Output-limit continuation: endpoints that cap each reply (and forget
        # earlier turns) receive the saved prefix and return only the rest.
        self.partial = None  # {"name": str, "lines": [str], "parts": int}
        self.max_continuations = 8
        # Method-at-a-time building for endpoints with short replies.
        self.incremental = bool(incremental)
        self.draft = None  # {"name", "code" (None until skeleton), "attempts", "steps", "note"}
        self.incremental_disabled = set()
        self.resumed = False
        # Sampling temperature the coder should use; raised when replies repeat.
        self.temperature_hint = None
        self.max_tokens_hint = None  # reply budget; small during repairs
        # Regression gate: best project version seen during repair, by passing tests.
        self.best = None  # {"passes": int, "files": {path: text}}
        self.reverts = 0
        # Fresh resampling of a function whose repairs keep failing.
        self.resample = None  # {"target": qualname, "file": path, "attempts": int, "improved": bool}
        self.max_resamples = 4
        self.assertion_rounds = 0  # consecutive repair rounds still failing on assertions
        # Functions whose fresh rewrites are used up; kept across rounds, cleared on improvement.
        self.resample_tried = []
        # Test audit: are the failing tests themselves wrong? {"stage": "ask"|"fix", "tests": [...]}
        self.test_audit = None
        self.audited = set()  # test names already judged, so an audit is never repeated
        self.convention_counts = {}  # issue -> rounds reported; capped so a false alarm cannot stall
        # Snapshot caller-owned criteria before the model can change the workspace.
        if acceptance is None:
            path = self.workspace / ".cloudcode" / "acceptance.json"
            acceptance = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.acceptance = json.loads(json.dumps(acceptance))
        if not isinstance(self.acceptance, dict):
            raise ValueError("Acceptance specification must be an object")
        if "task" in self.acceptance and self.acceptance["task"] != task:
            raise ValueError("Acceptance specification belongs to a different task")
        self.single_file = "hello.py" in task.lower() and "sanity check" in task.lower()
        if self.single_file:
            self.profile["logic"] = "hello.py"
        self.checks = self.acceptance.get("checks", [])
        self.required_files = self.acceptance.get("required_files", [])
        if not isinstance(self.checks, list) or not isinstance(self.required_files, list):
            raise ValueError("Acceptance checks and required_files must be lists")
        for check in self.checks:
            if not isinstance(check, dict) or not all(isinstance(check.get(k), str) and check[k].strip()
                                                     for k in ("requirement", "command")):
                raise ValueError("Each acceptance check needs a requirement and command")
        for name in self.required_files:
            self.path(name)
        protected = self.acceptance.get("protected_files", [])
        if not isinstance(protected, list):
            raise ValueError("protected_files must be a list")
        self.protected_files = {name: hashlib.sha256(self.path(name).read_bytes()).hexdigest() for name in protected}
        criteria_dir = self.workspace / ".cloudcode"
        if criteria_dir.exists():
            for path in criteria_dir.iterdir():
                if path.is_file() and path.name != STATE_FILE:
                    self.protected_files[path.relative_to(self.workspace).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        if resume:
            self._load_state()

    # ------------------------------------------------------------------ state
    def _state_path(self):
        return self.workspace / ".cloudcode" / STATE_FILE

    def _save_state(self):
        state = {"task": self.task, "phase": self.phase, "completed_phases": self.completed_phases,
                 "contract": self.contract, "partial": self.partial, "draft": self.draft,
                 "incremental": self.incremental, "incremental_disabled": sorted(self.incremental_disabled),
                 "best": self.best, "resample": self.resample, "reverts": self.reverts,
                 "resample_tried": self.resample_tried, "test_audit": self.test_audit,
                 "audited": sorted(self.audited), "convention_counts": self.convention_counts,
                 "files_created": sorted(self.files_created), "files_modified": sorted(self.files_modified)}
        try:
            path = self._state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(state, indent=1), encoding="utf-8")
        except OSError:
            pass

    def _load_state(self):
        path = self._state_path()
        if not path.is_file():
            return
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("task") != self.task:
            raise ValueError("Saved workflow state belongs to a different task; start a fresh run")
        self.phase = int(state.get("phase", 1))
        self.completed_phases = list(state.get("completed_phases", []))
        self.contract = state.get("contract", "")
        self.partial = state.get("partial")
        if (self.partial and state.get("incremental") and self.partial["name"].endswith(".py")
                and self.path(self.partial["name"]).is_file()):
            self.partial = None  # superseded by function-level patching
        self.draft = state.get("draft")
        self.incremental = self.incremental or bool(state.get("incremental"))
        self.incremental_disabled = set(state.get("incremental_disabled", []))
        self.best = state.get("best")
        self.resample = state.get("resample")
        self.reverts = int(state.get("reverts", 0))
        self.resample_tried = list(state.get("resample_tried", []))
        self.test_audit = state.get("test_audit")
        self.audited = set(state.get("audited", []))
        self.convention_counts = dict(state.get("convention_counts", {}))
        self.files_created = set(state.get("files_created", []))
        self.files_modified = set(state.get("files_modified", []))
        self.resumed = True
        NodeLog.emit("WORKFLOW_RESUMED", {"phase": self.phase, "partial": bool(self.partial),
                                           "draft": (self.draft or {}).get("name")})

    def initial_prompt(self):
        """First prompt when the default phased prompt does not apply, else None."""
        if self.resumed:
            errors = self.verify() if self.phase >= 4 else []
            return self.next_prompt(errors, [f"Resumed saved run at phase {self.phase}."])
        if self.incremental and not self.single_file:
            self._maybe_start_draft()
            if self.draft:
                return self.draft_prompt([])
        return None

    def criteria_errors(self):
        errors = []
        for name, digest in self.protected_files.items():
            try:
                unchanged = hashlib.sha256(self.path(name).read_bytes()).hexdigest() == digest
            except OSError:
                unchanged = False
            if not unchanged:
                errors.append(f"Caller-owned acceptance file changed: {name}")
        return errors

    def path(self, name):
        if not isinstance(name, str) or not name:
            raise ValueError("File path must be a nonempty string")
        path = (self.workspace / name).resolve()
        if not path.is_relative_to(self.workspace):
            raise ValueError(f"Path outside workspace: {name}")
        return path

    def snapshot(self):
        files = {}
        for path in self.workspace.rglob("*"):
            rel = path.relative_to(self.workspace)
            if any(p.startswith(".") or p in {"__pycache__", "node_modules", "nodelog_data", "venv"}
                   for p in rel.parts) or path.suffix == ".bak":
                continue
            if path.is_file() and path.resolve().is_relative_to(self.workspace):
                files[rel.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        return files

    def context(self):
        scan = NodeInsight.scan_workspace(str(self.workspace))
        files = scan.get("files", []) if scan.get("status") == "success" else []
        result = [f"Workspace: {self.workspace}", f"Inventory: {files[:200]}",
                  f"Verification Python: {sys.executable} ({sys.version.split()[0]}). "
                  f"Use {python_command('-m', 'pip')} for package operations. "
                  "Shell python resolves to this same environment. Inspect installed APIs with "
                  "short Python commands (help, dir, inspect) when uncertain; do not guess symbols."]
        if scan.get("status") != "success":
            result.append("Workspace scan failed: " + str(scan.get("error", "Unknown reader error")))
        budget = 16000
        for name in files:
            if budget <= 0:
                break
            if Path(name).suffix.lower() not in {".py", ".md", ".txt", ".json", ".toml", ".js", ".ts"}:
                continue
            if any(p.startswith(".") or p in {"node_modules", "nodelog_data", "__pycache__"}
                   for p in Path(name).parts):
                continue
            res = NodeInsight.read_file(name, workspace_path=str(self.workspace))
            if res.get("status") == "success":
                content = res.get("content", "")[:min(budget, 4000)]
                result.append(f"FILE {name} (possibly truncated):\n{content}")
                self.files_read.add(name)
                budget -= len(content)
        result.append("To inspect any file, emit a ```read block containing one relative path per line, "
                      "or path:START-END for an inclusive line range. "
                      "Shell blocks run in this workspace; commands must finish without GUI mainloops.")
        result.append("Acceptance requirements: " + json.dumps(self.acceptance))
        if not self.checks:
            result.append("No caller-provided acceptance checks are configured. Implement and test the project; "
                          "final status will remain VERIFICATION_FAILED until task-specific checks are supplied.")
        return "\n\n".join(result)

    def detect_incomplete_artifact(self, code, name):
        """FIX 2: Detect structurally incomplete artifacts beyond fence-level checks."""
        issues = []
        if not code or not code.strip():
            return issues
        # The Python parser understands inline suites, quoted punctuation and
        # triple quotes inside comments/strings. Text heuristics rejected valid
        # repairs such as `def add(a,b): return a+b` in four existing regressions.
        if name.lower().endswith('.py'):
            try:
                compile(code, name, 'exec')
            except SyntaxError as exc:
                issues.append(f"Incomplete or invalid Python artifact {name}: {exc}")
            return issues
        # Unclosed triple-quoted strings
        if code.count('"""') % 2 != 0:
            issues.append(f"Unclosed triple-quote string in {name}")
        if code.count("'''") % 2 != 0:
            issues.append(f"Unclosed triple-quote string in {name}")
        # Truncated at bracket/brace/paren/comma
        stripped = code.rstrip()
        if stripped and stripped[-1] in ('{', '[', '(', ','):
            issues.append(f"Artifact {name} appears truncated (ends with '{stripped[-1]}')")
        # Python file ending with incomplete def/class header
        if name.endswith('.py'):
            lines = code.splitlines()
            if lines:
                last_nonblank = ''
                for line in reversed(lines):
                    if line.strip():
                        last_nonblank = line.strip()
                        break
                if re.match(r'(def|class)\s+\w+', last_nonblank) and not last_nonblank.endswith(':'):
                    issues.append(f"Artifact {name} ends with an incomplete definition")
        return issues

    def dependency_preflight(self):
        """FIX 7: Check whether declared dependencies (requirements.txt) are importable.

        Returns a list of human-readable error strings.  Does NOT install packages.
        This gives the model actionable feedback distinguishing 'library missing'
        from 'wrong import name'.
        """
        req_path = self.workspace / "requirements.txt"
        if not req_path.is_file():
            return []  # No requirements declared yet — nothing to check.
        errors = []
        try:
            text = req_path.read_text(encoding="utf-8")
        except OSError:
            return []
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            # Extract package name (strip version specifiers)
            pkg = re.split(r"[<>=!~;\[\]]", line, 1)[0].strip()
            if not pkg:
                continue
            # Normalise: pip uses dashes but Python import uses underscores
            import_name = pkg.replace("-", "_").lower()
            # Some well-known package→import mappings
            known_mappings = {
                "python_chess": "chess",
                "pillow": "PIL",
                "scikit_learn": "sklearn",
                "beautifulsoup4": "bs4",
                "pyyaml": "yaml",
                "opencv_python": "cv2",
            }
            import_name = known_mappings.get(import_name, import_name)
            probe = self.execute(
                python_command("-c", f"import {import_name}; print('{import_name} OK')"),
                "dependency_check"
            )
            if not self.passed(probe):
                output = command_output(probe)
                if "ModuleNotFoundError" in output or "No module named" in output:
                    errors.append(
                        f"Dependency '{pkg}' (import as '{import_name}') is declared in "
                        f"requirements.txt but NOT installed in this environment. "
                        f"Install it with: {python_command('-m', 'pip', 'install', pkg)} "
                        f"Preserve dependencies explicitly required by the original task."
                    )
                else:
                    errors.append(
                        f"Dependency '{pkg}' probe failed: {output[:300]}"
                    )
        return errors

    @staticmethod
    def classify_import_error(output):
        """FIX 8: Classify an import failure to give targeted feedback.

        Returns (category, detail) where category is one of:
          'missing_package'  — third-party library not installed
          'bad_import_name'  — module exists but imported name is wrong
          'syntax_error'     — file has a syntax error
          'other'            — unclassified
        """
        if "ModuleNotFoundError" in output or "No module named" in output:
            # Extract the module name
            m = re.search(r"No module named ['\"]([^'\"]+)['\"]", output)
            mod = m.group(1) if m else "unknown"
            # Standard-library modules that should always be available
            stdlib = {"sys", "os", "json", "re", "pathlib", "http", "urllib",
                      "unittest", "argparse", "hashlib", "io", "collections",
                      "functools", "itertools", "math", "datetime", "threading",
                      "subprocess", "tempfile", "shutil", "typing", "abc",
                      "dataclasses", "enum", "socket", "html", "xml"}
            top = mod.split(".")[0]
            if top in stdlib:
                return "bad_import_name", mod
            return "missing_package", mod
        if "ImportError: cannot import name" in output:
            m = re.search(r"cannot import name ['\"]([^'\"]+)['\"]", output)
            name = m.group(1) if m else "unknown"
            return "bad_import_name", name
        if "SyntaxError" in output:
            return "syntax_error", output[:300]
        return "other", output[:300]

    def execute(self, command, kind="command", requirement=None):
        if self.should_stop():
            self.stop("CANCELLED", "User requested cancellation")
            return {"status": "aborted", "exit_code": -1, "output": "Cancelled"}
        # A same-size repair within one timestamp tick must not load stale .pyc.
        with tempfile.TemporaryDirectory(prefix="cloudcode-bytecode-") as cache:
            result = NodePulse.execute_command(command, cwd=str(self.workspace), should_stop=self.should_stop,
                                              env={"PYTHONPYCACHEPREFIX": cache, "PYTHONDONTWRITEBYTECODE": "1",
                                                   "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")})
        if not isinstance(result, dict):
            result = {"status": "error", "exit_code": -1, "output": str(result)}
        record = {"command": command, "kind": kind, "requirement": requirement,
                  "round": self.rounds, "exit_code": result.get("exit_code"),
                  "status": result.get("status"), "output": command_output(result)}
        self.commands.append(record)
        if result.get("status") == "cancelled":
            self.stop("CANCELLED", "User requested cancellation")
        NodeLog.emit("COMMAND_RUN", record)
        return result

    @staticmethod
    def passed(result):
        return result.get("exit_code") == 0 and result.get("status") == "success"

    def file_errors(self, names):
        errors = []
        for name in dict.fromkeys(names):
            try:
                path = self.path(name)
                data = path.read_bytes()
                if not data:
                    raise ValueError("empty file")
                if path.suffix.lower() in {".py", ".txt", ".md", ".json", ".toml", ".js", ".ts", ".html", ".css"} and not data.decode("utf-8").strip():
                    raise ValueError("empty text file")
                if path.suffix.lower() == ".py":
                    text = data.decode("utf-8")
                    compile(text, name, "exec")
                    if name == self.profile["logic"] and not self.single_file:
                        tree = ast.parse(text)
                        if not any(isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) for n in tree.body):
                            raise ValueError("core logic has no callable API; provide the actual implementation, not a filename or placeholder")
            except (OSError, ValueError, SyntaxError) as exc:
                errors.append(f"{name}: {exc}")
        return errors

    def behavior_trace(self, module):
        """Concrete call/return evidence for failing tests; empty when unavailable."""
        try:
            result = subprocess.run([sys.executable, str(Path(__file__).with_name("behavior_trace.py")),
                                     str(self.workspace), module], cwd=self.workspace, capture_output=True,
                                    text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return result.stdout.strip()[:4000] if result.returncode == 0 else ""

    def verify(self, final=False):
        """FIX 3: Partial verification — continue checking even when some files are missing.

        Previously, if any file was missing the method returned immediately
        with only the file-level errors, never running tests or import checks.
        Now all discoverable problems are surfaced in a single pass.
        Missing files are still reported as errors — acceptance criteria are unchanged.
        """
        before = self.snapshot()
        names = [self.profile[k] for k in ("logic", "gui", "main", "tests")]
        if final:
            names += [self.profile["guide"], *self.required_files]
        file_errs = self.file_errors(names) + self.criteria_errors()
        errors = list(file_errs)

        # FIX 3: Determine which files exist so we can still check them
        existing_names = [n for n in names if self.path(n).is_file()]
        existing_py = [n for n in existing_names if n.endswith('.py')]

        # Run test suite if the test file exists (even if other files are missing)
        test_file = self.profile.get("tests", "")
        if test_file and self.path(test_file).is_file():
            with tempfile.TemporaryDirectory(prefix="cloudcode-check-") as evidence_dir:
                evidence = Path(evidence_dir) / "tests.json"
                command_index = len(self.commands)
                test = self.execute(python_command("-I", str(Path(__file__).with_name("verification_runner.py")),
                                    str(self.workspace), str(evidence)), "tests")
                try:
                    measured = json.loads(evidence.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    measured = {}
                if (not self.passed(test) or measured.get("tests_run", 0) < 1
                        or not measured.get("successful") or measured.get("skipped", 0)):
                    errors.append("Generated test suite must run at least one test, with no failures or skips:\n" + command_output(test))
                    observed = self.behavior_trace(Path(test_file).stem)
                    if observed:
                        errors.append("Observed behaviour in failing tests (calls into project code -> actual "
                                      "return values; compare with what each test expects):\n" + observed)
                if len(self.commands) > command_index:
                    self.commands[command_index]["test_result"] = measured
        elif not file_errs or test_file not in [e.split(":")[0] for e in file_errs]:
            # Test file missing but not yet reported via file_errs
            if test_file and not self.path(test_file).is_file():
                errors.append(f"{test_file}: file is missing; test suite cannot run")

        # Values spelled differently from the task (e.g. 'w' where the task says 'W')
        # slip past the model's own tests, which share the same invented spelling.
        if self.phase >= 4 and existing_py:
            sources = {}
            for name in existing_py:
                try:
                    sources[name] = self.path(name).read_text(encoding="utf-8")
                except OSError:
                    pass
            for issue in conventions.check(self.task, sources):
                count = self.convention_counts.get(issue, 0) + 1
                self.convention_counts[issue] = count
                if count <= 3:
                    errors.append("Naming convention: " + issue)

        # Stubs left by a partially saved draft must be implemented.
        for name in existing_py:
            try:
                remaining = inc.find_stubs(self.path(name).read_text(encoding="utf-8"))
            except (OSError, SyntaxError, ValueError):
                continue
            if remaining and self.incremental:
                errors.append(f"{name}: these functions still raise NotImplementedError and must be implemented: "
                              + ", ".join(remaining))

        # Calls to methods a class never defines fail only at runtime; report them.
        for name in existing_py:
            try:
                missing = inc.undefined_self_calls(self.path(name).read_text(encoding="utf-8"))
            except (OSError, SyntaxError, ValueError):
                continue
            for cls, method, caller, call in missing:
                errors.append(f"{name}:{call.lineno}: {cls}.{caller} calls self.{method}(), which {cls} does not "
                              f"define. Implement {method} or call an existing method.")

        # FIX 7: Run dependency preflight before import checks
        dep_errors = self.dependency_preflight()
        if dep_errors:
            errors.extend(dep_errors)

        # FIX 3: Import check against existing modules even when some files are missing
        importable = [Path(self.profile[k]).stem for k in ("logic", "gui", "main")
                      if self.path(self.profile[k]).is_file()]
        if importable:
            smoke = self.execute(python_command("-c", "import importlib; " +
                                 "; ".join(f"importlib.import_module({m!r})" for m in importable)), "imports")
            if not self.passed(smoke):
                output = command_output(smoke)
                # FIX 8: Classify the import error for better feedback
                category, detail = self.classify_import_error(output)
                if category == "missing_package":
                    errors.append(
                        f"Import failed because package '{detail}' is not installed. "
                        f"Inspect whether it is a missing local module, nested import, or dependency "
                        f"in verification Python {sys.executable}. Install required dependencies "
                        f"using that interpreter; do not remove a task-required library.\n{output}"
                    )
                elif category == "bad_import_name":
                    errors.append(
                        f"Import failed: '{detail}' cannot be imported. "
                        f"The module may exist but you are importing a name that does not. "
                        f"Check the library's actual API documentation.\n{output}"
                    )
                else:
                    errors.append("Application import integration failed:\n" + output)

        if final:
            if not self.checks:
                errors.append("Task-specific acceptance checks are not configured; requirement coverage is unverified.")
            for check in self.checks:
                result = self.execute(check["command"], "acceptance", check["requirement"])
                if not self.passed(result):
                    errors.append(check["requirement"] + ":\n" + command_output(result))
        after = self.snapshot()
        if after != before:
            errors.append("Project files changed during verification; repair the checks and verify the final files again.")
        errors.extend(self.file_errors(names))
        errors.extend(self.criteria_errors())
        self.verification.append({"round": self.rounds, "final": final, "passed": not errors,
                                  "errors": list(dict.fromkeys(errors)), "files_before": before, "files": after})
        return list(dict.fromkeys(errors))

    def consume_messages(self, messages):
        """Consume each transport response once, including the final bounded turn."""
        responses = [m for m in messages if m.get("role") == "user"]
        while self.responses_received < len(responses):
            message = responses[self.responses_received]
            self.responses_received += 1
            self._last_feedback = self.process(message.get("content", ""))
        return self._last_feedback

    def transition(self, phase):
        self.completed_phases = sorted(set(self.completed_phases + [self.phase]))
        NodeLog.emit("PHASE_TRANSITION", {"from_phase": self.phase, "to_phase": phase})
        self.phase = phase

    def stop(self, status, reason):
        self.status, self.reason = status, reason

    def directive(self):
        if self.single_file:
            return "Implement the requested hello.py and repair the reported runtime or acceptance failure."
        p = self.profile
        return {
            1: f"PHASE 1: Implement complete core logic in {p['logic']}.",
            2: "PHASE 2: Repair the core logic so its API contract can be extracted.",
            3: f"PHASE 3: Implement {p['gui']} using this API contract:\n{self.contract}",
            4: f"PHASE 4: Implement {p['main']} and {p['tests']}, one complete file per response. "
               "Write concise meaningful unittest cases using loops/subTest for related cases; "
               "do not enumerate repetitive assertions or invent unchecked fixtures. "
               "Fix import/runtime blockers before adding tests. "
               "Run all tests and resolve failures. Guard application launch with __name__ == '__main__'.",
            5: f"PHASE 5: Write {p['guide']} with dependencies, launch instructions and controls. "
               "Repair remaining requirements and request final verification using TASK_COMPLETE.",
            6: "Verified completion.",
        }[self.phase]

    def repair_checkpoint(self, errors):
        """Give the next response one concrete target and observed source context.

        Keep this self-contained for endpoints with weak conversation retention.
        Reader evidence is recorded but automatic reads do not reset stall counts.
        """
        existing = self.snapshot()
        blockers = [e for e in errors if any(s in e.lower() for s in
                    ('import failed', 'modulenotfounderror', 'cannot import name', 'not installed'))]
        focus = None
        for error in blockers:
            paths = re.findall(r'File "([^"]+\.py)"', error)
            for path in reversed(paths):
                candidate = Path(path)
                if candidate.is_absolute() and candidate.is_relative_to(self.workspace):
                    focus = candidate.relative_to(self.workspace).as_posix()
                    break
            if focus:
                break
        logic_bug = False
        convention = [e for e in errors if e.startswith("Naming convention: ")]
        if focus is None and convention and not blockers:
            focus = convention[0][len("Naming convention: "):].split(":", 1)[0]
        if focus is None:
            keys = {1: ['logic'], 2: ['logic'], 3: ['gui'], 4: ['main', 'tests'], 5: ['guide']}[self.phase]
            needed = [self.profile[k] for k in keys] + list(self.required_files)
            focus = next((name for name in needed if not self.path(name).is_file()), None)
            # Assertion failures in generated tests usually mean the logic is
            # wrong; steering the model to the test file invites weakened tests.
            logic_bug = (focus is None and self.phase >= 4 and self.path(self.profile['logic']).is_file()
                         and any(e.startswith("Generated test suite") and "AssertionError" in e for e in errors))
            if logic_bug:
                focus = self.profile['logic']
            elif focus is None:
                focus = self.profile[keys[-1]]
        context = []
        shown = [focus, self.profile['logic']] + ([self.profile['tests']] if logic_bug else [])
        for name in dict.fromkeys(shown):
            if name not in existing:
                continue
            result = NodeInsight.read_file(name, workspace_path=str(self.workspace))
            if result.get('status') == 'success':
                value = result.get('content', '')
                self.files_read.add(name)
                limit = 14000 if name == focus else 7000
                # Repairs: models copy whatever file they are shown. Show the outline
                # plus full source only of the functions the failures involve.
                if name.endswith('.py') and (self.incremental and len(value) > 6000
                                             or self.phase >= 4 and logic_bug and len(value) > 3000):
                    # Short-reply endpoints are slow on long prompts: send the
                    # outline plus full source of functions the errors mention.
                    try:
                        mentioned = inc.relevant_sources(value, "\n".join(errors))
                        context.append(f'CURRENT FILE {name} (outline; implemented bodies shown as ...):\n'
                                       + inc.outline(value) +
                                       ('\nFULL SOURCE OF FUNCTIONS INVOLVED IN THE FAILURES:\n' + mentioned
                                        if mentioned else ''))
                        continue
                    except SyntaxError:
                        pass
                context.append(f'CURRENT FILE {name}:\n' + value[:limit] +
                               ('\n[Excerpt only; use a read block for the remaining lines.]' if len(value) > limit else ''))
        if blockers:
            next_action = f'NEXT RESPONSE: inspect/fix the blocking import in {focus}. '
        elif convention:
            files = sorted({e[len("Naming convention: "):].split(":", 1)[0] for e in convention})
            next_action = ("NEXT RESPONSE: the code or tests spell task-defined values differently from the task "
                           "(see the 'Naming convention' lines). Change every listed place to the task's spelling, "
                           "keeping behaviour otherwise the same, in: " + ", ".join(files) + ". Output ONLY the "
                           "corrected function/method definitions, one ```python block per file whose first line is "
                           "'# filename: <file>'. ")
        elif logic_bug:
            deliver = ("output ONLY the corrected function/method definitions in one python block whose first "
                       f"line is '# filename: {focus}'; the runner splices them into the file")
            next_action = (f"NEXT RESPONSE: the unit tests fail because {focus} behaves incorrectly (see the failing "
                           "assertions, the observed calls and return values, and the test source above). Find the "
                           f"bug in {focus} by comparing it with the original task requirements, then {deliver}. "
                           "Do not weaken, skip or delete tests; change a test only if it contradicts the original "
                           "requirements. ")
        else:
            next_action = f'NEXT RESPONSE: produce one concise complete {focus}. '

        next_action += ('Use an explicitly named file block with a closing fence. Do not repeat other files. '
                        'Read/shell inspection is allowed before repair. Keep all original requirements. '
                        'Use loops for related test cases, never enumerate hundreds of assertions. '
                        'If a file cannot fit, factor cohesive helpers into additional complete files; '
                        'do not omit required features or required public entrypoints.')
        return ('Workspace files: ' + ', '.join(sorted(existing)) + '\n' + '\n\n'.join(context), next_action)

    DEF_LINE = re.compile(r"\s*(?:async\s+def|def|class)\s+\w+")

    @classmethod
    def merge_continuation(cls, prefix, addition):
        """Join a saved truncated prefix with a continuation, removing overlap.

        Returns (lines, restarted). Weak models often restate an earlier
        definition (or the whole file) instead of continuing; a restated
        def/class line re-anchors the reply there instead of duplicating it.
        """
        norm = lambda seq: [line.rstrip() for line in seq]
        p, a = norm(prefix), norm(addition)
        lead = 0
        while lead < len(a) and not a[lead].strip():
            lead += 1
        a, addition = a[lead:], list(addition)[lead:]
        head = [line for line in p if line.strip()][:3]
        if head and [line for line in a if line.strip()][:len(head)] == head:
            return list(addition), True
        defs = [j for j, line in enumerate(p) if cls.DEF_LINE.match(line)]
        for i, line in enumerate(a):
            if cls.DEF_LINE.match(line) and line in p:
                j = p.index(line)
                if defs and j == defs[0]:
                    return list(addition), True  # full restart, keep its imports
                return list(prefix[:j]) + list(addition)[i:], False
        for k in range(min(len(p), len(a)), 0, -1):
            if p[-k:] == a[:k] and (k > 1 or a[0].strip()):
                return list(prefix) + list(addition)[k:], False
        # The reply may restate a few earlier lines before continuing.
        if len(a) >= 2 and a[0].strip():
            for j in range(len(p) - 1, max(len(p) - 80, 0) - 1, -1):
                if p[j:j + 2] == a[:2]:
                    return list(prefix[:j]) + list(addition), False
        return list(prefix) + list(addition), False

    @classmethod
    def trim_to_boundary(cls, lines, floor):
        """Cut an unfinished trailing definition so the next part restarts at
        its def line. Only trims when that still keeps more than `floor` lines."""
        for j in range(len(lines) - 1, -1, -1):
            if cls.DEF_LINE.match(lines[j]) and len(lines[j]) - len(lines[j].lstrip()) <= 4:
                return lines[:j] if j > floor else lines
        return lines

    @staticmethod
    def trim_repetition(text, repeats=3):
        """Return text cut before a degenerate repetition loop, or None if no loop."""
        lines = text.splitlines()
        seen = {}
        for i, line in enumerate(lines):
            key = line.strip()
            if len(key) < 12:
                continue
            seen.setdefault(key, []).append(i)
            if len(seen[key]) >= repeats:
                cut = seen[key][1]
                return "\n".join(lines[:cut]).rstrip() + "\n" if cut >= 5 else None
        return None

    def _partial_size(self):
        return (self.partial["name"], len(self.partial["lines"])) if self.partial else None

    def continuation_prompt(self, errors=()):
        name, lines = self.partial["name"], self.partial["lines"]
        text = "\n".join(lines)
        shown = text if len(text) <= 14000 else "[... earlier lines omitted ...]\n" + text[-14000:]
        fence = "````" if name.lower().endswith((".md", ".txt")) else "```"
        last = next((line for line in reversed(lines) if line.strip()), "")
        others = [e for e in errors if "continuation pending" not in e]
        parts = [
            "Original task requirements (unchanged):\n" + self.task + "\n",
            f"CONTINUATION REQUIRED ({self.partial['parts']} part(s) received so far).",
            f"Your previous reply was truncated by the output length limit while writing {name}. "
            f"Lines 1-{len(lines)} are already saved; they are shown below. The last shown line is complete.",
            f"=== SAVED {name} (lines 1-{len(lines)}) ===\n{shown}\n=== END OF SAVED LINES ===\n",
        ]
        if last:
            parts.append(f"The last saved line is indented {len(last) - len(last.lstrip())} spaces: {last.strip()!r}. "
                         "Your first line must continue from there at the correct indentation.")
        parts.append(
            f"NEXT RESPONSE: continue {name} starting at line {len(lines) + 1}. Output ONLY the remaining lines, "
            f"in one {fence}python block (use {fence}text for non-code) whose first line is '# filename: {name}'. "
            "Do NOT repeat saved lines and do NOT restart the file. Keep the same indentation and style. "
            "Write compact code: share logic through loops/helpers instead of duplicating it per color or piece. "
            "Finish every remaining required method of this file, then close the fence. "
            "If the limit is reached again, stop cleanly at the end of a line; you will be asked to continue.")
        if others:
            parts.append("Other remaining checks (address after finishing this file):\n" + "\n".join(others)[-3000:])
        return "\n".join(parts)

    # ------------------------------------------------------ incremental build
    def _progress_marker(self):
        draft = None
        if self.draft:
            code = self.draft.get("code")
            # Method continuation, split/continue mode and bounded retries are
            # progress; the draft's own limits (defer, abandon, step cap) end loops.
            draft = (self.draft["name"], hashlib.sha256(code.encode()).hexdigest() if code else None,
                     self.draft.get("mode"), len(self.draft.get("method_partial") or []),
                     self.draft.get("attempts"), tuple(self.draft.get("deferred") or []))
        resample = (self.resample["target"], self.resample["attempts"]) if self.resample else None
        audit = (self.test_audit["stage"], tuple(self.test_audit["tests"])) if self.test_audit else None
        return (self._partial_size(), draft, resample, self.reverts, audit, tuple(self.resample_tried))

    def _maybe_start_draft(self):
        if self.draft or self.partial or self.single_file:
            return
        target = {1: "logic", 2: "logic", 3: "gui"}.get(self.phase)
        name = self.profile.get(target) if target else None
        if (name and name.endswith(".py") and name not in self.incremental_disabled
                and not self.path(name).is_file()):
            self.draft = {"name": name, "code": None, "attempts": 0, "steps": 0, "note": ""}
            NodeLog.emit("DRAFT_STARTED", {"file": name})

    @staticmethod
    def _draft_target(d):
        """Next stub to request: source order, with deferred stubs last."""
        stubs = inc.find_stubs(d["code"])
        deferred = [q for q in d.get("deferred", []) if q in stubs]
        return ([q for q in stubs if q not in deferred] + deferred)[0]

    def draft_prompt(self, errors=()):
        d, name = self.draft, self.draft["name"]
        parts = ["Original task requirements (unchanged):\n" + self.task + "\n"]
        if name == self.profile.get("gui") and self.contract:
            parts.append("API CONTRACT of the logic module (call exactly these names):\n" + self.contract + "\n")
        parts.append(f"BUILD MODE: replies from this model endpoint are short, so the runner assembles {name} "
                     "step by step and saves it when every function is implemented.")
        if d.get("note"):
            parts.append("PROBLEM WITH YOUR PREVIOUS REPLY: " + d["note"])
        if d["code"] is None:
            parts.append(
                f"STEP 1 - SKELETON. Output the skeleton of {name} in one ```python block whose first line is "
                f"'# filename: {name}'. Include:\n"
                "- all imports and module-level constants, fully written (tables, colours, glyph maps);\n"
                "- every class, with __init__ fully implemented;\n"
                "- EVERY other method or function the file needs (the public API named in the task plus private "
                "helpers), each with its full signature, a one-line docstring stating exactly what it does and "
                "returns, and the body `raise NotImplementedError`.\n"
                "Design it so that no function needs more than about 40 lines: give each large sub-task its own "
                "helper stub (for example one move-generation helper per piece type, one drawing helper per "
                "screen element). Keep the skeleton under 110 lines. Do not implement the other bodies yet; the "
                "runner requests them one at a time. No prose outside the code block.")
        else:
            stubs = inc.find_stubs(d["code"])
            target = self._draft_target(d)
            done = len(inc.definitions(d["code"])) - len(stubs)
            parts.append(f"=== DRAFT OUTLINE {name} ({done} implemented, {len(stubs)} remaining: "
                         f"{', '.join(stubs)}; implemented bodies are shown as '...') ===\n"
                         f"{inc.outline(d['code'])}=== END OUTLINE ===\n"
                         "Do NOT copy the outline or any other function into your reply.")
            if d.get("mode") == "split":
                parts.append(
                    f"NEXT RESPONSE: `{target}` is too long for one reply. Rewrite it as a SHORT dispatcher of at "
                    "most 20 lines that delegates the work to NEW helper methods, one per case (for example one "
                    "helper per piece type). Output, in one ```python block whose first line is "
                    f"'# filename: {name}': the dispatcher's full definition, followed by each new helper as a "
                    "stub (signature, one-line docstring, body `raise NotImplementedError`). Do not implement the "
                    "helpers yet. No prose outside the code block.")
                return "\n".join(parts)
            if d.get("mode") == "continue":
                saved = d["method_partial"]
                last = next((line for line in reversed(saved) if line.strip()), "")
                parts.append(
                    f"Your definition of `{target}` was cut off by the reply length limit. Its first "
                    f"{len(saved)} lines are saved:\n=== SAVED {target} ===\n" + "\n".join(saved) +
                    f"\n=== END SAVED ===\nThe last saved line is indented {len(last) - len(last.lstrip())} spaces."
                    f"\nNEXT RESPONSE: continue `{target}` from the next line. Output ONLY the remaining lines of "
                    f"this one definition in one ```python block whose first line is '# filename: {name}', then "
                    "close the fence. Do not repeat saved lines. No prose outside the code block.")
                return "\n".join(parts)
            parts.append(
                f"NEXT RESPONSE: implement `{target}` completely. Output ONLY its full definition (def line and "
                f"body) in one ```python block whose first line is '# filename: {name}'. Follow its docstring and "
                "the original requirements, and call the draft's other functions by name. Keep it under about "
                "45 lines: if more is needed, move sub-steps into NEW helper methods and include each new helper "
                "as a stub (signature, docstring, `raise NotImplementedError`) right after it. Do not repeat "
                "other existing functions. No prose outside the code block.")
        return "\n".join(parts)

    def _consume_draft_reply(self, content, blocks, feedback):
        """Apply a skeleton or single-method reply to the draft; return other blocks."""
        d, name = self.draft, self.draft["name"]
        mine = [b for b in blocks if b["lang"] not in PY_COMMANDS and not b.get("routing_error")
                and (b["filename"] == name or b.get("inferred_filename"))]
        rest = [b for b in blocks if b not in mine]
        code, complete = None, True
        if mine:
            code, complete = mine[0]["code"], mine[0].get("complete") is not False
        elif not blocks and content.strip():
            code = content.strip("\n`")
        d["steps"] += 1
        note = info = ""
        if code is None:
            note = f"it contained no python code block for {name}."
        elif d["code"] is None:
            candidate = code if complete else None
            if not complete and d["attempts"] >= 1:
                # Second truncated skeleton: keep the complete definitions.
                candidate = "\n".join(self.trim_to_boundary(code.splitlines()[:-1], 0))
            try:
                if candidate is None:
                    raise SyntaxError("cut off by the output limit")
                compile(candidate, name, "exec")
                if not inc.definitions(candidate):
                    raise ValueError("it defines no functions or methods")
                d["code"], added = inc.add_missing_stubs(candidate.rstrip() + "\n")
                if added:
                    feedback.append(f"Added stubs for undefined helpers: {', '.join(added)}")
                feedback.append(f"Skeleton of {name} accepted with {len(inc.find_stubs(d['code']))} stub(s)")
            except (SyntaxError, ValueError) as exc:
                note = (f"the skeleton was not usable ({exc}). Send a SHORTER skeleton: signatures, one-line "
                        "docstrings and `raise NotImplementedError` bodies only.")
        else:
            target = self._draft_target(d)
            mode = d.get("mode")
            text = code
            if mode == "continue":
                text = "\n".join(self.merge_continuation(d["method_partial"], code.splitlines())[0])
            # Replies often echo the whole draft; keep every complete new
            # implementation they contain, even when the echo was cut off.
            stubs = {q.rsplit(".", 1)[-1]: q for q in inc.find_stubs(d["code"])}
            harvested = dict((stubs[n], src) for n, src in inc.harvest(text, complete) if n in stubs)
            applied = []
            if len(harvested) > 1 or (harvested and not complete):
                for qual, src in harvested.items():
                    try:
                        d["code"], _ = inc.splice(d["code"], src, target=qual)
                        applied.append(qual)
                    except (SyntaxError, ValueError):
                        pass
            try:
                if applied:
                    d["code"], added = inc.add_missing_stubs(d["code"])
                    feedback.append(f"Implemented {', '.join(applied)} from an echoed draft of {name}")
                    d["mode"], d["method_partial"] = None, None
                elif not complete:
                    raise EOFError
                else:
                    before_stubs = set(inc.find_stubs(d["code"]))
                    d["code"], changed = inc.splice(d["code"], text, target=target)
                    d["code"], added = inc.add_missing_stubs(d["code"])
                    if mode == "split":
                        # Helpers created by a split are continued, never split again,
                        # so the number of functions cannot keep growing.
                        born = set(inc.find_stubs(d["code"])) - before_stubs
                        d["split_born"] = sorted(set(d.get("split_born") or []) | born)
                    feedback.append(f"Implemented {', '.join(changed)} in draft {name}" +
                                    (f"; added stubs for undefined helpers {', '.join(added)}" if added else ""))
                    d["mode"], d["method_partial"] = None, None
            except EOFError:
                # Cut off by the reply limit. First ask the model to split the
                # function into helper stubs; if the (short) definition is still
                # cut off, continue it from its saved lines instead.
                saved = text.splitlines()[:-1]
                if mode is None and target in (d.get("split_born") or []) and saved:
                    d["mode"], d["method_partial"] = "continue", saved
                    feedback.append(f"Saved {len(saved)} lines of {target}; requesting the rest")
                elif mode is None:
                    d["mode"] = "split"  # a strategy change, not a failed attempt
                    info = f"`{target}` was cut off by the reply length limit, so it must be split up."
                elif len(saved) > len(d.get("method_partial") or []) and len(saved) < 400:
                    d["mode"], d["method_partial"] = "continue", saved
                    feedback.append(f"Saved {len(saved)} lines of {target}; requesting the rest")
                else:
                    note = f"`{target}` was cut off again without new lines."
            except (SyntaxError, ValueError) as exc:
                note = (f"`{target}` could not be applied ({exc}). Send ONLY that one definition; if it is long, "
                        "make it shorter by moving sub-steps into new helper stubs.")
        if note:
            d["attempts"] += 1
            d["note"] = note
        else:
            d["attempts"], d["note"] = 0, info
        if d["code"] is not None and d["attempts"] >= 2 and self.draft is d:
            # One stubborn function must not discard the whole draft: move it
            # to the back of the queue and fill in the others first.
            stubs = inc.find_stubs(d["code"])
            deferred = [q for q in d.get("deferred", []) if q in stubs]
            current = self._draft_target(d)
            if current not in deferred and len(deferred) + 1 < len(stubs):
                d["deferred"] = deferred + [current]
                d["attempts"], d["mode"], d["method_partial"] = 0, None, None
                d["note"] = ""
                feedback.append(f"Deferred {current}; continuing with the other functions of {name}")
        if d["attempts"] >= 3 or d["steps"] > 80:
            self.incremental_disabled.add(name)
            self.draft = None
            stubs = inc.find_stubs(d["code"]) if d["code"] else []
            total = len(inc.definitions(d["code"])) if d["code"] else 0
            if d["code"] and total and len(stubs) <= total // 2:
                # Keep a mostly finished draft: save it with its remaining stubs,
                # which verification reports and repair patches one by one.
                rest.insert(0, {"code": d["code"], "lang": "python", "filename": name, "raw_code": d["code"],
                                "inferred_filename": False, "routing_error": None, "complete": True})
                feedback.append(f"Saved {name} with {len(stubs)} unimplemented function(s) for repair: "
                                + ", ".join(stubs))
                NodeLog.emit("DRAFT_SAVED_PARTIAL", {"file": name, "stubs": stubs})
            else:
                feedback.append(f"Step-by-step build of {name} abandoned; request the complete file instead")
                NodeLog.emit("DRAFT_ABANDONED", {"file": name, "reason": note})
        elif d["code"] is not None and not inc.find_stubs(d["code"]):
            rest.insert(0, {"code": d["code"], "lang": "python", "filename": name, "raw_code": d["code"],
                            "inferred_filename": False, "routing_error": None, "complete": True})
            self.draft = None
            feedback.append(f"Every function of {name} is implemented; saving the assembled file")
            NodeLog.emit("DRAFT_COMPLETED", {"file": name, "steps": d["steps"]})
        return rest

    def _rewrite_of_existing(self, name, code, block):
        """A whole-file (or outline-copying) reply for an existing Python file."""
        try:
            path = self.path(name)
        except ValueError:
            return False
        if not path.is_file():
            return False
        if "...  # implemented" in code:
            return True
        current = path.read_text(encoding="utf-8")
        return len(code.splitlines()) >= 0.7 * len(current.splitlines()) or block.get("complete") is False

    def _patch_from_rewrite(self, name, code, complete, feedback, errors):
        """Patch only complete, changed functions from a rewrite; never overwrite
        working code with a truncated or placeholder copy. Returns True if handled."""
        path = self.path(name)
        current = path.read_text(encoding="utf-8")
        try:
            existing = {q.rsplit(".", 1)[-1]: q for q in inc.definitions(current)}
            unchanged = {src.strip() for _, src in inc.harvest(current)}
        except SyntaxError:
            return False
        missing = {m for _, m, _, _ in inc.undefined_self_calls(current)}
        candidates = [(n, src) for n, src in inc.harvest(code, complete)
                      if (n in existing or n in missing) and src.strip() not in unchanged]
        if self.resample and self.resample.get("file") == name:
            # A fresh rewrite may replace only its target (plus brand-new helpers);
            # other functions echoed back with changes are not what was asked for.
            target = self.resample["target"].rsplit(".", 1)[-1]
            candidates = [(n, src) for n, src in candidates if n == target or n not in existing]
        applied = []
        for n, src in candidates:
            try:
                current, changed = inc.splice(current, src, replace_implemented=True)
                applied.extend(changed)
            except (SyntaxError, ValueError):
                continue
        if applied:
            res = NodeForge.write_file(name, current, workspace_path=str(self.workspace))
            if not isinstance(res, dict) or res.get("status") != "success":
                errors.append(f"Write failed: {name}: {res}")
                return True
            self.files_modified.add(name)
            feedback.append(f"Patched {', '.join(applied)} in {name} from a whole-file reply")
            NodeLog.emit("FILE_MODIFIED", {"file": name, "patched": applied})
        else:
            errors.append(f"Your reply rewrote {name} without a usable change"
                          + (" (it copied the outline placeholders)" if "# implemented" in code else "")
                          + f". Send ONLY the corrected function definitions for {name}, not the whole file.")
        return True

    # --------------------------------------------------------- repair quality
    def _project_sources(self):
        files = {}
        for rel in self.snapshot():
            if rel.endswith(".py"):
                try:
                    files[rel] = self.path(rel).read_text(encoding="utf-8")
                except OSError:
                    pass
        return files

    def _test_passes(self):
        """Passing test count from this round's trusted test run, or None."""
        for record in reversed(self.commands):
            if record.get("kind") == "tests" and record.get("round") == self.rounds:
                measured = record.get("test_result") or {}
                if "tests_run" not in measured:
                    return None
                return measured["tests_run"] - measured.get("failures", 0) - measured.get("errors", 0)
        return None

    def _gate(self, errors, final=False):
        """Keep the best version seen during repair; revert changes that pass fewer tests."""
        passes = self._test_passes()
        if passes is None or self.single_file:
            return errors
        files = self._project_sources()
        tests = self.profile.get("tests")
        best = self.best
        same_tests = best is not None and best["files"].get(tests) == files.get(tests)
        if best is not None and same_tests and passes < best["passes"] and files != best["files"]:
            for rel, text in best["files"].items():
                if files.get(rel) != text:
                    NodeForge.write_file(rel, text, workspace_path=str(self.workspace))
            for rel in set(files) - set(best["files"]):
                try:
                    self.path(rel).unlink()
                except OSError:
                    pass
            self.reverts += 1
            NodeLog.emit("REGRESSION_REVERTED", {"from": best["passes"], "to": passes})
            note = (f"REVERTED: your last change made the tests worse ({passes} passing, down from "
                    f"{best['passes']}), so the files were restored to the best version. Try a different fix.")
            if self.resample:
                self.resample["last_result"] = f"candidate passed {passes} tests (best is {best['passes']}); discarded"
            restored = self.verify(final=final)
            # The round itself failed; record that honestly after restoring.
            self.verification.append({"round": self.rounds, "final": final, "passed": False,
                                      "errors": [note], "reverted": True, "files": self.snapshot()})
            return [note] + restored
        if best is None or not same_tests or passes >= best["passes"]:
            if best is not None and passes > best["passes"]:
                if self.resample:
                    self.resample["last_result"] = f"candidate improved passing tests to {passes}; kept"
                    self.resample["improved"] = True
                self.resample_tried = []  # new code: earlier suspects may matter again
            self.best = {"passes": passes, "files": files}
        return errors

    def _failing_function(self, errors):
        """Project function the behaviour trace implicates in the first failing test."""
        text = "\n".join(e for e in errors if e.startswith("Observed behaviour"))
        tried = set(self.resample_tried)
        known = {}
        try:
            known = {q.rsplit(".", 1)[-1]: (rel, q) for rel in self._project_sources()
                     if rel != self.profile.get("tests")
                     for q in inc.definitions(self.path(rel).read_text(encoding="utf-8"))}
        except (OSError, SyntaxError):
            pass
        # Best evidence first: the spectrum-based ranking of functions that failing
        # tests run much more than passing ones (see behavior_trace.suspiciousness).
        ranking = re.search(r"^SUSPICIOUS[^:]*: (.+)$", text, re.M)
        if ranking:
            for item in ranking.group(1).split(", "):
                name = item.split("=", 1)[0].rsplit(".", 1)[-1]
                if name in known and known[name][1] not in tried and not name.startswith("__"):
                    return known[name]
        # Direct calls from the failing tests first, then the functions they call.
        # A bug copied into a callee is invisible while its caller returns early.
        calls = re.findall(r"^(\s{4,8})(?:[A-Za-z_]\w*\.)?([A-Za-z_]\w*)\(.*?\) -> ", text, re.M)
        ordered = [n for indent, n in calls if len(indent) == 4] + [n for indent, n in calls if len(indent) > 4]
        for name in ordered:
            if name != "__init__" and name in known and known[name][1] not in tried:
                return known[name]
        # No traced callee left: try functions the implicated one calls by name.
        for rel, qual in (known.get(n) for n in ordered if n in known):
            try:
                body = inc.relevant_sources(self.path(rel).read_text(encoding="utf-8"), qual.rsplit(".", 1)[-1])
            except (OSError, SyntaxError):
                continue
            for callee in re.findall(r"self\.([A-Za-z_]\w*)\(", body):
                if callee in known and known[callee][1] not in tried and callee != "__init__":
                    return known[callee]
        return None

    def _failing_tests(self, errors):
        suite = "\n".join(e for e in errors if e.startswith("Generated test suite"))
        return sorted(set(re.findall(r"^(?:FAIL|ERROR): (\w+)", suite, re.M)))

    def _update_resample(self, errors):
        """Switch from repair to fresh resampling when assertion failures persist,
        and audit the tests once rewriting the code stops helping."""
        logic_fail = self.phase >= 4 and any(e.startswith("Generated test suite") and "AssertionError" in e
                                             for e in errors)
        if not logic_fail:
            if self.resample:
                self.temperature_hint = None  # undo only the resampling temperature
            self.resample = None
            self.test_audit = None
            self.assertion_rounds = 0
            return
        self.assertion_rounds += 1
        if self.test_audit:
            return
        if self.resample and self.resample["attempts"] >= self.max_resamples:
            if self.resample["target"] not in self.resample_tried:
                self.resample_tried.append(self.resample["target"])
            unimproved = not self.resample.get("improved")
            failing = [t for t in self._failing_tests(errors) if t not in self.audited]
            self.resample = None
            self.temperature_hint = None
            if unimproved and failing:
                # Rewriting the code changed nothing: maybe the tests are what is wrong.
                self.test_audit = {"stage": "ask", "tests": failing}
                NodeLog.emit("TEST_AUDIT_STARTED", {"tests": failing})
                return
        if not self.resample and (self.assertion_rounds >= 2 or self.resample_tried):
            # Two repair rounds capture most of the gain; resample after that.
            found = self._failing_function(errors)
            if found:
                self.resample = {"target": found[1], "file": found[0], "attempts": 0}
                NodeLog.emit("RESAMPLE_STARTED", {"target": found[1]})
        if self.resample:
            self.resample["attempts"] += 1
            self.temperature_hint = 0.8

    def _test_sources(self, names):
        tests_name = self.profile.get("tests")
        try:
            source = self.path(tests_name).read_text(encoding="utf-8")
        except (OSError, TypeError):
            return ""
        return inc.relevant_sources(source, " ".join(names), limit=9000)

    def audit_prompt(self, errors):
        a = self.test_audit
        tests_name = self.profile.get("tests")
        trace = "\n".join(e for e in errors if e.startswith("Observed behaviour"))
        suite = "\n".join(e for e in errors if e.startswith("Generated test suite"))
        failures = "\n".join(line for line in suite.splitlines()
                             if line.startswith(("FAIL:", "ERROR:", "AssertionError", "    self.assert")))
        head = ["Original task requirements (unchanged):\n" + self.task + "\n",
                f"=== FAILING TESTS in {tests_name} ===\n{self._test_sources(a['tests'])}\n=== END ===",
                "Assertion failures:\n" + failures[-3000:]]
        if trace:
            head.append(trace[-3000:])
        if a["stage"] == "ask":
            head.append(
                "TEST AUDIT: several fresh rewrites of the implementation did not make these tests pass. A test "
                "can itself be wrong (a wrong expected value, or an expectation the requirements never asked for). "
                "Judge each failing test ONLY against the original requirements and the rules of the domain.\n"
                "NEXT RESPONSE: one line per test, exactly in the form\n"
                "<test_name>: TEST_WRONG - <reason>   or   <test_name>: CODE_WRONG - <reason>\n"
                "No code and no other text.")
        else:
            head.append(
                f"You judged these tests wrong: {', '.join(a['wrong'])}. NEXT RESPONSE: rewrite ONLY those test "
                "methods so they check the original requirements correctly. Keep every test (do not delete, skip "
                "or empty any) and keep their names. Output the corrected method definitions inside their class "
                f"in one ```python block whose first line is '# filename: {tests_name}'. No other code, no prose.")
        return "\n".join(head)

    def _consume_audit_reply(self, content, blocks, feedback, errors):
        """Handle a test-audit verdict or a test fix; returns the remaining blocks."""
        a = self.test_audit
        tests_name = self.profile.get("tests")
        if a["stage"] == "ask":
            verdicts = dict(re.findall(r"(\w+)\s*:\s*(TEST_WRONG|CODE_WRONG)", content or ""))
            wrong = [t for t in a["tests"] if verdicts.get(t) == "TEST_WRONG"]
            self.audited.update(a["tests"])
            feedback.append("Test audit verdicts: " + (", ".join(f"{t}={v}" for t, v in verdicts.items()) or "none"))
            NodeLog.emit("TEST_AUDIT_VERDICT", {"verdicts": verdicts})
            self.test_audit = {"stage": "fix", "tests": a["tests"], "wrong": wrong} if wrong else None
            return [b for b in blocks if b["lang"] in PY_COMMANDS]
        # stage "fix": splice only the judged-wrong test methods, never fewer tests
        path = self.path(tests_name)
        current = path.read_text(encoding="utf-8")
        code = next((b["code"] for b in blocks if b.get("filename") == tests_name
                     and b.get("complete") is not False), None)
        self.test_audit = None
        if code is None:
            errors.append(f"The test fix did not contain a complete block for {tests_name}.")
            return [b for b in blocks if b["lang"] in PY_COMMANDS]
        kept = [src for n, src in inc.harvest(code) if n in a["wrong"]]
        try:
            patched, changed = inc.splice(current, "\n\n".join(kept), replace_implemented=True)
            before = len([q for q in inc.definitions(current) if q.rsplit(".", 1)[-1].startswith("test")])
            after = len([q for q in inc.definitions(patched) if q.rsplit(".", 1)[-1].startswith("test")])
            if after < before or "skip" in "".join(kept).lower():
                raise ValueError("the fix removed or skipped tests")
        except (SyntaxError, ValueError) as exc:
            errors.append(f"Test fix rejected: {exc}")
            return [b for b in blocks if b["lang"] in PY_COMMANDS]
        NodeForge.write_file(tests_name, patched, workspace_path=str(self.workspace))
        self.files_modified.add(tests_name)
        feedback.append(f"Corrected judged-wrong tests: {', '.join(changed)}")
        NodeLog.emit("TEST_AUDIT_FIXED", {"tests": changed})
        return [b for b in blocks if b["lang"] in PY_COMMANDS]

    def resample_prompt(self, errors, feedback):
        r = self.resample
        name, target = r["file"], r["target"]
        short = target.rsplit(".", 1)[-1]
        source = self.path(name).read_text(encoding="utf-8")
        tests_name = self.profile.get("tests")
        tests = self.path(tests_name).read_text(encoding="utf-8") if tests_name and self.path(tests_name).is_file() else ""
        # Signal, not noise: the explained trace plus the failing test names, not
        # pages of repeated unittest output about code the model cannot see.
        suite = "\n".join(e for e in errors if e.startswith("Generated test suite"))
        failing = sorted(set(re.findall(r"^(?:FAIL|ERROR): (\w+)", suite, re.M)))
        trace = "\n".join(e for e in errors if e.startswith("Observed behaviour"))
        observed = (("Failing tests: " + ", ".join(failing) + "\n") if failing else "") + (trace or suite)
        reverted = [e for e in errors if e.startswith("REVERTED")]
        parts = ["Original task requirements (unchanged):\n" + self.task + "\n",
                 f"FRESH REWRITE {r['attempts']}/{self.max_resamples} of `{target}` in {name}. Repairs of the "
                 "existing version kept failing the tests below, so it is being written again from scratch. The "
                 "old body is deliberately not shown: do not try to recall or reuse it.",
                 f"=== OUTLINE OF {name} (bodies shown as ...; use these names and attributes) ===\n"
                 + inc.outline(source) + "=== END OUTLINE ==="]
        if tests:
            parts.append(f"=== {tests_name} (the tests your function must pass) ===\n{tests[:6000]}\n=== END ===")
        parts.extend(reverted)  # never trimmed away with the long test output
        parts.append("Observed facts from the current version:\n" + observed[-5000:])
        if r.get("last_result"):
            parts.append("Previous fresh candidate: " + r["last_result"] + ".")
        parts.append(
            f"NEXT RESPONSE: write a NEW, complete implementation of `{short}` that satisfies the original "
            "requirements and makes these tests pass. Lines marked WHY show which condition in the old version "
            "caused the wrong result and the exact values it compared; make sure your version does not repeat "
            "it. Output ONLY that one function definition (plus any new helper "
            f"it needs) in one ```python block whose first line is '# filename: {name}'. No other code, no prose.")
        return "\n".join(parts)

    def process(self, content):
        from node_core.agents import extract_code_blocks_with_metadata, extract_api_contracts
        if self.status != "IN_PROGRESS":
            return None
        if self.should_stop():
            self.stop("CANCELLED", "User requested cancellation")
            return None
        if self.rounds >= self.max_rounds:
            self.stop("MAX_ROUNDS_REACHED", "Configured model response budget exhausted")
            return None
        self.rounds += 1
        before = (self.phase, self.snapshot(), set(self.read_evidence), self._progress_marker())
        feedback, errors = [], []
        content = content or ""
        defaults = {1: "logic", 2: "logic", 3: "gui", 4: "tests", 5: "guide"}
        blocks = extract_code_blocks_with_metadata(content, self.profile[defaults[self.phase]])
        if self.draft:
            blocks = self._consume_draft_reply(content, blocks, feedback)
        if self.test_audit:
            blocks = self._consume_audit_reply(content, blocks, feedback, errors)
        if self.partial:
            pending = self.partial
            commands = {"read", "bash", "sh", "shell", "cmd", "powershell"}
            match = next((b for b in blocks if b["lang"] not in commands and not b.get("routing_error")
                          and (b["filename"] == pending["name"] or b.get("inferred_filename"))), None)
            if match is None and not blocks and content.strip():
                match = {"code": content.strip("\n`"), "complete": True, "lang": ""}
            if match is not None:
                lines, restarted = self.merge_continuation(pending["lines"], match["code"].splitlines())
                if match.get("complete") is False and len(lines) <= len(pending["lines"]):
                    lines = pending["lines"]  # a shorter truncated part adds nothing
                match.update({"filename": pending["name"], "code": "\n".join(lines),
                              "inferred_filename": False, "routing_error": None, "continued": True,
                              "floor": len(pending["lines"])})
                pending["parts"] += 1
                self.partial = None
                if match.get("complete") is not False:
                    match["code"] += "\n"
                if match not in blocks:
                    blocks.insert(0, match)
                feedback.append(f"Joined continuation part {pending['parts']} of {pending['name']} "
                                f"({len(lines)} lines total)")
                if match.get("complete") is False:
                    match["parts"] = pending["parts"]
        for block in blocks:
            if self.should_stop():
                self.stop("CANCELLED", "User requested cancellation")
                break
            code, lang, name = block["code"], block["lang"], block["filename"]
            if block.get("routing_error"):
                errors.append(block["routing_error"])
                continue
            if ((self.incremental or self.phase >= 4) and name and name.endswith(".py") and lang not in PY_COMMANDS
                    and not block.get("continued") and not block.get("routing_error")
                    and self._rewrite_of_existing(name, code, block)):
                handled = self._patch_from_rewrite(name, code, block.get("complete") is not False, feedback, errors)
                if handled:
                    continue
            if block.get("complete") is False and name and name.lower().endswith((".txt", ".md")):
                trimmed = self.trim_repetition(code)
                if trimmed is not None:
                    code = block["code"] = trimmed
                    block["complete"] = True
                    feedback.append(f"{name} ended in a repetition loop; kept the text before the loop")
            if block.get("complete") is False:
                parts = block.get("parts", 1)
                # Keep only complete lines; the final line may be cut mid-token.
                lines = code.splitlines()[:-1]
                if name and name.lower().endswith(".py"):
                    lines = self.trim_to_boundary(lines, block.get("floor", 0))
                if (name and lang not in {"read", "bash", "sh", "shell", "cmd", "powershell"}
                        and parts < self.max_continuations and lines and not self.partial):
                    self.partial = {"name": name, "lines": lines, "parts": parts}
                    feedback.append(f"{name} was cut off by the output limit after {len(lines)} lines; "
                                    "saved as a partial draft for continuation")
                else:
                    errors.append(f"Incomplete code fence for {name}; response may be truncated. Send this complete file alone with a closing fence.")
                continue
            if lang == "read":
                for name in code.splitlines():
                    name = name.strip()
                    try:
                        match = re.fullmatch(r"(.+):(\d+)-(\d+)", name)
                        options = {}
                        label = name
                        if match:
                            name, start, end = match.groups()
                            options = {"start_line": int(start), "end_line": int(end), "line_numbers": True}
                        self.path(name)
                        res = NodeInsight.read_file(name, workspace_path=str(self.workspace), **options)
                        if res.get("status") != "success":
                            raise ValueError(res.get("error", "Read failed"))
                        self.files_read.add(name)
                        value = res.get('content', '')
                        self.read_evidence.add((label, hashlib.sha256(value.encode()).hexdigest()))
                        feedback.append(f"FILE {label}:\n{value[:16000]}" +
                                        ("\n[Truncated. Request path:START-END for later lines.]" if len(value) > 16000 else ""))
                    except (OSError, ValueError) as exc:
                        errors.append(str(exc))
            elif lang in {"bash", "sh", "shell", "cmd", "powershell"}:
                res = self.execute(code)
                feedback.append(f"Command {code!r}, exit {res.get('exit_code')}:\n{command_output(res)}")
                if not self.passed(res):
                    errors.append("Command failed; inspect its output and repair the cause.")
            elif name:
                try:
                    path = self.path(name)
                    if path.relative_to(self.workspace).parts[0].lower() == ".cloudcode":
                        raise ValueError("The .cloudcode directory is reserved for caller criteria and run evidence")
                    existed = path.exists()
                    if existed and block.get("inferred_filename"):
                        raise ValueError(f"Ambiguous overwrite of {name}; include '# filename: {name}' explicitly")
                    if (existed and (self.incremental or self.phase >= 4) and name.endswith(".py")
                            and inc.only_definitions(code)):
                        current = path.read_text(encoding="utf-8")
                        if self.resample and self.resample.get("file") == name:
                            target = self.resample["target"].rsplit(".", 1)[-1]
                            names = {q.rsplit(".", 1)[-1] for q in inc.definitions(current)}
                            kept = [src for n, src in inc.harvest(code) if n == target or n not in names]
                            if kept:
                                code = "\n\n".join(kept)
                        if len(code.splitlines()) < 0.7 * len(current.splitlines()):
                            try:
                                code, changed = inc.splice(current, code, replace_implemented=True)
                                feedback.append(f"Patched {', '.join(changed)} in {name}")
                            except (SyntaxError, ValueError) as exc:
                                raise ValueError(f"Could not apply definition patch to {name}: {exc}") from exc
                    # FIX 2: Check for structurally incomplete artifacts
                    artifact_issues = self.detect_incomplete_artifact(code, name)
                    if artifact_issues:
                        for issue in artifact_issues:
                            errors.append(issue)
                        errors.append(f"Incomplete artifact {name} was not written. Existing file preserved.")
                        continue
                    old_bytes = path.read_bytes() if existed else None
                    old = old_bytes.decode("utf-8") if existed else None
                    if name.lower().endswith(".py"):
                        compile(code, name, "exec")
                    res = NodeForge.write_file(name, code, workspace_path=str(self.workspace))
                    if not isinstance(res, dict) or res.get("status") != "success" or not path.is_file() or path.read_bytes() != code.encode("utf-8"):
                        # A failed/readback-mismatched write must preserve the
                        # previously inspected file, even if an adapter misbehaves.
                        if old_bytes is not None:
                            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as restored:
                                restored.write(old_bytes)
                            Path(restored.name).replace(path)
                        elif path.is_file():
                            path.unlink()
                        raise ValueError(f"Write failed: {name}: {res}")
                    if old is not None and old.strip() == code.strip() and self.phase >= 4:
                        # A byte-identical "fix" silently repeats the failure; say so.
                        errors.append(f"Your reply for {name} is IDENTICAL to the existing file: nothing was "
                                      "changed, so the failing behaviour reported above is still present. "
                                      "Change the code that the observed values point to.")
                    elif old != code:
                        (self.files_modified if existed else self.files_created).add(name)
                    feedback.append(f"Saved and read back {name}")
                    NodeLog.emit("FILE_CREATED" if not existed else "FILE_MODIFIED", {"file": name})
                except (OSError, ValueError, SyntaxError) as exc:
                    errors.append(str(exc))
        if self.partial:
            # A pending continuation blocks phase advancement like any missing file.
            errors.append(f"{self.partial['name']} is incomplete (truncated); continuation pending "
                          f"({len(self.partial['lines'])} lines saved)")
        if self.status != "IN_PROGRESS":
            return None
        if self.phase >= 3 and not self.single_file and not self.file_errors([self.profile["logic"]]):
            self.contract = extract_api_contracts(str(self.path(self.profile["logic"])))
        # A malformed documentation block must not hide real test failures in
        # already-written code. Retain every artifact error and do not advance.
        if errors and self.phase in (4, 5):
            errors.extend(self._gate(self.verify(final=self.phase == 5)))
        if not errors:
            if self.single_file:
                errors = self.file_errors(["hello.py", *self.required_files]) + self.criteria_errors()
                if not errors:
                    verification_before = self.snapshot()
                    runtime = self.execute(python_command("hello.py"), "runtime")
                    if not self.passed(runtime):
                        errors.append(command_output(runtime))
                    for check in self.checks:
                        result = self.execute(check["command"], "acceptance", check["requirement"])
                        if not self.passed(result):
                            errors.append(check["requirement"] + ": " + command_output(result))
                    if not self.checks:
                        errors.append("Task-specific acceptance checks are not configured")
                    if self.snapshot() != verification_before:
                        errors.append("Project files changed during verification; verify the final files again")
                    errors.extend(self.criteria_errors())
                    self.verification.append({"round": self.rounds, "final": True, "passed": not errors,
                                              "errors": errors, "files": self.snapshot()})
                    if not errors and self.status == "IN_PROGRESS":
                        self.completed_phases = [1, 4]
                        self.phase = 6
                        self.stop("COMPLETED", "Single-file implementation, runtime and acceptance checks passed")
                    elif not self.checks and self.status == "IN_PROGRESS":
                        self.stop("VERIFICATION_FAILED", "Task-specific acceptance checks are not configured")
            elif self.phase in (1, 2):
                errors = self.file_errors([self.profile["logic"]])
                if not errors:
                    if self.phase == 1:
                        self.transition(2)
                    self.contract = extract_api_contracts(str(self.path(self.profile["logic"])))
                    if self.contract.startswith("[!]"):
                        errors.append(self.contract)
                    else:
                        self.transition(3)
            elif self.phase == 3:
                errors = self.file_errors([self.profile["logic"], self.profile["gui"]])
                if not errors:
                    self.transition(4)
            elif self.phase == 4:
                errors = self._gate(self.verify())
                if not errors:
                    self.transition(5)
            elif self.phase == 5:
                # Final verification always re-runs after the last writes, even if
                # the model omitted its completion token or changed earlier files.
                errors = self._gate(self.verify(final=True), final=True)
                if not errors and self.status == "IN_PROGRESS":
                    self.transition(6)
                    self.stop("COMPLETED", "All phases and configured acceptance checks passed")
                elif self.status == "IN_PROGRESS" and not self.checks and not self.file_errors([self.profile["guide"]]):
                    self.stop("VERIFICATION_FAILED", "Task-specific acceptance checks are not configured")
        self.remaining = errors
        after = (self.phase, self.snapshot(), set(self.read_evidence), self._progress_marker())
        self.files_created.update(set(after[1]) - set(before[1]))
        self.files_modified.update(name for name in before[1] if name in after[1]
                                   and before[1][name] != after[1][name])
        self.no_progress = self.no_progress + 1 if before == after else 0
        fingerprint = (self.phase, tuple(sorted(after[1].items())), tuple(sorted(after[2])), after[3])
        self.seen_states[fingerprint] = self.seen_states.get(fingerprint, 0) + 1

        # FIX 4: Track error history for adaptive feedback
        # Ignore run timings ("Ran 6 tests in 0.001s") so identical failures compare equal.
        error_fingerprint = tuple(sorted({re.sub(r"\d+\.\d+s", "", e) for e in errors})) if errors else ()
        if self.error_history and self.error_history[-1] == error_fingerprint:
            self.consecutive_identical_errors += 1
        else:
            self.consecutive_identical_errors = 0
        self.error_history.append(error_fingerprint)
        # Near-deterministic sampling repeats the same wrong answer on a
        # near-identical prompt; add variety once errors stop changing.
        self.temperature_hint = (0.9 if self.consecutive_identical_errors >= 4 else
                                 0.6 if self.consecutive_identical_errors >= 2 else None)
        # Repairs need functions, not files: a small budget stops whole-file copies.
        self.max_tokens_hint = 800 if self.phase >= 4 and not self.single_file else None

        if self.status == "IN_PROGRESS":
            stall_detected = (self.no_progress >= self.stall_limit
                              or self.seen_states[fingerprint] > self.stall_limit)
            if self.rounds >= self.max_rounds:
                self.stop("MAX_ROUNDS_REACHED", "Configured model response budget exhausted")
            # FIX 5: Active stall recovery — attempt recovery before final STALLED
            elif stall_detected:
                if not self._recovery_attempted:
                    self._recovery_attempted = True
                    self.recovery_evidence.append({"round": self.rounds, "files": self.snapshot(),
                                                   "phase": self.phase, "errors": list(errors)})
                    NodeLog.emit("STALL_RECOVERY", {"round": self.rounds,
                                                     "no_progress": self.no_progress})
                    # Grant a bounded recovery window for inspect -> repair ->
                    # verify instead of terminating on the very next response.
                    self.no_progress = 0
                    self.seen_states.clear()
                    # Build a recovery-specific feedback message
                    if self.partial or self.draft:
                        self._save_state()
                        return self.next_prompt(errors, feedback)
                    self._save_state()
                    return self._build_recovery_feedback(errors, feedback)
                else:
                    self.stop("STALLED", "Repeated iterations made no new file or phase progress")
        self._save_state()
        if self.status != "IN_PROGRESS":
            return None
        return self.next_prompt(errors, feedback)

    def next_prompt(self, errors, feedback):
        """Build the next model prompt from current workflow state."""
        if self.partial:
            return self.continuation_prompt(errors)
        if any(e.startswith("Naming convention: ") for e in errors):
            self.resample = None  # fix the spelling first; it may be the real cause
        else:
            self._update_resample(errors)
        if self.test_audit:
            return self.audit_prompt(errors)
        if self.resample:
            return self.resample_prompt(errors, feedback)
        if self.incremental and not self.single_file:
            self._maybe_start_draft()
            if self.draft:
                return self.draft_prompt(errors)

        # FIX 4: Build adaptive feedback based on error repetition count
        adaptive_prefix = self._build_adaptive_prefix(errors)
        checkpoint, next_action = self.repair_checkpoint(errors)

        reverted = "".join(e + "\n" for e in errors if e.startswith("REVERTED"))
        return ("Original task requirements (unchanged):\n" + self.task + "\n\n" + checkpoint + "\n\n" +
                reverted +
                "Iteration finished; the overall task is still incomplete. TERMINATE, PHASE_COMPLETE and "
                "TASK_COMPLETE are requests for evaluation, never proof of completion.\n" +
                adaptive_prefix +
                "\n\n".join(feedback)[-16000:] + "\nRemaining checks:\n" + "\n".join(errors)[-12000:] +
                "\n" + self.directive() +
                "\nReturn complete file code blocks with '# filename: <relative path>' on line 1. "
                "For Markdown containing triple-backtick examples, use FOUR backticks for the outer file fence. "
                "If a response was truncated, return one file at a time. Perform repairs yourself using files or shell blocks." +
                "\n" + next_action)

    def _build_adaptive_prefix(self, errors):
        """FIX 4: Build adaptive prefix when errors repeat across rounds."""
        if self.consecutive_identical_errors <= 0 or not errors:
            return ""
        count = self.consecutive_identical_errors
        blockers = [e for e in errors if any(s in e.lower() for s in
                    ('not installed', 'import failed', 'modulenotfounderror', 'cannot import name'))]
        if blockers:
            return ("Repeated import blocker: inspect the installed library API and the failing file "
                    "with read/shell blocks first. Fix the actual import before generating missing files.\n" +
                    "\n".join(blockers)[:3000] + "\n")
        # Identify missing files from errors
        missing = [e.split(":")[0].strip() for e in errors
                   if "No such file" in e or "file is missing" in e or "Errno 2" in e]
        if count == 1:
            parts = []
            for m in missing:
                parts.append(f"{m} remains missing after your previous attempt. "
                             "Your last response did not create it. "
                             f"Produce only {m} in a single code block. "
                             "Keep it small enough to fit in one response.")
            if parts:
                return "\n".join(parts) + "\n"
            return ("Your previous response did not fix the reported errors. "
                    "Try a different approach.\n")
        # count >= 2: FIX 6 — encourage workspace inspection
        existing_files = sorted(f for f in self.snapshot().keys()
                                if not f.startswith(".") and not f.startswith("nodelog_data"))
        parts = [f"The same errors have persisted for {count + 1} consecutive rounds. "
                 "Your previous approach is not working."]
        if existing_files:
            parts.append("Before attempting repair, inspect the current workspace files:")
            read_block = "```read\n" + "\n".join(existing_files[:10]) + "\n```"
            parts.append(read_block)
        for m in missing:
            parts.append(f"Then produce a minimal but complete {m}.")
        if not missing:
            parts.append("Inspect existing files and make targeted repairs.")
        return "\n".join(parts) + "\n"

    def _build_recovery_feedback(self, errors, feedback):
        """FIX 5 + FIX 6 + FIX 9: Build a recovery-specific feedback after stall detection.

        FIX 9: Prioritise dependency/import errors over missing-file errors,
        because an unresolvable import blocks ALL progress.
        """
        existing_files = sorted(f for f in self.snapshot().keys()
                                if not f.startswith(".") and not f.startswith("nodelog_data"))
        phase_files = [self.profile[k] for k in ("logic", "gui", "tests", "main", "guide")]
        missing_files = [f for f in phase_files if not self.path(f).is_file()]

        # FIX 9: Separate blocking errors (dependency/import) from file-level errors
        dep_errors = [e for e in errors if "not installed" in e.lower() or "missing dependency" in e.lower()
                      or "ModuleNotFoundError" in e or "No module named" in e
                      or "cannot import name" in e.lower()]
        other_errors = [e for e in errors if e not in dep_errors]

        checkpoint, next_action = self.repair_checkpoint(errors)
        parts = [
            "Original task requirements (unchanged):\n" + self.task,
            checkpoint,
            "STALL RECOVERY: Your previous responses have not made progress. "
            "This is a recovery attempt before the task is terminated.",
            "",
        ]

        # FIX 9: Lead with the actual blocker
        if dep_errors:
            parts.append("=== BLOCKING ISSUE: DEPENDENCY / IMPORT ERROR ===")
            parts.append("The following dependency or import errors are preventing ALL progress:")
            for de in dep_errors[:3]:
                parts.append(f"  - {de[:500]}")
            parts.append("")
            parts.append(
                "You MUST fix this before any other work. Options:\n"
                f"1. Inspect the environment using {python_command('-m', 'pip', 'list')}; "
                "install missing required packages with this interpreter. Do not rewrite away "
                "a dependency explicitly required by the task.\n"
                "2. If you are importing a name that does not exist in the library, "
                "check the library's real API and fix the import statement.\n"
                "3. If a requirements.txt declares a package, ensure the import name matches."
            )
            parts.append("")

        if existing_files:
            parts.append(f"Files currently in workspace: {', '.join(existing_files)}")
        if missing_files:
            parts.append(f"Files still missing: {', '.join(missing_files)}")
            if not dep_errors:
                parts.append(f"Produce ONLY {missing_files[0]} in this response. "
                             "Do not include any other files. "
                             "Keep it complete but concise.")
            else:
                parts.append(
                    "Fix the blocking import errors first, then produce missing files."
                )
        else:
            parts.append("All expected files exist but verification is failing.")
            parts.append("Inspect the existing files and make targeted repairs.")
        if existing_files:
            parts.append("")
            parts.append("Inspect existing files first:")
            read_block = "```read\n" + "\n".join(existing_files[:10]) + "\n```"
            parts.append(read_block)
        parts.append("")
        if other_errors:
            parts.append("Other remaining errors:")
            parts.extend(other_errors[-5:])
        parts.append("")
        parts.append(self.directive())
        if feedback:
            parts.append("Latest tool evidence:\n" + "\n".join(feedback)[-16000:])
        parts.append(
            "Return complete file code blocks with '# filename: <relative path>' on line 1. "
            "If a response was truncated, return one file at a time.")
        parts.append(next_action)
        return "\n".join(parts)

    def report(self):
        phase_files = ["hello.py"] if self.single_file else [self.profile[k] for k in ("logic", "gui", "tests", "main", "guide")]
        expected = list(dict.fromkeys(phase_files + self.required_files))
        remaining = list(self.remaining)
        if self.status != "COMPLETED":
            remaining += [f"Missing deliverable: {name}" for name in expected if not self.path(name).is_file()]
            remaining += [check["requirement"] for check in self.checks
                          if not any(c["kind"] == "acceptance" and c["requirement"] == check["requirement"]
                                     and c["status"] == "success" and c["exit_code"] == 0
                                     for c in self.commands if c["round"] == self.rounds)]
        return {"status": self.status, "termination_reason": self.reason, "original_task": self.task,
                "rounds": self.rounds, "max_rounds": self.max_rounds, "current_phase": self.phase,
                "completed_phases": self.completed_phases, "files_expected": expected,
                "files_actual": self.snapshot(), "files_created": sorted(self.files_created),
                "files_modified": sorted(self.files_modified), "files_read": sorted(self.files_read),
                "read_evidence": sorted(self.read_evidence), "responses_received": self.responses_received,
                "commands": self.commands, "verification": self.verification,
                "recovery_evidence": [{**r, "files_changed": r["files"] != self.snapshot(),
                                       "phase_advanced": self.phase > r["phase"],
                                       "errors_changed": r["errors"] != self.remaining}
                                      for r in self.recovery_evidence],
                "acceptance": self.acceptance, "remaining_requirements": list(dict.fromkeys(remaining))}
