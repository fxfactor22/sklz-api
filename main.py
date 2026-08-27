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
from marketplace import router as marketplace_router
from copytrader.connections_api import router as copy_router
from copytrader.subscriptions_api import router as copy_subs_router
from copytrader.execution_api import router as copy_exec_router
from copytrader.trading_api import router as copy_trade_router
from copytrader.portfolio_api import router as portfolio_router
from copytrader.newtokens_api import router as newtokens_router
from public_stats import router as public_router
from updates_api import router as updates_router
from profile_api import router as profile_router
from research_api import router as research_router
import alerts_api
from alerts_api import router as alerts_router
from mail_api import router as mail_router
from video_api import router as video_router
import signal_lifecycle
import copy_api
import copy_scheduler
from copy_scheduler import router as poll_router
from tgbot import router as tgbot_router
from profile_api import codes_router
from signals_engine import router as signals_router
from scanner import router as scanner_router
from academy import router as academy_router
from affiliate import router as affiliate_router
from partner import router as partner_router
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
    # PATCH and DELETE were missing, which made every update and delete
    # endpoint unreachable from the browser — the preflight was rejected
    # before the request was ever sent.
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(bot_router)
app.include_router(signal_router)
app.include_router(gpt_router)
app.include_router(account_router)
app.include_router(journal_router)
app.include_router(marketplace_router)
app.include_router(copy_router)
app.include_router(copy_subs_router)
app.include_router(copy_exec_router)
app.include_router(copy_trade_router)
app.include_router(portfolio_router)
app.include_router(newtokens_router)
app.include_router(public_router)
app.include_router(updates_router)
app.include_router(profile_router)
app.include_router(research_router)
app.include_router(alerts_router)
app.include_router(mail_router)
app.include_router(video_router)
app.include_router(signal_lifecycle.router)
app.include_router(copy_api.router)
alerts_api.start(app)
signal_lifecycle.start(app)
app.include_router(poll_router)
copy_scheduler.start(app)
app.include_router(tgbot_router)
app.include_router(codes_router)
app.include_router(signals_router)
app.include_router(scanner_router)
app.include_router(academy_router)
app.include_router(affiliate_router)
app.include_router(partner_router)
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
