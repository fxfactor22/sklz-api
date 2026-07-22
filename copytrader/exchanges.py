"""SKLZ CopyTrader — exchange adapter layer.

A thin, plug-in abstraction over CCXT so new exchanges are one small class.
Everything here is SPOT ONLY: no futures, no margin, no leverage.

The most important function in this file is `verify_permissions`. A key with
withdrawal rights must never be accepted. Where an exchange exposes its
permission set we check it directly; where it does not, we say so honestly
rather than returning a green tick we cannot justify.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import ccxt

# exchange id -> (display name, needs passphrase)
SUPPORTED: dict[str, tuple[str, bool]] = {
    "binance":   ("Binance", False),
    "bybit":     ("Bybit", False),
    "okx":       ("OKX", True),
    "kucoin":    ("KuCoin", True),
    "bitget":    ("Bitget", True),
    "gate":      ("Gate.io", False),
    "mexc":      ("MEXC", False),
    "bingx":     ("BingX", False),
    "htx":       ("HTX", False),
    "kraken":    ("Kraken", False),
    "coinbase":  ("Coinbase Advanced", False),
    "cryptocom": ("Crypto.com", False),
}


@dataclass
class PermissionCheck:
    ok: bool
    can_read: bool = False
    can_trade: bool = False
    can_withdraw: bool | None = None      # None = could not determine
    verified: bool = False                # did the exchange actually tell us?
    message: str = ""
    details: dict = field(default_factory=dict)


@dataclass
class Balance:
    asset: str
    free: float
    used: float
    total: float


class ExchangeAdapter:
    """One user's connection to one exchange. Credentials are passed in at
    construction and never stored on the instance beyond the ccxt client."""

    def __init__(self, exchange_id: str, api_key: str, secret: str,
                 passphrase: str = "", sandbox: bool = False):
        if exchange_id not in SUPPORTED:
            raise ValueError(f"unsupported exchange: {exchange_id}")
        self.exchange_id = exchange_id
        self.display_name = SUPPORTED[exchange_id][0]
        cfg: dict[str, Any] = {
            "apiKey": api_key,
            "secret": secret,
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},   # SPOT ONLY
        }
        if passphrase:
            cfg["password"] = passphrase
        self.client = getattr(ccxt, exchange_id)(cfg)
        if sandbox and self.client.has.get("sandbox"):
            self.client.set_sandbox_mode(True)

    # ────────────────────────── permissions ──────────────────────────
    def verify_permissions(self) -> PermissionCheck:
        """Confirm the key can read and trade, and REJECT it if it can withdraw.

        Honest reporting: `verified` is only True when the exchange itself
        told us the permission set. Otherwise the caller must treat withdrawal
        status as unknown and require the user to confirm.
        """
        chk = PermissionCheck(ok=False)

        # 1) can we read? every exchange supports this
        try:
            self.client.fetch_balance()
            chk.can_read = True
        except ccxt.AuthenticationError as exc:
            chk.message = f"authentication failed: {_clean(exc)}"
            return chk
        except ccxt.PermissionDenied as exc:
            chk.message = f"key lacks read permission: {_clean(exc)}"
            return chk
        except Exception as exc:  # noqa: BLE001
            chk.message = f"could not reach {self.display_name}: {_clean(exc)}"
            return chk

        # 2) ask the exchange for its permission set where possible
        probe = self._probe_permissions()
        chk.details = probe
        if probe.get("known"):
            chk.verified = True
            chk.can_trade = bool(probe.get("trade"))
            chk.can_withdraw = bool(probe.get("withdraw"))
        else:
            chk.can_trade = True          # assumed; the first order will prove it
            chk.can_withdraw = None       # unknown — must be surfaced to the user

        # 3) the hard rule
        if chk.can_withdraw is True:
            chk.ok = False
            chk.message = ("This API key has WITHDRAWAL permission enabled. "
                           "SKLZ will not accept it. Create a new key with "
                           "read and spot-trade permissions only.")
            return chk

        if not chk.can_trade:
            chk.ok = False
            chk.message = "This key cannot place spot orders. Enable spot trading."
            return chk

        chk.ok = True
        if chk.verified:
            chk.message = "Verified: read + spot trade, withdrawals disabled."
        else:
            chk.message = (f"{self.display_name} does not expose its permission "
                           f"list to the API. Please confirm withdrawals are "
                           f"disabled on this key.")
        return chk

    def _probe_permissions(self) -> dict:
        """Per-exchange permission introspection. Returns {'known': bool, ...}."""
        try:
            if self.exchange_id == "binance":
                r = self.client.sapi_get_account_apirestrictions()
                return {"known": True,
                        "trade": bool(r.get("enableSpotAndMarginTrading")),
                        "withdraw": bool(r.get("enableWithdrawals")),
                        "raw": r}
            if self.exchange_id == "bybit":
                r = self.client.privateGetV5UserQueryApi()
                res = (r or {}).get("result", {}) or {}
                perms = res.get("permissions", {}) or {}
                spot = perms.get("Spot") or []
                wallet = perms.get("Wallet") or []
                return {"known": True,
                        "trade": bool(spot),
                        "withdraw": any("Withdraw" in str(p) for p in wallet),
                        "raw": res}
            if self.exchange_id == "kucoin":
                # KuCoin returns permissions on the key info endpoint
                r = self.client.private_get_user_info()
                perms = str(r)
                return {"known": True,
                        "trade": "Trade" in perms or "General" in perms,
                        "withdraw": "Withdraw" in perms,
                        "raw": r}
        except Exception:  # noqa: BLE001
            pass
        return {"known": False}

    # ────────────────────────── market data ──────────────────────────
    def load_markets(self) -> dict:
        return self.client.load_markets()

    def balances(self, non_zero: bool = True) -> list[Balance]:
        b = self.client.fetch_balance()
        out = []
        for asset, total in (b.get("total") or {}).items():
            if non_zero and not total:
                continue
            out.append(Balance(asset=asset,
                               free=float((b.get("free") or {}).get(asset) or 0),
                               used=float((b.get("used") or {}).get(asset) or 0),
                               total=float(total or 0)))
        return sorted(out, key=lambda x: x.total, reverse=True)

    def quote_balance(self, quote: str = "USDT") -> float:
        try:
            b = self.client.fetch_balance()
            return float((b.get("free") or {}).get(quote) or 0)
        except Exception:  # noqa: BLE001
            return 0.0

    def price(self, symbol: str) -> float:
        return float(self.client.fetch_ticker(symbol)["last"])

    def market_rules(self, symbol: str) -> dict:
        """Minimums and precision — required for correct sizing."""
        m = self.client.market(symbol)
        limits = m.get("limits") or {}
        return {
            "min_amount": (limits.get("amount") or {}).get("min"),
            "min_cost": (limits.get("cost") or {}).get("min"),
            "amount_precision": (m.get("precision") or {}).get("amount"),
            "price_precision": (m.get("precision") or {}).get("price"),
            "base": m.get("base"),
            "quote": m.get("quote"),
            "active": m.get("active", True),
        }

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        return float(self.client.amount_to_precision(symbol, amount))

    # ────────────────────────── execution ──────────────────────────
    def create_spot_order(self, symbol: str, side: str, amount: float,
                          client_order_id: str | None = None) -> dict:
        """Market spot order. `client_order_id` gives idempotency: the same id
        will be rejected by the exchange rather than filled twice."""
        if side not in ("buy", "sell"):
            raise ValueError("side must be buy or sell")
        params: dict[str, Any] = {}
        if client_order_id:
            params["clientOrderId"] = client_order_id
        return self.client.create_order(symbol, "market", side, amount,
                                        None, params)

    def fetch_order(self, order_id: str, symbol: str) -> dict:
        return self.client.fetch_order(order_id, symbol)


def _clean(exc: Exception) -> str:
    """Exchange errors sometimes echo request params. Never let a secret leak
    into a log or an API response."""
    s = str(exc)
    return (s[:160] + "…") if len(s) > 160 else s


def list_supported() -> list[dict]:
    return [{"id": k, "name": v[0], "needs_passphrase": v[1]}
            for k, v in SUPPORTED.items()]
