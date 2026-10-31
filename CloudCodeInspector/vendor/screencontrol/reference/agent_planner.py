"""
Reasoning & Planning Layer for Computer-Use Agent.
Interfaces with Google Gemini (multimodal) and provides an offline mock planner for testing.
"""

from __future__ import annotations

import os
import re
import json
import base64
import logging
import time
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field
import requests

from core.models import WindowInfo, UIElement, ActionReport, Rect
from core.env import ensure_environment

logger = logging.getLogger("UIA.AgentPlanner")


class GeminiAuthError(Exception):
    """Raised when Gemini API key is missing, unauthorized, or reported as leaked."""
    pass


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


SYSTEM_PROMPT = """You are an autonomous Windows Computer-Use Agent. Your goal is to complete the user's high-level task automatically step-by-step.

At each step, you receive:
1. High-Level User Task
2. Action History (previous steps, decisions, and execution outcomes)
3. Current Screen Observation (Active window, other open windows, list of interactive UI elements, and optional screenshot)

CRITICAL MULTI-STEP COMPLETION RULES:
1. Deconstruct the user's high-level task into all required sub-goals (e.g. Open App -> Open New Tab -> Navigate to URL -> Search Query -> Select/Click Result -> Verify playback / target state).
2. NEVER mark a task complete or return action "complete" until the ENTIRE high-level user objective has been fully executed and verified on screen.
3. NEVER stop merely because:
   - An application or window opened
   - A browser tab was created
   - A website or URL loaded (e.g., navigating to YouTube is NOT completing a task that asked to search or play a video!)
   - A search was entered but results have not yet been selected/opened
   - One intermediate subgoal succeeded
4. Replanning after every screen change:
   - After each action, carefully examine the updated screen observation and UI elements.
   - Explicitly list which subgoals are completed and which subgoals REMAIN in subgoals_completed and subgoals_remaining.
   - Plan the next concrete action to achieve the remaining sub-goals.
5. Only return action "complete" with "is_completed": true and "objective_complete": true when EVERY part of the user's instruction is visibly fulfilled in the final screen observation.

Available Actions:
- "open_app": Launch or activate an application window.
  parameters: {"app_name": "chrome" | "notepad" | "calculator" | ...}
- "focus_window": Bring an existing window to the foreground.
  parameters: {"window_title": "..."}
- "click": Click an element using natural-language grounding.
  parameters: {"target": "element name or description", "button": "left" | "right" | "double"}
- "click_coords": Click physical pixel coordinates (e.g. from visual screenshot).
  parameters: {"x": 1280, "y": 720, "button": "left" | "right" | "double"}
- "type_text": Type text into an element or the currently focused input.
  parameters: {"text": "...", "target": "element name (optional)", "clear_first": false, "press_enter": false}
- "press_key": Send keyboard shortcut or key.
  parameters: {"combo": "ctrl+l" | "enter" | "ctrl+t" | "alt+f4" | "tab" | "esc" | ...}
- "navigate_url": Navigate active browser to URL (focuses address bar, types URL, presses Enter).
  parameters: {"url": "https://..."}
- "wait": Pause briefly for UI animation, page loading, or app startup.
  parameters: {"seconds": 2.0}
- "complete": Conclude task ONLY when the entire user goal is 100% achieved.
  parameters: {"summary": "..."}
- "fail": Conclude task if permanently blocked or unable to proceed.
  parameters: {"reason": "..."}

Guidelines:
1. Reason carefully in the "thought" field before picking an action. Analyze what previous actions accomplished and what is the next logical step.
2. Application & Profile Switching:
   - If the desired application or window is not active, use "open_app" or "focus_window".
   - If the user asks for the Chrome profile picker ("Who's using Chrome?"):
     - If the "Google Chrome" / "Who's using Chrome?" window is already open in other windows, focus it with "focus_window": {"window_title": "Google Chrome"}.
     - If in an existing Chrome window, you can click the profile icon/avatar button in the top right, then click "Manage Chrome profiles".
   - On the profile picker screen, click the button or card for the target profile (e.g. "Open Abdullah_games profile" or "Abdullah_games").
3. Tabs & Navigation:
   - If asked to open a new tab without replacing existing tabs, use "press_key": {"combo": "ctrl+t"}.
   - To open a website (like YouTube), use "navigate_url": {"url": "https://www.youtube.com"}.
4. Searching & Video Playback:
   - To search in a web app like YouTube, locate the search bar and use "type_text": {"text": "...", "target": "Search", "press_enter": true} or click search box then type.
   - To play a video from search results, use "click" targeting the video title or thumbnail (e.g. "FATE", "Alan Walker") or click coordinates.
5. When the user's entire task is accomplished (e.g. the requested video is opened/playing), return action "complete" with "is_completed": true and "objective_complete": true.

Response format: Pure JSON matching this schema:
{
  "subgoals_completed": ["subgoal 1", ...],
  "subgoals_remaining": ["subgoal 2", ...],
  "objective_complete": false,
  "thought": "Your step-by-step reasoning about current state, remaining subgoals, and next action",
  "action": "open_app" | "focus_window" | "click" | "click_coords" | "type_text" | "press_key" | "navigate_url" | "wait" | "complete" | "fail",
  "parameters": { ... },
  "is_completed": false,
  "completion_summary": null
}
"""


from core.quota_manager import QuotaManager, mask_key


class GeminiAgentPlanner:
    """
    Multimodal Gemini planning layer that sends screen state and UI tree to Gemini,
    receiving structured action decisions with quota tracking and model scheduling.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        use_vision: bool = True,
        quota_manager: Optional[QuotaManager] = None,
    ):
        self.quota_manager = quota_manager or QuotaManager()
        if api_key and self.quota_manager.config.projects:
            if api_key not in self.quota_manager.config.projects[0].api_keys:
                self.quota_manager.config.projects[0].api_keys.insert(0, api_key)
        if model and self.quota_manager.is_model_compatible(model):
            self.quota_manager.config.primary_model = model

        self.use_vision = use_vision

    def plan_next_step(
        self,
        goal: str,
        history: List[Dict[str, Any]],
        observation: AgentObservation,
        task_id: Optional[str] = None,
    ) -> AgentDecision:
        """
        Formulates prompt, selects available model/project via QuotaManager,
        queries Gemini API, handles rate limits & fallbacks, and updates quota state.
        """
        prompt_text = self._build_prompt_text(goal, history, observation)
        parts: List[Dict[str, Any]] = [{"text": prompt_text}]

        has_image = False
        if self.use_vision and observation.screenshot_path and os.path.exists(observation.screenshot_path):
            try:
                with open(observation.screenshot_path, "rb") as f:
                    img_bytes = f.read()
                img_b64 = base64.b64encode(img_bytes).decode("utf-8")
                parts.append({
                    "inline_data": {
                        "mime_type": "image/png",
                        "data": img_b64,
                    }
                })
                has_image = True
            except Exception as e:
                logger.warning(f"Could not encode screenshot for Gemini: {e}")

        # Estimate tokens
        est_tokens = len(prompt_text) // 4 + (800 if has_image else 0) + 500

        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": parts,
                }
            ],
            "system_instruction": {
                "parts": [{"text": SYSTEM_PROMPT}]
            },
            "generationConfig": {
                "response_mime_type": "application/json",
                "temperature": 0.1,
            },
        }

        attempted_configs = set()
        max_attempts = len(self.quota_manager.config.get_all_models_in_priority_order()) * len(self.quota_manager.config.projects)
        last_error = None

        while len(attempted_configs) < max_attempts:
            proj, key, model_candidate, wait_sec = self.quota_manager.select_model_and_project(
                task_id=task_id,
                estimated_tokens=est_tokens,
                excluded_configs=attempted_configs,
            )

            # Check if all configurations exhausted
            if not proj or not model_candidate or not key:
                if last_error:
                    break
                print(self.quota_manager.format_all_exhausted_message())
                return AgentDecision(
                    thought="All Gemini model configurations are exhausted or unavailable.",
                    action="fail",
                    parameters={"reason": "All Gemini model quotas exhausted", "all_exhausted": True},
                    is_completed=True,
                    completion_summary="Execution stopped: All Gemini model quotas exhausted.",
                )

            # Check for RPM/TPM cooldown wait
            if wait_sec > 0:
                if wait_sec <= self.quota_manager.config.max_cooldown_wait_sec:
                    logger.info(f"[QUOTA] Model '{model_candidate}' in cooldown for {wait_sec:.1f}s. Waiting...")
                    time.sleep(wait_sec)
                else:
                    logger.warning(f"[QUOTA] Wait {wait_sec:.1f}s exceeds threshold. Re-evaluating candidate...")
                    attempted_configs.add((proj.id, model_candidate))
                    continue

            attempted_configs.add((proj.id, model_candidate))
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_candidate}:generateContent"
            headers = {"x-goog-api-key": key}
            try:
                logger.info(f"[QUOTA SCHEDULER] Dispatching step to model '{model_candidate}' (Project: {proj.id}, Key: {mask_key(key)})")
                resp = requests.post(url, json=payload, headers=headers, timeout=30)

                if resp.status_code == 200:
                    data = resp.json()
                    # Capture actual tokens from API usageMetadata
                    actual_tokens = est_tokens
                    prompt_tokens = 0
                    completion_tokens = 0
                    is_actual = False
                    if "usageMetadata" in data:
                        usage = data["usageMetadata"]
                        actual_tokens = usage.get("totalTokenCount", est_tokens)
                        prompt_tokens = usage.get("promptTokenCount", 0)
                        completion_tokens = usage.get("candidatesTokenCount", 0)
                        is_actual = True
                        logger.info(
                            f"[QUOTA USAGE] Real Gemini usageMetadata received: total={actual_tokens}, "
                            f"prompt={prompt_tokens}, candidates={completion_tokens}"
                        )
                    self.quota_manager.record_success(
                        proj.id,
                        model_candidate,
                        tokens_used=actual_tokens,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        is_actual=is_actual,
                    )

                    candidates = data.get("candidates", [])
                    if candidates:
                        text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "{}")
                        return self._parse_gemini_response(text)

                elif resp.status_code == 429:
                    last_error = f"Gemini rate limit (HTTP 429) on {model_candidate}"
                    retry_header = resp.headers.get("Retry-After")
                    retry_sec = float(retry_header) if retry_header and retry_header.isdigit() else 30.0
                    self.quota_manager.record_error(proj.id, model_candidate, 429, resp.text, retry_sec)
                    logger.warning(f"[QUOTA 429] Model '{model_candidate}' hit rate limit. Switching to fallback...")
                    continue

                elif resp.status_code in (400, 401, 403):
                    err_msg = resp.text
                    last_error = f"Gemini rejected {model_candidate} (HTTP {resp.status_code})"
                    self.quota_manager.record_error(proj.id, model_candidate, resp.status_code, err_msg)
                    if any(term in err_msg for term in ["API_KEY", "API key", "leaked", "PERMISSION_DENIED", "CREDENTIAL"]):
                        logger.error(f"[QUOTA AUTH] Key {mask_key(key)} invalid for project {proj.id}.")
                        # Check if another project has an available key
                        continue
                    logger.error(f"Gemini API error {resp.status_code}: {err_msg}")

                elif resp.status_code == 404:
                    last_error = f"Gemini model {model_candidate} is unavailable (HTTP 404)"
                    self.quota_manager.record_error(proj.id, model_candidate, 404, resp.text)
                    logger.warning(f"Model '{model_candidate}' returned 404, marking disabled...")
                    continue

                else:
                    last_error = f"Gemini {model_candidate} returned HTTP {resp.status_code}"
                    self.quota_manager.record_error(proj.id, model_candidate, resp.status_code, resp.text, retry_after_sec=5.0)
                    logger.warning(f"[QUOTA {resp.status_code}] Model '{model_candidate}' returned error {resp.status_code}. Retrying fallback candidate...")
                    continue

            except requests.RequestException as e:
                last_error = f"Could not reach Gemini: {type(e).__name__}"
                self.quota_manager.record_error(proj.id, model_candidate, 500, str(e), retry_after_sec=5.0)
                logger.warning(f"Network error querying Gemini ({model_candidate}): {e}. Retrying fallback candidate...")
                continue

        # If all attempts failed
        return AgentDecision(
            thought="Could not obtain a valid response from Gemini API across available configurations.",
            action="fail",
            parameters={"reason": last_error or "Gemini API unavailable or request failed"},
            is_completed=True,
            completion_summary="Failed to connect to Gemini API",
            objective_complete=False,
        )

    def _build_prompt_text(
        self,
        goal: str,
        history: List[Dict[str, Any]],
        observation: AgentObservation,
    ) -> str:
        lines = [
            f"HIGH-LEVEL USER TASK:\n\"{goal}\"\n",
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
        lines.append(observation.format_summary_for_prompt())

        lines.append(
            "\nDecide the next single action to move closer to completing the goal.\n"
            "Assess remaining sub-goals. Do NOT mark complete until the complete high-level objective is fully satisfied.\n"
            "Return pure JSON."
        )
        return "\n".join(lines)

    def _parse_gemini_response(self, text: str) -> AgentDecision:
        """Parses model text into AgentDecision."""
        return parse_agent_decision_json(text, provider_name="Gemini")


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


class MockAgentPlanner:
    """
    Deterministic rule-based mock planner for offline verification and local testing.
    Handles the standard Chrome YouTube task and general test patterns.
    """

    def __init__(self):
        self.step_counter = 0

    def plan_next_step(
        self,
        goal: str,
        history: List[Dict[str, Any]],
        observation: AgentObservation,
    ) -> AgentDecision:
        self.step_counter += 1
        goal_lower = goal.lower()

        # Check if active window is Chrome
        active_title = (observation.active_window.title if observation.active_window else "").lower()
        has_chrome_open = any("chrome" in w.title.lower() for w in observation.open_windows)

        # Notepad workflow
        if "notepad" in goal_lower:
            opened_notepad = any(h.get("action") == "open_app" and "notepad" in str(h.get("parameters")).lower() for h in history)
            if not opened_notepad and "notepad" not in active_title:
                return AgentDecision(
                    thought="Opening Notepad application.",
                    action="open_app",
                    parameters={"app_name": "notepad"},
                    subgoals_completed=[],
                    subgoals_remaining=["type_text"],
                    objective_complete=False,
                )

            # Check if text needs to be typed
            if "write" in goal_lower or "type" in goal_lower:
                text_match = re.search(r'(?:write|type)\s+["\']?([^"\']+)["\']?', goal, re.IGNORECASE)
                text_to_type = text_match.group(1).strip() if text_match else "hello Abdullah"
                typed = any(h.get("action") == "type_text" for h in history)
                if not typed:
                    return AgentDecision(
                        thought=f"Typing '{text_to_type}' into Notepad.",
                        action="type_text",
                        parameters={"text": text_to_type, "target": "Text Editor", "clear_first": False, "press_enter": True},
                        subgoals_completed=["open_notepad"],
                        subgoals_remaining=[],
                        objective_complete=False,
                    )

            return AgentDecision(
                thought="Notepad text written successfully. Goal complete.",
                action="complete",
                parameters={"summary": f"Opened Notepad and typed text."},
                is_completed=True,
                completion_summary=f"Opened Notepad and typed requested text.",
                subgoals_completed=["open_notepad", "type_text"],
                subgoals_remaining=[],
                objective_complete=True,
            )

        # Chrome + YouTube + Search workflow
        if "chrome" in goal_lower and "youtube" in goal_lower:
            opened_chrome = any(h.get("action") == "open_app" and "chrome" in str(h.get("parameters")).lower() for h in history)

            # Profile Picker & Window Focus
            has_picker = any("who's using chrome" in w.title.lower() or w.title == "Google Chrome" for w in observation.open_windows)
            picker_focused = observation.active_window and ("who's using chrome" in observation.active_window.title.lower() or observation.active_window.title == "Google Chrome")

            # Target Profile name
            target_profile = "Abdullah_games" if "abdullah_games" in goal_lower else "Abdullah"

            # Step 1: Open Chrome / Profile Picker if needed
            if "profile picker" in goal_lower or "who's using chrome" in goal_lower:
                if has_picker and not picker_focused:
                    return AgentDecision(
                        thought="Switching focus to the 'Who's using Chrome?' profile picker window.",
                        action="focus_window",
                        parameters={"window_title": "Google Chrome"},
                        subgoals_completed=[],
                        subgoals_remaining=["open_profile", "new_tab", "navigate_url", "search", "play_video"],
                        objective_complete=False,
                    )
                elif not has_picker and not opened_chrome and "chrome" not in active_title:
                    return AgentDecision(
                        thought="Opening Chrome profile picker.",
                        action="open_app",
                        parameters={"app_name": "chrome --profile-picker"},
                        subgoals_completed=[],
                        subgoals_remaining=["open_profile", "new_tab", "navigate_url", "search", "play_video"],
                        objective_complete=False,
                    )

            if "chrome" not in active_title and not opened_chrome and not picker_focused:
                return AgentDecision(
                    thought="Chrome is not in the active foreground. Opening/activating Chrome.",
                    action="open_app",
                    parameters={"app_name": "chrome"},
                    subgoals_completed=[],
                    subgoals_remaining=["new_tab", "navigate_url", "search", "play_video"],
                    objective_complete=False,
                )

            # Step 2: Handle profile selection
            if "profile" in goal_lower:
                profile_handled = any(
                    (h.get("action") == "click" and any(p in str(h.get("parameters")).lower() for p in [target_profile.lower(), "abdullah"]))
                    for h in history
                )
                if not profile_handled:
                    has_card = any(target_profile.lower() in el.name.lower() for el in observation.elements)
                    card_target = f"Open {target_profile} profile" if has_card else target_profile
                    return AgentDecision(
                        thought=f"Identified '{target_profile}' profile in Chrome. Clicking to select profile.",
                        action="click",
                        parameters={"target": card_target, "button": "left"},
                        subgoals_completed=["open_chrome"],
                        subgoals_remaining=["new_tab", "navigate_url", "search", "play_video"],
                        objective_complete=False,
                    )

            # Step 3: Open new tab with Ctrl+T if requested
            if "ctrl+t" in goal_lower or "new tab" in goal_lower:
                new_tab_done = any(h.get("action") == "press_key" and "ctrl+t" in str(h.get("parameters")).lower() for h in history)
                if not new_tab_done:
                    return AgentDecision(
                        thought="Opening a new tab using Ctrl+T without replacing any existing tab.",
                        action="press_key",
                        parameters={"combo": "ctrl+t"},
                        subgoals_completed=["open_chrome"],
                        subgoals_remaining=["navigate_url", "search", "play_video"],
                        objective_complete=False,
                    )

            # Step 4: Navigate to YouTube if not already on YouTube
            navigated_yt = any(h.get("action") == "navigate_url" and "youtube" in str(h.get("parameters")).lower() for h in history)
            if "youtube" not in active_title and not navigated_yt:
                return AgentDecision(
                    thought="Navigating to YouTube in the new tab.",
                    action="navigate_url",
                    parameters={"url": "https://www.youtube.com"},
                    subgoals_completed=["open_chrome", "new_tab"],
                    subgoals_remaining=["search", "play_video"],
                    objective_complete=False,
                )

            # Step 5: Search for query (e.g. 'FATE Alan Walker Ava Max' or 'Alan Walker Faded')
            search_query = "Alan Walker Faded"
            query_match = re.search(r"search (?:for )?['\"]?([^'\",]+)['\"]?", goal, re.IGNORECASE)
            if query_match:
                search_query = query_match.group(1).strip()

            searched = any(h.get("action") == "type_text" and any(w in str(h.get("parameters")).lower() for w in search_query.lower().split()[:2]) for h in history)
            if not searched:
                return AgentDecision(
                    thought=f"YouTube loaded. Entering search query '{search_query}' into search box and pressing Enter.",
                    action="type_text",
                    parameters={"text": search_query, "target": "Search", "clear_first": False, "press_enter": True},
                    subgoals_completed=["open_chrome", "new_tab", "navigate_url"],
                    subgoals_remaining=["select_video", "play_video"],
                    objective_complete=False,
                )

            # Step 6: Play the official video
            if "play" in goal_lower or "video" in goal_lower:
                video_played = any(h.get("action") == "click" and any(k in str(h.get("parameters")).lower() for k in ["fate", "alan walker", "faded", "video"]) for h in history)
                if not video_played:
                    # Look for video result in elements
                    video_target = "Alan Walker - Faded"
                    for el in observation.elements:
                        if any(k in el.name.lower() for k in ["faded", "fate", "alan walker", "official video"]):
                            video_target = el.name
                            break
                    return AgentDecision(
                        thought=f"Search results displayed. Clicking on '{video_target}' to play the official video.",
                        action="click",
                        parameters={"target": video_target, "button": "left"},
                        subgoals_completed=["open_chrome", "new_tab", "navigate_url", "search"],
                        subgoals_remaining=["play_video"],
                        objective_complete=False,
                    )

            # Step 7: Verification & Task Complete
            return AgentDecision(
                thought=f"Video playback started on YouTube for '{search_query}'. Task fully accomplished.",
                action="complete",
                parameters={"summary": f"Successfully opened Chrome, opened a new tab via Ctrl+T, navigated to YouTube, searched for '{search_query}', and played the official video."},
                is_completed=True,
                completion_summary=f"Successfully opened Chrome, opened a new tab via Ctrl+T, navigated to YouTube, searched for '{search_query}', and played the official video.",
                subgoals_completed=["open_chrome", "new_tab", "navigate_url", "search", "play_video"],
                subgoals_remaining=[],
                objective_complete=True,
            )

        # Generic fallback workflow
        if self.step_counter > 3:
            return AgentDecision(
                thought="Task steps simulated successfully.",
                action="complete",
                parameters={"summary": f"Task '{goal}' executed."},
                is_completed=True,
                completion_summary=f"Task '{goal}' executed.",
                subgoals_completed=["executed_actions"],
                subgoals_remaining=[],
                objective_complete=True,
            )

        return AgentDecision(
            thought=f"Executing next automated action for '{goal}'.",
            action="wait",
            parameters={"seconds": 1.0},
            subgoals_completed=[],
            subgoals_remaining=["pending_actions"],
            objective_complete=False,
        )


class OpenAIAgentPlanner:
    """
    Multimodal OpenAI planning layer (supporting GPT-4o / GPT-4o-mini)
    that sends desktop observations and UI tree to OpenAI for structured computer-use planning.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "gpt-4o",
        use_vision: bool = True,
    ):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.model = model
        self.use_vision = use_vision

    def plan_next_step(
        self,
        goal: str,
        history: List[Dict[str, Any]],
        observation: AgentObservation,
        task_id: Optional[str] = None,
    ) -> AgentDecision:
        prompt_text = self._build_prompt_text(goal, history, observation)
        content_parts: List[Dict[str, Any]] = [{"type": "text", "text": prompt_text}]

        if self.use_vision and observation.screenshot_path and os.path.exists(observation.screenshot_path):
            try:
                with open(observation.screenshot_path, "rb") as f:
                    img_bytes = f.read()
                img_b64 = base64.b64encode(img_bytes).decode("utf-8")
                content_parts.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{img_b64}"
                    }
                })
            except Exception as e:
                logger.warning(f"Could not encode screenshot for OpenAI: {e}")

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": content_parts}
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        try:
            resp = requests.post("https://api.openai.com/v1/chat/completions", json=payload, headers=headers, timeout=45)
            if resp.status_code == 200:
                data = resp.json()
                text = data["choices"][0]["message"]["content"]
                return self._parse_response(text)
            else:
                logger.error(f"OpenAI API error {resp.status_code}: {resp.text}")
                error_msg = f"HTTP {resp.status_code}"
                try:
                    err_data = resp.json().get("error", {})
                    if isinstance(err_data, dict):
                        raw_msg = err_data.get("message", error_msg)
                        if err_data.get("code") == "credit_balance_exhausted" or "credit" in raw_msg.lower():
                            error_msg = f"No credits remaining (Account credit balance exhausted). Please add credits at https://platform.openai.com/settings/organization/billing/ ({raw_msg})"
                        else:
                            error_msg = raw_msg
                except Exception:
                    error_msg = resp.text[:200]

                return AgentDecision(
                    thought=f"OpenAI API error: {error_msg}",
                    action="fail",
                    parameters={"reason": error_msg},
                    is_completed=True,
                    completion_summary=f"OpenAI Error: {error_msg}",
                    objective_complete=False,
                )
        except Exception as e:
            logger.error(f"Error querying OpenAI API: {e}")
            return AgentDecision(
                thought=f"Network error querying OpenAI API: {e}",
                action="fail",
                parameters={"reason": str(e)},
                is_completed=True,
                completion_summary=f"OpenAI Connection Error: {e}",
                objective_complete=False,
            )

    def _build_prompt_text(
        self,
        goal: str,
        history: List[Dict[str, Any]],
        observation: AgentObservation,
    ) -> str:
        lines = [
            f"HIGH-LEVEL USER TASK:\n\"{goal}\"\n",
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
        lines.append(observation.format_summary_for_prompt())
        lines.append(
            "\nDecide the next single action to move closer to completing the goal.\n"
            "Assess remaining sub-goals. Do NOT mark complete until the complete high-level objective is fully satisfied.\n"
            "Return pure JSON."
        )
        return "\n".join(lines)

    def _parse_response(self, text: str) -> AgentDecision:
        try:
            cleaned = text.strip()
            if cleaned.startswith("```"):
                cleaned = re.sub(r"^```(?:json)?\n?", "", cleaned)
                cleaned = re.sub(r"\n?```$", "", cleaned)

            data = json.loads(cleaned)
            thought = data.get("thought", "Proceeding with next step.")
            action = data.get("action", "wait").lower().strip()
            parameters = data.get("parameters", {})
            is_completed = data.get("is_completed", False)
            completion_summary = data.get("completion_summary", None)
            subgoals_completed = data.get("subgoals_completed", [])
            subgoals_remaining = data.get("subgoals_remaining", [])
            objective_complete = data.get("objective_complete", is_completed)

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
            logger.error(f"Failed to parse OpenAI JSON response: {e}")
            return AgentDecision(
                thought="Failed to parse model JSON decision.",
                action="wait",
                parameters={"seconds": 1.0},
                is_completed=False,
                objective_complete=False,
            )

