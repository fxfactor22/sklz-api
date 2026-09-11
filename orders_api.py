"""Crypto order capture for the provider demo.

A prospect who has sent USDT or SOL tells us here. Verification is manual
and deliberate: no blockchain watcher, no processor, no automatic
activation. The status ladder exists so a human can move an order along
and nobody has to remember where it got to.
"""
from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status as http
from pydantic import BaseModel
from supabase import Client

import policy
import provider_rules as rules
from aio import offload
from auth import get_current_user
from db import get_supabase
from routing import RoutingScope, resolve_destinations

router = APIRouter(prefix="/api/orders", tags=["orders"])

PACKAGES = {"signal_desk", "signal_desk_pro", "pro_trader_os"}
# What a crypto payment BUYS. Direct-wallet crypto cannot charge again
# next month, so activation and renewal are different events and the
# order has to say which one it is.
PAYMENT_TYPES = {"setup", "monthly", "setup_plus_initial_month", "renewal"}
ASSETS = {"USDT", "SOL"}
NETWORKS = {"TRC20", "Solana"}
STATUSES = ("awaiting_payment", "submitted", "verifying", "confirmed",
            "activation_pending", "activated", "rejected",
            "information_required")

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_recent: dict[str, list[float]] = {}


class CryptoOrderIn(BaseModel):
    name: str
    email: str
    telegram: str = ""
    package: str
    asset: str
    network: str
    amount: str
    wallet: str = ""
    tx_hash: str
    payment_type: str = "setup_plus_initial_month"


def _clean(v: str, n: int) -> str:
    return (v or "").strip()[:n]


def _rate_ok(ip: str) -> bool:
    import time
    now = time.time()
    hits = [t for t in _recent.get(ip, []) if now - t < 3600]
    if len(hits) >= 6:
        _recent[ip] = hits
        return False
    hits.append(now)
    _recent[ip] = hits
    return True


@router.post("/crypto")
async def submit_crypto_order(body: CryptoOrderIn, request: Request,
                              sb: Client = Depends(get_supabase)) -> dict:
    """Public. A prospect reporting a payment they have already made."""
    ip = (request.client.host if request.client else "") or "unknown"
    if not _rate_ok(ip):
        raise HTTPException(http.HTTP_429_TOO_MANY_REQUESTS,
                            "too many submissions — message us on Telegram")

    name = _clean(body.name, 120)
    email = _clean(body.email, 160).lower()
    tx = _clean(body.tx_hash, 200)
    if not name or not _EMAIL.match(email) or len(tx) < 12:
        raise HTTPException(http.HTTP_400_BAD_REQUEST,
                            "name, a valid email and the transaction hash "
                            "are all required")
    if body.package not in PACKAGES or body.asset not in ASSETS \
       or body.network not in NETWORKS:
        raise HTTPException(http.HTTP_400_BAD_REQUEST, "unknown selection")
    ptype = body.payment_type if body.payment_type in PAYMENT_TYPES \
        else "setup_plus_initial_month"

    reference = "SKLZ-" + secrets.token_hex(3).upper()
    row = {"reference": reference, "name": name, "email": email,
           "telegram": _clean(body.telegram, 80),
           "package": body.package, "asset": body.asset,
           "network": body.network, "amount": _clean(body.amount, 40),
           "wallet": _clean(body.wallet, 120), "tx_hash": tx,
           "payment_type": ptype, "status": "submitted"}

    def _insert():
        return sb.table("crypto_orders").insert(row).execute()

    try:
        await offload(_insert)
    except Exception as exc:  # noqa: BLE001
        if "crypto_orders_tx_uniq" in str(exc) or "duplicate" in str(exc):
            # The same hash twice is one payment. Return the original
            # reference rather than creating a second order.
            def _find():
                return (sb.table("crypto_orders").select("reference")
                        .eq("tx_hash", tx).limit(1).execute()).data or []
            try:
                found = await offload(_find)
                if found:
                    return {"ok": True, "reference": found[0]["reference"],
                            "duplicate": True,
                            "note": "we already have this transaction"}
            except Exception:  # noqa: BLE001
                pass
        raise HTTPException(http.HTTP_503_SERVICE_UNAVAILABLE,
                            "could not record the order") from exc

    print(f"[order] crypto {reference} {body.package} {body.amount} "
          f"{body.asset}/{body.network} from {email}")
    return {"ok": True, "reference": reference, "status": "submitted",
            "note": "We'll verify the transaction and contact you on Telegram."}


@router.get("")
async def list_orders(status_filter: str = "",
                      user=Depends(get_current_user),
                      sb: Client = Depends(get_supabase)) -> dict:
    if not rules.is_platform_admin(user):
        raise HTTPException(http.HTTP_403_FORBIDDEN, "platform admin only")

    def _q():
        q = sb.table("crypto_orders").select("*")
        if status_filter:
            q = q.eq("status", status_filter)
        return (q.order("created_at", desc=True).limit(100).execute()).data or []

    return {"ok": True, "orders": await offload(_q)}


class StatusIn(BaseModel):
    status: str
    notes: str = ""


@router.post("/{reference}/status")
async def set_status(reference: str, body: StatusIn,
                     user=Depends(get_current_user),
                     sb: Client = Depends(get_supabase)) -> dict:
    if not rules.is_platform_admin(user):
        raise HTTPException(http.HTTP_403_FORBIDDEN, "platform admin only")
    if body.status not in STATUSES:
        raise HTTPException(http.HTTP_400_BAD_REQUEST,
                            f"status must be one of {STATUSES}")

    def _upd():
        return (sb.table("crypto_orders")
                .update({"status": body.status,
                         "notes": _clean(body.notes, 500) or None,
                         "updated_at": datetime.now(timezone.utc).isoformat()})
                .eq("reference", reference).execute()).data or []

    rows = await offload(_upd)
    if not rows:
        raise HTTPException(http.HTTP_404_NOT_FOUND, "unknown reference")
    return {"ok": True, "reference": reference, "status": body.status}


# ── commercial model: one server-side source ────────────────────────
# Prices, wallets and quoted crypto amounts live here, not in markup.
# Every value is overridable by environment variable so the offer can
# change without a frontend deploy.
#
# Wallets default to the confirmed public receiving addresses. They are
# PUBLIC addresses — never a key, never a seed. If one is cleared, the
# corresponding payment option disappears from the page rather than
# showing something a customer might send money to.
import os as _os

WALLET_USDT_TRC20 = "TTjmrD5Vkp4jQUe2ACmo4YGcL8zGkQ3U4s"
WALLET_SOL = "8y4eKQDLeoy1P7fAdjXk9cb1bwLCzqjUsV5KgWkQ64Ya"

PACKAGE_DEFS = [
    {"key": "signal_desk", "name": "SKLZ Signal Desk",
     "who": "For traders and signal providers",
     "setup_usd": 499, "monthly_usd": 49,
     "stripe_setup": "sd_setup", "stripe_monthly": "sd_monthly"},
    {"key": "signal_desk_pro", "name": "SKLZ Signal Desk Pro",
     "who": "For established signal businesses",
     "setup_usd": 999, "monthly_usd": 99,
     "stripe_setup": "sdpro_setup", "stripe_monthly": "sdpro_monthly"},
    {"key": "pro_trader_os", "name": "SKLZ Pro Trader OS",
     "who": "The full business layer",
     "setup_usd": 1499, "monthly_usd": 149,
     "stripe_setup": "ptos_setup", "stripe_monthly": "ptos_monthly",
     "custom_from_usd": 1999},
]


def _num_env(name: str, default):
    raw = _os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw) if "." in raw else int(raw)
    except ValueError:
        return default


def _wallets() -> dict:
    """Public receiving addresses. Empty means the option is hidden."""
    usdt = _os.environ.get("SKLZ_WALLET_USDT_TRC20", WALLET_USDT_TRC20).strip()
    sol = _os.environ.get("SKLZ_WALLET_SOL", WALLET_SOL).strip()
    return {
        "usdt_trc20": {"address": usdt, "network": "TRON (TRC20)",
                       "asset": "USDT", "available": bool(usdt),
                       "warning": "USDT — TRC20 ONLY"},
        "sol": {"address": sol, "network": "Solana", "asset": "SOL",
                "available": bool(sol), "warning": "SOL — SOLANA NETWORK ONLY"},
    }


def _package_config() -> dict:
    """The commercial model as the pages should present it."""
    out = {}
    for d in PACKAGE_DEFS:
        k = d["key"].upper()
        setup = _num_env(f"SKLZ_PRICE_{k}_SETUP", d["setup_usd"])
        monthly = _num_env(f"SKLZ_PRICE_{k}_MONTHLY", d["monthly_usd"])
        # SOL moves. There is no rate feed in this phase, so an amount is
        # quoted only when an operator has set one; otherwise the page
        # asks the customer to request it rather than inventing a figure.
        sol_setup = _num_env(f"SKLZ_SOL_{k}_SETUP", None)
        sol_monthly = _num_env(f"SKLZ_SOL_{k}_MONTHLY", None)
        entry = {
            "key": d["key"], "name": d["name"], "who": d["who"],
            "available": _os.environ.get(f"SKLZ_PKG_{k}", "1") != "0",
            "setup": {"usd": setup, "usdt": setup, "sol": sol_setup,
                      "display": f"${setup:,}", "label": "one-time setup"},
            "monthly": {"usd": monthly, "usdt": monthly, "sol": sol_monthly,
                        "display": f"${monthly:,}", "label": "per month"},
            "stripe": {"setup": d["stripe_setup"],
                       "monthly": d["stripe_monthly"]},
            # Every customer pays both. Activation is setup + the first
            # monthly period; the parts stay separate so the customer can
            # see what each buys, and the total is stated because that is
            # what the checkout actually charges.
            "activation": {
                "usd": setup + monthly,
                "usdt": setup + monthly,
                "sol": (None if sol_setup is None or sol_monthly is None
                        else round(sol_setup + sol_monthly, 4)),
                "display": f"${setup + monthly:,}",
                "label": "due today",
                "breakdown": f"${setup:,} setup + ${monthly:,} first month"},
        }
        if d.get("custom_from_usd"):
            entry["custom_from"] = {
                "usd": _num_env(f"SKLZ_PRICE_{k}_CUSTOM_FROM",
                                d["custom_from_usd"]),
                "note": "custom implementations quoted from"}
        out[d["key"]] = entry
    return out


@router.get("/packages")
async def packages() -> dict:
    """Public. The whole commercial model, for both product pages.

    Setup and monthly are returned as separate figures deliberately: the
    customer is shown a setup fee and a monthly fee, and Stripe charges
    exactly those, in separate sessions. No combined "today" total is
    quoted, because no combined charge is made.
    """
    # Stripe appears only when its six Signal Desk products actually
    # exist. A card button that 503s in front of a prospect is worse than
    # no card button, and outreach must not wait on Stripe setup.
    stripe_ready = _os.environ.get("SKLZ_STRIPE_SIGNAL_DESK_READY", "0") == "1"
    return {"ok": True, "packages": _package_config(),
            "wallets": _wallets(),
            "stripe": {"available": stripe_ready,
                       "note": ("" if stripe_ready else
                                "card payment is being finalised; crypto is "
                                "available now")},
            "crypto_note": ("Crypto transfers are irreversible. Confirm the "
                            "wallet address and network before sending.")}


class LeadIn(BaseModel):
    name: str
    email: str
    telegram: str = ""
    audience: str = ""
    markets: str = ""
    note: str = ""
    source: str = ""



leads_router = APIRouter(prefix="/api/leads", tags=["leads"])


@leads_router.post("/demo-request")
async def demo_request(body: LeadIn, request: Request,
                       sb: Client = Depends(get_supabase)) -> dict:
    """A signal provider asking for a personalised private demo.

    The front of the funnel: this is how a cold prospect becomes a demo
    we build for them. Stored, not emailed, so nothing is lost if a
    mailbox rule eats it.
    """
    ip = (request.client.host if request.client else "") or "unknown"
    if not _rate_ok("lead:" + ip):
        raise HTTPException(http.HTTP_429_TOO_MANY_REQUESTS,
                            "too many requests — message us on Telegram")
    name = _clean(body.name, 120)
    email = _clean(body.email, 160).lower()
    if not name or not _EMAIL.match(email):
        raise HTTPException(http.HTTP_400_BAD_REQUEST,
                            "a name and a valid email are required")

    row = {"name": name, "email": email,
           "telegram": _clean(body.telegram, 80),
           "audience": _clean(body.audience, 60),
           "markets": _clean(body.markets, 160),
           "note": _clean(body.note, 800),
           "source": _clean(body.source, 60) or "unknown",
           "status": "new"}

    def _insert():
        return sb.table("demo_leads").insert(row).execute()

    try:
        await offload(_insert)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(http.HTTP_503_SERVICE_UNAVAILABLE,
                            "could not record the request") from exc
    print(f"[lead] demo request from {email} ({body.audience}) "
          f"via {row['source']}")
    return {"ok": True, "note": "We'll build your demo and send the link."}


@leads_router.get("")
async def list_leads(user=Depends(get_current_user),
                     sb: Client = Depends(get_supabase)) -> dict:
    if not rules.is_platform_admin(user):
        raise HTTPException(http.HTTP_403_FORBIDDEN, "platform admin only")

    def _q():
        return (sb.table("demo_leads").select("*")
                .order("created_at", desc=True).limit(200).execute()).data or []

    return {"ok": True, "leads": await offload(_q)}


# ── private 48-hour prospect demo links ─────────────────────────────
demo_router = APIRouter(prefix="/api/demo-links", tags=["demo-links"])

DEMO_HOURS = 48
LANGS = {"en", "ar", "ru"}


class DemoLinkIn(BaseModel):
    provider_name: str
    telegram_channel: str
    language: str = "en"
    contact_name: str = ""
    contact_email: str = ""
    logo_url: str = ""
    note: str = ""
    hours: int = DEMO_HOURS


@demo_router.post("")
async def create_demo_link(body: DemoLinkIn,
                           user=Depends(get_current_user),
                           sb: Client = Depends(get_supabase)) -> dict:
    """Mint one private, prospect-specific demo link."""
    if not rules.is_platform_admin(user):
        raise HTTPException(http.HTTP_403_FORBIDDEN, "platform admin only")
    name = _clean(body.provider_name, 80)
    chan = _clean(body.telegram_channel, 80)
    if not name or not chan:
        raise HTTPException(http.HTTP_400_BAD_REQUEST,
                            "provider name and Telegram channel are required")
    lang = (body.language or "en").lower()[:2]
    if lang not in LANGS:
        lang = "en"
    hours = max(1, min(int(body.hours or DEMO_HOURS), 168))

    # 32 hex chars of CSPRNG. The link IS the credential, so it has to be
    # unguessable rather than merely unlisted.
    token = secrets.token_hex(16)
    expires = datetime.now(timezone.utc) + timedelta(hours=hours)
    row = {"token": token, "provider_name": name, "telegram_channel": chan,
           "language": lang, "contact_name": _clean(body.contact_name, 80),
           "contact_email": _clean(body.contact_email, 160).lower(),
           "logo_url": _clean(body.logo_url, 400),
           "note": _clean(body.note, 500),
           "expires_at": expires.isoformat(),
           "created_by": str(getattr(user, "id", "")) or None}

    def _insert():
        return sb.table("demo_links").insert(row).execute()

    try:
        await offload(_insert)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(http.HTTP_503_SERVICE_UNAVAILABLE,
                            f"could not create the link: {str(exc)[:120]}") from exc

    site = _os.environ.get("SITE_URL", "https://www.sklzlabs.com").rstrip("/")
    return {"ok": True, "token": token,
            "url": f"{site}/demo/signal-desk.html?t={token}",
            "provider_name": name, "telegram_channel": chan,
            "language": lang, "expires_at": expires.isoformat(),
            "hours": hours}


@demo_router.get("/{token}")
async def read_demo_link(token: str,
                         sb: Client = Depends(get_supabase)) -> dict:
    """Public. One link's branding, by token.

    Returns only that prospect's own fields — there is no listing path
    here, so a token cannot be used to discover another prospect.
    """
    tok = (token or "").strip().lower()
    if len(tok) != 32 or any(ch not in "0123456789abcdef" for ch in tok):
        raise HTTPException(http.HTTP_404_NOT_FOUND, "unknown link")

    def _get():
        return (sb.table("demo_links").select("*")
                .eq("token", tok).limit(1).execute()).data or []

    try:
        rows = await offload(_get)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(http.HTTP_503_SERVICE_UNAVAILABLE,
                            "demo store unavailable") from exc
    if not rows:
        raise HTTPException(http.HTTP_404_NOT_FOUND, "unknown link")
    row = rows[0]

    if row.get("revoked"):
        raise HTTPException(http.HTTP_410_GONE, "this demo has been closed")
    try:
        exp = datetime.fromisoformat(
            str(row["expires_at"]).replace("Z", "+00:00"))
    except (TypeError, ValueError, KeyError):
        raise HTTPException(http.HTTP_410_GONE, "this demo has expired") from None
    now = datetime.now(timezone.utc)
    if exp <= now:
        # The server decides expiry. A countdown in the browser is a
        # display, not a lock.
        raise HTTPException(http.HTTP_410_GONE, "this demo has expired")

    def _touch():
        patch = {"opened_count": int(row.get("opened_count") or 0) + 1,
                 "last_opened_at": now.isoformat()}
        if not row.get("first_opened_at"):
            patch["first_opened_at"] = now.isoformat()
        return sb.table("demo_links").update(patch).eq("token", tok).execute()

    try:
        await offload(_touch)
    except Exception:  # noqa: BLE001
        pass

    return {"ok": True, "provider_name": row["provider_name"],
            "telegram_channel": row["telegram_channel"],
            "language": row.get("language") or "en",
            "logo_url": row.get("logo_url") or "",
            "contact_name": row.get("contact_name") or "",
            "expires_at": row["expires_at"],
            "seconds_remaining": int((exp - now).total_seconds())}


@demo_router.get("")
async def list_demo_links(user=Depends(get_current_user),
                          sb: Client = Depends(get_supabase)) -> dict:
    if not rules.is_platform_admin(user):
        raise HTTPException(http.HTTP_403_FORBIDDEN, "platform admin only")

    def _q():
        return (sb.table("demo_links").select("*")
                .order("created_at", desc=True).limit(100).execute()).data or []

    rows = await offload(_q)
    site = _os.environ.get("SITE_URL", "https://www.sklzlabs.com").rstrip("/")

    def _runs():
        try:
            return (sb.table("bot_orders").select("demo_token")
                    .eq("demo_kind", "market").execute()).data or []
        except Exception:
            return []

    used: dict = {}
    for row in await offload(_runs):
        t = row.get("demo_token")
        if t:
            used[t] = used.get(t, 0) + 1

    for r in rows:
        r["url"] = f"{site}/demo/signal-desk.html?t={r['token']}"
        # A prospect link must start unused. This is the number that says so.
        r["runs_used"] = used.get(r["token"], 0)
        r["runs_allowed"] = DEMO_RUNS_PER_TOKEN
        r["runs_remaining"] = max(0, DEMO_RUNS_PER_TOKEN - r["runs_used"])
    return {"ok": True, "links": rows,
            "live_demo_enabled": _demo_enabled(),
            "runs_allowed": DEMO_RUNS_PER_TOKEN}


@demo_router.post("/{token}/revoke")
async def revoke_demo_link(token: str, user=Depends(get_current_user),
                           sb: Client = Depends(get_supabase)) -> dict:
    """Close a demo immediately, without deleting its history."""
    if not rules.is_platform_admin(user):
        raise HTTPException(http.HTTP_403_FORBIDDEN, "platform admin only")
    tok = (token or "").strip().lower()

    def _upd():
        return (sb.table("demo_links").update({"revoked": True})
                .eq("token", tok).execute()).data or []

    rows = await offload(_upd)
    return {"ok": True, "token": tok, "revoked": True,
            "note": "the link now shows the closed page"}


# ── live demo execution ─────────────────────────────────────────────
# A cold prospect clicks a button and a real order appears on a real
# broker's demo account. Everything about WHAT is traded is decided here;
# the browser supplies a token and nothing else.
#
# The guard is identity as OBSERVED. The Runner reports its MT5 login on
# every poll, and this refuses unless that login is the configured demo
# account. A label cannot satisfy it, and neither can a request body.

DEMO_BOT_NAME = "sklz-demo"
DEMO_SYMBOL = "EURUSD"          # the default when none is chosen
DEMO_LOT = 0.01

# A prospect may CHOOSE from this list and nothing else. The browser
# still names no account, no Runner and no lot — it picks a row here.
#
# Lots are per symbol because 0.01 means wildly different money across
# instruments: 0.01 BTCUSD is ~$770 of exposure, 0.01 XAUUSD ~$43, and
# 0.01 EURUSD ~$1,160. A single lot size would make some picks trivial
# and others reckless on a $20k demo account.
DEMO_SYMBOLS: dict[str, dict] = {
    "EURUSD": {"lot": 0.01, "label": "EUR/USD", "class": "forex"},
    "GBPUSD": {"lot": 0.01, "label": "GBP/USD", "class": "forex"},
    "USDJPY": {"lot": 0.01, "label": "USD/JPY", "class": "forex"},
    "XAUUSD": {"lot": 0.01, "label": "Gold", "class": "metal"},
    "BTCUSD": {"lot": 0.01, "label": "Bitcoin", "class": "crypto",
               "always_open": True},
    "ETHUSD": {"lot": 0.10, "label": "Ethereum", "class": "crypto",
               "always_open": True},
}


def _demo_symbol(choice: str | None) -> tuple[str, float]:
    """Resolve a chosen symbol to (symbol, lot). Unknown → the default.

    Never trusts the string: an unlisted symbol falls back rather than
    reaching the broker, so a crafted request cannot trade something we
    never sized.
    """
    key = (choice or "").strip().upper()
    if key in DEMO_SYMBOLS:
        return key, float(DEMO_SYMBOLS[key]["lot"])
    return DEMO_SYMBOL, DEMO_LOT
DEMO_SL_PIPS = 150
DEMO_TP_PIPS = 220
DEMO_RUNS_PER_TOKEN = 3
DEMO_STALE_POLL_SECONDS = 90


def _demo_login() -> str:
    """The one account a demo command may execute on. No default."""
    return _os.environ.get("SKLZ_DEMO_MT5_LOGIN", "").strip()


def _demo_enabled() -> bool:
    return _os.environ.get("SKLZ_LIVE_DEMO_ENABLED", "0") == "1"


def _runner_identity(sb: Client) -> dict:
    """What the demo Runner last reported about itself."""
    try:
        rows = (sb.table("bot_state").select("*")
                .eq("bot_name", DEMO_BOT_NAME).limit(1).execute()).data or []
    except Exception:
        return {}
    return rows[0] if rows else {}


def _demo_guard(sb: Client) -> str:
    """Why a live demo run must not happen, or "" if it may.

    Fail closed at every step. An unset variable, a silent Runner, a
    mismatched login — all of them stop the run. None of them fall back.
    """
    if not _demo_enabled():
        return "live demo execution is switched off"
    expected = _demo_login()
    if not expected:
        return ("SKLZ_DEMO_MT5_LOGIN is not configured — refusing to "
                "execute without a named demo account")

    state = _runner_identity(sb)
    if not state:
        return "the demo Runner has never reported in"

    observed = str(state.get("last_account") or "").strip()
    if not observed:
        return "the demo Runner has not reported an MT5 login"
    if observed != expected:
        # The whole point. A demo command may only execute where the
        # Runner has PROVEN it is attached.
        return (f"refusing: the demo Runner is attached to account "
                f"{observed}, not {expected}")

    server = str(state.get("last_server") or "")
    want_server = _os.environ.get("SKLZ_DEMO_MT5_SERVER", "").strip()
    if want_server and want_server.lower() not in server.lower():
        return (f"refusing: the demo Runner reports server '{server}', "
                f"which is not '{want_server}'")

    seen = state.get("account_seen_at")
    if seen:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(
                str(seen).replace("Z", "+00:00"))).total_seconds()
            if age > DEMO_STALE_POLL_SECONDS:
                return (f"the demo Runner has not polled for {int(age)}s — "
                        f"it may be offline")
        except (TypeError, ValueError):
            pass
    return ""


@demo_router.post("/{token}/run-live")
async def run_live_demo(token: str, request: Request,
                        sb: Client = Depends(get_supabase)) -> dict:
    """Place ONE real order on the demo account, on behalf of a prospect.

    The browser sends a token. It does not send — and cannot send — a bot
    name, an account, a symbol, a lot size, a stop or a destination.
    """
    link = await read_demo_link(token, sb)      # 404/410 handles expiry

    why = _demo_guard(sb)
    if why:
        raise HTTPException(http.HTTP_503_SERVICE_UNAVAILABLE,
                            {"error": "live_demo_unavailable", "detail": why})

    tok = token.strip().lower()

    def _count():
        return (sb.table("bot_orders").select("id")
                .eq("demo_token", tok).execute()).data or []

    try:
        used = len(await offload(_count))
    except Exception:
        used = 0
    if used >= DEMO_RUNS_PER_TOKEN:
        raise HTTPException(
            http.HTTP_429_TOO_MANY_REQUESTS,
            {"error": "demo_runs_exhausted",
             "detail": f"this demo has placed its {DEMO_RUNS_PER_TOKEN} "
                       f"trades. Message us for a fresh link.",
             "used": used})

    # Everything below is decided here, not requested.
    row = {"bot_name": DEMO_BOT_NAME, "symbol": DEMO_SYMBOL, "side": "buy",
           "note": f"[demo] {link['provider_name']}"[:300],
           "lots": DEMO_LOT, "sl": 0, "tp": 0,
           "status": "pending", "demo_token": tok, "demo_kind": "market",
           "mode": "execute", "command_type": "market"}

    def _insert():
        return sb.table("bot_orders").insert(row).execute()

    try:
        res = await offload(_insert)
        created = (res.data or [{}])[0]
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(http.HTTP_503_SERVICE_UNAVAILABLE,
                            {"error": "queue_unavailable",
                             "detail": str(exc)[:160]}) from exc

    print(f"[demo] live run queued token={tok[:8]} "
          f"command={created.get('command_id')} provider={link['provider_name']}")
    return {"ok": True, "command_id": created.get("command_id"),
            "symbol": DEMO_SYMBOL, "side": "buy", "volume": DEMO_LOT,
            "runs_used": used + 1, "runs_allowed": DEMO_RUNS_PER_TOKEN,
            "state": "queued",
            "note": "The demo Runner polls every few seconds."}


@demo_router.get("/{token}/run-live/{command_id}")
async def demo_run_state(token: str, command_id: str,
                         sb: Client = Depends(get_supabase)) -> dict:
    """What the broker actually did. Polled by the prospect's page.

    Never claims a fill the Runner has not reported.
    """
    await read_demo_link(token, sb)
    tok = token.strip().lower()

    def _get():
        return (sb.table("bot_orders").select("*")
                .eq("command_id", command_id).eq("demo_token", tok)
                .limit(1).execute()).data or []

    rows = await offload(_get)
    if not rows:
        raise HTTPException(http.HTTP_404_NOT_FOUND, "unknown command")
    r = rows[0]

    out = {"ok": True, "state": r.get("status"),
           "symbol": r.get("symbol"), "side": r.get("side"),
           "volume": r.get("lots"),
           "ticket": r.get("ticket"), "fill_price": r.get("fill_price"),
           "retcode": r.get("retcode"),
           "broker_comment": r.get("broker_comment"),
           "account": r.get("actual_account"),
           "timings": {"queued": r.get("created_at"),
                       "runner_received": r.get("runner_received_at"),
                       "mt5_requested": r.get("mt5_requested_at"),
                       "broker_confirmed": r.get("broker_confirmed_at")}}
    # Real latency, from the timestamps A3 already records.
    try:
        a = datetime.fromisoformat(str(r["created_at"]).replace("Z", "+00:00"))
        b = datetime.fromisoformat(
            str(r["broker_confirmed_at"]).replace("Z", "+00:00"))
        out["latency_ms"] = int((b - a).total_seconds() * 1000)
    except (TypeError, ValueError, KeyError):
        out["latency_ms"] = None

    # The signal is generated and delivered only once the broker has
    # CONFIRMED the fill. Nothing is announced before it exists.
    if r.get("status") == "succeeded" and r.get("ticket"):
        link = await read_demo_link(token, sb)
        tg = await offload(_deliver_demo_signal, sb, r, link["provider_name"])
        out["telegram"] = tg
        if tg.get("message_id") and out["latency_ms"] is not None:
            try:
                sent = r.get("demo_tg_sent_at") or datetime.now(
                    timezone.utc).isoformat()
                t2 = datetime.fromisoformat(str(sent).replace("Z", "+00:00"))
                out["telegram_latency_ms"] = int((t2 - a).total_seconds() * 1000)
            except (TypeError, ValueError):
                out["telegram_latency_ms"] = None

    # The close result lives on ITS OWN row. The parent was written once
    # at queue time and never updated, so it reported "queued" forever
    # while the position had actually closed. Read the truth from the
    # close command and persist it back.
    out["auto_close"] = await offload(_close_state, sb, r)
    # keep the account tidy without needing a separate scheduler
    try:
        await offload(_sweep_demo_closes, sb)
    except Exception:  # noqa: BLE001
        pass
    return out


# ── auto-close and real demo Telegram ───────────────────────────────
# An interactive prospect needs time to inspect, modify, breakeven and
# close. 90s was right for a fire-and-forget demo and wrong the moment
# the desk became usable. The sweep remains the backstop for positions
# nobody came back to.
DEMO_HOLD_SECONDS = 600
DEMO_TG_CHAT = "-1004489542294"  # @sklzlabsdemo — the ONLY demo destination


def _tg_message_url(chat_id: str, message_id) -> str:
    """A t.me link to one post in a private channel."""
    if not message_id:
        return ""
    cid = str(chat_id).strip()
    if cid.startswith("-100"):
        return f"https://t.me/c/{cid[4:]}/{message_id}"
    return ""


def _demo_signal_text(row: dict, provider: str) -> str:
    """Built from the CONFIRMED fill, never from the request."""
    px = row.get("fill_price")
    sym = row.get("resolved_symbol") or row.get("symbol") or ""
    side = str(row.get("side") or "buy").upper()
    lines = ["\u26a0\ufe0f SIMULATED DEMO — demonstration signal, not a "
             "live trade", ""]
    if provider:
        lines += [f"\U0001F4CA {provider}", ""]
    lines += [f"{side}  {sym}",
              f"Entry: {px}" if px else "Entry: —",
              f"Volume: {row.get('filled_volume') or row.get('lots')}",
              f"Ticket: {row.get('ticket')}", "",
              "Executed on a broker DEMO account. Trading leveraged "
              "products carries risk. Not financial advice."]
    return "\n".join(lines)


def _deliver_demo_signal(sb: Client, row: dict, provider: str) -> dict:
    """Post the signal to @sklzlabsdemo. Once, and nowhere else."""
    if row.get("demo_tg_message_id"):
        return {"message_id": row["demo_tg_message_id"],
                "url": _tg_message_url(DEMO_TG_CHAT, row["demo_tg_message_id"]),
                "replay": True}

    text = _demo_signal_text(row, provider)
    ok, reason = policy.validate(text, strict=False)
    if not ok:
        return {"error": f"policy:{reason}"}

    dests = resolve_destinations(RoutingScope(purpose="demo_signal"))
    dest = dests[0] if dests else None
    if not dest or not dest.enabled:
        return {"error": "demo delivery disabled"}
    # Belt and braces: the demo signal may reach ONE chat id, whatever
    # routing returns. A production channel must be unreachable from here.
    if str(dest.chat_id) not in (DEMO_TG_CHAT, "@sklzlabsdemo"):
        return {"error": f"refused: resolver returned {dest.chat_id}"}

    import urllib.request
    payload = {"chat_id": dest.chat_id, "text": text,
               "disable_web_page_preview": True}
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{dest.token.reveal()}/sendMessage",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode())
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}"}
    if not d.get("ok"):
        return {"error": str(d.get("description", "telegram refused"))[:120]}

    mid = (d.get("result") or {}).get("message_id")
    try:
        sb.table("bot_orders").update({
            "demo_tg_message_id": mid,
            "demo_tg_sent_at": datetime.now(timezone.utc).isoformat()
        }).eq("command_id", row["command_id"]).execute()
    except Exception:  # noqa: BLE001
        pass
    return {"message_id": mid,
            "url": _tg_message_url(DEMO_TG_CHAT, mid), "replay": False}


def _sweep_demo_closes(sb: Client) -> int:
    """Queue a close for every demo fill that has been open long enough.

    Uses the existing A3 close command, by exact ticket, against the demo
    Runner only. Idempotent: `demo_close_command_id` is written once and
    a row with one is never swept again.
    """
    if _demo_guard(sb):
        return 0                      # the guard also protects the sweep
    cutoff = (datetime.now(timezone.utc)
              - timedelta(seconds=DEMO_HOLD_SECONDS)).isoformat()
    try:
        rows = (sb.table("bot_orders")
                .select("command_id,ticket,demo_token,executed_at,"
                        "actual_account,demo_close_command_id")
                .eq("status", "succeeded").eq("demo_kind", "market")
                .is_("demo_close_command_id", "null")
                .lt("executed_at", cutoff).limit(20).execute()).data or []
    except Exception:
        return 0

    expected = _demo_login()
    queued = 0
    for r in rows:
        tk = r.get("ticket")
        if not tk:
            continue
        # never close a ticket that was filled on a different account
        if str(r.get("actual_account") or "") != expected:
            print(f"[demo] NOT closing ticket {tk}: it was filled on "
                  f"{r.get('actual_account')}, not {expected}")
            continue
        try:
            res = (sb.table("bot_orders").insert({
                "bot_name": DEMO_BOT_NAME, "symbol": "", "side": "buy",
                "note": f"[demo] auto-close {tk}", "lots": 0,
                "ticket": tk, "command_type": "close", "status": "pending",
                "demo_token": r.get("demo_token"), "demo_kind": "close",
                "mode": "execute"}).execute()).data or []
            cid = (res[0] or {}).get("command_id")
            sb.table("bot_orders").update(
                {"demo_close_command_id": cid,
                 "demo_close_state": "queued"}) \
                .eq("command_id", r["command_id"]).execute()
            queued += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[demo] close queue failed for {tk}: {str(exc)[:100]}")
    return queued


def _close_state(sb: Client, parent: dict) -> dict:
    """What actually happened to the auto-close, from the close row."""
    out = {"state": parent.get("demo_close_state"),
           "closed_at": parent.get("demo_closed_at"),
           "after_seconds": DEMO_HOLD_SECONDS,
           "command_id": parent.get("demo_close_command_id")}
    cid = parent.get("demo_close_command_id")
    if not cid:
        out["state"] = out["state"] or "not_scheduled"
        return out
    try:
        rows = (sb.table("bot_orders").select(
            "status,executed_at,retcode,broker_comment,resolved_symbol,"
            "actual_account").eq("command_id", cid).limit(1).execute()).data or []
    except Exception:
        return out
    if not rows:
        return out
    row = rows[0]
    out.update({"state": row.get("status"),
                "closed_at": row.get("executed_at"),
                "retcode": row.get("retcode"),
                "broker_comment": row.get("broker_comment"),
                "closed_on_account": row.get("actual_account")})
    # persist once, so the parent stops lying to every later reader
    if row.get("status") in ("succeeded", "failed") and \
            not parent.get("demo_closed_at"):
        try:
            sb.table("bot_orders").update({
                "demo_close_state": row.get("status"),
                "demo_closed_at": row.get("executed_at")
            }).eq("command_id", parent["command_id"]).execute()
        except Exception:  # noqa: BLE001
            pass
    return out


@demo_router.post("/admin/sweep")
async def demo_sweep(user=Depends(get_current_user),
                     sb: Client = Depends(get_supabase)) -> dict:
    """Run the close sweep now. Also runs opportunistically on each poll."""
    if not rules.is_platform_admin(user):
        raise HTTPException(http.HTTP_403_FORBIDDEN, "platform admin only")
    return {"ok": True, "queued": await offload(_sweep_demo_closes, sb)}


# ── interactive control desk ────────────────────────────────────────
CONTROL_ACTIONS = {"buy", "sell", "modify", "breakeven", "close", "positions"}


class ControlIn(BaseModel):
    action: str
    ticket: int | None = None
    sl: float | None = None
    tp: float | None = None
    symbol: str | None = None        # chosen from DEMO_SYMBOLS, or ignored


async def _owned_ticket(sb: Client, tok: str, ticket: int) -> dict:
    """The order THIS token created with THIS ticket, or nothing.

    A prospect may only act on a position their own demo opened. Without
    this, any ticket number typed into a request could be modified or
    closed on the demo account — including one another prospect is
    currently looking at.
    """
    def _q():
        return (sb.table("bot_orders")
                .select("command_id,ticket,symbol,side,lots,fill_price,"
                        "actual_account,status")
                .eq("demo_token", tok).eq("ticket", ticket)
                .eq("status", "succeeded").limit(1).execute()).data or []

    rows = await offload(_q)
    if not rows:
        raise HTTPException(http.HTTP_404_NOT_FOUND,
                            {"error": "not_your_position",
                             "detail": "this demo did not open that ticket"})
    row = rows[0]
    if str(row.get("actual_account") or "") != _demo_login():
        raise HTTPException(http.HTTP_409_CONFLICT,
                            {"error": "account_mismatch",
                             "detail": "that ticket was filled on another "
                                       "account"})
    return row


@demo_router.post("/{token}/control")
async def demo_control(token: str, body: ControlIn,
                       sb: Client = Depends(get_supabase)) -> dict:
    """One door for every real action on the demo master.

    The browser names an action and, for position actions, a ticket it
    owns. It never names a Runner, an account, or a destination.
    """
    link = await read_demo_link(token, sb)          # expiry / revoked
    why = _demo_guard(sb)                            # login / freshness
    if why:
        raise HTTPException(http.HTTP_503_SERVICE_UNAVAILABLE,
                            {"error": "live_demo_unavailable", "detail": why})

    action = (body.action or "").lower().strip()
    if action not in CONTROL_ACTIONS:
        raise HTTPException(http.HTTP_400_BAD_REQUEST,
                            f"action must be one of {sorted(CONTROL_ACTIONS)}")
    tok = token.strip().lower()

    row: dict = {"bot_name": DEMO_BOT_NAME, "status": "pending",
                 "demo_token": tok, "mode": "execute", "lots": 0,
                 "symbol": "", "side": "buy"}

    if action in ("buy", "sell"):
        def _count():
            return (sb.table("bot_orders").select("id")
                    .eq("demo_token", tok).eq("demo_kind", "market")
                    .execute()).data or []
        used = len(await offload(_count))
        if used >= DEMO_RUNS_PER_TOKEN:
            raise HTTPException(http.HTTP_429_TOO_MANY_REQUESTS,
                                {"error": "demo_runs_exhausted",
                                 "used": used})
        sym, lot = _demo_symbol(body.symbol)
        row.update({"symbol": sym, "side": action, "lots": lot,
                    "command_type": "market", "demo_kind": "market",
                    "note": f"[demo] {link['provider_name']} {sym}"[:300]})
    elif action == "positions":
        # A positions read needs no ticket: "what is open?" is a valid
        # question with an empty answer. Requiring one meant the read
        # could never be made without already knowing what to read.
        row.update({"command_type": "positions", "demo_kind": "positions",
                    "note": "[demo] read positions"})
        if body.ticket:
            row["ticket"] = int(body.ticket)
    else:
        if not body.ticket:
            raise HTTPException(http.HTTP_400_BAD_REQUEST,
                                "this action needs the ticket it applies to")
        owned = await _owned_ticket(sb, tok, int(body.ticket))
        row.update({"ticket": int(body.ticket),
                    "symbol": owned.get("symbol") or "",
                    "side": owned.get("side") or "buy",
                    "note": f"[demo] {action} {body.ticket}"[:300]})
        if action == "modify":
            if body.sl is None and body.tp is None:
                raise HTTPException(http.HTTP_400_BAD_REQUEST,
                                    "give a stop, a target, or both")
            row.update({"command_type": "modify", "demo_kind": "modify",
                        "sl": float(body.sl or 0), "tp": float(body.tp or 0)})
        elif action == "breakeven":
            # The Runner defaults the offset to 0 — the stop goes to the
            # ACTUAL entry. offset_pips has no column in bot_orders, and
            # inventing one for two pips is not worth a migration.
            row.update({"command_type": "breakeven", "demo_kind": "breakeven"})
        elif action == "close":
            row.update({"command_type": "close", "demo_kind": "close"})
        else:   # positions
            row.update({"command_type": "positions", "demo_kind": "positions"})

    def _insert():
        return sb.table("bot_orders").insert(row).execute()

    try:
        res = await offload(_insert)
        created = (res.data or [{}])[0]
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(http.HTTP_503_SERVICE_UNAVAILABLE,
                            {"error": "queue_unavailable",
                             "detail": str(exc)[:160]}) from exc

    return {"ok": True, "action": action,
            "command_id": created.get("command_id"),
            "state": "queued",
            "note": "poll the command for the broker's answer"}


@demo_router.get("/{token}/trailing")
async def demo_trailing(token: str,
                        sb: Client = Depends(get_supabase)) -> dict:
    """The trailing configuration as the Runner actually runs it.

    Read-only and display-only: a public demo token must never be able
    to change global Runner configuration.
    """
    await read_demo_link(token, sb)
    trigger = _num_env("SKLZ_TRAIL_TRIGGER_PIPS", 20)
    distance = _num_env("SKLZ_TRAIL_DISTANCE_PIPS", 10)
    return {"ok": True, "active": True,
            "trigger_pips": trigger, "distance_pips": distance,
            "where": "broker",
            "note": ("Protection is applied at the broker and continues if "
                     "the browser is closed."),
            "read_only": True}


# ── AI communication centre ─────────────────────────────────────────
# The model receives FACTS AS DATA and is asked to phrase them. It is
# never asked what happened, because it has no way to know — every
# number here comes from bot_orders, which the Runner wrote from the
# broker's own answer.
AI_INTENTS = {"explain", "breakeven", "modified", "closed", "summary",
              "update"}


class AIDraftIn(BaseModel):
    intent: str = "update"
    ticket: int | None = None
    instruction: str = ""


def _verified_facts(sb: Client, tok: str, ticket: int | None) -> dict:
    """Everything the AI is allowed to know, straight from the ledger."""
    def _q():
        q = (sb.table("bot_orders")
             .select("ticket,symbol,side,lots,fill_price,retcode,"
                     "broker_comment,status,demo_kind,sl,tp,executed_at,"
                     "demo_closed_at,demo_close_state,actual_account")
             .eq("demo_token", tok).order("created_at", desc=True).limit(25))
        return (q.execute()).data or []

    rows = [r for r in (sb and _q() or []) if r.get("status") == "succeeded"]
    trades = [r for r in rows if r.get("demo_kind") == "market"]
    if ticket:
        trades = [r for r in trades if int(r.get("ticket") or 0) == ticket]
    events = [r for r in rows if r.get("demo_kind") in
              ("modify", "breakeven", "close")]
    return {"trades": trades[:5], "events": events[:8],
            "account_type": "broker demo account"}


def _ai_draft(facts: dict, intent: str, instruction: str,
              provider: str) -> tuple[str, str]:
    """Ask Claude to phrase the facts. Returns (draft, error)."""
    import urllib.request
    key = _os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return "", "AI drafting is not configured"

    system = (
        "You write short Telegram updates for a trading signal channel.\n"
        "ABSOLUTE RULE: you may ONLY state facts present in the DATA "
        "below. Never invent or estimate a price, profit, loss, win rate, "
        "subscriber count or outcome. If a number is not in the DATA, do "
        "not mention it.\n"
        "These trades were executed on a BROKER DEMO ACCOUNT. Say so.\n"
        "Never promise results, never imply past performance predicts "
        "future returns, never give financial advice.\n"
        "Write plainly, 2-5 short lines, no hype, no emoji spam.")
    user = (f"Channel: {provider}\nIntent: {intent}\n"
            f"Operator instruction: {instruction or '(none)'}\n\n"
            f"DATA (the only facts you may use):\n"
            f"{json.dumps(facts, indent=2, default=str)}")

    body = json.dumps({"model": "claude-sonnet-4-6", "max_tokens": 500,
                       "system": system,
                       "messages": [{"role": "user", "content": user}]})
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body.encode(),
        headers={"content-type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read().decode())
    except Exception as exc:  # noqa: BLE001
        return "", f"{type(exc).__name__}"
    text = "".join(b.get("text", "") for b in (d.get("content") or [])
                   if b.get("type") == "text").strip()
    return text, "" if text else "the model returned nothing"


@demo_router.post("/{token}/ai-draft")
async def demo_ai_draft(token: str, body: AIDraftIn,
                        sb: Client = Depends(get_supabase)) -> dict:
    """Draft a subscriber update from verified facts. Sends nothing."""
    link = await read_demo_link(token, sb)
    intent = (body.intent or "update").lower()
    if intent not in AI_INTENTS:
        intent = "update"
    tok = token.strip().lower()

    facts = await offload(_verified_facts, sb, tok, body.ticket)
    if not facts["trades"] and not facts["events"]:
        return {"ok": False, "reason": "no verified trades yet — open one "
                                       "first and the draft will describe it"}

    draft, err = await offload(_ai_draft, facts, intent,
                               _clean(body.instruction, 300),
                               link["provider_name"])
    if err:
        return {"ok": False, "reason": err, "facts_used": facts}

    ok, why = policy.validate(draft, strict=False)
    return {"ok": True, "draft": draft, "policy_ok": ok,
            "policy_reason": "" if ok else why,
            "facts_used": facts,
            "note": "edit freely — it is checked again before sending"}


class AISendIn(BaseModel):
    text: str


@demo_router.post("/{token}/ai-send")
async def demo_ai_send(token: str, body: AISendIn,
                       sb: Client = Depends(get_supabase)) -> dict:
    """Send an operator-approved message to the demo channel only."""
    await read_demo_link(token, sb)
    text = (body.text or "").strip()
    if len(text) < 10:
        raise HTTPException(http.HTTP_400_BAD_REQUEST, "nothing to send")
    if len(text) > 3000:
        raise HTTPException(http.HTTP_400_BAD_REQUEST, "too long")

    # Edited text is re-checked. The draft passing is not a licence for
    # whatever the operator typed over it.
    ok, why = policy.validate(text, strict=False)
    if not ok:
        raise HTTPException(http.HTTP_400_BAD_REQUEST,
                            {"error": "policy_refused", "detail": why})

    marked = text if "DEMO" in text.upper() else (
        "\u26a0\ufe0f DEMO ACCOUNT — demonstration message\n\n" + text)
    row = {"command_id": f"ai-{secrets.token_hex(8)}", "fill_price": None,
           "ticket": None, "symbol": "", "side": "", "lots": 0,
           "resolved_symbol": "", "filled_volume": 0,
           "demo_tg_message_id": None}
    out = await offload(_deliver_demo_signal_text, sb, marked)
    return {"ok": bool(out.get("message_id")), **out}


def _deliver_demo_signal_text(sb: Client, text: str) -> dict:
    """Deliver arbitrary approved text to the demo channel. One chat."""
    dests = resolve_destinations(RoutingScope(purpose="demo_signal"))
    dest = dests[0] if dests else None
    if not dest or not dest.enabled:
        return {"error": "demo delivery disabled"}
    if str(dest.chat_id) not in (DEMO_TG_CHAT, "@sklzlabsdemo"):
        return {"error": f"refused: resolver returned {dest.chat_id}"}
    import urllib.request
    payload = {"chat_id": dest.chat_id, "text": text,
               "disable_web_page_preview": True}
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{dest.token.reveal()}/sendMessage",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode())
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}"}
    if not d.get("ok"):
        return {"error": str(d.get("description", "refused"))[:120]}
    mid = (d.get("result") or {}).get("message_id")
    return {"message_id": mid, "url": _tg_message_url(DEMO_TG_CHAT, mid)}


@demo_router.get("/{token}/symbols")
async def demo_symbols(token: str,
                       sb: Client = Depends(get_supabase)) -> dict:
    """What this demo may trade. The list is the permission."""
    await read_demo_link(token, sb)
    return {"ok": True, "default": DEMO_SYMBOL,
            "symbols": [{"symbol": k, **v} for k, v in DEMO_SYMBOLS.items()],
            "note": ("Lot size is set per instrument so the exposure is "
                     "comparable. It is not chosen by the browser.")}
