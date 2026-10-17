"""
hub/browser_navigation.py
-------------------------
Browser URL and destination resolution for Cortex (Phase A1).

Supports:
- Normalizing URLs (http/https scheme addition, domain sanitation)
- Unambiguous semantic public destinations (e.g. "YouTube", "GitHub", "Google", "Reddit")
- Fallback search-assisted resolution for arbitrary website names
"""

import re
import urllib.parse
from typing import Optional
import logging

log = logging.getLogger("cortex.browser_navigation")

# Small standard public web destinations resolver (bounded, high-confidence)
COMMON_WEB_DESTINATIONS = {
    "youtube": "https://www.youtube.com",
    "github": "https://github.com",
    "google": "https://www.google.com",
    "reddit": "https://www.reddit.com",
    "wikipedia": "https://www.wikipedia.org",
    "twitter": "https://x.com",
    "x": "https://x.com",
    "gmail": "https://mail.google.com",
    "netflix": "https://www.netflix.com",
    "amazon": "https://www.amazon.com",
    "linkedin": "https://www.linkedin.com",
    "spotify": "https://open.spotify.com",
    "chatgpt": "https://chatgpt.com",
    "openai": "https://openai.com",
    "huggingface": "https://huggingface.co",
}


def is_valid_url(candidate: str) -> bool:
    """Check if a string is already a formatted URL or domain name."""
    cand = candidate.strip().lower()
    if cand.startswith("http://") or cand.startswith("https://"):
        return True
    if cand.startswith("www."):
        return True
    # Domain pattern like foo.com, bar.org, github.io
    if re.match(r"^[a-zA-Z0-9-]+\.[a-zA-Z]{2,}(?:/[^\s]*)?$", cand):
        return True
    return False


def normalize_url(raw_url: str) -> str:
    """Ensure URL has a valid scheme."""
    cand = raw_url.strip()
    if not cand.startswith("http://") and not cand.startswith("https://"):
        return f"https://{cand}"
    return cand


async def resolve_web_destination(destination: str) -> str:
    """
    Resolve a user's web destination to a valid URL.
    - If already a URL / domain: normalizes scheme.
    - If known common entity: returns official URL.
    - If arbitrary name: resolves via search provider.
    """
    dest_clean = destination.strip()
    dest_lower = dest_clean.lower()

    # 1. Strip conversational prefixes if present ("open ", "go to ", "visit ")
    for prefix in ("open ", "go to ", "visit ", "navigate to "):
        if dest_lower.startswith(prefix):
            dest_lower = dest_lower[len(prefix):].strip()
            dest_clean = dest_clean[len(prefix):].strip()

    # 2. Check standard common destinations
    if dest_lower in COMMON_WEB_DESTINATIONS:
        return COMMON_WEB_DESTINATIONS[dest_lower]

    # 3. Check if already formatted as URL / domain
    if is_valid_url(dest_clean):
        return normalize_url(dest_clean)

    # 4. Fallback search resolution for unknown entity
    try:
        from .search_provider import get_search_provider
        provider = get_search_provider()
        if provider:
            res = await provider.search(f"{dest_clean} official website", max_results=1)
            results = res.get("results", [])
            if results and results[0].get("url"):
                return results[0]["url"]
    except Exception as exc:
        log.warning(f"[resolve_web_destination] Search fallback failed for '{dest_clean}': {exc}")

    # Fallback to constructing domain
    sanitized = re.sub(r"[^a-zA-Z0-9-]", "", dest_lower)
    return f"https://www.{sanitized}.com"
