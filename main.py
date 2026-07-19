"""SKLZ Labs API — application entrypoint.

Phase 1a: waitlist capture. Structured so accounts, license validation, and
Stripe webhooks bolt on as additional routers without touching this file's
shape.
"""
from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import get_settings
from auth import router as auth_router
from bot_ingest import router as bot_router
from signals import router as signal_router
from account import router as account_router
from journal import router as journal_router
from signals_engine import router as signals_router
from scanner import router as scanner_router
from academy import router as academy_router
from billing import router as billing_router
from tv_access import router as tv_router
from tradegpt import router as gpt_router
from waitlist import router as waitlist_router

app = FastAPI(
    title="SKLZ Labs API",
    version="0.1.0",
    docs_url="/docs",
    openapi_url="/openapi.json",
)

settings = get_settings()

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(bot_router)
app.include_router(signal_router)
app.include_router(gpt_router)
app.include_router(account_router)
app.include_router(journal_router)
app.include_router(signals_router)
app.include_router(scanner_router)
app.include_router(academy_router)
app.include_router(billing_router)
app.include_router(tv_router)
app.include_router(waitlist_router)


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "service": "sklz-api",
        "supabase_configured": settings.configured,
        "environment": settings.environment,
    }


@app.get("/")
async def root() -> dict:
    return {"service": "SKLZ Labs API", "docs": "/docs"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port)
