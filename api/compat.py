"""Stable compatibility imports used while app.py is migrated incrementally.

New route modules should import application services through this boundary.
The implementation modules can move later without forcing a simultaneous API
route rewrite or changing the Docker/Uvicorn entrypoint.
"""

from application.provider_store import ProviderStore
from security.auth_service import AuthService

__all__ = ["AuthService", "ProviderStore"]
