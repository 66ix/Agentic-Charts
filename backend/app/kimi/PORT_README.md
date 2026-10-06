# Kimi Cooked – Elite Edition v5.7.4 · Python port

A Python port of the TradingView indicator. One bar-by-bar loop runs the script's own steps in the
script's own order, on closed candles, and produces the same two tables you see on the chart:

* **PATH VERIFY**: how the forecast line did (Traj Acc, Skill vs random walk, Tgt Hit, MATE, Cover,
  band widths, gains, t / joint, out-of-sample score, Call %, Miss up/dn, next-candle record)
* **Signal Stats**: DIV, U/Dn, Early ?, Conf top/rest, Long, Short and Random, as win% (W/N) plus
  the edge over random entries taken in the same direction at the same time, and Exp/Trade

It also gives you every signal with its outcome, every recorded forecast with what happened at its
horizon, and the last forecast with the level odds (S/R and Fib ladder).

Needs Python 3.9+ and numpy (pandas only for `signals_frame()`). About 1.5 s per 6,000 candles.

## Quick start

```bash
python run_example.py sample_BTCUSDT_1h.csv            # script defaults
python run_example.py sample_BTCUSDT_1h.csv --mine     # your chart's settings (Node Tolerance 0.05, Coverage 0.75)
python run_example.py my_candles.csv --bars 6000 --set statCostPct=0.05 --set fcNcMinTf=5
```

It prints both tables and the last forecast, and writes `<file>_signals.csv` and
`<file>_forecasts.csv` next to the input.

### In Python

```python
from kimi_v574 import KimiCooked, Inputs
from data import load_csv, tf_minutes_of

d = load_csv("my_candles.csv")                      # time, open, high, low, close, volume
eng = KimiCooked(tf_minutes_of(d["t"]), Inputs())   # or Inputs.users_chart(), or Inputs(fcCover=0.8, ...)
res = eng.run(d["t"], d["o"], d["h"], d["l"], d["c"], d["v"])

res.print_tables()
res.verify_table()          # dict, PATH VERIFY
res.stats_table()           # dict, Signal Stats
res.signals                 # list of Signal (bar, dir, type, entry, conf, tier, result, R, ...)
res.signals_frame()         # same as a pandas DataFrame (random controls left out)
res.forecasts               # every recorded forecast: path, band, horizon, gains, tilt, outcome
res.last_forecast()         # path, range, S/R odds, Fib ladder odds on the last candle
res.level_odds(price)       # chance price reaches `price` within the window (what the % tags show)
```

All inputs carry the script's variable names (`pivotLen`, `vTolMult`, `fcCover`, `statCostPct`,
`fcNcMinTf`, ...) and default to the script's defaults.

## Getting candles

* **TradingView**: chart → ⋯ → *Export chart data*. The CSV loads as is (`load_csv`).
* **Binance klines JSON** (`/api/v3/klines` responses): `load_binance_json("folder/*.json")`.
* **github.com/finom/static-klines** (free Binance spot history, updated daily):
  `load_static_klines(repo_dir, "BTCUSDT", "15m")`.

Two things decide whether you reproduce a chart exactly:

1. **Bar 0.** On TradingView bar 0 is the first candle the chart loaded. The warm-up starts there,
   and the random-entry controls sit on every 5th bar counted from it. So feed the same candles
   the chart has: `last_n(d, n, end_ms)` keeps the last `n` candles up to a time.
2. **Higher-timeframe history.** `request.security` always sees the full daily/weekly/monthly
   history. Pass a longer history of the same symbol so the HTF anchor and the daily-trend state
   of the band are right from the first candle:
   `eng.run(..., history={"t": full["t"], "c": full["c"]})`.
   Without it, a short intraday window (e.g. 6,000 × 15m = 62 days) starts with no 50-day trend
   SMA and no weekly anchor.

## How close it is

Below, the port against the live TradingView tables on 9 Binance charts read on 2 Oct 2026, at your
settings, with the same number of candles and the full history passed in
(`validation/compare_with_tradingview.py` reruns this).

| chart | Evaluated | Traj Acc | Skill | Cover | Call % | Next candle | DIV | U/Dn | Early ? | Random |
|---|---|---|---|---|---|---|---|---|---|---|
| BTC 15m | 5981 / 5981 | 52.32 / 52.32 | 0.03 / 0.03 | 73.69 / 73.69 | 20.77 / 20.77 | 53.4 / 53.4 | 5/24 = | 4/11 = | 31/115 = | 207/1026 = |
| BTC 1h | 6566 / 6566 | 56.64 / 56.14 | 0.02 / 0.04 | 74.99 / 74.98 | 18.63 / 18.21 | 59.4 / 59.5 | 5/30 = | 2/12 = | 39/137 = | 382/1178 = |
| BTC 4h | 6010 / 6009 | 46.61 / 46.62 | -0.06 / -0.06 | 74.58 / 74.57 | 14.70 / 14.74 | 56.3 / 56.2 | 15/27 = | 5/12 = | 53/124 = | 496/1113 = |
| ETH 15m | 5989 / 5989 | 54.04 / 53.70 | 0.10 / 0.11 | 75.58 / 75.59 | 18.45 / 17.50 | 54.9 / 54.9 | 3/23 = | 1/4 = | 29/122 = | 263/1080 = |
| ETH 1h | 6573 / 6573 | 56.35 / 56.70 | 0.06 / 0.06 | 74.35 / 74.33 | 15.06 / 14.94 | 55.4 / 55.4 | 7/30 = | 2/7 = | 40/126 = | 404/1170 vs 411/1170 |
| ETH 4h | 6011 / 6010 | 52.77 / 52.77 | 0.00 / 0.00 | 78.01 / 77.97 | 12.59 / 12.63 | 57.2 / 57.1 | 16/22 = | 3/10 = | 41/103 = | 522/1141 vs 517/1144 |
| LINK 15m | 5987 / 5987 | 51.05 / 51.09 | -0.01 / -0.03 | 75.37 / 75.36 | 20.42 / 20.66 | 52.9 / 52.9 | 6/23 = | 3/11 = | 35/127 = | 325/1138 = |
| LINK 1h | 6571 / 6570 | 47.55 / 52.40 | -0.07 / 0.04 | 74.34 / 74.25 | 23.61 / 16.13 | 55.5 / 55.4 | 8/31 vs 8/30 | 3/9 = | 40/121 vs 38/118 | 482/1229 = |
| LINK 4h | 6006 / 6005 | 44.62 / 45.77 | -0.13 / -0.10 | 71.76 / 71.76 | 22.58 / 23.13 | 54.9 / 54.9 | 15/36 = | 4/14 = | 52/142 = | 525/1148 = |

Each cell reads port / TradingView; "=" means identical.

On 8 of the 9 charts the DIV, U/Dn and Early counts are identical. Evaluated, MATE, Cover and
Miss up/dn match to about the second decimal, and the random-entry wins match exactly on 7 charts
(within 1.5% on the other two). The edges vs random agree as well (BTC 15m: DIV +1, U/Dn +17,
Early +7 points in both). The fit statistics (t, joint, OOS) and, through them, Call % / Traj Acc /
Tgt Hit can differ a little. On TradingView, chart-pattern and harmonic targets also pull the
forecast path. That changes the "Lvl" input the fit learns from, and those modules are not ported.
LINK 1h is the one chart that doesn't line up: the random entries match exactly (482/1229) but 3
Early and 1 DIV signals differ. A candle that differs between TradingView's feed and Binance's API
would do that; the cause wasn't confirmed.

## What is ported and what is not

**Ported, with the same formulas and constants:** dynamic ATR, regime filter, spike/noise pause,
the adaptive structure engine (pivot length, pivot gap, lookback, horizon), RSI/MACD/MFI,
regular/hidden/early divergences (including the v5.7.1 pivot-RSI fix), the S/R engine (push,
evict-weakest, touch/decay, mitigation, expiry, retest, near flags), the HTF anchor and the second
anchor, the quick drift, confluence factors w0 w1 w4 w5 w6 w7 w9, adaptive weights, the top-%
tiers, the signal outcome tracker (fixed-R bracket auto-scaled by timeframe, costs charged once,
loss checked first, expiry), the random controls and their decayed baseline, Exp/Trade, the
forecast path (S/R magnets, learned gains, next-candle exhaustion tilt with the v5.7.3
lowest-timeframe gate and the z < −2 auto-off), PATH VERIFY scoring, the skewed conformal band,
level odds, the out-of-sample shadows, the thinned decayed OLS with intercept, and the joint Wald
gate.

**Not ported:**
* chart patterns and harmonics (no "Pat BO" / "Harmonics" rows, no pattern/harmonic magnets,
  confluence factors w2/w3 always off)
* the HTF divergence factor w8
* session filters (off by default in the script)
* drawings and alerts

Because w2, w3 and w8 are always off, confluence scores are a little lower than on the chart. So
the Conf top/rest split, and Long/Short (which on the chart also include pattern and harmonic
trades), can differ. DIV, U/Dn, Early and Random are unaffected.

## Things worth knowing when you read the output

* **Closed candles only**, like TradingView's history. v5.7.4 makes the live chart decide signals
  on the close as well, so live and history agree.
* **When a DIV / U/Dn could actually be traded.** The label sits on the pivot, but the signal
  exists only `effPivotLen` candles later, when the pivot confirms. `Signal.bar` is that later
  candle, and the stats enter at its close.
* **Win% mostly reflects the bracket and the fee.** Random entries move by the same amount, so
  judge a signal by its edge over random (`edge_pts`, `z`), not by its raw win%.
* **Traj Acc / Tgt Hit can read "-"** at the default Node Tolerance (0.25 × ATR). While the gains
  are off, the only lean in the line is the 0.1σ next-candle tilt, which never clears that
  tolerance. The chart shows "-" too; your 0.05 setting counts the tilt as a call.
* **The next-candle call** is scored on every chart, but below `fcNcMinTf` (15 min by default) it
  does not tilt the forecast. Same behaviour as the chart.

## Files

| file | what it is |
|---|---|
| `kimi_v574.py` | the engine (`Inputs`, `KimiCooked`, `Result`) |
| `data.py` | loaders: CSV, Binance JSON, static-klines repo, `last_n`, `tf_minutes_of` |
| `run_example.py` | command-line runner |
| `sample_BTCUSDT_1h.csv` | 6,600 Binance BTCUSDT 1h candles to try it on |
| `validation/` | the TradingView comparison above |
