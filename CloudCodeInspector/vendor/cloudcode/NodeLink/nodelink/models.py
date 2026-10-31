from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from datetime import datetime

@dataclass
class ConnectionHandle:
    target_id: str
    status: str
    endpoint: str
    auth_type: str
    uptime_seconds: int = 0
    bytes_sent: int = 0
    bytes_received: int = 0
    last_heartbeat: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "target_id": self.target_id,
            "status": self.status,
            "endpoint": self.endpoint,
            "auth_type": self.auth_type,
            "uptime_seconds": self.uptime_seconds,
            "bytes_sent": self.bytes_sent,
            "bytes_received": self.bytes_received,
            "last_heartbeat": self.last_heartbeat
        }

@dataclass
class ApiResponse:
    status_code: int
    headers: Dict[str, str]
    data: Any
    
@dataclass
class RemoteJobHandle:
    target_id: str
    job_id: str
    status: str
    started_at: datetime = field(default_factory=datetime.utcnow)
    
@dataclass
class JobResult:
    job_id: str
    status: str
    result_data: Any = None
    error: Optional[str] = None
    downloaded_path: Optional[str] = None

@dataclass
class ConnectionState:
    target_id: str
    status: str
    endpoint: str
    auth_type: str
    connected_at: datetime = field(default_factory=datetime.utcnow)
    last_heartbeat: Optional[datetime] = None
    bytes_sent: int = 0
    bytes_received: int = 0

    @property
    def uptime_seconds(self) -> int:
        return int((datetime.utcnow() - self.connected_at).total_seconds())

    def to_handle(self) -> ConnectionHandle:
        return ConnectionHandle(
            target_id=self.target_id,
            status=self.status,
            endpoint=self.endpoint,
            auth_type=self.auth_type,
            uptime_seconds=self.uptime_seconds,
            bytes_sent=self.bytes_sent,
            bytes_received=self.bytes_received,
            last_heartbeat=self.last_heartbeat.isoformat() + "Z" if self.last_heartbeat else None
        )
