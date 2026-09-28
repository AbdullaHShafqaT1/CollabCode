"""Re-run failing unit tests and report the project calls they made.

Usage: python behavior_trace.py <workspace> <test_module>
Prints, per failing test, the calls from the test into workspace code with
their arguments and return values, so a small model sees concrete behaviour
("make_move((6, 4), (5, 4)) -> False") instead of only "False is not true".
"""
import io
import linecache
import os
import sys
import unittest

MAX_CALLS, MAX_REPR = 14, 110


def short(value):
    try:
        text = repr(value)
    except Exception:
        text = f"<{type(value).__name__}>"
    return text if len(text) <= MAX_REPR else text[:MAX_REPR - 3] + "..."


def scalar_values(frame, limit=8):
    """Simple locals and self attributes the function uses, at its return."""
    simple = (str, int, float, bool, type(None))
    shown = []
    for name, value in frame.f_locals.items():
        if name != "self" and isinstance(value, simple):
            shown.append(f"{name}={short(value)}")
    owner = frame.f_locals.get("self")
    if owner is not None:
        for name in frame.f_code.co_names:
            value = getattr(owner, name, None) if name in getattr(owner, "__dict__", {}) else None
            if name in getattr(owner, "__dict__", {}) and isinstance(value, simple):
                shown.append(f"self.{name}={short(value)}")
    return ", ".join(dict.fromkeys(shown[:limit]))


def explain_branch(frame):
    """Evaluate the `if` condition that led to this return, operand by operand.

    Turns "returned at line 121" into "`piece[0] != self.current_turn` was
    'w' != 'W' -> True", the step small models are worst at doing themselves.
    Only names, attributes, subscripts and constants are evaluated (no calls).
    """
    import ast
    code = frame.f_code
    try:
        source = linecache.getlines(code.co_filename)
        tree = ast.parse("".join(source))
    except (SyntaxError, ValueError):
        return ""
    line = frame.f_lineno
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and node.lineno <= line <= (node.end_lineno or node.lineno):
            in_body = any(s.lineno <= line <= (s.end_lineno or s.lineno) for s in node.body)
            if in_body and (target is None or node.lineno > target.lineno):
                target = node
    skipped = False
    if target is None:
        # `return` right after an `if` that was not entered: that condition was false.
        for node in ast.walk(tree):
            for field in ("body", "orelse"):
                block = getattr(node, field, None)
                if not isinstance(block, list):
                    continue
                for prev, stmt in zip(block, block[1:]):
                    if isinstance(stmt, ast.Return) and stmt.lineno == line and isinstance(prev, ast.If):
                        target, skipped = prev, True
    if target is None:
        return ""

    def safe(node):
        return all(isinstance(n, (ast.Name, ast.Attribute, ast.Subscript, ast.Constant, ast.Load,
                                  ast.Index if hasattr(ast, "Index") else ast.Constant, ast.Slice,
                                  ast.UnaryOp, ast.USub, ast.Tuple))
                   for n in ast.walk(node))

    def value(node):
        if not safe(node):
            raise ValueError("unsafe")
        return eval(compile(ast.Expression(node), "<cond>", "eval"), frame.f_globals, dict(frame.f_locals))

    parts = []
    for comp in (n for n in ast.walk(target.test) if isinstance(n, ast.Compare)):
        try:
            operands = [value(comp.left)] + [value(c) for c in comp.comparators]
            result = value(comp) if safe(comp) else eval(compile(ast.Expression(comp), "<c>", "eval"),
                                                        frame.f_globals, dict(frame.f_locals))
        except Exception:
            continue
        ops = [type(o).__name__ for o in comp.ops]
        symbols = {"Eq": "==", "NotEq": "!=", "Lt": "<", "LtE": "<=", "Gt": ">", "GtE": ">=", "In": "in",
                   "NotIn": "not in", "Is": "is", "IsNot": "is not"}
        shown = short(operands[0]) + "".join(f" {symbols.get(o, o)} {short(v)}" for o, v in zip(ops, operands[1:]))
        text = f"`{ast.unparse(comp)}` evaluated as {shown} -> {result}"
        if len(operands) == 2 and ops[0] in ("Eq", "NotEq"):
            a, b = operands
            if isinstance(a, str) and isinstance(b, str) and a != b and a.lower() == b.lower():
                text += f". NOTE: {short(a)} and {short(b)} differ only in letter case, and == is case-sensitive"
            elif type(a) is not type(b) and str(a) == str(b):
                text += (f". NOTE: {short(a)} ({type(a).__name__}) and {short(b)} ({type(b).__name__}) "
                         "look alike but have different types, so they are never equal")
        parts.append(text)
    where = (f"(`if {ast.unparse(target.test)}:` at line {target.lineno} was not entered, so execution "
             f"fell through to this return)" if skipped else
             f"(branch `if {ast.unparse(target.test)}:` at line {target.lineno})")
    return ("; ".join(parts) + " " + where) if parts else ""


def tests_of(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from tests_of(item)
        else:
            yield item


def trace(test, workspace, test_file):
    """Record test -> project calls and one level of project -> project calls."""
    calls, pending = [], {}

    def profiler(frame, event, arg):
        code = frame.f_code
        if not code.co_filename.startswith(workspace) or code.co_filename == test_file:
            return
        caller = frame.f_back
        if caller is None:
            return
        if event == "call":
            if caller.f_code.co_filename == test_file:
                depth = 1
            elif id(caller) in pending and pending[id(caller)][0] == 1:
                depth = 2
            else:
                return
            names = code.co_varnames[:code.co_argcount]
            args = [short(frame.f_locals.get(n)) for n in names if n not in ("self", "cls")]
            owner = frame.f_locals.get("self")
            label = (type(owner).__name__ + "." if owner is not None else "") + code.co_name
            slot = len(calls)
            calls.append(None)
            pending[id(frame)] = (depth, f"{label}({', '.join(args)})", slot)
        elif event == "return" and id(frame) in pending:
            depth, text, slot = pending.pop(id(frame))
            where = ""
            if depth == 1:
                # Which line produced the result tells a model which branch fired.
                line = linecache.getline(code.co_filename, frame.f_lineno).strip()
                where = f"   [returned at {os.path.basename(code.co_filename)}:{frame.f_lineno}: {line[:80]}]"
                values = scalar_values(frame)
                if values:
                    where += f"   [values there: {values}]"
                why = explain_branch(frame)
                if why:
                    where += f"\n        WHY: {why}"
            calls[slot] = "    " * (depth - 1) + f"{text} -> {short(arg)}{where}"

    result = unittest.TestResult()
    sys.setprofile(profiler)
    try:
        test.run(result)
    finally:
        sys.setprofile(None)
    for depth, text, slot in pending.values():
        calls[slot] = "    " * (depth - 1) + f"{text} raised an exception"
    return [c for c in calls if c]


def coverage(test, workspace, test_file):
    """Qualified names of project functions a test executes (any call depth)."""
    seen = set()

    def profiler(frame, event, arg):
        if event == "call":
            code = frame.f_code
            if code.co_filename.startswith(workspace) and code.co_filename != test_file \
                    and not code.co_name.startswith("<"):
                seen.add(getattr(code, "co_qualname", code.co_name))

    sys.setprofile(profiler)
    try:
        test.run(unittest.TestResult())
    finally:
        sys.setprofile(None)
    return seen


def suspiciousness(runs):
    """Ochiai spectrum-based fault localisation over [(failed, functions)].

    score(f) = failing tests running f / sqrt(all failing tests * all tests running f).
    Helpers used by passing tests too rank low; code mostly run by failing tests ranks high.
    """
    import math
    total_failed = sum(1 for failed, _ in runs if failed)
    if not total_failed:
        return []
    names = set().union(*(funcs for _, funcs in runs)) if runs else set()
    scores = []
    for name in names:
        ef = sum(1 for failed, funcs in runs if failed and name in funcs)
        ep = sum(1 for failed, funcs in runs if not failed and name in funcs)
        if ef:
            scores.append((ef / math.sqrt(total_failed * (ef + ep)), name))
    return sorted(scores, key=lambda item: (-item[0], item[1]))


def main():
    workspace, module = os.path.realpath(sys.argv[1]), sys.argv[2]
    os.chdir(workspace)
    sys.path.insert(0, workspace)
    suite = unittest.defaultTestLoader.loadTestsFromName(module)
    test_file = os.path.realpath(sys.modules[module].__file__)
    lines, runs = [], []
    for test in tests_of(suite):
        result = unittest.TestResult()
        test.run(result)
        failed = result.failures + result.errors
        runs.append((bool(failed), coverage(test, workspace, test_file)))
        if not failed:
            continue
        message = failed[0][1].strip().splitlines()[-1]
        calls = trace(test, workspace, test_file)
        lines.append(f"{test.id().split('.')[-1]}: {message}")
        lines.extend("    " + call for call in calls[-MAX_CALLS:])
        if not calls:
            lines.append("    (no calls into project code were recorded)")
    ranked = [(score, name) for score, name in suspiciousness(runs)
              if not (name.rsplit(".", 1)[-1].startswith("__") and name.endswith("__"))]  # skip setup dunders
    if ranked:
        lines.insert(0, "SUSPICIOUS (most likely buggy first, by how much more failing tests run them than "
                        "passing ones): " + ", ".join(f"{name}={score:.2f}" for score, name in ranked[:6]))
    sys.stdout = sys.__stdout__
    print("\n".join(lines))


if __name__ == "__main__":
    sys.stdout = io.StringIO()  # silence prints from project code while loading
    main()
