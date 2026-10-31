"""
Data models representing UI elements, windows, search matches, and action execution logs.
"""

from dataclasses import dataclass, field
from typing import Optional, List, Any, Tuple


@dataclass
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    @property
    def center_x(self) -> int:
        return self.left + self.width // 2

    @property
    def center_y(self) -> int:
        return self.top + self.height // 2

    @property
    def center(self) -> Tuple[int, int]:
        return (self.center_x, self.center_y)

    @property
    def is_valid(self) -> bool:
        return self.width > 0 and self.height > 0

    def to_dict(self) -> dict:
        return {
            "left": self.left,
            "top": self.top,
            "right": self.right,
            "bottom": self.bottom,
            "width": self.width,
            "height": self.height,
            "center": self.center,
        }

    def __str__(self) -> str:
        return f"({self.left},{self.top},{self.right},{self.bottom}) [{self.width}x{self.height}] Center={self.center}"


@dataclass
class UIElement:
    name: str
    control_type: str
    automation_id: str
    class_name: str
    rect: Rect
    is_enabled: bool = True
    is_offscreen: bool = False
    depth: int = 0
    handle: int = 0
    patterns: List[str] = field(default_factory=list)
    raw_control: Any = None

    @property
    def display_name(self) -> str:
        return self.name.strip() if self.name else "<unnamed>"

    def summary(self) -> str:
        name_str = f'"{self.name}"' if self.name else '<unnamed>'
        id_str = f' id="{self.automation_id}"' if self.automation_id else ''
        return f"[{self.control_type}] {name_str}{id_str} @ {self.rect.center} ({self.rect.width}x{self.rect.height})"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "control_type": self.control_type,
            "automation_id": self.automation_id,
            "class_name": self.class_name,
            "rect": self.rect.to_dict(),
            "is_enabled": self.is_enabled,
            "is_offscreen": self.is_offscreen,
            "depth": self.depth,
            "patterns": self.patterns,
        }


@dataclass
class WindowInfo:
    title: str
    class_name: str
    handle: int
    process_id: int
    rect: Rect
    is_minimized: bool = False
    is_visible: bool = True
    raw_control: Any = None

    def summary(self) -> str:
        status = " (minimized)" if self.is_minimized else ""
        return f'Window: "{self.title}" [PID={self.process_id}, HWND={hex(self.handle)}] {self.rect}{status}'


@dataclass
class MatchCandidate:
    element: UIElement
    score: float
    match_reason: str

    def summary(self) -> str:
        return f"{self.score:.2f} ({self.match_reason}) -> {self.element.summary()}"


@dataclass
class ResolutionResult:
    query: str
    target_element: Optional[UIElement]
    confidence: float
    candidates: List[MatchCandidate] = field(default_factory=list)
    is_ambiguous: bool = False
    used_fallback: bool = False
    fallback_reason: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.target_element is not None and self.target_element.rect.is_valid


@dataclass
class ActionReport:
    action_type: str  # 'click', 'double_click', 'right_click', 'type', 'key', 'fallback_click'
    target_query: str
    target_element: Optional[UIElement]
    coordinates_used: Optional[Tuple[int, int]]
    success: bool
    message: str
    duration_ms: float = 0.0
    used_fallback: bool = False
    fallback_reason: Optional[str] = None
    dpi_info: Optional[str] = None
