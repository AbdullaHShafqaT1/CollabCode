import asyncio
import os
import sys
from datetime import datetime, timezone
import re
from typing import Dict, Any, AsyncIterator, List, Optional, Union, Callable
import uuid
import json
from pathlib import Path

# Prevent UnicodeEncodeError on Windows cmd/powershell for characters like ♔ ♕
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    os.environ["PYTHONIOENCODING"] = "utf-8"

from autogen import GroupChat, GroupChatManager

from node_core.schemas import (
    SessionHandle, DomainType, PhaseUpdate, PhaseStatus, 
    JobStatus, SessionSummary, SessionStatus, ExecutionState,
    NodeCoreState, RemediationStatus, ErrorType, ErrorDiagnosis,
    PatchInstruction, RemediationPlan, ToolDispatchCall,
    ExecutionResult, TelemetryEvent, RemediationFailedException
)
from node_core.agents import (
    create_architect_agent, create_dev_agent, 
    create_pulse_agent, create_link_agent, create_supervisor_agent,
    create_user_proxy_agent, register_node_tools,
    create_coder_agent, create_user_proxy_runner,
    prepare_phased_task_prompt
)
from node_core.tools import NodeInsight, NodeForge, NodePulse, NodeLink, NodeLog, configure_tools


# In-memory store for active sessions
_ACTIVE_SESSIONS: Dict[str, Dict[str, Any]] = {}

def create_cloud_llm_config(
    base_url: str = "https://min-referenced-celtic-fiscal.trycloudflare.com",
    model: str = "qwen2.5-coder:32b",
    api_key: str = "not-needed"
) -> Dict[str, Any]:
    """Helper to build an AutoGen-compatible LLM configuration for the Cloudflare endpoint."""
    return {
        "config_list": [
            {
                "model": model,
                "base_url": base_url.rstrip("/") if base_url.rstrip("/").endswith("/v1") else base_url.rstrip("/") + "/v1",
                "api_key": api_key,
                "max_tokens": 16384,
            }
        ],
        "temperature": 0.2,
        # Long replies from a slow GPU (e.g. a 14B model on a Colab T4) take minutes.
        "timeout": 600,
        "max_tokens": 16384,
    }

def initialize_session(
    project_id: str, 
    workspace_path: str, 
    domain_type: str, 
    env_config: dict,
    max_rounds: int = 50,
) -> SessionHandle:
    """
    Initialize a new NodeCore session, provisioning the required agents.
    """
    # Configure live tools for the specified workspace path
    try:
        configure_tools(workspace_path)
    except Exception:
        pass

    session_id = f"sess_{uuid.uuid4().hex[:8]}"
    domain_enum = DomainType(domain_type)
    
    # Provision agents based on domain type
    agents = []
    
    # Always include Supervisor, Architect, Dev, and Pulse
    supervisor = create_supervisor_agent(env_config)
    architect = create_architect_agent(env_config)
    dev = create_dev_agent(env_config)
    pulse = create_pulse_agent(env_config)
    
    agents.extend([supervisor, architect, dev, pulse])
    
    if domain_enum == DomainType.AI_MLOPS_PIPELINE:
        link_agent = create_link_agent(env_config)
        agents.append(link_agent)
    
    # Register tool bindings for the agents
    # For now, we simulate this by just keeping track of the chat setup
    
    groupchat = GroupChat(agents=agents, messages=[], max_round=max_rounds)
    manager = GroupChatManager(groupchat=groupchat, llm_config=env_config)
    
    session_handle = SessionHandle(
        session_id=session_id,
        project_id=project_id,
        workspace_path=workspace_path
    )
    
    _ACTIVE_SESSIONS[session_id] = {
        "handle": session_handle,
        "manager": manager,
        "agents": agents,
        "domain_type": domain_enum,
        "status": SessionStatus.INITIALIZED,
        "steps_completed": 0,
        "llm_config": env_config,
        "max_rounds": max_rounds,
        "started_at": datetime.now(timezone.utc),
        "cancelled": False,
    }
    
    NodeLog.emit("SESSION_INITIALIZED", {"session_id": session_id, "domain": domain_type})
    return session_handle


async def execute_goal(session_handle: SessionHandle, prompt: str) -> AsyncIterator[PhaseUpdate]:
    """Run the same verified workflow through the legacy asynchronous session API."""
    session_id = session_handle.session_id
    if session_id not in _ACTIVE_SESSIONS:
        raise ValueError(f"Invalid session ID: {session_id}")
    session_data = _ACTIVE_SESSIONS[session_id]
    session_data["status"] = SessionStatus.RUNNING
    yield PhaseUpdate(session_id=session_id, phase="Execution", status=PhaseStatus.IN_PROGRESS,
                      details={"message": "Autonomous execution started"})
    runner = NodeCore(workspace_root=session_handle.workspace_path, llm_config=session_data["llm_config"])
    try:
        result = await asyncio.to_thread(
            runner.start_task, prompt, max_rounds=session_data["max_rounds"],
            should_stop=lambda: session_data.get("cancelled", False),
        )
    except asyncio.CancelledError:
        session_data["cancelled"] = True
        raise
    session_data["result"] = result
    session_data["steps_completed"] = len(result["completed_phases"])
    completed = result["status"] == "COMPLETED"
    session_data["status"] = SessionStatus.COMPLETED if completed else SessionStatus.INCOMPLETE
    cancelled = result["status"] == "CANCELLED"
    if cancelled:
        session_data["status"] = SessionStatus.CANCELLED
    yield PhaseUpdate(session_id=session_id, phase="Verification",
                      status=PhaseStatus.COMPLETED if completed else PhaseStatus.CANCELLED if cancelled else PhaseStatus.FAILED,
                      details=result)

def dispatch_remote_task(session_handle: SessionHandle, target: str, payload: dict) -> JobStatus:
    """
    Dispatch a task remotely using NodeLink.
    """
    session_id = session_handle.session_id
    if session_id not in _ACTIVE_SESSIONS:
        raise ValueError(f"Invalid session ID: {session_id}")
    
    result = NodeLink.dispatch_remote(target, payload)
    return JobStatus(
        job_id=result.get("job_id", f"job_{uuid.uuid4().hex[:6]}"),
        status=result.get("status", "unknown"),
        result=result
    )


def terminate_session(session_handle: SessionHandle) -> SessionSummary:
    """
    Terminate the session and return a summary.
    """
    session_id = session_handle.session_id
    if session_id not in _ACTIVE_SESSIONS:
        raise ValueError(f"Invalid session ID: {session_id}")
        
    session_data = _ACTIVE_SESSIONS[session_id]
    session_data["cancelled"] = True
    session_data["status"] = SessionStatus.TERMINATED
    
    summary = SessionSummary(
        session_id=session_id,
        total_steps=5,
        completed_steps=session_data["steps_completed"],
        final_status=SessionStatus.TERMINATED,
        metrics={"duration_seconds": (datetime.now(timezone.utc) - session_data["started_at"]).total_seconds(),
                 "task_status": session_data.get("result", {}).get("status", "CANCELLED")}
    )
    
    NodeLog.emit("SESSION_TERMINATED", {"session_id": session_id, "summary": summary.model_dump()})
    
    # Cleanup
    del _ACTIVE_SESSIONS[session_id]
    return summary


# =====================================================================
# Error Diagnosis Parser
# =====================================================================

class ErrorParser:
    """
    Parses raw traceback and error strings (Python SyntaxError, ModuleNotFoundError,
    IndentationError, HTML/DOM errors, etc.) into structured ErrorDiagnosis schemas.
    """

    @staticmethod
    def parse(raw_traceback: str) -> ErrorDiagnosis:
        if not raw_traceback or not isinstance(raw_traceback, str):
            return ErrorDiagnosis(
                error_type=ErrorType.UNKNOWN.value,
                message="Empty or invalid traceback provided",
                raw_traceback=str(raw_traceback) if raw_traceback else ""
            )

        text = raw_traceback.strip()

        # 1. ModuleNotFoundError / ImportError
        module_match = re.search(
            r"(?:ModuleNotFoundError|ImportError):\s*(?:No module named\s+['\"](?P<module>[^'\"]+)['\"]|cannot import name\s+['\"](?P<import_name>[^'\"]+)['\"])",
            text
        )
        if module_match:
            missing_module = module_match.group("module") or module_match.group("import_name")
            file_match = re.findall(r'File\s+["\'](?P<file>[^"\']+)["\'],\s+line\s+(?P<line>\d+)', text)
            file_path = file_match[-1][0] if file_match else None
            line_num = int(file_match[-1][1]) if file_match else None
            return ErrorDiagnosis(
                error_type=ErrorType.MODULE_NOT_FOUND.value,
                message=f"No module named '{missing_module}'",
                file_path=file_path,
                line_number=line_num,
                offending_symbol=missing_module,
                raw_traceback=raw_traceback,
                details={"missing_module": missing_module}
            )

        # 2. SyntaxError / IndentationError
        syntax_match = re.search(
            r'File\s+["\'](?P<file>[^"\']+)["\'],\s+line\s+(?P<line>\d+)(?:,\s+in\s+.*)?\n(?:\s*(?P<code_line>.*)\n)?(?:\s*\^+\s*\n)?(?P<err_type>SyntaxError|IndentationError):\s*(?P<message>.*)',
            text
        )
        if syntax_match:
            err_type = syntax_match.group("err_type")
            file_path = syntax_match.group("file")
            line_num = int(syntax_match.group("line"))
            message = syntax_match.group("message").strip() or f"Invalid syntax at line {line_num}"
            code_line = (syntax_match.group("code_line") or "").strip()
            return ErrorDiagnosis(
                error_type=err_type,
                message=message,
                file_path=file_path,
                line_number=line_num,
                offending_symbol=code_line,
                raw_traceback=raw_traceback,
                details={"code_line": code_line}
            )

        # Fallback for SyntaxError / IndentationError without full multi-line traceback
        if "SyntaxError:" in text or "IndentationError:" in text:
            err_line_match = re.search(r'(?P<err_type>SyntaxError|IndentationError):\s*(?P<message>.*)', text)
            file_match = re.findall(r'File\s+["\'](?P<file>[^"\']+)["\'],\s+line\s+(?P<line>\d+)', text)
            file_path = file_match[-1][0] if file_match else None
            line_num = int(file_match[-1][1]) if file_match else None
            err_type = err_line_match.group("err_type") if err_line_match else ErrorType.SYNTAX_ERROR.value
            message = err_line_match.group("message").strip() if err_line_match else "Syntax error detected"
            return ErrorDiagnosis(
                error_type=err_type,
                message=message,
                file_path=file_path,
                line_number=line_num,
                raw_traceback=raw_traceback
            )

        # 3. HTML / DOM errors (e.g. element not found, selector failure)
        dom_match = re.search(
            r'(?P<err_type>DOMException|ElementNotFoundError|SelectorError|DOMError):\s*(?P<message>.*)',
            text,
            re.IGNORECASE
        )
        if dom_match:
            err_type = dom_match.group("err_type")
            message = dom_match.group("message").strip()
            selector_match = re.search(r"['\"`]([#\.\w\-\[\]=:\s]+)['\"`]", message)
            selector = selector_match.group(1).strip() if selector_match else None
            file_match = re.search(r'(?:in|at|file)\s+["\']?([A-Za-z0-9_\-\.\/]+\.html?)["\']?', text, re.IGNORECASE)
            file_path = file_match.group(1) if file_match else "index.html"
            return ErrorDiagnosis(
                error_type=ErrorType.DOM_ERROR.value,
                message=message,
                file_path=file_path,
                offending_symbol=selector,
                raw_traceback=raw_traceback,
                details={"selector": selector, "dom_error_type": err_type}
            )

        # 4. Standard Python exceptions (TypeError, ValueError, NameError, RuntimeError, etc.)
        py_err_match = re.search(
            r'(?P<err_type>[A-Za-z0-9_]*(?:Error|Exception)):\s*(?P<message>.*)',
            text
        )
        file_match = re.findall(r'File\s+["\'](?P<file>[^"\']+)["\'],\s+line\s+(?P<line>\d+)', text)
        file_path = file_match[-1][0] if file_match else None
        line_num = int(file_match[-1][1]) if file_match else None

        if py_err_match:
            err_type = py_err_match.group("err_type")
            message = py_err_match.group("message").strip()
            return ErrorDiagnosis(
                error_type=err_type,
                message=message,
                file_path=file_path,
                line_number=line_num,
                raw_traceback=raw_traceback
            )

        # 5. Default fallback
        first_line = text.splitlines()[0] if text else "Unknown error"
        return ErrorDiagnosis(
            error_type=ErrorType.UNKNOWN.value,
            message=first_line[:120],
            file_path=file_path,
            line_number=line_num,
            raw_traceback=raw_traceback
        )


# =====================================================================
# Remediation Plan Generator
# =====================================================================

class RemediationGenerator:
    """
    Generates strongly-typed RemediationPlan models containing concrete
    PatchInstruction elements based on parsed diagnoses or mock LLM returns.
    Never returns plain unstructured text.
    """

    @staticmethod
    def generate(
        diagnosis: ErrorDiagnosis, 
        llm_suggestion: Optional[Dict[str, Any]] = None
    ) -> RemediationPlan:
        plan_id = f"plan_{uuid.uuid4().hex[:8]}"
        instructions: List[PatchInstruction] = []
        target_tools: List[str] = ["NodeForge"]

        # Case A: LLM suggestion provided (e.g. from mock LLM inference fixture)
        if llm_suggestion and isinstance(llm_suggestion, dict):
            raw_instructions = llm_suggestion.get("instructions", [])
            if raw_instructions and isinstance(raw_instructions, list):
                for idx, item in enumerate(raw_instructions):
                    if isinstance(item, dict):
                        instructions.append(PatchInstruction(
                            instruction_id=item.get("instruction_id", f"{plan_id}_inst_{idx+1}"),
                            target_file=item.get("target_file", diagnosis.file_path or "src/main.py"),
                            action=item.get("action", "apply_patch"),
                            patch_content=item.get("patch_content"),
                            original_content=item.get("original_content"),
                            line_start=item.get("line_start", diagnosis.line_number),
                            line_end=item.get("line_end", diagnosis.line_number),
                            description=item.get("description", "LLM proposed remediation patch")
                        ))
            elif "patch_content" in llm_suggestion or "patch" in llm_suggestion:
                patch_content = llm_suggestion.get("patch_content") or llm_suggestion.get("patch")
                instructions.append(PatchInstruction(
                    instruction_id=f"{plan_id}_inst_1",
                    target_file=llm_suggestion.get("target_file", diagnosis.file_path or "src/main.py"),
                    action=llm_suggestion.get("action", "apply_patch"),
                    patch_content=patch_content,
                    line_start=llm_suggestion.get("line_start", diagnosis.line_number),
                    line_end=llm_suggestion.get("line_end", diagnosis.line_number),
                    description=llm_suggestion.get("description", "LLM proposed remediation patch")
                ))

            if llm_suggestion.get("target_tools"):
                target_tools = llm_suggestion["target_tools"]

        # Case B: Rule-based fallback if no LLM instructions parsed
        if not instructions:
            if diagnosis.error_type == ErrorType.MODULE_NOT_FOUND.value:
                target_file = "requirements.txt"
                mod = diagnosis.offending_symbol or "dependency"
                instructions.append(PatchInstruction(
                    instruction_id=f"{plan_id}_inst_1",
                    target_file=target_file,
                    action="apply_patch",
                    patch_content=f"{mod}\n",
                    description=f"Add missing dependency '{mod}' to requirements.txt"
                ))
            elif diagnosis.error_type in (ErrorType.SYNTAX_ERROR.value, ErrorType.INDENTATION_ERROR.value):
                target_file = diagnosis.file_path or "src/main.py"
                instructions.append(PatchInstruction(
                    instruction_id=f"{plan_id}_inst_1",
                    target_file=target_file,
                    action="apply_patch",
                    line_start=diagnosis.line_number or 1,
                    line_end=diagnosis.line_number or 1,
                    description=f"Fix syntax error at line {diagnosis.line_number}: {diagnosis.message}"
                ))
            elif diagnosis.error_type == ErrorType.DOM_ERROR.value:
                target_file = diagnosis.file_path or "index.html"
                instructions.append(PatchInstruction(
                    instruction_id=f"{plan_id}_inst_1",
                    target_file=target_file,
                    action="apply_patch",
                    description=f"Fix DOM selector/element '{diagnosis.offending_symbol}': {diagnosis.message}"
                ))
            else:
                target_file = diagnosis.file_path or "src/main.py"
                instructions.append(PatchInstruction(
                    instruction_id=f"{plan_id}_inst_1",
                    target_file=target_file,
                    action="apply_patch",
                    line_start=diagnosis.line_number,
                    line_end=diagnosis.line_number,
                    description=f"Remediate {diagnosis.error_type}: {diagnosis.message}"
                ))

        created_ts = datetime.now(timezone.utc).isoformat()
        return RemediationPlan(
            plan_id=plan_id,
            diagnosis=diagnosis,
            instructions=instructions,
            target_tools=target_tools,
            confidence=1.0,
            created_at=created_ts
        )


# =====================================================================
# NodeCore Central Orchestrator
# =====================================================================

class NodeCore:
    """
    Central orchestrator and decision-making brain of the autonomous self-healing system.
    Coordinates diagnosis, remediation plan generation, subordinate tool dispatch,
    retry/backoff tracking, and standard telemetry emission.
    """

    def __init__(
        self,
        max_retries: int = 3,
        initial_backoff: float = 0.5,
        backoff_factor: float = 2.0,
        workspace_root: str = ".",
        telemetry_sink: Optional[Callable[[Dict[str, Any]], None]] = None,
        llm_config: Optional[Dict[str, Any]] = None,
        tools: Optional[Dict[str, Any]] = None,
        **kwargs
    ):
        self.max_retries = max_retries
        self.initial_backoff = initial_backoff
        self.backoff_factor = backoff_factor
        self.workspace_root = workspace_root
        self.telemetry_sink = telemetry_sink
        self.llm_config = llm_config
        self.tools = tools if tools is not None else configure_tools(self.workspace_root)

        self.state: NodeCoreState = NodeCoreState.IDLE
        self.status: str = RemediationStatus.IDLE.value
        self.retry_count: int = 0
        self.current_diagnosis: Optional[ErrorDiagnosis] = None
        self.current_plan: Optional[RemediationPlan] = None
        self.history: List[Dict[str, Any]] = []

    def start_task(self, task_prompt: str, max_rounds: int = 30,
                   acceptance: Optional[Dict[str, Any]] = None,
                   should_stop: Optional[Callable[[], bool]] = None,
                   compat: bool = False, endpoint_resolver: Optional[Callable[[], str]] = None,
                   resume: bool = False) -> Dict[str, Any]:
        """Keep setup failures as reviewable outcomes, like execution failures.

        compat: the endpoint drops system/history or caps replies; fold rules
        into each prompt and build large files one method at a time.
        endpoint_resolver: returns the current endpoint URL; enables waiting out
        outages (and switching to a new URL) instead of failing the run.
        resume: continue from the workspace's saved workflow state.
        """
        try:
            return self._start_task(task_prompt, max_rounds, acceptance, should_stop,
                                    compat=compat, endpoint_resolver=endpoint_resolver, resume=resume)
        except Exception as exc:
            self.status = "FAILED"
            report = {"status": "FAILED", "termination_reason": str(exc), "original_task": task_prompt,
                      "rounds": 0, "turns": 0, "max_rounds": max_rounds, "messages": [],
                      "message_count": 0, "responses_received": 0, "commands": [], "verification": [],
                      "completed_phases": [], "remaining_requirements": ["Task setup or evidence persistence failed"]}
            path = Path(self.workspace_root) / ".cloudcode" / "runs" / f"{uuid.uuid4().hex}.json"
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                report["report_path"] = str(path.resolve())
                path.write_text(json.dumps(report, indent=2), encoding="utf-8")
            except OSError as storage_error:
                report.update(report_path=None, evidence_error=str(storage_error))
            self.emit_telemetry("TASK_FINISHED", "WARN", str(exc), payload=report)
            return report

    def _start_task(self, task_prompt: str, max_rounds: int = 30,
                   acceptance: Optional[Dict[str, Any]] = None,
                   should_stop: Optional[Callable[[], bool]] = None,
                   compat: bool = False, endpoint_resolver: Optional[Callable[[], str]] = None,
                   resume: bool = False) -> Dict[str, Any]:
        """
        Execute the CoderAgent/UserProxyRunner conversation with explicit evidence.
        max_rounds counts model responses including local processing of the last
        response; completion is decided by TaskWorkflow rather than chat exit.
        """
        self.emit_telemetry(
            event_type="TASK_STARTED",
            status_level="INFO",
            message=f"Starting autonomous task with {len(self.tools or {})} registered tools",
            payload={"prompt_preview": task_prompt[:150]}
        )

        if not self.llm_config:
            raise ValueError("NodeCore cannot start autonomous task without llm_config.")

        # Ensure tools are configured
        if not self.tools:
            self.tools = configure_tools(self.workspace_root)

        # One configured round permits one coder response and its local processing.
        # Optional behaviours are passed only when enabled so the default call
        # signatures (and existing substitutes of these factories) are unchanged.
        proxy_options = {k: v for k, v in (("incremental", compat), ("resume", resume)) if v}
        user_proxy = create_user_proxy_runner("UserProxyRunner", workspace_path=self.workspace_root,
                                              max_rounds=max_rounds, task_prompt=task_prompt,
                                              acceptance=acceptance, should_stop=should_stop, **proxy_options)
        workflow = user_proxy.workflow
        coder_options = {}
        if compat:
            coder_options["fold_system"] = True
        if endpoint_resolver is not None:
            coder_options.update(endpoint_resolver=endpoint_resolver, should_stop=should_stop)
        if coder_options:
            import inspect
            try:
                accepted = inspect.signature(create_coder_agent).parameters
                if not any(p.kind == p.VAR_KEYWORD for p in accepted.values()):
                    coder_options = {k: v for k, v in coder_options.items() if k in accepted}
            except (TypeError, ValueError):
                pass
        coder = create_coder_agent(self.llm_config, **coder_options)
        coder.workflow_ref = workflow  # lets the reply wrapper follow the workflow's temperature hint
        coder.human_input_mode = "NEVER"
        coder.update_max_consecutive_auto_reply(max_rounds)

        # 2. Attach telemetry hooks to broadcast each message to NodeLog
        original_user_send = user_proxy.send
        def logged_user_send(message, recipient, request_reply=None, silent=False):
            msg_str = message if isinstance(message, str) else message.get("content", "")
            NodeLog.emit("AGENT_MESSAGE", {
                "source": "UserProxyRunner",
                "recipient": getattr(recipient, "name", str(recipient)),
                "message": msg_str
            })
            return original_user_send(message, recipient, request_reply=request_reply, silent=silent)
        user_proxy.send = logged_user_send

        original_coder_send = coder.send
        def logged_coder_send(message, recipient, request_reply=None, silent=False):
            msg_str = message if isinstance(message, str) else message.get("content", "")
            NodeLog.emit("AGENT_MESSAGE", {
                "source": "CoderAgent",
                "recipient": getattr(recipient, "name", str(recipient)),
                "message": msg_str
            })
            return original_coder_send(message, recipient, request_reply=request_reply, silent=silent)
        coder.send = logged_coder_send

        # 3. Initiate Autonomous Coder <-> UserProxy Conversation with Phased Prompt
        phased_prompt = workflow.initial_prompt() if hasattr(workflow, "initial_prompt") else None
        if phased_prompt is None:
            phased_prompt = prepare_phased_task_prompt(task_prompt) + "\n\n" + workflow.context()
        messages = []
        try:
            if should_stop and should_stop():
                workflow.stop("CANCELLED", "User requested cancellation")
            else:
                chat_result = user_proxy.initiate_chat(
                    coder, message=phased_prompt, max_turns=max_rounds
                )
                messages = chat_result.chat_history
                # AutoGen 0.2.35 does not call the proxy handler for the last
                # response when max_turns expires. Consume it exactly once.
                response_count = sum(m.get("role") == "user" for m in messages)
                if (workflow.status == "IN_PROGRESS" and response_count > workflow.rounds
                        and messages[-1].get("role") == "user"):
                    workflow.consume_messages(messages)
                if workflow.status == "IN_PROGRESS":
                    workflow.stop("INCOMPLETE", "Conversation ended before verification completed")
        except KeyboardInterrupt:
            workflow.stop("CANCELLED", "Execution interrupted")
        except Exception as e:
            NodeLog.emit("TASK_ERROR", {"error": str(e), "source": "NodeCore"})
            status = "TIMEOUT" if "timeout" in type(e).__name__.lower() else "FAILED"
            workflow.stop(status, str(e))
            messages = user_proxy.chat_messages.get(coder, [])

        report = workflow.report()
        report.update(messages=messages, message_count=len(messages), turns=workflow.rounds)
        final_status = report["status"]
        self.status = final_status
        report_path = Path(self.workspace_root) / ".cloudcode" / "runs" / f"{uuid.uuid4().hex}.json"
        try:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        except OSError as exc:
            report.update(status="FAILED", evidence_error=str(exc),
                          termination_reason="Could not persist run evidence; in-memory evidence retained")
            self.status = final_status = "FAILED"

        self.emit_telemetry(
            event_type="TASK_COMPLETED" if final_status == "COMPLETED" else "TASK_FINISHED",
            status_level="SUCCESS" if final_status == "COMPLETED" else "WARN",
            message=f"Autonomous task execution finished with status: {final_status}",
            payload={"rounds": workflow.rounds, "final_status": final_status,
                     "termination_reason": workflow.reason, "report_path": str(report_path)}
        )

        return {
            **report,
            "messages": messages,
            "turns": workflow.rounds,
            "message_count": len(messages),
            "report_path": str(report_path.resolve()) if "evidence_error" not in report else None
        }



    def reset(self) -> None:
        """Reset state machine to initial clean IDLE state."""
        self.state = NodeCoreState.IDLE
        self.status = RemediationStatus.IDLE.value
        self.retry_count = 0
        self.current_diagnosis = None
        self.current_plan = None

    def emit_telemetry(
        self,
        event_type: str,
        status_level: str,
        message: str,
        payload: Optional[Dict[str, Any]] = None,
        phase: Optional[str] = None
    ) -> TelemetryEvent:
        """
        Emits standard telemetry payload compatible with NodeLog schema.
        """
        timestamp = datetime.now(timezone.utc).isoformat()
        event = TelemetryEvent(
            source="NodeCore",
            event_type=event_type,
            status_level=status_level,
            message=message,
            phase=phase or self.state.value,
            payload=payload or {},
            timestamp=timestamp
        )
        event_dict = event.model_dump()
        self.history.append(event_dict)

        # Notify optional user telemetry listener
        if self.telemetry_sink and callable(self.telemetry_sink):
            try:
                self.telemetry_sink(event_dict)
            except Exception:
                pass

        # Emit to NodeLog
        NodeLog.emit(event_type, event_dict)
        return event

    def transition_to(self, new_state: NodeCoreState, reason: str = "") -> None:
        """
        Transitions state machine and emits standard transition telemetry.
        """
        old_state = self.state
        self.state = new_state
        self.emit_telemetry(
            event_type="STATE_TRANSITION",
            status_level="INFO",
            message=f"Transitioned from {old_state.value} to {new_state.value}. {reason}".strip(),
            phase=new_state.value,
            payload={
                "from_state": old_state.value,
                "to_state": new_state.value,
                "retry_count": self.retry_count,
                "reason": reason
            }
        )

    def calculate_backoff(self, attempt: Optional[int] = None) -> float:
        """
        Computes exponential backoff delay based on retry attempt.
        """
        k = attempt if attempt is not None else self.retry_count
        if k <= 0:
            return 0.0
        return self.initial_backoff * (self.backoff_factor ** max(0, k - 1))

    def handle_execution_failure(
        self, 
        failure_output: Union[ExecutionResult, Dict[str, Any], str]
    ) -> ToolDispatchCall:
        """
        Ingests a simulated or live execution failure, transitions IDLE -> ANALYZING,
        diagnoses the error, and prepares an inspection call to NodeInsight.
        """
        # 1. Normalize execution result
        if isinstance(failure_output, ExecutionResult):
            raw_trace = failure_output.stderr or failure_output.stdout or failure_output.error_message or ""
        elif isinstance(failure_output, dict):
            raw_trace = failure_output.get("stderr") or failure_output.get("stdout") or failure_output.get("error_message") or ""
        else:
            raw_trace = str(failure_output)

        # 2. State transition
        self.status = RemediationStatus.IN_PROGRESS.value
        self.transition_to(NodeCoreState.ANALYZING, reason="Execution failure detected")

        # 3. Diagnosis
        diagnosis = ErrorParser.parse(raw_trace)
        self.current_diagnosis = diagnosis

        self.emit_telemetry(
            event_type="ERROR_DIAGNOSED",
            status_level="WARN",
            message=f"Diagnosed error: {diagnosis.error_type} - {diagnosis.message}",
            payload=diagnosis.model_dump()
        )

        # 4. Formulate tool dispatch for inspection (NodeInsight)
        target_file = diagnosis.file_path or "src/main.py"
        line_num = diagnosis.line_number or 1
        return ToolDispatchCall(
            target_node="NodeInsight",
            action="read",
            parameters={
                "file_path": target_file,
                "line_number": line_num,
                "context_lines": 10,
                "error_type": diagnosis.error_type
            }
        )

    def generate_remediation_plan(
        self,
        diagnosis: Optional[ErrorDiagnosis] = None,
        llm_suggestion: Optional[Dict[str, Any]] = None
    ) -> RemediationPlan:
        """
        Generates structured remediation plan, transitioning ANALYZING -> PATCHING.
        Ensures explicit patch instructions rather than unstructured text.
        """
        diag = diagnosis or self.current_diagnosis
        if not diag:
            raise ValueError("No error diagnosis available to generate remediation plan.")

        self.transition_to(NodeCoreState.PATCHING, reason="Formulating structured remediation plan")

        plan = RemediationGenerator.generate(diag, llm_suggestion)
        self.current_plan = plan

        self.emit_telemetry(
            event_type="PLAN_GENERATED",
            status_level="INFO",
            message=f"Generated remediation plan {plan.plan_id} with {len(plan.instructions)} instructions",
            payload={
                "plan_id": plan.plan_id,
                "instructions_count": len(plan.instructions),
                "target_tools": plan.target_tools
            }
        )
        return plan

    def prepare_patch_dispatch(
        self,
        plan: Optional[RemediationPlan] = None
    ) -> ToolDispatchCall:
        """
        Prepares subordinate tool dispatch to NodeForge to apply the generated patch.
        """
        target_plan = plan or self.current_plan
        if not target_plan or not target_plan.instructions:
            raise ValueError("No remediation plan or instructions available for patch dispatch.")

        first_inst = target_plan.instructions[0]
        return ToolDispatchCall(
            target_node="NodeForge",
            action="apply_patch",
            parameters={
                "plan_id": target_plan.plan_id,
                "instruction_id": first_inst.instruction_id,
                "target_file": first_inst.target_file,
                "action": first_inst.action,
                "patch_content": first_inst.patch_content,
                "line_start": first_inst.line_start,
                "line_end": first_inst.line_end,
                "description": first_inst.description
            }
        )

    def prepare_validation(
        self,
        command: str = "pytest"
    ) -> ToolDispatchCall:
        """
        Transitions PATCHING -> VALIDATING and prepares test execution call to NodePulse.
        """
        self.transition_to(NodeCoreState.VALIDATING, reason="Verifying patch via test execution")

        self.emit_telemetry(
            event_type="VALIDATION_TRIGGERED",
            status_level="INFO",
            message=f"Dispatching validation command '{command}' to NodePulse",
            payload={"command": command}
        )

        return ToolDispatchCall(
            target_node="NodePulse",
            action="run_command",
            parameters={"command": command}
        )

    def evaluate_validation_result(
        self,
        result: Union[ExecutionResult, Dict[str, Any]]
    ) -> NodeCoreState:
        """
        Evaluates test validation output.
        - On success: transitions to SUCCESS, resets retries, emits success telemetry.
        - On failure: increments retries; if retries >= max_retries, transitions to
          RETRY_LIMIT and marks status as RemediationFailed; otherwise transitions
          back to ANALYZING for retry attempt.
        """
        if isinstance(result, ExecutionResult):
            exit_code = result.exit_code
            raw_trace = result.stderr or result.stdout or result.error_message or ""
        elif isinstance(result, dict):
            exit_code = result.get("exit_code", 1)
            raw_trace = result.get("stderr") or result.get("stdout") or result.get("error_message") or ""
        else:
            exit_code = 1
            raw_trace = str(result)

        if exit_code == 0:
            self.transition_to(NodeCoreState.SUCCESS, reason="Validation succeeded post-patch")
            self.status = RemediationStatus.SUCCESS.value
            self.retry_count = 0
            self.emit_telemetry(
                event_type="REMEDIATION_SUCCESS",
                status_level="SUCCESS",
                message="Autonomous repair successfully verified",
                payload={"final_status": self.status}
            )
            return NodeCoreState.SUCCESS
        else:
            self.retry_count += 1
            backoff_delay = self.calculate_backoff(self.retry_count)

            if self.retry_count >= self.max_retries:
                self.transition_to(
                    NodeCoreState.RETRY_LIMIT,
                    reason=f"Hit max retries ({self.max_retries})"
                )
                self.status = RemediationStatus.RemediationFailed.value
                self.emit_telemetry(
                    event_type="REMEDIATION_FAILED",
                    status_level="CRITICAL",
                    message=f"Autonomous repair failed after {self.retry_count} retries",
                    payload={
                        "retry_count": self.retry_count,
                        "max_retries": self.max_retries,
                        "final_status": self.status
                    }
                )
                return NodeCoreState.RETRY_LIMIT
            else:
                self.transition_to(
                    NodeCoreState.ANALYZING,
                    reason=f"Validation failed (attempt {self.retry_count}/{self.max_retries}). Retrying with backoff {backoff_delay:.2f}s"
                )
                if raw_trace:
                    self.current_diagnosis = ErrorParser.parse(raw_trace)
                self.emit_telemetry(
                    event_type="RETRY_ATTEMPT",
                    status_level="WARN",
                    message=f"Initiating retry attempt {self.retry_count + 1} with backoff {backoff_delay:.2f}s",
                    payload={
                        "retry_count": self.retry_count,
                        "backoff_delay": backoff_delay,
                        "error": self.current_diagnosis.model_dump() if self.current_diagnosis else {}
                    }
                )
                return NodeCoreState.ANALYZING

    def dispatch_tool_action(
        self,
        intent: str,
        context: Optional[Dict[str, Any]] = None
    ) -> ToolDispatchCall:
        """
        Explicitly selects and formats subordinate tool dispatch based on intent and context.
        E.g.:
          - 'read' / 'inspect' -> NodeInsight ('read')
          - 'apply_patch' / 'patch' -> NodeForge ('apply_patch')
          - 'run_command' / 'test' -> NodePulse ('run_command')
          - 'dispatch_remote' -> NodeLink ('dispatch_remote')
        """
        ctx = context.copy() if context else {}
        intent_lower = intent.lower()

        if intent_lower in ("read", "inspect", "scan"):
            action = ctx.pop("action", "read")
            return ToolDispatchCall(
                target_node="NodeInsight",
                action=action,
                parameters=ctx
            )
        elif intent_lower in ("apply_patch", "patch", "write_file", "write"):
            action = ctx.pop("action", "apply_patch")
            return ToolDispatchCall(
                target_node="NodeForge",
                action=action,
                parameters=ctx
            )
        elif intent_lower in ("run_command", "execute", "validate", "test"):
            action = ctx.pop("action", "run_command")
            return ToolDispatchCall(
                target_node="NodePulse",
                action=action,
                parameters=ctx
            )
        elif intent_lower in ("dispatch_remote", "remote", "cloud"):
            action = ctx.pop("action", "dispatch_remote")
            return ToolDispatchCall(
                target_node="NodeLink",
                action=action,
                parameters=ctx
            )
        else:
            return ToolDispatchCall(
                target_node="NodeCore",
                action="custom",
                parameters=ctx
            )
