# Work in progress (paused)

Step 1 of docs/upgrade-plan.md (bug fixes) was paused part-way. These patches are the unfinished, unreviewed work of
two fix groups, against main at 484b323. Apply with `git apply docs/wip/<file>`, then review, finish and test.

- `fix-feeds.partial.patch`: fixes 0-2 (stream fallback never returning to Binance, one timeout switching every caller to
  demo candles, signal checks losing the candle at a feed blip). Only a draft test file so far, no code changes yet.
- `fix-agent.partial.patch`: fixes 3, 4, 7, 8, 9 (history over 2000 chars failing requests, follow-up chips wiping the
  plan, "500usdt" read as a coin, raw symbols in the tool planner, the narrator not told the coin or demo data). Code
  changes in agent.py, agent_loop.py, llm.py, schemas.py, symbols.py, evals and a draft test file; not yet run.

The other five groups (account, notify, desk-kimi, ta, frontend) have not been started.
