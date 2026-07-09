"""SKLZ Labs API configuration.

All secrets come from environment variables (set in Railway, never committed).
Supabase key handling supports both the new sb_secret_* keys and the legacy
service_role key — set SUPABASE_SERVICE_KEY to whichever your project uses.
"""
from __future__ import annotations

import os
from functools import lru_cache


class Settings:
    def __init__(self) -> None:
        self.supabase_url: str = os.environ.get("SUPABASE_URL", "")
        # Server-side secret key (sb_secret_* preferred; legacy service_role works).
        self.supabase_service_key: str = os.environ.get("SUPABASE_SERVICE_KEY", "")
        # Comma-separated list of allowed origins for CORS.
        self.allowed_origins: list[str] = [
            o.strip() for o in os.environ.get(
                "ALLOWED_ORIGINS",
                "https://www.sklzlabs.com,https://sklzlabs.com",
            ).split(",") if o.strip()
        ]
        self.environment: str = os.environ.get("ENVIRONMENT", "production")

    @property
    def configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_service_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
