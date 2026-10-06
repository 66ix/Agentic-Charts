"""Economic calendar and news: parsing, coin tagging, caching and graceful failure (network mocked)."""

import asyncio
import json
import time
from datetime import datetime, timezone

import httpx

from app.config import Settings
from app.events import EventsService, match_coins, parse_calendar, parse_feed, symbol_base

CAL_THIS = "https://feeds.test/thisweek.json"
CAL_NEXT = "https://feeds.test/nextweek.json"
FEED_A = "https://www.coindesk.com/arc/outboundfeeds/rss/"
FEED_B = "https://cointelegraph.com/rss"


def _settings(**kw) -> Settings:
    return Settings(**{"calendar_urls": (CAL_THIS, CAL_NEXT), "calendar_countries": ("USD",),
                       "news_feeds": (FEED_A, FEED_B), **kw})


def _service(handler, **kw) -> EventsService:
    svc = EventsService(_settings(**kw))
    svc._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return svc


def _run(svc, fn):
    async def go():
        try:
            return await fn()
        finally:
            await svc.close()
    return asyncio.run(go())


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat()


def _cal_row(title, ts, country="USD", impact="High", forecast="3.1%", previous="3.0%"):
    return {"title": title, "country": country, "date": _iso(ts), "impact": impact, "forecast": forecast,
            "previous": previous}


# ------------------------------------------------------------------------------------------ parsing


def test_parse_calendar_filters_countries_and_sorts():
    now = time.time()
    rows = [_cal_row("CPI y/y", now + 7200), _cal_row("Bank Holiday", now + 3600, impact="Holiday", forecast=""),
            _cal_row("ECB Press Conference", now + 1800, country="EUR"), {"title": "", "date": _iso(now)},
            {"title": "No date"}, _cal_row("CPI y/y", now + 7200)]
    events = parse_calendar(rows, {"USD"})
    assert [e["title"] for e in events] == ["Bank Holiday", "CPI y/y"]  # by time, EUR dropped, duplicate dropped
    assert events[0]["impact"] == "Holiday" and events[0]["forecast"] is None
    assert events[1] == {"time": int(now + 7200), "title": "CPI y/y", "country": "USD", "impact": "High",
                         "forecast": "3.1%", "previous": "3.0%"}
    assert len(parse_calendar(rows, None)) == 3  # every country


def test_parse_calendar_keeps_the_feeds_offset():
    # Forex Factory dates carry an offset; the UNIX time must respect it.
    [ev] = parse_calendar([{"title": "FOMC", "country": "USD", "date": "2026-03-18T14:00:00-04:00",
                            "impact": "High"}], {"USD"})
    assert ev["time"] == int(datetime(2026, 3, 18, 18, 0, tzinfo=timezone.utc).timestamp())


RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>CoinDesk</title>
  <item><title>Bitcoin ETF inflows hit a record</title>
    <link>https://www.coindesk.com/markets/btc-etf</link>
    <description>&lt;p&gt;Spot BTC funds took in $1B as ether lagged.&lt;/p&gt;</description>
    <pubDate>Mon, 06 Oct 2025 08:30:00 +0000</pubDate><category>Markets</category></item>
  <item><title>One thing is near certain about the next rate cut</title>
    <link>https://www.coindesk.com/policy/rates</link>
    <description>Traders are not sure. The link between policy and crypto is one to watch.</description>
    <pubDate>Mon, 06 Oct 2025 07:00:00 +0000</pubDate></item>
  <item><title>No date here</title><link>https://www.coindesk.com/x</link></item>
  <item><title>No link</title><pubDate>Mon, 06 Oct 2025 07:00:00 +0000</pubDate></item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Cointelegraph</title>
  <entry><title>$INJ and Solana lead the bounce</title>
    <link rel="replies" href="https://cointelegraph.com/comments/1"/>
    <link rel="alternate" href="https://cointelegraph.com/news/inj-sol"/>
    <updated>2025-10-06T09:15:00Z</updated>
    <summary>Injective up 12%, SOL up 6%.</summary></entry>
</feed>"""


def test_parse_rss_and_atom():
    items = parse_feed(RSS.encode(), FEED_A)
    assert [i["title"] for i in items] == ["Bitcoin ETF inflows hit a record",
                                           "One thing is near certain about the next rate cut"]
    first = items[0]
    assert first["source"] == "CoinDesk" and first["url"] == "https://www.coindesk.com/markets/btc-etf"
    assert first["time"] == 1759739400 and first["summary"] == "Spot BTC funds took in $1B as ether lagged."
    assert set(first["coins"]) == {"BTC", "ETH"}
    assert items[1]["coins"] == []  # "One" and "near" in ordinary words tag nothing

    [entry] = parse_feed(ATOM.encode(), FEED_B)
    assert entry["source"] == "Cointelegraph" and entry["url"] == "https://cointelegraph.com/news/inj-sol"
    assert set(entry["coins"]) == {"INJ", "SOL"} and entry["time"] == 1759742100


def test_parse_feed_refuses_a_dtd():
    evil = b'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY x SYSTEM "file:///etc/passwd">]><rss/>'
    try:
        parse_feed(evil, FEED_A)
    except ValueError as exc:
        assert "DTD" in str(exc)
    else:
        raise AssertionError("a feed with a DTD must be refused")


def test_coin_matching_avoids_false_positives():
    assert match_coins("Bitcoin and Ethereum rally") == ["BTC", "ETH"]
    assert "ONE" not in match_coins("One trader near the top of the leaderboard")
    assert "NEAR" not in match_coins("Price came near resistance")
    assert match_coins("$NEAR jumps after the Near Protocol upgrade") == ["NEAR"]
    assert match_coins("OP tokens unlock") == [] and match_coins("Optimism Superchain grows") == ["OP"]
    assert match_coins("SOL and AVAX lead") == ["SOL", "AVAX"]
    assert match_coins("Bitcoin Cash forks again") == ["BCH"]  # not BTC
    assert match_coins("ethereum classic nodes") == ["ETC"]
    assert symbol_base("BTCUSDT") == "BTC" and symbol_base("inj/usdt") == "INJ" and symbol_base("PEPE") == "PEPE"


# ------------------------------------------------------------------------------------------ service


def _handler(cal_rows, calls, cal_next_ok=True, feeds_ok=(True, True)):
    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        calls.append(url)
        if url == CAL_THIS:
            return httpx.Response(200, json=cal_rows)
        if url == CAL_NEXT:
            return httpx.Response(200, json=[]) if cal_next_ok else httpx.Response(404, text="not yet")
        if url == FEED_A:
            return httpx.Response(200, content=RSS.encode()) if feeds_ok[0] else httpx.Response(503)
        if url == FEED_B:
            return httpx.Response(200, content=ATOM.encode()) if feeds_ok[1] else httpx.Response(503)
        return httpx.Response(404)
    return handler


def test_calendar_endpoint_window_impact_and_cache():
    now = time.time()
    rows = [_cal_row("CPI y/y", now + 3600), _cal_row("Retail Sales", now + 2 * 86400, impact="Medium"),
            _cal_row("Fed Speech", now + 20 * 86400), _cal_row("Jobless Claims", now - 2 * 86400, impact="Medium"),
            _cal_row("FOMC Statement", now - 3600)]
    calls = []
    svc = _service(_handler(rows, calls))

    async def go():
        return (await svc.calendar(7, "high"), await svc.calendar(7, "medium"), await svc.calendar(7, "all", 7),
                await svc.upcoming_events(2, "high"), await svc.upcoming_events(72, "all"))

    high, medium, past, soon, all_soon = _run(svc, go)
    assert [e["title"] for e in high["events"]] == ["CPI y/y"] and high["source"] == "live"
    assert high["countries"] == ["USD"] and high["updated_at"] > 0 and "note" not in high
    assert [e["title"] for e in medium["events"]] == ["CPI y/y", "Retail Sales"]
    assert [e["title"] for e in past["events"]] == ["Jobless Claims", "FOMC Statement", "CPI y/y", "Retail Sales"]
    assert [e["title"] for e in soon] == ["CPI y/y"]  # next 2h, nothing in the past
    assert [e["title"] for e in all_soon] == ["CPI y/y", "Retail Sales"]
    assert calls.count(CAL_THIS) == 1  # one fetch for all five reads


def test_calendar_survives_a_missing_next_week_feed():
    now = time.time()
    calls = []
    svc = _service(_handler([_cal_row("CPI y/y", now + 60)], calls, cal_next_ok=False))
    out = _run(svc, lambda: svc.calendar(7, "high"))
    assert out["source"] == "live" and len(out["events"]) == 1 and CAL_NEXT in calls


def test_calendar_unavailable_is_empty_and_said_so():
    def dead(req):
        raise httpx.ConnectError("offline")

    svc = _service(dead)

    async def go():
        return await svc.calendar(), await svc.upcoming_events()

    out, upcoming = _run(svc, go)
    assert out == {"events": [], "source": "unavailable", "updated_at": None, "countries": ["USD"],
                   "note": out["note"]} and "could not be reached" in out["note"]
    assert upcoming == []  # never invented


def test_calendar_serves_the_last_copy_when_the_feed_breaks():
    now = time.time()
    state = {"ok": True}

    def handler(req):
        if str(req.url) == CAL_THIS and state["ok"]:
            return httpx.Response(200, json=[_cal_row("CPI y/y", now + 3600)])
        if str(req.url) == CAL_NEXT:
            return httpx.Response(200, json=[])
        return httpx.Response(500)

    svc = _service(handler)

    async def go():
        first = await svc.calendar()
        state["ok"] = False
        svc._cal_state = (svc._cal_state[0], svc._cal_state[1], 0.0)  # expire the hour-long cache
        return first, await svc.calendar()

    first, second = _run(svc, go)
    assert first["source"] == "live" and second["source"] == "stale"
    assert [e["title"] for e in second["events"]] == ["CPI y/y"] and "last calendar" in second["note"]


def test_news_merges_feeds_tags_coins_and_filters_by_symbol():
    calls = []
    svc = _service(_handler([], calls))

    async def go():
        return (await svc.news(None, 10), await svc.news("INJUSDT"), await svc.news("BTCUSDT"),
                await svc.headlines("SOLUSDT", hours=10**6, limit=3))

    everything, inj, btc, sol = _run(svc, go)
    assert [i["title"][:9] for i in everything["items"]] == ["$INJ and ", "Bitcoin E", "One thing"]  # newest first
    assert everything["source"] == "live" and len(everything["feeds"]) == 2
    assert all(f["ok"] for f in everything["feeds"]) and everything["items"][0]["source"] == "Cointelegraph"
    assert [i["coins"] for i in inj["items"]] == [["SOL", "INJ"]]
    assert [i["title"] for i in btc["items"]] == ["Bitcoin ETF inflows hit a record"]
    assert len(sol) == 1 and set(sol[0]) == {"time", "title", "source", "url"}
    assert calls.count(FEED_A) == 1


def test_news_one_dead_feed_is_still_a_result():
    svc = _service(_handler([], [], feeds_ok=(False, True)))
    out = _run(svc, lambda: svc.news())
    assert out["source"] == "live" and len(out["items"]) == 1
    assert [f["ok"] for f in out["feeds"]] == [False, True]


def test_news_unavailable_is_empty_and_said_so():
    svc = _service(_handler([], [], feeds_ok=(False, False)))

    async def go():
        return await svc.news(), await svc.headlines("BTCUSDT")

    out, heads = _run(svc, go)
    assert out["items"] == [] and out["source"] == "unavailable" and "retrying" in out["note"] and heads == []


def test_calendar_countries_all():
    now = time.time()
    svc = _service(_handler([_cal_row("ECB Rate", now + 60, country="EUR")], []), calendar_countries=("ALL",))
    out = _run(svc, lambda: svc.calendar())
    assert out["countries"] == ["ALL"] and [e["country"] for e in out["events"]] == ["EUR"]


def test_oversized_feed_is_refused():
    big = json.dumps([_cal_row("Pad", time.time() + 60, forecast="x" * 100) for _ in range(60_000)])
    svc = _service(lambda req: httpx.Response(200, content=big.encode()))
    out = _run(svc, lambda: svc.calendar())
    assert out["source"] == "unavailable"


def test_calendar_and_news_endpoints():
    from fastapi.testclient import TestClient

    from app.main import app

    now = time.time()
    with TestClient(app) as client:
        svc = app.state.events
        svc.s = _settings()
        svc._client = httpx.AsyncClient(transport=httpx.MockTransport(_handler([_cal_row("CPI y/y", now + 60)], [])))
        cal = client.get("/api/calendar", params={"days": 3, "impact": "medium"}).json()
        assert cal["source"] == "live" and [e["title"] for e in cal["events"]] == ["CPI y/y"]
        news = client.get("/api/news", params={"symbol": "BTCUSDT", "limit": 5}).json()
        assert news["source"] == "live" and news["items"][0]["coins"] == ["BTC", "ETH"]
        assert client.get("/api/calendar", params={"impact": "huge"}).status_code == 422
        assert client.get("/api/news", params={"symbol": "BTC USDT!"}).status_code == 422
