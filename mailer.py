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
                     "Content-Type": "application/json",
                     # Cloudflare fronts the Resend API and rejects the default
                     # Python-urllib agent outright (403, error code 1010).
                     # A named agent is all it wants.
                     "User-Agent": "SKLZ-Labs/1.0 (+https://www.sklzlabs.com)",
                     "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            body = json.loads(r.read().decode())
        return {"ok": True, "id": body.get("id")}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode()[:300]
        except Exception:
            pass
        if exc.code == 403 and "1010" in detail:
            return {"ok": False,
                    "reason": ("Cloudflare blocked the request (error 1010). "
                               "This is the User-Agent, not your API key.")}
        return {"ok": False, "reason": f"HTTP {exc.code}: {detail[:200]}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": str(exc)[:200]}


# ── layout ──────────────────────────────────────────────────────────
def _shell(title: str, body_html: str, footer_extra: str = "",
           preheader: str = "") -> str:
    """One layout for everything, matching the platform's own look.

    Tables and inline styles throughout, because email clients strip
    stylesheets and ignore flexbox. Outlook in particular renders through Word,
    so anything clever silently degrades — a table that works everywhere beats
    a layout that looks better in three clients and breaks in the rest.
    """
    pre = ""
    if preheader:
        # the grey line shown next to the subject in most inboxes. Left empty,
        # clients grab the first words of the body, which is rarely what you
        # would choose.
        pre = (f'<div style="display:none;max-height:0;overflow:hidden;'
               f'opacity:0;">{preheader}</div>')

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<meta name="color-scheme" content="dark"/>
<title>{title}</title></head>
<body style="margin:0;padding:0;background:#080A0F;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,sans-serif;">
{pre}
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
  style="background:#080A0F;padding:24px 12px;">
<tr><td align="center">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
    style="max-width:560px;background:#0F131C;border:1px solid #1E2735;
    border-radius:16px;overflow:hidden;">

    <!-- masthead -->
    <tr><td style="background:#0B0E15;padding:22px 26px;
      border-bottom:1px solid #1E2735;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td style="font-family:ui-monospace,Menlo,Consolas,monospace;
          font-size:15px;font-weight:700;letter-spacing:.16em;color:#F5A623;">
          SKLZ<span style="color:#EAEEF6;">LABS</span></td>
        <td align="right" style="font-family:ui-monospace,Menlo,monospace;
          font-size:9.5px;letter-spacing:.1em;color:#5B6377;">
          TRADING TOOLS THAT SHOW THEIR WORK</td>
      </tr></table>
    </td></tr>

    <!-- gold rule -->
    <tr><td style="height:3px;background:#F5A623;font-size:0;line-height:0;">
      &nbsp;</td></tr>

    <tr><td style="padding:28px 26px;color:#EAEEF6;font-size:15px;
      line-height:1.65;">
      {body_html}
    </td></tr>

    <tr><td style="padding:18px 26px 22px;border-top:1px solid #1E2735;
      background:#0B0E15;color:#5B6377;font-size:11px;line-height:1.8;
      font-family:ui-monospace,Menlo,monospace;">
      Software only &middot; Not financial advice &middot;
      Trading involves risk of loss<br/>
      <a href="{SITE}" style="color:#8B94A8;text-decoration:none;">sklzlabs.com</a>
      {footer_extra}
    </td></tr>
  </table>
</td></tr></table>
</body></html>"""


def _button(text: str, url: str) -> str:
    """A wide, obvious call to action. Buttons in email are table cells with a
    background — a styled <a> alone collapses in Outlook."""
    return (f'<table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0" style="margin:22px 0;"><tr>'
            f'<td align="center" style="background:#F5A623;border-radius:10px;">'
            f'<a href="{url}" style="display:block;padding:14px 20px;'
            f'color:#08101A;font-weight:700;font-size:15px;'
            f'text-decoration:none;">{text}</a></td></tr></table>')


def _h1(text: str) -> str:
    return (f'<h1 style="font-size:22px;line-height:1.3;margin:0 0 14px;'
            f'color:#EAEEF6;font-weight:700;letter-spacing:-.01em;">{text}</h1>')


def _p(text: str) -> str:
    return f'<p style="margin:0 0 15px;color:#8B94A8;">{text}</p>'


def _stats(items: list[tuple[str, str]]) -> str:
    """A row of figures. Two columns rather than four: narrow phone screens
    squash anything more into unreadable slivers."""
    cells = ""
    for i, (value, label) in enumerate(items):
        if i % 2 == 0 and i:
            cells += '</tr><tr>'
        cells += (
            f'<td width="50%" style="padding:6px;">'
            f'<table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0" style="background:#141A26;border:1px solid #1E2735;'
            f'border-radius:10px;"><tr><td style="padding:14px;">'
            f'<div style="font-family:ui-monospace,Menlo,monospace;font-size:20px;'
            f'font-weight:700;color:#EAEEF6;">{value}</div>'
            f'<div style="font-family:ui-monospace,Menlo,monospace;font-size:9px;'
            f'letter-spacing:.06em;color:#5B6377;text-transform:uppercase;'
            f'margin-top:5px;">{label}</div>'
            f'</td></tr></table></td>')
    return (f'<table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0" style="margin:6px 0 18px;"><tr>{cells}</tr></table>')


def _feature(icon: str, title: str, text: str) -> str:
    return (f'<table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0" style="margin-bottom:14px;"><tr>'
            f'<td width="34" valign="top" style="font-size:19px;'
            f'padding-top:1px;">{icon}</td>'
            f'<td><div style="color:#EAEEF6;font-weight:600;font-size:14.5px;'
            f'margin-bottom:3px;">{title}</div>'
            f'<div style="color:#8B94A8;font-size:13.5px;line-height:1.6;">'
            f'{text}</div></td></tr></table>')


def _note(text: str, tone: str = "info") -> str:
    """A tinted callout. Used for the honest caveats — they should be visible,
    not buried in a paragraph."""
    colours = {"info": ("#1DA9E0", "rgba(29,169,224,.08)"),
               "warn": ("#F5A623", "rgba(245,166,35,.08)"),
               "good": ("#34D399", "rgba(52,211,153,.08)")}
    line, bg = colours.get(tone, colours["info"])
    return (f'<table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0" style="margin:18px 0;"><tr>'
            f'<td style="background:{bg};border:1px solid {line}40;'
            f'border-left:3px solid {line};border-radius:8px;padding:13px 15px;'
            f'color:#8B94A8;font-size:13px;line-height:1.65;">{text}</td>'
            f'</tr></table>')


def _divider() -> str:
    return ('<table role="presentation" width="100%"><tr><td '
            'style="border-top:1px solid #1E2735;font-size:0;line-height:0;'
            'padding:9px 0;">&nbsp;</td></tr></table>')


# ── transactional ───────────────────────────────────────────────────
def send_transactional(to: str, subject: str, body_html: str,
                       from_addr: str | None = None,
                       preheader: str = "") -> dict:
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
        "html": _shell(subject, body_html, preheader=preheader),
    })


def welcome(to: str, plan: str, display_name: str = "") -> dict:
    name = display_name or "there"
    body = (
        _h1(f"Welcome, {name}") +
        _p(f"Your <b style='color:#EAEEF6;'>{plan}</b> subscription is live. "
           f"Everything below is unlocked on your dashboard right now.") +
        _stats([("4", "TradingView indicators"),
                ("\u221e", "AI chart analyses"),
                ("24/7", "signal coverage"),
                ("200", "trades before we claim an edge")]) +
        _divider() +
        _feature("\U0001F4CA", "Indicator suite",
                 "Structure, regime, divergence and multi-timeframe scanning \u2014 "
                 "each with an honest performance panel showing what it actually did.") +
        _feature("\U0001F9E0", "TradeGPT",
                 "Send a chart screenshot, get a structured plan with exact "
                 "position sizing.") +
        _feature("\U0001F4E1", "Signals",
                 "Real entry zones, stops and targets \u2014 every one tracked to "
                 "its outcome, including the ones that missed.") +
        _feature("\U0001F4D2", "Honest journal",
                 "Flags when your setups are statistically coin-flips. Most are.") +
        _button("Open your dashboard", f"{SITE}/dashboard.html") +
        _note("We withhold statistics below 200 trades because smaller samples "
              "cannot support a conclusion. You will sometimes see \u2018not "
              "enough data\u2019 where other platforms would show you a "
              "confident number. That is the point.", "info") +
        _p("<span style='font-size:13.5px;color:#5B6377;'>Questions go to a "
           "human \u2014 just reply to this email.</span>")
    )
    return send_transactional(
        to, f"Your SKLZ {plan} subscription is live", body,
        preheader="Everything is unlocked \u2014 here is what you have access to.")


def payment_failed(to: str, plan: str) -> dict:
    body = (
        _h1("A payment did not go through") +
        _p(f"Your card was declined for the <b style='color:#EAEEF6;'>{plan}</b> "
           f"subscription.") +
        _note("Your access stays on. This is a heads-up, not a cut-off \u2014 "
              "we will retry, and nothing is switched off while we do.", "warn") +
        _button("Update your card", f"{SITE}/pricing.html") +
        _p("<span style='font-size:13.5px;color:#5B6377;'>If the card is fine "
           "and this looks wrong, reply and we will look into it.</span>")
    )
    return send_transactional(
        to, "Payment issue on your SKLZ subscription", body,
        preheader="Your access stays on \u2014 we just need a working card.")


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
            headers={"Authorization": f"Bearer {key}",
                     "User-Agent": "SKLZ-Labs/1.0 (+https://www.sklzlabs.com)",
                     "Accept": "application/json"})
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
