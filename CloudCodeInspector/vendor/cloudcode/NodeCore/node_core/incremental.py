"""Method-at-a-time assembly of Python files for short-reply model endpoints.

A file is first produced as a skeleton whose functions are stubs
(``raise NotImplementedError``). Each later reply implements one stub; the
definitions are spliced into the draft by line range, preserving everything
else byte-for-byte. The same splice patches individual methods of an existing
file during repair, so a fix never requires re-sending the whole file.
"""
from __future__ import annotations

import ast
import textwrap
from typing import List, Optional, Tuple

FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)


def is_stub(node) -> bool:
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    if not body:
        return True
    if len(body) != 1:
        return False
    stmt = body[0]
    if isinstance(stmt, ast.Pass):
        return True
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and stmt.value.value is Ellipsis:
        return True
    if isinstance(stmt, ast.Raise) and stmt.exc is not None:
        exc = stmt.exc.func if isinstance(stmt.exc, ast.Call) else stmt.exc
        return isinstance(exc, ast.Name) and exc.id == "NotImplementedError"
    return False


def _scopes(tree):
    """Yield (qualname, node) for module functions and class methods in source order."""
    for node in tree.body:
        if isinstance(node, FUNCS):
            yield node.name, node
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, FUNCS):
                    yield f"{node.name}.{item.name}", item


def find_stubs(code: str) -> List[str]:
    return [qual for qual, node in _scopes(ast.parse(code)) if is_stub(node)]


def definitions(code: str) -> List[str]:
    return [qual for qual, _ in _scopes(ast.parse(code))]


def _start(node) -> int:
    return min([node.lineno] + [d.lineno for d in node.decorator_list])


def _segment(lines: List[str], node) -> List[str]:
    return lines[_start(node) - 1:node.end_lineno]


def _reindent(block: List[str], indent: int) -> List[str]:
    text = textwrap.dedent("\n".join(block))
    return [(" " * indent + line) if line.strip() else "" for line in text.splitlines()]


def parse_reply(reply: str) -> Tuple[List[str], list, List[str]]:
    """Return (reply_lines, [(owner_class_or_None, def_node)], import_lines)."""
    text = textwrap.dedent(reply.strip("\n"))
    tree = ast.parse(text)
    lines = text.splitlines()
    defs, imports = [], []
    for node in tree.body:
        if isinstance(node, FUNCS):
            defs.append((None, node))
        elif isinstance(node, ast.ClassDef):
            defs.extend((node.name, item) for item in node.body if isinstance(item, FUNCS))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.extend(lines[node.lineno - 1:node.end_lineno])
    return lines, defs, imports


def only_definitions(reply: str) -> bool:
    """True when a reply is a set of def blocks (optionally with imports or a
    wrapping class of methods), i.e. a patch rather than a whole module."""
    try:
        tree = ast.parse(textwrap.dedent(reply.strip("\n")))
    except SyntaxError:
        return False
    kinds = [type(n) for n in tree.body]
    has_def = any(k in FUNCS for k in kinds) or any(
        isinstance(n, ast.ClassDef) and all(isinstance(i, FUNCS) or isinstance(i, ast.Expr) for i in n.body)
        for n in tree.body)
    return has_def and all(k in FUNCS or k in (ast.Import, ast.ImportFrom, ast.ClassDef) for k in kinds)


def splice(draft: str, reply: str, target: Optional[str] = None,
           replace_implemented: bool = False) -> Tuple[str, List[str]]:
    """Splice reply definitions into draft; return (new_code, changed_qualnames).

    target: qualname that must be defined by the reply (method-at-a-time step).
    New helper definitions are inserted right after the target (or after the
    first replaced definition). Already-implemented definitions other than the
    target are kept unless replace_implemented is set (repair patches).
    Raises ValueError/SyntaxError when the reply cannot be applied.
    """
    draft_lines = draft.splitlines()
    tree = ast.parse(draft)
    existing = dict(_scopes(tree))
    reply_lines, defs, imports = parse_reply(reply)
    if not defs:
        raise ValueError("Reply contains no function or method definition")

    def resolve(owner, name):
        if owner and f"{owner}.{name}" in existing:
            return f"{owner}.{name}"
        if target and target.rsplit(".", 1)[-1] == name:
            return target
        matches = [q for q in existing if q.rsplit(".", 1)[-1] == name]
        return matches[0] if len(matches) == 1 else (f"{owner}.{name}" if owner else None)

    anchor = target
    replacements, additions = {}, []
    for owner, node in defs:
        qual = resolve(owner, node.name)
        segment = _segment(reply_lines, node)
        if qual in existing:
            if qual == target or is_stub(existing[qual]) or replace_implemented:
                replacements[qual] = segment
                anchor = anchor or qual
        else:
            additions.append(segment)
    if target and target not in replacements:
        raise ValueError(f"Reply did not define {target.rsplit('.', 1)[-1]}")
    edits = []
    if not replacements:
        # Only new definitions: accept them when the file already calls them,
        # inserting each right after its caller.
        callers = {name: f"{cls}.{caller}" for cls, name, caller, _ in undefined_self_calls(draft)}
        placed = []
        for owner, node in defs:
            caller = callers.get(node.name)
            if caller in existing:
                host = existing[caller]
                edits.append((host.end_lineno, host.end_lineno,
                              [""] + _reindent(_segment(reply_lines, node), host.col_offset)))
                placed.append(f"{caller.split('.')[0]}.{node.name}")
        if not placed:
            raise ValueError("Reply definitions do not match any definition in the file")
        for start, end, new in sorted(edits, reverse=True):
            draft_lines[start:end] = new
        code = "\n".join(draft_lines) + "\n"
        compile(code, "<draft>", "exec")
        return code, placed
    if anchor not in replacements:
        anchor = next(iter(replacements))

    for qual, segment in replacements.items():
        node = existing[qual]
        new = _reindent(segment, node.col_offset)
        if qual == anchor:
            for extra in additions:
                new += [""] + _reindent(extra, node.col_offset)
        edits.append((_start(node) - 1, node.end_lineno, new))
    for start, end, new in sorted(edits, reverse=True):
        draft_lines[start:end] = new

    present = {line.strip() for line in draft_lines}
    missing = [line for line in imports if line.strip() not in present]
    if missing:
        first = next((n.lineno - 1 for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))), 0)
        draft_lines[first:first] = missing
    code = "\n".join(draft_lines) + "\n"
    compile(code, "<draft>", "exec")
    return code, list(replacements) + [f"+{len(additions)} helper(s)"] * bool(additions)


def undefined_self_calls(code: str):
    """Return [(class, method, caller, call_node)] for self.<m>(...) calls to
    methods the class does not define (classes with explicit bases, which may
    inherit methods, are skipped)."""
    tree = ast.parse(code)
    found, seen = [], set()
    for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
        if any(not (isinstance(b, ast.Name) and b.id == "object") for b in cls.bases):
            continue
        defined = {i.name for i in cls.body if isinstance(i, FUNCS)}
        defined |= {t.id for i in cls.body if isinstance(i, ast.Assign) for t in i.targets if isinstance(t, ast.Name)}
        for method in (i for i in cls.body if isinstance(i, FUNCS)):
            assigned = {t.attr for n in ast.walk(method) if isinstance(n, (ast.Assign, ast.AnnAssign))
                        for t in (n.targets if isinstance(n, ast.Assign) else [n.target])
                        if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self"}
            for node in ast.walk(method):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name) and node.func.value.id == "self"):
                    name = node.func.attr
                    if name not in defined and name not in assigned and (cls.name, name) not in seen:
                        seen.add((cls.name, name))
                        found.append((cls.name, name, method.name, node))
    return found


def add_missing_stubs(code: str) -> Tuple[str, List[str]]:
    """Insert stubs for undefined self-method calls right after their caller."""
    missing = undefined_self_calls(code)
    if not missing:
        return code, []
    lines = code.splitlines()
    tree = ast.parse(code)
    methods = {(q.split(".")[0], q.split(".")[-1]): n for q, n in _scopes(tree) if "." in q}
    inserts = {}
    for cls, name, caller, call in missing:
        params = ["self"]
        for i, arg in enumerate(call.args):
            param = arg.id if isinstance(arg, ast.Name) and arg.id != "self" else f"arg{i + 1}"
            params.append(param if param not in params else f"{param}_{i + 1}")
        params += [kw.arg for kw in call.keywords if kw.arg and kw.arg not in params]
        anchor = methods[(cls, caller)]
        pad = " " * anchor.col_offset
        stub = ["", f"{pad}def {name}({', '.join(params)}):",
                f'{pad}    """Helper called by {caller}; implement what {caller} needs from it."""',
                f"{pad}    raise NotImplementedError"]
        inserts.setdefault(anchor.end_lineno, []).extend(stub)
    for end in sorted(inserts, reverse=True):
        lines[end:end] = inserts[end]
    new = "\n".join(lines) + "\n"
    compile(new, "<draft>", "exec")
    return new, [f"{cls}.{name}" for cls, name, _, _ in missing]


def outline(code: str, keep=("__init__",)) -> str:
    """Draft view for prompts: implemented bodies collapse to '...' so the
    model sees the API (signatures, docstrings, stubs) without copying it."""
    tree = ast.parse(code)
    lines = code.splitlines()
    cuts = []
    for qual, node in _scopes(tree):
        if node.name in keep or is_stub(node):
            continue
        body = node.body
        first = body[1] if (len(body) > 1 and isinstance(body[0], ast.Expr)
                            and isinstance(getattr(body[0], "value", None), ast.Constant)
                            and isinstance(body[0].value.value, str)) else body[0]
        pad = " " * first.col_offset
        cuts.append((first.lineno - 1, node.end_lineno, [pad + "...  # implemented"]))
    for start, end, new in sorted(cuts, reverse=True):
        lines[start:end] = new
    return "\n".join(lines) + "\n"


def harvest(reply: str, complete: bool = True) -> List[Tuple[str, str]]:
    """Extract complete, non-stub function definitions from any reply text,
    including a truncated echo of the whole file. Returns [(name, source)]."""
    import re
    lines = reply.splitlines()
    heads = [i for i, line in enumerate(lines) if re.match(r"\s*(?:async\s+)?def\s+\w+", line)]
    found = []
    for n, start in enumerate(heads):
        indent = len(lines[start]) - len(lines[start].lstrip())
        begin = start
        while begin > 0 and lines[begin - 1].strip().startswith("@"):
            begin -= 1
        end = len(lines)
        for j in range(start + 1, len(lines)):
            text = lines[j]
            if text.strip() and len(text) - len(text.lstrip()) <= indent:
                end = j
                break
        else:
            if not complete:
                continue  # the final definition of a cut-off reply may be partial
        chunk = "\n".join(lines[begin:end]).rstrip()
        try:
            node = ast.parse(textwrap.dedent(chunk)).body[0]
        except (SyntaxError, IndexError):
            continue
        if isinstance(node, FUNCS) and not is_stub(node):
            found.append((node.name, chunk))
    return found


def relevant_sources(code: str, text: str, limit: int = 7000) -> str:
    """Full source of functions whose names appear in text (errors, traces)."""
    import re
    lines = code.splitlines()
    words = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text))
    parts, used = [], 0
    for qual, node in _scopes(ast.parse(code)):
        if node.name in words and node.name != "__init__":
            chunk = "\n".join(_segment(lines, node))
            if used + len(chunk) > limit:
                break
            parts.append(chunk)
            used += len(chunk)
    return "\n\n".join(parts)
