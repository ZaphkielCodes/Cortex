"""
hub/pipeline/normalizer.py
--------------------------
Normalizes raw user requests and extracts structural cues (Section 1).
"""

import re
from dataclasses import dataclass, field
from typing import List


@dataclass
class NormalizedInput:
    """Normalized request with structural and conversational cues."""
    raw_text: str
    cleaned_text: str
    stripped_text: str
    is_question: bool
    is_fast_response: bool
    tokens: List[str] = field(default_factory=list)


class InputNormalizer:
    """
    Normalizes incoming requests without destroying semantic meaning or entities.
    """

    CONVERSATIONAL_PREFIXES = [
        "i said ",
        "please ",
        "can you ",
        "could you ",
        "would you ",
        "cortex ",
        "hey cortex ",
        "ok cortex ",
        "tell me ",
    ]

    QUESTION_STARTERS = {
        "which", "what", "who", "when", "where", "why", "how",
        "explain", "define", "translate", "is", "are", "do",
        "does", "can", "could", "am", "so"
    }

    ACTION_VERBS = {
        "open", "launch", "start", "run", "move", "copy", "delete",
        "remove", "create", "send", "turn", "set", "play", "pause",
        "stop", "close", "kill", "find", "take", "show", "read",
        "list", "get", "switch", "focus", "bring", "minimize",
        "maximize", "restore", "clean", "organize"
    }

    @classmethod
    def normalize(cls, raw_request: str) -> NormalizedInput:
        raw = raw_request.strip()
        lowered = raw.lower()

        # Cleaned text: normalized spaces
        cleaned = re.sub(r"\s+", " ", lowered).strip()

        # Stripped text: remove conversational leading wrappers
        stripped = cleaned
        changed = True
        while changed:
            changed = False
            for pfx in cls.CONVERSATIONAL_PREFIXES:
                if stripped.startswith(pfx):
                    stripped = stripped[len(pfx):].strip()
                    changed = True
                    break

        tokens = stripped.split()
        first_word = tokens[0] if tokens else ""

        is_question = (
            raw.endswith("?")
            or first_word in cls.QUESTION_STARTERS
            or stripped.startswith("so ")
        )

        is_fast_response = False
        if is_question:
            is_fast_response = True
        elif len(tokens) <= 2 and first_word not in cls.ACTION_VERBS:
            # Ambiguous bare nouns like "Spotify", "Chrome"
            is_fast_response = True

        return NormalizedInput(
            raw_text=raw,
            cleaned_text=cleaned,
            stripped_text=stripped,
            is_question=is_question,
            is_fast_response=is_fast_response,
            tokens=tokens,
        )
