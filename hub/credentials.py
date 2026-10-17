"""
hub/credentials.py
------------------
Secure credential storage for Cortex Phase 5.
Abstracts OS-level secure storage (Windows Credential Manager / macOS Keychain / Linux Secret Service)
using the `keyring` package.

Ensures tokens are isolated from general Cortex database.
"""

import json
import logging
import keyring
from typing import Optional

log = logging.getLogger(__name__)

SERVICE_NAME = "cortex_integrations"

class CredentialStore:
    @staticmethod
    def _get_key(integration_id: str) -> str:
        return f"{integration_id}_oauth_tokens"

    @staticmethod
    def store_tokens(integration_id: str, tokens: dict) -> bool:
        """Store OAuth tokens securely for a specific integration account."""
        key = CredentialStore._get_key(integration_id)
        try:
            # We serialize the dict of tokens to a string
            token_str = json.dumps(tokens)
            keyring.set_password(SERVICE_NAME, key, token_str)
            return True
        except Exception as e:
            log.error(f"Failed to securely store credentials for {integration_id}: {e}")
            return False

    @staticmethod
    def get_tokens(integration_id: str) -> Optional[dict]:
        """Retrieve stored OAuth tokens."""
        key = CredentialStore._get_key(integration_id)
        try:
            token_str = keyring.get_password(SERVICE_NAME, key)
            if token_str:
                return json.loads(token_str)
            return None
        except Exception as e:
            log.error(f"Failed to retrieve secure credentials for {integration_id}: {e}")
            return None

    @staticmethod
    def delete_tokens(integration_id: str) -> bool:
        """Delete stored OAuth tokens."""
        key = CredentialStore._get_key(integration_id)
        try:
            # Check if it exists before deleting to avoid exceptions from keyring
            if keyring.get_password(SERVICE_NAME, key):
                keyring.delete_password(SERVICE_NAME, key)
            return True
        except Exception as e:
            log.error(f"Failed to delete secure credentials for {integration_id}: {e}")
            return False
