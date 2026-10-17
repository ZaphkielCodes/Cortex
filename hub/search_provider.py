"""
hub/search_provider.py
----------------------
Search Provider interface and implementations for Cortex (Phase A1).

Supports:
- DuckDuckGo HTML search (default, free, no API key required)
- Tavily search (if configured via CORTEX_SEARCH_API_KEY / TAVILY_API_KEY)
- Mock search provider (for testing and isolated evaluation)
- Provider status & graceful failure handling (never hallucinates when unavailable)
"""

import os
import re
import urllib.parse
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from html import unescape
from typing import Any, Dict, List, Optional
import logging

log = logging.getLogger("cortex.search_provider")


class SearchProvider(ABC):
    """Abstract base class for Cortex search providers."""

    @abstractmethod
    async def search(
        self,
        query: str,
        max_results: int = 5,
        recency_days: Optional[int] = None,
        domains: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Execute a search query.

        Returns:
            {
                "query": str,
                "results": [
                    {
                        "title": str,
                        "url": str,
                        "snippet": str,
                        "source": str,
                        "published_at": Optional[str],
                    }
                ],
                "provider": str,
                "retrieved_at": str,
            }
        """
        pass

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Name of the search provider."""
        pass

    @property
    def is_available(self) -> bool:
        """Whether the provider is currently configured and operational."""
        return True


class DuckDuckGoSearchProvider(SearchProvider):
    """
    Real search provider using DuckDuckGo HTML endpoint.
    Requires no API keys or third-party search SDKs.
    """

    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout

    @property
    def provider_name(self) -> str:
        return "duckduckgo"

    async def search(
        self,
        query: str,
        max_results: int = 5,
        recency_days: Optional[int] = None,
        domains: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        import httpx

        effective_query = query.strip()
        if domains:
            domain_filters = " ".join(f"site:{d.strip()}" for d in domains if d.strip())
            effective_query = f"{effective_query} {domain_filters}".strip()

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }

        url = "https://html.duckduckgo.com/html/"
        data = {"q": effective_query}

        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                resp = await client.post(url, data=data, headers=headers)
                resp.raise_for_status()
                html_text = resp.text
        except Exception as exc:
            log.error(f"[DuckDuckGoSearchProvider] Request failed: {exc}")
            raise RuntimeError(f"DuckDuckGo search error: {exc}") from exc

        # Extract items using regex pattern matching DDG's server-rendered HTML
        pattern = re.compile(
            r'<a[^>]+class=[\'"]result__a[\'"][^>]+href=[\'"]([^\'"]+)[\'"][^>]*>(.*?)</a>.*?'
            r'<a[^>]+class=[\'"]result__snippet[\'"][^>]*>(.*?)</a>',
            re.DOTALL,
        )

        results: List[Dict[str, Any]] = []
        for match in pattern.finditer(html_text):
            if len(results) >= max_results:
                break
            raw_url, title_raw, snippet_raw = match.groups()
            
            # DDG redirects via /l/?kh=-1&uddg=<target_url>
            parsed = urllib.parse.urlparse(raw_url)
            qs = urllib.parse.parse_qs(parsed.query)
            target_url = qs.get("uddg", [raw_url])[0] if "uddg" in qs else raw_url

            title = unescape(re.sub(r"<[^>]+>", "", title_raw).strip())
            snippet = unescape(re.sub(r"<[^>]+>", "", snippet_raw).strip())
            source = urllib.parse.urlparse(target_url).netloc or "duckduckgo.com"

            results.append({
                "title": title,
                "url": target_url,
                "snippet": snippet,
                "source": source,
                "published_at": None,
            })

        return {
            "query": query,
            "results": results,
            "provider": self.provider_name,
            "retrieved_at": now_iso,
        }


class TavilySearchProvider(SearchProvider):
    """
    Optional search provider using Tavily Search API.
    Used when CORTEX_SEARCH_PROVIDER=tavily and an API key is provided.
    """

    def __init__(self, api_key: str, timeout: float = 10.0):
        self.api_key = api_key
        self.timeout = timeout

    @property
    def provider_name(self) -> str:
        return "tavily"

    async def search(
        self,
        query: str,
        max_results: int = 5,
        recency_days: Optional[int] = None,
        domains: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        import httpx

        payload: Dict[str, Any] = {
            "api_key": self.api_key,
            "query": query,
            "max_results": max_results,
        }
        if domains:
            payload["include_domains"] = domains

        url = "https://api.tavily.com/search"
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            log.error(f"[TavilySearchProvider] Request failed: {exc}")
            raise RuntimeError(f"Tavily search error: {exc}") from exc

        raw_results = data.get("results", [])
        results = []
        for r in raw_results[:max_results]:
            u = r.get("url", "")
            results.append({
                "title": r.get("title", ""),
                "url": u,
                "snippet": r.get("content", ""),
                "source": urllib.parse.urlparse(u).netloc,
                "published_at": r.get("published_date"),
            })

        return {
            "query": query,
            "results": results,
            "provider": self.provider_name,
            "retrieved_at": now_iso,
        }


class MockSearchProvider(SearchProvider):
    """Mock search provider for deterministic testing."""

    def __init__(self, predefined_results: Optional[List[Dict[str, Any]]] = None):
        self._results = predefined_results or [
            {
                "title": "NVIDIA Announces Next-Gen RTX 5090",
                "url": "https://nvidianews.nvidia.com/news/rtx-5090-announcement",
                "snippet": "NVIDIA today unveiled the GeForce RTX 5090 desktop and laptop GPUs featuring Blackwell architecture.",
                "source": "nvidianews.nvidia.com",
                "published_at": "2026-10-10T12:00:00Z",
            },
            {
                "title": "RTX 5090 Laptops: Specs, Price, and Release Date",
                "url": "https://techradar.com/news/rtx-5090-laptops",
                "snippet": "Everything you need to know about RTX 5090 mobile GPUs for laptops arriving this season.",
                "source": "techradar.com",
                "published_at": "2026-10-11T14:30:00Z",
            },
        ]

    @property
    def provider_name(self) -> str:
        return "mock"

    async def search(
        self,
        query: str,
        max_results: int = 5,
        recency_days: Optional[int] = None,
        domains: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        filtered = self._results
        if domains:
            filtered = [r for r in filtered if any(d.lower() in r.get("source", "").lower() for d in domains)]

        return {
            "query": query,
            "results": filtered[:max_results],
            "provider": self.provider_name,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
        }


_OVERRIDE_PROVIDER: Optional[SearchProvider] = None


def set_search_provider_override(provider: Optional[SearchProvider]) -> None:
    """Explicitly set or reset the search provider override (for testing)."""
    global _OVERRIDE_PROVIDER
    _OVERRIDE_PROVIDER = provider


def get_search_provider() -> Optional[SearchProvider]:
    """
    Resolve the active search provider according to environment configuration.
    Returns None if disabled or no provider is configured.
    """
    global _OVERRIDE_PROVIDER
    if _OVERRIDE_PROVIDER is not None:
        return _OVERRIDE_PROVIDER

    enabled_env = os.environ.get("CORTEX_SEARCH_ENABLED", "true").lower()
    if enabled_env in ("false", "0", "no"):
        return None

    provider_type = os.environ.get("CORTEX_SEARCH_PROVIDER", "duckduckgo").lower().strip()

    if provider_type == "mock":
        return MockSearchProvider()
    elif provider_type == "tavily":
        key = os.environ.get("CORTEX_SEARCH_API_KEY") or os.environ.get("TAVILY_API_KEY")
        if not key:
            log.warning("CORTEX_SEARCH_PROVIDER is tavily but no API key configured.")
            return None
        return TavilySearchProvider(api_key=key)
    elif provider_type == "duckduckgo":
        return DuckDuckGoSearchProvider()
    elif provider_type in ("none", "disabled"):
        return None
    else:
        log.warning(f"Unknown CORTEX_SEARCH_PROVIDER: '{provider_type}'")
        return None
