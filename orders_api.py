"""Crypto order capture for the provider demo.

A prospect who has sent USDT or SOL tells us here. Verification is manual
and deliberate: no blockchain watcher, no processor, no automatic
activation. The status ladder exists so a human can move an order along
and nobody has to remember where it got to.
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status as http
from pydantic import BaseModel
from supabase import Client

import provider_rules as rules
from aio import offload
from auth import get_current_user
from db import get_supabase

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
    for r in rows:
        r["url"] = f"{site}/demo/signal-desk.html?t={r['token']}"
    return {"ok": True, "links": rows}
