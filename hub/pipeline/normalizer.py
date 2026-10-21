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

    @classmethod
    def normalize(cls, raw_request: str) -> NormalizedInput:
        raw = raw_request.strip()
        cleaned = re.sub(r"\s+", " ", raw.lower()).strip()
        # Preserve negation, names, slang and wording for the model. Only safety
        # controls inspect cleaned_text; no verb/prefix dictionary routes requests.
        return NormalizedInput(raw_text=raw, cleaned_text=cleaned, stripped_text=raw,
            is_question=raw.endswith("?"), is_fast_response=False, tokens=raw.split())
