import ast
import os
import re
import sys
from typing import Dict, Any, List, Union, Optional, Tuple
from autogen import ConversableAgent, UserProxyAgent, register_function
from autogen.code_utils import extract_code

from node_core.tools import NodeForge, NodePulse, NodeLog, safe_console_print, sanitize_for_console


def extract_code_blocks_with_metadata(
    content: str,
    default_filename: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Extracts code blocks with robust detection of target filenames and languages.
    Hardenings:
    - Safe non-greedy parsing: handles multi-file blocks safely without swallowing adjacent blocks.
    - Gracefully parses code blocks where trailing triple backticks (```) are missing due to token truncation.
    - Explicit '# filename: <path>' extraction on line 1.
    - Falls back to a structured workspace path rather than dropping output.
    """
    if not content or not isinstance(content, str):
        return []

    results: List[Dict[str, Any]] = []
    lines = content.splitlines()
    i = 0
    n = len(lines)
    preceding_lines: List[str] = []

    in_block = False
    current_lang = ""
    current_header = ""
    current_code_lines: List[str] = []
    block_preceding_text = ""
    current_fence = "```"
    document_nested_fences = []

    fence_pattern = re.compile(r"^\s*(?P<fence>`{3,})(?P<lang>[a-zA-Z0-9_\-]*)(?:[ \t]+(?P<header>[^\r\n]+))?\s*$")

    def process_collected_block(lang: str, header: str, code_lines: List[str], prec_text: str, block_idx: int) -> Dict[str, Any]:
        code = "\n".join(code_lines)
        target_filename = None

        # 1. Detect filename from header: ```python filename=main.py or ```python:main.py
        if header:
            h_match = re.search(r'(?:filename|filepath)?[:=]?\s*([a-zA-Z0-9_\-\.\/\\]+\.[a-zA-Z0-9]+)', header, re.IGNORECASE)
            if h_match:
                target_filename = h_match.group(1).strip()

        # 2. Strict check: detect filename from line 1 of code
        code_split = code.splitlines()
        if not target_filename and code_split:
            first_line = code_split[0].strip()
            # Explicit '# filename: <path>' or '// filename: <path>'
            explicit_match = re.search(
                r'^(?:#|//|<!--|;)?\s*(?:filename|filepath|file)\s*[:=]\s*([a-zA-Z0-9_\-\.\/\\]+\.[a-zA-Z0-9]+)',
                first_line,
                re.IGNORECASE
            )
            if explicit_match:
                target_filename = explicit_match.group(1).strip()
            elif re.search(r'^(?:#|//|<!--|;)\s*([a-zA-Z0-9_\-]+\.[a-zA-Z0-9]{1,5})\s*(?:-->)?$', first_line, re.IGNORECASE):
                target_filename = re.search(r'^(?:#|//|<!--|;)\s*([a-zA-Z0-9_\-]+\.[a-zA-Z0-9]{1,5})\s*(?:-->)?$', first_line, re.IGNORECASE).group(1).strip()

        # 3. Detect filename from preceding markdown lines
        if not target_filename and prec_text:
            last_lines = prec_text.splitlines()[-4:]
            tail = "\n".join(last_lines)
            md_matches = list(re.finditer(
                r"""(?:(?:file(?:name)?|save as|here is|here's)\s*[:=]?\s*[`"']?|###?\s*(?:(?:step\s*)?[0-9]+[\.\):]\s*)?[`"']?|\*\*(?:file:)?\s*(?:(?:step\s*)?[0-9]+[\.\):]\s*)?[`"']?)([a-zA-Z0-9_\-\.\/\\]+\.[a-zA-Z0-9]{1,5})[`"']?""",
                tail,
                re.IGNORECASE
            ))
            # The nearest heading names this block; earlier prose can mention
            # imports or other files and must not redirect the write.
            md_match = md_matches[-1] if md_matches else None
            if md_match:
                cand = md_match.group(1).strip()
                if not cand.lower().endswith((".png", ".jpg", ".gif", ".exe")):
                    target_filename = cand

        explicit_filename = target_filename is not None
        # 4. Fallback inference based on content signatures
        if not target_filename:
            code_lower = code.lower()
            if "class chessboard" in code_lower or "class chessgame" in code_lower:
                target_filename = "chess_logic.py"
            elif "class ludoboard" in code_lower or "class ludogame" in code_lower:
                target_filename = "ludo_logic.py"
            elif "class sudokuboard" in code_lower or "class sudokugame" in code_lower:
                target_filename = "sudoku_logic.py"
            elif "class chessgui" in code_lower:
                target_filename = "chess_gui.py"
            elif "class ludogui" in code_lower:
                target_filename = "ludo_gui.py"
            elif "class sudokugui" in code_lower:
                target_filename = "sudoku_gui.py"
            elif "unittest.testcase" in code_lower or "def test_" in code_lower:
                target_filename = "test_suite.py"
            elif 'if __name__ == "__main__":' in code and ("gui" in code_lower or "game" in code_lower or "app" in code_lower):
                target_filename = "main.py"
            elif "running guide" in code_lower or "rules of" in code_lower or "how to play" in code_lower:
                target_filename = "RunningGUIDE.txt"

        # 5. Structured workspace fallback: NEVER drop output
        if not target_filename:
            if default_filename and (lang in {"python", "py"} and default_filename.lower().endswith(".py") or lang in {"text", "txt"} and default_filename.lower().endswith(".txt")):
                target_filename = default_filename
            elif lang in ["python", "py"]:
                target_filename = f"workspace_artifact_{block_idx + 1}.py"
            elif lang in ["bash", "sh", "shell", "cmd", "powershell"]:
                target_filename = None
            elif lang in ["json"]:
                target_filename = f"config_{block_idx + 1}.json"
            else:
                target_filename = f"artifact_{block_idx + 1}.txt"

        # Strip only an actual first-line file directive. Ordinary comments and
        # TERMINATE inside string literals are part of the user's program.
        clean_code = code
        routing_error = None
        if code_split:
            directive = re.fullmatch(r"(?:#|//|<!--|;)\s*(?:(?:filename|filepath|file)\s*[:=]\s*)?([a-zA-Z0-9_\-./\\]+\.[a-zA-Z0-9]+)\s*(?:-->)?", code_split[0].strip(), re.IGNORECASE)
            if directive and directive.group(1) == target_filename:
                clean_code = "\n".join(code_split[1:])
            elif directive:
                routing_error = "Conflicting filenames in fence header and file directive; send one explicit target"

        return {
            "lang": lang,
            "filename": target_filename,
            "code": clean_code,
            "raw_code": code,
            "inferred_filename": not explicit_filename,
            "routing_error": routing_error,
        }

    while i < n:
        line = lines[i]
        fence_match = fence_pattern.match(line)

        if fence_match:
            lang = (fence_match.group("lang") or "").strip().lower()
            header = (fence_match.group("header") or "").strip()
            fence = fence_match.group("fence")

            if in_block:
                # Fences inside a document are document bytes, never commands.
                # This also handles a model using equal-width nested fences.
                is_document = current_lang in {"markdown", "md", "text", "txt"}
                if len(fence) < len(current_fence):
                    current_code_lines.append(line)
                    i += 1
                    continue
                if is_document and (lang or header or document_nested_fences):
                    if lang or header:
                        document_nested_fences.append(fence)
                    else:
                        document_nested_fences.pop()
                    current_code_lines.append(line)
                    i += 1
                    continue
                # If closing fence (no lang, no header)
                if not lang and not header:
                    results.append(process_collected_block(current_lang, current_header, current_code_lines, block_preceding_text, len(results)))
                    in_block = False
                    current_code_lines = []
                    current_lang = ""
                    current_header = ""
                    document_nested_fences = []
                    block_preceding_text = ""
                    preceding_lines = []
                    i += 1
                    continue
                else:
                    # New code fence started without closing previous (truncated block)
                    unfinished = process_collected_block(current_lang, current_header, current_code_lines, block_preceding_text, len(results))
                    unfinished["complete"] = False
                    results.append(unfinished)
                    in_block = True
                    current_lang = lang
                    current_header = header
                    current_fence = fence
                    document_nested_fences = []
                    current_code_lines = []
                    block_preceding_text = "\n".join(preceding_lines[-4:])
                    preceding_lines = []
                    i += 1
                    continue
            else:
                # Starting a code block
                in_block = True
                current_lang = lang
                current_header = header
                current_fence = fence
                document_nested_fences = []
                current_code_lines = []
                block_preceding_text = "\n".join(preceding_lines[-4:])
                preceding_lines = []
                i += 1
                continue

        if in_block:
            current_code_lines.append(line)
        else:
            preceding_lines.append(line)
        i += 1

    # Gracefully handle truncated code block at EOF (missing closing ```)
    if in_block and current_code_lines:
        unfinished = process_collected_block(current_lang, current_header, current_code_lines, block_preceding_text, len(results))
        unfinished["complete"] = False
        results.append(unfinished)

    # Fallback for LLM responses that output code directly with '# filename: <path>' without opening markdown fences
    if not results:
        raw_sections = re.split(r'(?=^(?:#|//|<!--|;)?\s*(?:filename|filepath|file)\s*[:=]\s*[a-zA-Z0-9_\-\.\/\\]+\.[a-zA-Z0-9]+)', content, flags=re.MULTILINE)
        for section in raw_sections:
            sec_clean = section.strip()
            if not sec_clean:
                continue
            first_line = sec_clean.splitlines()[0].strip()
            fn_match = re.search(r'^(?:#|//|<!--|;)?\s*(?:filename|filepath|file)\s*[:=]\s*([a-zA-Z0-9_\-\.\/\\]+\.[a-zA-Z0-9]+)', first_line, re.IGNORECASE)
            if fn_match:
                fname = fn_match.group(1).strip()
                clean_sec = re.sub(r'```[a-zA-Z0-9_\-]*\s*$', '', sec_clean).strip()
                clean_sec = re.sub(r'^(?:#|//|<!--|;)[^\r\n]*(?:filename|filepath|file|\.[a-zA-Z0-9]{1,5})[^\r\n]*\r?\n', '', clean_sec, count=1, flags=re.IGNORECASE)
                # Preserve literal code, including completion words in strings.
                results.append({
                    "lang": "python" if fname.endswith(".py") else "text",
                    "filename": fname,
                    "code": clean_sec,
                    "raw_code": sec_clean
                })

    return results


def extract_api_contracts(target: str, is_code: bool = False, source_name: str = "") -> str:
    """
    Extracts public function and class signatures from a Python module using AST.
    Generates an exact, unambiguous Markdown API Contract block for downstream GUI,
    test suite, and main application launcher phases.
    """
    if not is_code and os.path.isfile(target):
        source_name = source_name or os.path.basename(target)
        try:
            with open(target, "r", encoding="utf-8", errors="replace") as f:
                code_text = f.read()
        except Exception as e:
            return f"[!] Error reading {target} for contract extraction: {e}"
    else:
        code_text = target
        source_name = source_name or "logic_module.py"

    try:
        tree = ast.parse(code_text, filename=source_name)
    except SyntaxError as se:
        return f"[!] Syntax error parsing {source_name} for contract extraction (line {se.lineno}): {se.msg}"

    classes_info = []
    functions_info = []
    constants_info = []

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            class_name = node.name
            doc = ast.get_docstring(node) or ""
            first_doc = doc.splitlines()[0].strip() if doc else ""
            methods = []
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    m_name = item.name
                    if m_name == "__init__" or not m_name.startswith("_"):
                        args_list = []
                        num_defaults = len(item.args.defaults)
                        num_args = len(item.args.args)
                        default_offset = num_args - num_defaults
                        for idx, a in enumerate(item.args.args):
                            ann = f": {ast.unparse(a.annotation)}" if a.annotation else ""
                            def_str = ""
                            if idx >= default_offset:
                                d_node = item.args.defaults[idx - default_offset]
                                def_str = f" = {ast.unparse(d_node)}"
                            args_list.append(f"{a.arg}{ann}{def_str}")
                        if item.args.vararg:
                            ann = f": {ast.unparse(item.args.vararg.annotation)}" if item.args.vararg.annotation else ""
                            args_list.append(f"*{item.args.vararg.arg}{ann}")
                        if item.args.kwarg:
                            ann = f": {ast.unparse(item.args.kwarg.annotation)}" if item.args.kwarg.annotation else ""
                            args_list.append(f"**{item.args.kwarg.arg}{ann}")
                        ret_ann = f" -> {ast.unparse(item.returns)}" if item.returns else ""
                        sig = f"{m_name}({', '.join(args_list)}){ret_ann}"
                        m_doc = ast.get_docstring(item) or ""
                        first_m_doc = f" # {m_doc.splitlines()[0].strip()}" if m_doc else ""
                        methods.append(f"    - {sig}{first_m_doc}")
            classes_info.append({
                "name": class_name,
                "doc": first_doc,
                "methods": methods
            })

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            f_name = node.name
            if not f_name.startswith("_"):
                args_list = []
                num_defaults = len(node.args.defaults)
                num_args = len(node.args.args)
                default_offset = num_args - num_defaults
                for idx, a in enumerate(node.args.args):
                    ann = f": {ast.unparse(a.annotation)}" if a.annotation else ""
                    def_str = ""
                    if idx >= default_offset:
                        d_node = node.args.defaults[idx - default_offset]
                        def_str = f" = {ast.unparse(d_node)}"
                    args_list.append(f"{a.arg}{ann}{def_str}")
                if node.args.vararg:
                    ann = f": {ast.unparse(node.args.vararg.annotation)}" if node.args.vararg.annotation else ""
                    args_list.append(f"*{node.args.vararg.arg}{ann}")
                if node.args.kwarg:
                    ann = f": {ast.unparse(node.args.kwarg.annotation)}" if node.args.kwarg.annotation else ""
                    args_list.append(f"**{node.args.kwarg.arg}{ann}")
                ret_ann = f" -> {ast.unparse(node.returns)}" if node.returns else ""
                sig = f"{f_name}({', '.join(args_list)}){ret_ann}"
                f_doc = ast.get_docstring(node) or ""
                first_f_doc = f" # {f_doc.splitlines()[0].strip()}" if f_doc else ""
                functions_info.append(f"  - {sig}{first_f_doc}")

        elif isinstance(node, ast.Assign):
            for t_node in node.targets:
                if isinstance(t_node, ast.Name) and t_node.id.isupper():
                    val_preview = ast.unparse(node.value)
                    if len(val_preview) > 60:
                        val_preview = val_preview[:57] + "..."
                    constants_info.append(f"  - {t_node.id} = {val_preview}")

    lines = [
        f"=== EXTRACTED API CONTRACT (from {source_name}) ===",
        "Your code MUST strictly conform to these exact class names, method signatures, and parameters:"
    ]

    if classes_info:
        lines.append("\nPublic Classes & Methods:")
        for c in classes_info:
            doc_str = f" - \"\"\"{c['doc']}\"\"\"" if c['doc'] else ""
            lines.append(f"  class {c['name']}:{doc_str}")
            if c['methods']:
                lines.extend(c['methods'])
            else:
                lines.append("    (no public methods)")

    if functions_info:
        lines.append("\nPublic Module Functions:")
        lines.extend(functions_info)

    if constants_info:
        lines.append("\nExported Constants:")
        lines.extend(constants_info)

    lines.append("==================================================")
    return "\n".join(lines)


def detect_project_profile(task_prompt: str, ws_files: Optional[List[str]] = None) -> Dict[str, str]:
    """Detects target file naming convention based on prompt or existing workspace."""
    p_lower = task_prompt.lower()
    ws_files = ws_files or []

    if "ludo" in p_lower:
        prefix = "ludo"
    elif "sudoku" in p_lower:
        prefix = "sudoku"
    elif "chess" in p_lower:
        prefix = "chess"
    else:
        logic_match = [f for f in ws_files if f.endswith("_logic.py")]
        if logic_match:
            prefix = logic_match[0][:-9]
        else:
            m = re.search(r'([a-zA-Z0-9_\-]+)_logic\.py', task_prompt, re.IGNORECASE)
            if m:
                prefix = m.group(1).lower()
            else:
                prefix = "app"

    return {
        "prefix": prefix,
        "logic": f"{prefix}_logic.py",
        "gui": f"{prefix}_gui.py",
        "tests": f"test_{prefix}.py",
        "main": "main.py",
        "guide": "RunningGUIDE.txt"
    }


def prepare_phased_task_prompt(task_prompt: str) -> str:
    """
    Transforms an open-ended multi-file project prompt into a scoped Phase 1 prompt,
    preventing the LLM from attempting to generate all project files at once
    and hitting remote token caps.
    """
    if "hello.py" in task_prompt.lower() and "sanity check" in task_prompt.lower():
        return ("TASK OBJECTIVE:\n" + task_prompt + "\nImplement and run hello.py. "
                "Only the orchestrator can declare completion after checking the file and actual output.")
    profile = detect_project_profile(task_prompt, [])
    logic_file = profile["logic"]

    return f"""TASK OBJECTIVE:
{task_prompt.strip()}

================================================================================
STAGED PIPELINE EXECUTION: PHASE 1 OF 5 (Interface & Logic Engine)
================================================================================
MANDATE FOR THIS TURN:
1. Implement ONLY the core logic engine file (`{logic_file}`).
2. Implement all core data models, board/state representations, rules, valid move calculations, and state serialization.
3. STRICT NEGATIVE CONSTRAINTS:
   - ZERO CONVERSATIONAL ADVICE: Do NOT provide human advice, greetings, or conversational intros.
   - DO NOT generate GUI, main entry point, test suite, or documentation in this turn.
   - FORBIDDEN STUBS: Never write '# TODO', '# Implement later', or empty stubs. All logic must be fully implemented.
4. Output 100% complete source code in a single Python code block with '# filename: {logic_file}' on line 1.
5. PHASE_COMPLETE finishes only this phase; ITERATION_COMPLETE finishes only an iteration.
   TASK_COMPLETE (or legacy TERMINATE) requests final verification. Only the orchestrator can declare completion.
================================================================================
"""


def bounded_model_messages(messages, max_history_chars=36000, max_total_chars=None):
    """Retain the original specification and latest feedback, not old failed code.

    Only the inference view changes; AutoGen's complete execution/evidence
    history stays intact. With ``max_total_chars`` (derived from the model's
    context window) the whole view must fit: the latest message is always kept
    (every runner prompt is self-contained), recent messages are added while
    they fit, and the long original prompt only if room remains. Character
    budgets are estimates, not tokenizer counts.
    """
    if not messages:
        return messages
    size = lambda m: len(str(m.get('content', '') or ''))
    if max_total_chars is None:
        if len(messages) < 3:
            return messages
        used = size(messages[0]) + size(messages[-1])
        recent = []
        for message in reversed(messages[1:-1]):
            if used + size(message) > max_history_chars or len(recent) >= 3:
                break
            recent.append(message)
            used += size(message)
        return [messages[0], *reversed(recent), messages[-1]]
    last = dict(messages[-1])
    content = str(last.get('content', '') or '')
    if len(content) > max_total_chars:
        # Keep the head (task) and the tail (current state and instructions).
        head = int(max_total_chars * 0.3)
        tail = max_total_chars - head - 80
        last['content'] = content[:head] + "\n[... middle of this prompt trimmed to fit the model context ...]\n" + content[-tail:]
    used = size(last)
    recent = []
    for message in reversed(messages[1:-1]):
        if used + size(message) > max_total_chars or len(recent) >= 3:
            break
        recent.append(message)
        used += size(message)
    view = [*reversed(recent), last]
    if len(messages) > 1 and used + size(messages[0]) <= max_total_chars:
        view.insert(0, messages[0])
    # Keep roles alternating as the chat template expects: never start on an assistant turn.
    while len(view) > 1 and view[0].get('role') == 'assistant':
        view.pop(0)
    return view


def create_coder_agent(llm_config: Dict[str, Any], fold_system: bool = False,
                       endpoint_resolver=None, should_stop=None,
                       prompt_budget_chars=None) -> ConversableAgent:
    """Streamlined autonomous coder agent tailored for Qwen-Coder and dual-agent runner."""
    agent = ConversableAgent(
        name="CoderAgent",
        system_message="""You are the Autonomous Principal Software Engineer (CoderAgent).
Your objective is to build complete, production-grade, fully functional software projects without missing any required files or features.

STRICT OPERATIONAL RULES & NEGATIVE CONSTRAINTS:
1. ZERO CONVERSATIONAL ADVICE:
   - You are an automated non-interactive engine. Never output conversational advice, pleasantries, step-by-step human instructions (e.g., 'Open file X and indent line Y'), or explanations when an error occurs.
   - Do NOT say 'Here is the code...' or 'I have implemented...'. Output file, shell, or read code blocks.
   - Use a ```read block containing relative file paths to inspect existing code before changing it.

2. COMPLETE RECOVERY ARTIFACTS:
   - When errors are returned, inspect existing files with read blocks and inspect installed dependency APIs with short shell commands as needed. Then return the complete corrected file containing `# filename: <filepath>` on line 1.
   - Never output diffs, snippets, or partial patches. Always provide the entire, fully implemented file.

3. FORBIDDEN STUBS & PLACEHOLDERS:
   - Never use placeholder comments such as `# TODO`, `# Implement later`, `# Add piece logic here`, or `pass`.
   - All core logic, algorithms, state serialization, and event handlers must be fully and thoroughly implemented.

4. PHASED CODE BLOCK FORMATTING:
   - Output EACH file in its own Markdown code block.
   - ALWAYS place `# filename: <filepath>` on the FIRST line of the code block.
   - Focus exclusively on the file(s) requested for the active phase. Do NOT output downstream files prematurely.

5. TESTS & HEADLESS EXECUTION:
   - Write automated unit tests using the standard `unittest` library.
   - Keep files concise. Use small representative fixtures and loops/subTest for related cases instead of hundreds of repetitive assertions. Preserve every requested feature.
   - Use required installed libraries and their actual APIs; do not replace task-required libraries with hand-written implementations.
   - DO NOT run GUI mainloops in test files or bash blocks, as GUI event loops block the non-interactive runner. Test game logic headlessly.

6. NO TOOLS:
   - You have no tool or function-calling ability. To run a command, write a ```bash block; to read a file,
     write a ```read block with its path. The runner executes these and returns the output.

7. TERMINATION:
   - PHASE_COMPLETE and ITERATION_COMPLETE are local progress signals.
   - TASK_COMPLETE or legacy TERMINATE requests verification, never ends an incomplete task.
   - Follow the runner's remaining requirements until it confirms completion.
""",
        llm_config=llm_config,
    )
    rules = agent.system_message
    budget = prompt_budget_chars

    def view(messages):
        return bounded_model_messages(messages, max_total_chars=budget)
    if fold_system:
        # Endpoints that forward only the last user message never see the
        # system prompt; repeat the rules where the model can read them.
        agent.register_hook("process_all_messages_before_reply",
                            lambda messages: fold_rules_into_last(view(messages), rules))
    else:
        agent.register_hook("process_all_messages_before_reply", view)
    if endpoint_resolver is not None:
        agent.register_reply([ConversableAgent, None], make_resilient_reply(endpoint_resolver, should_stop),
                             position=0)
    return agent


def fold_rules_into_last(messages, rules):
    """Return messages whose final entry carries the agent rules inline."""
    if not messages or not rules:
        return messages
    last = dict(messages[-1])
    last["content"] = ("OPERATING RULES (always apply):\n" + rules.strip() + "\n\n" +
                       "=" * 60 + "\n" + str(last.get("content") or ""))
    return [*messages[:-1], last]


REASONING_MODELS = ("gpt-oss", "deepseek-r1", "qwq", "qwen3", "magistral", "phi4-reasoning")


def tool_calls_to_blocks(reply):
    """Turn a native tool call into the code block the runner understands.

    Models trained with built-in tools (gpt-oss: container.exec / python) may
    call a tool that was never offered, returning no text. An empty reply ends
    the conversation, so express the call as a ```bash block instead.
    """
    import json as _json
    import shlex as _shlex
    get = (lambda o, k: o.get(k)) if isinstance(reply, dict) else (lambda o, k: getattr(o, k, None))
    calls = get(reply, "tool_calls") if reply is not None and not isinstance(reply, str) else None
    if not calls or (get(reply, "content") or "").strip():
        return reply
    blocks = []
    for call in calls:
        fn = get(call, "function") or {}
        fget = (lambda k: fn.get(k)) if isinstance(fn, dict) else (lambda k: getattr(fn, k, None))
        try:
            args = _json.loads(fget("arguments") or "{}")
        except ValueError:
            args = {"input": fget("arguments") or ""}
        name = str(fget("name") or "")
        path = args.get("path") or args.get("file") or args.get("filename") or args.get("file_path")
        if path and not (args.get("cmd") or args.get("command") or args.get("code")):
            # File viewers (e.g. repo_browser.open_file) become the runner's read block.
            lo = args.get("line_start") or args.get("start_line") or args.get("start")
            hi = args.get("line_end") or args.get("end_line") or args.get("end")
            ref = f"{path}:{int(lo)}-{int(hi)}" if lo and hi else str(path)
            blocks.append("```read\n" + ref + "\n```")
            continue
        query = args.get("query") or args.get("pattern") or args.get("regex")
        if query and not (args.get("cmd") or args.get("command")):
            blocks.append("```bash\ngrep -rn " + _shlex.quote(str(query)) + " .\n```")
            continue
        if not args and any(k in name.lower() for k in ("tree", "list", "ls")):
            blocks.append("```bash\nls -R\n```")
            continue
        cmd = args.get("cmd") or args.get("command") or args.get("input")
        code = args.get("code")
        if isinstance(cmd, list):
            cmd = cmd[2] if len(cmd) >= 3 and cmd[0] in ("bash", "sh") and cmd[1] in ("-c", "-lc") else _shlex.join(cmd)
        if code and not cmd:
            cmd = "python - <<'PYEOF'\n" + str(code) + "\nPYEOF"
        if cmd:
            blocks.append("```bash\n" + str(cmd).strip() + "\n```")
    if not blocks:
        names = ", ".join(str((get(c, "function") or {}).get("name") if isinstance(get(c, "function"), dict)
                              else getattr(get(c, "function"), "name", "?")) for c in calls)
        # Never let an unknown tool call end the conversation with an empty reply.
        return (f"(Tried to call unavailable tool(s): {names}. Use ```read, ```bash or ```python code blocks "
                "instead.)")
    NodeLog.emit("TOOL_CALL_CONVERTED", {"calls": len(calls)})
    return "\n\n".join(blocks)


def is_malformed_reply(exc) -> bool:
    """A 200 response carrying an error body instead of choices: autogen fails
    iterating the missing choices, or the parsed completion has no choices."""
    text = str(exc)
    return (isinstance(exc, (TypeError, AttributeError)) and "NoneType" in text
            and ("iterable" in text or "choices" in text or "subscriptable" in text))


def is_endpoint_outage(exc) -> bool:
    """Transport failures worth waiting out (tunnel down, gateway errors, timeouts)."""
    try:
        import openai
    except ImportError:  # pragma: no cover
        return False
    # autogen re-raises openai timeouts as the built-in TimeoutError.
    if isinstance(exc, (openai.APIConnectionError, openai.APITimeoutError, TimeoutError)):
        return True
    # A reply cut off mid-stream (e.g. tunnel dropped after the bridge's
    # whitespace heartbeat) arrives as unparseable JSON: retry, don't fail the run.
    import json as _json
    if isinstance(exc, _json.JSONDecodeError) or (isinstance(exc, ValueError) and "Expecting value" in str(exc)):
        return True
    return isinstance(exc, openai.APIStatusError) and getattr(exc, "status_code", 0) in {502, 503, 504, 520, 521,
                                                                                          522, 523, 524, 530}


def make_resilient_reply(endpoint_resolver, should_stop=None, wait=None, logger=None):
    """Wrap the default LLM reply so an endpoint outage pauses instead of failing.

    ``endpoint_resolver`` returns the current base URL (re-read from config);
    a changed URL rebuilds the agent's client in place.
    """
    from node_core.endpoint import wait_for_endpoint
    wait = wait or wait_for_endpoint
    logger = logger or safe_console_print

    def apply_temperature_hint(recipient):
        workflow = getattr(recipient, "workflow_ref", None)
        if workflow is None:
            return
        cfg = recipient.llm_config
        base = cfg.setdefault("_base_temperature", cfg.get("temperature", 0.2))
        base_tokens = cfg.setdefault("_base_max_tokens", cfg.get("max_tokens"))
        wanted = getattr(workflow, "temperature_hint", None) or base
        wanted_tokens = getattr(workflow, "max_tokens_hint", None) or base_tokens
        model = str((cfg.get("config_list") or [{}])[0].get("model", "")).lower()
        if wanted_tokens and any(tag in model for tag in REASONING_MODELS):
            # Reasoning models spend part of max_tokens thinking before they answer.
            wanted_tokens = max(wanted_tokens, 6144)
        if cfg.get("temperature") != wanted or cfg.get("max_tokens") != wanted_tokens:
            from autogen import OpenAIWrapper
            cfg["temperature"] = wanted
            if wanted_tokens is not None:
                cfg["max_tokens"] = wanted_tokens
                for entry in cfg.get("config_list", []):
                    entry["max_tokens"] = wanted_tokens
            recipient.client = OpenAIWrapper(**{k: v for k, v in cfg.items() if not k.startswith("_")})
            NodeLog.emit("SAMPLING_CHANGED", {"temperature": wanted, "max_tokens": wanted_tokens})

    def interruptible_reply(recipient, messages, sender, config):
        """Run the (possibly minutes-long) model request in a worker thread so a
        stop request takes effect within half a second instead of after the reply."""
        import threading
        box = {}

        def work():
            try:
                box["result"] = recipient.generate_oai_reply(messages, sender, config)
            except BaseException as exc:  # re-raised in the caller's thread
                box["error"] = exc

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        while worker.is_alive():
            worker.join(0.5)
            if should_stop and should_stop():
                workflow = getattr(recipient, "workflow_ref", None)
                if workflow is not None and getattr(workflow, "status", None) == "IN_PROGRESS":
                    workflow.stop("CANCELLED", "User requested cancellation")
                NodeLog.emit("TASK_CANCELLED", {"during": "model request"})
                return None  # the abandoned request finishes (or times out) on its own
        if "error" in box:
            raise box["error"]
        return box["result"]

    malformed = [0]  # consecutive replies that arrived without a completion

    def resilient_reply(recipient, messages=None, sender=None, config=None):
        apply_temperature_hint(recipient)
        while True:
            try:
                outcome = interruptible_reply(recipient, messages, sender, config)
                if outcome is None:
                    return True, None  # stopped by the user: end the conversation now
                final, reply = outcome
                malformed[0] = 0
                reply = tool_calls_to_blocks(reply)
                if reply is None or (isinstance(reply, str) and not reply.strip()):
                    # An empty answer (e.g. all tokens spent reasoning) would end the chat.
                    reply = "(The reply contained no answer text.)"
                return final, reply
            except Exception as exc:
                if is_malformed_reply(exc):
                    # The server answered with an error body instead of a reply (e.g. the
                    # model process failed mid-request). Retry a few times, then report.
                    malformed[0] += 1
                    NodeLog.emit("MALFORMED_REPLY", {"error": repr(exc)[:300], "count": malformed[0]})
                    if malformed[0] >= 3:
                        raise RuntimeError("The model server returned an error instead of a reply 3 times in a "
                                           "row; check the model server's log (for Colab: the bridge cell "
                                           "output lines starting with '!!! bridge error').") from exc
                elif not is_endpoint_outage(exc):
                    raise
                NodeLog.emit("ENDPOINT_OUTAGE", {"error": repr(exc)[:300]})
                model = recipient.llm_config["config_list"][0].get("model", "")
                url = wait(endpoint_resolver, model, should_stop=should_stop, logger=logger)
                if url is None:
                    raise
                current = recipient.llm_config["config_list"][0].get("base_url", "").rstrip("/")
                new_base = url.rstrip("/") if url.rstrip("/").endswith("/v1") else url.rstrip("/") + "/v1"
                if new_base != current:
                    from autogen import OpenAIWrapper
                    for entry in recipient.llm_config["config_list"]:
                        entry["base_url"] = new_base
                    recipient.client = OpenAIWrapper(**{k: v for k, v in recipient.llm_config.items()
                                                        if not k.startswith("_")})
                    NodeLog.emit("ENDPOINT_SWITCHED", {"base_url": new_base})
                    config = None
    return resilient_reply


def create_user_proxy_runner(
    name: str = "UserProxyRunner",
    workspace_path: Optional[str] = None,
    max_rounds: int = 30,
    task_prompt: str = "",
    acceptance: Optional[Dict[str, Any]] = None,
    should_stop=None,
    incremental: bool = False,
    resume: bool = False,
) -> UserProxyAgent:
    """Keep AutoGen transport while delegating phase evidence to TaskWorkflow."""
    from node_core.workflow import TaskWorkflow
    workflow = TaskWorkflow(workspace_path or ".", task_prompt,
                            detect_project_profile(task_prompt), max_rounds,
                            acceptance=acceptance, should_stop=should_stop,
                            incremental=incremental, resume=resume)
    user_proxy = UserProxyAgent(
        name=name, human_input_mode="NEVER", max_consecutive_auto_reply=max_rounds,
        is_termination_msg=lambda message: False, code_execution_config=False,
    )
    user_proxy.workflow = workflow

    def custom_execution_reply(recipient, messages=None, sender=None, config=None):
        messages = messages if messages is not None else recipient._oai_messages[sender]
        # AutoGen's bounded loop can ask for another proxy reply even when the
        # coder returned None. Never execute our own preceding prompt as code.
        if not messages or messages[-1].get("role") != "user":
            workflow.reason = "Conversation ended before verification completed"
            return True, None
        if not workflow.task and messages:
            workflow.task = messages[0].get("content", "") or ""
            workflow.profile = detect_project_profile(workflow.task)
        content = messages[-1].get("content", "") if messages else ""
        return True, workflow.consume_messages(messages)

    user_proxy.register_reply([ConversableAgent, None], custom_execution_reply, position=0)
    return user_proxy

def create_user_proxy_agent(
    name: str = "UserProxyRunner",
    work_dir: Optional[str] = None
) -> UserProxyAgent:
    """Alias for create_user_proxy_runner."""
    return create_user_proxy_runner(name=name, workspace_path=work_dir)


def create_architect_agent(llm_config: Dict[str, Any]) -> ConversableAgent:
    return ConversableAgent(
        name="ArchitectAgent",
        system_message="""You are the Lead Architect Agent.""",
        llm_config=llm_config,
    )


def create_dev_agent(llm_config: Dict[str, Any]) -> ConversableAgent:
    return ConversableAgent(
        name="DevAgent",
        system_message="""You are the Software Engineer Agent (DevAgent).""",
        llm_config=llm_config,
    )


def create_pulse_agent(llm_config: Dict[str, Any]) -> ConversableAgent:
    return ConversableAgent(
        name="PulseAgent",
        system_message="""You are the Verification & QA Agent (PulseAgent).""",
        llm_config=llm_config,
    )


def create_link_agent(llm_config: Dict[str, Any]) -> ConversableAgent:
    return ConversableAgent(
        name="LinkAgent",
        system_message="""You are the Cloud & MLOps Agent (LinkAgent).""",
        llm_config=llm_config,
    )


def create_supervisor_agent(llm_config: Dict[str, Any]) -> ConversableAgent:
    return ConversableAgent(
        name="NodeCoreSupervisor",
        system_message="""You are the Supervisor / Controller Agent.""",
        llm_config=llm_config,
    )


def register_node_tools(
    caller_agents: Union[ConversableAgent, List[ConversableAgent]],
    executor_agent: UserProxyAgent,
    tools_dict: Dict[str, Any]
) -> None:
    """Register all toolchain functions with callers (LLMs) and executor (UserProxy)."""
    callers = caller_agents if isinstance(caller_agents, list) else [caller_agents]
    for name, func in tools_dict.items():
        executor_agent.register_for_execution(name=name)(func)
    for caller in callers:
        for name, func in tools_dict.items():
            caller.register_for_llm(name=name, description=func.__doc__ or f"Execute {name}")(func)
