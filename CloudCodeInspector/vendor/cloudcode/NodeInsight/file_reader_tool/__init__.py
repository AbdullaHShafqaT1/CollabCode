"""
LegacyNode — FileReaderTool Package
=====================================
Self-contained, read-only filesystem inspection module.

Quick start::

    from file_reader_tool import FileReaderTool

    tool = FileReaderTool(workspace_root="/path/to/project")

    # Via unified dispatcher (tool-calling protocol compatible)
    result = await tool.call("read_file", path="src/main.py", line_numbers=True)

    # Or call individual methods directly
    response = await tool.read_file("src/main.py", start_line=1, end_line=50)

Public API
----------
FileReaderTool  — Main class; see file_reader_tool.py for full docs.
ToolResponse    — Response envelope: {status, data, error_message}.
ErrorCode       — String constants for error categorisation.
"""

from .file_reader_tool import FileReaderTool, NodeInsight
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

__all__ = [
    "FileReaderTool",
    "NodeInsight",
    "ToolResponse",
    "ErrorCode",
    "FileEntry",
    "ListDirResult",
    "SearchFilesResult",
    "ReadFileResult",
    "ChunkResult",
    "SearchMatch",
    "SearchInFileResult",
    "ASTResult",
    "ClassSymbol",
    "FunctionSymbol",
    "ImportEntry",
    "SymbolMatch",
    "SearchSymbolsResult",
    "StructuredReadResult",
]

__version__ = "1.1.0"
