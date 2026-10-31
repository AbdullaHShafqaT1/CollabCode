"""
LegacyNode — FileReaderTool · Models
Typed data structures for all tool responses and payloads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


# ─── Status Literals ─────────────────────────────────────────────────────────

Status = Literal["success", "error"]


# ─── Error Codes ─────────────────────────────────────────────────────────────

class ErrorCode:
    """
    Canonical error code strings returned in ToolResponse.error_message.
    These are structured prefixes so callers can pattern-match programmatically.
    """
    FILE_NOT_FOUND    = "FileNotFound"
    PERMISSION_DENIED = "PermissionDenied"
    FILE_TOO_LARGE    = "FileTooLarge"
    INVALID_LINE_RANGE = "InvalidLineRange"
    BINARY_FILE       = "BinaryFile"
    PATH_TRAVERSAL    = "PathTraversal"
    INVALID_ACTION    = "InvalidAction"
    INVALID_ARGUMENT  = "InvalidArgument"
    NOT_A_DIRECTORY   = "NotADirectory"
    IS_A_DIRECTORY    = "IsADirectory"
    PARSE_ERROR       = "ParseError"
    ENCODING_ERROR    = "EncodingError"


# ─── Response Envelope ───────────────────────────────────────────────────────

@dataclass
class ToolResponse:
    """
    Unified response envelope for all FileReaderTool operations.

    Attributes:
        status:        "success" or "error".
        data:          Payload on success; None on error.
        error_message: Structured error string (e.g. "FileNotFound: path/to/file")
                       on error; None on success.
    """
    status: Status
    data: Any
    error_message: str | None = None

    def to_dict(self) -> dict:
        """Serialise to a plain JSON-compatible dict."""
        return {
            "status": self.status,
            "data": self.data,
            "error_message": self.error_message,
        }

    # ── Convenience constructors ──────────────────────────────────────────────

    @classmethod
    def success(cls, data: Any) -> "ToolResponse":
        """Create a success response."""
        return cls(status="success", data=data, error_message=None)

    @classmethod
    def error(cls, code: str, detail: str) -> "ToolResponse":
        """
        Create an error response.

        Args:
            code:   One of the ErrorCode constants.
            detail: Human-readable message with context.
        """
        return cls(status="error", data=None, error_message=f"{code}: {detail}")


# ─── Payload Structures ───────────────────────────────────────────────────────

@dataclass
class FileEntry:
    """
    Metadata for a single file or directory discovered during list_dir or search_files.

    Attributes:
        name:     Basename of the entry.
        path:     Path relative to workspace root.
        type:     "file" or "directory".
        size:     File size in bytes (0 for directories).
        modified: Last-modified timestamp as ISO-8601 string.
    """
    name: str
    path: str
    type: Literal["file", "directory"]
    size: int
    modified: str

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "path": self.path,
            "type": self.type,
            "size": self.size,
            "modified": self.modified,
        }


@dataclass
class ListDirResult:
    """
    Result payload for the list_dir action.

    Attributes:
        root:    The queried path (relative to workspace root).
        entries: Flat list of FileEntry objects (dirs first, then files, alphabetical).
        tree:    ASCII-art tree string for quick LLM consumption.
        total_files: Total number of files found.
        total_dirs:  Total number of directories found.
    """
    root: str
    entries: list[FileEntry]
    tree: str
    total_files: int
    total_dirs: int

    def to_dict(self) -> dict:
        return {
            "root": self.root,
            "entries": [e.to_dict() for e in self.entries],
            "tree": self.tree,
            "total_files": self.total_files,
            "total_dirs": self.total_dirs,
        }


@dataclass
class SearchFilesResult:
    """
    Result payload for the search_files action.

    Attributes:
        query:   The search query that was used.
        matches: List of FileEntry objects for matching paths.
        truncated: True if results were capped at MAX_SEARCH_RESULTS.
    """
    query: str
    matches: list[FileEntry]
    truncated: bool

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "matches": [m.to_dict() for m in self.matches],
            "truncated": self.truncated,
        }


@dataclass
class ReadFileResult:
    """
    Result payload for the read_file action.

    Attributes:
        path:        Relative path to the file.
        content:     File content (full or ranged), optionally line-numbered.
        start_line:  First line returned (1-indexed).
        end_line:    Last line returned (1-indexed).
        total_lines: Total line count of the full file.
        encoding:    Detected/used encoding.
        size_bytes:  File size in bytes.
    """
    path: str
    content: str
    start_line: int
    end_line: int
    total_lines: int
    encoding: str
    size_bytes: int

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "content": self.content,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "total_lines": self.total_lines,
            "encoding": self.encoding,
            "size_bytes": self.size_bytes,
        }


@dataclass
class ChunkResult:
    """
    Result payload for the read_file_chunked action.

    Attributes:
        path:        Relative path to the file.
        content:     Lines in this chunk, as a single string.
        start_line:  First line in chunk (1-indexed).
        end_line:    Last line in chunk (1-indexed).
        total_lines: Total line count of the full file.
        has_more:    True if there are more lines beyond end_line.
        next_offset: Pass as offset_line in the next call to continue pagination.
        encoding:    Detected/used encoding.
    """
    path: str
    content: str
    start_line: int
    end_line: int
    total_lines: int
    has_more: bool
    next_offset: int
    encoding: str

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "content": self.content,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "total_lines": self.total_lines,
            "has_more": self.has_more,
            "next_offset": self.next_offset,
            "encoding": self.encoding,
        }


@dataclass
class SearchMatch:
    """
    A single pattern match found within a file.

    Attributes:
        line_number:    1-indexed line number of the match.
        line_content:   The matched line (stripped of trailing newline).
        context_before: Lines immediately before the match.
        context_after:  Lines immediately after the match.
    """
    line_number: int
    line_content: str
    context_before: list[str] = field(default_factory=list)
    context_after: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "line_number": self.line_number,
            "line_content": self.line_content,
            "context_before": self.context_before,
            "context_after": self.context_after,
        }


@dataclass
class SearchInFileResult:
    """
    Result payload for the search_in_file action.

    Attributes:
        path:       Relative path of the searched file.
        pattern:    The pattern that was searched.
        use_regex:  Whether the search was regex-based.
        matches:    List of SearchMatch objects.
        total_matches: Number of matches found (may be capped).
        truncated:  True if results were capped at max_matches.
    """
    path: str
    pattern: str
    use_regex: bool
    matches: list[SearchMatch]
    total_matches: int
    truncated: bool

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "pattern": self.pattern,
            "use_regex": self.use_regex,
            "matches": [m.to_dict() for m in self.matches],
            "total_matches": self.total_matches,
            "truncated": self.truncated,
        }


@dataclass
class FunctionSymbol:
    """Represents a parsed function or method symbol."""
    name: str
    line_number: int
    end_line: int
    signature: str
    docstring: str | None = None
    is_async: bool = False
    decorators: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "line_number": self.line_number,
            "end_line": self.end_line,
            "signature": self.signature,
            "docstring": self.docstring,
            "is_async": self.is_async,
            "decorators": self.decorators,
        }


@dataclass
class ClassSymbol:
    """Represents a parsed class symbol with methods."""
    name: str
    line_number: int
    end_line: int
    docstring: str | None = None
    bases: list[str] = field(default_factory=list)
    methods: list[FunctionSymbol] = field(default_factory=list)
    decorators: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "line_number": self.line_number,
            "end_line": self.end_line,
            "docstring": self.docstring,
            "bases": self.bases,
            "methods": [m.to_dict() for m in self.methods],
            "decorators": self.decorators,
        }


@dataclass
class ImportEntry:
    """Represents an import statement in a source file."""
    module: str
    names: list[str]
    line_number: int
    is_from: bool = False

    def to_dict(self) -> dict:
        return {
            "module": self.module,
            "names": self.names,
            "line_number": self.line_number,
            "is_from": self.is_from,
        }


@dataclass
class ASTResult:
    """Result payload for AST parsing operations."""
    path: str
    classes: list[ClassSymbol]
    functions: list[FunctionSymbol]
    imports: list[ImportEntry]
    import_graph: list[str]
    total_classes: int
    total_functions: int

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "classes": [c.to_dict() for c in self.classes],
            "functions": [f.to_dict() for f in self.functions],
            "imports": [i.to_dict() for i in self.imports],
            "import_graph": self.import_graph,
            "total_classes": self.total_classes,
            "total_functions": self.total_functions,
        }


@dataclass
class SymbolMatch:
    """Represents a symbol search match across the workspace."""
    name: str
    symbol_type: Literal["class", "function", "method"]
    path: str
    line_number: int
    signature: str | None = None
    parent_class: str | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "symbol_type": self.symbol_type,
            "path": self.path,
            "line_number": self.line_number,
            "signature": self.signature,
            "parent_class": self.parent_class,
        }


@dataclass
class SearchSymbolsResult:
    """Result payload for search_symbols action."""
    query: str
    matches: list[SymbolMatch]
    total_matches: int

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "matches": [m.to_dict() for m in self.matches],
            "total_matches": self.total_matches,
        }


@dataclass
class StructuredReadResult:
    """Result payload for read_structured action."""
    path: str
    format: str
    data: Any

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "format": self.format,
            "data": self.data,
        }
