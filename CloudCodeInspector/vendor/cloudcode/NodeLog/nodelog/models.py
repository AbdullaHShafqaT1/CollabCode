import json
from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Union

class EventType(str, Enum):
    SYSTEM = "SYSTEM"
    TOOL_EXECUTION = "TOOL_EXECUTION"
    PHASE_COMPLETION = "PHASE_COMPLETION"
    HEARTBEAT = "HEARTBEAT"
    USER_MESSAGE = "USER_MESSAGE"
    TELEMETRY = "TELEMETRY"

class StatusLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    PASS = "PASS"
    SUCCESS = "SUCCESS"
    WARN = "WARN"
    WARNING = "WARNING"
    ERROR = "ERROR"
    FAIL = "FAIL"
    CRITICAL = "CRITICAL"

LEVEL_WEIGHTS: Dict[str, int] = {
    StatusLevel.DEBUG.value: 10,
    StatusLevel.INFO.value: 20,
    StatusLevel.PASS.value: 25,
    StatusLevel.SUCCESS.value: 25,
    StatusLevel.WARN.value: 30,
    StatusLevel.WARNING.value: 30,
    StatusLevel.ERROR.value: 40,
    StatusLevel.FAIL.value: 40,
    StatusLevel.CRITICAL.value: 50,
}

def get_level_weight(level: Union[StatusLevel, str]) -> int:
    """Returns the numeric severity weight for level comparison."""
    level_str = level.value if isinstance(level, StatusLevel) else str(level).upper()
    return LEVEL_WEIGHTS.get(level_str, 0)

@dataclass
class EventRecord:
    seq_id: int
    timestamp: str
    source: str
    event_type: str
    status_level: str
    message: str
    phase: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None
    trace_id: Optional[str] = None

    @property
    def caller_tag(self) -> str:
        """Alias for source representing the component or caller tag."""
        return self.source

    @property
    def level(self) -> str:
        """Alias for status_level."""
        return self.status_level

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "seq_id": self.seq_id,
            "timestamp": self.timestamp,
            "source": self.source,
            "event_type": self.event_type,
            "status_level": self.status_level,
            "message": self.message,
        }
        if self.phase is not None:
            result["phase"] = self.phase
        if self.payload is not None:
            result["payload"] = self.payload
        if self.trace_id is not None:
            result["trace_id"] = self.trace_id
        return result

    def to_json(self) -> str:
        return json.dumps(self.to_dict())
