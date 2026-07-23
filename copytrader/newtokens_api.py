"""SKLZ — new token risk scanner API (read-only, public market data)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from supabase import Client

from auth import get_current_user
from db import get_supabase
from entitlements import require_paid
from copytrader.newtokens import scan

router = APIRouter(prefix="/api/newtokens", tags=["newtokens"])

CHAINS = ["solana", "ethereum", "base", "bsc", "arbitrum", "polygon"]


@router.get("/chains")
async def chains() -> dict:
    return {"chains": CHAINS}


@router.get("/scan")
async def token_scan(chain: str | None = Query(None),
                     limit: int = Query(25, le=40),
                     user=Depends(get_current_user),
                     sb: Client = Depends(get_supabase)) -> dict:
    require_paid(sb, user, "The new token risk scanner")
    return scan(limit=limit, chain=chain)
