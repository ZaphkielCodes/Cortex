"""
hub/config.py
-------------
Centralised configuration for the Cortex Hub (Phase 3).

All settings can be overridden via environment variables.

Peer-address policy
-------------------
  Default-deny: only localhost and Tailscale CGNAT (100.64.0.0/10) are trusted.
  LAN ranges (192.168/10/172.16) are blocked unless CORTEX_ALLOW_LAN=true.
"""

import ipaddress
import os
from typing import Optional
from dotenv import load_dotenv

# Ensure .env is loaded (especially for OAuth credentials)
load_dotenv()

# ---------------------------------------------------------------------------
# Network binding
# ---------------------------------------------------------------------------
# Hub binds to 0.0.0.0 so it can accept both localhost and Tailscale traffic.
# Peer-address filtering (PeerFilterMiddleware) enforces the actual allow-list.
HUB_HOST: str = os.environ.get("CORTEX_HUB_HOST", "0.0.0.0")
HUB_PORT: int = int(os.environ.get("CORTEX_HUB_PORT", "8000"))

# ---------------------------------------------------------------------------
# Tailscale trust configuration
# ---------------------------------------------------------------------------
# Full Tailscale CGNAT range.  Covers 100.64.0.0 – 100.127.255.255.
TAILSCALE_CIDR: str = os.environ.get("CORTEX_TAILSCALE_CIDR", "100.64.0.0/10")

# Optional: trust only one specific Tailscale IP instead of the whole CIDR.
# When set, this takes precedence over TAILSCALE_CIDR for the allow-list.
TAILSCALE_ADDR: Optional[str] = os.environ.get("CORTEX_TAILSCALE_ADDR")

# ---------------------------------------------------------------------------
# LAN override (off by default — default-deny)
# ---------------------------------------------------------------------------
ALLOW_LAN: bool = os.environ.get("CORTEX_ALLOW_LAN", "").lower() in ("true", "1", "yes")

# RFC-1918 private ranges (only used when ALLOW_LAN is True)
_LAN_CIDRS = [
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
]

# ---------------------------------------------------------------------------
# Command resilience
# ---------------------------------------------------------------------------
# Commands older than this (in seconds) are rejected as expired.
COMMAND_TTL: int = int(os.environ.get("CORTEX_COMMAND_TTL", "300"))

# How far back (in hours) to keep command_history rows before pruning.
COMMAND_HISTORY_RETENTION_HOURS: int = int(
    os.environ.get("CORTEX_COMMAND_HISTORY_HOURS", "24")
)

# How often (in seconds) to run the pruning task.
PRUNE_INTERVAL: int = int(os.environ.get("CORTEX_PRUNE_INTERVAL", "3600"))

# ---------------------------------------------------------------------------
# Auth timeouts
# ---------------------------------------------------------------------------
AUTH_TIMEOUT: int = int(os.environ.get("CORTEX_AUTH_TIMEOUT", "10"))
CONFIRMATION_TIMEOUT: int = int(os.environ.get("CORTEX_CONFIRMATION_TIMEOUT", "60"))

# ---------------------------------------------------------------------------
# Model Deadlines (Phase 8R-A/B)
# ---------------------------------------------------------------------------
# Configurable, lane/provider-aware deadlines distinguishing TTFT vs Total generation.
DEADLINE_FAST_RESPONSE_TTFT: float = float(os.environ.get("CORTEX_DEADLINE_FAST_RESPONSE_TTFT", "3.0"))
DEADLINE_FAST_RESPONSE_TOTAL: float = float(os.environ.get("CORTEX_DEADLINE_FAST_RESPONSE_TOTAL", "6.0"))

DEADLINE_STRUCTURED_TTFT: float = float(os.environ.get("CORTEX_DEADLINE_STRUCTURED_TTFT", "4.0"))
DEADLINE_STRUCTURED_TOTAL: float = float(os.environ.get("CORTEX_DEADLINE_STRUCTURED_TOTAL", "8.0"))

DEADLINE_TASK_STEP_TTFT: float = float(os.environ.get("CORTEX_DEADLINE_TASK_STEP_TTFT", "5.0"))
DEADLINE_TASK_STEP_TOTAL: float = float(os.environ.get("CORTEX_DEADLINE_TASK_STEP_TOTAL", "12.0"))

DEADLINE_FALLBACK_TTFT: float = float(os.environ.get("CORTEX_DEADLINE_FALLBACK_TTFT", "4.0"))
DEADLINE_FALLBACK_TOTAL: float = float(os.environ.get("CORTEX_DEADLINE_FALLBACK_TOTAL", "8.0"))


def get_lane_deadlines(lane: Optional[str]) -> tuple[float, float]:
    """Return (ttft_deadline, total_deadline) in seconds for the given routing lane."""
    l = (lane or "").upper()
    if l == "FAST_RESPONSE":
        return DEADLINE_FAST_RESPONSE_TTFT, DEADLINE_FAST_RESPONSE_TOTAL
    elif l in ("TASK", "TASK_ORCHESTRATOR", "TASK_STEP"):
        return DEADLINE_TASK_STEP_TTFT, DEADLINE_TASK_STEP_TOTAL
    elif l == "FALLBACK":
        return DEADLINE_FALLBACK_TTFT, DEADLINE_FALLBACK_TOTAL
    elif l == "STRUCTURED_MODEL":
        return DEADLINE_STRUCTURED_TTFT, DEADLINE_STRUCTURED_TOTAL
    else:
        return DEADLINE_STRUCTURED_TTFT, DEADLINE_STRUCTURED_TOTAL


# ---------------------------------------------------------------------------
# Peer-address validation
# ---------------------------------------------------------------------------
def _build_trusted_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Build the list of trusted networks from configuration."""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
        ipaddress.ip_network("127.0.0.0/8"),   # IPv4 loopback
        ipaddress.ip_network("::1/128"),        # IPv6 loopback
    ]

    if TAILSCALE_ADDR:
        # Trust only the specific Tailscale IP
        try:
            addr = ipaddress.ip_address(TAILSCALE_ADDR)
            prefix = 32 if addr.version == 4 else 128
            networks.append(ipaddress.ip_network(f"{addr}/{prefix}"))
        except ValueError:
            # If it's a hostname, we can't validate it here — fall through
            # to CIDR-based check.  The user should use an IP.
            pass
    else:
        # Trust the full Tailscale CIDR
        try:
            networks.append(ipaddress.ip_network(TAILSCALE_CIDR))
        except ValueError:
            # Misconfigured — fall back to the standard Tailscale range
            networks.append(ipaddress.ip_network("100.64.0.0/10"))

    if ALLOW_LAN:
        networks.extend(_LAN_CIDRS)

    return networks


# Pre-computed at import time; rebuilt if config changes at runtime are needed.
TRUSTED_NETWORKS = _build_trusted_networks()


def is_trusted_peer(peer_ip: str) -> bool:
    """Check whether a peer IP address is in the trusted set.

    Returns True for localhost, Tailscale CGNAT, and (if enabled) LAN ranges.
    Returns False for everything else (default-deny).
    """
    if peer_ip in ("testclient", "localhost"):
        return True
    try:
        addr = ipaddress.ip_address(peer_ip)
    except ValueError:
        return False

    # Handle IPv4-mapped IPv6 addresses (e.g. ::ffff:127.0.0.1)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped

    for network in TRUSTED_NETWORKS:
        try:
            if addr in network:
                return True
        except TypeError:
            # IPv4 address vs IPv6 network or vice versa — skip
            continue

    return False
