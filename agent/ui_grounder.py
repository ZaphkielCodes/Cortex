"""
agent/ui_grounder.py
--------------------
Stable Element Grounding Layer for Cortex General Computer Use (Phase A10).

Resolves semantic target references (element IDs, semantic role/labels, relative
spatial relationships, ordinal indicators) into deterministic screen coordinates
grounded in the active UIObservation.
"""

from dataclasses import dataclass
import logging
import re
from typing import Any, Dict, List, Optional, Tuple, Union

from .ui_models import UIElement, UIObservation

log = logging.getLogger("cortex.ui_grounder")


@dataclass
class GroundingResult:
    """Outcome of resolving a semantic target against a UIObservation."""
    success: bool
    element: Optional[UIElement] = None
    target_point: Optional[Tuple[int, int]] = None  # (x, y) coordinates
    strategy: str = "none"  # "id_lookup" | "exact_label" | "role_label" | "ordinal" | "spatial_relative" | "substring" | "coordinate_fallback"
    confidence: float = 0.0
    explanation: str = ""
    target_query: Any = None


class UIElementGrounder:
    """
    Grounds high-level target references to concrete coordinates
    at execution time against an active UIObservation.
    """

    ORDINAL_MAP = {
        "first": 0, "1st": 0,
        "second": 1, "2nd": 1,
        "third": 2, "3rd": 2,
        "fourth": 3, "4th": 3,
        "fifth": 4, "5th": 4,
        "last": -1,
    }

    @classmethod
    def ground(
        cls,
        target: Any,
        observation: UIObservation,
        coords: Optional[Tuple[int, int]] = None,
    ) -> GroundingResult:
        """
        Ground target reference into concrete screen coordinates.
        Precedence:
        1. Direct element_id lookup
        2. Structured selector / role + label
        3. Ordinal reference ('second item', 'first button')
        4. Spatial relative reference ('button under search box')
        5. Exact label match
        6. Substring / fuzzy label match
        7. Bounded coordinate fallback
        """
        if not observation:
            return GroundingResult(
                success=False,
                explanation="No active UI observation available for grounding.",
                target_query=target,
            )

        # Case 0: Target is already a UIElement
        if isinstance(target, UIElement):
            cp = target.center_point
            return GroundingResult(
                success=True,
                element=target,
                target_point=cp,
                strategy="direct_element",
                confidence=target.confidence,
                explanation=f"Direct UIElement reference '{target.label}' ({target.element_id}) grounded at {cp}.",
                target_query=target.element_id,
            )

        # Case 1: element_id lookup (e.g. "el_17")
        if isinstance(target, str) and target.startswith("el_"):
            el = observation.find_element_by_id(target.strip())
            if el:
                cp = el.center_point
                return GroundingResult(
                    success=True,
                    element=el,
                    target_point=cp,
                    strategy="id_lookup",
                    confidence=el.confidence,
                    explanation=f"Element ID '{target}' resolved to '{el.label}' ({el.role}) at {cp}.",
                    target_query=target,
                )

        # Case 2: Structured dict selector (e.g. {"role": "button", "label": "Save"})
        if isinstance(target, dict):
            res = cls._ground_dict_selector(target, observation)
            if res.success:
                return res

        # Case 3: CSS/Selector-like string e.g. "button[label='Save']" or "button[Save]"
        if isinstance(target, str) and "[" in target and "]" in target:
            res = cls._ground_selector_string(target, observation)
            if res.success:
                return res

        target_str = str(target or "").strip()

        # Case 4: Ordinal reference (e.g. "the second item", "first button", "2nd result")
        if target_str:
            ord_res = cls._ground_ordinal(target_str, observation)
            if ord_res.success:
                return ord_res

        # Case 5: Spatial relative query (e.g. "button under the search box", "below search")
        if target_str and any(w in target_str.lower() for w in ("under", "below", "next to", "beside")):
            rel_res = cls._ground_spatial_relative(target_str, observation)
            if rel_res.success:
                return rel_res

        # Case 6: Exact label or text match
        if target_str:
            clean_target = target_str.strip("'\"")
            exact_matches = [
                e for e in observation.elements
                if e.label.lower() == clean_target.lower()
                or (e.text and e.text.lower() == clean_target.lower())
            ]
            if exact_matches:
                # Prefer enabled and actionable controls
                exact_matches.sort(key=lambda e: (e.enabled, e.role in ("button", "link", "input", "menuitem", "tabitem")), reverse=True)
                chosen = exact_matches[0]
                cp = chosen.center_point
                return GroundingResult(
                    success=True,
                    element=chosen,
                    target_point=cp,
                    strategy="exact_label",
                    confidence=chosen.confidence,
                    explanation=f"Exact label match '{chosen.label}' ({chosen.role}, {chosen.element_id}) grounded at {cp}.",
                    target_query=target,
                )

        # Case 7: Substring label match
        if target_str:
            clean_target = target_str.strip("'\"").lower()
            sub_matches = [
                e for e in observation.elements
                if clean_target in e.label.lower()
                or (e.text and clean_target in e.text.lower())
            ]
            if sub_matches:
                sub_matches.sort(key=lambda e: (e.enabled, e.role in ("button", "link", "input", "menuitem")), reverse=True)
                chosen = sub_matches[0]
                cp = chosen.center_point
                return GroundingResult(
                    success=True,
                    element=chosen,
                    target_point=cp,
                    strategy="substring",
                    confidence=0.9 * chosen.confidence,
                    explanation=f"Substring match '{chosen.label}' for '{target_str}' ({chosen.element_id}) grounded at {cp}.",
                    target_query=target,
                )

        # Case 7b: Semantic content-word matching for natural language goal phrases
        if target_str:
            stopwords = {"a", "an", "the", "my", "your", "to", "for", "on", "in", "and", "me", "show", "change", "turn", "set", "switch", "make", "find", "open", "is", "of", "with", "page", "please"}
            words = [re.sub(r"[^\w]", "", w.lower()) for w in target_str.split()]
            content_words = [w for w in words if w and w not in stopwords and len(w) > 1]
            if content_words:
                def score_element(e):
                    el_text = f"{e.label} {e.text or ''}".lower()
                    matches = sum(1 for w in content_words if w in el_text)
                    if matches == 0:
                        return 0
                    actionable_bonus = 2 if e.role in ("button", "link", "input", "combobox", "listitem", "menuitem") else 0
                    enabled_bonus = 1 if e.enabled else 0
                    return matches * 10 + actionable_bonus + enabled_bonus

                scored = [(score_element(e), e) for e in observation.elements]
                scored = [item for item in scored if item[0] > 0]
                if scored:
                    scored.sort(key=lambda item: item[0], reverse=True)
                    chosen = scored[0][1]
                    cp = chosen.center_point
                    return GroundingResult(
                        success=True,
                        element=chosen,
                        target_point=cp,
                        strategy="semantic_keyword",
                        confidence=0.85 * chosen.confidence,
                        explanation=f"Semantic match '{chosen.label}' ({chosen.role}) for query '{target_str}' grounded at {cp}.",
                        target_query=target,
                    )

        # Case 8: Coordinate fallback if provided
        if coords and len(coords) >= 2:
            cx, cy = int(coords[0]), int(coords[1])
            sw = observation.screen_size.get("width", 1920)
            sh = observation.screen_size.get("height", 1080)
            if 0 <= cx <= sw and 0 <= cy <= sh:
                # Check if coordinates fall inside any element
                enclosing = [
                    e for e in observation.elements
                    if e.x <= cx <= (e.x + e.width) and e.y <= cy <= (e.y + e.height)
                ]
                enc_el = enclosing[0] if enclosing else None
                return GroundingResult(
                    success=True,
                    element=enc_el,
                    target_point=(cx, cy),
                    strategy="coordinate_fallback",
                    confidence=0.8,
                    explanation=f"Coordinate fallback ({cx}, {cy}) accepted within screen bounds."
                    + (f" Enclosed by '{enc_el.label}' ({enc_el.role})." if enc_el else ""),
                    target_query=target,
                )

        return GroundingResult(
            success=False,
            explanation=f"Could not ground target '{target}' against observation {observation.observation_id} ({len(observation.elements)} elements inspected).",
            target_query=target,
        )

    @classmethod
    def _ground_dict_selector(cls, sel: Dict[str, Any], obs: UIObservation) -> GroundingResult:
        """Ground a structured dictionary selector."""
        role = (sel.get("role") or "").lower().strip()
        label = (sel.get("label") or sel.get("name") or sel.get("text") or "").lower().strip()
        idx = int(sel.get("index", 0))

        candidates = obs.elements
        if role:
            candidates = [e for e in candidates if e.role.lower() == role]
        if label:
            candidates = [e for e in candidates if label in e.label.lower() or label in e.text.lower()]

        if candidates:
            idx = max(0, min(idx, len(candidates) - 1))
            chosen = candidates[idx]
            cp = chosen.center_point
            return GroundingResult(
                success=True,
                element=chosen,
                target_point=cp,
                strategy="role_label",
                confidence=chosen.confidence,
                explanation=f"Structured selector {sel} grounded to '{chosen.label}' ({chosen.role}) at {cp}.",
                target_query=sel,
            )
        return GroundingResult(success=False, explanation=f"No elements matched selector {sel}.")

    @classmethod
    def _ground_selector_string(cls, selector: str, obs: UIObservation) -> GroundingResult:
        """Parse and ground strings like button[label='Save'] or button[Save]."""
        m = re.match(r"^(\w+)\s*\[(?:label\s*=\s*['\"]?)?([^'\"\]]+)['\"]?\]$", selector.strip(), re.IGNORECASE)
        if m:
            role_q = m.group(1).lower()
            label_q = m.group(2).lower().strip()
            return cls._ground_dict_selector({"role": role_q, "label": label_q}, obs)
        return GroundingResult(success=False, explanation=f"Could not parse selector '{selector}'.")

    @classmethod
    def _ground_ordinal(cls, text: str, obs: UIObservation) -> GroundingResult:
        """Ground ordinal references like 'second item', 'first button', 'the 2nd result'."""
        text_lower = text.lower().strip()
        found_ord = None
        for ord_word, idx_val in cls.ORDINAL_MAP.items():
            if re.search(rf"\b{re.escape(ord_word)}\b", text_lower):
                found_ord = idx_val
                break

        if found_ord is None:
            return GroundingResult(success=False)

        # Detect target role or category (e.g. "button", "item", "result", "link", "tab")
        role_filter = None
        for r in ("button", "link", "input", "edit", "tab", "listitem", "menuitem", "checkbox"):
            if r in text_lower:
                role_filter = r
                break

        candidates = [e for e in obs.elements if e.enabled]
        if role_filter:
            candidates = [e for e in candidates if e.role.lower() == role_filter]

        if not candidates:
            # Fallback to actionable elements
            candidates = [e for e in obs.elements if e.role in ("button", "link", "listitem", "input")]

        # Sort spatially: top-to-bottom, then left-to-right
        candidates.sort(key=lambda e: (e.y, e.x))

        if not candidates:
            return GroundingResult(success=False, explanation="No candidates found for ordinal query.")

        target_idx = found_ord if found_ord >= 0 else len(candidates) + found_ord
        if 0 <= target_idx < len(candidates):
            chosen = candidates[target_idx]
            cp = chosen.center_point
            return GroundingResult(
                success=True,
                element=chosen,
                target_point=cp,
                strategy="ordinal",
                confidence=chosen.confidence,
                explanation=f"Ordinal query '{text}' resolved to index {target_idx} ('{chosen.label}', {chosen.role}) at {cp}.",
                target_query=text,
            )

        return GroundingResult(success=False, explanation=f"Ordinal index {target_idx} out of range (found {len(candidates)} candidates).")

    @classmethod
    def _ground_spatial_relative(cls, text: str, obs: UIObservation) -> GroundingResult:
        """Ground relative references like 'button under the search box' or 'below Settings'."""
        text_lower = text.lower().strip()
        # Find anchor keyword
        anchor_match = re.search(r"(?:under|below|beneath)\s+(?:the\s+)?([a-zA-Z0-9_\-\s]+)", text_lower)
        if not anchor_match:
            return GroundingResult(success=False)

        anchor_query = anchor_match.group(1).strip()
        # Find anchor element
        anchor_elements = [
            e for e in obs.elements
            if anchor_query in e.label.lower()
            or anchor_query in e.text.lower()
            or (anchor_query == "search" and e.role in ("input", "edit"))
            or (anchor_query in ("search box", "search bar") and e.role in ("input", "edit"))
        ]

        if not anchor_elements:
            return GroundingResult(success=False, explanation=f"Anchor element '{anchor_query}' not found.")

        anchor = anchor_elements[0]
        anchor_bottom = anchor.y + anchor.height

        # Candidates below the anchor
        below_candidates = [
            e for e in obs.elements
            if e.y >= (anchor_bottom - 5) and e.element_id != anchor.element_id and e.enabled
        ]

        # If user specified role (e.g. "button under ...")
        if "button" in text_lower:
            below_candidates = [e for e in below_candidates if e.role == "button"]
        elif "link" in text_lower:
            below_candidates = [e for e in below_candidates if e.role == "link"]

        if below_candidates:
            # Sort by vertical distance from anchor bottom
            below_candidates.sort(key=lambda e: (e.y - anchor_bottom, abs(e.x - anchor.x)))
            chosen = below_candidates[0]
            cp = chosen.center_point
            return GroundingResult(
                success=True,
                element=chosen,
                target_point=cp,
                strategy="spatial_relative",
                confidence=chosen.confidence,
                explanation=f"Spatial relative query '{text}' grounded to '{chosen.label}' ({chosen.role}) below '{anchor.label}' at {cp}.",
                target_query=text,
            )

        return GroundingResult(success=False, explanation=f"No elements found below anchor '{anchor.label}'.")
