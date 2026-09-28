"""Check generated code against the literal values the task spells out.

Models often invent a spelling of a task-defined value (e.g. the task says the
turn is 'W' but the code tests ``color == 'w'``), and their own tests then use
the same invented spelling, so nothing fails. This check finds string values
passed to, or compared inside, the functions the task names, that differ from
a task literal only in letter case and never appear in the task themselves.
"""
from __future__ import annotations

import ast
import re
from typing import Dict, List

# Names that look like calls in prose but are not part of a project API.
_NOT_API = {"print", "range", "len", "open", "str", "int", "list", "dict", "set", "tuple", "main"}


def task_literals(task: str) -> set:
    return {a or b for a, b in re.findall(r"'([^'\n]{1,24})'|\"([^\"\n]{1,24})\"", task)}


def task_functions(task: str) -> set:
    names = set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\(", task))
    return {n for n in names if n not in _NOT_API and (("_" in n) or len(n) >= 5)}


def _context(task: str, literal: str) -> str:
    for quote in ("'", '"'):
        i = task.find(quote + literal + quote)
        if i >= 0:
            snippet = task[max(0, i - 40):i + len(literal) + 42].replace("\n", " ").strip()
            return snippet
    return ""


def _variant(value, literals: set):
    """The task literal that `value` misspells by case only, else None."""
    if not isinstance(value, str) or not value or value in literals:
        return None
    for literal in sorted(literals):
        if literal != value and literal.lower() == value.lower():
            return literal
    return None


def check(task: str, sources: Dict[str, str], limit: int = 8) -> List[str]:
    """Return human-readable convention issues for the given project sources."""
    literals, functions = task_literals(task), task_functions(task)
    if not literals or not functions:
        return []
    issues, seen = [], set()

    def report(name, node, value, literal, what):
        key = (name, getattr(node, "lineno", 0), value)
        if key in seen:
            return
        seen.add(key)
        where = _context(task, literal)
        issues.append(f"{name}:{node.lineno}: {what} uses {value!r}, but the task spells this value {literal!r}"
                      + (f' (task: "...{where}...")' if where else "")
                      + f". Use {literal!r} as the task specifies.")

    for name, source in sources.items():
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
                if func in functions:
                    for arg in list(node.args) + [k.value for k in node.keywords]:
                        for const in (n for n in ast.walk(arg) if isinstance(n, ast.Constant)):
                            literal = _variant(const.value, literals)
                            if literal:
                                report(name, node, const.value, literal, f"call to {func}()")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in functions:
                params = {a.arg for a in node.args.args + node.args.kwonlyargs} - {"self", "cls"}
                for comp in (n for n in ast.walk(node) if isinstance(n, ast.Compare)):
                    sides = [comp.left] + list(comp.comparators)
                    names = [s.id for s in sides if isinstance(s, ast.Name) and s.id in params]
                    consts = [s.value for s in sides if isinstance(s, ast.Constant)]
                    for value in consts:
                        literal = _variant(value, literals)
                        if names and literal:
                            report(name, comp, value, literal, f"{node.name}() comparing parameter `{names[0]}`")
        if len(issues) >= limit:
            break
    return issues[:limit]
