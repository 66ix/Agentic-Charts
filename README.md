# Agentic Charts

Real-time crypto charting with AI-detected market structure. A dark, TradingView-style
workspace: live Binance candles, a global market header, a manual drawing toolbar, and a
chart agent that turns plain-English requests ("Identify the current H4 supply zone and key
resistance high") into zones, levels and lines drawn on the chart.

![Agent overlays](docs/screenshot-agent.png)

## How it works

```
 Browser (Next.js 15 + Lightweight Charts v4)              FastAPI (Python 3.11+)
 ┌───────────────────────────────────────────┐            ┌─────────────────────────────────────┐
 │ MarketHeader ── GET /api/market/metrics ──┼──────────▶ │ market_metrics.py  CoinGecko, F&G   │
 │ AgenticChart ── GET /api/klines ──────────┼──────────▶ │ market_data.py     Binance REST     │
 │              ── WS  /ws/klines ───────────┼──────────▶ │ stream_hub.py      Binance WS fan-out│
 │ AgentPanel ──── POST /api/agent/analyze ──┼──────────▶ │ agent.py                            │
 │   ▲ overlays JSON → custom primitives     │            │   llm.py      prompt → intent (JSON)│
 │   BoxZone / LabeledRay / TrendLine        │            │   ta_agent.py SciPy detectors       │
 │ DrawingLayer (user drawings)              │            │   llm.py      facts → summary       │
 └───────────────────────────────────────────┘            └─────────────────────────────────────┘
```

The LLM never invents prices. It only chooses which detectors to run (via a JSON-schema
structured output) and phrases the result. Every level comes from the SciPy engine, and
the whole pipeline works with no LLM at all (a keyword parser and templated summary take over).

## Project structure

```
agentic-charts/
├── backend/
│   ├── app/
│   │   ├── main.py            FastAPI app: REST + WebSocket routes
│   │   ├── ta_agent.py        Swings, S/R clustering, supply/demand, window levels, trendlines
│   │   ├── agent.py           prompt → intent → data → detectors → summary
│   │   ├── llm.py             Ollama / OpenAI-compatible / Anthropic structured outputs + rule fallback
│   │   ├── market_data.py     Binance klines (paginated, 3h resampled), synthetic fallback
│   │   ├── stream_hub.py      Shared upstream Binance kline streams, fan-out to browsers
│   │   ├── market_metrics.py  Header metrics (CoinGecko, Fear & Greed, Binance futures)
│   │   ├── derivatives.py     Open interest + rolling 24h liquidations from Binance futures
│   │   ├── alerts.py          Server-side price alerts, Telegram / Discord notifications
│   │   ├── schemas.py         Pydantic models (overlay contract)
│   │   └── config.py          Env configuration
│   ├── tests/                 pytest: detectors, API, conversation, payloads (mocked) + opt-in live checks
│   ├── requirements.txt
│   └── .env.example
├── frontend/
│   ├── app/                   layout, page, globals.css
│   ├── components/
│   │   ├── AgenticChart.tsx   Chart engine, live feed, overlays, drawing interaction
│   │   ├── ChartWorkspace.tsx State + wiring (tools, indicators, agent, persistence)
│   │   ├── MarketHeader.tsx   Global metrics bar
│   │   ├── ChartHeader.tsx    Ticker, timeframes, indicators, layout, search, screenshot
│   │   ├── DrawingToolbar.tsx Left toolbar
│   │   ├── AgentPanel.tsx     Prompt bar + agent replies
│   │   └── SymbolSearch.tsx   Symbol picker
│   ├── lib/chart/
│   │   ├── primitives/        BoxZone, LabeledRay, TrendLine, DrawingLayer (LWC series primitives)
│   │   └── timeMapper.ts      Time ↔ x mapping for points between/beyond bars
│   ├── lib/                   api client, types, indicators (EMA, Parabolic SAR), formatting
│   └── package.json
└── README.md
```

## Run it locally

Requirements: **Python 3.11+**, **Node.js 18.18+** (20 or 22 recommended).
Optional: **[Ollama](https://ollama.com)** for the local LLM.

### 1. Backend

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                 # edit if needed
uvicorn app.main:app --reload --port 8000
```

Check it: <http://localhost:8000/api/health> and the interactive docs at <http://localhost:8000/docs>.

### 2. Frontend

```bash
cd frontend
npm install
cp .env.example .env.local           # points at http://localhost:8000
npm run dev
```

Open <http://localhost:3000>.

For a production build: `npm run build && npm start`.

### 3. (Optional) AI model

The agent works without any model. To let an LLM interpret free-form requests and write the summary:

**Local, Ollama (default provider):**

```bash
ollama pull llama3.1:8b              # or qwen2.5:7b, mistral, etc.
# backend/.env
LLM_PROVIDER=ollama
OLLAMA_MODEL=llama3.1:8b
```

**Cloud, any OpenAI-compatible API (OpenAI, Groq, Together, LM Studio, vLLM):**

```bash
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
OPENAI_BASE_URL=https://api.openai.com/v1
```

**Cloud, Anthropic:**

```bash
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-sonnet-5-5
```

If the model is unreachable or returns invalid JSON, the backend logs a warning, falls back to
the rule parser for that request, and skips the LLM for 30 seconds.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `DATA_SOURCE` | `auto` | `auto` = Binance with synthetic fallback, `binance`, or `synthetic` (offline demo) |
| `BINANCE_REST_URL` | `https://api.binance.com` | Primary spot host |
| `BINANCE_WS_URL` | `wss://stream.binance.com:9443/ws` | Primary spot stream host |
| `BINANCE_FALLBACK_REST_URL` / `_WS_URL` | `binance.vision` hosts | Used automatically when the primary answers 451/403 (US IPs) |
| `CANDLE_CACHE` / `CANDLE_CACHE_PATH` | `on`, `backend/.cache/candles.sqlite3` | Closed Binance candles kept in SQLite, so restarts only fetch new bars |
| `DERIVATIVES` | `on` | Live open interest and liquidations from Binance futures (`off` to mock them) |
| `OI_TOP_SYMBOLS` | `40` | Open interest sums this many top USDT perpetuals by volume |
| `LLM_PROVIDER` | `ollama` | `ollama`, `openai`, `anthropic`, `none` |
| `CORS_ORIGINS` | `http://localhost:3000,...` | Comma-separated frontend origins |
| `METRICS_CACHE_SECONDS` | `60` | Header metrics cache |
| `ALERTS_STORE` | `backend/.cache/alerts.json` | Where price alerts are saved; `memory` = not saved |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | empty | Send fired alerts to Telegram (see [Alerts](#alerts)) |
| `DISCORD_WEBHOOK_URL` | empty | Send fired alerts to a Discord channel |
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000` | Frontend → backend |
| `NEXT_PUBLIC_WS_URL` | derived from API URL | Override for proxies |

When Binance is unreachable the header shows a yellow **DEMO DATA** badge and the chart runs on a
deterministic synthetic feed, so the UI is never blank. Binance returns HTTP 451 to US IPs; the
backend then switches to the `binance.vision` market-data hosts by itself.

Header metrics are all live from free, keyless sources: CoinGecko (market cap, volume, BTC
dominance), alternative.me (Fear & Greed) and Binance USD-M futures (open interest across the top
perpetuals, with the change versus 24h ago, and liquidations from the `!forceOrder@arr` stream as a
rolling 24h total saved to `backend/.cache/`). Both futures figures cover Binance only, and Binance
sends at most one liquidation per symbol per second, so that total is a lower bound; hover a metric
for its source. Binance futures has no US-accessible mirror, so from a US IP those two fall back to
mocked values (marked with a dot).

## Alerts

Price alerts are stored and checked by the backend, so they fire with every browser tab closed.
For each symbol with armed alerts the backend watches the live 1m stream and fires an alert once,
when price crosses its level or enters its zone (or gaps through it). A fired alert shows as a
toast, a sound and a desktop notification in any open tab, and is sent to Telegram and/or Discord
when configured. Prices from the synthetic fallback feed never fire alerts (unless
`DATA_SOURCE=synthetic`), so a Binance outage cannot send a false notification.

**Telegram**

1. In Telegram, message [@BotFather](https://t.me/BotFather), send `/newbot` and follow the prompts. It replies with the bot token (`123456:ABC...`).
2. Open a chat with your new bot and send it any message (for a group, add the bot to the group and post there).
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `"chat":{"id": ...}` (group ids are negative).
4. In `backend/.env`: `TELEGRAM_BOT_TOKEN=...` and `TELEGRAM_CHAT_ID=...`.

**Discord**

1. In your server: **Server Settings > Integrations > Webhooks > New Webhook**, pick the channel, then **Copy Webhook URL**.
2. In `backend/.env`: `DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...`.

Restart the backend, open the bell panel in the header and press **Send test**, or run
`curl -X POST localhost:8000/api/alerts/test`. Messages look like
`INJUSDT: price entered 24.10–24.60 (H4 Demand) at 24.32`. Delivery failures are logged (without
the token) and never block other alerts. Alerts created before this version lived in the browser;
they are uploaded once the first time the app connects to a backend that has none.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Status, data source, LLM provider, active streams |
| GET | `/api/klines?symbol=INJUSDT&interval=4h&limit=500` | Historical candles (`3h` is resampled from `1h`); `&since=<unix s>` returns only bars from that time on |
| GET | `/api/symbols` | Tradable USDT spot pairs |
| GET | `/api/market/metrics` | Header metrics, each tagged `live` or `mock` |
| POST | `/api/agent/analyze` | `{symbol, interval, prompt, candles?, history?, overlays?, previous_intent?}` → overlays + summary + alerts |
| GET | `/api/alerts` | `{alerts, channels: {telegram, discord}}` |
| POST | `/api/alerts` | `{symbol, alerts: [{kind: "cross"\|"zone", price?, price_low?, price_high?, label}]}` → created alerts |
| DELETE | `/api/alerts/{id}` | Delete an alert |
| POST | `/api/alerts/{id}/rearm` | Re-arm a fired alert |
| POST | `/api/alerts/clear-triggered` | Delete all fired alerts |
| POST | `/api/alerts/test` | Send a test message to the configured channels → `{results: {telegram: true}}` |
| WS | `/ws/klines?symbol=INJUSDT&interval=4h` | `{type:"kline", candle, closed, source}` and `{type:"status"}` messages |
| WS | `/ws/alerts` | `{type:"snapshot", alerts}` on connect and on every change, `{type:"fired", alert, price}` |

Example:

```bash
curl -s localhost:8000/api/agent/analyze -H 'content-type: application/json' \
  -d '{"symbol":"INJUSDT","interval":"4h","prompt":"Identify the current H4 supply zone and key resistance high"}'
```

```json
{
  "overlays": [
    { "type": "box", "label": "H4 Resistance", "price_high": 7.90, "price_low": 7.70, "color": "rgba(239, 68, 68, 0.22)", "kind": "resistance" },
    { "type": "box", "label": "H4 Supply (fresh)", "price_high": 8.16, "price_low": 7.98, "color": "rgba(249, 115, 22, 0.18)", "kind": "supply" },
    { "type": "horizontal_line", "label": "H4 window high", "price": 8.70, "color": "#ef4444", "time_start": 1759636800 }
  ],
  "summary": "INJUSDT on H4: ... Nearest H4 resistance 7.70–7.90 (3 touches) ...",
  "intent": { "features": ["support_resistance", "supply_demand", "window_levels"], "timeframe": "4h", "max_zones": 1 },
  "engine": { "intent": "ollama:llama3.1:8b", "summary": "ollama:llama3.1:8b", "detector": "scipy" }
}
```

Overlay types: `box`, `horizontal_line`, `trendline`, `marker` (see `backend/app/schemas.py` and
`frontend/lib/types.ts`).

## Detection methods (`ta_agent.py`)

- **Swing highs/lows:** `scipy.signal.find_peaks` on highs and inverted lows, with prominence ≥ 1 ATR and a minimum bar distance. Each swing is tagged HH/LH/HL/LL against the previous one.
- **Support/resistance zones:** complete-linkage hierarchical clustering of swing prices (`scipy.cluster.hierarchy`) with a 0.6 ATR threshold; adjacent bands are merged (up to 1.5 ATR tall). Scored by touches, recency and prominence; the best zones above and below price are kept.
- **Supply/demand zones:** a base of up to 3 small-bodied candles followed by an impulse ≥ 1.6 ATR. Zones a later candle has closed through are discarded; untested ones are marked "fresh".
- **Window highs/lows:** the high and low of the last completed H4 / D1 (or requested) candle, drawn as rays from that candle.
- **Trendlines:** through the two latest swing highs (if falling) and swing lows (if rising), extended right.

## Using the app

- **Agent:** press `/` or click **Agent**, then type a request or tap a suggestion. Mention a timeframe ("H4", "daily") to analyse it regardless of the chart's timeframe. The agent remembers the conversation and what it drew, so you can follow up: "also show swings", "same on daily", "remove the trendlines", "clear the chart", or give your own prices ("line at 25.4", "zone 24 to 25", "entry at 24.2, stop at 23.8, target at 27"). **Clear** removes AI overlays and the trash icon starts a new conversation. AI overlays are saved per symbol and timeframe and the conversation per symbol, so a reload keeps them. With **Auto AI levels** on (layout menu), key levels are drawn when a chart has none saved.
- **Alerts:** ask the agent ("alert me at 65k", "alert me if price enters the supply zone", "alert me on these levels"), or select a horizontal ray or rectangle and press the bell in the toolbar. Alerts show as amber dotted lines, are listed under the bell in the header, and fire once with a sound, a toast and a desktop notification (if allowed). The backend checks them against live 1m prices, so they also fire with the app closed; set up [Telegram or Discord](#alerts) to hear about those.
- **Drawing tools:** Trendline `T`, Horizontal ray `H`, Fibonacci `F`, Rectangle `R`, Text `N`, XABCD pattern `P`, Measure `M`. Click to place points; `Esc` cancels. In crosshair mode, click a drawing to select it, drag to move it, `Delete` to remove it. Magnet snaps to the nearest OHLC price; Lock freezes drawings. Drawings are saved per symbol in the browser.
- **Indicators:** EMA 20, EMA 50, Parabolic SAR, Volume. **Layout:** log scale, grid, auto levels. The camera button saves a PNG.

## Tests

```bash
cd backend && pip install -r requirements-dev.txt && pytest -q
cd frontend && npm run typecheck && npm run lint && npm run build

# Against the real APIs (CoinGecko, Fear & Greed, Binance spot + futures):
cd backend && LIVE_TESTS=1 pytest tests/test_live.py -v -rs
```

GitHub Actions runs the unit tests, lint, typecheck and build on every push and PR
(`.github/workflows/ci.yml`), and the live checks weekly and on demand
(`live-smoke.yml`). Binance futures blocks GitHub's US runners, so those checks skip there.

## Production notes

- Run the API with several workers behind a reverse proxy that supports WebSockets, e.g. `uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2` (each worker keeps its own Binance upstreams).
- Set `CORS_ORIGINS` and `NEXT_PUBLIC_API_URL` / `NEXT_PUBLIC_WS_URL` to your real domains (`wss://` behind TLS).
- Open interest and liquidations cover Binance only. For cross-exchange totals, plug a CoinGlass (or similar) key into `market_metrics.py`.
- Nothing here is financial advice; detections are heuristics.
