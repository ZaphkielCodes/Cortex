"""
hub/integrations/base.py
------------------------
Base classes and interfaces for Phase 5 Integrations.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

class IntegrationPlugin(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        """Name of the integration plugin (e.g. 'gmail', 'calendar')"""
        pass

    @abstractmethod
    async def get_authorization_url(self, account_id: str, redirect_uri: str) -> dict:
        """Returns auth url, state, and other metadata (like PKCE verifier) needed for OAuth flow."""
        pass

    @abstractmethod
    async def exchange_code(self, account_id: str, code: str, redirect_uri: str, metadata: dict) -> bool:
        """Exchange the code for tokens and store them securely."""
        pass

    @abstractmethod
    async def revoke(self, account_id: str) -> bool:
        """Revoke tokens and disconnect."""
        pass

    @abstractmethod
    async def get_valid_token(self, account_id: str) -> Optional[str]:
        """Returns a valid access token, refreshing if necessary."""
        pass

    @abstractmethod
    async def execute_action(self, account_id: str, action: str, **kwargs) -> Any:
        """Executes a specific action via the integration."""
        pass
