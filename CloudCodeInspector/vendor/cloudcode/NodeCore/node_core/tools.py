"""
NodeCore Tool Adapters
======================
Unified tool binding layer for the LegacyNode ecosystem.
Dynamically wraps live modules (NodeInsight, NodeForge, NodePulse, NodeLink, NodeLog)
when available, with transparent fallback to mock implementations for standalone tests.
"""
from __future__ import annotations

import asyncio
import os
import sys
import subprocess
from pathlib import Path
from typing import Dict, Any, Optional, List, Union

# ---------------------------------------------------------------------------
# Path bootstrap — make sibling CloudCode packages discoverable
# ---------------------------------------------------------------------------
_THIS = Path(__file__).resolve().parent
_NODECORE_DIR = _THIS.parent                        # CloudCode/NodeCore
_ROOT = _NODECORE_DIR.parent                         # CloudCode

for _p in [
    str(_ROOT),
    str(_ROOT / "NodeInsight"),
    str(_ROOT / "NodeForge"),
    str(_ROOT / "NodePulse"),
    str(_ROOT / "NodeLink"),
    str(_ROOT / "NodeLog"),
    str(_ROOT / "LegacyBridge"),
]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------------------
# Global Tool Configuration State
# ---------------------------------------------------------------------------
_ACTIVE_WORKSPACE: str = str(_ROOT)
_ACTIVE_LOG_DIR: Optional[str] = None
_LIVE_NODELOG_INSTANCE: Optional[Any] = None

# Mapping of chess pieces and common non-ASCII symbols to safe representations
UNICODE_GLYPH_MAP: Dict[str, str] = {
    # White pieces
    '♔': '[wK]', '♕': '[wQ]', '♖': '[wR]', '♗': '[wB]', '♘': '[wN]', '♙': '[wP]',
    # Black pieces
    '♚': '[bK]', '♛': '[bQ]', '♜': '[bR]', '♝': '[bB]', '♞': '[bN]', '♟': '[bP]',
    # UI and status symbols
    '●': '*', '✔': '[OK]', '✓': '[OK]', '✖': '[X]', '✗': '[X]', '⚠': '[!]',
    '⚡': '[zap]', '🔍': '[search]', '📁': '[dir]', '📄': '[file]', '🚀': '[launch]'
}


def sanitize_for_console(text: str, fallback_encoding: str = "ascii") -> str:
    """Sanitize non-ASCII Unicode glyphs (such as chess pieces or emojis) before
    writing to Windows command prompt or legacy AutoGen console outputs to prevent crash loops."""
    if not isinstance(text, str):
        text = str(text)

    # 1. Replace known troublesome glyphs
    for glyph, replacement in UNICODE_GLYPH_MAP.items():
        if glyph in text:
            text = text.replace(glyph, replacement)

    # 2. Test encoding capability of console
    console_encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(console_encoding)
        return text
    except (UnicodeEncodeError, LookupError):
        pass

    # 3. Fallback: replace unencodable characters
    try:
        return text.encode(console_encoding, errors="replace").decode(console_encoding, errors="replace")
    except Exception:
        return text.encode(fallback_encoding, errors="replace").decode(fallback_encoding, errors="replace")


def safe_console_print(*args, **kwargs) -> None:
    """Print to console safely on Windows, preventing UnicodeEncodeError."""
    sep = kwargs.pop("sep", " ")
    end = kwargs.pop("end", "\n")
    file = kwargs.pop("file", sys.stdout)

    msg = sep.join(str(a) for a in args)
    sanitized = sanitize_for_console(msg)
    try:
        file.write(sanitized + end)
        file.flush()
    except UnicodeEncodeError:
        ascii_msg = sanitized.encode("ascii", errors="replace").decode("ascii", errors="replace")
        try:
            file.write(ascii_msg + end)
            file.flush()
        except Exception:
            pass
    except Exception:
        pass

# Attempt live module imports
try:
    from NodeInsight.file_reader_tool.file_reader_tool import FileReaderTool
    _HAS_INSIGHT = True
except Exception:
    try:
        from file_reader_tool.file_reader_tool import FileReaderTool
        _HAS_INSIGHT = True
    except Exception:
        FileReaderTool = None
        _HAS_INSIGHT = False

try:
    from NodeForge.tools.file_writer_tool import FileWriterTool
    _HAS_FORGE = True
except Exception:
    try:
        from tools.file_writer_tool import FileWriterTool
        _HAS_FORGE = True
    except Exception:
        FileWriterTool = None
        _HAS_FORGE = False

try:
    from NodePulse.terminal_executor import TerminalExecutorTool
    _HAS_PULSE = True
except Exception:
    try:
        from terminal_executor import TerminalExecutorTool
        _HAS_PULSE = True
    except Exception:
        TerminalExecutorTool = None
        _HAS_PULSE = False

try:
    from nodelink.gateway import NodeLink as LiveNodeLink
    _HAS_LINK = True
except Exception:
    LiveNodeLink = None
    _HAS_LINK = False

try:
    from nodelog import NodeLog as LiveNodeLog
    _HAS_LOG = True
except Exception:
    LiveNodeLog = None
    _HAS_LOG = False


def file_reader(filepath: str) -> str:
    """Read the complete content of a file in the workspace.
    
    Args:
        filepath: Relative or absolute path of the file to read.
    """
    res = NodeInsight.read_file(filepath)
    if isinstance(res, dict) and res.get("status") == "success":
        return res.get("content", "")
    return f"Error reading {filepath}: {res.get('error', 'Unknown error') if isinstance(res, dict) else str(res)}"


def scan_workspace(path: str = ".") -> str:
    """List all files in the current workspace or subdirectory.
    
    Args:
        path: Directory path to scan (defaults to workspace root).
    """
    res = NodeInsight.scan_workspace(path)
    if isinstance(res, dict) and res.get("status") == "success":
        files = res.get("files", [])
        return "Files in workspace:\n" + "\n".join(files)
    return f"Error scanning workspace: {res.get('error', 'Unknown error') if isinstance(res, dict) else str(res)}"


def file_writer(filepath: str, content: str) -> str:
    """Create or overwrite a file in the workspace with the provided content.
    
    Args:
        filepath: Relative path of the file to create or update.
        content: The text/code content to write into the file.
    """
    res = NodeForge.write_file(filepath, content)
    if isinstance(res, dict) and res.get("status") == "success":
        return f"Successfully wrote {res.get('bytes_written', len(content))} bytes to {filepath}"
    return f"Error writing to {filepath}: {res.get('error', 'Unknown error') if isinstance(res, dict) else str(res)}"


def patch_file(filepath: str, patch_content: str) -> str:
    """Apply a patch or diff to an existing file in the workspace.
    
    Args:
        filepath: Relative path of the file to patch.
        patch_content: The patch or diff content to apply.
    """
    res = NodeForge.patch_file(filepath, patch_content)
    if isinstance(res, dict) and res.get("status") == "success":
        return f"Successfully patched {filepath}"
    return f"Error patching {filepath}: {res.get('error', 'Unknown error') if isinstance(res, dict) else str(res)}"


def terminal_executor(command: str) -> str:
    """Execute a shell or terminal command in the workspace (e.g., run tests, run python, install dependencies).
    
    Args:
        command: The terminal command to execute.
    """
    res = NodePulse.execute_command(command)
    if isinstance(res, dict):
        exit_code = res.get("exit_code", 0)
        output = res.get("output") or res.get("stdout") or res.get("stderr") or ""
        sanitized_output = sanitize_for_console(str(output))
        return f"Exit code: {exit_code}\nOutput:\n{sanitized_output}"
    return f"Command execution result: {sanitize_for_console(str(res))}"


def dispatch_remote(target: str, payload_json: str) -> str:
    """Dispatch a remote cloud job via NodeLink.
    
    Args:
        target: The target endpoint or cluster name.
        payload_json: JSON string payload for the remote task.
    """
    import json
    try:
        payload = json.loads(payload_json) if isinstance(payload_json, str) else payload_json
    except Exception:
        payload = {"data": payload_json}
    res = NodeLink.dispatch_remote(target, payload)
    return f"Remote job status: {res.get('status')} (Job ID: {res.get('job_id')})"


def record_log(level: str, message: str, event_type: str = "AGENT_EVENT") -> str:
    """Record an audit log or telemetry event in NodeLog.
    
    Args:
        level: Severity level (INFO, WARN, ERROR, SUCCESS).
        message: Log description.
        event_type: Category/type of event.
    """
    NodeLog.emit(event_type, {"status_level": level, "message": message, "source": "AutoGenAgent"})
    return f"Logged [{level}] {message}"


def configure_tools(workspace_root: str, log_dir: Optional[str] = None) -> Dict[str, Any]:
    """Set the active workspace and logging directory for all live tool instances.
    Returns a dictionary of tool functions ready for AutoGen registration.
    """
    global _ACTIVE_WORKSPACE, _ACTIVE_LOG_DIR, _LIVE_NODELOG_INSTANCE
    _ACTIVE_WORKSPACE = os.path.abspath(workspace_root)
    _ACTIVE_LOG_DIR = log_dir or os.path.join(_ACTIVE_WORKSPACE, "nodelog_data")
    if _HAS_LOG and LiveNodeLog:
        try:
            os.makedirs(_ACTIVE_LOG_DIR, exist_ok=True)
            _LIVE_NODELOG_INSTANCE = LiveNodeLog(log_dir=_ACTIVE_LOG_DIR, min_level="DEBUG", auto_flush_disk=True)
        except Exception:
            _LIVE_NODELOG_INSTANCE = None

    return {
        "file_reader": file_reader,
        "scan_workspace": scan_workspace,
        "file_writer": file_writer,
        "patch_file": patch_file,
        "terminal_executor": terminal_executor,
        "dispatch_remote": dispatch_remote,
        "record_log": record_log,
    }


# Helper to run async methods synchronously when needed
def _run_async(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


# ===========================================================================
# 1. NodeInsight Adapter
# ===========================================================================
class NodeInsight:
    """Workspace scanning, AST analysis, and context retrieval."""

    @staticmethod
    def get_tool(workspace_path: Optional[str] = None):
        target = workspace_path or _ACTIVE_WORKSPACE
        if _HAS_INSIGHT and FileReaderTool:
            return FileReaderTool(workspace_root=target)
        return None

    @staticmethod
    def scan_workspace(workspace_path: str) -> Dict[str, Any]:
        """Workspace scanning, AST analysis, and context retrieval."""
        workspace_path = str((Path(_ACTIVE_WORKSPACE) / workspace_path).resolve())
        tool = NodeInsight.get_tool(workspace_path)
        if tool:
            try:
                res = _run_async(tool.list_dir(recursive=True))
                if res and res.status == "success":
                    file_list = [f["path"] for f in res.data["entries"] if f["type"] == "file"]
                    return {"status": "success", "files": file_list, "workspace": workspace_path}
                return {"status": "error", "error": getattr(res, "error_message", "Invalid reader response"), "files": []}
            except Exception as e:
                return {"status": "error", "error": str(e), "files": []}
        return {"status": "error", "error": "NodeInsight is unavailable", "files": []}

    @staticmethod
    def read_file(filepath: str, workspace_path: Optional[str] = None, **options) -> Dict[str, Any]:
        tool = NodeInsight.get_tool(workspace_path)
        if tool:
            try:
                res = _run_async(tool.read_file(path=filepath, **options))
                if res:
                    if res.status != "success":
                        return {"status": "error", "error": res.error_message}
                    data = getattr(res, "data", None)
                    if isinstance(data, dict) and "content" in data:
                        return {"status": "success", "content": data["content"], "filepath": filepath}
                    elif hasattr(data, "content"):
                        return {"status": "success", "content": data.content, "filepath": filepath}
                    elif hasattr(res, "content"):
                        return {"status": "success", "content": res.content, "filepath": filepath}
            except Exception as e:
                return {"status": "error", "error": str(e)}
        return {"status": "error", "error": "NodeInsight unavailable or invalid response"}

    @staticmethod
    def call(action: str, **kwargs) -> Dict[str, Any]:
        ws = kwargs.pop("workspace_root", None) or _ACTIVE_WORKSPACE
        tool = NodeInsight.get_tool(ws)
        if tool:
            try:
                res = _run_async(tool.call(action, **kwargs))
                if hasattr(res, "model_dump"):
                    return res.model_dump()
                if hasattr(res, "to_dict"):
                    return res.to_dict()
                return res if isinstance(res, dict) else {"status": getattr(res, "status", "error"), "data": getattr(res, "data", None), "error": getattr(res, "error_message", "Invalid reader response")}
            except Exception as e:
                return {"status": "error", "error": str(e)}
        return {"status": "error", "error": "NodeInsight is unavailable", "action": action}


# ===========================================================================
# 2. NodeForge Adapter
# ===========================================================================
class NodeForge:
    """Safe atomic file writes, patching, and rollback."""

    @staticmethod
    def get_tool(workspace_path: Optional[str] = None):
        target = workspace_path or _ACTIVE_WORKSPACE
        if _HAS_FORGE and FileWriterTool:
            return FileWriterTool(workspace_root=target)
        return None

    @staticmethod
    def write_file(filepath: str, content: str, workspace_path: Optional[str] = None) -> Dict[str, Any]:
        """Safe atomic file writes."""
        tool = NodeForge.get_tool(workspace_path)
        if tool:
            try:
                res = _run_async(tool.write_file(path=filepath, content=content))
                if isinstance(res, dict) and res.get("status") == "success":
                    return {"status": "success", "filepath": filepath, "bytes_written": res.get("bytes_written", 0)}
                return res
            except Exception as e:
                return {"status": "error", "error": str(e)}
        return {"status": "error", "error": "NodeForge is unavailable", "filepath": filepath}

    @staticmethod
    def patch_file(filepath: str, patch_content: str, workspace_path: Optional[str] = None) -> Dict[str, Any]:
        tool = NodeForge.get_tool(workspace_path)
        if tool:
            try:
                from NodeForge.tools.unified_patch import apply_unified_patch
                original = NodeInsight.read_file(filepath, workspace_path)
                if original.get("status") != "success":
                    return original
                patched = apply_unified_patch(original["content"], patch_content, filepath)
                res = _run_async(tool.write_file(path=filepath, content=patched))
                return res
            except Exception as e:
                return {"status": "error", "error": str(e)}
        return {"status": "error", "error": "NodeForge is unavailable", "filepath": filepath}

    @staticmethod
    def call(action: str, **kwargs) -> Dict[str, Any]:
        ws = kwargs.pop("workspace_root", None) or _ACTIVE_WORKSPACE
        tool = NodeForge.get_tool(ws)
        if tool:
            try:
                return _run_async(tool.call(action, **kwargs))
            except Exception as e:
                return {"status": "error", "error": str(e)}
        return {"status": "error", "error": "NodeForge is unavailable", "action": action}


# ===========================================================================
# 3. NodePulse Adapter
# ===========================================================================
class NodePulse:
    """Local terminal execution, build verification, and test runs."""

    @staticmethod
    def get_tool(allowed_root: Optional[str] = None):
        target = allowed_root or _ACTIVE_WORKSPACE
        if _HAS_PULSE and TerminalExecutorTool:
            return TerminalExecutorTool(allowed_project_root=target)
        return None

    @staticmethod
    def execute_command(command: str, cwd: Optional[str] = None, **options) -> Dict[str, Any]:
        """Local terminal execution, build verification, and test runs with resilient UTF-8 subprocess handling."""
        tool = NodePulse.get_tool(cwd or _ACTIVE_WORKSPACE)
        if tool:
            try:
                res = _run_async(tool.run_command(command=command, cwd=cwd, **options))
                if isinstance(res, dict):
                    for key in ("stdout", "stderr", "output"):
                        if key in res and isinstance(res[key], bytes):
                            res[key] = res[key].decode("utf-8", errors="replace")
                    return res
                return {"status": "error", "output": "Invalid terminal response: " + str(res), "exit_code": -1}
            except Exception as exc:
                # An exception does not prove the command never ran. Do not
                # silently execute a potentially mutating command a second time.
                return {"status": "error", "error": str(exc), "exit_code": -1}

        return {"status": "error", "error": "NodePulse is unavailable", "exit_code": -1}

    @staticmethod
    def call(action: str, **kwargs) -> Dict[str, Any]:
        tool = NodePulse.get_tool()
        if tool:
            try:
                return _run_async(tool.call(action, **kwargs))
            except Exception as e:
                return {"status": "error", "error": str(e)}
        return {"status": "error", "error": "NodePulse is unavailable", "action": action}


# ===========================================================================
# 4. NodeLink Adapter
# ===========================================================================
class NodeLink:
    """Remote execution, API interactions, and secure cloud tunneling."""

    _gateway: Optional[Any] = None

    @classmethod
    def get_gateway(cls):
        if cls._gateway is None and _HAS_LINK and LiveNodeLink:
            cls._gateway = LiveNodeLink()
        return cls._gateway

    @staticmethod
    def dispatch_remote(target: str, payload: dict) -> Dict[str, Any]:
        """Remote execution, API interactions, and secure cloud tunneling."""
        gw = NodeLink.get_gateway()
        if gw:
            try:
                if not payload.get("config", {}).get("endpoint"):
                    return {"status": "error", "error": "An explicit remote endpoint is required"}
                if not isinstance(payload.get("command"), str) or not payload["command"].strip():
                    return {"status": "error", "error": "An explicit remote command is required"}
                async def submit():
                    gw.connect(target, payload["config"])
                    return await gw.run_remote(target, payload["command"], payload.get("params"))
                handle = _run_async(submit())
                return {"job_id": handle.job_id, "status": handle.status,
                        "handle": {"target_id": handle.target_id, "job_id": handle.job_id,
                                   "status": handle.status, "started_at": handle.started_at.isoformat()}}
            except Exception as e:
                return {"job_id": "job_err", "status": "error", "error": str(e)}
        return {"status": "error", "error": "NodeLink is unavailable"}


# ===========================================================================
# 5. NodeLog Adapter
# ===========================================================================
class NodeLog:
    """Real-time event streaming, phase logging, and telemetry ingestion."""

    _listeners: List[Any] = []

    @classmethod
    def add_listener(cls, callback: Any) -> None:
        """Register a callback (event_type: str, data: dict) to receive real-time events."""
        if callback not in cls._listeners:
            cls._listeners.append(callback)

    @classmethod
    def remove_listener(cls, callback: Any) -> None:
        """Unregister a previously registered event callback."""
        if callback in cls._listeners:
            cls._listeners.remove(callback)

    @staticmethod
    def emit(event_type: str, data: dict) -> None:
        """Real-time event streaming, phase logging, and telemetry ingestion."""
        # Broadcast to all registered listeners (e.g. GUI console queue)
        for listener in list(NodeLog._listeners):
            try:
                listener(event_type, data)
            except Exception:
                pass

        global _LIVE_NODELOG_INSTANCE
        if _LIVE_NODELOG_INSTANCE:
            try:
                lvl = data.get("status_level", "INFO")
                msg = data.get("message", f"Event {event_type}")
                src = data.get("source", "NodeCore")
                _LIVE_NODELOG_INSTANCE.emit(
                    source=src,
                    event_type=event_type,
                    status_level=lvl,
                    message=msg,
                    payload=data
                )
                if hasattr(_LIVE_NODELOG_INSTANCE, "flush"):
                    _LIVE_NODELOG_INSTANCE.flush()
                return
            except Exception:
                pass
        # Fallback print
        safe_console_print(f"[NodeLog] [{event_type}] {data}")

    @staticmethod
    def query(**kwargs) -> List[Any]:
        if _LIVE_NODELOG_INSTANCE:
            try:
                return _LIVE_NODELOG_INSTANCE.query(**kwargs)
            except Exception:
                pass
        return []

    @staticmethod
    def stats() -> Dict[str, Any]:
        if _LIVE_NODELOG_INSTANCE:
            try:
                buf = getattr(_LIVE_NODELOG_INSTANCE, "_buffer", getattr(_LIVE_NODELOG_INSTANCE, "buffer", []))
                supp = getattr(_LIVE_NODELOG_INSTANCE, "suppressed_count", 0)
                if callable(supp):
                    supp = supp()
                return {
                    "buffer_size": len(buf),
                    "suppressed": supp,
                    "live": True
                }
            except Exception:
                pass
        return {"buffer_size": 0, "suppressed": 0, "live": False}
