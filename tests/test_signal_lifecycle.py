"""One trade, one message: the signal card is posted once with STATUS: OPEN
and edited in place on secured / closed, in every channel it went to. The
daily summary reaches every signal channel."""
import asyncio
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import routing  # noqa: E402
import signals_engine as SE  # noqa: E402
import signal_lifecycle as SL  # noqa: E402
import sklz_arabic as AR  # noqa: E402

KEY = "signal-key-value"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SIGNAL_WEBHOOK_KEY", KEY)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok-main")
    monkeypatch.setenv("TG_CHANNEL_METALS", "-100111")
    monkeypatch.setenv("TG_CHANNEL_SIGNALS", "-100222")
    monkeypatch.setenv("TG_MIRROR_CHAT", "-100333")
    monkeypatch.setenv("TG_MIRROR_TOKEN", "tok-mirror")
    monkeypatch.setenv("TG_ARABIC_CHAT", "-100444")
    monkeypatch.setenv("TG_ARABIC_TOKEN", "tok-ar")
    monkeypatch.setenv("SIGNAL_CHANNEL_ID", "-100555")
    monkeypatch.setenv("TG_SALES_BOT_TOKEN", "tok-sales")
    for v in ("TG_CHANNEL_FOREX", "TG_CHANNEL_CRYPTO", "TG_CHANNEL_STOCKS",
              "TG_MIRROR2_CHAT", "TG_MIRROR3_CHAT"):
        monkeypatch.delenv(v, raising=False)
    routing.set_resolver(routing.EnvTelegramDestinationResolver())


# ── Telegram double: records sends and edits, hands out message ids ──
class TG:
    def __init__(self):
        self.sent, self.edits, self.next_id = [], [], 100

    def send(self, chat, text, token=""):
        if not (chat and token):
            return None
        self.next_id += 1
        self.sent.append({"chat": chat, "token": token, "text": text,
                          "id": self.next_id})
        return self.next_id

    def edit(self, chat, mid, text, token):
        self.edits.append({"chat": chat, "id": int(mid), "text": text,
                           "token": token})
        return True


@pytest.fixture
def tg(monkeypatch):
    t = TG()
    monkeypatch.setattr(SE, "_post_telegram_id", t.send)
    monkeypatch.setattr(SE, "edit_message", t.edit)
    return t


# ── Supabase double ──────────────────────────────────────────────────
class _Q:
    def __init__(self, db, table, fail_cols=()):
        self.db, self.t, self.fail = db, table, fail_cols
        self.f, self.ins, self.gte_ = [], [], []
        self._op, self._patch, self._row, self._cols = "select", None, None, ""

    def select(self, cols="*", **k): self._cols = cols; return self
    def eq(self, c, v): self.f.append((c, v)); return self
    def in_(self, c, vs): self.ins.append((c, list(vs))); return self
    def gte(self, c, v): self.gte_.append((c, v)); return self
    def limit(self, *a, **k): return self
    def order(self, *a, **k): return self
    def insert(self, row): self._op, self._row = "insert", dict(row); return self
    def update(self, patch): self._op, self._patch = "update", dict(patch); return self

    def _hit(self):
        out = []
        for r in self.db.setdefault(self.t, []):
            if any(r.get(c) != v for c, v in self.f):
                continue
            if any(r.get(c) not in vs for c, vs in self.ins):
                continue
            if any((r.get(c) or "") < v for c, v in self.gte_):
                continue
            out.append(r)
        return out

    def execute(self):
        if self._op == "insert":
            rows = self.db.setdefault(self.t, [])
            self._row.setdefault("id", len(rows) + 1)
            self._row.setdefault("status", "active")
            self._row.setdefault("received_at", "2026-09-23T05:00:00+00:00")
            rows.append(self._row)
            return types.SimpleNamespace(data=[dict(self._row)])
        if self._op == "update":
            if any(c in self.fail for c in self._patch):
                raise RuntimeError("column does not exist")
            hit = self._hit()
            for r in hit:
                r.update(self._patch)
            return types.SimpleNamespace(data=[dict(r) for r in hit])
        if any(c in self._cols for c in self.fail):
            raise RuntimeError("column does not exist")
        return types.SimpleNamespace(data=[dict(r) for r in self._hit()])


class SB:
    def __init__(self, db=None, fail_cols=()):
        self.db, self.fail = db if db is not None else {}, fail_cols

    def table(self, name):
        return _Q(self.db, name, self.fail)


PAYLOAD = {"symbol": "XAUUSD", "side": "buy", "price": 2400.0, "atr": 4.0,
           "timeframe": "M5", "note": "SKLZ bot scan — X",
           "entry": 2400.0, "sl": 2394.0, "tp": 2412.0}


def _post(sb):
    return asyncio.run(SE.signal_webhook(KEY, dict(PAYLOAD), sb))


# ── A. the card is posted once, with STATUS: OPEN, and remembered ────
def test_a_card_posted_with_open_status_and_ids_stored(tg):
    sb = SB()
    out = _post(sb)
    assert out["ok"] and out["signal_id"] == 1
    chats = sorted(s["chat"] for s in tg.sent)
    assert chats == ["-100111", "-100222", "-100333", "-100444"]
    assert all("STATUS: OPEN" in s["text"] or "الحالة: مفتوحة" in s["text"]
               for s in tg.sent)
    row = sb.db["signals"][0]
    keys = sorted(p["key"] for p in row["tg_posts"])
    assert keys == ["arabic", "general", "metals", "tg_mirror"]
    assert "STATUS" not in row["tg_text"]          # the body never carries status
    assert row["tg_text_ar"].startswith("*إشارة SKLZ رادار*")
    # the mirror used ITS bot, and no token was stored anywhere
    assert {s["token"] for s in tg.sent} == {"tok-main", "tok-mirror", "tok-ar"}
    assert "tok" not in str(row["tg_posts"])


# ── B. secured -> every post edited to TRAILING, no new post ─────────
def test_b_secured_edits_every_post_in_place(tg):
    sb = SB()
    _post(sb)
    n_sent = len(tg.sent)
    res = asyncio.run(SL._push_status(
        SL.StatusIn(symbol="XAUUSD", side="buy", status="secured", pips=8.0), sb))
    assert res["ok"] and res["edited"] == 4
    assert len(tg.sent) == n_sent                  # nothing new was posted
    ids_sent = sorted(s["id"] for s in tg.sent)
    assert sorted(e["id"] for e in tg.edits) == ids_sent
    en = [e for e in tg.edits if e["chat"] != "-100444"]
    ar = [e for e in tg.edits if e["chat"] == "-100444"]
    assert all("STATUS: TRAILING" in e["text"] and "+8.0 pips locked" in e["text"]
               for e in en)
    assert all("تتبع الوقف" in e["text"] and "+8.0 نقطة" in e["text"] for e in ar)
    # the body is still there under the new status
    assert all("Entry zone" in e["text"] for e in en)
    assert sb.db["signals"][0]["status"] == "secured"


# ── C. closed with a result: ✅ for a win, ❌ for a loss ──────────────
def test_c_closed_shows_result(tg):
    sb = SB()
    _post(sb)
    asyncio.run(SL._push_status(
        SL.StatusIn(symbol="XAUUSD", side="buy", status="closed", pips=-6.0), sb))
    en = [e for e in tg.edits if e["chat"] == "-100111"][-1]
    assert "❌ STATUS: CLOSED — -6.0 pips" in en["text"]
    assert sb.db["signals"][0]["outcome"] == "loss"

    tg.edits.clear()
    _post(sb)                                       # a second, winning trade
    asyncio.run(SL._push_status(
        SL.StatusIn(symbol="XAUUSD", side="buy", status="closed", pips=14.0), sb))
    en = [e for e in tg.edits if e["chat"] == "-100111"][-1]
    assert "✅ STATUS: CLOSED — +14.0 pips" in en["text"]
    assert en["id"] == sb.db["signals"][1]["tg_posts"][0]["message_id"]


def test_c_trailing_is_an_alias_for_secured(tg):
    sb = SB()
    _post(sb)
    res = asyncio.run(SL._push_status(
        SL.StatusIn(symbol="XAUUSD", side="buy", status="trailing"), sb))
    assert res["ok"] and sb.db["signals"][0]["status"] == "secured"
    assert all("TRAILING" in e["text"] or "تتبع" in e["text"] for e in tg.edits)


# ── D. before P14 runs, nothing breaks: the signal still goes out ─────
def test_d_missing_columns_are_tolerated(tg):
    sb = SB(fail_cols=("tg_posts",))
    out = _post(sb)
    assert out["ok"] and len(tg.sent) == 4
    res = asyncio.run(SL._push_status(
        SL.StatusIn(symbol="XAUUSD", side="buy", status="closed", pips=3.0), sb))
    assert res["ok"] and res["edited"] == 0
    assert sb.db["signals"][0]["status"] == "closed"


# ── E. status lines say only what the engine can prove ──────────────
def test_e_status_lines():
    assert SE.status_line("open") == "🟢 STATUS: OPEN"
    assert SE.status_line("secured") == "📈 STATUS: TRAILING — stop moved into profit"
    assert SE.status_line("closed") == "⚪ STATUS: CLOSED"   # no pips -> no claim
    assert SE.status_line("closed", 0.0).startswith("❌")
    for txt in (SE.status_line("closed", 5), SE.status_line("secured", 5)):
        assert "TP hit" not in txt and "SL hit" not in txt


# ── F. the daily numbers reach every signal channel, once each ───────
def test_f_summary_goes_to_signal_channels_once(monkeypatch):
    dests = SL.summary_destinations()
    chats = [d.chat_id for d in dests if d.enabled]
    # summary channel + metals + general + mirror (arabic is sent separately)
    assert set(chats) >= {"-100555", "-100111", "-100222", "-100333"}
    monkeypatch.setenv("SIGNAL_SUMMARY_TO_SIGNAL_CHANNELS", "0")
    chats = [d.chat_id for d in SL.summary_destinations() if d.enabled]
    assert chats == ["-100555"]


def test_f_summary_counts_trades_and_pips():
    sb = SB({"signals": [
        {"id": 1, "status": "closed", "outcome": "win", "result_pips": 12.0,
         "category": "metals", "received_at": "2999-01-01"},
        {"id": 2, "status": "closed", "outcome": "loss", "result_pips": -5.0,
         "category": "forex", "received_at": "2999-01-01"},
        {"id": 3, "status": "secured", "received_at": "2999-01-01"},
    ]})
    day = SL._summarise(sb, 1)
    assert (day["closed"], day["wins"], day["losses"]) == (2, 1, 1)
    assert day["net_pips"] == 7.0 and day["won_pips"] == 12.0 and day["lost_pips"] == -5.0
    assert day["by_category"]["metals"]["net_pips"] == 12.0
    text = SL.format_summary(day, day)
    assert "trades closed today: 2 — 1 won · 1 lost · 1 still running" in text
    assert "pips collected: *+7.0 pips*" in text and "· forex: 0W / 1L, -5.0 pips" in text
    assert "small sample" in text
    ar = AR.format_summary_ar(day, day)
    assert "صفقات أُغلقت اليوم: 2" in ar and "+7.0 نقطة" in ar
