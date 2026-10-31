"""
LegacyNode — FileReaderTool
============================
Safe, sandboxed, read-only filesystem inspection for the AI controller.

Provides five core operations accessible via individual async methods or through
the unified `call(action, **kwargs)` dispatcher:

    • list_dir         — directory exploration with optional recursion & filtering
    • search_files     — name/glob search across the project hierarchy
    • read_file        — full or line-range text read, with optional line numbers
    • read_file_chunked — paginated reading for large files
    • search_in_file   — literal string / regex search within a file

All paths are resolved relative to `workspace_root` and validated against
directory-traversal attacks before any I/O is performed.
"""

from __future__ import annotations

import ast
import collections
import fnmatch
import json
import re
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiofiles
import structlog

from .models import (
    ASTResult,
    ChunkResult,
    ClassSymbol,
    ErrorCode,
    FileEntry,
    FunctionSymbol,
    ImportEntry,
    ListDirResult,
    ReadFileResult,
    SearchFilesResult,
    SearchInFileResult,
    SearchMatch,
    SearchSymbolsResult,
    StructuredReadResult,
    SymbolMatch,
    ToolResponse,
)

log = structlog.get_logger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

MAX_FILE_SIZE: int = 10 * 1024 * 1024  # 10 MB hard limit for text reads
DEFAULT_CHUNK_LINES: int = 200         # lines per chunk in paginated reads
MAX_SEARCH_RESULTS: int = 500          # cap on search_files hits
MAX_MATCH_RESULTS: int = 200           # cap on search_in_file hits
BINARY_SNIFF_BYTES: int = 8192         # bytes to inspect for binary detection

DEFAULT_IGNORED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "env",
        ".env",
        "dist",
        "build",
        ".build",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".legacynode",
        "coverage",
        ".coverage",
        ".tox",
        "eggs",
        ".eggs",
        "site-packages",
    }
)

# ─── Helpers ──────────────────────────────────────────────────────────────────


def _mtime_iso(path: Path) -> str:
    """Return last-modified time for *path* as an ISO-8601 UTC string."""
    try:
        ts = path.stat().st_mtime
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except OSError:
        return "unknown"


def _file_entry(path: Path, root: Path) -> FileEntry:
    """Construct a FileEntry for *path* relative to *root*."""
    rel = path.relative_to(root).as_posix()
    try:
        stat = path.stat()
        size = stat.st_size if path.is_file() else 0
        modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except OSError:
        size, modified = 0, "unknown"

    return FileEntry(
        name=path.name,
        path=rel,
        type="file" if path.is_file() else "directory",
        size=size,
        modified=modified,
    )


def _format_ast_arg(arg: ast.arg) -> str:
    res = arg.arg
    if arg.annotation:
        try:
            res += f": {ast.unparse(arg.annotation)}"
        except Exception:
            pass
    return res


def _format_ast_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    try:
        parts: list[str] = []
        posonly = getattr(node.args, "posonlyargs", [])
        for arg in posonly:
            parts.append(_format_ast_arg(arg))
        if posonly:
            parts.append("/")

        defaults = [None] * (len(node.args.args) - len(node.args.defaults)) + list(node.args.defaults)
        for arg, default in zip(node.args.args, defaults):
            formatted = _format_ast_arg(arg)
            if default is not None:
                try:
                    formatted += f" = {ast.unparse(default)}"
                except Exception:
                    pass
            parts.append(formatted)

        if node.args.vararg:
            parts.append(f"*{_format_ast_arg(node.args.vararg)}")
        elif node.args.kwonlyargs:
            parts.append("*")

        for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
            formatted = _format_ast_arg(arg)
            if default is not None:
                try:
                    formatted += f" = {ast.unparse(default)}"
                except Exception:
                    pass
            parts.append(formatted)

        if node.args.kwarg:
            parts.append(f"**{_format_ast_arg(node.args.kwarg)}")

        sig = f"({', '.join(parts)})"
        if node.returns:
            try:
                sig += f" -> {ast.unparse(node.returns)}"
            except Exception:
                pass
        return sig
    except Exception:
        return "(...)"


def _extract_decorator_name(dec: ast.AST) -> str:
    try:
        return ast.unparse(dec)
    except Exception:
        if isinstance(dec, ast.Name):
            return dec.id
        return "decorator"


def _extract_ast_symbols(code: str, rel_path: str) -> ASTResult:
    """Parse Python code with standard ast and extract classes, functions, and imports."""
    tree = ast.parse(code, filename=rel_path)

    classes: list[ClassSymbol] = []
    functions: list[FunctionSymbol] = []
    imports: list[ImportEntry] = []
    import_modules: set[str] = set()

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(
                FunctionSymbol(
                    name=node.name,
                    line_number=node.lineno,
                    end_line=getattr(node, "end_lineno", node.lineno),
                    signature=_format_ast_signature(node),
                    docstring=ast.get_docstring(node),
                    is_async=isinstance(node, ast.AsyncFunctionDef),
                    decorators=[_extract_decorator_name(d) for d in node.decorator_list],
                )
            )
        elif isinstance(node, ast.ClassDef):
            methods: list[FunctionSymbol] = []
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.append(
                        FunctionSymbol(
                            name=item.name,
                            line_number=item.lineno,
                            end_line=getattr(item, "end_lineno", item.lineno),
                            signature=_format_ast_signature(item),
                            docstring=ast.get_docstring(item),
                            is_async=isinstance(item, ast.AsyncFunctionDef),
                            decorators=[_extract_decorator_name(d) for d in item.decorator_list],
                        )
                    )
            bases: list[str] = []
            for b in node.bases:
                try:
                    bases.append(ast.unparse(b))
                except Exception:
                    pass
            classes.append(
                ClassSymbol(
                    name=node.name,
                    line_number=node.lineno,
                    end_line=getattr(node, "end_lineno", node.lineno),
                    docstring=ast.get_docstring(node),
                    bases=bases,
                    methods=methods,
                    decorators=[_extract_decorator_name(d) for d in node.decorator_list],
                )
            )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                import_modules.add(alias.name.split(".")[0])
                imports.append(
                    ImportEntry(
                        module=alias.name,
                        names=[alias.name if not alias.asname else f"{alias.name} as {alias.asname}"],
                        line_number=node.lineno,
                        is_from=False,
                    )
                )
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod:
                import_modules.add(mod.split(".")[0])
            names = [a.name if not a.asname else f"{a.name} as {a.asname}" for a in node.names]
            imports.append(
                ImportEntry(
                    module=mod,
                    names=names,
                    line_number=node.lineno,
                    is_from=True,
                )
            )

    return ASTResult(
        path=rel_path,
        classes=classes,
        functions=functions,
        imports=imports,
        import_graph=sorted(import_modules),
        total_classes=len(classes),
        total_functions=len(functions),
    )


# ─── Main Class ───────────────────────────────────────────────────────────────


class FileReaderTool:
    """
    Read-only filesystem inspection tool for LegacyNode's AI controller.

    Parameters
    ----------
    workspace_root : str
        Absolute or relative path to the project root directory. All path
        arguments passed to methods are resolved relative to this root, and
        any path that resolves *outside* this root is rejected.
    max_file_size : int
        Maximum file size (bytes) allowed for text reads. Defaults to 10 MB.
    """

    # ── Construction ─────────────────────────────────────────────────────────

    def __init__(
        self,
        workspace_root: str = ".",
        max_file_size: int = MAX_FILE_SIZE,
    ) -> None:
        self._root = Path(workspace_root).resolve()
        self._max_size = max_file_size
        log.debug("FileReaderTool initialised", root=str(self._root))

    # ── Internal: Path Validation ─────────────────────────────────────────────

    def _safe_path(self, path: str) -> Path:
        """
        Resolve *path* relative to the workspace root.

        Raises
        ------
        ValueError
            If the resolved path escapes the workspace sandbox.
        """
        resolved = (self._root / path).resolve()
        # Use os.path.commonpath for robust Windows drive-letter handling
        try:
            resolved.relative_to(self._root)
        except ValueError:
            raise ValueError(
                f"{ErrorCode.PATH_TRAVERSAL}: '{path}' resolves to "
                f"'{resolved}', which is outside workspace root '{self._root}'"
            )
        return resolved

    # ── Internal: Binary Detection ────────────────────────────────────────────

    @staticmethod
    def _is_binary(path: Path) -> bool:
        """
        Return True if *path* appears to be a binary file.

        Uses a null-byte sniff over the first BINARY_SNIFF_BYTES bytes — the
        same heuristic used by git and many editors.
        """
        try:
            with open(path, "rb") as fh:
                chunk = fh.read(BINARY_SNIFF_BYTES)
            return b"\x00" in chunk
        except OSError:
            return False

    # ── Internal: Encoding Detection ──────────────────────────────────────────

    @staticmethod
    def _detect_encoding(path: Path) -> str:
        """
        Attempt to detect file encoding.

        Falls back through: chardet → UTF-8 → latin-1 (always succeeds).
        Returns the encoding name to use when opening the file.
        """
        try:
            import chardet  # optional; may not be installed

            with open(path, "rb") as fh:
                raw = fh.read(min(path.stat().st_size, 65536))
            result = chardet.detect(raw)
            if result and result.get("confidence", 0) >= 0.75 and result.get("encoding"):
                return result["encoding"]
        except ImportError:
            pass
        except OSError:
            pass
        return "utf-8"

    # ──────────────────────────────────────────────────────────────────────────
    # A. list_dir
    # ──────────────────────────────────────────────────────────────────────────

    async def list_dir(
        self,
        path: str = ".",
        recursive: bool = False,
        max_depth: int = 3,
        file_types: Optional[list[str]] = None,
        include_hidden: bool = False,
        include_ignored: bool = False,
    ) -> ToolResponse:
        """
        List the contents of a directory.

        Parameters
        ----------
        path : str
            Relative path to the target directory (default: workspace root).
        recursive : bool
            Whether to descend into subdirectories.
        max_depth : int
            Maximum recursion depth when `recursive=True` (1 = immediate children).
        file_types : list[str] | None
            If provided, only files whose suffix matches one of these extensions
            are included (e.g. ``['.py', '.js']``). Directories are always listed.
        include_hidden : bool
            If False (default), entries whose name starts with '.' are skipped.
        include_ignored : bool
            If False (default), entries matching DEFAULT_IGNORED_DIRS are skipped.

        Returns
        -------
        ToolResponse
            data: ListDirResult
        """
        try:
            safe = self._safe_path(path)
        except ValueError as exc:
            return ToolResponse.error(ErrorCode.PATH_TRAVERSAL, str(exc))

        if not safe.exists():
            return ToolResponse.error(ErrorCode.FILE_NOT_FOUND, f"Directory not found: '{path}'")
        if not safe.is_dir():
            return ToolResponse.error(ErrorCode.NOT_A_DIRECTORY, f"Not a directory: '{path}'")

        # Normalise extension filter
        exts: Optional[frozenset[str]] = None
        if file_types:
            exts = frozenset(
                ext if ext.startswith(".") else f".{ext}" for ext in file_types
            )

        entries: list[FileEntry] = []
        tree_lines: list[str] = []

        def _collect(dir_path: Path, depth: int, prefix: str) -> None:
            """Recursively walk dir_path and populate entries + tree_lines."""
            try:
                children = sorted(dir_path.iterdir(), key=lambda p: (p.is_file(), p.name))
            except PermissionError:
                tree_lines.append(f"{prefix}[permission denied]")
                return

            for idx, child in enumerate(children):
                is_last = idx == len(children) - 1
                connector = "└── " if is_last else "├── "
                sub_prefix = prefix + ("    " if is_last else "│   ")

                # Hidden file filter
                if not include_hidden and child.name.startswith("."):
                    continue
                # Ignored dir filter
                if not include_ignored and child.name in DEFAULT_IGNORED_DIRS:
                    continue

                if child.is_file():
                    # Extension filter
                    if exts and child.suffix.lower() not in exts:
                        continue
                    entries.append(_file_entry(child, self._root))
                    tree_lines.append(f"{prefix}{connector}{child.name}")

                elif child.is_dir():
                    entries.append(_file_entry(child, self._root))
                    tree_lines.append(f"{prefix}{connector}{child.name}/")
                    if recursive and depth < max_depth:
                        _collect(child, depth + 1, sub_prefix)

        tree_lines.append(f"{safe.name}/")
        _collect(safe, depth=1, prefix="")

        total_files = sum(1 for e in entries if e.type == "file")
        total_dirs = sum(1 for e in entries if e.type == "directory")

        result = ListDirResult(
            root=path,
            entries=entries,
            tree="\n".join(tree_lines),
            total_files=total_files,
            total_dirs=total_dirs,
        )
        log.debug("list_dir completed", path=path, files=total_files, dirs=total_dirs)
        return ToolResponse.success(result.to_dict())

    # ──────────────────────────────────────────────────────────────────────────
    # B. search_files
    # ──────────────────────────────────────────────────────────────────────────

    async def search_files(
        self,
        path: str = ".",
        query: str = "*",
        file_types: Optional[list[str]] = None,
        include_hidden: bool = False,
        include_ignored: bool = False,
    ) -> ToolResponse:
        """
        Search for files/directories whose name matches *query* under *path*.

        *query* supports:
        - Glob patterns (``*.py``, ``**/*.test.ts``)
        - Plain substrings (matched case-insensitively against the basename)

        Parameters
        ----------
        path : str
            Root directory to search from (relative to workspace root).
        query : str
            Glob pattern or keyword to match against file/directory names.
        file_types : list[str] | None
            Restrict results to these extensions.
        include_hidden : bool
            Include entries starting with '.'.
        include_ignored : bool
            Include entries in DEFAULT_IGNORED_DIRS.

        Returns
        -------
        ToolResponse
            data: SearchFilesResult
        """
        try:
            safe_root = self._safe_path(path)
        except ValueError as exc:
            return ToolResponse.error(ErrorCode.PATH_TRAVERSAL, str(exc))

        if not safe_root.exists():
            return ToolResponse.error(ErrorCode.FILE_NOT_FOUND, f"Directory not found: '{path}'")
        if not safe_root.is_dir():
            return ToolResponse.error(ErrorCode.NOT_A_DIRECTORY, f"Not a directory: '{path}'")

        exts: Optional[frozenset[str]] = None
        if file_types:
            exts = frozenset(
                ext if ext.startswith(".") else f".{ext}" for ext in file_types
            )

        is_glob = any(c in query for c in ("*", "?", "["))

        matches: list[FileEntry] = []
        truncated = False

        for child in safe_root.rglob("*"):
            if len(matches) >= MAX_SEARCH_RESULTS:
                truncated = True
                break

            # Filter hidden / ignored components in path
            parts = child.relative_to(safe_root).parts
            if not include_hidden and any(p.startswith(".") for p in parts):
                continue
            if not include_ignored and any(p in DEFAULT_IGNORED_DIRS for p in parts):
                continue
            if exts and child.is_file() and child.suffix.lower() not in exts:
                continue

            # Match
            if is_glob:
                matched = fnmatch.fnmatch(child.name, query)
            else:
                matched = query.lower() in child.name.lower()

            if matched:
                matches.append(_file_entry(child, self._root))

        result = SearchFilesResult(query=query, matches=matches, truncated=truncated)
        log.debug("search_files completed", query=query, hits=len(matches), truncated=truncated)
        return ToolResponse.success(result.to_dict())

    # ──────────────────────────────────────────────────────────────────────────
    # C. read_file
    # ──────────────────────────────────────────────────────────────────────────

    async def read_file(
        self,
        path: str,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        line_numbers: bool = False,
        encoding: Optional[str] = None,
    ) -> ToolResponse:
        """
        Read a text file — either fully or within a specific line range.

        Parameters
        ----------
        path : str
            Relative path to the file.
        start_line : int | None
            First line to return (1-indexed, inclusive). Defaults to 1.
        end_line : int | None
            Last line to return (1-indexed, inclusive). Defaults to last line.
        line_numbers : bool
            If True, each output line is prefixed with its 1-indexed line number
            and a colon (e.g. ``42: def foo():``) to assist code referencing.
        encoding : str | None
            Force a specific encoding. If None, auto-detected.

        Returns
        -------
        ToolResponse
            data: ReadFileResult
        """
        # -- Path validation --------------------------------------------------
        try:
            safe = self._safe_path(path)
        except ValueError as exc:
            return ToolResponse.error(ErrorCode.PATH_TRAVERSAL, str(exc))

        if not safe.exists():
            return ToolResponse.error(ErrorCode.FILE_NOT_FOUND, f"File not found: '{path}'")
        if safe.is_dir():
            return ToolResponse.error(ErrorCode.IS_A_DIRECTORY, f"Path is a directory: '{path}'")

        # -- Size guard -------------------------------------------------------
        size = safe.stat().st_size
        if size > self._max_size:
            return ToolResponse.error(
                ErrorCode.FILE_TOO_LARGE,
                f"File '{path}' is {size / (1024 * 1024):.2f} MB, "
                f"exceeding the {self._max_size // (1024 * 1024)} MB limit. "
                f"Use read_file_chunked instead.",
            )

        # -- Binary guard -----------------------------------------------------
        if self._is_binary(safe):
            return ToolResponse.error(
                ErrorCode.BINARY_FILE,
                f"'{path}' appears to be a binary file; text read refused. "
                f"Size: {size} bytes.",
            )

        # -- Read all lines ---------------------------------------------------
        used_encoding = encoding or self._detect_encoding(safe)
        try:
            async with aiofiles.open(safe, "r", encoding=used_encoding, errors="replace") as fh:
                all_lines = await fh.readlines()
        except PermissionError:
            return ToolResponse.error(ErrorCode.PERMISSION_DENIED, f"Cannot read '{path}': permission denied.")
        except OSError as exc:
            return ToolResponse.error(ErrorCode.PERMISSION_DENIED, f"Cannot read '{path}': {exc}")

        total = len(all_lines)

        # -- Line range -------------------------------------------------------
        sl = (start_line if start_line is not None else 1)
        el = (end_line   if end_line   is not None else total)

        if sl < 1 or el < 1 or sl > el or sl > total:
            return ToolResponse.error(
                ErrorCode.INVALID_LINE_RANGE,
                f"Invalid line range [{sl}, {el}] for file '{path}' "
                f"which has {total} lines. Lines are 1-indexed.",
            )
        el = min(el, total)

        selected = all_lines[sl - 1 : el]  # zero-indexed slice

        # -- Line numbering ---------------------------------------------------
        if line_numbers:
            numbered: list[str] = []
            for i, line in enumerate(selected, start=sl):
                stripped = line.rstrip("\n\r")
                numbered.append(f"{i}: {stripped}")
            content = "\n".join(numbered)
        else:
            content = "".join(selected)

        result = ReadFileResult(
            path=path,
            content=content,
            start_line=sl,
            end_line=el,
            total_lines=total,
            encoding=used_encoding,
            size_bytes=size,
        )
        log.debug("read_file completed", path=path, lines=f"{sl}-{el}/{total}")
        return ToolResponse.success(result.to_dict())

    # ──────────────────────────────────────────────────────────────────────────
    # D. read_file_chunked
    # ──────────────────────────────────────────────────────────────────────────

    async def read_file_chunked(
        self,
        path: str,
        chunk_size: int = DEFAULT_CHUNK_LINES,
        offset_line: int = 1,
        encoding: Optional[str] = None,
    ) -> ToolResponse:
        """
        Read a file in paginated line batches (suitable for large files).

        Parameters
        ----------
        path : str
            Relative path to the file.
        chunk_size : int
            Number of lines to return per call (default: 200).
        offset_line : int
            1-indexed line to start reading from. On the first call use 1;
            subsequent calls should pass the ``next_offset`` from the previous
            ChunkResult to continue pagination.
        encoding : str | None
            Force a specific encoding. If None, auto-detected.

        Returns
        -------
        ToolResponse
            data: ChunkResult
        """
        try:
            safe = self._safe_path(path)
        except ValueError as exc:
            return ToolResponse.error(ErrorCode.PATH_TRAVERSAL, str(exc))

        if not safe.exists():
            return ToolResponse.error(ErrorCode.FILE_NOT_FOUND, f"File not found: '{path}'")
        if safe.is_dir():
            return ToolResponse.error(ErrorCode.IS_A_DIRECTORY, f"Path is a directory: '{path}'")

        if self._is_binary(safe):
            size = safe.stat().st_size
            return ToolResponse.error(
                ErrorCode.BINARY_FILE,
                f"'{path}' appears to be a binary file. Size: {size} bytes.",
            )

        if chunk_size < 1:
            return ToolResponse.error(
                ErrorCode.INVALID_ARGUMENT, "chunk_size must be >= 1."
            )
        if offset_line < 1:
            return ToolResponse.error(
                ErrorCode.INVALID_ARGUMENT, "offset_line must be >= 1."
            )

        used_encoding = encoding or self._detect_encoding(safe)
        try:
            async with aiofiles.open(safe, "r", encoding=used_encoding, errors="replace") as fh:
                all_lines = await fh.readlines()
        except PermissionError:
            return ToolResponse.error(
                ErrorCode.PERMISSION_DENIED, f"Cannot read '{path}': permission denied."
            )

        total = len(all_lines)

        if offset_line > total:
            return ToolResponse.error(
                ErrorCode.INVALID_LINE_RANGE,
                f"offset_line {offset_line} exceeds total lines ({total}) in '{path}'.",
            )

        sl = offset_line
        el = min(sl + chunk_size - 1, total)
        selected = all_lines[sl - 1 : el]
        has_more = el < total

        result = ChunkResult(
            path=path,
            content="".join(selected),
            start_line=sl,
            end_line=el,
            total_lines=total,
            has_more=has_more,
            next_offset=el + 1 if has_more else total,
            encoding=used_encoding,
        )
        log.debug(
            "read_file_chunked completed",
            path=path,
            chunk=f"{sl}-{el}/{total}",
            has_more=has_more,
        )
        return ToolResponse.success(result.to_dict())

    # ──────────────────────────────────────────────────────────────────────────
    # E. search_in_file
    # ──────────────────────────────────────────────────────────────────────────

    async def search_in_file(
        self,
        path: str,
        pattern: str,
        use_regex: bool = False,
        context_lines: int = 2,
        max_matches: int = MAX_MATCH_RESULTS,
        encoding: Optional[str] = None,
    ) -> ToolResponse:
        """
        Search for *pattern* within the text content of a file.

        Reads the file line-by-line (memory efficient) and returns each
        matching line with surrounding context.

        Parameters
        ----------
        path : str
            Relative path to the file.
        pattern : str
            Literal string or regex pattern to search for.
        use_regex : bool
            If True, *pattern* is compiled as a regular expression.
            If False (default), a plain case-sensitive substring match is used.
        context_lines : int
            Number of lines of context to include before and after each match.
        max_matches : int
            Maximum number of matches to return.
        encoding : str | None
            Force a specific encoding. If None, auto-detected.

        Returns
        -------
        ToolResponse
            data: SearchInFileResult
        """
        try:
            safe = self._safe_path(path)
        except ValueError as exc:
            return ToolResponse.error(ErrorCode.PATH_TRAVERSAL, str(exc))

        if not safe.exists():
            return ToolResponse.error(ErrorCode.FILE_NOT_FOUND, f"File not found: '{path}'")
        if safe.is_dir():
            return ToolResponse.error(ErrorCode.IS_A_DIRECTORY, f"Path is a directory: '{path}'")

        size = safe.stat().st_size
        if size > self._max_size:
            return ToolResponse.error(
                ErrorCode.FILE_TOO_LARGE,
                f"File '{path}' is {size / (1024 * 1024):.2f} MB, "
                f"exceeding limit. Use read_file_chunked + manual search instead.",
            )

        if self._is_binary(safe):
            return ToolResponse.error(
                ErrorCode.BINARY_FILE,
                f"'{path}' appears to be a binary file; text search refused.",
            )

        # Compile regex if needed
        compiled: Optional[re.Pattern] = None
        if use_regex:
            try:
                compiled = re.compile(pattern)
            except re.error as exc:
                return ToolResponse.error(
                    ErrorCode.INVALID_ARGUMENT,
                    f"Invalid regex pattern '{pattern}': {exc}",
                )

        used_encoding = encoding or self._detect_encoding(safe)
        try:
            async with aiofiles.open(safe, "r", encoding=used_encoding, errors="replace") as fh:
                all_lines = await fh.readlines()
        except PermissionError:
            return ToolResponse.error(
                ErrorCode.PERMISSION_DENIED, f"Cannot read '{path}': permission denied."
            )

        # Strip trailing newlines for cleaner output
        lines = [ln.rstrip("\n\r") for ln in all_lines]
        total_lines = len(lines)

        # Context buffer: deque keeps the N most-recent lines
        context_buf: collections.deque[str] = collections.deque(maxlen=context_lines)

        # Two-pass: first collect (line_no, line_content), then fetch after-context
        # We do a single pass using a forward-looking approach.
        pending: list[tuple[int, str, list[str]]] = []  # (lineno, content, before)

        for idx, line in enumerate(lines):
            lineno = idx + 1
            if use_regex:
                matched = bool(compiled.search(line))  # type: ignore[union-attr]
            else:
                matched = pattern in line

            if matched:
                before = list(context_buf)
                pending.append((lineno, line, before))

            context_buf.append(line)

        # Now collect after-context
        matches: list[SearchMatch] = []
        truncated = False

        for lineno, line_content, before in pending:
            if len(matches) >= max_matches:
                truncated = True
                break
            # After-context: lines[lineno : lineno + context_lines]
            after_start = lineno  # already 1-indexed, so index = lineno (0-based)
            after = lines[after_start : after_start + context_lines]
            matches.append(
                SearchMatch(
                    line_number=lineno,
                    line_content=line_content,
                    context_before=before,
                    context_after=after,
                )
            )

        result = SearchInFileResult(
            path=path,
            pattern=pattern,
            use_regex=use_regex,
            matches=matches,
            total_matches=len(matches),
            truncated=truncated,
        )
        log.debug(
            "search_in_file completed",
            path=path,
            pattern=pattern,
            matches=len(matches),
            truncated=truncated,
        )
        return ToolResponse.success(result.to_dict())

    # ──────────────────────────────────────────────────────────────────────────
    # F. parse_ast
    # ──────────────────────────────────────────────────────────────────────────

    async def parse_ast(self, path: str) -> ToolResponse:
        """
        Parse a Python source file into an Abstract Syntax Tree (AST) metadata structure.

        Extracts classes, methods, top-level functions, signatures, docstrings,
        and module import dependencies.

        Parameters
        ----------
        path : str
            Relative path to the Python file.

        Returns
        -------
        ToolResponse
            data: ASTResult
        """
        try:
            safe = self._safe_path(path)
        except ValueError as exc:
            return ToolResponse.error(ErrorCode.PATH_TRAVERSAL, str(exc))

        if not safe.exists():
            return ToolResponse.error(ErrorCode.FILE_NOT_FOUND, f"File not found: '{path}'")
        if safe.is_dir():
            return ToolResponse.error(ErrorCode.IS_A_DIRECTORY, f"Path is a directory: '{path}'")

        size = safe.stat().st_size
        if size > self._max_size:
            return ToolResponse.error(
                ErrorCode.FILE_TOO_LARGE,
                f"File '{path}' is {size / (1024 * 1024):.2f} MB, exceeding size limit.",
            )

        if self._is_binary(safe):
            return ToolResponse.error(
                ErrorCode.BINARY_FILE,
                f"'{path}' appears to be a binary file; AST parsing refused.",
            )

        used_encoding = self._detect_encoding(safe)
        try:
            async with aiofiles.open(safe, "r", encoding=used_encoding, errors="replace") as fh:
                code = await fh.read()
        except PermissionError:
            return ToolResponse.error(ErrorCode.PERMISSION_DENIED, f"Cannot read '{path}': permission denied.")
        except OSError as exc:
            return ToolResponse.error(ErrorCode.PERMISSION_DENIED, f"Cannot read '{path}': {exc}")

        try:
            rel_path = safe.relative_to(self._root).as_posix()
            ast_data = _extract_ast_symbols(code, rel_path)
            return ToolResponse.success(ast_data.to_dict())
        except SyntaxError as exc:
            return ToolResponse.error(
                ErrorCode.PARSE_ERROR,
                f"Syntax error in '{path}' at line {exc.lineno}: {exc.msg}",
            )
        except Exception as exc:
            return ToolResponse.error(ErrorCode.PARSE_ERROR, f"Failed to parse AST for '{path}': {exc}")

    # ──────────────────────────────────────────────────────────────────────────
    # G. search_symbols
    # ──────────────────────────────────────────────────────────────────────────

    async def search_symbols(
        self,
        query: str,
        path: str = ".",
        symbol_type: Optional[str] = None,
        include_hidden: bool = False,
        include_ignored: bool = False,
    ) -> ToolResponse:
        """
        Search for class, method, or function symbols matching *query* across Python files.

        Parameters
        ----------
        query : str
            Symbol name or regex pattern to search for.
        path : str
            Directory to search from (relative to workspace root).
        symbol_type : str | None
            Optional filter: 'class', 'function', or 'method'.
        include_hidden : bool
            Include hidden files/directories.
        include_ignored : bool
            Include ignored directories (e.g. node_modules, .git).

        Returns
        -------
        ToolResponse
            data: SearchSymbolsResult
        """
        try:
            safe_root = self._safe_path(path)
        except ValueError as exc:
            return ToolResponse.error(ErrorCode.PATH_TRAVERSAL, str(exc))

        if not safe_root.exists():
            return ToolResponse.error(ErrorCode.FILE_NOT_FOUND, f"Directory not found: '{path}'")
        if not safe_root.is_dir():
            return ToolResponse.error(ErrorCode.NOT_A_DIRECTORY, f"Not a directory: '{path}'")

        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error:
            pattern = None

        matches: list[SymbolMatch] = []

        for child in safe_root.rglob("*.py"):
            parts = child.relative_to(safe_root).parts
            if not include_hidden and any(p.startswith(".") for p in parts):
                continue
            if not include_ignored and any(p in DEFAULT_IGNORED_DIRS for p in parts):
                continue

            if self._is_binary(child):
                continue

            try:
                rel = child.relative_to(self._root).as_posix()
                encoding = self._detect_encoding(child)
                with open(child, "r", encoding=encoding, errors="replace") as fh:
                    code = fh.read()
                ast_res = _extract_ast_symbols(code, rel)
            except Exception:
                continue

            for cls in ast_res.classes:
                is_match = bool(pattern.search(cls.name)) if pattern else (query.lower() in cls.name.lower())
                if is_match and (symbol_type is None or symbol_type == "class"):
                    matches.append(
                        SymbolMatch(
                            name=cls.name,
                            symbol_type="class",
                            path=rel,
                            line_number=cls.line_number,
                            signature=None,
                            parent_class=None,
                        )
                    )

                for m in cls.methods:
                    m_match = bool(pattern.search(m.name)) if pattern else (query.lower() in m.name.lower())
                    if m_match and (symbol_type is None or symbol_type == "method"):
                        matches.append(
                            SymbolMatch(
                                name=m.name,
                                symbol_type="method",
                                path=rel,
                                line_number=m.line_number,
                                signature=m.signature,
                                parent_class=cls.name,
                            )
                        )

            for fn in ast_res.functions:
                fn_match = bool(pattern.search(fn.name)) if pattern else (query.lower() in fn.name.lower())
                if fn_match and (symbol_type is None or symbol_type == "function"):
                    matches.append(
                        SymbolMatch(
                            name=fn.name,
                            symbol_type="function",
                            path=rel,
                            line_number=fn.line_number,
                            signature=fn.signature,
                            parent_class=None,
                        )
                    )

        result = SearchSymbolsResult(query=query, matches=matches, total_matches=len(matches))
        return ToolResponse.success(result.to_dict())

    # ──────────────────────────────────────────────────────────────────────────
    # H. read_structured
    # ──────────────────────────────────────────────────────────────────────────

    async def read_structured(self, path: str) -> ToolResponse:
        """
        Safely read structured data formats (JSON, YAML, Markdown).

        Returns parsed Python data structures for JSON and YAML, or structured
        sections for Markdown.
        """
        try:
            safe = self._safe_path(path)
        except ValueError as exc:
            return ToolResponse.error(ErrorCode.PATH_TRAVERSAL, str(exc))

        if not safe.exists():
            return ToolResponse.error(ErrorCode.FILE_NOT_FOUND, f"File not found: '{path}'")
        if safe.is_dir():
            return ToolResponse.error(ErrorCode.IS_A_DIRECTORY, f"Path is a directory: '{path}'")

        if self._is_binary(safe):
            return ToolResponse.error(ErrorCode.BINARY_FILE, f"'{path}' is a binary file.")

        used_encoding = self._detect_encoding(safe)
        try:
            async with aiofiles.open(safe, "r", encoding=used_encoding, errors="replace") as fh:
                content = await fh.read()
        except PermissionError:
            return ToolResponse.error(ErrorCode.PERMISSION_DENIED, f"Cannot read '{path}': permission denied.")
        except OSError as exc:
            return ToolResponse.error(ErrorCode.PERMISSION_DENIED, f"Cannot read '{path}': {exc}")

        ext = safe.suffix.lower()
        if ext == ".json":
            try:
                parsed = json.loads(content)
                res = StructuredReadResult(path=path, format="json", data=parsed)
                return ToolResponse.success(res.to_dict())
            except json.JSONDecodeError as exc:
                return ToolResponse.error(ErrorCode.PARSE_ERROR, f"Invalid JSON in '{path}': {exc}")
        elif ext in (".yaml", ".yml"):
            try:
                import yaml
                parsed = yaml.safe_load(content)
            except ImportError:
                parsed = {}
                for line in content.splitlines():
                    if ":" in line and not line.strip().startswith("#"):
                        k, _, v = line.partition(":")
                        parsed[k.strip()] = v.strip()
            except Exception as exc:
                return ToolResponse.error(ErrorCode.PARSE_ERROR, f"Invalid YAML in '{path}': {exc}")
            res = StructuredReadResult(path=path, format="yaml", data=parsed)
            return ToolResponse.success(res.to_dict())
        elif ext in (".md", ".markdown"):
            sections: list[dict[str, Any]] = []
            current_heading: str | None = None
            current_lines: list[str] = []

            for line in content.splitlines():
                if line.startswith("#"):
                    if current_heading is not None or current_lines:
                        sections.append({
                            "heading": current_heading,
                            "content": "\n".join(current_lines).strip(),
                        })
                        current_lines = []
                    current_heading = line.lstrip("#").strip()
                else:
                    current_lines.append(line)
            if current_heading is not None or current_lines:
                sections.append({
                    "heading": current_heading,
                    "content": "\n".join(current_lines).strip(),
                })

            res = StructuredReadResult(
                path=path,
                format="markdown",
                data={"raw": content, "sections": sections},
            )
            return ToolResponse.success(res.to_dict())
        else:
            return ToolResponse.error(
                ErrorCode.INVALID_ARGUMENT,
                f"Unsupported structured file format '{ext}' for '{path}'. Supported: .json, .yaml, .yml, .md",
            )

    # ──────────────────────────────────────────────────────────────────────────
    # Unified Dispatcher
    # ──────────────────────────────────────────────────────────────────────────

    VALID_ACTIONS: frozenset[str] = frozenset(
        {
            "list_dir",
            "search_files",
            "read_file",
            "read_file_chunked",
            "search_in_file",
            "parse_ast",
            "search_symbols",
            "read_structured",
        }
    )

    async def call(self, action: str, **kwargs: Any) -> dict:
        """
        Unified async dispatcher compatible with tool/function-calling protocols.

        Routes *action* to the corresponding method, passing **kwargs as
        arguments.  Always returns a plain JSON-serialisable dict:

        .. code-block:: json

            {
                "status": "success" | "error",
                "data":   <payload or null>,
                "error_message": "<ErrorCode: detail>" | null
            }

        Parameters
        ----------
        action : str
            One of: ``list_dir``, ``search_files``, ``read_file``,
            ``read_file_chunked``, ``search_in_file``, ``parse_ast``,
            ``search_symbols``, ``read_structured``.
        **kwargs
            Arguments forwarded to the specific action method.

        Returns
        -------
        dict
            Serialisable ToolResponse dict.
        """
        if action not in self.VALID_ACTIONS:
            return ToolResponse.error(
                ErrorCode.INVALID_ACTION,
                f"Unknown action '{action}'. "
                f"Valid actions: {sorted(self.VALID_ACTIONS)}",
            ).to_dict()

        dispatch = {
            "list_dir":          self.list_dir,
            "search_files":      self.search_files,
            "read_file":         self.read_file,
            "read_file_chunked": self.read_file_chunked,
            "search_in_file":    self.search_in_file,
            "parse_ast":         self.parse_ast,
            "search_symbols":    self.search_symbols,
            "read_structured":   self.read_structured,
        }

        try:
            response: ToolResponse = await dispatch[action](**kwargs)
            return response.to_dict()
        except TypeError as exc:
            return ToolResponse.error(
                ErrorCode.INVALID_ARGUMENT,
                f"Invalid arguments for action '{action}': {exc}",
            ).to_dict()
        except Exception as exc:
            log.exception("Unexpected error in FileReaderTool.call", action=action)
            return ToolResponse.error(
                ErrorCode.PERMISSION_DENIED,
                f"Unexpected error: {exc}",
            ).to_dict()


# ─── NodeInsight Reader Alias ─────────────────────────────────────────────────

NodeInsight = FileReaderTool
