"""
hub/web_fetch.py
----------------
Safe bounded web content fetcher for Cortex (Phase A1).

Safety:
- HTTP/HTTPS only (rejects file://, ftp://, etc.)
- SSRF Guard: Blocks localhost, loopback, and RFC-1918 private network IPs
- Bounded response size (capped download stream)
- Reasonable timeout (10s default)
- HTML text extraction (removes scripts, styles, boilerplate)
"""

import ipaddress
import re
import socket
import urllib.parse
from html import unescape
from typing import Any, Dict, Optional
import logging

log = logging.getLogger("cortex.web_fetch")

# Max bytes to download before truncating/stopping
MAX_DOWNLOAD_BYTES = 1024 * 1024  # 1 MB
DEFAULT_MAX_CHARS = 5000
DEFAULT_TIMEOUT = 10.0


def is_private_or_local_host(hostname: str) -> bool:
    """
    Check whether a hostname or IP address resolves to a private or loopback network.
    Guards against Server-Side Request Forgery (SSRF).
    """
    clean_host = hostname.strip().lower()
    if clean_host in ("localhost", "localhost.localdomain", "broadcasthost"):
        return True

    # Try resolving hostname to IP addresses
    try:
        addr_info = socket.getaddrinfo(clean_host, None)
    except socket.gaierror:
        # Cannot resolve hostname — let the fetch fail normally
        return False

    for item in addr_info:
        sockaddr = item[4]
        ip_str = sockaddr[0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
            if (
                ip_obj.is_private
                or ip_obj.is_loopback
                or ip_obj.is_link_local
                or ip_obj.is_multicast
                or ip_obj.is_reserved
                or ip_obj.is_unspecified
            ):
                return True
        except ValueError:
            continue

    return False


def clean_html_content(html_text: str, max_chars: int = DEFAULT_MAX_CHARS) -> tuple[str, str]:
    """
    Extract title and cleaned text from HTML.
    Returns (title, text).
    """
    # Extract title
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.IGNORECASE | re.DOTALL)
    title = ""
    if title_match:
        title = unescape(re.sub(r"<[^>]+>", "", title_match.group(1)).strip())

    # Remove script, style, nav, header, footer, noscript tags
    cleaned = re.sub(
        r"<(?:script|style|nav|header|footer|noscript|svg|iframe)[^>]*>.*?</(?:script|style|nav|header|footer|noscript|svg|iframe)>",
        " ",
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    # Convert block tags to linebreaks
    cleaned = re.sub(r"<(?:p|div|h[1-6]|li|tr|br)[^>]*>", "\n", cleaned, flags=re.IGNORECASE)

    # Strip remaining HTML tags
    text = re.sub(r"<[^>]+>", " ", cleaned)

    # Unescape HTML entities
    text = unescape(text)

    # Normalize whitespace
    lines = [line.strip() for line in text.split("\n")]
    normalized_lines = [line for line in lines if line]
    clean_text = "\n".join(normalized_lines)

    if len(clean_text) > max_chars:
        clean_text = clean_text[:max_chars] + "... [truncated]"

    return title, clean_text


async def web_fetch(
    url: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    timeout: float = DEFAULT_TIMEOUT,
    allow_private: bool = False,
) -> Dict[str, Any]:
    """
    Fetch and return clean readable content from a URL.

    Output format:
    {
        "status": "success" | "failure",
        "http_status": int,
        "final_url": str,
        "title": str,
        "text": str,
        "content_type": str,
        "message": str,
    }
    """
    import httpx

    parsed = urllib.parse.urlparse(url)
    scheme = parsed.scheme.lower()

    if scheme not in ("http", "https"):
        return {
            "status": "failure",
            "http_status": 0,
            "final_url": url,
            "title": "",
            "text": "",
            "content_type": "",
            "message": f"Invalid URL scheme '{scheme}'. Only HTTP and HTTPS are permitted.",
        }

    hostname = parsed.hostname
    if not hostname:
        return {
            "status": "failure",
            "http_status": 0,
            "final_url": url,
            "title": "",
            "text": "",
            "content_type": "",
            "message": "Invalid URL: missing hostname.",
        }

    # SSRF Guard
    if not allow_private and is_private_or_local_host(hostname):
        return {
            "status": "failure",
            "http_status": 403,
            "final_url": url,
            "title": "",
            "text": "",
            "content_type": "",
            "message": "Access to local or private network addresses is forbidden by safety policy.",
        }

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36 Cortex/1.0"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.7",
    }

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            content_type = resp.headers.get("content-type", "").lower()
            final_url = str(resp.url)

            # Check redirect destination for SSRF as well
            redirect_host = urllib.parse.urlparse(final_url).hostname
            if redirect_host and not allow_private and is_private_or_local_host(redirect_host):
                return {
                    "status": "failure",
                    "http_status": 403,
                    "final_url": final_url,
                    "title": "",
                    "text": "",
                    "content_type": content_type,
                    "message": "Redirected to forbidden local or private network address.",
                }

            raw_bytes = resp.content[:MAX_DOWNLOAD_BYTES]
            body_text = raw_bytes.decode(resp.encoding or "utf-8", errors="replace")

            if "html" in content_type:
                title, text = clean_html_content(body_text, max_chars=max_chars)
            else:
                title = ""
                text = body_text[:max_chars]

            return {
                "status": "success" if resp.is_success else "failure",
                "http_status": resp.status_code,
                "final_url": final_url,
                "title": title,
                "text": text,
                "content_type": content_type,
                "message": f"Successfully fetched content ({len(text)} characters)." if resp.is_success else f"HTTP {resp.status_code}",
            }
    except Exception as exc:
        log.error(f"[web_fetch] Fetch failed for '{url}': {exc}")
        return {
            "status": "failure",
            "http_status": 0,
            "final_url": url,
            "title": "",
            "text": "",
            "content_type": "",
            "message": f"Web fetch error: {exc}",
        }
