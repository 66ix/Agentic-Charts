"""Chart screenshots: a vision model (mocked over HTTP) reads the drawings, they become overlays and are compared
with the app's own levels."""

import base64
import json
import os

os.environ["DATA_SOURCE"] = "synthetic"
os.environ["LLM_PROVIDER"] = "none"

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.llm import LLMClient  # noqa: E402
from app.main import app  # noqa: E402
from app.screenshot import ScreenshotRead, clean, compare, split_image, to_overlays  # noqa: E402

PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()


def _vision_llm(fields_for, seen):
    """Anthropic mocked: answers with the tool call `fields_for(last_price)` would make."""
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200, json={"content": [{"type": "tool_use", "name": "chart_screenshot",
                                                      "input": fields_for()}]})

    llm = LLMClient(Settings(llm_provider="anthropic", anthropic_api_key="k"))
    llm._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return llm


def test_split_image_checks_type_and_size():
    assert split_image(PNG)[0] == "image/png"
    assert split_image("data:image/jpg;base64,QUJD")[0] == "image/jpeg"
    with pytest.raises(ValueError):
        split_image("data:image/svg+xml;base64,QUJD")
    with pytest.raises(ValueError):
        split_image("data:image/png;base64,not base64!")


def test_overlays_map_positions_to_times_when_the_axis_is_read():
    read = ScreenshotRead.model_validate({
        "symbol": "BTCUSDT", "interval": "4h", "time_left": "2026-09-01T00:00", "time_right": "2026-09-11T00:00Z",
        "levels": [{"price": 60000, "kind": "support", "label": ""}],
        "zones": [{"price_high": 64000, "price_low": 63000, "kind": "supply", "label": "", "x_start": 0.5,
                   "x_end": None}],
        "lines": [{"x1": 0.9, "price1": 62000, "x2": 0.1, "price2": 58000, "label": ""}],
        "patterns": [{"name": "Ascending triangle", "direction": "bullish", "key_price": 64000, "target": 70000,
                      "x": 0.6}],
        "notes": ""})
    ov = {o.id: o for o in to_overlays(read)}
    assert ov["shot:level:0"].price == 60000 and ov["shot:level:0"].kind == "screenshot"
    t0 = 1788220800  # 2026-09-01 UTC
    assert ov["shot:zone:0"].time_start == t0 + 5 * 86400 and ov["shot:zone:0"].time_end is None
    line = ov["shot:line:0"]
    assert (line.time1, line.price1, line.time2, line.price2) == (t0 + 86400, 58000, t0 + 9 * 86400, 62000)
    assert ov["shot:target:0"].price == 70000 and "▲" in ov["shot:pattern:0"].label
    # Without readable dates, trendlines are left out and boxes span the chart.
    no_dates = to_overlays(read.model_copy(update={"time_left": None}))
    assert not any(o.type == "trendline" for o in no_dates)
    assert next(o for o in no_dates if o.type == "box").time_start is None


def test_clean_drops_misread_prices_and_compare_matches_levels():
    read = ScreenshotRead(levels=[{"price": 100.0}, {"price": 5000.0}, {"price": 131.0}], interval="7h")
    out, dropped = clean(read, (90.0, 140.0))
    assert [lv.price for lv in out.levels] == [100.0, 131.0] and dropped == 1 and out.interval is None
    detected = [{"type": "horizontal_line", "price": 100.4}, {"type": "box", "price_low": 120, "price_high": 125}]
    assert compare(out, detected) == (1, [131.0])


def test_endpoint_reads_draws_and_compares():
    seen: list = []
    with TestClient(app) as client:
        kl = client.get("/api/klines", params={"symbol": "ETHUSDT", "interval": "4h", "limit": 300}).json()["candles"]
        last = kl[-1]["close"]
        before = app.state.llm
        app.state.llm = _vision_llm(lambda: {
            "symbol": "ETH/USDT", "interval": "4h", "time_left": None, "time_right": None,
            "levels": [{"price": last * 0.97, "kind": "support", "label": "Weekly support"},
                       {"price": last * 40, "kind": "resistance", "label": ""}],
            "zones": [{"price_high": last * 1.05, "price_low": last * 1.04, "kind": "supply", "label": "",
                       "x_start": None, "x_end": None}],
            "lines": [], "patterns": [{"name": "Bull flag", "direction": "bullish", "key_price": last * 1.02,
                                       "target": None, "x": None}],
            "notes": ""}, seen)
        try:
            r = client.post("/api/screenshot", json={"image": PNG, "symbol": "BTCUSDT", "interval": "1h"})
        finally:
            app.state.llm = before
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["symbol"] == "ETHUSDT" and j["interval"] == "4h" and j["detected"]
    assert {o["id"] for o in j["overlays"]} == {"shot:level:0", "shot:zone:0", "shot:pattern:0"}
    assert "skipped" in j["summary"] and "Bull flag (bullish)" in j["summary"] and "match levels" in j["summary"]
    img = seen[0]["messages"][0]["content"][0]
    assert img["type"] == "image" and img["source"]["media_type"] == "image/png"
    assert seen[0]["tool_choice"] == {"type": "tool", "name": "chart_screenshot"}


def test_endpoint_without_a_model_says_what_is_needed():
    with TestClient(app) as client:
        r = client.post("/api/screenshot", json={"image": PNG})
        assert r.status_code == 503 and "images" in r.json()["detail"]
        assert client.post("/api/screenshot", json={"image": "data:text/plain;base64,QQ=="}).status_code == 422
