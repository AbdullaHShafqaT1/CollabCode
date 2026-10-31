"""A compact, navigable map of a Python project for repair prompts.

Instead of pasting whole files (large prompts make models, especially reasoning
models, spend their budget re-reading noise), a repair prompt carries one line
per function: where it is, its signature, what it calls and what calls it,
plus structural red flags. The model then asks for exactly the functions it
needs with a ``read`` block such as ``ludo_gui.py::LudoGUI.__init__``.

Everything here is derived from the syntax tree; nothing is project-specific.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)


@dataclass
class Entry:
    file: str
    qualname: str          # Class.method, func, or Class.method.inner for nested functions
    name: str
    start: int
    end: int
    signature: str
    doc: str
    owner_class: Optional[str]
    nested_in: Optional[str]  # qualname of the enclosing function, if nested
    calls: List[str] = field(default_factory=list)       # short names called
    self_calls: List[str] = field(default_factory=list)  # self.<name>(...) calls
    called_by: List[str] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)


def _signature(node) -> str:
    try:
        args = ast.unparse(node.args)
    except Exception:  # pragma: no cover - very old/odd syntax
        args = "..."
    returns = f" -> {ast.unparse(node.returns)}" if getattr(node, "returns", None) is not None else ""
    return f"{node.name}({args}){returns}"


def _is_stub(node) -> bool:
    body = [s for s in node.body if not (isinstance(s, ast.Expr) and isinstance(getattr(s, "value", None), ast.Constant)
                                         and isinstance(s.value.value, str))]
    if not body:
        return True
    if len(body) == 1:
        stmt = body[0]
        if isinstance(stmt, ast.Pass):
            return True
        if isinstance(stmt, ast.Raise) and stmt.exc is not None:
            exc = stmt.exc.func if isinstance(stmt.exc, ast.Call) else stmt.exc
            return isinstance(exc, ast.Name) and exc.id == "NotImplementedError"
    return False


def build(sources: Dict[str, str]) -> List[Entry]:
    """Parse every source and return one Entry per function (any nesting depth)."""
    entries: List[Entry] = []
    class_methods: Dict[str, set] = {}
    class_attrs: Dict[str, set] = {}
    inherits: set = set()  # classes with base classes may inherit methods we cannot see

    def visit(node, file, prefix, owner_class, nested_in):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                class_methods.setdefault(child.name, set()).update(
                    i.name for i in child.body if isinstance(i, FUNCS))
                if any(not (isinstance(b, ast.Name) and b.id == "object") for b in child.bases):
                    inherits.add(child.name)
                visit(child, file, child.name, child.name, None)
            elif isinstance(child, FUNCS):
                qual = f"{prefix}.{child.name}" if prefix else child.name
                doc = (ast.get_docstring(child) or "").strip().splitlines()
                entry = Entry(file, qual, child.name, child.lineno, child.end_lineno or child.lineno,
                              _signature(child), doc[0][:90] if doc else "", owner_class, nested_in)
                for sub in ast.walk(child):
                    if isinstance(sub, ast.Call):
                        func = sub.func
                        if isinstance(func, ast.Attribute):
                            if isinstance(func.value, ast.Name) and func.value.id == "self":
                                entry.self_calls.append(func.attr)
                            entry.calls.append(func.attr)
                        elif isinstance(func, ast.Name):
                            entry.calls.append(func.id)
                    elif isinstance(sub, (ast.Assign, ast.AnnAssign)) and owner_class:
                        targets = sub.targets if isinstance(sub, ast.Assign) else [sub.target]
                        for t in targets:
                            if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self":
                                class_attrs.setdefault(owner_class, set()).add(t.attr)
                if _is_stub(child):
                    entry.flags.append("STUB (not implemented)")
                entries.append(entry)
                # Functions defined inside a function are local helpers, not methods.
                visit(child, file, qual, owner_class, qual)

    for file, source in sources.items():
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            entries.append(Entry(file, "<syntax error>", "<syntax error>", exc.lineno or 0, exc.lineno or 0,
                                 "", str(exc)[:90], None, None, flags=["SYNTAX ERROR"]))
            continue
        visit(tree, file, "", None, None)

    by_name: Dict[str, List[Entry]] = {}
    for e in entries:
        by_name.setdefault(e.name, []).append(e)
    for e in entries:
        e.calls = sorted({c for c in e.calls if c in by_name and c != e.name})
        for callee in e.calls:
            for target in by_name[callee]:
                target.called_by.append(e.qualname)
        if e.nested_in:
            e.flags.append(f"NESTED inside {e.nested_in} (a local helper, NOT a method; self.{e.name}() cannot reach it)")
        if e.owner_class:
            known = class_methods.get(e.owner_class, set()) | class_attrs.get(e.owner_class, set())
            for name in sorted(set(e.self_calls)):
                if name not in known and not name.startswith("__"):
                    nested = [x.qualname for x in by_name.get(name, []) if x.nested_in]
                    if e.owner_class in inherits and not nested:
                        continue  # probably inherited from a base class
                    hint = f" (only defined NESTED as {nested[0]})" if nested else ""
                    e.flags.append(f"calls self.{name}() but {e.owner_class} has no method {name}{hint}")
    for e in entries:
        e.called_by = sorted(set(e.called_by))
    return entries


def _line(e: Entry) -> str:
    args = e.signature[len(e.name):]  # "(self, row, col) -> list"
    parts = [f"  {e.start}-{e.end} {e.qualname}{args}"]
    if e.calls:
        parts.append("-> " + ", ".join(e.calls[:5]) + ("..." if len(e.calls) > 5 else ""))
    if e.called_by:
        parts.append("<- " + ", ".join(c.rsplit(".", 1)[-1] for c in e.called_by[:3])
                     + ("..." if len(e.called_by) > 3 else ""))
    if e.doc:
        parts.append(f'"{e.doc[:60]}"')
    line = "  ".join(parts)
    if e.flags:
        line += "  <<< " + "; ".join(e.flags)
    return line


def render(entries: List[Entry], limit: int = 5000) -> str:
    """Grouped by file, one line per function; structural problems listed first
    so they are never trimmed. Legend: -> calls, <- called by."""
    out = ["CODE MAP (lines function(args)  -> calls  <- called by):"]
    flagged = [e for e in entries if e.flags]
    if flagged:
        out.append("STRUCTURAL PROBLEMS FOUND:")
        out.extend(f"{e.file}:{_line(e).strip()}" for e in flagged)
    current = None
    for e in entries:
        if e.file != current:
            current = e.file
            out.append(f"{e.file}:")
        out.append(_line(e) if not e.flags else f"  {e.start}-{e.end} {e.qualname}  (see problems above)")
    text = "\n".join(out)
    return text if len(text) <= limit else text[:limit] + "\n  [map truncated]"


def source_of(sources: Dict[str, str], selector: str) -> Optional[str]:
    """Numbered source of the function named by `file::Qual.name`, `Qual.name` or `name`."""
    file_part, _, name_part = selector.partition("::") if "::" in selector else ("", "", selector)
    matches = [e for e in build({k: v for k, v in sources.items() if not file_part or k == file_part})
               if e.qualname == name_part or e.name == name_part or e.qualname.endswith("." + name_part)]
    if not matches:
        return None
    parts = []
    for e in matches[:3]:
        text = sources[e.file].splitlines()[e.start - 1:e.end]
        numbered = "\n".join(f"{e.start + i:4d}  {line}" for i, line in enumerate(text))
        parts.append(f"FUNCTION {e.file}::{e.qualname} (lines {e.start}-{e.end}):\n{numbered}")
    return "\n\n".join(parts)


SELECTOR = re.compile(r"^(?:[\w./-]+\.py::)?[A-Za-z_][\w.]*$")
