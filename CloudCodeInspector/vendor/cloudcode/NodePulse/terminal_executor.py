import asyncio
import os
import re
import time
import signal
import codecs
import contextlib
from datetime import datetime, timezone

class TerminalExecutorTool:
    """
    TerminalExecutorTool provides a controlled, secure, and monitored execution runtime 
    environment for executing shell commands.
    """
    
    # Simple regex for some definitely destructive or fork bomb patterns.
    FORBIDDEN_PATTERNS = [
        re.compile(r"rm\s+-r[fF]?\s+(/|/\*)"),  # block rm -rf /
        re.compile(r"mkfs"),                   # block mkfs
        re.compile(r":\(\)\{ :\|:& \};:"),     # block classical fork bomb
        re.compile(r"\b(shutdown|reboot|halt|poweroff)\b") # block system state commands
    ]
    
    def __init__(self, allowed_project_root: str):
        """
        :param allowed_project_root: The absolute path to the directory where commands are allowed to run.
        """
        # Normalize the path to ensure reliable comparisons
        self.allowed_project_root = os.path.realpath(allowed_project_root)

    async def call(self, action: str, **kwargs) -> dict:
        """
        Unified async entrypoint.
        """
        if action == "run_command":
            return await self.run_command(**kwargs)
        else:
            return self._build_error_response(action, kwargs.get("command", ""), "Unsupported action")

    async def run_command(self, command: str, cwd: str = None, timeout: float = 60,
                          env: dict = None, should_stop=None, on_output=None, max_output_chars=100000) -> dict:
        started = time.monotonic()
        result = self._build_error_response("run_command", command, "")
        process = None
        job = None
        readers = []
        waiter = None
        try:
            if not isinstance(command, str) or not command.strip():
                raise ValueError("Command must be a nonempty string")
            if not self._is_command_allowed(command):
                raise ValueError("Command violates safety guardrails (forbidden pattern).")
            if isinstance(timeout, bool) or timeout <= 0:
                raise ValueError("Timeout must be positive")
            result["cwd"] = self._resolve_and_verify_cwd(cwd)
            if should_stop and should_stop():
                result.update(status="cancelled", error_message="Cancelled by user")
                return result
            target_env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive", "CI": "true", **(env or {})}
            process = await asyncio.create_subprocess_shell(
                command, cwd=result["cwd"], env=target_env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=(os.name != "nt"),
                **({"creationflags": 0x08000000} if os.name == "nt" else {}),
            )
            if os.name == "nt":
                try:
                    from .process_job import ProcessJob
                except ImportError:
                    from process_job import ProcessJob
                job = ProcessJob(process.pid)
            readers = [asyncio.create_task(self._read_stream_with_truncation(stream, max_chars=max_output_chars, on_output=on_output))
                       for stream in (process.stdout, process.stderr)]
            waiter = asyncio.create_task(process.wait())
            while not waiter.done():
                if should_stop and should_stop():
                    result.update(status="cancelled", error_message="Cancelled by user")
                    break
                if time.monotonic() - started >= timeout:
                    result.update(status="timeout", error_message=f"Command exceeded timeout of {timeout} seconds.")
                    break
                await asyncio.wait([waiter], timeout=min(0.05, timeout))
            else:
                result.update(status="success" if process.returncode == 0 else "error", error_message=None)
            if not waiter.done():
                if job:
                    job.close()
                await self._kill_tree(process)
            try:
                captured = await asyncio.wait_for(asyncio.gather(*readers), timeout=3)
                result["stdout"], result["stderr"] = [item[0] for item in captured]
                result["truncated"] = any(item[1] for item in captured)
            except asyncio.TimeoutError:
                if result["status"] not in {"cancelled", "timeout"}:
                    result.update(status="timeout", error_message="Child process kept output pipes open")
            result["exit_code"] = process.returncode if process.returncode is not None else -1
            result["process_exit_code"] = result["exit_code"]
            if result["status"] in {"timeout", "cancelled"}:
                result["exit_code"] = 124 if result["status"] == "timeout" else 130
        except asyncio.CancelledError:
            if job:
                job.close()
            if process:
                await self._kill_tree(process)
            raise
        except Exception as exc:
            result.update(status="error", error_message=f"Failed to execute command: {exc}")
            if process:
                await self._kill_tree(process)
        finally:
            if job:
                job.close()
            pending = [t for t in [waiter, *readers] if t is not None and not t.done()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if process and getattr(process, "_transport", None):
                process._transport.close()
                await asyncio.sleep(0)
            result["duration_ms"] = int((time.monotonic() - started) * 1000)
        return result

    async def _kill_tree(self, process):
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(process.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                creationflags=0x08000000,
            )
            try:
                await asyncio.wait_for(killer.wait(), 3)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    killer.kill()
        else:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), 3)

    def _is_command_allowed(self, command: str) -> bool:
        """
        Check against forbidden patterns.
        """
        for pattern in self.FORBIDDEN_PATTERNS:
            if pattern.search(command):
                return False
        return True

    def _resolve_and_verify_cwd(self, cwd: str) -> str:
        """
        Resolve the working directory and ensure it falls within the allowed project root.
        """
        if not cwd:
            return self.allowed_project_root
            
        # Construct absolute path
        target_cwd = os.path.realpath(os.path.join(self.allowed_project_root, cwd))
        
        # Verify it is a sub-directory of allowed_project_root
        common = os.path.commonpath([self.allowed_project_root, target_cwd])
        if common != self.allowed_project_root:
            raise ValueError(f"Target directory {target_cwd} is outside the allowed project root.")
            
        return target_cwd

    async def _read_stream_with_truncation(self, stream, max_chars=100000, on_output=None):
        if stream is None:
            return "", False
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        head, tail, total = "", "", 0
        half = max_chars // 2
        while True:
            chunk = await stream.read(8192)
            decoded = decoder.decode(chunk, final=not chunk)
            if decoded and on_output:
                on_output(decoded)
            total += len(decoded)
            if len(head) < half:
                take = half - len(head)
                head += decoded[:take]
                decoded = decoded[take:]
            tail = (tail + decoded)[-(max_chars - half):]
            if not chunk:
                break
        marker = "\n...[output truncated; final output follows]...\n" if total > max_chars else ""
        # The marker is included inside the same output budget.
        if marker:
            head = head[:max(0, len(head) - len(marker))]
        return head + marker + tail, total > max_chars

    def _build_error_response(self, action: str, command: str, error_msg: str) -> dict:
        return {
            "status": "error",
            "action": action,
            "command": command,
            "cwd": self.allowed_project_root,
            "exit_code": -1,
            "stdout": "",
            "stderr": "",
            "duration_ms": 0,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "error_message": error_msg,
            "truncated": False
        }
