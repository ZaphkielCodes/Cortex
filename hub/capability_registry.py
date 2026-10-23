"""
hub/capability_registry.py
--------------------------
Canonical Capability Registry for Cortex.
Single source of truth for all Cortex core capabilities.

Exposes:
- name
- description
- argument schema
- required arguments
- optional arguments
- target type
- permission tier
- risk
- classification
- runtime availability
- device availability
- provider
- side-effect class
- privacy metadata
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union


@dataclass
class RegisteredCapability:
    """Canonical capability record including schema, availability, risk, and provider status."""
    name: str
    description: str
    arguments: Dict[str, Any]
    risk: str = "low"  # "low", "medium", "high"
    classification: str = "read"  # "read", "write", "destructive", "device_action"
    target_type: str = "device"  # "hub" or "device"
    permission_tier: int = 1
    availability: bool = True
    result_source: Optional[str] = None  # Execution provenance category, never NL routing.
    provider: Optional[str] = None
    provider_status: Dict[str, Any] = field(default_factory=dict)
    side_effect_class: Optional[str] = None  # "none", "state_change", "filesystem", "system", "destructive"
    privacy_metadata: Dict[str, Any] = field(default_factory=dict)
    device_availability: bool = True

    @property
    def required_arguments(self) -> List[str]:
        return [
            k for k, v in self.arguments.items()
            if isinstance(v, dict) and v.get("required") is True
        ]

    @property
    def optional_arguments(self) -> List[str]:
        return [
            k for k, v in self.arguments.items()
            if not (isinstance(v, dict) and v.get("required") is True)
        ]

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["required_arguments"] = self.required_arguments
        d["optional_arguments"] = self.optional_arguments
        return d


# Backwards compatibility alias for planner.py
CapabilitySchema = RegisteredCapability


class CanonicalCapabilityRegistry:
    """
    Canonical single source of truth for Cortex capabilities.
    Consumed by:
    - SystemRegistry
    - GoalPlanner
    - TaskOrchestrator
    - AgentHarness / SemanticInterpreter
    - ExecutorSelector
    - PrivacyPolicyEngine / ControlLayer
    """

    _instance: Optional["CanonicalCapabilityRegistry"] = None

    def __init__(self):
        self._capabilities: Dict[str, RegisteredCapability] = {}
        self.reload()

    @classmethod
    def get_instance(cls) -> "CanonicalCapabilityRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        cls._instance = None

    def register(self, cap: RegisteredCapability) -> None:
        """Register or override a capability."""
        self._capabilities[cap.name] = cap

    def _refresh_dynamic_providers(self) -> None:
        """Dynamically update capability availability based on live providers."""
        from .integrations.ecosystem import IntegrationRegistry
        for cap in IntegrationRegistry.get_instance().canonical_capabilities():
            self._capabilities[cap.name] = cap
        try:
            from .weather_provider import get_weather_provider
            wp = get_weather_provider()
            w_avail = (wp is not None)
            for w_name in ("weather.current", "weather.forecast"):
                if w_name in self._capabilities:
                    self._capabilities[w_name].availability = w_avail
                    if "available" in self._capabilities[w_name].provider_status:
                        self._capabilities[w_name].provider_status["available"] = w_avail
        except Exception:
            pass

        try:
            from .search_provider import get_search_provider
            sp = get_search_provider()
            s_avail = (sp is not None)
            if "web.search" in self._capabilities:
                self._capabilities["web.search"].availability = s_avail
                if "available" in self._capabilities["web.search"].provider_status:
                    self._capabilities["web.search"].provider_status["available"] = s_avail
        except Exception:
            pass

        try:
            from .vision_provider import get_vision_provider
            vp = get_vision_provider()
            v_avail = (vp is not None and getattr(vp, "is_available", lambda: True)())
            if "screen.analyze" in self._capabilities:
                self._capabilities["screen.analyze"].availability = v_avail
                if "available" in self._capabilities["screen.analyze"].provider_status:
                    self._capabilities["screen.analyze"].provider_status["available"] = v_avail
        except Exception:
            pass

    def get_capability(self, name: str) -> Optional[RegisteredCapability]:
        """Retrieve capability by name."""
        self._refresh_dynamic_providers()
        return self._capabilities.get(name)

    def list_capabilities(self) -> List[RegisteredCapability]:
        """Return list of all registered capabilities."""
        self._refresh_dynamic_providers()
        return sorted((c for c in self._capabilities.values() if not c.provider_status.get('discovery_only')),
            key=lambda c:-c.provider_status.get("preference",0) if c.availability else 0)

    def get_all(self) -> Dict[str, RegisteredCapability]:
        """Return dictionary of all registered capabilities."""
        self._refresh_dynamic_providers()
        return dict(self._capabilities)

    def get_available_capabilities(
        self,
        device_id: Optional[str] = None,
        filter_supported: Optional[List[str]] = None,
    ) -> List[RegisteredCapability]:
        """
        Return capabilities that are available and optionally supported by a target device.
        """
        self._refresh_dynamic_providers()
        caps = [c for c in self._capabilities.values() if c.availability and not c.provider_status.get('discovery_only')]
        if device_id:
            from .executor_registry import ExecutorRegistry
            handle = ExecutorRegistry.get_instance().get_device_handle(device_id)
            if handle and handle.metadata.get('capability_discovery'):
                caps = [c for c in caps if c.target_type == 'hub' or
                        handle.online and handle.supports_capability(c.name)]
        if filter_supported is not None:
            supported_set = set(filter_supported)
            caps = [c for c in caps if c.name in supported_set or c.target_type == "hub"]
        return sorted(caps,key=lambda c:-c.provider_status.get("preference",0))

    def validate_call(
        self,
        capability_name: str,
        arguments: Optional[Dict[str, Any]] = None,
        target_device: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        Validate whether a capability call is valid against schema and availability.
        Returns (is_valid, error_reason).
        """
        cap = self.get_capability(capability_name)
        if not cap:
            return False, f"Capability '{capability_name}' is not registered in Cortex."

        if not cap.availability:
            return False, f"Capability '{capability_name}' is currently unavailable."

        args = arguments or {}
        for req_arg in cap.required_arguments:
            if req_arg not in args or args[req_arg] is None or args[req_arg] == "":
                return False, f"Missing required argument '{req_arg}' for capability '{capability_name}'."

        return True, None

    def reload(self) -> None:
        """Load or refresh all canonical capabilities with live provider statuses."""
        try:
            from .search_provider import get_search_provider
            search_p = get_search_provider()
        except Exception:
            search_p = None

        try:
            from .weather_provider import get_weather_provider
            weather_p = get_weather_provider()
        except Exception:
            weather_p = None

        try:
            from .vision_provider import get_vision_provider
            vision_p = get_vision_provider()
        except Exception:
            vision_p = None

        caps: Dict[str, RegisteredCapability] = {
            # Web & Search
            "web.search": RegisteredCapability(
                name="web.search",
                result_source="live_search",
                description="Search the live web using a search provider and return top results with URLs and snippets.",
                arguments={
                    "query": {"type": "string", "required": True, "description": "Search query keywords"},
                    "max_results": {"type": "integer", "required": False, "description": "Maximum number of search results (default 5)"},
                    "recency_days": {"type": "integer", "required": False, "description": "Optional recency filter in days"},
                    "domains": {"type": "array", "required": False, "description": "Optional list of domains to filter results by"},
                },
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=(search_p is not None),
                provider=(search_p.provider_name if search_p else None),
                provider_status={"provider": search_p.provider_name if search_p else None, "available": (search_p is not None)},
                side_effect_class="none",
            ),
            "web.fetch": RegisteredCapability(
                name="web.fetch",
                description="Safely retrieve and parse readable content from a specific HTTP/HTTPS URL.",
                arguments={
                    "url": {"type": "string", "required": True, "description": "Public HTTP or HTTPS URL to fetch"},
                    "max_chars": {"type": "integer", "required": False, "description": "Maximum number of characters to extract (default 5000)"},
                },
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="cortex_fetch",
                provider_status={"provider": "cortex_fetch", "available": True},
                side_effect_class="none",
            ),
            "weather.current": RegisteredCapability(
                name="weather.current",
                result_source="live_weather",
                description="Get real-time current weather conditions for a specified location.",
                arguments={
                    "location": {"type": "string", "required": True, "description": "City or location name to query"},
                    "units": {"type": "string", "required": False, "description": "Temperature units: 'metric' (default) or 'imperial'"},
                },
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=(weather_p is not None),
                provider=(weather_p.provider_name if weather_p else None),
                provider_status={"provider": weather_p.provider_name if weather_p else None, "available": (weather_p is not None)},
                side_effect_class="none",
            ),
            "weather.forecast": RegisteredCapability(
                name="weather.forecast",
                result_source="live_weather",
                description="Get multi-day weather forecast for a specified location.",
                arguments={
                    "location": {"type": "string", "required": True, "description": "City or location name to query"},
                    "units": {"type": "string", "required": False, "description": "Temperature units: 'metric' (default) or 'imperial'"},
                },
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=(weather_p is not None),
                provider=(weather_p.provider_name if weather_p else None),
                provider_status={"provider": weather_p.provider_name if weather_p else None, "available": (weather_p is not None)},
                side_effect_class="none",
            ),
            "browser.open_url": RegisteredCapability(
                name="browser.open_url",
                description="Open a validated URL in the default browser on the target device.",
                arguments={
                    "url": {"type": "string", "required": True, "description": "Web URL or destination to open in browser"},
                    "target_device": {"type": "string", "required": False, "description": "Device on which to open the browser"},
                },
                risk="low",
                classification="device_action",
                target_type="device",
                permission_tier=1,
                availability=True,
                provider="system_browser",
                provider_status={"provider": "system_browser", "available": True},
                side_effect_class="system",
            ),

            # Screen & Vision
            "screen.capture": RegisteredCapability(
                name="screen.capture",
                description="Capture the real OS screen display, bounded display or region.",
                arguments={
                    "target_device": {"type": "string", "required": False, "description": "Device on which to capture screen"},
                    "display_index": {"type": "integer", "required": False, "description": "Optional display monitor index"},
                    "region": {"type": "object", "required": False, "description": "Optional bounding box: {x, y, width, height}"},
                },
                risk="low",
                classification="read",
                target_type="device",
                permission_tier=1,
                availability=True,
                provider="os_display",
                provider_status={"provider": "os_display", "available": True},
                side_effect_class="none",
            ),
            "screenshot.read": RegisteredCapability(
                name="screenshot.read",
                description="Capture a full screenshot of the target device display.",
                arguments={
                    "target_device": {"type": "string", "required": False, "description": "Target device ID"},
                },
                risk="low",
                classification="read",
                target_type="device",
                permission_tier=1,
                availability=True,
                provider="os_display",
                provider_status={"provider": "os_display", "available": True},
                side_effect_class="none",
            ),
            "screen.analyze": RegisteredCapability(
                name="screen.analyze",
                description="Analyze a captured screen image to describe UI, identify open apps, inspect errors, or locate visual elements.",
                arguments={
                    "screenshot": {"type": "string", "required": False, "description": "Screenshot file path or image reference"},
                    "image_ref": {"type": "string", "required": False, "description": "Image file reference or path"},
                    "question": {"type": "string", "required": False, "description": "Specific question about visible screen contents"},
                    "mode": {"type": "string", "required": False, "description": "Analysis mode: 'describe' | 'locate' | 'inspect'"},
                },
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=(vision_p is not None),
                provider=(vision_p.provider_name if vision_p else None),
                provider_status={"provider": vision_p.provider_name if vision_p else None, "available": (vision_p is not None)},
                side_effect_class="none",
            ),

            # General Computer Use (Phase A10)
            "ui.observe": RegisteredCapability(
                name="ui.observe",
                description="Read foreground app metadata by default. Explicitly request include_tree=true for interactive controls.",
                arguments={
                    "target_device": {"type": "string", "required": False, "description": "Target device ID"},
                    "include_tree": {"type": "boolean", "required": False, "description": "Whether to extract OS accessibility elements (default False; explicit opt-in)"},
                    "include_vision": {"type": "boolean", "required": False, "description": "Whether to include multimodal vision perception fallback"},
                    "target_window_id": {"type": "integer", "required": False, "description": "Optional specific window HWND to inspect"},
                    "mode": {"type": "string", "required": False, "description": "Observation mode: 'full' or 'fast'"},
                },
                risk="low",
                classification="read",
                target_type="device",
                permission_tier=1,
                availability=True,
                provider="ui_observer",
                provider_status={"provider": "ui_observer", "available": True},
                side_effect_class="none",
            ),
            "ui.click": RegisteredCapability(
                name="ui.click",
                description="Click a grounded UI element, semantic label, or bounded coordinate with observation version fencing.",
                arguments={
                    "target": {"type": "string", "required": False, "description": "Element ID, semantic label, or selector to click"},
                    "x": {"type": "integer", "required": False, "description": "Optional explicit horizontal coordinate"},
                    "y": {"type": "integer", "required": False, "description": "Optional explicit vertical coordinate"},
                    "observation_id": {"type": "string", "required": False, "description": "Observation version ID for stale-screen fencing"},
                    "button": {"type": "string", "required": False, "description": "Mouse button: 'left' (default) or 'right'"},
                    "target_device": {"type": "string", "required": False, "description": "Target device ID"},
                },
                risk="low",
                classification="device_action",
                target_type="device",
                permission_tier=1,
                availability=True,
                provider="ui_executor",
                provider_status={"provider": "ui_executor", "available": True},
                side_effect_class="state_change",
            ),
            "ui.double_click": RegisteredCapability(
                name="ui.double_click",
                description="Double-click a grounded UI element or coordinate.",
                arguments={
                    "target": {"type": "string", "required": False, "description": "Element ID, semantic label, or selector to double-click"},
                    "x": {"type": "integer", "required": False, "description": "Optional explicit horizontal coordinate"},
                    "y": {"type": "integer", "required": False, "description": "Optional explicit vertical coordinate"},
                    "observation_id": {"type": "string", "required": False, "description": "Observation version ID for stale-screen fencing"},
                    "target_device": {"type": "string", "required": False, "description": "Target device ID"},
                },
                risk="low",
                classification="device_action",
                target_type="device",
                permission_tier=1,
                availability=True,
                provider="ui_executor",
                provider_status={"provider": "ui_executor", "available": True},
                side_effect_class="state_change",
            ),
            "ui.right_click": RegisteredCapability(
                name="ui.right_click",
                description="Right-click a grounded UI element or coordinate to open context menus.",
                arguments={
                    "target": {"type": "string", "required": False, "description": "Element ID, semantic label, or selector to right-click"},
                    "x": {"type": "integer", "required": False, "description": "Optional explicit horizontal coordinate"},
                    "y": {"type": "integer", "required": False, "description": "Optional explicit vertical coordinate"},
                    "observation_id": {"type": "string", "required": False, "description": "Observation version ID for stale-screen fencing"},
                    "target_device": {"type": "string", "required": False, "description": "Target device ID"},
                },
                risk="low",
                classification="device_action",
                target_type="device",
                permission_tier=1,
                availability=True,
                provider="ui_executor",
                provider_status={"provider": "ui_executor", "available": True},
                side_effect_class="state_change",
            ),
            "ui.move_pointer": RegisteredCapability(
                name="ui.move_pointer",
                description="Move mouse pointer to grounded element or coordinates.",
                arguments={
                    "target": {"type": "string", "required": False, "description": "Target element ID or label"},
                    "x": {"type": "integer", "required": False, "description": "Target horizontal coordinate"},
                    "y": {"type": "integer", "required": False, "description": "Target vertical coordinate"},
                    "observation_id": {"type": "string", "required": False, "description": "Observation version ID"},
                    "target_device": {"type": "string", "required": False, "description": "Target device ID"},
                },
                risk="low",
                classification="device_action",
                target_type="device",
                permission_tier=1,
                availability=True,
                provider="ui_executor",
                provider_status={"provider": "ui_executor", "available": True},
                side_effect_class="none",
            ),
            "ui.scroll": RegisteredCapability(
                name="ui.scroll",
                description="Scroll interface vertically or horizontally by specified amount.",
                arguments={
                    "direction": {"type": "string", "required": False, "description": "Scroll direction: 'down' (default), 'up', 'left', 'right'"},
                    "amount": {"type": "integer", "required": False, "description": "Scroll delta amount (default 300)"},
                    "target": {"type": "string", "required": False, "description": "Optional element to hover over while scrolling"},
                    "observation_id": {"type": "string", "required": False, "description": "Observation version ID"},
                    "target_device": {"type": "string", "required": False, "description": "Target device ID"},
                },
                risk="low",
                classification="device_action",
                target_type="device",
                permission_tier=1,
                availability=True,
                provider="ui_executor",
                provider_status={"provider": "ui_executor", "available": True},
                side_effect_class="none",
            ),
            "ui.type": RegisteredCapability(
                name="ui.type",
                description="Enter exact text into an observed editable field in any application, browser search field, chat composer or editor. Requires verified focus; does not submit. Observe afterward to verify the field value.",
                arguments={
                    "text": {"type": "string", "required": True, "description": "Text to enter, or credential placeholder"},
                    "target": {"type": "string", "required": False, "description": "Observed editable element ID (preferred) or unique exact label; omitted only for an observed focused editable field"},
                    "clear_existing": {"type": "boolean", "required": False, "description": "Whether to clear existing text before typing"},
                    "press_enter": {"type": "boolean", "required": False, "description": "Must be false. Submission is a separate authorized ui.keypress or ui.click after verification"},
                    "is_credential": {"type": "boolean", "required": False, "description": "Whether the input is a sensitive secret requiring masking"},
                    "secret_id": {"type": "string", "required": False, "description": "Optional stored secret identifier"},
                    "observation_id": {"type": "string", "required": False, "description": "Observation version ID"},
                    "target_device": {"type": "string", "required": False, "description": "Target device ID"},
                },
                risk="low",
                classification="device_action",
                target_type="device",
                permission_tier=1,
                availability=True,
                provider="ui_executor",
                provider_status={"provider": "ui_executor", "available": True},
                side_effect_class="state_change",
            ),
            "ui.keypress": RegisteredCapability(
                name="ui.keypress",
                description="Press an individual keyboard key (enter, tab, escape, backspace, space, etc.).",
                arguments={
                    "key": {"type": "string", "required": True, "description": "Key name to press"},
                    "observation_id": {"type": "string", "required": False, "description": "Observation version ID"},
                    "target_device": {"type": "string", "required": False, "description": "Target device ID"},
                },
                risk="low",
                classification="device_action",
                target_type="device",
                permission_tier=1,
                availability=True,
                provider="ui_executor",
                provider_status={"provider": "ui_executor", "available": True},
                side_effect_class="state_change",
            ),
            "ui.hotkey": RegisteredCapability(
                name="ui.hotkey",
                description="Press a keyboard shortcut combination (e.g. ['ctrl', 'a'], ['alt', 'left']).",
                arguments={
                    "keys": {"type": "array", "required": True, "description": "List or string of key names in combination"},
                    "observation_id": {"type": "string", "required": False, "description": "Observation version ID"},
                    "target_device": {"type": "string", "required": False, "description": "Target device ID"},
                },
                risk="low",
                classification="device_action",
                target_type="device",
                permission_tier=1,
                availability=True,
                provider="ui_executor",
                provider_status={"provider": "ui_executor", "available": True},
                side_effect_class="state_change",
            ),
            "ui.drag": RegisteredCapability(
                name="ui.drag",
                description="Drag and drop between two points or UI elements.",
                arguments={
                    "from_x": {"type": "integer", "required": False, "description": "Source X coordinate"},
                    "from_y": {"type": "integer", "required": False, "description": "Source Y coordinate"},
                    "to_x": {"type": "integer", "required": False, "description": "Destination X coordinate"},
                    "to_y": {"type": "integer", "required": False, "description": "Destination Y coordinate"},
                    "from_target": {"type": "string", "required": False, "description": "Source element ID or label"},
                    "to_target": {"type": "string", "required": False, "description": "Destination element ID or label"},
                    "observation_id": {"type": "string", "required": False, "description": "Observation version ID"},
                    "target_device": {"type": "string", "required": False, "description": "Target device ID"},
                },
                risk="low",
                classification="device_action",
                target_type="device",
                permission_tier=1,
                availability=True,
                provider="ui_executor",
                provider_status={"provider": "ui_executor", "available": True},
                side_effect_class="state_change",
            ),
            "ui.select": RegisteredCapability(
                name="ui.select",
                description="Select an option or item in a dropdown, combobox, or list control.",
                arguments={
                    "target": {"type": "string", "required": False, "description": "Dropdown or list element ID/label"},
                    "value": {"type": "string", "required": True, "description": "Option value or text to select"},
                    "observation_id": {"type": "string", "required": False, "description": "Observation version ID"},
                    "target_device": {"type": "string", "required": False, "description": "Target device ID"},
                },
                risk="low",
                classification="device_action",
                target_type="device",
                permission_tier=1,
                availability=True,
                provider="ui_executor",
                provider_status={"provider": "ui_executor", "available": True},
                side_effect_class="state_change",
            ),
            "ui.wait": RegisteredCapability(
                name="ui.wait",
                description="Sleep for a bounded duration in seconds, returning only after the pause elapses (maximum 30 seconds).",
                arguments={
                    "seconds": {"type": "number", "required": False, "description": "Duration to wait in seconds (default 1.0, max 30.0)"},
                    "target_device": {"type": "string", "required": False, "description": "Target device ID"},
                },
                risk="low",
                classification="read",
                target_type="device",
                permission_tier=1,
                availability=True,
                provider="ui_executor",
                provider_status={"provider": "ui_executor", "available": True},
                side_effect_class="none",
            ),
            "clipboard.read": RegisteredCapability(
                name="clipboard.read",
                description="Safely inspect and read text from the target device clipboard.",
                arguments={
                    "target_device": {"type": "string", "required": False, "description": "Target device ID"},
                },
                risk="low",
                classification="read",
                target_type="device",
                permission_tier=1,
                availability=True,
                provider="clipboard",
                provider_status={"provider": "clipboard", "available": True},
                side_effect_class="none",
            ),
            "clipboard.write": RegisteredCapability(
                name="clipboard.write",
                description="Safely copy text into the target device clipboard.",
                arguments={
                    "text": {"type": "string", "required": True, "description": "Text content to copy to clipboard"},
                    "target_device": {"type": "string", "required": False, "description": "Target device ID"},
                },
                risk="low",
                classification="device_action",
                target_type="device",
                permission_tier=1,
                availability=True,
                provider="clipboard",
                provider_status={"provider": "clipboard", "available": True},
                side_effect_class="state_change",
            ),

            # Apps & Windows
            "app.list_running": RegisteredCapability(
                name="app.list_running",
                description="Inspect running applications with process names, PIDs, executable paths, and window counts.",
                arguments={},
                risk="low",
                classification="read",
                target_type="device",
                permission_tier=1,
                availability=True,
                provider="os_process_table",
                provider_status={"provider": "os_process_table", "available": True},
                side_effect_class="none",
            ),
            "process.list": RegisteredCapability(
                name="process.list",
                description="Inspect running processes with PID, executable path, process name, memory usage in bytes and percent, and CPU percent.",
                arguments={
                    "sort_by": {"type": "string", "required": False, "description": "Sort criterion: 'memory' (default), 'cpu', or 'name'"},
                    "limit": {"type": "integer", "required": False, "description": "Maximum number of processes to return (default 20)"},
                    "target_device": {"type": "string", "required": False, "description": "Target device ID"},
                },
                risk="low",
                classification="read",
                target_type="device",
                permission_tier=1,
                availability=True,
                provider="os_process_table",
                provider_status={"provider": "os_process_table", "available": True},
                side_effect_class="none",
            ),
            "app.launch": RegisteredCapability(
                name="app.launch",
                description="Launch an application, verify its process/window and activate its window unless activate=false.",
                arguments={
                    "app": {"type": "string", "required": True, "description": "Application display name, alias, or executable target"},
                    "activate": {"type":"boolean","required":False,"description":"Bring the launched window to foreground (default True). False launches without changing focus."},
                    "target_device": {"type": "string", "required": False, "description": "Target device ID"},
                },
                risk="medium",
                classification="write",
                target_type="device",
                permission_tier=1,
                availability=True,
                provider="os_process_launcher",
                provider_status={"provider": "os_process_launcher", "available": True},
                side_effect_class="state_change",
            ),
            "app.close": RegisteredCapability(
                name="app.close",
                description="Close an application gracefully or forcefully, verifying process termination.",
                arguments={
                    "app": {"type": "string", "required": False, "description": "Application name or process to close"},
                    "process": {"type": "string", "required": False, "description": "Process name"},
                    "window_id": {"type": "integer", "required": False, "description": "Window handle to close"},
                    "graceful": {"type": "boolean", "required": False, "description": "Whether to request graceful termination (default True)"},
                    "force": {"type": "boolean", "required": False, "description": "Whether to force-kill process (default False)"},
                    "target_device": {"type": "string", "required": False, "description": "Target device ID"},
                },
                risk="medium",
                classification="write",
                target_type="device",
                permission_tier=1,
                availability=True,
                provider="os_process_manager",
                provider_status={"provider": "os_process_manager", "available": True},
                side_effect_class="state_change",
            ),
            "window.list": RegisteredCapability(
                name="window.list",
                description="Enumerate visible windows with handle IDs, titles, process info, visibility, and bounds.",
                arguments={
                    "target_device": {"type": "string", "required": False, "description": "Target device ID"},
                },
                risk="low",
                classification="read",
                target_type="device",
                permission_tier=1,
                availability=True,
                provider="os_window_manager",
                provider_status={"provider": "os_window_manager", "available": True},
                side_effect_class="none",
            ),
            "window.focus": RegisteredCapability(
                name="window.focus",
                description="Restore if minimized and switch foreground focus to the target window.",
                arguments={
                    "pid": {"type": "integer", "required": False, "description": "Observed process identity; rejects a recycled handle owned by another process"},
                    "window_id": {"type": "integer", "required": False, "description": "Target window handle / ID"},
                    "title": {"type": "string", "required": False, "description": "Window title to match"},
                    "process": {"type": "string", "required": False, "description": "Process name to match"},
                    "app": {"type": "string", "required": False, "description": "Application name to match"},
                    "target_device": {"type": "string", "required": False, "description": "Target device ID"},
                },
                risk="low",
                classification="write",
                target_type="device",
                permission_tier=1,
                availability=True,
                provider="os_window_manager",
                provider_status={"provider": "os_window_manager", "available": True},
                side_effect_class="state_change",
            ),
            "window.minimize": RegisteredCapability(
                name="window.minimize",
                description="Minimize a visible window to the taskbar.",
                arguments={
                    "window_id": {"type": "integer", "required": False},
                    "title": {"type": "string", "required": False},
                    "target_device": {"type": "string", "required": False},
                },
                risk="low",
                classification="write",
                target_type="device",
                permission_tier=1,
                availability=True,
                provider="os_window_manager",
                provider_status={"provider": "os_window_manager", "available": True},
                side_effect_class="state_change",
            ),
            "window.maximize": RegisteredCapability(
                name="window.maximize",
                description="Maximize a window to full screen.",
                arguments={
                    "window_id": {"type": "integer", "required": False},
                    "title": {"type": "string", "required": False},
                    "target_device": {"type": "string", "required": False},
                },
                risk="low",
                classification="write",
                target_type="device",
                permission_tier=1,
                availability=True,
                provider="os_window_manager",
                provider_status={"provider": "os_window_manager", "available": True},
                side_effect_class="state_change",
            ),
            "window.restore": RegisteredCapability(
                name="window.restore",
                description="Restore a minimized or maximized window to normal state.",
                arguments={
                    "window_id": {"type": "integer", "required": False},
                    "title": {"type": "string", "required": False},
                    "target_device": {"type": "string", "required": False},
                },
                risk="low",
                classification="write",
                target_type="device",
                permission_tier=1,
                availability=True,
                provider="os_window_manager",
                provider_status={"provider": "os_window_manager", "available": True},
                side_effect_class="state_change",
            ),

            # Filesystem Operations
            "fs.open": RegisteredCapability(
                name="fs.open",
                description="Open a file with its default application, or a directory in File Explorer.",
                arguments={
                    "path": {"type": "string", "required": True, "description": "File or folder path to open"},
                    "target_device": {"type": "string", "required": False, "description": "Target device ID"},
                },
                risk="medium",
                classification="write",
                target_type="device",
                permission_tier=1,
                availability=True,
                provider="os_shell",
                provider_status={"provider": "os_shell", "available": True},
                side_effect_class="system",
            ),
            "fs.read": RegisteredCapability(
                name="fs.read",
                description="Read and retrieve contents of a local file safely.",
                arguments={
                    "path": {"type": "string", "required": True, "description": "File path to read"},
                    "max_chars": {"type": "integer", "required": False, "description": "Max characters to read"},
                    "target_device": {"type": "string", "required": False, "description": "Target device ID"},
                },
                risk="low",
                classification="read",
                target_type="device",
                permission_tier=1,
                availability=True,
                provider="os_filesystem",
                provider_status={"provider": "os_filesystem", "available": True},
                side_effect_class="none",
            ),
            "fs.list": RegisteredCapability(
                name="fs.list",
                description="List direct children of a directory with file metadata (name, type, extension, size, modified, path).",
                arguments={
                    "path": {"type": "string", "required": True, "description": "Directory path to list"},
                    "max_results": {"type": "integer", "required": False, "description": "Maximum number of items to return"},
                    "include_hidden": {"type": "boolean", "required": False, "description": "Whether to include hidden files"},
                    "target_device": {"type": "string", "required": False, "description": "Target device ID"},
                },
                risk="low",
                classification="read",
                target_type="device",
                permission_tier=1,
                availability=True,
                provider="os_filesystem",
                provider_status={"provider": "os_filesystem", "available": True},
                side_effect_class="none",
            ),
            "fs.stat": RegisteredCapability(
                name="fs.stat",
                description="Inspect metadata of a specific file or directory.",
                arguments={
                    "path": {"type": "string", "required": True, "description": "Path to inspect"},
                    "target_device": {"type": "string", "required": False, "description": "Target device ID"},
                },
                risk="low",
                classification="read",
                target_type="device",
                permission_tier=1,
                availability=True,
                provider="os_filesystem",
                provider_status={"provider": "os_filesystem", "available": True},
                side_effect_class="none",
            ),
            "fs.shortcut_info": RegisteredCapability(
                name="fs.shortcut_info",
                description="Inspect metadata and target path of a Windows shortcut (.lnk file).",
                arguments={
                    "path": {"type": "string", "required": True, "description": "Path to shortcut to inspect"},
                    "target_device": {"type": "string", "required": False, "description": "Target device ID"},
                },
                risk="low",
                classification="read",
                target_type="device",
                permission_tier=1,
                availability=True,
                provider="os_filesystem",
                provider_status={"provider": "os_filesystem", "available": True},
                side_effect_class="none",
            ),
            "fs.copy": RegisteredCapability(
                name="fs.copy",
                description="Copy a file or directory with overwrite protection and verification.",
                arguments={
                    "src": {"type": "string", "required": True, "description": "Source path"},
                    "dst": {"type": "string", "required": True, "description": "Destination path"},
                    "overwrite": {"type": "boolean", "required": False, "description": "Allow overwriting destination (default False)"},
                    "target_device": {"type": "string", "required": False, "description": "Target device ID"},
                },
                risk="medium",
                classification="write",
                target_type="device",
                permission_tier=2,
                availability=True,
                provider="os_filesystem",
                provider_status={"provider": "os_filesystem", "available": True},
                side_effect_class="filesystem",
            ),
            "fs.move": RegisteredCapability(
                name="fs.move",
                description="Move a file or directory with overwrite protection and verification.",
                arguments={
                    "src": {"type": "string", "required": True, "description": "Source path"},
                    "dst": {"type": "string", "required": True, "description": "Destination path"},
                    "overwrite": {"type": "boolean", "required": False, "description": "Allow overwriting destination (default False)"},
                    "target_device": {"type": "string", "required": False, "description": "Target device ID"},
                },
                risk="medium",
                classification="write",
                target_type="device",
                permission_tier=2,
                availability=True,
                provider="os_filesystem",
                provider_status={"provider": "os_filesystem", "available": True},
                side_effect_class="filesystem",
            ),
            "fs.rename": RegisteredCapability(
                name="fs.rename",
                description="Rename a file or directory with overwrite protection and verification.",
                arguments={
                    "src": {"type": "string", "required": True, "description": "Source path"},
                    "new_name": {"type": "string", "required": True, "description": "New filename or target path"},
                    "overwrite": {"type": "boolean", "required": False, "description": "Allow overwriting destination (default False)"},
                    "target_device": {"type": "string", "required": False, "description": "Target device ID"},
                },
                risk="medium",
                classification="write",
                target_type="device",
                permission_tier=2,
                availability=True,
                provider="os_filesystem",
                provider_status={"provider": "os_filesystem", "available": True},
                side_effect_class="filesystem",
            ),
            "fs.delete": RegisteredCapability(
                name="fs.delete",
                description="Permanently delete a file or directory. Destructive action verified absent.",
                arguments={
                    "path": {"type": "string", "required": True, "description": "Path to delete"},
                    "target_device": {"type": "string", "required": False, "description": "Target device ID"},
                },
                risk="high",
                classification="destructive",
                target_type="device",
                permission_tier=3,
                availability=True,
                provider="os_filesystem",
                provider_status={"provider": "os_filesystem", "available": True},
                side_effect_class="destructive",
            ),

            # Device Telemetry & Notifications
            "device.status": RegisteredCapability(
                name="device.status",
                description="Read battery charge and AC power status.",
                arguments={
                    "target_device": {"type": "string", "required": False, "description": "Target device ID"},
                },
                risk="low",
                classification="read",
                target_type="device",
                permission_tier=1,
                availability=True,
                provider="os_device_telemetry",
                provider_status={"provider": "os_device_telemetry", "available": True},
                side_effect_class="none",
            ),
            "device.info": RegisteredCapability(
                name="device.info",
                description="Query device hardware specifications, OS, hostname, and identity.",
                arguments={
                    "target_device": {"type": "string", "required": False, "description": "Target device ID"},
                },
                risk="low",
                classification="read",
                target_type="device",
                permission_tier=1,
                availability=True,
                provider="os_device_telemetry",
                provider_status={"provider": "os_device_telemetry", "available": True},
                side_effect_class="none",
            ),
            "ui.notify": RegisteredCapability(
                name="ui.notify",
                description="Display a visual desktop notification or alert dialog to the user.",
                arguments={
                    "message": {"type": "string", "required": True, "description": "Message content to display"},
                    "title": {"type": "string", "required": False, "description": "Optional notification title"},
                    "target_device": {"type": "string", "required": False, "description": "Target device ID"},
                },
                risk="low",
                classification="write",
                target_type="device",
                permission_tier=1,
                availability=True,
                provider="os_notification",
                provider_status={"provider": "os_notification", "available": True},
                side_effect_class="system",
            ),
            "shell.run": RegisteredCapability(
                name="shell.run",
                description="Execute a bounded shell command on the target device.",
                arguments={
                    "command": {"type": "string", "required": True, "description": "Shell command line to execute"},
                    "target_device": {"type": "string", "required": False, "description": "Target device ID"},
                },
                risk="high",
                classification="destructive",
                target_type="device",
                permission_tier=3,
                availability=True,
                provider="os_shell",
                provider_status={"provider": "os_shell", "available": True},
                side_effect_class="system",
            ),

            # Goals & Wait Conditions
            "goal.wait": RegisteredCapability(
                name="goal.wait",
                description="Check whether a goal condition is satisfied. GoalRunner persists unsatisfied conditions; this call does not sleep.",
                arguments={
                    "condition_type": {"type": "string", "required": True, "description": "Condition type: 'world_state', 'device_online', 'time'"},
                    "condition_key": {"type": "string", "required": True, "description": "Condition key or device ID to monitor"},
                    "operator": {"type": "string", "required": False, "description": "Comparison operator (default '==')"},
                    "expected_value": {"type": "any", "required": False, "description": "Expected value for satisfaction"},
                },
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="goal_manager",
                provider_status={"provider": "goal_manager", "available": True},
                side_effect_class="none",
            ),
            "goal.create": RegisteredCapability(
                name="goal.create",
                description="Create and register a new persistent autonomous goal in Cortex.",
                arguments={
                    "title": {"type": "string", "required": True, "description": "Title of the goal"},
                    "original_request": {"type": "string", "required": False, "description": "Original natural user request"},
                },
                risk="low",
                classification="write",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="goal_manager",
                provider_status={"provider": "goal_manager", "available": True},
                side_effect_class="state_change",
            ),
            "goal.get": RegisteredCapability(
                name="goal.get",
                description="Inspect details, status, steps, and progress of a persistent goal.",
                arguments={
                    "goal_id": {"type": "string", "required": True, "description": "Goal ID"},
                },
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="goal_manager",
                provider_status={"provider": "goal_manager", "available": True},
                side_effect_class="none",
            ),
            "goal.list": RegisteredCapability(
                name="goal.list",
                description="List all persistent goals optionally filtered by status.",
                arguments={
                    "status": {"type": "string", "required": False, "description": "Filter by status: ACTIVE, PENDING, WAITING, COMPLETED, FAILED, CANCELLED"},
                },
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="goal_manager",
                provider_status={"provider": "goal_manager", "available": True},
                side_effect_class="none",
            ),
            "goal.cancel": RegisteredCapability(
                name="goal.cancel",
                description="Cancel an active or waiting persistent goal.",
                arguments={
                    "goal_id": {"type": "string", "required": True, "description": "Goal ID"},
                    "reason": {"type": "string", "required": False, "description": "Cancellation reason"},
                },
                risk="low",
                classification="write",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="goal_manager",
                provider_status={"provider": "goal_manager", "available": True},
                side_effect_class="state_change",
            ),
            "goal.resume": RegisteredCapability(
                name="goal.resume",
                description="Resume execution of a waiting or paused persistent goal.",
                arguments={
                    "goal_id": {"type": "string", "required": True, "description": "Goal ID"},
                },
                risk="low",
                classification="write",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="goal_manager",
                provider_status={"provider": "goal_manager", "available": True},
                side_effect_class="state_change",
            ),

            # Phase A4: System Registry & Self-Knowledge
            "system.runtime_state": RegisteredCapability(
                name="system.runtime_state",
                description="Inspect the requesting device's tasks, execution ownership, handoff/wait metadata, transfer progress and current privacy policy.",
                arguments={}, risk="low", classification="read", target_type="hub", permission_tier=1,
                provider="runtime_introspection", side_effect_class="none",
                privacy_metadata={"privacy_class": "SENSITIVE"}),
            "system.identity": RegisteredCapability(
                name="system.identity",
                description="Inspect Cortex product identity, version, architecture mode, and node role.",
                arguments={},
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="system_registry",
                provider_status={"provider": "system_registry", "available": True},
                side_effect_class="none",
            ),
            "system.models": RegisteredCapability(
                name="system.models",
                description="Inspect active intelligence providers and models for reasoning, vision, and planning.",
                arguments={},
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="system_registry",
                provider_status={"provider": "system_registry", "available": True},
                side_effect_class="none",
            ),
            "system.list_capabilities": RegisteredCapability(
                name="system.list_capabilities",
                description="Enumerate all registered capabilities with live runtime availability.",
                arguments={},
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="system_registry",
                provider_status={"provider": "system_registry", "available": True},
                side_effect_class="none",
            ),
            "system.capability_status": RegisteredCapability(
                name="system.capability_status",
                description="Check status and availability for a specific capability or list of capabilities.",
                arguments={
                    "capability": {"type": "string", "required": False, "description": "Capability name or comma-separated names to check"},
                },
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="system_registry",
                provider_status={"provider": "system_registry", "available": True},
                side_effect_class="none",
            ),
            "system.list_devices": RegisteredCapability(
                name="system.list_devices",
                description="List all enrolled devices with online/offline state and advertised capabilities.",
                arguments={},
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="system_registry",
                provider_status={"provider": "system_registry", "available": True},
                side_effect_class="none",
            ),
            "system.device_status": RegisteredCapability(
                name="system.device_status",
                description="Inspect detailed state, roles, and capabilities for a specific device.",
                arguments={
                    "device_id": {"type": "string", "required": False, "description": "Device ID or name"},
                },
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="system_registry",
                provider_status={"provider": "system_registry", "available": True},
                side_effect_class="none",
            ),
            "system.list_integrations": RegisteredCapability(
                name="system.list_integrations",
                description="Enumerate external integrations with live OAuth authorization and scope status.",
                arguments={},
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="system_registry",
                provider_status={"provider": "system_registry", "available": True},
                side_effect_class="none",
            ),
            "system.integration_status": RegisteredCapability(
                name="system.integration_status",
                description="Check authorization and connection state for a specific service integration.",
                arguments={
                    "integration": {"type": "string", "required": False, "description": "Integration name (e.g. 'gmail', 'calendar')"},
                },
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="system_registry",
                provider_status={"provider": "system_registry", "available": True},
                side_effect_class="none",
            ),
            "system.providers": RegisteredCapability(
                name="system.providers",
                description="Report live configured and available status of all subsystems with secret masking.",
                arguments={},
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="system_registry",
                provider_status={"provider": "system_registry", "available": True},
                side_effect_class="none",
            ),
            "system.explain_capability": RegisteredCapability(
                name="system.explain_capability",
                description="Explain why a requested capability or task cannot run (unsupported, missing integration, disabled provider, etc.).",
                arguments={
                    "query": {"type": "string", "required": True, "description": "Requested capability or task to explain"},
                },
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="system_registry",
                provider_status={"provider": "system_registry", "available": True},
                side_effect_class="none",
            ),
            # Telemetry
            "telemetry.get_system_usage": RegisteredCapability(
                name="telemetry.get_system_usage",
                description="Read CPU, RAM and disk usage metrics.",
                arguments={
                    "target_device": {"type": "string", "required": False, "description": "Device ID to inspect (default local device)"},
                },
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="world_state",
                provider_status={"provider": "world_state", "available": True},
                side_effect_class="none",
            ),
            "telemetry.get_hardware_info": RegisteredCapability(
                name="telemetry.get_hardware_info",
                description="Inspect device hardware metadata (manufacturer, model, processor, GPU, OS).",
                arguments={
                    "target_device": {"type": "string", "required": False, "description": "Device ID to inspect (default local device)"},
                },
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="world_state",
                provider_status={"provider": "world_state", "available": True},
                side_effect_class="none",
            ),
            # Calendar
            "calendar.status": RegisteredCapability(
                name="calendar.status",
                description="Check Google Calendar integration connection and account status.",
                arguments={},
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="calendar_adapter",
                provider_status={"provider": "calendar_adapter", "available": True},
                side_effect_class="none",
            ),
            "calendar.get_current": RegisteredCapability(
                name="calendar.get_current",
                description="Get currently active event from calendar.",
                arguments={},
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=False,
                provider="calendar_adapter",
                provider_status={"provider": "calendar_adapter", "available": False,
                                 "discovery_only": True, "reason": "unimplemented_legacy_capability"},
                side_effect_class="none",
            ),
            "calendar.get_next": RegisteredCapability(
                name="calendar.get_next",
                description="Get the next upcoming event from calendar.",
                arguments={},
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=False,
                provider="calendar_adapter",
                provider_status={"provider": "calendar_adapter", "available": False,
                                 "discovery_only": True, "reason": "unimplemented_legacy_capability"},
                side_effect_class="none",
            ),
            "calendar.get_availability": RegisteredCapability(
                name="calendar.get_availability",
                description="Check whether user is currently free or busy according to calendar.",
                arguments={},
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=False,
                provider="calendar_adapter",
                provider_status={"provider": "calendar_adapter", "available": False,
                                 "discovery_only": True, "reason": "unimplemented_legacy_capability"},
                side_effect_class="none",
            ),
            # Workload
            "workload.add_item": RegisteredCapability(
                name="workload.add_item",
                description="Add a new work item, task, or reminder to the workload backlog.",
                arguments={
                    "title": {"type": "string", "required": True, "description": "Title of the item"},
                    "type": {"type": "string", "required": False, "description": "Type of item (assignment, project, exam, meeting, task)"},
                    "due_at": {"type": "string", "required": False, "description": "Due date in ISO-8601"},
                },
                risk="low",
                classification="write",
                target_type="hub",
                permission_tier=2,
                availability=True,
                provider="workload_manager",
                provider_status={"provider": "workload_manager", "available": True},
                side_effect_class="none",
            ),
            "workload.get_item": RegisteredCapability(
                name="workload.get_item",
                description="Retrieve details of a specific work item by ID.",
                arguments={
                    "work_item_id": {"type": "string", "required": True, "description": "ID of work item"},
                },
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="workload_manager",
                provider_status={"provider": "workload_manager", "available": True},
                side_effect_class="none",
            ),
            "workload.update_item": RegisteredCapability(
                name="workload.update_item",
                description="Update fields of a work item.",
                arguments={
                    "work_item_id": {"type": "string", "required": True, "description": "ID of work item"},
                },
                risk="low",
                classification="write",
                target_type="hub",
                permission_tier=2,
                availability=True,
                provider="workload_manager",
                provider_status={"provider": "workload_manager", "available": True},
                side_effect_class="none",
            ),
            "workload.complete_item": RegisteredCapability(
                name="workload.complete_item",
                description="Mark a work item as completed.",
                arguments={
                    "work_item_id": {"type": "string", "required": False, "description": "ID of work item"},
                    "title": {"type": "string", "required": False, "description": "Title of work item"},
                },
                risk="low",
                classification="write",
                target_type="hub",
                permission_tier=2,
                availability=True,
                provider="workload_manager",
                provider_status={"provider": "workload_manager", "available": True},
                side_effect_class="none",
            ),
            # Speakers & Social
            "speaker.get_current": RegisteredCapability(
                name="speaker.get_current",
                description="Get active speaker identification.",
                arguments={},
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="speaker_manager",
                provider_status={"provider": "speaker_manager", "available": True},
                side_effect_class="none",
            ),
            "speakers.get_current": RegisteredCapability(
                name="speakers.get_current",
                description="Get active speaker identification.",
                arguments={},
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="speaker_manager",
                provider_status={"provider": "speaker_manager", "available": True},
                side_effect_class="none",
            ),
            "speaker.resolve": RegisteredCapability(
                name="speaker.resolve",
                description="Resolve speaker identity from acoustic hint or label.",
                arguments={
                    "speaker_hint": {"type": "string", "required": True, "description": "Speaker label or hint"},
                },
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="speaker_manager",
                provider_status={"provider": "speaker_manager", "available": True},
                side_effect_class="none",
            ),
            "speakers.resolve": RegisteredCapability(
                name="speakers.resolve",
                description="Resolve speaker identity from acoustic hint or label.",
                arguments={
                    "speaker_hint": {"type": "string", "required": True, "description": "Speaker label or hint"},
                },
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="speaker_manager",
                provider_status={"provider": "speaker_manager", "available": True},
                side_effect_class="none",
            ),
            "speaker.link_person": RegisteredCapability(
                name="speaker.link_person",
                description="Link an acoustic speaker voice profile to a person record.",
                arguments={
                    "speaker_id": {"type": "string", "required": True, "description": "Speaker profile ID"},
                    "person_id": {"type": "string", "required": True, "description": "Person record ID"},
                },
                risk="low",
                classification="write",
                target_type="hub",
                permission_tier=2,
                availability=True,
                provider="speaker_manager",
                provider_status={"provider": "speaker_manager", "available": True},
                side_effect_class="none",
            ),
            "speakers.link_person": RegisteredCapability(
                name="speakers.link_person",
                description="Link an acoustic speaker voice profile to a person record.",
                arguments={
                    "speaker_id": {"type": "string", "required": True, "description": "Speaker profile ID"},
                    "person_id": {"type": "string", "required": True, "description": "Person record ID"},
                },
                risk="low",
                classification="write",
                target_type="hub",
                permission_tier=2,
                availability=True,
                provider="speaker_manager",
                provider_status={"provider": "speaker_manager", "available": True},
                side_effect_class="none",
            ),
            "speaker.list_recent": RegisteredCapability(
                name="speaker.list_recent",
                description="List recent identified speakers on this device.",
                arguments={},
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="speaker_manager",
                provider_status={"provider": "speaker_manager", "available": True},
                side_effect_class="none",
            ),
            "speakers.list_recent": RegisteredCapability(
                name="speakers.list_recent",
                description="List recent identified speakers on this device.",
                arguments={},
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="speaker_manager",
                provider_status={"provider": "speaker_manager", "available": True},
                side_effect_class="none",
            ),
            "social.context": RegisteredCapability(
                name="social.context",
                description="Get current social context, participants, and conversational tone guidance.",
                arguments={},
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="social_manager",
                provider_status={"provider": "social_manager", "available": True},
                side_effect_class="none",
            ),
            "social.get_context": RegisteredCapability(
                name="social.get_context",
                description="Get current social context, participants, and conversational tone guidance.",
                arguments={},
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="social_manager",
                provider_status={"provider": "social_manager", "available": True},
                side_effect_class="none",
            ),
            "social.remember_interaction": RegisteredCapability(
                name="social.remember_interaction",
                description="Store a durable social interaction fact about a person.",
                arguments={
                    "person_id": {"type": "string", "required": True, "description": "Person ID"},
                    "key": {"type": "string", "required": False, "description": "Fact key"},
                    "value": {"type": "string", "required": False, "description": "Fact value"},
                },
                risk="low",
                classification="write",
                target_type="hub",
                permission_tier=2,
                availability=True,
                provider="social_manager",
                provider_status={"provider": "social_manager", "available": True},
                side_effect_class="none",
            ),
            # World State & Context
            "context.get_world_state": RegisteredCapability(
                name="context.get_world_state",
                description="Retrieve the current fused world state and situational signals.",
                arguments={},
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="world_state_manager",
                provider_status={"provider": "world_state_manager", "available": True},
                side_effect_class="none",
            ),
            "context.update_signal": RegisteredCapability(
                name="context.update_signal",
                description="Ingest or update a contextual world state signal.",
                arguments={
                    "key": {"type": "string", "required": True, "description": "Signal key"},
                    "value": {"type": "any", "required": True, "description": "Signal payload value"},
                    "ttl": {"type": "number", "required": False, "description": "TTL in seconds"},
                },
                risk="low",
                classification="write",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="world_state_manager",
                provider_status={"provider": "world_state_manager", "available": True},
                side_effect_class="none",
            ),
            "context.get_device_state": RegisteredCapability(
                name="context.get_device_state",
                description="Retrieve situational state for a specific device.",
                arguments={
                    "device_id": {"type": "string", "required": False, "description": "Device ID to inspect"},
                },
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="world_state_manager",
                provider_status={"provider": "world_state_manager", "available": True},
                side_effect_class="none",
            ),
            "context.get_recent_events": RegisteredCapability(
                name="context.get_recent_events",
                description="Retrieve recent world state events from the event buffer.",
                arguments={
                    "limit": {"type": "integer", "required": False, "description": "Maximum events to return"},
                },
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="world_state_manager",
                provider_status={"provider": "world_state_manager", "available": True},
                side_effect_class="none",
            ),
            # Attention Router & Proactive Engine
            "attention.route_event": RegisteredCapability(
                name="attention.route_event",
                description="Route a candidate event through the attention router for desktop notification or delivery.",
                arguments={
                    "event_id": {"type": "string", "required": True, "description": "Candidate proactive event ID"},
                },
                risk="low",
                classification="write",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="attention_router",
                provider_status={"provider": "attention_router", "available": True},
                side_effect_class="none",
            ),
            "attention.get_decision": RegisteredCapability(
                name="attention.get_decision",
                description="Get delivery decision by decision ID.",
                arguments={
                    "decision_id": {"type": "string", "required": True, "description": "Delivery decision ID"},
                },
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="attention_router",
                provider_status={"provider": "attention_router", "available": True},
                side_effect_class="none",
            ),
            "attention.list_pending": RegisteredCapability(
                name="attention.list_pending",
                description="List pending delivery decisions awaiting delivery.",
                arguments={},
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="attention_router",
                provider_status={"provider": "attention_router", "available": True},
                side_effect_class="none",
            ),
            "attention.cancel": RegisteredCapability(
                name="attention.cancel",
                description="Cancel a delivery decision by decision ID.",
                arguments={
                    "decision_id": {"type": "string", "required": True, "description": "Delivery decision ID"},
                },
                risk="low",
                classification="write",
                target_type="hub",
                permission_tier=2,
                availability=True,
                provider="attention_router",
                provider_status={"provider": "attention_router", "available": True},
                side_effect_class="none",
            ),
            "attention.reevaluate": RegisteredCapability(
                name="attention.reevaluate",
                description="Reevaluate pending delivery decisions against current world state.",
                arguments={},
                risk="low",
                classification="write",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="attention_router",
                provider_status={"provider": "attention_router", "available": True},
                side_effect_class="none",
            ),
            "proactive.register_condition": RegisteredCapability(
                name="proactive.register_condition",
                description="Register a proactive signal condition for proactive monitoring.",
                arguments={
                    "condition_type": {"type": "string", "required": False, "description": "Type of condition"},
                    "signal_key": {"type": "string", "required": False, "description": "Signal key to monitor"},
                    "operator": {"type": "string", "required": False, "description": "Comparison operator"},
                    "target_value": {"type": "any", "required": False, "description": "Target comparison value"},
                },
                risk="low",
                classification="write",
                target_type="hub",
                permission_tier=2,
                availability=True,
                provider="proactive_engine",
                provider_status={"provider": "proactive_engine", "available": True},
                side_effect_class="none",
            ),
            "proactive.evaluate": RegisteredCapability(
                name="proactive.evaluate",
                description="Evaluate all registered proactive conditions against live world state.",
                arguments={},
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="proactive_engine",
                provider_status={"provider": "proactive_engine", "available": True},
                side_effect_class="none",
            ),
            "proactive.list_candidates": RegisteredCapability(
                name="proactive.list_candidates",
                description="List generated proactive candidate events.",
                arguments={},
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="proactive_engine",
                provider_status={"provider": "proactive_engine", "available": True},
                side_effect_class="none",
            ),
            "proactive.get_event": RegisteredCapability(
                name="proactive.get_event",
                description="Get proactive candidate event details by ID.",
                arguments={
                    "event_id": {"type": "string", "required": True, "description": "Proactive event ID"},
                },
                risk="low",
                classification="read",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="proactive_engine",
                provider_status={"provider": "proactive_engine", "available": True},
                side_effect_class="none",
            ),
            "proactive.dismiss": RegisteredCapability(
                name="proactive.dismiss",
                description="Dismiss a proactive candidate event.",
                arguments={
                    "event_id": {"type": "string", "required": True, "description": "Proactive event ID"},
                },
                risk="low",
                classification="write",
                target_type="hub",
                permission_tier=1,
                availability=True,
                provider="proactive_engine",
                provider_status={"provider": "proactive_engine", "available": True},
                side_effect_class="none",
            ),
        }

        caps['agent.orchestrate'] = RegisteredCapability(
            name='agent.orchestrate',
            description='Delegate parallel worker agents. Run the entire complex request as an A14 dependency graph with independent workers, capability execution and verified synthesis. Select one CALL_CAPABILITY with arguments {} to delegate; the original request is obtained from its owned AgentTurn. Do not execute one substep first or claim workers ran without this capability. This capability does not need a separate PLAN, success_condition or expected_effect; its data.verified is the graph verification receipt. Simple requests use direct capabilities or a direct response. Delegation grants no permissions.',
            arguments={}, target_type='hub', permission_tier=1, risk='low', classification='read',
            provider='orchestrator', result_source='verified_subtasks', side_effect_class='none')
        caps["hub.transfer_file"] = RegisteredCapability(
            name="hub.transfer_file", description="Copy a real file between enrolled workers over paired TLS. Destination is relative to the receiver's configured folder; never overwrites or deletes the source.",
            arguments={key: {"type": "string", "required": True} for key in
                       ("source_device", "destination_device", "source_path", "destination_path")},
            target_type="hub", permission_tier=2, risk="high", classification="write", side_effect_class="filesystem")
        # Existing agent session handlers, now described by the same canonical
        # catalog as native handlers. This does not add execution authority.
        from .permissions import COMMAND_TIERS
        for name in ('transfer.inspect','transfer.prepare','transfer.send','transfer.cancel',
                     'voice.mute','voice.unmute','voice.push_to_talk'):
            caps[name] = RegisteredCapability(name=name, description='Existing authenticated agent session control: '+name,
                arguments={},target_type='device',permission_tier=COMMAND_TIERS.get(name,3),
                risk='low' if name=='transfer.inspect' or name.startswith('voice.') else 'medium',
                classification='read' if name=='transfer.inspect' else 'device_action',provider_status={'discovery_only':True})
        self._capabilities = caps


def get_core_capabilities() -> Dict[str, RegisteredCapability]:
    """Retrieve all core capabilities from the canonical capability registry."""
    return CanonicalCapabilityRegistry.get_instance().get_all()


def get_capability(name: str) -> Optional[RegisteredCapability]:
    """Retrieve a single registered capability by name."""
    return CanonicalCapabilityRegistry.get_instance().get_capability(name)
