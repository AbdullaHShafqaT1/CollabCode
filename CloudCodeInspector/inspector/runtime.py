from __future__ import annotations
import json
import os
import subprocess
import sys
import time
from pathlib import Path
import threading
from contextlib import contextmanager

@contextmanager
def workspace_lock(workspace):
    path = Path(workspace)/'.cloudcode/inspector.lock'
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+b') as stream:
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b'0');stream.flush()
        stream.seek(0)
        try:
            if sys.platform == 'win32':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError('Another inspector run is using this target project') from exc
        try:
            yield
        finally:
            stream.seek(0)
            if sys.platform == 'win32':
                msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:
                fcntl.flock(stream.fileno(),fcntl.LOCK_UN)

_CHILDREN = set()
_CHILD_LOCK = threading.Lock()

def cancel_all_children():
    with _CHILD_LOCK:
        children = list(_CHILDREN)
    for proc in children:
        stop_tree(proc)

def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding='utf-8')
    temp.replace(path)

def stop_tree(proc):
    job=getattr(proc,'_inspector_job',None)
    if job is not None:
        job.close()
        proc._inspector_job=None
    if proc.poll() is not None:
        return
    if sys.platform == 'win32':
        subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], capture_output=True, timeout=15)
    else:
        import signal
        os.killpg(proc.pid, signal.SIGKILL)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()

def run_process(argv, cwd, log, timeout, should_stop=lambda: False, env=None):
    """Persist full output without a pipe deadlock; bound and cancel whole process trees."""
    started = time.monotonic()
    log = Path(log)
    log.parent.mkdir(parents=True, exist_ok=True)
    status = 'failed'
    with log.open('wb') as stream:
        proc = subprocess.Popen(argv, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT,
                                env=env, start_new_session=sys.platform != 'win32',
                                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
        with _CHILD_LOCK:
            _CHILDREN.add(proc)
        try:
            if sys.platform == 'win32':
                from vendor.cloudcode.NodePulse.process_job import ProcessJob
                proc._inspector_job=ProcessJob(proc.pid)
            while proc.poll() is None:
                if should_stop():
                    status = 'cancelled'
                    break
                if time.monotonic() - started > timeout:
                    status = 'timeout'
                    break
                time.sleep(.1)
            else:
                status = 'passed' if proc.returncode == 0 else 'failed'
        finally:
            stop_tree(proc)
            with _CHILD_LOCK:
                _CHILDREN.discard(proc)
    return {'status': status, 'exit_code': proc.returncode, 'duration_seconds': round(time.monotonic()-started, 3),
            'argv': argv, 'log': str(log), 'output': log.read_text(encoding='utf-8', errors='replace')[-16000:]}
