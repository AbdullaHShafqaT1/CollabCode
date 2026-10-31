from __future__ import annotations
import os, re, json, base64, logging, time
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field
import requests
from .models import WindowInfo, UIElement, ActionReport, Rect
logger = logging.getLogger("Inspector.LocalVision")
SYSTEM_PROMPT = "Inspect only the supplied target page. Follow the task's action restrictions. Return one JSON action."
def _clean_json_text(text: str) -> str:
    """Remove common model wrappers around a JSON object."""
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned)
    if not cleaned.startswith("{"):
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            cleaned = match.group(0)
    return cleaned

def parse_agent_decision_json(text: str, provider_name: str = "model") -> AgentDecision:
    """Parses a planner JSON response into an AgentDecision."""
    try:
        data = json.loads(_clean_json_text(text))
        thought = data.get("thought", "Proceeding with next step.")
        action = data.get("action", "wait").lower().strip()
        parameters = data.get("parameters", {})
        if not isinstance(parameters, dict):
            parameters = {}
        is_completed = data.get("is_completed", False)
        completion_summary = data.get("completion_summary", None)

        subgoals_completed = data.get("subgoals_completed", [])
        subgoals_remaining = data.get("subgoals_remaining", [])
        objective_complete = data.get("objective_complete", False)
        if not isinstance(subgoals_completed, list):
            subgoals_completed = [str(subgoals_completed)]
        if not isinstance(subgoals_remaining, list):
            subgoals_remaining = [str(subgoals_remaining)]

        if action == "complete" or (objective_complete and not subgoals_remaining):
            is_completed = True
            objective_complete = True
            if not completion_summary:
                completion_summary = parameters.get("summary", "Task completed.")

        return AgentDecision(
            thought=thought,
            action=action,
            parameters=parameters,
            is_completed=is_completed,
            completion_summary=completion_summary,
            subgoals_completed=subgoals_completed,
            subgoals_remaining=subgoals_remaining,
            objective_complete=objective_complete,
        )
    except Exception as e:
        logger.error(f"Failed to parse {provider_name} response JSON: {e}\nRaw content: {text}")
        return AgentDecision(
            thought=f"Could not parse {provider_name} JSON decision. Waiting briefly before retrying.",
            action="wait",
            parameters={"seconds": 1.0},
            is_completed=False,
            objective_complete=False,
        )

@dataclass
class AgentObservation:
    """Snapshot of current desktop and window state."""
    active_window: Optional[WindowInfo]
    open_windows: List[WindowInfo]
    elements: List[UIElement]
    screenshot_path: Optional[str] = None
    last_action_report: Optional[ActionReport] = None
    browser_url: Optional[str] = None

    def format_summary_for_prompt(self, max_elements: int = 60) -> str:
        """Formats the observation into a clean, token-efficient summary."""
        lines = []

        # 1. Foreground window
        if self.active_window:
            lines.append(f"Active Foreground Window: \"{self.active_window.title}\" "
                         f"[Class={self.active_window.class_name}, PID={self.active_window.process_id}, Bounds={self.active_window.rect}]")
        else:
            lines.append("Active Foreground Window: None (Desktop)")

        # 2. Other open windows
        other_wins = [w.title for w in self.open_windows if w.title and (not self.active_window or w.handle != self.active_window.handle)]
        if other_wins:
            lines.append(f"Other Open Windows: {', '.join([repr(t) for t in other_wins[:10]])}")

        # 3. Last action feedback
        if self.last_action_report:
            status = "SUCCESS" if self.last_action_report.success else "FAILED"
            lines.append(f"Last Action Feedback: [{status}] {self.last_action_report.message}")

        # 4. Interactive elements in active window
        lines.append(f"\nVisible UI Elements in Active Window ({len(self.elements)} detected, showing top {min(len(self.elements), max_elements)}):")
        if not self.elements:
            lines.append("  (No direct UIA elements detected; rely on visual layout or standard keyboard shortcuts)")
        else:
            for idx, el in enumerate(self.elements[:max_elements], 1):
                name = f'"{el.name}"' if el.name else '<unnamed>'
                auto_id = f' id="{el.automation_id}"' if el.automation_id else ''
                coords = f"@ {el.rect.center}" if el.rect.is_valid else "[Offscreen]"
                lines.append(f"  {idx:2d}. [{el.control_type}] {name}{auto_id} {coords}")

        return "\n".join(lines)

@dataclass
class AgentDecision:
    """Action decision produced by the planning layer."""
    thought: str
    action: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    is_completed: bool = False
    completion_summary: Optional[str] = None
    subgoals_completed: List[str] = field(default_factory=list)
    subgoals_remaining: List[str] = field(default_factory=list)
    objective_complete: bool = False

    def to_dict(self) -> dict:
        return {
            "thought": self.thought,
            "action": self.action,
            "parameters": self.parameters,
            "is_completed": self.is_completed,
            "completion_summary": self.completion_summary,
            "subgoals_completed": self.subgoals_completed,
            "subgoals_remaining": self.subgoals_remaining,
            "objective_complete": self.objective_complete,
        }

class OllamaLlavaAgentPlanner:
    """
    Local multimodal planning layer using Ollama vision models such as llava:latest.
    No cloud APIs are called by this planner.
    """

    def __init__(
        self,
        model: str = "llava:latest",
        ollama_url: str = "http://127.0.0.1:11434",
        use_vision: bool = True,
        request_timeout: float = 120.0,
        temperature: float = 0.1,
        num_predict: int = 768,
        num_ctx: int = 8192,
    ):
        self.model = model
        self.ollama_url = ollama_url.rstrip("/")
        self.use_vision = use_vision
        self.request_timeout = request_timeout
        self.temperature = temperature
        self.num_predict = num_predict
        self.num_ctx = num_ctx
        self.last_request_info: Dict[str, Any] = {}

    def plan_next_step(
        self,
        goal: str,
        history: List[Dict[str, Any]],
        observation: AgentObservation,
        task_id: Optional[str] = None,
    ) -> AgentDecision:
        del task_id
        prompt_text = self._build_prompt_text(goal, history, observation)
        images = []
        if self.use_vision and observation.screenshot_path and os.path.exists(observation.screenshot_path):
            try:
                with open(observation.screenshot_path, "rb") as f:
                    images.append(base64.b64encode(f.read()).decode("utf-8"))
            except Exception as e:
                logger.warning(f"Could not encode screenshot for Ollama LLaVA: {e}")

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": prompt_text,
                    **({"images": images} if images else {}),
                },
            ],
            "stream": False,
            "format": "json",
            "think": False,
            "options": {
                "temperature": self.temperature,
                "num_predict": self.num_predict,
                "num_ctx": self.num_ctx,
            },
        }

        start = time.time()
        self.last_request_info = {
            "provider": "ollama",
            "model": self.model,
            "ollama_url": self.ollama_url,
            "image_count": len(images),
            "valid_json": False,
            "duration_sec": None,
        }
        try:
            resp = requests.post(
                f"{self.ollama_url}/api/chat",
                json=payload,
                timeout=self.request_timeout,
            )
            self.last_request_info["duration_sec"] = time.time() - start
            self.last_request_info["status_code"] = resp.status_code
            if resp.status_code != 200:
                return AgentDecision(
                    thought=f"Local Ollama planner returned HTTP {resp.status_code}.",
                    action="fail",
                    parameters={"reason": f"Ollama HTTP {resp.status_code}: {resp.text[:500]}"},
                    is_completed=True,
                    objective_complete=False,
                    completion_summary="Local Ollama planner failed.",
                )

            data = resp.json()
            text = data.get("message", {}).get("content", "")
            decision = parse_agent_decision_json(text, provider_name="Ollama LLaVA")
            self.last_request_info["valid_json"] = decision.action != "wait" or "parse" not in decision.thought.lower()
            self.last_request_info["eval_duration_sec"] = data.get("eval_duration", 0) / 1_000_000_000 if data.get("eval_duration") else None
            self.last_request_info["prompt_eval_count"] = data.get("prompt_eval_count")
            self.last_request_info["eval_count"] = data.get("eval_count")
            return decision
        except requests.exceptions.Timeout:
            self.last_request_info["duration_sec"] = time.time() - start
            return AgentDecision(
                thought=f"Local Ollama planner timed out after {self.request_timeout:.0f}s.",
                action="fail",
                parameters={"reason": f"Ollama timeout after {self.request_timeout:.0f}s"},
                is_completed=True,
                objective_complete=False,
                completion_summary="Local Ollama planner timed out.",
            )
        except Exception as e:
            self.last_request_info["duration_sec"] = time.time() - start
            return AgentDecision(
                thought=f"Local Ollama planner failed: {e}",
                action="fail",
                parameters={"reason": str(e)},
                is_completed=True,
                objective_complete=False,
                completion_summary="Local Ollama planner failed.",
            )

    def _build_prompt_text(
        self,
        goal: str,
        history: List[Dict[str, Any]],
        observation: AgentObservation,
    ) -> str:
        lines = [
            f"HIGH-LEVEL USER TASK:\n\"{goal}\"\n",
            "LOCAL-ONLY RUN: Use only the supplied screenshot, UIA summary, action history, and local reasoning. Do not assume a cloud fallback exists.",
            "COORDINATES: If UIA element names are insufficient, use click_coords with screenshot-relative x/y normalized from 0 to 1000.",
            "YOUTUBE SEARCH CONSTRAINT: Once YouTube is open, use YouTube's own search box for video queries, not the browser address bar.",
            "STATE RULE: If the active window title contains 'YouTube', do not use navigate_url for youtube.com again. The next search step should be type_text with target 'Search' and press_enter true.",
            "REPEAT RULE: Do not repeat the same successful navigate_url or ctrl+t action when the active page/window already advanced to YouTube.",
            "PREVIOUS ACTION HISTORY:",
        ]
        if not history:
            lines.append("  (No previous actions. This is Step 1.)")
        else:
            for item in history:
                step_no = item.get("step", 0)
                action = item.get("action", "")
                params = item.get("parameters", {})
                result = item.get("result", "")
                thought = item.get("thought", "")
                lines.append(f"  Step {step_no}: Action '{action}' {params} -> {result} (Thought: {thought})")

        lines.append("\nCURRENT SCREEN OBSERVATION:")
        lines.append(observation.format_summary_for_prompt(max_elements=90))
        if observation.browser_url:
            lines.append(f"Browser URL: {observation.browser_url}")
        lines.append(
            "\nReturn exactly one next action as pure JSON. Prefer keyboard shortcuts for browser startup/navigation, "
            "but respect the YouTube search constraint after YouTube is loaded."
        )
        return "\n".join(lines)
