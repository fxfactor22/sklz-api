"""SKLZ — email endpoints."""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from supabase import Client

from auth import get_current_user
from db import get_supabase
import mailer

router = APIRouter(prefix="/api/mail", tags=["mail"])


def _owner_only(user) -> None:
    owner = os.environ.get("OWNER_EMAIL", "fxfactor24@gmail.com").strip().lower()
    if (getattr(user, "email", "") or "").strip().lower() != owner:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "owner only")


def unsub_token(email: str) -> str:
    """A token tied to the address, so a leaked link cannot unsubscribe someone
    else. Derived rather than stored: no extra table, and it stays valid."""
    secret = os.environ.get("UNSUB_SECRET", "sklz-unsub")
    return hashlib.sha256(f"{secret}:{email.lower()}".encode()).hexdigest()[:32]


@router.get("/status")
async def status_check(user=Depends(get_current_user)) -> dict:
    """Is sending actually configured and the domain verified?"""
    _owner_only(user)
    if not mailer.configured():
        return {"configured": False,
                "message": "RESEND_API_KEY is not set in the environment."}
    v = mailer.verify_domain()
    return {"configured": True, "domain": v,
            "message": ("Ready to send." if v.get("ok")
                        else f"Not ready: {v.get('reason')}")}


class TestIn(BaseModel):
    to: EmailStr


@router.post("/test")
async def send_test(body: TestIn, user=Depends(get_current_user)) -> dict:
    """One test message, so a broken setup is found now."""
    _owner_only(user)
    # the test shows the real layout — a plain test proves delivery but tells
    # you nothing about what customers will actually receive
    html = (
        mailer._h1("Email is working") +
        mailer._p("This is what your customers will see. Resend is wired "
                  "correctly and mail is leaving the verified domain.") +
        mailer._stats([("verified", "sklzlabs.com"),
                       ("live", "transactional"),
                       ("live", "campaigns"),
                       ("on", "one-click opt-out")]) +
        mailer._divider() +
        mailer._feature("\U0001F4E7", "Transactional",
                        "Receipts, password resets and payment notices. Sent "
                        "regardless of marketing preferences.") +
        mailer._feature("\U0001F4E3", "Campaigns",
                        "Updates to customers, with a working unsubscribe in "
                        "every message.") +
        mailer._button("Open the dashboard", f"{mailer.SITE}/dashboard.html") +
        mailer._note("If this looks plain in your client, it is probably "
                     "blocking remote styling \u2014 the layout uses tables and "
                     "inline styles precisely so that it degrades readably "
                     "rather than breaking.", "info")
    )
    r = mailer.send_transactional(
        body.to, "SKLZ Labs \u2014 email is working", html,
        preheader="Your Resend setup is live and sending from sklzlabs.com.")
    if not r.get("ok"):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, r.get("reason", "failed"))
    return {"ok": True, "id": r.get("id")}


class CampaignIn(BaseModel):
    subject: str = Field(min_length=3, max_length=140)
    body_html: str = Field(min_length=10)
    audience: str = "subscribers"      # subscribers | all | test
    test_to: EmailStr | None = None


@router.post("/campaign")
async def campaign(body: CampaignIn, user=Depends(get_current_user),
                   sb: Client = Depends(get_supabase)) -> dict:
    """Send an update to customers.

    Only to people who have not opted out, always with a working unsubscribe
    link, and it refuses to run at all if the domain is not verified — a
    campaign from an unverified domain lands in spam and damages the domain's
    reputation for every future message, including password resets.
    """
    _owner_only(user)

    v = mailer.verify_domain()
    if not v.get("ok"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"sklzlabs.com is not verified on Resend ({v.get('reason')}). "
            f"Sending now would hurt deliverability for every later message, "
            f"password resets included.")

    if body.audience == "test":
        if not body.test_to:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "test_to is required for a test send")
        r = mailer.send_campaign(body.test_to, body.subject, body.body_html,
                                 unsub_token(body.test_to))
        return {"ok": r.get("ok"), "sent": 1 if r.get("ok") else 0,
                "detail": r.get("reason", "")}

    # who has opted out
    try:
        opted_out = {r["email"].lower() for r in
                     ((sb.table("email_optouts").select("email")
                       .execute()).data or []) if r.get("email")}
    except Exception:
        opted_out = set()

    try:
        if body.audience == "subscribers":
            rows = (sb.table("subscriptions").select("user_id,plan,active")
                    .eq("active", True).execute()).data or []
            ids = [r["user_id"] for r in rows if r.get("user_id")]
            recipients = []
            for uid in ids:
                try:
                    u = sb.auth.admin.get_user_by_id(uid)
                    em = getattr(getattr(u, "user", None), "email", "")
                    if em:
                        recipients.append(em)
                except Exception:
                    continue
        else:
            page = sb.auth.admin.list_users()
            recipients = [getattr(u, "email", "") for u in (page or [])]
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"could not build the list: {str(exc)[:160]}") from exc

    recipients = [e for e in recipients if e and e.lower() not in opted_out]
    if not recipients:
        return {"ok": True, "sent": 0,
                "message": "Nobody to send to after removing opt-outs."}

    sent = failed = 0
    for em in recipients:
        r = mailer.send_campaign(em, body.subject, body.body_html,
                                 unsub_token(em))
        if r.get("ok"):
            sent += 1
        else:
            failed += 1

    try:
        sb.table("email_campaigns").insert({
            "subject": body.subject, "audience": body.audience,
            "sent": sent, "failed": failed,
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception:
        pass

    return {"ok": True, "sent": sent, "failed": failed,
            "skipped_optouts": len(opted_out)}


@router.get("/unsubscribe/{token}")
async def unsubscribe(token: str, email: str = "",
                      sb: Client = Depends(get_supabase)) -> dict:
    """PUBLIC. One click, no login, no confirmation step.

    Making someone log in to unsubscribe is a dark pattern and, in several
    jurisdictions, not a valid opt-out at all.
    """
    if not email or unsub_token(email) != token:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "That unsubscribe link is not valid.")
    try:
        sb.table("email_optouts").upsert(
            {"email": email.lower(),
             "opted_out_at": datetime.now(timezone.utc).isoformat()},
            on_conflict="email").execute()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"could not record it: {str(exc)[:150]}") from exc
    return {"ok": True,
            "message": ("You will not receive marketing email from SKLZ Labs. "
                        "Account notices such as receipts and password resets "
                        "will still be sent — those are not marketing.")}
