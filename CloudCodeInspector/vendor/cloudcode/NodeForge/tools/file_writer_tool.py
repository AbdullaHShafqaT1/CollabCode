"""
FileWriterTool — NodeForge
==========================
Safe, audited, atomic, and granular write/modification capabilities
for the local project filesystem, scoped to a strict workspace root.

Author : NodeForge / LEGACYStudios
Created: 2026-08-29
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------

class SecurityError(PermissionError):
    """Raised when a requested path escapes the workspace root."""


# ---------------------------------------------------------------------------
# FileWriterTool
# ---------------------------------------------------------------------------

class FileWriterTool:
    """
    Provides controlled write access (creation, modification, diff-patching,
    deletion, backup, and rollback) within a strictly bounded workspace root.

    Parameters
    ----------
    workspace_root : str | Path
        Absolute path to the project root. All operations are confined here.
    """

    # Human-readable labels for the unified dispatcher
    SUPPORTED_ACTIONS = frozenset(
        {"write_file", "patch_file", "append_file", "create_dir", "delete_file", "rollback", "create_backup"}
    )

    def __init__(self, workspace_root: str | Path) -> None:
        self.workspace_root = Path(workspace_root).resolve()

    # ------------------------------------------------------------------
    # Unified async entrypoint
    # ------------------------------------------------------------------

    async def call(self, action: str, **kwargs: Any) -> dict:
        """
        Unified dispatcher.  Route *action* to the corresponding method and
        return a structured audit/telemetry dict in all cases (including errors).

        Parameters
        ----------
        action  : One of SUPPORTED_ACTIONS.
        **kwargs: Forwarded verbatim to the target method.
        """
        if action not in self.SUPPORTED_ACTIONS:
            return self._error_record(
                action=action,
                path=kwargs.get("path", ""),
                message=f"Unknown action '{action}'. Supported: {sorted(self.SUPPORTED_ACTIONS)}",
            )

        dispatch = {
            "write_file":    self.write_file,
            "patch_file":    self.patch_file,
            "append_file":   self.append_file,
            "create_dir":    self.create_dir,
            "delete_file":   self.delete_file,
            "rollback":      self.rollback,
            "create_backup": self.create_backup,
        }

        try:
            return await dispatch[action](**kwargs)
        except SecurityError as exc:
            return self._error_record(action=action, path=kwargs.get("path", ""), message=str(exc))
        except Exception as exc:  # noqa: BLE001
            return self._error_record(action=action, path=kwargs.get("path", ""), message=f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    # A. File creation & overwrite
    # ------------------------------------------------------------------

    async def write_file(
        self,
        path: str,
        content: str,
        backup: bool = True,
    ) -> dict:
        """
        Create a new file or overwrite an existing one with *content*.

        - Parent directories are created automatically.
        - An existing file is backed up before overwriting (when backup=True).
        - Uses atomic write (temp-file + os.replace) to prevent partial writes.
        """
        abs_path = self._resolve_safe_path(path)
        backup_path: str | None = None

        # Backup pre-existing content
        if abs_path.exists() and backup:
            backup_result = await self.create_backup(path)
            if backup_result["status"] == "error":
                return backup_result
            backup_path = backup_result["backup_created"]

        # Ensure parent directories exist
        abs_path.parent.mkdir(parents=True, exist_ok=True)

        # Atomic write
        encoded = content.encode("utf-8")
        bytes_written = await asyncio.to_thread(self._atomic_write, abs_path, encoded)

        return self._build_record(
            action="write_file",
            path=path,
            status="success",
            data="File written successfully.",
            bytes_written=bytes_written,
            backup_created=backup_path,
        )

    # ------------------------------------------------------------------
    # B. Diff & line-level patching
    # ------------------------------------------------------------------

    async def patch_file(
        self,
        path: str,
        target_block: str,
        replacement_block: str,
        backup: bool = True,
    ) -> dict:
        """
        Replace a unique *target_block* in the file with *replacement_block*.

        Fails safely if the block is not found or appears more than once.
        """
        abs_path = self._resolve_safe_path(path)

        if not abs_path.exists():
            return self._error_record(
                action="patch_file",
                path=path,
                message=f"File not found: {path}",
            )

        original = abs_path.read_text(encoding="utf-8")

        count = original.count(target_block)
        if count == 0:
            return self._error_record(
                action="patch_file",
                path=path,
                message="target_block not found in file — patch aborted.",
            )
        if count > 1:
            return self._error_record(
                action="patch_file",
                path=path,
                message=(
                    f"target_block found {count} times — ambiguous match, patch aborted. "
                    "Provide a more specific anchor."
                ),
            )

        backup_path: str | None = None
        if backup:
            backup_result = await self.create_backup(path)
            if backup_result["status"] == "error":
                return backup_result
            backup_path = backup_result["backup_created"]

        patched = original.replace(target_block, replacement_block, 1)

        # Count lines affected (lines in replacement minus lines in target)
        lines_removed = target_block.count("\n") + 1
        lines_added = replacement_block.count("\n") + 1
        lines_affected = abs(lines_added - lines_removed) + min(lines_added, lines_removed)

        encoded = patched.encode("utf-8")
        bytes_written = await asyncio.to_thread(self._atomic_write, abs_path, encoded)

        return self._build_record(
            action="patch_file",
            path=path,
            status="success",
            data="Patch applied successfully.",
            bytes_written=bytes_written,
            lines_affected=lines_affected,
            backup_created=backup_path,
        )

    # ------------------------------------------------------------------
    # C. Content appending
    # ------------------------------------------------------------------

    async def append_file(self, path: str, content: str) -> dict:
        """Append *content* to the end of an existing (or new) file."""
        abs_path = self._resolve_safe_path(path)
        abs_path.parent.mkdir(parents=True, exist_ok=True)

        def _append() -> int:
            with abs_path.open("a", encoding="utf-8") as fh:
                fh.write(content)
            return len(content.encode("utf-8"))

        bytes_written = await asyncio.to_thread(_append)

        return self._build_record(
            action="append_file",
            path=path,
            status="success",
            data="Content appended successfully.",
            bytes_written=bytes_written,
        )

    # ------------------------------------------------------------------
    # D. Backup & rollback
    # ------------------------------------------------------------------

    async def create_backup(self, path: str) -> dict:
        """
        Copy the file at *path* to *<path>.bak*.

        If a `.bak` already exists, a timestamped variant is used instead:
        ``<path>.<YYYYmmddHHMMSS>.bak``
        """
        abs_path = self._resolve_safe_path(path)

        if not abs_path.exists():
            return self._error_record(
                action="create_backup",
                path=path,
                message=f"Cannot back up non-existent file: {path}",
            )

        bak_path = abs_path.with_suffix(abs_path.suffix + ".bak")
        if bak_path.exists():
            ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d%H%M%S")
            bak_path = abs_path.with_suffix(f"{abs_path.suffix}.{ts}.bak")

        await asyncio.to_thread(shutil.copy2, abs_path, bak_path)
        rel_bak = str(bak_path.relative_to(self.workspace_root))

        return self._build_record(
            action="create_backup",
            path=path,
            status="success",
            data=f"Backup created at {rel_bak}",
            backup_created=rel_bak,
            bytes_written=bak_path.stat().st_size,
        )

    async def rollback(self, path: str) -> dict:
        """
        Restore *path* from its most recent ``<path>.bak`` file.

        Uses atomic replace to prevent corruption during restore.
        """
        abs_path = self._resolve_safe_path(path)
        bak_path = abs_path.with_suffix(abs_path.suffix + ".bak")

        if not bak_path.exists():
            return self._error_record(
                action="rollback",
                path=path,
                message=f"No backup found at {bak_path.name} — rollback aborted.",
            )

        data = bak_path.read_bytes()
        bytes_written = await asyncio.to_thread(self._atomic_write, abs_path, data)

        return self._build_record(
            action="rollback",
            path=path,
            status="success",
            data=f"Rolled back from {bak_path.name}.",
            bytes_written=bytes_written,
            backup_created=str(bak_path.relative_to(self.workspace_root)),
        )

    # ------------------------------------------------------------------
    # E. Directory & file management
    # ------------------------------------------------------------------

    async def create_dir(self, path: str) -> dict:
        """Create *path* and all intermediate parent directories."""
        abs_path = self._resolve_safe_path(path)

        def _mkdir() -> None:
            abs_path.mkdir(parents=True, exist_ok=True)

        await asyncio.to_thread(_mkdir)

        return self._build_record(
            action="create_dir",
            path=path,
            status="success",
            data="Directory created (or already exists).",
        )

    async def delete_file(self, path: str, confirm: bool = False) -> dict:
        """
        Remove a file from the workspace.

        *confirm* **must** be ``True`` or the operation is rejected.
        A backup is created automatically before deletion.
        """
        if not confirm:
            return self._error_record(
                action="delete_file",
                path=path,
                message="Deletion requires confirm=True as an explicit safety flag.",
            )

        abs_path = self._resolve_safe_path(path)

        if not abs_path.exists():
            return self._error_record(
                action="delete_file",
                path=path,
                message=f"File not found: {path}",
            )

        # Backup before deletion
        backup_result = await self.create_backup(path)
        backup_path: str | None = None
        if backup_result["status"] == "success":
            backup_path = backup_result["backup_created"]

        file_size = abs_path.stat().st_size
        await asyncio.to_thread(abs_path.unlink)

        return self._build_record(
            action="delete_file",
            path=path,
            status="success",
            data="File deleted successfully.",
            bytes_written=file_size,
            backup_created=backup_path,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_safe_path(self, path: str | Path) -> Path:
        """
        Resolve *path* relative to the workspace root and ensure it does not
        escape the root via traversal sequences (``../../``).

        Raises
        ------
        SecurityError
            If the resolved path is outside the workspace root.
        """
        # Treat the path as relative to workspace root
        candidate = (self.workspace_root / path).resolve()

        try:
            candidate.relative_to(self.workspace_root)
        except ValueError:
            raise SecurityError(
                f"Path traversal blocked: '{path}' resolves to '{candidate}' "
                f"which is outside workspace root '{self.workspace_root}'."
            )

        return candidate

    @staticmethod
    def _atomic_write(dest: Path, data: bytes) -> int:
        """
        Write *data* to a temporary file in the same directory as *dest*,
        then atomically rename it into place using ``os.replace``.

        Returns the number of bytes written.
        """
        dir_ = dest.parent
        fd, tmp_path = tempfile.mkstemp(dir=dir_, prefix=".~tmp_")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp_path, dest)
        except Exception:
            # Clean up the temp file if anything goes wrong
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        return len(data)

    @staticmethod
    def _build_record(
        action: str,
        path: str,
        status: str,
        data: str = "",
        bytes_written: int | None = None,
        lines_affected: int | None = None,
        backup_created: str | None = None,
        error_message: str | None = None,
    ) -> dict:
        return {
            "status": status,
            "action": action,
            "path": str(path),
            "backup_created": backup_created,
            "bytes_written": bytes_written,
            "lines_affected": lines_affected,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "data": data,
            "error_message": error_message,
        }

    @staticmethod
    def _error_record(action: str, path: str, message: str) -> dict:
        return FileWriterTool._build_record(
            action=action,
            path=str(path),
            status="error",
            data="",
            error_message=message,
        )
