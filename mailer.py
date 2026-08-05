"""SKLZ — email via Resend.

Transactional mail (receipts, notices, account changes) and campaigns, sent
from the verified sklzlabs.com domain.

TWO KINDS OF MAIL, TREATED DIFFERENTLY
======================================
Transactional mail is a reply to something the person did — they bought
something, changed a password, had a payment fail. It goes regardless of
marketing preferences, because withholding a receipt because someone
unsubscribed from a newsletter would be absurd.

Marketing mail is us deciding to contact them. That requires consent, an
unsubscribe link in every message, and a record of who opted out. Those are
legal requirements in most of the places SKLZ has customers, and they are also
just the difference between a business and a nuisance.

The two paths are separate functions on purpose. It should not be possible to
send a campaign by accident through the transactional path.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone

FROM_DEFAULT = "SKLZ Labs <hello@sklzlabs.com>"
REPLY_TO = "fxfactor24@gmail.com"
SITE = "https://www.sklzlabs.com"


def _api_key() -> str:
    return os.environ.get("RESEND_API_KEY", "").strip()


def configured() -> bool:
    return bool(_api_key())


def _post(payload: dict) -> dict:
    key = _api_key()
    if not key:
        return {"ok": False, "reason": "RESEND_API_KEY is not set"}
    try:
        req = urllib.request.Request(
            "https://api.resend.com/emails",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            body = json.loads(r.read().decode())
        return {"ok": True, "id": body.get("id")}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode()[:300]
        except Exception:
            pass
        return {"ok": False, "reason": f"HTTP {exc.code}: {detail}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": str(exc)[:200]}


# ── layout ──────────────────────────────────────────────────────────
def _shell(title: str, body_html: str, footer_extra: str = "") -> str:
    """One layout for everything, matching the platform's own look.

    Inline styles throughout: email clients strip stylesheets, and a stylesheet
    that silently does not apply is how a careful design arrives as unstyled
    text.
    """
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{title}</title></head>
<body style="margin:0;padding:0;background:#080A0F;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
  style="background:#080A0F;padding:28px 14px;">
<tr><td align="center">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
    style="max-width:520px;background:#0F131C;border:1px solid #1E2735;
    border-radius:14px;overflow:hidden;">
    <tr><td style="padding:26px 26px 0;">
      <div style="font-family:ui-monospace,Menlo,monospace;font-size:11px;
        letter-spacing:.14em;color:#F5A623;">SKLZ LABS</div>
    </td></tr>
    <tr><td style="padding:18px 26px 26px;color:#EAEEF6;font-size:15px;
      line-height:1.65;">
      {body_html}
    </td></tr>
    <tr><td style="padding:18px 26px 24px;border-top:1px solid #1E2735;
      color:#5B6377;font-size:11px;line-height:1.75;
      font-family:ui-monospace,Menlo,monospace;">
      Software only &middot; Not financial advice &middot;
      Trading involves risk of loss<br/>
      <a href="{SITE}" style="color:#5B6377;">sklzlabs.com</a>
      {footer_extra}
    </td></tr>
  </table>
</td></tr></table>
</body></html>"""


def _button(text: str, url: str) -> str:
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" '
            f'style="margin:20px 0;"><tr><td style="background:#F5A623;'
            f'border-radius:9px;"><a href="{url}" style="display:inline-block;'
            f'padding:12px 24px;color:#08101A;font-weight:700;font-size:14px;'
            f'text-decoration:none;">{text}</a></td></tr></table>')


# ── transactional ───────────────────────────────────────────────────
def send_transactional(to: str, subject: str, body_html: str,
                       from_addr: str | None = None) -> dict:
    """Mail sent in response to something the person did.

    Sent regardless of marketing preferences — a receipt is not a newsletter.
    """
    if not to:
        return {"ok": False, "reason": "no recipient"}
    return _post({
        "from": from_addr or FROM_DEFAULT,
        "to": [to],
        "reply_to": REPLY_TO,
        "subject": subject,
        "html": _shell(subject, body_html),
    })


def welcome(to: str, plan: str, display_name: str = "") -> dict:
    name = display_name or "there"
    body = f"""
      <h1 style="font-size:20px;margin:0 0 12px;">Welcome, {name}</h1>
      <p style="margin:0 0 14px;color:#8B94A8;">
        Your <b style="color:#EAEEF6;">{plan}</b> subscription is active.
        Everything is unlocked on your dashboard.</p>
      <p style="margin:0 0 6px;color:#8B94A8;">A few things worth knowing:</p>
      <ul style="margin:0 0 14px;padding-left:18px;color:#8B94A8;">
        <li style="margin-bottom:6px;">The performance panels show real numbers,
          losing periods included. That is deliberate.</li>
        <li style="margin-bottom:6px;">Statistics are withheld below 200 trades,
          because smaller samples cannot support a conclusion.</li>
        <li>Signals carry entry, stop and target — and every one is tracked to
          its outcome, including the ones that missed.</li>
      </ul>
      {_button("Open your dashboard", f"{SITE}/dashboard.html")}
      <p style="margin:14px 0 0;color:#5B6377;font-size:13px;">
        Questions go straight to a human — just reply to this email.</p>
    """
    return send_transactional(to, f"Your SKLZ {plan} subscription is active", body)


def payment_failed(to: str, plan: str) -> dict:
    body = f"""
      <h1 style="font-size:20px;margin:0 0 12px;">A payment did not go through</h1>
      <p style="margin:0 0 14px;color:#8B94A8;">
        Your card was declined for the <b style="color:#EAEEF6;">{plan}</b>
        subscription. Access stays on for now — this is a heads-up, not a
        cut-off.</p>
      {_button("Update your card", f"{SITE}/pricing.html")}
      <p style="margin:14px 0 0;color:#5B6377;font-size:13px;">
        If the card is fine and this looks wrong, reply and we will check.</p>
    """
    return send_transactional(to, "Payment issue on your SKLZ subscription", body)


def subscription_cancelled(to: str, plan: str, ends: str = "") -> dict:
    when = f" You keep access until {ends}." if ends else ""
    body = f"""
      <h1 style="font-size:20px;margin:0 0 12px;">Your subscription is cancelled</h1>
      <p style="margin:0 0 14px;color:#8B94A8;">
        The <b style="color:#EAEEF6;">{plan}</b> plan will not renew.{when}</p>
      <p style="margin:0 0 14px;color:#8B94A8;">
        No hard feelings and no retention tricks. If something specific did not
        work, reply and tell us — that is more useful to us than a discount
        offer would be to you.</p>
    """
    return send_transactional(to, "Your SKLZ subscription is cancelled", body)


# ── marketing ───────────────────────────────────────────────────────
def send_campaign(to: str, subject: str, body_html: str,
                  unsubscribe_token: str) -> dict:
    """Mail we chose to send.

    Requires an unsubscribe token — not optional, and not a courtesy. Every
    campaign message must carry a working opt-out, both because the law says
    so in most of SKLZ's markets and because the alternative is being the thing
    everyone's inbox filters.
    """
    if not to:
        return {"ok": False, "reason": "no recipient"}
    if not unsubscribe_token:
        return {"ok": False,
                "reason": ("refusing to send: no unsubscribe token. Marketing "
                           "mail without a working opt-out is not something "
                           "this function will do.")}

    unsub = f"{SITE}/unsubscribe.html?t={unsubscribe_token}"
    footer = (f'<br/><a href="{unsub}" style="color:#5B6377;">Unsubscribe</a> '
              f'from updates like this')

    return _post({
        "from": FROM_DEFAULT,
        "to": [to],
        "reply_to": REPLY_TO,
        "subject": subject,
        "html": _shell(subject, body_html, footer_extra=footer),
        # the header lets mail clients offer one-click opt-out, which
        # measurably reduces spam complaints
        "headers": {
            "List-Unsubscribe": f"<{unsub}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
    })


def verify_domain() -> dict:
    """Check the sending domain is actually verified before a campaign goes out."""
    key = _api_key()
    if not key:
        return {"ok": False, "reason": "RESEND_API_KEY is not set"}
    try:
        req = urllib.request.Request(
            "https://api.resend.com/domains",
            headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
        domains = data.get("data") or []
        ours = [d for d in domains
                if "sklzlabs" in (d.get("name") or "").lower()]
        if not ours:
            return {"ok": False,
                    "reason": "sklzlabs.com is not on this Resend account"}
        d = ours[0]
        return {"ok": d.get("status") == "verified",
                "status": d.get("status"),
                "name": d.get("name"),
                "reason": ("" if d.get("status") == "verified"
                           else f"domain status is {d.get('status')}")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": str(exc)[:200]}
