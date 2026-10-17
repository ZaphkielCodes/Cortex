"""
Approval Resolver for Cortex
Recognizes bounded affirmative and negative approval responses in conversational context.
"""

import re
from typing import Optional


AFFIRMATIVE_APPROVAL_PHRASES = {
    "yes",
    "yeah",
    "yep",
    "yup",
    "go ahead",
    "do it",
    "proceed",
    "confirm",
    "okay",
    "ok",
    "sure",
    "that's fine",
    "thats fine",
    "do that",
    "please proceed",
    "approve",
    "approved",
    "that's okay",
    "thats okay",
    "sounds good",
    "yes please",
    "sure go ahead",
    "yeah do it",
    "go for it",
    "no problem",
    "all good",
}

NEGATIVE_APPROVAL_PHRASES = {
    "no",
    "nope",
    "don't",
    "dont",
    "cancel",
    "stop",
    "never mind",
    "nevermind",
    "deny",
    "do not",
    "negative",
    "abort",
    "don't do that",
    "dont do that",
    "don't do it",
    "dont do it",
    "please don't",
    "please dont",
    "no thanks",
}


def resolve_approval_intent(text: str) -> Optional[str]:
    """
    Recognize bounded affirmative and negative intent for pending approval requests.
    Returns 'approved', 'denied', or None.

    Affirmative examples:
      yes, yeah, yep, go ahead, do it, proceed, confirm, okay, ok, sure, that's fine

    Negative examples:
      no, nope, don't, cancel, stop, never mind, deny
    """
    if not text:
        return None

    clean = text.lower().strip(" \t.,!?;:'\"")
    clean = re.sub(r"\s+", " ", clean)

    if clean in AFFIRMATIVE_APPROVAL_PHRASES:
        return "approved"

    if clean in NEGATIVE_APPROVAL_PHRASES:
        return "denied"

    words = clean.split()
    if words and len(words) <= 4:
        # Check bounded affirmative starters (e.g., 'yes please', 'ok proceed', 'sure, do it')
        if clean.startswith(("yes ", "yeah ", "yep ", "sure ", "ok ", "okay ", "go ahead ")):
            if not any(w in words for w in ("not", "don't", "dont", "no", "never", "stop", "cancel", "deny")):
                return "approved"

        # Check bounded negative starters (e.g., 'no stop', 'don't do that')
        if clean.startswith(("no ", "nope ", "don't ", "dont ", "stop ", "cancel ", "never ")):
            return "denied"

    return None
