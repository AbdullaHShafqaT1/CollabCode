import asyncio
import json
import logging
import os
import sys
import threading
from collections import deque
from datetime import datetime, timezone
from typing import AsyncIterator, Callable, Dict, List, Optional, Any, Tuple, Union, TextIO

from .models import EventRecord, EventType, StatusLevel, get_level_weight
from .sanitizer import sanitize_text, sanitize_data, DEFAULT_MASK

class NodeLog:
    def __init__(
        self,
        log_dir: str = "logs",
        maxlen: int = 2000,
        min_level: Union[StatusLevel, str] = StatusLevel.DEBUG,
        enable_sanitization: bool = True,
        mask: str = DEFAULT_MASK,
        enable_console: bool = False,
        console_stream: Optional[TextIO] = None,
        auto_flush_disk: bool = False,
    ):
        self.log_dir = log_dir
        self.maxlen = maxlen
        self.enable_sanitization = enable_sanitization
        self.mask = mask
        self.enable_console = enable_console
        self.console_stream = console_stream or sys.stdout
        self.auto_flush_disk = auto_flush_disk
        
        self._min_level: Union[StatusLevel, str] = min_level
        self._lock = threading.RLock()
        self._buffer: deque[EventRecord] = deque(maxlen=self.maxlen)
        self._seq_id_counter = 0
        self._suppressed_count = 0
        
        # Async stream subscribers (queues)
        self._subscribers: List[asyncio.Queue[EventRecord]] = []
        
        # Callback listeners: (callback_fn, optional_filter_fn)
        self._callbacks: List[Tuple[Callable[[EventRecord], None], Optional[Callable[[EventRecord], bool]]]] = []
        
        # Disk persistence
        self._log_queue: asyncio.Queue[EventRecord] = asyncio.Queue()
        self._disk_writer_task: Optional[asyncio.Task] = None
        
        if not os.path.exists(self.log_dir):
            os.makedirs(self.log_dir, exist_ok=True)

    @property
    def min_level(self) -> Union[StatusLevel, str]:
        return self._min_level

    @min_level.setter
    def min_level(self, level: Union[StatusLevel, str]):
        with self._lock:
            self._min_level = level

    def set_min_level(self, level: Union[StatusLevel, str]):
        """Configures the severity threshold for event ingestion."""
        self.min_level = level

    @property
    def suppressed_count(self) -> int:
        """Returns the number of events suppressed due to severity thresholds."""
        with self._lock:
            return self._suppressed_count

    def subscribe_callback(
        self,
        callback: Callable[[EventRecord], None],
        filter_fn: Optional[Callable[[EventRecord], bool]] = None
    ) -> Callable[[EventRecord], None]:
        """
        Registers an event callback listener for immediate, non-blocking delivery.
        """
        with self._lock:
            self._callbacks.append((callback, filter_fn))
        return callback

    def unsubscribe_callback(self, callback: Callable[[EventRecord], None]) -> bool:
        """Removes a registered callback listener."""
        with self._lock:
            for i, (cb, _) in enumerate(self._callbacks):
                if cb == callback:
                    self._callbacks.pop(i)
                    return True
        return False

    # Aliases
    add_callback = subscribe_callback
    register_listener = subscribe_callback
    remove_callback = unsubscribe_callback
    unregister_listener = unsubscribe_callback

    async def start(self):
        """Starts background tasks like disk persistence."""
        if self._disk_writer_task is None:
            self._disk_writer_task = asyncio.create_task(self._disk_writer_loop())

    async def stop(self):
        """Stops background tasks gracefully and flushes pending writes."""
        if self._disk_writer_task is not None:
            self._disk_writer_task.cancel()
            try:
                await self._disk_writer_task
            except asyncio.CancelledError:
                pass
            self._disk_writer_task = None
            
        # Flush remaining items
        while not self._log_queue.empty():
            try:
                record = self._log_queue.get_nowait()
                await self._write_record(record)
            except asyncio.QueueEmpty:
                break

    def flush(self):
        """Synchronously flushes all queued records to disk."""
        with self._lock:
            while not self._log_queue.empty():
                try:
                    record = self._log_queue.get_nowait()
                    dt = datetime.fromisoformat(record.timestamp.replace("Z", "+00:00"))
                    filepath = self._get_log_filepath(dt)
                    line = json.dumps(record.to_dict()) + "\n"
                    self._sync_write(filepath, line)
                except asyncio.QueueEmpty:
                    break

    def _get_log_filepath(self, dt: datetime) -> str:
        date_str = dt.strftime("%Y-%m-%d")
        return os.path.join(self.log_dir, f"nodelog-{date_str}.jsonl")

    async def _write_record(self, record: EventRecord):
        dt = datetime.fromisoformat(record.timestamp.replace("Z", "+00:00"))
        filepath = self._get_log_filepath(dt)
        line = json.dumps(record.to_dict()) + "\n"
        
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._sync_write, filepath, line)
        except RuntimeError:
            self._sync_write(filepath, line)

    def _sync_write(self, filepath: str, line: str):
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(line)

    async def _disk_writer_loop(self):
        try:
            while True:
                record = await self._log_queue.get()
                try:
                    await self._write_record(record)
                except Exception as e:
                    logging.error(f"Failed to write record to disk: {e}")
                finally:
                    self._log_queue.task_done()
        except asyncio.CancelledError:
            pass

    def emit(
        self,
        source: str = "",
        event_type: Union[EventType, str] = EventType.SYSTEM,
        status_level: Union[StatusLevel, str] = StatusLevel.INFO,
        message: str = "",
        payload: Optional[Dict[str, Any]] = None,
        phase: Optional[str] = None,
        trace_id: Optional[str] = None,
        caller_tag: Optional[str] = None,
        level: Optional[Union[StatusLevel, str]] = None,
    ) -> Optional[EventRecord]:
        """
        Emits an event into NodeLog.
        This is thread-safe, synchronous, non-blocking, and routes to configured sinks.
        """
        resolved_source = caller_tag if caller_tag is not None else source
        resolved_level = level if level is not None else status_level
        
        # Convert enums to string values
        level_str = resolved_level.value if isinstance(resolved_level, StatusLevel) else str(resolved_level).upper()
        type_str = event_type.value if isinstance(event_type, EventType) else str(event_type)

        with self._lock:
            # 1. Level Filtering & Suppression Check
            event_weight = get_level_weight(level_str)
            min_weight = get_level_weight(self._min_level)
            if event_weight < min_weight:
                self._suppressed_count += 1
                return None

            # 2. Sanitization
            clean_message = message
            clean_payload = payload
            if self.enable_sanitization:
                clean_message = sanitize_text(message, mask=self.mask)
                if payload is not None:
                    clean_payload = sanitize_data(payload, mask=self.mask)

            # 3. Sequencing and Timestamping
            self._seq_id_counter += 1
            now = datetime.now(timezone.utc)
            timestamp_str = now.isoformat().replace("+00:00", "Z")

            record = EventRecord(
                seq_id=self._seq_id_counter,
                timestamp=timestamp_str,
                source=resolved_source,
                event_type=type_str,
                status_level=level_str,
                message=clean_message,
                phase=phase,
                payload=clean_payload,
                trace_id=trace_id,
            )

            # 4. In-Memory Circular Buffer Sink
            self._buffer.append(record)

            # 5. Console Sink (stdout / stream)
            if self.enable_console:
                try:
                    self.console_stream.write(
                        f"[{record.timestamp}] [{record.status_level}] [{record.source}] {record.message}\n"
                    )
                    self.console_stream.flush()
                except Exception as e:
                    logging.error(f"Console sink error: {e}")

            # 6. Disk Persistence Queue
            try:
                self._log_queue.put_nowait(record)
                if self.auto_flush_disk:
                    self.flush()
            except Exception:
                pass

            # 7. Live Callback Subscriptions
            for cb, filter_fn in list(self._callbacks):
                if filter_fn is None or filter_fn(record):
                    try:
                        cb(record)
                    except Exception as e:
                        logging.error(f"NodeLog callback execution error: {e}")

            # 8. Async Queue Subscribers
            for sub_queue in list(self._subscribers):
                try:
                    sub_queue.put_nowait(record)
                except Exception:
                    pass

            return record

    def query(
        self,
        status_levels: Optional[List[Union[StatusLevel, str]]] = None,
        event_types: Optional[List[Union[EventType, str]]] = None,
        sources: Optional[List[str]] = None,
        since_timestamp: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[EventRecord]:
        """
        Retrieves events from the in-memory ring buffer based on filters.
        """
        normalized_levels = [
            lvl.value if isinstance(lvl, StatusLevel) else str(lvl).upper()
            for lvl in status_levels
        ] if status_levels else None

        normalized_types = [
            t.value if isinstance(t, EventType) else str(t)
            for t in event_types
        ] if event_types else None

        with self._lock:
            results = []
            for record in reversed(self._buffer):
                if normalized_levels and record.status_level not in normalized_levels:
                    continue
                if normalized_types and record.event_type not in normalized_types:
                    continue
                if sources and record.source not in sources:
                    continue
                if since_timestamp and record.timestamp < since_timestamp:
                    continue
                results.append(record)

            results.reverse()
            return results[offset : offset + limit]

    async def subscribe(self, filter_fn: Optional[Callable[[EventRecord], bool]] = None) -> AsyncIterator[EventRecord]:
        """
        Yields an asynchronous generator for real-time subscription.
        """
        queue: asyncio.Queue[EventRecord] = asyncio.Queue()
        with self._lock:
            self._subscribers.append(queue)

        try:
            while True:
                record = await queue.get()
                if filter_fn is None or filter_fn(record):
                    yield record
                queue.task_done()
        finally:
            with self._lock:
                if queue in self._subscribers:
                    self._subscribers.remove(queue)
