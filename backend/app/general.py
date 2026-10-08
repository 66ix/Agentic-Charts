"""Plain-language questions that aren't about a chart: "what day is it?", "what did the FOMC decide?", "which coins
have an upgrade coming?".

The chart agent answers these from what it can actually check, never from memory alone:

  * the date and time (the server clock, UTC),
  * the economic calendar (events.py: the last week and the next one, with forecasts and previous values; the free
    feed has no released numbers, so results come from the news or the web),
  * recent crypto headlines (events.py), the ones sharing words with the question first,
  * a web search, when the model's provider offers one (llm.LLMClient.answer: Anthropic's web search tool, OpenAI's
    Responses API web search; WEB_SEARCH=off turns it off).

Without a language model the answer is a template: the date, or the matching headlines and calendar events.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Optional

from .events import EventsService

CALENDAR_PAST_DAYS = 7
CALENDAR_AHEAD_DAYS = 7
NEWS_DAYS = 7
MAX_MATCHES = 8
MAX_LATEST = 6

STOPWORDS = frozenset("""a an and any are as at be been but by can could did do does for from had has have how i in is it
its just me my of on or our so than that the their them then there these they this to us was we were what when where which
who why will with would you your about after again all also am before being both each few get got going more most much
now only other out over same should some such tell think up very want news latest recent today yesterday week coin coins
crypto results result meeting happened""".split())

DATE_WORDS = re.compile(r"\b(?:what(?:'s| is)? (?:the |today'?s )?(?:date|day|time)|what day is (?:it|today)|"
                        r"today'?s date|what time is it|which day is (?:it|today)|what year is it|what month is it)\b",
                        re.I)


def now_facts(now: Optional[float] = None) -> dict:
    dt = datetime.fromtimestamp(time.time() if now is None else now, tz=timezone.utc)
    return {"date": dt.strftime("%Y-%m-%d"), "weekday": dt.strftime("%A"),
            "time_utc": dt.strftime("%H:%M"), "readable": f"{dt:%A} {dt.day} {dt:%B %Y, %H:%M} UTC"}


# Words a headline uses for the same thing the question asks about.
SYNONYMS: dict[str, tuple[str, ...]] = {
    "fomc": ("fed", "powell", "rate cut", "rate hike", "interest rate"),
    "fed": ("fomc", "powell", "interest rate"),
    "cpi": ("inflation",),
    "inflation": ("cpi", "pce"),
    "upgrade": ("hard fork", "mainnet", "network upgrade", "hardfork"),
    "upgrades": ("upgrade", "hard fork", "mainnet", "hardfork"),
    "unlock": ("token unlock", "unlocks"),
    "etf": ("etfs", "spot etf"),
}


def keywords(text: str) -> list[str]:
    words = [w.lstrip("$") for w in re.findall(r"[a-z0-9$]{2,}", text.lower())]
    out = [w for w in words if w not in STOPWORDS and len(w) >= 3]
    for w in list(out):
        out += SYNONYMS.get(w, ())
    return list(dict.fromkeys(out))


def _ago(seconds: float) -> str:
    h = seconds / 3600
    return f"{h:.0f}h ago" if h < 48 else f"{h / 24:.0f} days ago"


async def context(events: Optional[EventsService], question: str, now: Optional[float] = None) -> dict:
    """What the answer may use: the date, the calendar around today and the headlines that match the question."""
    t = time.time() if now is None else now
    out: dict = {"now": now_facts(t)}
    if events is None:
        return out
    try:
        cal = await events.calendar(days=CALENDAR_AHEAD_DAYS, impact="medium", past_days=CALENDAR_PAST_DAYS)
    except Exception:  # the answer goes out without it
        cal = {"source": "unavailable", "events": []}
    if cal.get("source") != "unavailable":
        rows = cal.get("events") or []
        out["calendar_past"] = [{"when": _ago(t - e["time"]), "date": now_facts(e["time"])["date"], "title": e["title"],
                                 "country": e["country"], "impact": e["impact"], "forecast": e.get("forecast"),
                                 "previous": e.get("previous")} for e in rows if e["time"] < t][-12:]
        out["calendar_next"] = [{"in_hours": round((e["time"] - t) / 3600, 1), "date": now_facts(e["time"])["date"],
                                 "title": e["title"], "country": e["country"], "impact": e["impact"],
                                 "forecast": e.get("forecast"), "previous": e.get("previous")}
                                for e in rows if e["time"] >= t][:10]
        out["calendar_note"] = "The calendar lists forecasts and previous values only; released numbers come from news."
    try:
        news = await events.news(None, 200)
    except Exception:
        news = {"items": [], "source": "unavailable"}
    items = [i for i in news.get("items") or [] if i["time"] >= t - NEWS_DAYS * 86400]
    words = keywords(question)
    scored = []
    for i in items:
        hay = f"{i['title']} {i.get('summary', '')}".lower()
        hits = sum(1 for w in words if w in hay) + sum(2 for c in i.get("coins") or [] if c.lower() in words)
        if hits:
            scored.append((hits, i))
    scored.sort(key=lambda hi: (-hi[0], -hi[1]["time"]))
    pick = lambda i: {"when": _ago(t - i["time"]), "title": i["title"], "source": i["source"], "url": i["url"],  # noqa: E731
                      "summary": (i.get("summary") or "")[:240]}
    out["matching_headlines"] = [pick(i) for _, i in scored[:MAX_MATCHES]]
    seen = {h["url"] for h in out["matching_headlines"]}
    out["latest_headlines"] = [pick(i) for i in items if i["url"] not in seen][:MAX_LATEST]
    if news.get("source") == "unavailable":
        out["news_note"] = "The news feeds could not be reached just now."
    return out


def _when(e: dict) -> str:
    return e["when"] if "when" in e else f"in {e['in_hours']:g}h"


def is_date_question(text: str) -> bool:
    return bool(DATE_WORDS.search(text))


def template_answer(question: str, ctx: dict) -> str:
    """The answer without a language model."""
    now = ctx["now"]
    if is_date_question(question):
        return f"Today is {now['readable']}."
    parts = []
    if ctx.get("matching_headlines"):
        heads = "; ".join(f"{h['source']}, {h['when']}: {h['title']}" for h in ctx["matching_headlines"][:4])
        parts.append(f"Recent headlines on that: {heads}.")
    words = keywords(question)
    events = [e for e in ctx.get("calendar_past", []) + ctx.get("calendar_next", [])
              if any(w in e["title"].lower() for w in words)]
    if events:
        parts.append("On the calendar: " + "; ".join(f"{e['title']} ({_when(e)})" for e in events[:4]) + ".")
    if not parts:
        parts.append("I couldn't find anything on that in the recent headlines or the economic calendar.")
    parts.append("Set up a language model in Settings to get full answers with web search.")
    return f"It's {now['readable']}. " + " ".join(parts)


def sources_from(ctx: dict) -> list[dict[str, str]]:
    return [{"title": h["title"], "url": h["url"], "source": h["source"]} for h in ctx.get("matching_headlines", [])[:5]]
