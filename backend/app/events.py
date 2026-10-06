"""Economic calendar and crypto news, for the events panel, the event lines on the chart and the chat agent.

* Calendar: the free Forex Factory JSON feeds (this week, and next week once it is published), cached for an hour.
  Only the countries in CALENDAR_COUNTRIES are kept (USD by default: CPI, FOMC, payrolls move crypto most).
* News: RSS/Atom feeds (CoinDesk and Cointelegraph by default), parsed with the standard library and cached for
  five minutes. Each headline is tagged with the coins it is about, from a small map of the top coins. Short or
  ambiguous tickers ("NEAR", "ONE", "OP", "LINK") only count as "$NEAR", "(NEAR)" or the full name, so ordinary
  words do not tag a coin.

Nothing here is ever made up: when a feed cannot be fetched the endpoint says `source: "unavailable"` and returns
an empty list, or keeps serving the last good copy as `source: "stale"`.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import httpx

from .config import Settings, get_settings

log = logging.getLogger(__name__)

CALENDAR_TTL, CALENDAR_RETRY = 3600.0, 300.0  # Forex Factory throttles clients that poll often
NEWS_TTL, NEWS_RETRY = 300.0, 120.0
MAX_NEWS = 200
MAX_FEED_BYTES = 4_000_000
IMPACT_LEVELS: dict[str, tuple[str, ...] | None] = {"high": ("High",), "medium": ("High", "Medium"), "all": None}
FEED_NAMES = {"coindesk.com": "CoinDesk", "cointelegraph.com": "Cointelegraph", "decrypt.co": "Decrypt",
              "theblock.co": "The Block", "bitcoinmagazine.com": "Bitcoin Magazine"}
QUOTES = ("USDT", "USDC", "FDUSD", "BUSD", "TUSD", "USD")

# Top coins: ticker → (full-name patterns matched case-insensitively, names matched case-sensitively).
COINS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "BTC": ((r"bitcoin(?!\s+(?:cash|sv)\b)",), ()),
    "ETH": ((r"ethereum(?!\s+classic)", r"ether"), ()),
    "SOL": (("solana",), ()),
    "BNB": (("bnb chain", "binance coin"), ()),
    "XRP": (("ripple",), ()),
    "DOGE": (("dogecoin",), ()),
    "ADA": (("cardano",), ()),
    "TRX": (("tron",), ()),
    "AVAX": (("avalanche network", "avalanche blockchain", "ava labs"), ()),
    "LINK": (("chainlink",), ()),
    "DOT": (("polkadot",), ()),
    "TON": (("toncoin", "the open network"), ()),
    "SUI": (("sui network", "sui blockchain", "mysten labs"), ("Sui",)),
    "SHIB": (("shiba inu",), ()),
    "LTC": (("litecoin",), ()),
    "BCH": (("bitcoin cash",), ()),
    "UNI": (("uniswap",), ()),
    "NEAR": (("near protocol",), ()),
    "APT": (("aptos",), ()),
    "ARB": (("arbitrum",), ()),
    "OP": (("op mainnet", "optimism superchain"), ()),
    "INJ": (("injective",), ()),
    "TIA": (("celestia",), ()),
    "SEI": (("sei network",), ("Sei",)),
    "PEPE": (("pepe",), ()),
    "ATOM": (("cosmos hub", "cosmos network"), ()),
    "FIL": (("filecoin",), ()),
    "ICP": (("internet computer",), ()),
    "HBAR": (("hedera",), ()),
    "XLM": (("stellar lumens", "stellar network", "stellar development foundation"), ()),
    "ETC": (("ethereum classic",), ()),
    "TAO": (("bittensor",), ()),
    "WLD": (("worldcoin",), ()),
    "ENA": (("ethena",), ()),
    "AAVE": (("aave",), ()),
    "HYPE": (("hyperliquid",), ()),
    "ONE": (("harmony one",), ()),
}
# Tickers that are also English words or common abbreviations: only "$X", "(X)" or the name count.
AMBIGUOUS = {"LINK", "DOT", "NEAR", "OP", "ONE", "UNI", "APT", "ATOM", "ETC", "TIA", "SEI", "TAO", "HYPE"}


def _coin_regex(ticker: str, names: tuple[str, ...] = (), cs_names: tuple[str, ...] = ()) -> re.Pattern[str]:
    t = re.escape(ticker)
    parts = [rf"(?i:\${t})(?!\w)", rf"\({t}\)"]
    if ticker not in AMBIGUOUS and len(ticker) >= 3:
        parts.append(rf"(?<![\w$]){t}(?!\w)")  # the ticker in capitals
    parts += [rf"(?i:\b{n}\b)" for n in names]
    parts += [rf"\b{n}\b" for n in cs_names]
    return re.compile("|".join(parts))


COIN_PATTERNS = {tk: _coin_regex(tk, *spec) for tk, spec in COINS.items()}


def match_coins(text: str) -> list[str]:
    """Tickers of the known coins `text` is about."""
    return [tk for tk, rx in COIN_PATTERNS.items() if rx.search(text)]


def symbol_base(symbol: str) -> str:
    s = symbol.upper().replace("/", "").replace("-", "")
    for q in QUOTES:
        if s.endswith(q) and len(s) > len(q):
            return s[: -len(q)]
    return s


def coin_pattern(base: str) -> re.Pattern[str]:
    """The matcher for one coin: the map's entry, or just "$X" / "(X)" / X-in-capitals for coins not in it."""
    return COIN_PATTERNS.get(base) or _coin_regex(base)


# ------------------------------------------------------------------------------------------------ parsing


def parse_calendar(rows: list, countries: set[str] | None) -> list[dict]:
    """Forex Factory rows → [{time, title, country, impact, forecast, previous}] sorted by time, de-duplicated."""
    seen, out = set(), []
    for r in rows:
        if not isinstance(r, dict) or not r.get("title") or not r.get("date"):
            continue
        try:
            dt = datetime.fromisoformat(str(r["date"]).replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        country = str(r.get("country") or "").upper()
        if countries and country not in countries:
            continue
        ev = {"time": int(dt.timestamp()), "title": str(r["title"]).strip(), "country": country,
              "impact": str(r.get("impact") or "").strip().title() or "Low",
              "forecast": str(r.get("forecast") or "").strip() or None,
              "previous": str(r.get("previous") or "").strip() or None}
        key = (ev["time"], ev["title"], country)
        if key not in seen:
            seen.add(key)
            out.append(ev)
    return sorted(out, key=lambda e: e["time"])


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _text(el: ET.Element | None) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


def _clean(text: str, limit: int | None = None) -> str:
    text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text))).strip()
    if limit and len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0] + "…"
    return text


def _parse_time(value: str) -> int | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def feed_name(url: str, channel_title: str = "") -> str:
    host = (urlparse(url).hostname or "").removeprefix("www.")
    for domain, name in FEED_NAMES.items():
        if host == domain or host.endswith("." + domain):
            return name
    return _clean(channel_title, 30) or host or "News"


def parse_feed(data: bytes, url: str) -> list[dict]:
    """RSS 2.0 or Atom → [{time, title, url, source, coins, summary}]. Items without a date or a web link are
    skipped. Documents with a DTD are refused: feeds never need one, and entity tricks live there."""
    head = data[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in data.lower():
        raise ValueError("feed declares a DTD")
    root = ET.fromstring(data)
    channel_title = ""
    for el in root.iter():
        if _local(el.tag) == "title":
            channel_title = _text(el)
            break
    source = feed_name(url, channel_title)
    out = []
    for item in root.iter():
        if _local(item.tag) not in ("item", "entry"):
            continue
        # First text of each child tag. Texts, not elements: an Element's truth value is unreliable.
        fields: dict[str, str] = {}
        categories: list[str] = []
        for child in item:
            name = _local(child.tag)
            if name == "category":
                categories.append(_text(child) or child.get("term", ""))
            elif name == "link" and child.get("href"):
                if child.get("rel", "alternate") == "alternate":  # Atom: only the web page link
                    fields.setdefault("link", child.get("href", ""))
            elif name == "guid" and child.get("isPermaLink", "true") != "true":
                continue
            else:
                fields.setdefault(name, _text(child))
        title = _clean(fields.get("title", ""))
        link = fields.get("link") or fields.get("guid", "")
        t = _parse_time(next((fields[k] for k in ("pubDate", "published", "updated", "date") if fields.get(k)), ""))
        if not title or t is None or not link.startswith(("https://", "http://")):
            continue
        summary = _clean(fields.get("description") or fields.get("summary", ""), 220)
        coins = match_coins(" ".join((title, summary, " ".join(categories))))
        out.append({"time": t, "title": title, "url": link, "source": source, "coins": coins, "summary": summary})
    return out


# ------------------------------------------------------------------------------------------------ service


class EventsService:
    """Cached calendar and news. Public methods never raise for network trouble."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.s = settings or get_settings()
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=True,
                                         headers={"User-Agent": "agentic-charts/1.0 (+news reader)",
                                                  "Accept": "application/json, application/rss+xml, application/xml, "
                                                            "text/xml;q=0.9, */*;q=0.5"})
        self._cal: list[dict] = []
        self._cal_state = ("unavailable", None, 0.0)  # (source, updated_at UNIX s, expires monotonic)
        self._news: list[dict] = []
        self._feeds: list[dict] = []
        self._news_state = ("unavailable", None, 0.0)
        self._cal_lock = asyncio.Lock()
        self._news_lock = asyncio.Lock()

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def countries(self) -> set[str] | None:
        c = {x.upper() for x in self.s.calendar_countries}
        return None if not c or {"ALL", "*"} & c else c

    async def _fetch(self, url: str) -> bytes:
        async with self._client.stream("GET", url) as r:
            r.raise_for_status()
            buf = bytearray()
            async for chunk in r.aiter_bytes():
                buf += chunk
                if len(buf) > MAX_FEED_BYTES:
                    raise ValueError(f"{url} is larger than {MAX_FEED_BYTES} bytes")
            return bytes(buf)

    # ---------------------------------------------------------------- calendar
    async def _calendar(self) -> tuple[list[dict], str, int | None]:
        async with self._cal_lock:
            state, updated, expires = self._cal_state
            if expires > time.monotonic():
                return self._cal, state, updated
            results = await asyncio.gather(*(self._fetch(u) for u in self.s.calendar_urls), return_exceptions=True)
            rows: list = []
            primary_ok = False
            for i, (url, res) in enumerate(zip(self.s.calendar_urls, results)):
                try:
                    if isinstance(res, BaseException):
                        raise res
                    data = json.loads(res)
                    if not isinstance(data, list):
                        raise ValueError("not a list of events")
                    rows += data
                    primary_ok = primary_ok or i == 0
                except Exception as exc:  # next week's feed is often not published yet
                    log.info("Calendar feed %s unavailable: %s", url, exc)
            # The first feed (this week) decides: without it a fresh copy would lose this week's events, so the
            # last good copy is kept instead. A missing next-week feed only means fewer events until it appears.
            if primary_ok:
                self._cal = parse_calendar(rows, self.countries)
                self._cal_state = ("live", int(time.time()), time.monotonic() + CALENDAR_TTL)
            else:
                self._cal_state = ("stale" if self._cal else "unavailable", updated,
                                   time.monotonic() + CALENDAR_RETRY)
            return self._cal, self._cal_state[0], self._cal_state[1]

    async def calendar(self, days: int = 7, impact: str = "high", past_days: int = 0) -> dict:
        """{events: [{time, title, country, impact, forecast, previous}], source, countries, updated_at, note?}
        for events from `past_days` ago to `days` ahead. impact: high | medium (and high) | all."""
        events, source, updated = await self._calendar()
        now = time.time()
        allowed = IMPACT_LEVELS.get(impact, IMPACT_LEVELS["high"])
        picked = [e for e in events if now - past_days * 86400 <= e["time"] <= now + days * 86400
                  and (allowed is None or e["impact"] in allowed)]
        out: dict = {"events": picked, "source": source, "updated_at": updated,
                     "countries": sorted(self.countries) if self.countries else ["ALL"]}
        if source == "unavailable":
            out["note"] = "The economic calendar (Forex Factory) could not be reached; retrying in a few minutes"
        elif source == "stale":
            out["note"] = "Showing the last calendar fetched; the feed did not answer just now"
        return out

    async def upcoming_events(self, hours: int = 24, impact: str = "high") -> list[dict]:
        """Economic events in the next `hours` (for the chat agent and the morning brief), oldest first:
        [{time, title, country, impact, forecast, previous}]. Empty when the calendar is unreachable."""
        events, _, _ = await self._calendar()
        now = time.time()
        allowed = IMPACT_LEVELS.get(impact, IMPACT_LEVELS["high"])
        return [dict(e) for e in events if now <= e["time"] <= now + hours * 3600
                and (allowed is None or e["impact"] in allowed)]

    # ---------------------------------------------------------------- news
    async def _all_news(self) -> tuple[list[dict], str, int | None]:
        async with self._news_lock:
            state, updated, expires = self._news_state
            if expires > time.monotonic():
                return self._news, state, updated
            results = await asyncio.gather(*(self._fetch(u) for u in self.s.news_feeds), return_exceptions=True)
            items: list[dict] = []
            feeds = []
            for url, res in zip(self.s.news_feeds, results):
                try:
                    if isinstance(res, BaseException):
                        raise res
                    got = await asyncio.to_thread(parse_feed, res, url)
                    items += got
                    feeds.append({"name": feed_name(url), "ok": True, "items": len(got)})
                except Exception as exc:
                    log.info("News feed %s unavailable: %s", url, exc)
                    feeds.append({"name": feed_name(url), "ok": False, "items": 0})
            self._feeds = feeds
            if any(f["ok"] for f in feeds):
                seen: set[str] = set()
                merged = []
                for it in sorted(items, key=lambda i: -i["time"]):
                    key = it["url"].split("?")[0].rstrip("/")
                    if key not in seen and it["title"].lower() not in seen:
                        seen.update((key, it["title"].lower()))
                        merged.append(it)
                self._news = merged[:MAX_NEWS]
                self._news_state = ("live", int(time.time()), time.monotonic() + NEWS_TTL)
            else:
                self._news_state = ("stale" if self._news else "unavailable", updated, time.monotonic() + NEWS_RETRY)
            return self._news, self._news_state[0], self._news_state[1]

    async def news(self, symbol: str | None = None, limit: int = 30) -> dict:
        """{items: [{time, title, url, source, coins, summary}], source, feeds, updated_at, note?}, newest first.
        With `symbol` (e.g. BTCUSDT), only items about that coin."""
        items, source, updated = await self._all_news()
        if symbol:
            base = symbol_base(symbol)
            rx = coin_pattern(base)
            items = [i for i in items if base in i["coins"] or rx.search(f"{i['title']} {i['summary']}")]
        out: dict = {"items": items[: max(1, min(limit, MAX_NEWS))], "source": source, "updated_at": updated,
                     "feeds": self._feeds}
        if source == "unavailable":
            out["note"] = "The news feeds could not be reached; retrying shortly"
        elif source == "stale":
            out["note"] = "Showing the last headlines fetched; the feeds did not answer just now"
        return out

    async def headlines(self, symbol: str | None = None, hours: int = 24, limit: int = 8) -> list[dict]:
        """Recent headlines for the chat agent: [{time, title, source, url}], newest first, optionally only about
        one coin. Empty when no feed is reachable."""
        res = await self.news(symbol, MAX_NEWS)
        cutoff = time.time() - hours * 3600
        return [{k: i[k] for k in ("time", "title", "source", "url")} for i in res["items"] if i["time"] >= cutoff][
            :limit]
