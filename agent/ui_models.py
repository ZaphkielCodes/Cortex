"""
agent/ui_models.py
------------------
Data models for Cortex General Computer Use (Phase A10).

Provides structured representations for:
- UIElement: Bounded interactive UI control with accessibility and semantic metadata
- UIObservation: Structured snapshot of visible desktop / application interface
- UIActionResult: Deterministic execution result with verification metadata
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple, Union
import uuid


@dataclass
class UIElement:
    """
    Structured representation of a single UI control or visual element.
    Bounds format: [x, y, width, height] in screen pixels.
    """
    element_id: str
    role: str                       # e.g. "button", "input", "edit", "text", "link", "checkbox", "menuitem", "window", "pane"
    label: str                      # Accessible name or visible text label
    text: str = ""                  # Value pattern, edit text, or inner text
    bounds: List[int] = field(default_factory=lambda: [0, 0, 0, 0])  # [x, y, width, height]
    confidence: float = 1.0         # 1.0 for native OS accessibility tree, ~0.8-0.95 for vision
    enabled: bool = True            # Is the control active/clickable
    metadata: Dict[str, Any] = field(default_factory=dict)  # control_type_id, automation_id, class_name, etc.

    @property
    def center_point(self) -> Tuple[int, int]:
        """Return the center point (x, y) of the element's bounding box."""
        if len(self.bounds) >= 4:
            x, y, w, h = self.bounds[0], self.bounds[1], self.bounds[2], self.bounds[3]
            return (x + max(1, w // 2), y + max(1, h // 2))
        return (0, 0)

    @property
    def x(self) -> int:
        return self.bounds[0] if len(self.bounds) >= 1 else 0

    @property
    def y(self) -> int:
        return self.bounds[1] if len(self.bounds) >= 2 else 0

    @property
    def width(self) -> int:
        return self.bounds[2] if len(self.bounds) >= 3 else 0

    @property
    def height(self) -> int:
        return self.bounds[3] if len(self.bounds) >= 4 else 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UIElement":
        return cls(
            element_id=data.get("element_id") or f"el_{uuid.uuid4().hex[:6]}",
            role=data.get("role") or "unknown",
            label=data.get("label") or "",
            text=data.get("text") or "",
            bounds=list(data.get("bounds") or [0, 0, 0, 0]),
            confidence=float(data.get("confidence", 1.0)),
            enabled=bool(data.get("enabled", True)),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass
class UIObservation:
    """
    Structured scene and element observation of the current desktop or foreground interface.
    Carries a unique observation_id for fencing stale-screen actions.
    """
    observation_id: str
    device_id: str
    timestamp: str
    screen_size: Dict[str, int]               # {"width": int, "height": int}
    foreground_app: str
    window_title: str
    summary: str
    elements: List[UIElement] = field(default_factory=list)
    window_id: Optional[int] = None
    window_bounds: Optional[List[int]] = None # [x, y, width, height]
    state_hash: str = ""
    source_image_reference: Optional[str] = None
    provenance: Dict[str, Any] = field(default_factory=dict)
    pid: Optional[int] = None
    process_name: Optional[str] = None

    def __post_init__(self):
        if not self.state_hash:
            self.state_hash = self.compute_state_hash()

    def compute_state_hash(self) -> str:
        """Deterministic hash representing the active UI layout state."""
        raw = json.dumps({"device":self.device_id,"window":self.window_id,"app":self.foreground_app,
            "title":self.window_title,"bounds":self.window_bounds,"elements":[
                {"role":e.role,"label":e.label,"text":e.text,"bounds":e.bounds,"enabled":e.enabled,
                 "metadata":e.metadata} for e in self.elements]},sort_keys=True,ensure_ascii=False,default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["elements"] = [e.to_dict() if isinstance(e, UIElement) else e for e in self.elements]
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UIObservation":
        raw_elements = data.get("elements") or []
        parsed_elements = [
            UIElement.from_dict(e) if isinstance(e, dict) else e
            for e in raw_elements
        ]
        return cls(
            observation_id=data.get("observation_id") or f"obs_{uuid.uuid4().hex[:8]}",
            device_id=data.get("device_id") or "asus",
            timestamp=data.get("timestamp") or datetime.now(timezone.utc).isoformat(),
            screen_size=data.get("screen_size") or {"width": 1920, "height": 1080},
            foreground_app=data.get("foreground_app") or "Desktop",
            window_title=data.get("window_title") or "",
            summary=data.get("summary") or "",
            elements=parsed_elements,
            window_id=data.get("window_id"),
            window_bounds=data.get("window_bounds"),
            state_hash=data.get("state_hash") or "",
            source_image_reference=data.get("source_image_reference"),
            provenance=dict(data.get("provenance") or {}),
            pid=data.get("pid") or (data.get("provenance", {}).get("window_identity") or {}).get("pid"),
            process_name=data.get("process_name") or data.get("foreground_app"),
        )

    def find_element_by_id(self, element_id: str) -> Optional[UIElement]:
        for el in self.elements:
            if el.element_id == element_id:
                return el
        return None

    def find_elements_by_role(self, role: str) -> List[UIElement]:
        role_lower = role.lower()
        return [e for e in self.elements if e.role.lower() == role_lower]

    def find_elements_by_label(self, label: str, case_sensitive: bool = False) -> List[UIElement]:
        target = label if case_sensitive else label.lower().strip()
        matches = []
        for e in self.elements:
            candidate = e.label if case_sensitive else e.label.lower().strip()
            if candidate == target:
                matches.append(e)
        return matches


@dataclass
class UIActionResult:
    """Structured result from executing a bounded UI interaction."""
    status: str                         # "success" | "failure" | "stale_observation" | "requires_approval"
    action: str                         # e.g. "ui.click", "ui.type"
    target: Optional[str] = None        # element_id or target label
    target_label: Optional[str] = None
    target_role: Optional[str] = None
    observation_id: Optional[str] = None
    coordinates: Optional[List[int]] = None
    executed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    verification_required: bool = True
    verified: Optional[bool] = None
    verification_details: Dict[str, Any] = field(default_factory=dict)
    message: str = ""
    error: Optional[str] = None
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
