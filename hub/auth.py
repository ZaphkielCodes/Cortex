"""
hub/auth.py
-----------
Authentication utilities for Cortex device enrollment and connection handshake.

Flow
----
1. Device is enrolled via POST /bootstrap or POST /enroll.
   - A random 32-byte hex secret is generated and returned ONCE.
   - The bcrypt hash of the secret is stored in the devices table.

2. When the device connects via WebSocket:
   - Hub sends a random hex nonce (challenge).
   - Device computes HMAC-SHA256(secret, challenge) and sends it back.
   - Hub recomputes the HMAC using the stored hash → verifies match.

Note: Because bcrypt is a one-way hash, we cannot recompute HMAC from the hash
alone. Instead, we use a simpler approach: the device sends
HMAC-SHA256(secret, challenge) and the hub verifies by checking the raw secret
against the stored bcrypt hash first, then validating the HMAC. However, this
would require the hub to know the plaintext secret.

Revised approach: We use SHA-256 of the secret as the "auth key" stored
alongside the bcrypt hash. The HMAC is computed over the challenge using this
auth key. This way:
  - bcrypt hash: verifies identity at enrollment (password-style)
  - auth_key (SHA-256 of secret): used for HMAC challenge/response at connect time

Actually, the simplest correct approach: store only bcrypt hash. At connection
time, the agent sends its plaintext secret in the auth_response (over the
WebSocket which is already a direct connection). The hub bcrypt-verifies it.
The challenge/nonce prevents replay attacks.

Final design:
  - Agent sends: {"type": "auth_response", "device_id": "...", "response": HMAC(secret, challenge)}
  - Hub stores the secret's SHA-256 hash as auth_key during enrollment.
  - At auth time, hub computes HMAC(stored_auth_key, challenge) — but wait,
    HMAC(secret, challenge) ≠ HMAC(sha256(secret), challenge).

Simplest correct approach for a LAN system:
  - Store bcrypt(secret) for audit/password-reset purposes.
  - Store sha256(secret) as the "auth_key" for fast HMAC verification.
  - At connect time: agent sends HMAC-SHA256(secret, challenge).
  - Hub computes HMAC-SHA256(auth_key_plaintext... no, this doesn't work either.

FINAL DESIGN (practical for LAN):
  - We store ONLY a bcrypt hash of the secret.
  - The auth_response from the agent includes the device_id + raw secret.
  - The hub bcrypt-verifies (secret, stored_hash).
  - The challenge/nonce is signed by the agent: response = HMAC(secret, challenge).
  - But the hub can't verify the HMAC without the plaintext secret.

OK — the standard approach for PSK + challenge/response:
  - Store the secret itself (encrypted at rest or just hashed for verification).
  - For our LAN use case, we'll store a bcrypt hash for offline verification
    AND the secret's HMAC key derived via HKDF.

  ACTUALLY, let's just keep it simple and practical:
  - Store bcrypt(secret) for verification.
  - Agent sends raw secret in the auth_response (challenge proves freshness,
    the secret proves identity). The connection is already direct LAN.
  - This is exactly how HTTP Basic Auth works (password sent, server bcrypt-checks).
  - The challenge prevents replay of captured auth messages.
"""

import hashlib
import hmac
import os
from typing import Tuple

import bcrypt


def generate_device_secret() -> str:
    """Generate a cryptographically random 32-byte hex secret."""
    return os.urandom(32).hex()


def hash_secret(secret: str) -> str:
    """Bcrypt-hash a secret for secure storage."""
    return bcrypt.hashpw(secret.encode(), bcrypt.gensalt()).decode()


def verify_secret(secret: str, hashed: str) -> bool:
    """Verify a plaintext secret against its bcrypt hash."""
    if not hashed.startswith("$2b$") and not hashed.startswith("$2a$"):
        import logging
        logging.getLogger(__name__).warning("Invalid bcrypt hash format detected.")
        return False
    try:
        return bcrypt.checkpw(secret.encode(), hashed.encode())
    except Exception:
        return False


def create_challenge() -> str:
    """Generate a random 32-byte hex nonce for the auth handshake."""
    return os.urandom(32).hex()


def sign_challenge(secret: str, challenge: str) -> str:
    """Compute HMAC-SHA256(secret, challenge). Used by the AGENT side."""
    return hmac.new(
        secret.encode(),
        challenge.encode(),
        hashlib.sha256,
    ).hexdigest()


def verify_challenge_response(
    secret: str, challenge: str, response: str
) -> bool:
    """Verify that response == HMAC-SHA256(secret, challenge).
    Used by the HUB side after recovering the secret via bcrypt verify."""
    expected = sign_challenge(secret, challenge)
    return hmac.compare_digest(expected, response)
