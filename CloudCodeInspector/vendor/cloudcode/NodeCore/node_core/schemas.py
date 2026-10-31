from typing import List, Optional, Any, Dict
from pydantic import BaseModel, Field
from enum import Enum

class DomainType(str, Enum):
    SOFTWARE_DEV = "SOFTWARE_DEV"
    AI_MLOPS_PIPELINE = "AI_MLOPS_PIPELINE"
    MULTI_PROJECT_CONCURRENCY = "MULTI_PROJECT_CONCURRENCY"

class PhaseStatus(str, Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

class SessionStatus(str, Enum):
    INITIALIZED = "INITIALIZED"
    RUNNING = "RUNNING"
    TERMINATED = "TERMINATED"
    ERROR = "ERROR"
    COMPLETED = "COMPLETED"
    INCOMPLETE = "INCOMPLETE"
    CANCELLED = "CANCELLED"

class Step(BaseModel):
    step: int
    title: str
    status: PhaseStatus = PhaseStatus.PENDING
    tool: Optional[str] = None

class ExecutionState(BaseModel):
    session_id: str
    project_id: str
    domain_type: DomainType
    status: SessionStatus = SessionStatus.INITIALIZED
    active_agent: Optional[str] = None
    current_phase: Optional[str] = None
    plan: List[Step] = Field(default_factory=list)

class SessionHandle(BaseModel):
    session_id: str
    project_id: str
    workspace_path: str

class PhaseUpdate(BaseModel):
    session_id: str
    phase: str
    status: PhaseStatus
    details: Dict[str, Any] = Field(default_factory=dict)

class JobStatus(BaseModel):
    job_id: str
    status: str
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

class SessionSummary(BaseModel):
    session_id: str
    total_steps: int
    completed_steps: int
    final_status: SessionStatus
    metrics: Dict[str, Any] = Field(default_factory=dict)


# =====================================================================
# NodeCore Orchestrator & Self-Healing Schemas
# =====================================================================

class NodeCoreState(str, Enum):
    IDLE = "IDLE"
    ANALYZING = "ANALYZING"
    PATCHING = "PATCHING"
    VALIDATING = "VALIDATING"
    SUCCESS = "SUCCESS"
    RETRY_LIMIT = "RETRY_LIMIT"


class RemediationStatus(str, Enum):
    IDLE = "IDLE"
    IN_PROGRESS = "IN_PROGRESS"
    SUCCESS = "SUCCESS"
    RemediationFailed = "RemediationFailed"


class ErrorType(str, Enum):
    SYNTAX_ERROR = "SyntaxError"
    MODULE_NOT_FOUND = "ModuleNotFoundError"
    DOM_ERROR = "DOMError"
    RUNTIME_ERROR = "RuntimeError"
    TYPE_ERROR = "TypeError"
    INDENTATION_ERROR = "IndentationError"
    UNKNOWN = "Unknown"


class ErrorDiagnosis(BaseModel):
    error_type: str
    message: str
    file_path: Optional[str] = None
    line_number: Optional[int] = None
    offending_symbol: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)
    raw_traceback: str = ""


class PatchInstruction(BaseModel):
    instruction_id: str
    target_file: str
    action: str = "apply_patch"  # apply_patch, write_file, install_dependency, etc.
    patch_content: Optional[str] = None
    original_content: Optional[str] = None
    line_start: Optional[int] = None
    line_end: Optional[int] = None
    description: str = ""


class RemediationPlan(BaseModel):
    plan_id: str
    diagnosis: ErrorDiagnosis
    instructions: List[PatchInstruction] = Field(default_factory=list)
    target_tools: List[str] = Field(default_factory=list)
    confidence: float = 1.0
    created_at: Optional[str] = None


class ToolDispatchCall(BaseModel):
    target_node: str  # e.g., NodeInsight, NodeForge, NodePulse, NodeLink
    action: str  # e.g., read, apply_patch, run_command, dispatch_remote
    parameters: Dict[str, Any] = Field(default_factory=dict)


class ExecutionResult(BaseModel):
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_ms: Optional[int] = None
    status: str = "success"
    error_message: Optional[str] = None


class TelemetryEvent(BaseModel):
    source: str = "NodeCore"
    event_type: str
    status_level: str
    message: str
    phase: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)
    timestamp: Optional[str] = None


class RemediationFailedException(Exception):
    """Raised when an autonomous repair sequence fails or hits MAX_RETRIES."""
    def __init__(self, message: str = "Remediation failed after reaching retry limit", details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.details = details or {}
