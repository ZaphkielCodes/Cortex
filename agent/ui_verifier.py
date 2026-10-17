"""
agent/ui_verifier.py
--------------------
Post-Action Verification Engine for Cortex General Computer Use (Phase A10).

Compares pre-action observation vs post-action observation to verify:
- Window title or path changes
- Control state changes (input values, selection, toggles)
- Dialog appearance or disappearance
- Layout state changes (state_hash divergence)
- Replan necessity if expected outcome failed (prevents blind click spam)
"""

from dataclasses import dataclass, field
import logging
from typing import Any, Dict, List, Optional

from .ui_models import UIObservation

log = logging.getLogger("cortex.ui_verifier")


@dataclass
class VerificationOutcome:
    """Outcome of comparing post-action UI state with pre-action expectations."""
    verified: bool
    change_detected: bool
    replan_suggested: bool
    summary: str
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verified": self.verified,
            "change_detected": self.change_detected,
            "replan_suggested": self.replan_suggested,
            "summary": self.summary,
            "details": self.details,
        }


class UIActionVerifier:
    """
    Deterministic post-action verifier evaluating whether a bounded UI interaction
    materially achieved its intended progress before the planner continues.
    """

    @classmethod
    def verify(
        cls,
        action: str,
        target_label: Optional[str],
        pre_obs: Optional[UIObservation],
        post_obs: Optional[UIObservation],
        action_args: Optional[Dict[str, Any]] = None,
        expected_effect: Optional[str] = None,
    ) -> VerificationOutcome:
        """
        Compare pre-action and post-action observations to verify progress.
        """
        args = action_args or {}

        if not pre_obs or not post_obs:
            return VerificationOutcome(
                verified=False,
                change_detected=False,
                replan_suggested=True,
                summary="Cannot verify: observation snapshot missing.",
            )

        title_changed = (pre_obs.window_title != post_obs.window_title)
        app_changed = (pre_obs.foreground_app != post_obs.foreground_app)
        hash_changed = (pre_obs.state_hash != post_obs.state_hash)
        elements_count_changed = (len(pre_obs.elements) != len(post_obs.elements))

        details = {
            "pre_title": pre_obs.window_title,
            "post_title": post_obs.window_title,
            "title_changed": title_changed,
            "app_changed": app_changed,
            "hash_changed": hash_changed,
            "pre_element_count": len(pre_obs.elements),
            "post_element_count": len(post_obs.elements),
        }

        # Verification logic by action type
        if action in ("ui.click", "ui.double_click"):
            # Check if expected effect was specifically matched
            if expected_effect:
                eff_lower = expected_effect.lower()
                matched_effect = (
                    eff_lower in (post_obs.window_title or "").lower()
                    or any(eff_lower in e.label.lower() or eff_lower in (e.text or "").lower() for e in post_obs.elements)
                )
                if matched_effect:
                    return VerificationOutcome(
                        verified=True,
                        change_detected=True,
                        replan_suggested=False,
                        summary=f"Click verified: expected effect '{expected_effect}' achieved.",
                        details=details,
                    )

            # If target was navigation (e.g. "Downloads", "Settings", a tab or link)
            if title_changed or app_changed:
                return VerificationOutcome(
                    verified=True,
                    change_detected=True,
                    replan_suggested=False,
                    summary=f"Click verified: window title updated to '{post_obs.window_title}'.",
                    details=details,
                )

            # Check if modal dialog or elements changed
            if hash_changed or elements_count_changed:
                return VerificationOutcome(
                    verified=True,
                    change_detected=True,
                    replan_suggested=False,
                    summary=f"Click verified: UI layout updated ({len(post_obs.elements)} elements present).",
                    details=details,
                )

            # If expected target is still present and nothing changed
            return VerificationOutcome(
                verified=False,
                change_detected=False,
                replan_suggested=True,
                summary=f"Verification failed: click on '{target_label}' produced no visible UI change. Replan suggested.",
                details=details,
            )

        elif action == "ui.type":
            from .ui_text import verify_text_entry
            verified = verify_text_entry(pre_obs, post_obs, args)
            return VerificationOutcome(verified=verified, change_detected=verified,
                replan_suggested=not verified,
                summary="Text verified in the selected field." if verified else
                        "Text entry remains unverified in the selected field; re-observe before retrying.",
                details=details)

        elif action == "ui.scroll":
            # Scrolling typically changes layout or visible elements
            if hash_changed or elements_count_changed:
                return VerificationOutcome(
                    verified=True,
                    change_detected=True,
                    replan_suggested=False,
                    summary="Scroll verified: visible viewport shifted.",
                    details=details,
                )
            return VerificationOutcome(
                verified=True,
                change_detected=False,
                replan_suggested=False,
                summary="Scroll completed.",
                details=details,
            )

        elif action in ("ui.hotkey", "ui.keypress"):
            key_name = args.get("key") or str(args.get("keys"))
            if hash_changed or title_changed:
                return VerificationOutcome(
                    verified=True,
                    change_detected=True,
                    replan_suggested=False,
                    summary=f"Key action '{key_name}' verified: UI responded.",
                    details=details,
                )
            return VerificationOutcome(
                verified=True,
                change_detected=False,
                replan_suggested=False,
                summary=f"Key action '{key_name}' executed.",
                details=details,
            )

        # Default fallback
        return VerificationOutcome(
            verified=True,
            change_detected=hash_changed,
            replan_suggested=False,
            summary="Action executed and verified.",
            details=details,
        )

    @classmethod
    def get_hardware_display_frequency(cls) -> Optional[int]:
        """Query current hardware display refresh rate directly via Win32 API."""
        try:
            import ctypes
            from ctypes import wintypes
            class DEVMODEW(ctypes.Structure):
                _fields_ = [
                    ("dmDeviceName", wintypes.WCHAR * 32),
                    ("dmSpecVersion", wintypes.WORD),
                    ("dmDriverVersion", wintypes.WORD),
                    ("dmSize", wintypes.WORD),
                    ("dmDriverExtra", wintypes.WORD),
                    ("dmFields", wintypes.DWORD),
                    ("dmOrientation", wintypes.SHORT),
                    ("dmPaperSize", wintypes.SHORT),
                    ("dmPaperLength", wintypes.SHORT),
                    ("dmPaperWidth", wintypes.SHORT),
                    ("dmScale", wintypes.SHORT),
                    ("dmCopies", wintypes.SHORT),
                    ("dmDefaultSource", wintypes.SHORT),
                    ("dmPrintQuality", wintypes.SHORT),
                    ("dmColor", wintypes.SHORT),
                    ("dmDuplex", wintypes.SHORT),
                    ("dmYResolution", wintypes.SHORT),
                    ("dmTTOption", wintypes.SHORT),
                    ("dmCollate", wintypes.SHORT),
                    ("dmFormName", wintypes.WCHAR * 32),
                    ("dmLogPixels", wintypes.WORD),
                    ("dmBitsPerPel", wintypes.DWORD),
                    ("dmPelsWidth", wintypes.DWORD),
                    ("dmPelsHeight", wintypes.DWORD),
                    ("dmDisplayFlags", wintypes.DWORD),
                    ("dmDisplayFrequency", wintypes.DWORD),
                ]
            dm = DEVMODEW()
            dm.dmSize = ctypes.sizeof(DEVMODEW)
            res = ctypes.windll.user32.EnumDisplaySettingsW(None, -1, ctypes.byref(dm))
            if res:
                return int(dm.dmDisplayFrequency)
        except Exception as exc:
            log.debug(f"[UIActionVerifier] Win32 display query failed: {exc}")
        return None

    @classmethod
    def verify_goal_completion(cls, goal, success_condition, obs, target_device=None):
        """Natural-language completion belongs to AgentTurn's evidence-driven loop.

        Kept for compatibility callers: names, process presence and prior input
        attempts cannot prove a goal succeeded.
        """
        return VerificationOutcome(False, False, True,
            "Goal completion requires interpretation of fresh evidence by the agent runtime.")


get_hardware_display_frequency = UIActionVerifier.get_hardware_display_frequency

