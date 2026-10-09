"""Read-only access to the user's Binance account: the API key, signed requests and the key's permission check.

The key is used for reading only. Before any account call the backend asks Binance what the key may do
(`GET /sapi/v1/account/apiRestrictions`) and refuses to use a key that can trade, transfer or withdraw, or one
whose permissions it cannot read. The check is repeated every KEY_CHECK_TTL seconds, so a key whose permissions
are widened on Binance later stops working here too. The secret never leaves the server: the API answers with
the key's last four characters only.

Where the key lives: BINANCE_API_KEY / BINANCE_API_SECRET in the environment win; otherwise the key entered in the
app, kept in BINANCE_KEY_STORE (backend/.cache/binance_key.json) with file mode 600.

Signed requests follow Binance's SIGNED (TRADE/USER_DATA) rules: the query string (with `timestamp` in ms and
`recvWindow`) is signed with HMAC-SHA256 using the secret, the hex digest is appended as `signature`, and the
key goes in the X-MBX-APIKEY header. When Binance answers -1021 (timestamp outside recvWindow) the clock offset is
read from /api/v3/time and the request is retried once.

Endpoints used (all GET, all read-only; weights from Binance's docs):
  /sapi/v1/account/apiRestrictions       what the key may do                                   (weight 1)
  /api/v3/account?omitZeroBalances=true  spot balances                                         (20)
  /api/v3/myTrades                       spot fills of one symbol, paged by fromId              (20)
  /api/v3/allOrders                      spot orders of one symbol (clientOrderId), by orderId  (20)
  /sapi/v1/asset/wallet/balance          value of each wallet (Spot, Funding, Trading Bots...)  (60)
  /fapi/v1/userTrades                    USD-M futures fills of one symbol (7-day windows)      (5)
  /fapi/v1/allOrders                     USD-M futures orders of one symbol, by orderId          (5)
  /fapi/v1/income?incomeType=REALIZED_PNL which futures symbols had realized PnL                (30)
  /fapi/v2/positionRisk                  open USD-M positions                                   (5)
  /sapi/v1/simple-earn/flexible/position coins in Simple Earn Flexible, paged                    (150)
  /sapi/v1/simple-earn/locked/position   coins in Simple Earn Locked, paged                      (150)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from .config import Settings, get_settings

log = logging.getLogger(__name__)

RECV_WINDOW = 10_000
KEY_CHECK_TTL = 3600.0
KEY_RE = re.compile(r"^[A-Za-z0-9]{16,128}$")

# apiRestrictions flags that must be false for the app to use a key, with what each one allows.
FORBIDDEN = {
    "enableWithdrawals": "withdrawals",
    "enableInternalTransfer": "internal transfers",
    "permitsUniversalTransfer": "universal transfers between wallets",
    "enableSpotAndMarginTrading": "spot and margin trading",
    "enableMargin": "margin loans",
    "enableFutures": "futures trading",
    "enableVanillaOptions": "options trading",
    "enablePortfolioMarginTrading": "portfolio margin trading",
    "enableFixApiTrade": "FIX API trading",
}
# Flags that must be present (and false) for the answer to count as a permission report at all.
REQUIRED_FLAGS = ("enableReading", "enableWithdrawals", "enableSpotAndMarginTrading")


class BinanceKeyError(Exception):
    """No key, or a key the app will not use (it can trade or withdraw, or Binance rejected it)."""


class BinanceApiError(Exception):
    """Binance answered an account request with an error."""

    def __init__(self, message: str, status: int = 0, code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def sign_query(params: dict[str, Any], secret: str) -> str:
    """The query string with its HMAC-SHA256 signature appended, in Binance's SIGNED format:
    `a=1&b=2&signature=<hex of HMAC-SHA256(secret, "a=1&b=2")>`."""
    query = urlencode([(k, v) for k, v in params.items() if v is not None])
    sig = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    return f"{query}&signature={sig}"


def check_restrictions(payload: Any) -> list[str]:
    """What is wrong with a key, from its apiRestrictions answer (empty = read-only, fine to use). Each item reads
    after "This key ...", e.g. "allows withdrawals"."""
    if not isinstance(payload, dict) or any(k not in payload for k in REQUIRED_FLAGS):
        return ["has no permission report from Binance"]
    problems = [f"allows {what}" for flag, what in FORBIDDEN.items() if payload.get(flag) is True]
    if not payload.get("enableReading"):
        problems.append("does not have reading enabled")
    return problems


def write_private(path: Path, text: str) -> None:
    """Write a file only its owner can read (mode 600 from the moment it exists), atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o600)  # an older tmp file may have had wider permissions
    tmp.replace(path)


def mask(key: str) -> str:
    return f"••••{key[-4:]}" if key else ""


@dataclass
class ApiKey:
    key: str
    secret: str
    source: str  # "env" | "file"
    added_at: Optional[int] = None


class KeyStore:
    """The key from BINANCE_API_KEY/SECRET, or the one entered in the app (a JSON file with mode 600)."""

    def __init__(self, settings: Settings | None = None, path: str | None = None) -> None:
        s = settings or get_settings()
        self._env = ApiKey(s.binance_api_key, s.binance_api_secret, "env") if (
            s.binance_api_key and s.binance_api_secret) else None
        p = (path if path is not None else s.binance_key_store).strip()
        self.path = None if p.lower() in ("", "memory", "none", "off") else Path(p)
        self._memory: Optional[ApiKey] = None

    def load(self) -> Optional[ApiKey]:
        if self._env:
            return self._env
        if self.path is None:
            return self._memory
        try:
            if not self.path.exists():
                return None
            data = json.loads(self.path.read_text())
            return ApiKey(data["api_key"], data["api_secret"], "file", data.get("added_at"))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("Could not read the Binance key file %s: %s", self.path, exc)
            return None

    def save(self, key: str, secret: str) -> ApiKey:
        if self._env:
            raise BinanceKeyError("The key comes from BINANCE_API_KEY / BINANCE_API_SECRET on the server; change "
                                  "it there")
        api = ApiKey(key, secret, "file", int(time.time()))
        if self.path is None:
            self._memory = api
            return api
        # Created with mode 600 from the start, so the secret is never readable by others, not even briefly.
        write_private(self.path, json.dumps({"api_key": key, "api_secret": secret, "added_at": api.added_at}))
        return api

    def delete(self) -> bool:
        if self._env:
            raise BinanceKeyError("The key comes from BINANCE_API_KEY / BINANCE_API_SECRET on the server; remove "
                                  "it there")
        if self.path is None:
            had, self._memory = self._memory is not None, None
            return had
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return False


class BinanceAccount:
    """Signed, read-only Binance account calls with the permission check in front of every one."""

    def __init__(self, settings: Settings | None = None, store: KeyStore | None = None,
                 client: httpx.AsyncClient | None = None) -> None:
        self.s = settings or get_settings()
        self.store = store or KeyStore(self.s)
        self.spot_url = self.s.binance_rest_url
        self.futures_url = self.s.binance_futures_rest_url
        self._client = client
        self._offset_ms = 0
        self._check: Optional[dict] = None  # {"key": last4, "at": monotonic, "ok": bool, "problems": [...], ...}

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------ the key
    def status(self) -> dict:
        """What the settings section shows. Never contains the secret or the full key."""
        api = self.store.load()
        out: dict = {"configured": api is not None, "source": api.source if api else None,
                     "key_last4": api.key[-4:] if api else None, "masked": mask(api.key) if api else None,
                     "added_at": api.added_at if api else None, "checked_at": None, "ok": None, "problems": [],
                     "permissions": None, "error": None}
        chk = self._check
        if api and chk and chk["key"] == api.key[-4:]:
            out.update(checked_at=chk["checked_at"], ok=chk["ok"], problems=chk["problems"],
                       permissions=chk["permissions"], error=chk.get("error"))
        return out

    async def set_key(self, key: str, secret: str) -> dict:
        """Check a new key with Binance and save it only when it is read-only."""
        key, secret = key.strip(), secret.strip()
        if not KEY_RE.match(key) or not KEY_RE.match(secret):
            raise BinanceKeyError("That does not look like a Binance API key and secret (letters and digits only)")
        chk = await self._permissions(ApiKey(key, secret, "new"))
        if not chk["ok"]:
            raise BinanceKeyError(_refusal(chk))
        self.store.save(key, secret)
        self._check = chk
        return self.status()

    async def remove_key(self) -> dict:
        self.store.delete()
        self._check = None
        return self.status()

    async def test(self) -> dict:
        """Ask Binance again what the saved key may do."""
        api = self.store.load()
        if api is None:
            raise BinanceKeyError("No Binance API key saved")
        self._check = await self._permissions(api)
        return self.status()

    async def ensure_safe(self) -> ApiKey:
        """The key, after making sure (at most KEY_CHECK_TTL old) that it is read-only. Raises BinanceKeyError."""
        api = self.store.load()
        if api is None:
            raise BinanceKeyError("No Binance API key saved; add a read-only key first")
        chk = self._check
        if chk is None or chk["key"] != api.key[-4:] or time.monotonic() - chk["at"] > KEY_CHECK_TTL \
                or chk.get("error"):
            chk = self._check = await self._permissions(api)
        if not chk["ok"]:
            raise BinanceKeyError(_refusal(chk))
        return api

    async def _permissions(self, api: ApiKey) -> dict:
        chk: dict = {"key": api.key[-4:], "at": time.monotonic(), "checked_at": int(time.time()), "ok": False,
                     "problems": [], "permissions": None}
        try:
            payload = await self._signed(api, self.spot_url, "/sapi/v1/account/apiRestrictions", {})
        except BinanceApiError as exc:
            chk["problems"] = [f"was rejected by Binance: {exc}"] if exc.status in (400, 401) else []
            chk["error"] = str(exc)
            return chk
        except httpx.HTTPError as exc:
            chk["error"] = f"Could not reach Binance: {exc}"
            return chk
        chk["permissions"] = {k: payload.get(k) for k in ("enableReading", *FORBIDDEN, "ipRestrict")
                              if isinstance(payload, dict) and k in payload}
        chk["problems"] = check_restrictions(payload)
        chk["ok"] = not chk["problems"]
        return chk

    # ------------------------------------------------------ signed calls
    async def get(self, base: str, path: str, params: dict[str, Any] | None = None) -> Any:
        """A signed GET with the key checked first. Raises BinanceKeyError, BinanceApiError or httpx.HTTPError."""
        api = await self.ensure_safe()
        return await self._signed(api, base, path, params or {})

    async def _signed(self, api: ApiKey, base: str, path: str, params: dict[str, Any], retry: bool = True) -> Any:
        q = dict(params)
        q["recvWindow"] = RECV_WINDOW
        q["timestamp"] = int(time.time() * 1000) + self._offset_ms
        url = f"{base}{path}?{sign_query(q, api.secret)}"
        resp = await self.client.get(url, headers={"X-MBX-APIKEY": api.key})
        if resp.status_code < 400:
            return resp.json()
        code, msg = None, resp.text[:300]
        try:
            body = resp.json()
            code, msg = body.get("code"), body.get("msg", msg)
        except ValueError:
            pass
        if code == -1021 and retry:  # our clock is off: read Binance's and try again once
            await self._sync_time()
            return await self._signed(api, base, path, params, retry=False)
        if resp.status_code in (403, 451):
            msg = f"Binance is not available from this server's region (HTTP {resp.status_code})"
        raise BinanceApiError(f"{msg} (HTTP {resp.status_code}{f', code {code}' if code is not None else ''})",
                              resp.status_code, code)

    async def _sync_time(self) -> None:
        try:
            resp = await self.client.get(f"{self.spot_url}/api/v3/time")
            resp.raise_for_status()
            self._offset_ms = int(resp.json()["serverTime"]) - int(time.time() * 1000)
            log.info("Binance clock offset %d ms", self._offset_ms)
        except Exception as exc:  # keep the old offset; the retry reports the error
            log.warning("Could not read Binance's time: %s", exc)

    # --------------------------------------------------------- endpoints
    async def spot_balances(self) -> dict[str, float]:
        """Spot wallet balances (free + locked) by asset, zero balances left out."""
        data = await self.get(self.spot_url, "/api/v3/account", {"omitZeroBalances": "true"})
        out: dict[str, float] = {}
        for b in data.get("balances", []):
            total = float(b.get("free", 0)) + float(b.get("locked", 0))
            if total > 0:
                out[b["asset"]] = total
        return out

    async def wallet_balances(self) -> list[dict]:
        """The value of each wallet in USDT, e.g. [{"wallet": "Trading Bots", "usdt": 512.3, "active": True}]."""
        data = await self.get(self.spot_url, "/sapi/v1/asset/wallet/balance", {"quoteAsset": "USDT"})
        return [{"wallet": w.get("walletName", ""), "usdt": float(w.get("balance", 0)),
                 "active": bool(w.get("activate", True))} for w in data if isinstance(w, dict)]

    async def my_trades(self, symbol: str, from_id: int, limit: int = 1000) -> list[dict]:
        return await self.get(self.spot_url, "/api/v3/myTrades", {"symbol": symbol, "fromId": from_id,
                                                                  "limit": limit})

    async def all_orders(self, symbol: str, order_id: int, limit: int = 1000) -> list[dict]:
        return await self.get(self.spot_url, "/api/v3/allOrders", {"symbol": symbol, "orderId": order_id,
                                                                   "limit": limit})

    async def futures_trades(self, symbol: str, *, from_id: Optional[int] = None, start_ms: Optional[int] = None,
                             end_ms: Optional[int] = None, limit: int = 1000) -> list[dict]:
        return await self.get(self.futures_url, "/fapi/v1/userTrades", {
            "symbol": symbol, "fromId": from_id, "startTime": start_ms, "endTime": end_ms, "limit": limit})

    async def futures_orders(self, symbol: str, order_id: int, limit: int = 1000) -> list[dict]:
        return await self.get(self.futures_url, "/fapi/v1/allOrders", {"symbol": symbol, "orderId": order_id,
                                                                       "limit": limit})

    async def futures_pnl_symbols(self, start_ms: int) -> set[str]:
        rows = await self.get(self.futures_url, "/fapi/v1/income", {"incomeType": "REALIZED_PNL",
                                                                    "startTime": start_ms, "limit": 1000})
        return {r["symbol"] for r in rows if isinstance(r, dict) and r.get("symbol")}

    async def futures_positions(self) -> list[dict]:
        """Open USD-M positions (positionAmt != 0)."""
        rows = await self.get(self.futures_url, "/fapi/v2/positionRisk", {})
        return [r for r in rows if isinstance(r, dict) and float(r.get("positionAmt", 0) or 0) != 0]

    async def earn_positions(self) -> list[dict]:
        """Coins in Simple Earn, flexible and locked: [{"product", "asset", "qty", "apr_pct", ...}]. Binance pages
        these EARN_PAGE rows at a time; at most EARN_PAGES pages of each are read."""
        out: list[dict] = []
        for product, path in (("flexible", "/sapi/v1/simple-earn/flexible/position"),
                              ("locked", "/sapi/v1/simple-earn/locked/position")):
            for page in range(1, EARN_PAGES + 1):
                data = await self.get(self.spot_url, path, {"current": page, "size": EARN_PAGE})
                rows = data.get("rows", []) if isinstance(data, dict) else []
                out += [r for r in (parse_earn(product, r) for r in rows) if r]
                if len(rows) < EARN_PAGE:
                    break
        return out


EARN_PAGE = 100
EARN_PAGES = 5


def _num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_earn(product: str, r: Any) -> Optional[dict]:
    """One Simple Earn position in the app's shape. Flexible rows carry totalAmount and latestAnnualPercentageRate
    (a fraction: 0.05 = 5%); locked rows amount, APY (also a fraction), duration in days and redeemDate (ms)."""
    if not isinstance(r, dict) or not r.get("asset"):
        return None
    qty = _num(r.get("totalAmount") if product == "flexible" else r.get("amount"))
    if not qty or qty <= 0:
        return None
    apr = _num(r.get("latestAnnualPercentageRate") if product == "flexible" else r.get("APY"))
    out = {"product": product, "asset": str(r["asset"]).upper(), "qty": qty,
           "apr_pct": round(apr * 100, 2) if apr is not None else None}
    if product == "flexible":
        out["rewards_total"] = _num(r.get("cumulativeTotalRewards"))
        out["can_redeem"] = bool(r.get("canRedeem", True))
    else:
        out["duration_days"] = int(_num(r.get("duration")) or 0) or None
        redeem = _num(r.get("redeemDate"))
        out["redeem_at"] = int(redeem // 1000) if redeem else None
        out["auto_renew"] = bool(r.get("isAutoRenew", False))
        out["position_id"] = r.get("positionId")
    return out


def _refusal(chk: dict) -> str:
    if chk.get("problems"):
        return ("This key " + "; ".join(chk["problems"]) + ". The app only uses read-only keys: create one on "
                "Binance with only \"Enable Reading\" ticked (no trading, no withdrawals, no transfers).")
    return chk.get("error") or "Could not check the key's permissions with Binance"
