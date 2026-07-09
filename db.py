"""Supabase client provider.

One client is created at startup with the secret key (server-side, bypasses
RLS — this API is the trusted server). Handed to routes via dependency
injection rather than a module global, so it's easy to swap in tests.
"""
from __future__ import annotations

from functools import lru_cache

from supabase import Client, create_client

from config import get_settings


@lru_cache
def _client() -> Client | None:
    s = get_settings()
    if not s.configured:
        return None
    return create_client(s.supabase_url, s.supabase_service_key)


def get_supabase() -> Client:
    """FastAPI dependency: returns the Supabase client or raises if unconfigured."""
    client = _client()
    if client is None:
        raise RuntimeError(
            "Supabase is not configured. Set SUPABASE_URL and SUPABASE_SERVICE_KEY."
        )
    return client
