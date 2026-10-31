"""Apply one unified diff only when every source line and hunk count matches."""
import re
from pathlib import PurePosixPath


def apply_unified_patch(original, patch, filename):
    lines = patch.splitlines(keepends=True)
    while lines and not lines[0].startswith("--- "):
        if not lines[0].startswith(("diff --git ", "index ")):
            raise ValueError("Expected a unified diff file header")
        lines.pop(0)
    if len(lines) < 3 or not lines[1].startswith("+++ "):
        raise ValueError("Unified diff requires source and destination headers")
    expected = str(PurePosixPath(filename.replace("\\", "/")))
    for header in lines[:2]:
        name = header[4:].split("\t")[0].strip()
        if name.startswith(("a/", "b/")):
            name = name[2:]
        if name != expected:
            raise ValueError(f"Diff targets {name!r}, expected {expected!r}")
    # The no-newline marker describes the preceding source/addition line.
    normalized = []
    for line in lines[2:]:
        if line.startswith("\\ No newline at end of file"):
            if not normalized or normalized[-1].startswith("@@"):
                raise ValueError("Invalid no-newline marker")
            normalized[-1] = normalized[-1].rstrip("\r\n")
        else:
            normalized.append(line)
    source = original.splitlines(keepends=True)
    output, cursor, i, hunks = [], 0, 0, 0
    while i < len(normalized):
        match = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", normalized[i])
        if not match:
            raise ValueError("Invalid hunk header or multiple target files")
        old_start, old_count, new_start, new_count = [int(v) if v is not None else 1 for v in match.groups()]
        position = old_start - 1 if old_count else old_start
        if position < cursor or position > len(source):
            raise ValueError("Overlapping or out-of-range hunk")
        output.extend(source[cursor:position])
        cursor = position
        if len(output) != (new_start - 1 if new_count else new_start):
            raise ValueError("Destination hunk offset does not match")
        removed = added = 0
        i += 1
        while i < len(normalized) and not normalized[i].startswith("@@"):
            line = normalized[i]
            kind, value = line[:1], line[1:]
            if kind not in {" ", "+", "-"}:
                raise ValueError("Invalid unified diff line")
            if kind in {" ", "-"}:
                if cursor >= len(source) or source[cursor] != value:
                    raise ValueError("Patch context does not match current file")
                cursor += 1
                removed += 1
            if kind in {" ", "+"}:
                output.append(value)
                added += 1
            i += 1
        if (removed, added) != (old_count, new_count):
            raise ValueError("Hunk line counts do not match")
        hunks += 1
    if not hunks:
        raise ValueError("Patch has no hunks")
    return "".join(output + source[cursor:])
