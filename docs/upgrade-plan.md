# Upgrade plan (fresh-eyes review, October 2026)

Produced by eight parallel readers (agent brain, TA engine, learning, Kimi, market data, alerts and automation, portfolio, frontend UX), a synthesis pass and two adversarial critics (feasibility and duplication; trader value and correctness) that checked every item against the code. Each item carries the critics' verdicts; where a critic said *modify*, its change applies on top of the design. `upgrade-plan.json` has the same content plus every reader's raw findings.

## Progress

Built in chunks on `claude/optimistic-ride-crkkd7`, each tested before it is pushed.

| Chunk | Fixes | State |
| --- | --- | --- |
| Feeds | 1–3: a synthetic stream goes back to Binance; a failed stream connect no longer demotes REST; one failed request is retried, Binance is demoted only after 3 failures in a row (or a failed ping); a 429 pauses for its Retry-After instead of serving demo candles; the symbol list keeps its last good copy; a signal check that hits demo candles (or Kimi on demo data) is retried, reported on Status when it gives up, and re-queued on the next live tick; a watched stream off Binance for 5 min shows on Status | done |
| Agent | 4, 5, 8, 9, 10: chat turns clipped to 2000 chars; question-only follow-ups keep the plan on screen; amounts like `500usdt` are not coins and unlisted pairs are skipped; the tool planner normalises symbols and catches tool errors; the narrator is told the coin and when the data is demo | done |
| Account | 6, 11, 26: coins in Simple Earn stay in the holdings watch and keep their managed trade open; the average entry uses USD-quoted fills only and includes fees; BNB fees are priced at the fill hour and counted in the PnL; a paper sell from outside a plan (Sell all) takes the plan's coins and closes its stop and targets, Sell all cancels the coin's open sells, and a plan whose buy was cancelled drops its stop | done |
| Notify | 7, 12, 13, 14: timed scans send spot setups by default (`MARKET_SCAN_NOTIFY_SIDE`), drop demo coins from live scans, never send demo or mixed scans, and skip setups sent in the last 12 h; a trade added late catches up quietly with one summary message; the trade manager, brief and holdings watch report 'error' after a failing pass instead of 'ok'; Discord messages never ping @everyone or @here | done |
| Desk and Kimi | 15–19, 34: the repeat window is 2 × the entry window per timeframe, watched zones say why they weren't called, and one skipped for capacity or a running call is called once there is room (its watched copy leaves learning); a race that let two coins past `max_active` is fixed; paper size comes from the risk at the invalidation (net-of-fee Kelly, at most 1% of the wallet, a Kelly of 0.4 risks the full 1%); 'working now' needs 12 zones and chance under 10%, else 'too early'; Kimi verdicts are keyed by candle, type and direction; flat forecasts aren't scored for direction ('called' and 'right when called' rows); desk higher-timeframe frames use closed candles | done |
| TA | 21–25, 30, 31: higher-timeframe confluence uses only S/R touched twice that held as often as it broke and once-tested supply/demand (3 per side), and needs a 0.3 overlap on the same side (desk buckets carry a version so old statistics don't mix in); swings use one fixed spacing, level recency decays over absolute bars, and the chart, desk, scanner, brief, sell check, backtest and top-down all detect on 500 candles; zone tests count visits, not candles; trendlines already closed through are marked 'broken N bars ago' and not extended; 24h change is 24 hours on every timeframe; 1000x perpetuals (PEPE, SHIB, BONK, ...) are mapped with prices and sizes scaled; the sell check judges closed candles and calls an open-candle break provisional | done |
| Frontend | 20, 27, 28, 29, 32, 33, 35 + critic gaps: bar replay hides today's Kimi drawing and shows only labels confirmed by the replay candle; spot-only sizing is capped by cash and shows the risk that leaves instead of leverage (leverage setting hidden in spot mode); localStorage writes prune month-old chart drawings and slim old chats when full, then show a banner instead of failing silently; the dock rail scrolls with fades on short screens; a failed Kimi fetch retries after 5 s, 20 s, 60 s, then every 5 min; signal previews draw sell signals as down arrows (each Kimi hit its own way); Kimi panel times follow the chart's timezone. Critic gaps: a streaming answer is stored once complete, not per token; a stopped answer keeps its text marked stopped; the agent's own chart move no longer cancels its answer; question-only turns keep the previous plan's intent | done |
| Theme: follow-the-thread | The chat sends a focus (the newest plan, asked-about price or scan on this chart, under a day old) shown as a removable 'About:' chip; 'what would invalidate this?' answers from `plan_in_focus` (stop and the structure between stop and entry, filled / stopped / T1 since, higher timeframes for or against) and leaves the plan drawn; the planner sees a FOCUS line; the template reads out the indicator asked about (RSI, MACD, EMA, Stoch, Bollinger, VWAP) and says 'open space' with no resistance; next-step chips come from the answer's facts (`suggest.py`: alert on the zone price sits in, plan a fresh Kimi long, trim near resistance, the disagreeing timeframe...), each checked to parse as meant; vitest is set up for the frontend (`npm test`) | done |
| Theme: rules-first-router | `rule_route` plans clear requests with the rules (explicit actions, feature words, question-only, 'same again') and sends comparisons, should-I and judgement questions, several coins, pronoun follow-ups and plain questions to the model; `AGENT_ROUTER` (rules_first by default with Ollama) with a Planning select in Settings → AI model; the answer's meta says 'plan: rules (fast path)' and the route reason is logged; tool calls on a live chart can't use demo candles; a planner that looked but never drew keeps its research; tool results are clipped to valid JSON; Ollama gets only the forced tool and `<tool_call>` text is parsed; `python -m evals.run --route` reports 82/86 cases on the fast path, all right | done |
| Theme: holdings-truth | Positions carry one merged row per coin (`manual.holdings`: spot and Simple Earn, locked amount and its end date, Earn rewards counted at no cost, average entry over bought coins); LD-prefixed Earn balances in the spot account become Earn rows instead of fake pairs; fills are imported for held coins' USDC and FDUSD pairs where Binance lists them (after every held coin's USDT pair); the holdings watch, the trade manager's open check, the agent (with 'locked in Earn until 14 Oct: can't be sold before then'), the brief and 'what should I sell?' (held coins, the watchlist without a key) use the merged rows; the Account panel shows 'Everything you own' with Spot/Earn chips and a total, and the trade picker lists Earn coins with a badge | done |
| Desk follow-up (from use) | The desk made no calls: 0.25 ATR stops under 1H zones risk ~0.3%, so fees cost ~0.7R and nothing cleared the bar. Stops are now widened until fees cost at most 0.25R (0.8% risk); watched zones say which check stopped them (confidence, expected R, reward:risk, capacity, running call, one per run) and show their expected R; 'Least expected gain' is a setting; 15m, 30m and 1W timeframes (weekly candles on Monday); the Record says zones, not calls, when nothing was traded; the All tab lists watched zones too | done |
| Requests from use | Spot-mode scanner and agent setups say 'Buy' instead of 'Long'; Google AI Studio (Gemini) as a provider (GOOGLE_API_KEY, through Google's OpenAI-compatible endpoint); hidden coins (never requested from Binance, left out of holdings, a Hide button per holding, undo in Setup) and pairs Binance rejects are remembered and not asked again; tickers leave unknown pairs out of the batch (one bad symbol used to fail every price), reuse answers for 3 s and serve the last real prices when a refresh fails; market data panels and the watchlist show their last data at once while refreshing | done |
| Theme: exit-plan | `exit_plan.py`: for each coin held, rungs at the resistance, supply and window highs above price (≥1 4h ATR apart, within +35%; daily-ATR spacing in open space), your average as a break-even rung when it is above price, sizes by profile (quarters, thirds, money back first, which falls back to quarters when the first rung is under your average), rungs under $10 merged and quantities rounded down, locked Earn left out, and the invalidation at the nearest daily support at least one daily ATR below; plans are kept in SQLite, armed as one price alert per rung plus the invalidation (re-arming replaces them), and rungs you sold at are marked done; an 'Exit plan' button per holding opens a card with the profiles, the rungs, Arm alerts and Show on chart; 'where do I take profit?' on a coin you hold answers with the sizes | done |
| Theme: desk-on-chart | The desk's running calls are drawn on every chart of their coin (live, not saved with the panels), on a 'Desk calls' layer with its own eye toggle; each call puts a clickable chip on the chart ('Desk 4H · waiting to buy · 62% if filled (random 33%) · uncalibrated'; demo calls in yellow) that opens the Desk tab on it; one shared poll (60 s) refreshes at once when the desk makes, fills or closes a call (the backend broadcasts desk_scored and the socket's desk messages are no longer dropped); desk lines carry their timeframe; the Desk tab refreshes on those events, its sliders save on release and quick setting clicks no longer overwrite each other | done |
| Theme: alert-cards + chart-snapshots | Notifications are cards (`notify.py`): Discord embeds coloured by side (demo cards grey and marked), the levels as fields, never pinging @everyone, several queued within 2 s in one post; Telegram HTML; a queue and worker per channel that waits out a 429's retry-after, backs off on 5xx and records delivery stats (Alerts → Notify shows failures; /api/status has them); price alerts, signal alerts, triggers, desk calls and results and trade advice send cards; signal, trigger and new desk call cards carry a chart snapshot rendered server-side with Pillow from the candles the signal judged (stamped with the close, DEMO watermark), as an embed image on Discord and a photo on Telegram, kept for the History tab's thumbnails; with PUBLIC_APP_URL each card links back to that chart and opens the right tab | done |
| Theme: close-confirmed-alerts | New signal `level_close`: fires when 1–3 candles of the chosen timeframe close above, below or inside a level (a wick through it does not count, and it fires on the crossing close only, not on every close beyond); `POST /api/level-alerts`; the same level again replaces the old alert; listed with the signal alerts ('4H close below 7.95 (2 closes)'). The New alert form has 'Fire on': touch (live price, as before) or a 15m/1H/4H/daily close, with closes in a row; a level above price waits for a close above, one below for a close below, a zone for a close inside. Exit plans arm the invalidation as a daily close below it instead of a touch. Repeating price alerts no longer re-fire while price hovers on the level: after a fire the side only flips once price is 0.3% away | done |
| Theme: desk-zone-trigger | Each new desk call arms a confirmation trigger inside its buy zone (5m for 15m–1H calls, 15m for 4H and up; any confirmation, fires once), owned by the call so it is listed under Alerts → Triggers and deleted when the call ends (take-profit, invalidated, timed out, expired); it stays armed while the call waits and after it fills, since that is when price is in the zone. The ping says where the call stands ('Inside the desk's 4H buy zone 7.10–7.25; desk limit filled at 7.25, take-profit 8.20, wrong below 7.02') and never quotes the desk's odds, which were measured for the limit entry, not this one. A setting in the Desk tab switches it off | done |
| Theme: notification-center | Desk calls, trade advice and market alerts now toast in the app too, from the history message the backend already broadcasts for each (no per-type mapping; price and signal alerts keep theirs); clicking a toast opens its coin (and timeframe) and the tab it belongs to; toasts other than your own price alerts go after 12 s, paused while hovered, at most 4. The browser tab reads '(2) INJ 24.31 ▲1.2% · 4H', counting events that arrived while it was hidden, and its icon gets a red dot until you come back. 'Since you last looked' works for background tabs: a chart is only marked seen while visible, and coming back to the tab after 2 h shows the card | done |
| Theme: watchlist-badges (part) | [ / ] and Alt+↑/↓ step through the watchlist in the order the Watchlist tab shows it (its sort), not the list's saved order; before that tab has been opened they use the saved order. Not yet: held/desk/alert/note badges, 'My holdings first' sort, the 'something going on' filter | partly done |


Agentic Charts should work like one trader's desk, not 16 separate tabs. The local qwen agent should keep track of the conversation: "what invalidates this?" should be answered about the plan on screen and leave that plan drawn. It should never print a price the chart doesn't have, and easy requests should go through the rules so the drawings appear in about a second. Trick's holdings become central. Coins in Simple Earn stay watched, average entries are correct, one account total has a curve and allocation, and every coin held gets a scale-out exit ladder armed as alerts. The desk's learned odds and Kimi's factors and reach odds appear where buy decisions are made (the plan card, the chart, the crosshair). Discord pings become cards with a chart image, a link back into the app, and quiet hours. The chart gains a level card, editable anchors, a spot position tool, a command palette and a watchlist that knows what Trick holds.

## Contents

- **An agent that keeps track of the conversation and never invents a number**: `follow-the-thread`, `grounded-narrator`, `conversation-evals`, `rules-first-router`, `live-thinking`
- **Your holdings come first**: `holdings-truth`, `portfolio-cockpit`, `exit-plan`
- **The desk and Kimi in every decision**: `desk-on-chart`, `desk-odds-on-plans`, `desk-zone-trigger`, `kimi-inspector`, `kimi-odds-anywhere`
- **Discord pings worth opening**: `alert-cards`, `chart-snapshots`, `quiet-hours-router`, `close-confirmed-alerts`
- **A chart that feels alive**: `level-card`, `drawing-v2`, `command-palette`, `watchlist-badges`, `notification-center`
- **Fixes**: 35 bugs and risks
- **Gaps the critics found**
- **Deferred ideas**

## An agent that keeps track of the conversation and never invents a number

Answers get worse on the local 7B model. On the rule path the app's own follow-up chips wipe the plan. The narrator is never told which coin it is talking about, and nothing checks its numbers. Every request also pays for a ~5k-token planning round, even when the regex parser already passes 72/72 cases. These five items make qwen2.5:7b sharp and safe on an 8 GB card, measure it, and keep a later switch to Claude safe.

### `follow-the-thread`: Follow-ups that know what 'this' is, and next-step chips built from the answer

Effort **L** · impact **5/5** · proposed by agent-brain

> Under a plan, "What would invalidate this?" should answer "a close below 7.51 (the stop, 1.4% away) or an H4 CHoCH under 7.45; D1 trends up, so the daily agrees" and leave the plan on the chart. The chips under each answer should come from what the agent just found ("Alert me if 7.53–7.57 breaks", "Plan the Kimi B+ long"), not the same five strings every time.

**Design**

A) Question-only intents. In llm.rule_intent, add a branch ahead of the follow_up branch (llm.py ~l.824). It uses QUESTION_ONLY = re.compile(r"^\s*(why\b|what (would|could|will) (invalidate|kill|break|stop)|does (the )?(daily|weekly|4h|h4|1d|d1|w1|1w|htf|higher)\b.*\bagree|is (this|that|it) (a )?(good|safe|valid|worth)|how far (is|are)|(is|what'?s|how'?s) (the )?(rsi|macd|ema|vwap|bollinger|stoch|sar|cvd)\b)", re.I). When it matches and nothing 'acting' matched, return AnalysisIntent(features=[], keep_existing=True, timeframe=None, answer_hint=prompt[:200]). merge_overlays (agent.py:114) already keeps every overlay when features is empty. Add the same rule to INTENT_SYSTEM so the LLM planner agrees: a question about the plan, zone or price in FOCUS draws nothing.

B) Focus.
- schemas.py gets class Focus(BaseModel) with kind: Literal['plan','price','zone','scan'], symbol, interval, at (UNIX s), plan: Optional[TradePlan], price: Optional[float], zone_low/zone_high: Optional[float], label: str = '' and symbols: list[str] (max 10). Add AnalyzeRequest.focus: Optional[Focus].
- New frontend/lib/focus.ts has focusFrom(messages, symbol, interval, nowMs): Focus | null. It takes the newest agent message on this symbol in this order: a plan, then facts.price_in_question (price), then setups/scan (symbols). It returns null when that message is older than 24 h.
- ChartWorkspace.runAnalysis sends focus unless the request is silent or the user dismissed it.
- AgentPanel shows a removable chip above the textarea, e.g. 'About: INJ long 7.62 → stop 7.51'. Its X sets a focusCleared message id in ChartWorkspace, which resets on the next answer.
- context_block adds a 'FOCUS:' line for the LLM planner.

C) Facts. New pure helper agent.plan_in_focus(focus, df, result, htf, last, atr). It runs when focus.kind == 'plan', focus.symbol == symbol, it is under 24 h old and the intent builds no new plan. It returns:
- direction, entry, stop and targets;
- entry and stop distance in % and in ATR;
- filled_since, stop_hit_since and t1_hit_since, each from the candles with time > focus.at;
- invalidation: {stop, structure_level: the newest of result.swing_lows below entry, why};
- htf_agree from facts['higher_timeframes'] plus a verdict (agrees / disagrees / mixed against the plan direction);
- the plan's track_record summary.
It is stored as facts['plan_in_focus']. For kind 'price', when the prompt has no price of its own, call ta_agent.price_check(df, focus.price, tf, htf_frames) so 'what if it breaks?' works.

D) Template writer (ta_agent.describe).
- Add _focus_sentences that lead the answer: the invalidation, filled or still waiting, and HTF agreement.
- Add _TOPIC_WORDS entries for invalidat|wrong|stop, agree|daily|weekly|htf and rsi|macd|ema|bollinger|stoch|vwap|sar|cvd. Each gets a sentence from facts['indicators'] / facts['higher_timeframes'] (today RSI is only said at ≤30 or ≥70, and MACD never).
- When the resistance list is empty, say 'no resistance above on H4: open space'.
- If grounded-narrator has landed, add a NARRATE_RULES['plan_in_focus'] paragraph.

E) Next-step chips. New pure backend/app/suggest.py: next_steps(facts, intent, symbol, interval, spot_only) returns at most 4 Suggestion{label, prompt, reason}, ranked by score:
- inside or within 0.3 ATR of a support/demand zone → 'Alert me if {low}–{high} breaks';
- kimi.recent_signals[-1] is a long with bars_ago ≤ 3 → 'Plan the Kimi {label} long';
- momentum.divergence → 'Show swings and the divergence';
- volume.unusual → 'Any news on {coin}?';
- a higher_timeframes trend differs from facts.trend → 'Why does the {tf} disagree?';
- upcoming_events within 24 h → '{title}: what could it do to {coin}?';
- your_position with pnl > 0 and resistance or take_profit within 5% → 'Where do I trim {coin}?';
- agent_desk has a running call → "How is the desk's {tf} call doing?";
- plan.track_record avg_r < 0 → 'Backtest this setup';
- plan present → 'What would invalidate this?'.
No short-side chips in spot mode. Add AnalyzeResponse.suggestions and mirror it in frontend/lib/types.ts. AgentPanel.followUps prefers m.suggestions (reason shown as the tooltip); the current fixed lists stay as the fallback.

Edge cases: a focus on another symbol, or older than 24 h, is ignored. Navigating to another coin drops the focus. Silent auto-level requests never send one.

**Files**: `backend/app/llm.py`, `backend/app/schemas.py`, `backend/app/agent.py`, `backend/app/ta_agent.py`, `backend/app/suggest.py`, `frontend/lib/focus.ts`, `frontend/lib/types.ts`, `frontend/lib/api.ts`, `frontend/components/ChartWorkspace.tsx`, `frontend/components/AgentPanel.tsx`, `frontend/package.json`, `backend/evals/intents.jsonl`, `backend/tests/test_conversation.py`, `backend/tests/test_suggest.py`

**Tests**

- rule_intent returns features=[], keep_existing=True and timeframe=None for 'What would invalidate this?', 'Does the daily agree?', 'Is RSI overbought here?', 'why?' and 'is that a good entry?' (today they return ['support_resistance'] with keep False). Add these as 6 new intents.jsonl lines.
- run_analysis on synthetic INJ 4h: 'Give me a long setup', then 'What would invalidate this?' with focus. Overlay kinds still include plan_entry, plan_stop and plan_target; facts.plan_in_focus.invalidation.stop == plan.stop; the template answer contains the stop price.
- The same request with focus.symbol=SOLUSDT, or focus.at two days old, has no plan_in_focus.
- describe() mentions the RSI value when asked about RSI at 50 and the MACD histogram when asked about MACD.
- suggest.next_steps with fixed facts: inside demand puts the alert chip first; spot_only never gives a short chip; never more than 4.
- Frontend: add vitest as a devDependency with an `npm test` script (there is no frontend test runner today). focusFrom unit tests cover plan, then price, then scan, and the 24 h expiry.

**Critic: modify**

- Problems: Bug reproduced: rule_intent('What would invalidate this?', previous=<long plan>) → ['support_resistance'], keep False; 'Does the daily agree?' → tf 1d; 'Is RSI overbought here?', 'why?' and 'is that a good entry?' also redraw. Part E partly exists: AgentPanel.followUps already varies chips by plan, setups, gridCoins, scan, sells, ladder, walk, sources, Kimi and stoch zone. Frontend gap: ChartWorkspace.apply calls setLastIntent(r.intent) on every prompted answer (l.651). After a question-only answer lastIntent would have features=[] and no trade_plan, so the next 'Same on daily' (a FOLLOW_UPS chip, run through the follow_up branch on previous.features) would draw nothing. The item is L, and its vitest setup is needed by about 9 other items.
- Change: Ship part A (the question-only branch plus the INTENT_SYSTEM line) on its own as the fix. In ChartWorkspace, don't overwrite lastIntent when r.intent.features is empty and keep_existing is set, or have rule_intent carry previous.features and trade_plan into question-only intents. Move the vitest setup into its own prerequisite item. Keep B–E.

**Critic: modify**

- Problems: Part A fixes a real bug. I reproduced it: rule_intent('What would invalidate this?', previous=<long plan>) returns features ['support_resistance'] with keep_existing False; 'Does the daily agree?' sets timeframe 1d; 'Is RSI overbought here?' and 'why?' fall to the default S/R+windows. merge_overlays (agent.py:114) then keeps only custom kinds, so every plan_* overlay is dropped.

The design leaves gaps.
(1) ChartWorkspace.tsx:651 runs `if (prompt) setLastIntent(r.intent)`, so the empty question-only intent becomes previous_intent. The next 'Same on daily' or 'again' then follows previous.features=[] and trade_plan=None (llm.py follow_up branch) and redraws nothing, losing the plan anyway.
(2) The pitch overstates the chip problem. AgentPanel.followUps (AgentPanel.tsx:123-150) already branches on plan, setups, gridCoins, scan, sells, ladder, walk, sources and kimi; it is not 'the same five strings every time'.
(3) invalidation.structure_level is 'the newest swing low below entry'. That level can sit below the stop, where it is meaningless as invalidation, or between stop and entry, and the design doesn't say which.
(4) plan_in_focus computes ATR distances and filled/stop-hit on the current chart's df even when focus.interval differs from the chart interval.
(5) Chips like 'Alert me if 7.53–7.57 breaks' go back through rule_intent and must parse as a zone alert. No eval case covers that.
- Change: Ship A plus the lastIntent fix first, as a standalone S item.
- Backend: when QUESTION_ONLY matches, return previous.model_copy(update={features: [], keep_existing: True, answer_hint: prompt[:200], timeframe: None}) when previous exists, and set AnalyzeResponse.question_only=true.
- ChartWorkspace: skip setLastIntent when question_only, so 'same again' still means the plan.
- plan_in_focus: compute only when focus.interval == the analysed interval; otherwise fetch focus.interval candles. structure_level = the highest swing low strictly between stop and entry, else null with 'stop is the structure'.
- Add intents.jsonl lines for every chip suggest.py can emit, so each chip is proven to parse as intended (alert chips → zone alert, plan chips → trade_plan long).
- Reword E as 'chips derived from facts' layered on the existing contextual followUps, after A ships.

### `grounded-narrator`: Grounded narrator: a slim prompt with a draft, plus a check on every number

Effort **M** · impact **5/5** · proposed by agent-brain

> Ask qwen anything and it answers like a trader, names the coin and says when the numbers are demo data. Every price it writes is one the chart has. A sentence with a made-up number is dropped, and if most sentences are, the built-in answer replaces the model's.

**Design**

1) New backend/app/grounding.py.
- Move _NUM, _leaves and grounded from postmortem.py (postmortem imports them back; behaviour unchanged).
- Add split_sentences(buf) -> (done: list[str], rest: str). It splits on (?<=[.!?])\s+ and on newlines, never inside a number like 7.53, and skips 'e.g.' and 'vs.'.
- Add bad_numbers(text, facts, extra) -> list[str] for logging.

2) Facts.
- run_analysis sets facts['symbol'] = symbol and facts['coin'] = the base asset.
- It sets facts['data_source'] = 'demo (synthetic)' when source == 'synthetic', and does the same on research rows.
- ta_agent._zone_fact adds distance_pct = round(abs(edge - last) / last * 100, 2), so the model never has to calculate a percentage.
- The narrator's facts drop indicators.lengths, and drop session_clock unless the prompt mentions time or sessions.

3) Prompt.
- Split llm.NARRATE_SYSTEM (7.1k chars, 31 FACTS.* paragraphs) into NARRATE_BASE and NARRATE_RULES.
- NARRATE_BASE holds tone, 'only numbers in FACTS or DRAFT', 'lead with what is asked', 'name the coin from FACTS.symbol', 'if FACTS.data_source is set, say these are demo numbers' and the spot line.
- NARRATE_RULES: dict[str, str] is keyed by facts key and holds each existing FACTS.<key> paragraph verbatim.
- narrate_system(detail, keys) returns BASE plus only the paragraphs whose key is in facts.
- The user message becomes CONVERSATION + TODAY + REQUEST + 'DRAFT (correct, built-in): {fallback}' + FACTS, with: 'Answer the REQUEST in your own words. Keep the DRAFT's numbers; add a number only if it is in FACTS.'

4) Guard. narrate_stream(..., on_replace=None) buffers the stream.
- For each complete sentence: if grounded(sentence, facts, fallback), pass it to on_delta(sentence + ' '); otherwise dropped += 1 and log the bad numbers. The rest is flushed through the same check at the end.
- If kept == 0 or dropped > kept, call on_replace(fallback) and return (fallback, "template (model's numbers didn't check out)").
- The engine string becomes f'{provider}:{model}' plus ' · N sentence(s) left out'. If the stream errored after writing text, add ' · cut off'.
- narrate() (non-streamed) applies the same filter to the whole text.
- llm.answer (web search) is not filtered, because it quotes outside numbers.

5) Streaming and UI.
- main.py adds an SSE event {type:'replace', text}.
- api.ts analyzeStream gains handlers.onReplace; ChartWorkspace sets got.written = text.
- The AgentPanel meta line shows '1 sentence left out (number not in the chart data)'.

6) Stats. LLMClient.narration = {answers, sentences, dropped, replaced} over the last 200 answers, returned by health(). StatusPanel's AI row shows 'numbers checked: 98% of sentences kept, 1 answer replaced'.

Edge cases: a sentence with no numbers always passes. Small integers and years are already allowed. The printed-precision tolerance in grounded() covers rounding. Buffering delays the first visible text by one sentence.

**Files**: `backend/app/grounding.py`, `backend/app/postmortem.py`, `backend/app/llm.py`, `backend/app/agent.py`, `backend/app/ta_agent.py`, `backend/app/main.py`, `frontend/lib/api.ts`, `frontend/components/ChartWorkspace.tsx`, `frontend/components/AgentPanel.tsx`, `frontend/components/StatusPanel.tsx`, `backend/tests/test_grounding.py`, `backend/tests/test_postmortem.py`

**Tests**

- The existing postmortem tests pass through the new import.
- split_sentences keeps '7.53–7.57' and 'e.g.' intact.
- A fake streaming LLM writes 'INJ holds 7.53–7.57. It will hit 99.12 next.': only the first sentence reaches on_delta, and the engine says 1 left out.
- An all-ungrounded stream calls on_replace with the fallback.
- narrate_system(detail, {'plan'}) has the plan paragraph and not the Kimi one, and stays under 2.5k chars for a plain chart question.
- Regression guard: narrate_system with every key contains every FACTS.<name> the old prompt had.
- run_analysis facts carry symbol, plus data_source on synthetic data; _zone_fact has distance_pct.
- health() exposes the narration stats.

**Critic: keep**

- Problems: Verified: NARRATE_SYSTEM is 7,149 chars, grounded/_leaves/_NUM live in postmortem.py (l.422-439), and narrate_facts (agent.py:915) has no symbol or data_source. Frontend: the new 'replace' SSE event must be added to analyzeStream's event union (api.ts l.190), and its 404 fallback path must still work. Buffering per sentence also reduces the per-delta localStorage writes (see missing).

**Critic: modify**

- Problems: The facts gaps are real. llm.narrate/narrate_stream (llm.py:1074-1110) send no symbol and no DRAFT, and agent.py never sets facts['symbol'] or facts['data_source']. The slim prompt is also justified: narrate_system('normal') measures 7,149 chars.

The guard is much weaker than the pitch claims. postmortem.grounded (postmortem.py:439) passes any integer ≤100. It also passes any number within half a printed unit of ANY leaf in facts, and facts hold hundreds of leaves: zones, EMAs, VWAP, windows, research, scan rows.
- An invented 'RSI 85', '62% odds' or '3x' always passes.
- On a coin trading at 7.5, a 2-decimal price matches some leaf within ±0.005 almost everywhere.
- It cannot catch mis-attribution, e.g. calling the 7.62 resistance 'support' or the stop 'the entry'.
So 'every price it writes is one the chart has' and 'never invents a number' overpromise. Separately, dropping one sentence can leave the next one dangling ('That level…').
- Change: Keep the facts, DRAFT and NARRATE_BASE/RULES split. Tighten grounding before advertising it:
- Integers ≤100 followed by '%', or within 3 words of rsi|odds|chance|probability|confidence|win, must match a fact leaf ±1.
- Numbers between 0.5× and 2× last_price must match a leaf under a price-like key (low, high, price, entry, stop, target, level, last_price, close, tp) and be printed with at least 3 significant digits. Vaguer ones count as 'rounded' in the stats but are not dropped.
- Add rr and distance_pct to plan, zone and price_in_question facts, so the model doesn't need to compute them.
- When a sentence is dropped and the next one opens with a pronoun (That|This|It|Those), drop that one too.
- Word the UI and StatusPanel as 'numbers checked against chart data', never 'can't invent'.

### `conversation-evals`: Conversation evals that test the agent the way the chat uses it

Effort **M** · impact **4/5** · depends on follow-the-thread · proposed by agent-brain

> Before switching from qwen2.5 to qwen3 or Claude, one click should show something like: 'follow-ups kept the chart 18/20, answers grounded 94%, named the coin 100%, 6.2 s median, prompt peaked at 7.1k of 8.2k tokens'.

**Design**

1) New backend/evals/conversations.jsonl. Each line is {name, symbol, interval, turns: [{prompt, expect: {intent fields, as in intents.jsonl}, chart_keeps: ['plan_*'], chart_has: ['support'], must_mention: ['stop'], must_not: ['short','leverage'], grounded: true}]}. Start with about 20 scripts:
- plan → invalidate / does the daily agree / alert me at the entry / where do I take profit;
- scan → plan on the top coin;
- Kimi → Kimi on the daily;
- price question → what if it breaks;
- open ETH daily → same on 4h;
- a short asked for in spot mode → the sell note.

2) New evals/converse.py with run_conversation(script, llm, mode).
- It calls agent.run_analysis with req.candles from synthetic_klines(symbol, interval, 500, end=FIXED_END), so results don't depend on the clock. Services are None (no desk or Binance).
- It threads overlays (result.overlays become the next turn's req.overlays), previous_intent, history (last 10 role/text turns) and focus (a Python port of focusFrom), exactly as ChartWorkspace.runAnalysis does.
- Per turn it records: the intent engine, overlay kinds, pass/fail per expectation, grounding.grounded(summary, facts, fallback), whether the coin is named, stage latencies (time.perf_counter around planning, detection and narration, via an on_timing hook on run_analysis) and Ollama prompt_eval_count (LLMClient.last_usage from live-thinking, or null).
- CLI: python -m evals.converse [--llm] [--route] prints a table and writes JSON to .cache/evals/.

3) New backend/tests/test_converse.py runs the rules-only mode in CI: fast, deterministic, no model.

4) model_choice.ModelChooser.start_eval(choice, mode='intents'|'agent'). 'agent' runs the conversations after the intent cases in the same background task, with the same progress reporting as EvalRun. The new EvalRun fields are follow_ups_kept, grounded_pct, coin_named_pct, median_latency_s and peak_context.

5) ModelSettings.tsx shows those columns plus a 'Test the full agent (slow)' checkbox.

Edge cases: LLM mode is opt-in and can be cancelled. A missing Ollama gives a clear 'model unreachable' row instead of failing the run.

**Files**: `backend/evals/conversations.jsonl`, `backend/evals/converse.py`, `backend/app/agent.py`, `backend/app/model_choice.py`, `backend/app/main.py`, `frontend/components/ModelSettings.tsx`, `frontend/lib/api.ts`, `backend/tests/test_converse.py`, `backend/tests/test_model_choice.py`

**Tests**

- The rules-only run passes every script (with follow-the-thread merged). The plan-wipe script fails when the question-only branch is reverted, which proves it would have caught the bug.
- run_conversation threads overlays and previous_intent between turns (checked with a stubbed run_analysis).
- The JSON report schema is checked.
- ModelChooser in 'agent' mode with a stub LLM completes and reports grounded_pct and coin_named_pct.

**Critic: keep**

- Problems: Backend and eval work, outside the frontend scope the user narrowed this to; reviewed only for wiring. The ModelSettings columns are straightforward.

**Critic: modify**

- Problems: The rules-only CI script is the right regression guard for the plan-wipe bug.

Problems:
(1) must_not: ['short', 'leverage'] as substrings will false-fail correct answers. The spot note itself ('a short asked for in spot mode → the sell note', SPOT_SHORT_NOTE) and phrases like 'no leverage' contain those words.
(2) The LLM mode depends on LLMClient.last_usage, which only live-thinking adds, but depends_on doesn't list it.
(3) qwen2.5:7b runs at temperature 0.2 (narrate) are nondeterministic, so a single-run '18/20' comparison between models is noise.
(4) The ModelChooser 'agent' mode and ModelSettings columns are a lot of UI for an occasional model switch.
- Change: - Make must_not phrase regexes that target recommendations, e.g. `\b(open|take|enter) (a )?short\b`, `\b\d+(\.\d+)?x leverage\b`.
- Declare depends_on live-thinking for prompt_eval_count, or record null.
- In LLM mode, pin temperature 0 and a seed, and run each script N=3 times, reporting the mean and the spread.
- Ship conversations.jsonl, converse.py and test_converse.py now. Defer the ModelChooser agent mode and the ModelSettings columns to a later round; the CLI table is enough to decide a model switch.

### `rules-first-router`: Rules first, the model only when the rules are unsure, and tool calls that can't fall back to demo data

Effort **M** · impact **5/5** · depends on conversation-evals · proposed by agent-brain

> Tap 'Best spot buys' or 'Where do I take profit?' and the drawings land in about a second. qwen only plans when the regex parser is unsure, and a sloppy tool call like 'INJ' or 'BTC/USDT' can no longer turn the answer into demo candles or a 502.

**Design**

1) llm.py: rule_route(prompt, previous, known, chart) -> Route(intent, confident: bool, reason: str), with intent = rule_intent(...).
- Confident when an explicit action fired (custom_levels, remove, alert_prices/targets, metric_alerts, zone_trigger, scan_watchlist/market, trade_plan, grid_plan, top_down, dip_ladder, sell_check, take_profit, indicators_on/off, asks_kimi, a date question, or the question-only branch), or when an explicit feature word matched.
- Unsure when any of these apply: UNSURE = r"\b(compare|vs\.?|versus|than|relative|should i|what if|better|worse|instead)\b"; two or more distinct coins named (find_symbol over the tokens); the default branch (feats == ['support_resistance','window_levels'] with no feature word); the pronoun follow-up branch; general explain or macro questions.
- reason is a short string like 'explicit: trade_plan' or 'unsure: comparison'.

2) Settings.
- config.py: agent_router from AGENT_ROUTER, defaulting to rules_first when LLM_PROVIDER=ollama and llm_first otherwise.
- model_choice persists a runtime override; GET /api/llm/models returns it and PUT /api/llm/model accepts router.
- ModelSettings.tsx gets a 'Planning' select: 'Rules first, model when unsure (fastest)' or 'Model plans every request'.

3) agent.run_analysis (~l.659). With rules_first and route.confident, set intent = route.intent and intent_engine = 'rules (fast path)', and skip both plan_with_tools and parse_intent. Narration still uses the model. The AgentPanel meta line shows 'plan: rules (fast path)'.

4) Local-model hardening in agent_loop.py.
- _run_tool normalises symbols with symbols.norm_symbol/find_symbol and checks them against await market.list_symbols(). An unknown symbol returns {error: 'unknown symbol X; use a USDT pair like INJUSDT'} without calling the market. Today 'INJ' becomes synthetic candles in auto mode, and a RuntimeError, i.e. a 502, in binance mode.
- Each call is wrapped in try/except Exception returning {error}, so analyze()'s 'Need at least 30 candles' doesn't end the loop.
- A result whose data_source is synthetic while the chart is live becomes {error: 'no live data for X'}.
- plan_with_tools returns LoopResult(intent=None, steps, research) instead of None when draw_on_chart is never called. run_analysis keeps the research and steps and then makes the single parse_intent call.
- _clip trims whole list items until the result fits MAX_RESULT_CHARS, so the JSON stays valid.

5) llm.tool_turn on Ollama.
- When force is a tool name, send only that tool (the Anthropic branch already does this).
- When tool_calls is empty, parse a <tool_call>{json}</tool_call> or a bare {name, arguments} object from the content.

6) evals/run.py --route reports how many cases take the fast path and their pass rate; converse --route does the same per turn.

Edge cases: a confident misread gets no second opinion; the unsure markers and the conversation evals guard against it. With provider none the behaviour is unchanged.

**Files**: `backend/app/llm.py`, `backend/app/agent.py`, `backend/app/agent_loop.py`, `backend/app/config.py`, `backend/app/model_choice.py`, `backend/app/main.py`, `backend/evals/run.py`, `frontend/components/ModelSettings.tsx`, `frontend/components/AgentPanel.tsx`, `backend/tests/test_router.py`, `backend/tests/test_agent_tools.py`

**Tests**

- Router:
  - every SHORTCUTS/SUGGESTIONS phrase in AgentPanel.tsx (copied into the test) is confident;
  - 'compare INJ and SOL', 'is INJ better than SOL?', 'what about it?' and 'what is staking?' are unsure.
- run_analysis with a fake LLM whose tool_turn/parse_intent raise if called answers 'Best spot buys' with engine.intent == 'rules (fast path)'.
- _run_tool:
  - 'INJ' → look('INJUSDT') and 'BTC/USDT' → BTCUSDT;
  - 'XYZ' → an error result with no market call;
  - a tool that raises ValueError gives an error result and the loop continues.
- Ollama tool_turn with force='draw_on_chart' sends exactly one tool (MockTransport payload assert); '<tool_call>{...}</tool_call>' content is parsed into a call.
- _clip output parses as JSON.
- evals --route prints its counts.

**Critic: keep**

- Problems: Backend-heavy; reviewed lightly per the user's 'frontend only' instruction. The frontend part (a ModelSettings select and 'plan: rules (fast path)' in the meta line) is small and fits. The SHORTCUTS and SUGGESTIONS constants copied into the test come from AgentPanel.tsx l.99-118.

**Critic: modify**

- Problems: The cost case is real. LOOP_SYSTEM is 3,550 chars plus 14,734 chars of TOOLS JSON on every request, a heavy load on an 8k context. Step 4 is also confirmed: agent_loop._run_tool only .upper()s the symbol, look() calls get_klines directly, and plan_with_tools' except tuple misses MarketDataError (a RuntimeError).

Risks:
(1) Confident rule misreads now get no LLM second opinion. find_symbol('buy INJ with 500usdt') returns 500USDT (verified), so with the router a confident trade_plan would navigate to a bogus pair and serve synthetic candles.
(2) 'An explicit feature word matched → confident' is too broad. 'Will the 7.5 support hold if BTC dumps?' or 'is this demand safe to buy?' are judgement questions that the tool planner currently researches (market_context, other timeframes).
(3) Nothing measures how often the fast path is wrong in real use.
- Change: - Gate the router on the find_symbol fix (letter-in-base lookahead plus a list_symbols check).
- Extend UNSURE with modal and judgement words: `\b(will|would|could|can|should|hold|safe|worth|good|bad|why|risk|if)\b` and a trailing '?'. A feature word plus a question becomes unsure.
- Ship steps 4 and 5 (tool symbol normalisation, a per-tool try/except, Ollama single forced tool, <tool_call> parsing, and _clip keeping JSON valid) on their own first: they are independent and fix live failures.
- Log route.reason with the answer engine, so misroutes can be seen in Status.

### `live-thinking`: Show progress while the local model works: live steps, a Stop button, warm-up and a context meter

Effort **S** · impact **3/5** · proposed by agent-brain, frontend-ux

> While qwen thinks you see 'Looking at INJ 1d… Reading Kimi on 4h…', a Stop button replaces Send, the first question after lunch doesn't wait on a cold model load, and Status shows tokens/s and how full the 8k context got.

**Design**

1) Step events.
- agent_loop.plan_with_tools(..., on_step: Callable[[str], Awaitable[None]] | None = None) calls on_step(step_text) before each tool and on_step('Planning…') before each LLM round.
- run_analysis also calls it before the context gather ('Reading the chart and N data sources…') and before narration ('Writing the answer…').
- main.py's analyze stream puts {type:'step', text} on the queue. Each step also resets the 90 s idle timer in api.ts.
- analyzeStream gains handlers.onStep. ChartWorkspace keeps stepNote state (cleared on result) and passes it to AgentPanel, whose busy row (AgentPanel.tsx ~l.835) shows the latest step.

2) Stop.
- While p.busy, the send button becomes a Stop button (Square icon, title 'Stop (Esc)') wired to a new onStop prop that calls ChartWorkspace.stopAnswer (exists, ~l.484, currently only reachable through New chat).
- Esc in the textarea also stops.
- A partly written answer keeps its text plus ' (stopped)'.

3) Warm-up.
- config: OLLAMA_KEEP_ALIVE, default '30m'; '0' turns warm-up off for a GPU shared with games. llm.py adds 'keep_alive' to every Ollama payload (none is sent today).
- New POST /api/llm/warm: for Ollama it POSTs {ollama_url}/api/generate with {model, prompt: '', keep_alive} (60 s timeout). It is skipped when a warm-up ran less than 5 min ago or the provider isn't Ollama.
- Frontend lib/api.ts warmModel() runs at most every 10 min, when the Agent tab opens or the prompt box gets focus.

4) Usage.
- Record prompt_eval_count, eval_count and eval_duration from non-streamed Ollama replies and the final streamed chunk (done: true) into LLMClient.last_usage and a rolling peak. Log a warning above 90% of num_ctx.
- health() adds tokens_per_s, context_peak and num_ctx.
- StatusPanel's AI row reads 'qwen2.5:7b · 38 tok/s · context peak 6.9k / 8.2k', amber above 90%.

Edge cases: steps that arrive after an abort are ignored, and warm-up is never sent for other providers.

**Files**: `backend/app/agent_loop.py`, `backend/app/agent.py`, `backend/app/main.py`, `backend/app/llm.py`, `backend/app/config.py`, `frontend/lib/api.ts`, `frontend/lib/status.ts`, `frontend/components/ChartWorkspace.tsx`, `frontend/components/AgentPanel.tsx`, `frontend/components/StatusPanel.tsx`, `backend/tests/test_agent_tools.py`, `backend/tests/test_api.py`, `backend/tests/test_status.py`

**Tests**

- plan_with_tools with a fake LLM that calls look_at_chart twice calls on_step with both 'Looked at…' texts in order.
- The SSE stream (TestClient) emits step events before the result event.
- The Ollama payload carries keep_alive.
- /api/llm/warm:
  - provider none → {skipped: true};
  - Ollama mocked with httpx.MockTransport → exactly one /api/generate call;
  - a second call within 5 min is skipped.
- After a mocked reply with eval_count/eval_duration, health() reports tokens_per_s and context_peak.

**Critic: modify**

- Problems: Confirmed: no keep_alive is sent (llm.py has only num_ctx at l.1223), there are no step events, stopAnswer (ChartWorkspace l.484) is reachable only through New chat, openChat or a market change, and analyzeStream's 90 s idle timer (api.ts l.133) also covers the wait before the first 'result', which a cold qwen with 4 tool steps can exceed. Already present: AgentPanel's busy row shows walkNote (l.835), so stepNote should share that slot. Gap: every existing abort path (New chat, symbol change, and the agent's own navigation; see missing) leaves the message with `streaming: true` forever. That means a pulsing cursor, no Copy button (l.815) and no meta, and it is excluded from the screenshot. The '(stopped)' fix only covers the new button. Esc in the textarea never reaches the global handler (it returns early when typing), so AgentPanel must handle it.
- Change: Write one finalizeStopped(id) in ChartWorkspace that clears `streaming` and appends ' (stopped)'. Call it from stopAnswer, newChat/openChat, the market-change abort and the new Stop button. Use one 'busy note' slot for walkNote and stepNote. Handle Esc in AgentPanel's textarea onKeyDown.

**Critic: keep**

- Problems: Verified gaps:
- No keep_alive is sent in any Ollama payload (grep llm.py).
- stopAnswer exists (ChartWorkspace.tsx:484) but is only reachable through New chat.
- No usage counters are recorded.

Minor: OLLAMA_KEEP_ALIVE=30m pins about 5 GB of the 3070 Ti's 8 GB while Trick may be gaming. An env-only opt-out is easy to miss.
- Change: Keep. Also show the keep-alive choice in ModelSettings ('Keep model loaded: 30 min / 5 min / unload after each answer'), not only as an env var. Warm-up should skip when the last request was under 5 min ago (as designed).

## Your holdings come first

Trick is a spot holder (INJ among others) with a read-only key. Today a coin moved into Simple Earn drops out of the holdings watch and can close its managed trade. Average entries mix INJBTC with INJUSDT fills and ignore BNB fees. Nothing gives one account total, and 'where do I take profit?' lists levels with no sizes. These three items make every money number correct and turn each holding into a plan.

### `holdings-truth`: Earn-aware holdings and a correct cost basis

Effort **M** · impact **5/5** · proposed by portfolio, alerts-automation

> INJ in Simple Earn still counts as held: it keeps its sell alerts, average entry and managed trade. The average entry ignores BTC-quoted fills and includes BNB fees, and 'what should I sell?' checks what you hold, not your watchlist.

**Design**

1) binance_import.positions() adds manual.holdings: one merged row per non-stable asset with {asset, symbol, spot_qty, earn_qty, locked_qty, redeem_at (earliest locked end), qty, avg_entry, value, unrealized_pnl, sources: ['spot','earn']}. An asset held only in Earn also gets a row (today it has no average entry anywhere). average_entry covers min(tracked, spot + earn), not just spot. manual.spot and manual.earn stay for the Account panel.

2) Quotes. `mine` keeps only fills whose quote is in pnl_calendar.USD_QUOTES. Others are listed in notes ('INJBTC fills left out of the average: not USD-quoted').

3) Pairs. _spot_symbols tries {a}USDT, {a}USDC and {a}FDUSD for every spot and Earn asset. Unknown pairs already land in _invalid via code -1121.

4) BNB fees.
- run() builds fee_px: {hour → BNBUSDT 1h close} from market.get_range, but only for hours that have a BNB commission.
- _fee(f, fee_px) converts those commissions to the quote at that hour. The 'not counted' note stays only when no price is found.
- Pass an optional fee_px into build_round_trips, average_entry, pnl_calendar.daily_pnl and coach.trades_from.

5) Consumers.
- HoldingsWatch.coins reads manual.holdings (source 'spot+earn').
- open_position_keys counts holding:spot:{asset} as open when the spot + Earn value is ≥ MIN_OPEN_VALUE, so subscribing INJ to Earn no longer closes its managed trade.
- brief.holdings_lines, agent.holding_facts and portfolio_facts use the merged row, e.g. '120 locked in Earn until 14 Oct'.
- In run_analysis, an unnamed 'what should I sell?' awaits _positions first (cached 30 s) and checks held coins worth ≥ $10. It falls back to the watchlist without a key.

6) UI.
- AccountPanel PositionsView shows one row per asset with Spot/Earn split chips and a totals footer (value, unrealized PnL).
- TradeManagerPanel's Binance picker lists the merged rows, with an Earn badge.

Edge cases: locked Earn shows 'can't sell until <date>' in the sell-check text. Stablecoins in Earn count as cash.

**Files**: `backend/app/binance_import.py`, `backend/app/pnl_calendar.py`, `backend/app/coach.py`, `backend/app/holdings_watch.py`, `backend/app/brief.py`, `backend/app/agent.py`, `frontend/lib/binance.ts`, `frontend/components/AccountPanel.tsx`, `frontend/components/TradeManagerPanel.tsx`, `backend/tests/test_binance_import.py`, `backend/tests/test_holdings_watch.py`, `backend/tests/test_pnl_calendar.py`, `backend/tests/test_trade_manager.py`

**Tests**

- A positions() fixture with INJ at 0 spot and 120 locked Earn gives a manual.holdings row with qty 120 and avg_entry from the INJUSDT fills.
- HoldingsWatch.coins includes that row, and open_position_keys contains holding:spot:INJ.
- An INJBTC fill is left out of avg_entry and noted.
- A fill with a BNB commission and BNBUSDT at 600 that hour lowers realized PnL by commission × 600, and pnl_calendar includes the fee.
- The agent's 'what should I sell?', with a positions stub, checks held coins instead of the watchlist.

**Critic: keep**

- Problems: Backend claims spot-checked: HoldingsWatch.coins reads only manual.spot (holdings_watch.py l.99), and positions() selects fills by base asset only (binance_import.py l.722). The frontend Spot/Earn chips in AccountPanel PositionsView are straightforward. Other consumers (watchlist-badges) should not block on this item.

**Critic: modify**

- Problems: Confirmed:
- HoldingsWatch.coins reads only positions['manual']['spot'] (holdings_watch.py:99).
- open_position_keys builds keys from spot rows only (binance_import.py:794-807).
- _fee returns 'not counted' for BNB (binance_import.py:293).

The quote-mixing claim is mostly theoretical. _spot_symbols (l.566) imports only {asset}USDT plus settings.symbols, bots and cursors, so INJBTC fills appear only if Trick added a BTC pair. The real gap is that INJUSDC and INJFDUSD buys are never imported, so their cost is missing from the average.

Risks the design ignores:
(1) Binance's /api/v3/account often lists Simple Earn Flexible as LD-prefixed assets (LDINJ, LDUSDT). spot_balances (binance_account.py:309) keeps every asset, so positions() would build an 'LDINJUSDT' row. Merging spot and earn could then double-count INJ, and HoldingsWatch would arm alerts on an invalid symbol (value None passes the min_value check).
(2) Trying USDT, USDC and FDUSD for every asset triples myTrades calls (weight 20 each) and can push watchlist symbols out of MAX_SYMBOLS=40.
(3) Earn rewards add quantity with no fill, which inflates untracked_qty or skews avg_entry.
- Change: - Before merging, map any LD<asset> balance to that asset's Earn row and drop it from spot. Verify on the live key and add a fixture.
- Only try quote pairs that exist in exchangeInfo for that base, and only quotes in USD_QUOTES; count them against MAX_SYMBOLS after held coins.
- Treat Earn rewards (earn qty minus net bought) as zero-cost quantity, shown as 'rewards: N INJ'. Compute avg_entry over bought quantity only, and label the PnL 'incl. rewards'.
- Keep the BNB fee pricing, the HoldingsWatch and open_position_keys changes, and the 'what should I sell?' change as designed.

### `portfolio-cockpit`: Portfolio cockpit: account value over time, allocation, and whether trading beat holding

Effort **M** · impact **5/5** · depends on holdings-truth · proposed by portfolio

> One number for everything you own (spot, Earn, stablecoins, bots) with a curve, today/7d/30d change, drawdown, how concentrated you are in INJ, and whether your trading beat just holding.

**Design**

1) New backend/app/portfolio.py: PortfolioService(binance, market, db).
- db.migrate('portfolio', ['CREATE TABLE portfolio_snapshots (ts INTEGER PRIMARY KEY, total REAL, stables REAL, bots REAL, assets TEXT, backfill INTEGER DEFAULT 0)']). assets is JSON {asset: {qty, value}}.
- snapshot(positions) is pure. total = the sum of positions['wallets'] usdt for active wallets: Spot, Funding, Earn and Trading Bots, each already priced by Binance's /sapi/v1/asset/wallet/balance, which positions() already fetches. Without wallets it falls back to holdings + cash + the bot wallet. Per-asset values come from manual.holdings.
- Job: jobs.declare('portfolio', 'Portfolio snapshots', 3600). It runs at :02 each hour when a key is configured. Hourly rows are kept for 14 days, then one per UTC day.
- First-run backfill: new BinanceAccount.account_snapshot(limit=30) → GET /sapi/v1/accountSnapshot?type=SPOT, called once (weight 2400). Each day is priced with get_range(asset+'USDT', '1d') closes and stored as backfill=1, with the note 'spot wallet only'.

2) metrics(rows, now) returns:
- change 24h/7d/30d, in $ and %;
- max_drawdown_30d;
- allocation [{asset, pct}], flagging any non-BTC/ETH/stable coin above 35%;
- vs_hodl_7d/30d: the asset quantities held N days ago valued at today's prices, against today's total. A stablecoin jump of more than 2% that hour with no fills is labelled 'deposit/withdrawal?' and left out;
- vs_btc.

3) API: GET /api/portfolio → {configured, total, change, drawdown, allocation, vs_hodl, vs_btc, updated_at, notes}; GET /api/portfolio/history?days=30 → [{ts, total}].

4) Frontend.
- AccountPanel gets a first tab, 'Overview': a big total, change chips, a stacked allocation bar (top 6 + other, INJ highlighted), a value curve and 'Trading vs holding (30d): +$84' with a tooltip explaining the method. EquityCurve.tsx is generalised to take {time, value}[].
- MarketHeader shows a compact 'Account $X ▲1.2%' chip when a key is set; clicking it opens the Account tab.

5) brief.holdings_lines starts with 'Account $X (+2.1% since the last brief)'. agent.portfolio_facts adds the total and allocation.

Edge cases: without a key the tab shows the setup prompt. The first day shows 'collecting'. A missing price is left out of the total with a note.

**Files**: `backend/app/portfolio.py`, `backend/app/binance_account.py`, `backend/app/main.py`, `backend/app/brief.py`, `backend/app/agent.py`, `frontend/lib/binance.ts`, `frontend/components/AccountPanel.tsx`, `frontend/components/EquityCurve.tsx`, `frontend/components/MarketHeader.tsx`, `backend/tests/test_portfolio.py`

**Tests**

- snapshot() sums the active wallets from a positions fixture.
- metrics() on synthetic rows gives the right drawdown and 7d change, and flags INJ at 48%.
- vs_hodl with fixed quantities and prices; a deposit hour is labelled and excluded.
- Retention keeps hourly rows for 14 days, then daily.
- The backfill uses a mocked accountSnapshot exactly once.
- GET /api/portfolio without a key returns configured:false.

**Critic: modify**

- Problems: EquityCurve.tsx is built for cumulative R: baseline 0, x by trade index, 'No closed trades yet' copy. Journal and backtest use it. Changing it to {time, value}[] breaks those callers, and index spacing would distort a series of hourly rows plus daily backfill rows. Time-based value curves already exist ad hoc in DcaPanel.Curves (l.46) and two SVGs in GridBotPanel. The MarketHeader account chip adds yet another account poll.
- Change: Add a shared time-axis components/ValueCurve.tsx (extracted from DcaPanel.Curves) and use it here, in DcaPanel and in GridBotPanel. Leave EquityCurve as is. Feed the header chip from the shared positions/portfolio store instead of a new poll.

**Critic: modify**

- Problems: Statistics problems:
(1) change_24h/7d/30d and max_drawdown_30d computed on raw account value count every withdrawal as a loss and every deposit as a gain. The 'stablecoin jump >2% with no fills' heuristic misses coin deposits, transfers to Funding, P2P and Earn rewards.
(2) The accountSnapshot backfill is SPOT-wallet only, while live rows sum Spot, Funding, Earn and Trading Bots. Splicing them creates a fake step at the seam that corrupts the 30d change, the drawdown and vs_hodl_30d for the first month, the exact window the feature shows.
(3) vs_hodl mixes scopes the same way, and counts Earn rewards as 'trading beat holding'.
- Change: - Use the read-only cash-flow endpoints (/sapi/v1/capital/deposit/hisrec, /sapi/v1/capital/withdraw/history, and the Earn rewards history) to compute a time-weighted return. Show change as both '$ value change' and 'return excl. deposits/withdrawals'. Drawdown is computed on the TWR curve.
- Store the backfill rows but draw them as a separate dashed 'spot wallet only' series, and exclude them from every metric.
- vs_hodl only uses non-backfill rows with the same holdings scope at both ends. Rewards are reported separately as income.
- Keep the 35% concentration line as information, not a warning.

### `exit-plan`: An exit plan for every coin held: scale-out rungs sized from your cost basis, plus the line where the idea is wrong

Effort **M** · impact **5/5** · depends on holdings-truth · proposed by portfolio, agent-brain

> For each coin you hold: 'TP1 26.40: sell 25% ≈ 30 INJ ≈ $790, +23% on your 21.40 entry', more rungs above, and 'wrong on a daily close below 18.90', drawn on the chart and armed as Discord alerts with one click.

**Design**

1) New backend/app/exit_plan.py. It builds on the existing agent.take_profits (agent.py:527). Move that function to exit_plan.py and import it back into agent.py, so nothing is duplicated.

2) build_exit_plan(holding, df_4h, df_1d, profile: 'cost_out'|'quarters'|'thirds') -> ExitPlan{symbol, qty, sellable_qty, avg_entry, rungs: [ExitRung{price, low, high, label, sell_qty, sell_pct, usdt, pnl_vs_entry_pct, gain_from_now_pct}], invalidation{price, label, rule: 'daily close below'}, notes}.
- Rungs are take_profits rows at least 1 ATR(4h) apart, capped at +35%. With no structure above (open space), use last + k·2·ATR(1d) and note 'spaced by ATR'.
- cost_out: rung 1 sells cost/price coins (your money back), and the rest is split across the higher rungs.
- quarters: 25/25/25 with a 25% runner. thirds: 33/33/34.
- Invalidation: the nearest D1 support/demand below price from sell_check._zones on the daily, with the rule 'daily close below the zone low'.
- A coin under its average entry gets the note 'under water: trims into resistance first'.
- Locked Earn qty is left out of sellable_qty until redeem_at.

3) Storage: an exit_plans table via db.migrate (symbol PK, profile, json, created, rungs_done JSON).

4) API.
- POST /api/exit-plan {symbol, profile, qty?, avg_entry?}: the manual overrides let it work without a key.
- GET /api/exit-plans and DELETE /api/exit-plans/{symbol}.
- POST /api/exit-plans/{symbol}/arm creates one cross price alert per rung through AlertService.add, labelled e.g. 'Exit INJ TP1: sell 25% ≈ $310'. It also creates a 1d lost_support signal alert via SignalAlertService with owner f'exit:{symbol}'.

5) Done tracking: after each Binance import, a manual sell within 0.5% of a rung's price, after the plan was made, marks that rung done.

6) Frontend.
- Each AccountPanel holdings row gets an 'Exit plan' button that opens ExitPlanCard: a profile select, a rung table (done rungs struck through), 'Arm alerts' and 'Show on chart'.
- 'Show on chart' calls onChartOverlays('exit:'+symbol, symbol, exitOverlays(plan)): dashed TP lines labelled with size, and a red invalidation line. layers.panelLayer maps 'exit' to 'plan'.

7) Agent: a take_profit intent on a held coin adds facts['exit_plan'], and describe_take_profits uses its sizes.

Edge cases: a coin not held gives today's plain take-profit answer. Dust under $10 gets no plan.

**Files**: `backend/app/exit_plan.py`, `backend/app/agent.py`, `backend/app/main.py`, `backend/app/alerts.py`, `backend/app/signal_alerts.py`, `backend/app/binance_import.py`, `frontend/components/AccountPanel.tsx`, `frontend/components/ExitPlanCard.tsx`, `frontend/lib/binance.ts`, `frontend/lib/layers.ts`, `backend/tests/test_exit_plan.py`

**Tests**

- build_exit_plan on synthetic frames with qty 120 and avg 21.4:
  - rungs ascend and are at least 1 ATR apart;
  - the sell quantities sum to no more than sellable_qty;
  - in cost_out, rung 1 qty × price ≈ qty × avg;
  - the invalidation is below price;
  - locked Earn qty is excluded;
  - open space gives ATR rungs with the note.
- arm creates N cross alerts plus one signal alert with owner exit:INJUSDT, and re-arming doesn't duplicate them.
- A sell fill at a rung's price marks the rung done.
- Agent facts include exit_plan for a held coin.

**Critic: keep**

- Problems: Backend-heavy; reviewed lightly. Frontend: the 'exit:' panel key needs a panelLayer mapping, as the design says. ExitPlanCard's 'Arm alerts' should use the shared PlanActions/alert helpers.

**Critic: modify**

- Problems: The idea is high value for a spot holder, but the maths and alerts have holes.
(1) In cost_out, rung 1 sells cost/price coins. When TP1 sits below the average entry (an under-water holder; INJ is plausibly one), that quantity is ≥ qty and the plan sells everything at a loss.
(2) Rungs are capped at +35%, so an under-water holder never sees a break-even rung.
(3) Invalidation is described as 'daily close below 18.90' but armed as a generic 1d lost_support SignalAlert. That alert fires on whatever support the detector picks at that close, not on 18.90, so the alert and the plan can disagree.
(4) HoldingsWatch already arms lost_support for held coins (holdings_watch.py SIGNALS, owner 'holdings'). With its interval set to 1d, the same close pings twice.
(5) The nearest D1 zone below price can be under 1 ATR away, which turns daily noise into 'you're wrong'.
(6) Binance spot minimum notional (about $5) and LOT_SIZE make small rungs unsellable.
- Change: - cost_out: rung1_qty = min(sellable, cost/price). If price1 < avg_entry, fall back to 'quarters' with a note.
- Every rung below avg_entry shows 'realizes −$X vs your average'. Add an avg-entry marker rung when it lies above price.
- Arm invalidation as a level_close (1d, below, zone_low) alert from close-confirmed-alerts. Until that lands, use a cross alert labelled 'check the daily close'. Skip arming it when a holdings lost_support on 1d already covers the coin.
- Require invalidation ≥ 1 ATR(1d) below price; otherwise use the next zone down.
- Merge rungs whose usdt is below max($10, minNotional); round sell_qty down to stepSize from exchangeInfo.

## The desk and Kimi in every decision

The desk learns edges, and Kimi Cooked has its own reach odds, factor weights and take/skip filter, but all of it stays in its own tab. Desk WebSocket events are dropped by the frontend. Chat plans quote only a backtest line. A desk call pings hours before price reaches the zone. Kimi labels are bare letters with no reasons, even though the engine records all ten confluence factors per signal. This theme puts those numbers on the chart, the plan card and the crosshair, where Trick makes his buy decisions.

### `desk-on-chart`: The desk on your chart, live

Effort **M** · impact **5/5** · depends on notification-center · proposed by frontend-ux, learning

> When the desk calls INJ 4H, its buy zone, take-profit and invalidation are already on your INJ chart with a chip reading 'Desk 4H · waiting to buy · 62%', and the Desk tab refreshes the moment it happens instead of up to 30 s later.

**Design**

1) New hook frontend/hooks/useDeskCalls.ts(symbol).
- fetchDeskCalls in lib/desk.ts gains a symbol param; the backend /api/desk/calls already accepts symbol=.
- The hook loads non-shadow active calls on symbol change, every 60 s while the document is visible, and on the window event 'ac:desk-changed' (dispatched by notification-center).

2) ChartWorkspace calls onChartOverlays('desk:active', symbol, calls.flatMap(deskOverlays)). deskOverlays already gives deterministic ids (desk-zone-<id>), so composeOverlays dedupes against 'desk:selected'. 'desk:active' is kept out of the persisted ac:panel-overlays because it is refetched live.

3) lib/layers.ts adds LayerId 'desk' ('Desk calls', group 'Chart agent'). The 'desk_zone' and 'desk_level' kinds go to it, and panelLayer maps keys starting with 'desk:' to 'desk' (today they fall into 'panels'). The Layers eye toggle then hides them.

4) AgenticChart's chip row (~l.1607, next to the Session/Heatmap chips) shows one chip per call: 'Desk 4H · Waiting to buy · 62%', toned by status with STATUS_LABEL. Clicking it opens the Desk tab and dispatches CustomEvent('ac:desk-select', {callId}); DeskPanel listens and calls select(callId).

5) DeskPanel fixes.
- Reload at once on 'ac:desk-changed'.
- The selected call's overlays get an owner and are cleared when the call closes; today they persist across reloads with no label.
- The min-confidence slider saves on pointerup instead of on every step.
- Settings patches are computed from the latest state, so two quick checkbox clicks don't lose the first.

6) Backend: after each score() run that changed a status, AgentDesk broadcasts {type: 'desk_scored'} so open apps refresh without a toast.

Edge cases: ratio and index charts show no calls. Calls on other desk timeframes are drawn with their TF in the label.

**Files**: `frontend/hooks/useDeskCalls.ts`, `frontend/lib/desk.ts`, `frontend/lib/layers.ts`, `frontend/components/ChartWorkspace.tsx`, `frontend/components/AgenticChart.tsx`, `frontend/components/DeskPanel.tsx`, `backend/app/agent_desk.py`, `backend/tests/test_agent_desk.py`

**Tests**

- Backend: a score() run that changes a call's status broadcasts desk_scored to a fake AlertService; /api/desk/calls?symbol=INJUSDT returns only INJ calls.
- Frontend (vitest): panelLayer('desk:active') === 'desk'; overlayLayer of desk_zone is 'desk'; deskChip(call) gives the label and tone per status.

**Critic: modify**

- Problems: Confirmed: desk WS messages are dropped (useAlerts.ts). panelLayer maps 'desk:*' to 'panels' (layers.ts). DeskPanel's slider calls save() on every range onChange (l.383). Settings.save builds from the `s` prop, so two quick clicks can lose the first (l.331). The selected call's overlays persist in ac:panel-overlays. Feasibility problems: (1) 'ac:desk-select' sent to DeskPanel is lost when the tab was never opened, because Dock mounts tabs lazily. (2) AgenticChart's chip row is `pointer-events-none` (l.1606), so the chips can't be clicked without changing the row, and the chart needs a new prop (through ChartCell) to receive calls. (3) Keeping 'desk:active' out of persisted panels means a separate non-persisted overlay set merged in composeOverlays for both active and passive cells; ChartCell composes passive overlays from `panels` itself. (4) A new 60 s useDeskCalls, plus watchlist-badges' 60 s fetch, plus DeskPanel's 30 s REFRESH_MS, means three pollers on /api/desk/calls. (5) Calls with data_source 'synthetic' should look like demo calls. (6) The TP and stop line labels from deskOverlays have no TF (only the zone label does).
- Change: Build useDeskCalls on usePolled and share it (one poll for all active non-shadow calls, filtered per symbol; refresh on ac:desk-changed). Pass 'desk:active' as a non-persisted extra panel set into composeOverlays/ChartCell. Make individual chips pointer-events-auto and route the click through the workspace pendingFocus state instead of a window event. Add the TF to the desk TP and stop labels. Mark demo calls yellow. Keep the DeskPanel fixes, using pointerup and a functional settings merge.

**Critic: modify**

- Problems: Confirmed:
- useAlerts.ts handles snapshot, signal_snapshot, fired, signal_fired, trade_advice and history, and drops 'desk' even though agent_desk._notify broadcasts it (agent_desk.py:488).
- layers.panelLayer has no desk layer.

Misleading-number risk: DeskCall.confidence is the chance of the take-profit before the invalidation ONCE BOUGHT (desk_calls.py, field description). A chip reading 'Desk 4H · Waiting to buy · 62%' reads as '62% this works', ignoring the fill probability and the random baseline.

The desk analyses 300 bars and the chart 500 (find_swings distance = n//60), so desk boxes and chart zones for the same area often differ by a few ticks and look like two different levels.
- Change: - Chip text: 'Desk 4H buy zone · waiting · 62% if filled (random 45%)'. Add 'uncalibrated' when the desk's calibration has fewer than MIN_CAL_CALLS. Use the call's random_odds from features or estimate().
- Draw desk boxes with a distinct dashed border and a 'Desk' prefix.
- Sequence after the zone-consistency fix (one ZONE_BARS) so desk and chart zones agree.
- Keep the rest: the hook, the layer, the DeskPanel fixes, and desk_scored.

### `desk-odds-on-plans`: Desk odds on every long plan and scanner row

Effort **S** · impact **4/5** · proposed by learning

> Ask for a long on INJ and the plan card reads 'Desk odds 41% to T1 before the stop (random 29%, 1.4x on H4 fresh demand with D1 behind, 63 zones) · learned TP 85% of the way'. The scanner ranks spot buys on it too.

**Design**

1) AgentDesk.odds_for(symbol, interval, plan, trends, track) -> DeskOdds | None. It returns None unless plan.direction is long, plan.zone_kind is in SPOT_KINDS and interval is in DESK_TIMEFRAMES.
- agree = agree_level of the trends ratio.
- bucket = desk_calls.bucket_of(interval, plan.zone_kind, plan.zone_fresh, bool(plan.zone_htf), agree).
- rr_level = (t1 − entry) / (entry − stop).
- est = desk_learning.estimate(symbol, interval, kind, bucket, rr_level, track, learnable(self._calls.values(), self.demo_ok), now, demo_ok).
- tp = desk_learning.take_profit(entry, stop, t1, est.p, plan.risk_pct, interval, kind, history, now).
- Returns {p, random_odds, lift, n_zones, levels: [{name, lift, n}], basis: est.basis, tp_price, tp_fraction, expected_r}.
The desk builds its candidates with the same trade_plan.build_plan('long', ...), so a chat plan on the same zone gets the same entry, stop and bucket.

2) schemas.py: new DeskOdds; TradePlan.desk_odds: Optional[DeskOdds], mirrored in types.ts.

3) agent.py, after build_plan and the track record (~l.794):
- trends = [result.stats.trend] + [desk_calls.trend_of(f) for f in htf_frames.values()];
- plan.desk_odds = desk.odds_for(...);
- facts['plan']['desk_odds'] = {p, random, lift, n};
- one describe sentence, plus a NARRATE rule: 'quote desk odds with their n; under 10 zones say too early'.

4) market_scanner: in main.py, set app.state.market_scanner.desk = app.state.desk. Fill desk_odds for the top 20 spot setups. best_spot sorts by expected_r when n_zones ≥ 20 and by the current score otherwise.

5) Frontend.
- The AgentPanel PlanCard gets one line under TrackRecordLine: 'Desk odds 41% (random 29%) · 1.4x · 63 zones'. Hovering it shows the edge chain levels and 'Learned TP: 85% of the way (26.10)'.
- With n < 10 the line reads 'Desk: too early (7 zones)'.
- ScannerPanel rows show a '41%' pill.

**Files**: `backend/app/agent_desk.py`, `backend/app/schemas.py`, `backend/app/agent.py`, `backend/app/ta_agent.py`, `backend/app/market_scanner.py`, `backend/app/main.py`, `frontend/lib/types.ts`, `frontend/components/AgentPanel.tsx`, `frontend/components/ScannerPanel.tsx`, `backend/tests/test_agent_desk.py`, `backend/tests/test_market_scanner.py`

**Tests**

- odds_for returns None for a short plan, for 15m, and for a plan with no zone_kind.
- Drift guard: build a desk Candidate on a synthetic df, pass its plan to odds_for, and assert the bucket, entry and stop match the candidate.
- p equals estimate().p for the same inputs.
- run_analysis with a desk stub puts desk_odds in plan and facts.
- The scanner fills desk_odds for its top rows and orders by expected_r once n ≥ 20.

**Critic: keep**

- Problems: Backend; reviewed lightly. The frontend part (one PlanCard line under TrackRecordLine and a ScannerPanel pill) fits the existing components.

**Critic: modify**

- Problems: (1) desk_learning.estimate sets p = random_odds(rr)×lift = lift/(1+rr) (l.152, until the MAX_P cap). Gross expected R is therefore p·rr − (1−p) = lift − 1, independent of the target, and net expected_r = lift − 1 − fee_r(risk_pct). Sorting best_spot by expected_r once n ≥ 20 is just sorting by bucket lift, with ties broken toward WIDER stops (smaller fee_r). That adds no information and biases the scanner toward wide-stop setups.
(2) The 'same plan' claim is false. The desk calls build_plan('long', …, [zone]+resist, …) on 300 bars (desk_calls.candidates), while the chat plan uses all levels on 500 bars, so the entry, stop and even the zone can differ. The drift-guard test only checks the desk's own candidate against itself.
(3) A chat plan outside the desk's sample (distance > MAX_DIST_ATR=4, rr < MIN_RR) would get extrapolated odds.
(4) The odds are conditional on a fill, and the card doesn't say so.
- Change: - Show the odds line only. Do not re-rank the scanner by expected_r; keep the current score, optionally with a lift lower confidence bound as a tiebreak.
- odds_for computes bucket and rr_level from the chat plan's own zone and targets, and returns None (UI: 'outside the desk's sample') when distance_atr > MAX_DIST_ATR or rr < MIN_RR.
- Label the line 'Desk edge for H4 fresh demand, D1 behind: 41% to T1 before stop if filled (random 29%, n=63)'.
- Replace the drift test with a check that a chat plan on the desk's zone yields the same bucket key.

### `desk-zone-trigger`: Desk calls arm a lower-timeframe confirmation in their zone

Effort **S** · impact **4/5** · proposed by alerts-automation, learning

> When the desk calls 'buy INJ 7.10–7.25', a 15m confirmation trigger is armed inside that zone. The second ping comes when a 15m candle actually confirms there: 'Desk zone confirmed on 15m: limit 7.25, TP 8.20 (+13%), wrong below 7.02, 58%'.

**Design**

1) Settings and wiring.
- DeskSettings.arm_triggers: bool = True, with a checkbox in DeskPanel's settings.
- AgentDesk.signals: SignalAlertService | None, set in main.py with app.state.desk.signals = app.state.signal_alerts (signals are created before the desk).

2) SignalAlertService.
- add_trigger(spec, owner: Optional[str] = None) stores the owner; SignalAlert.owner already exists.
- New remove_owned(owner) deletes alerts with that owner.
- New register_context(prefix, fn) lets the desk supply extra text for its alerts.

3) When a non-shadow call is created (after AgentDesk._place), the desk arms ZoneTriggerSpec(symbol, interval={'1h':'5m','4h':'15m','1d':'15m'}[c.interval], zone=TriggerZone(source='fixed', price_low=c.zone_low, price_high=c.zone_high, direction='long', timeframe=c.interval, label=f'Desk {TF} buy zone'), confirm='any', cooldown_min=60, repeat=False), with owner=f'desk:{c.id}'.

4) When the trigger fires, signal_alerts._fire appends the desk's context line for owners starting with 'desk:': 'limit {entry}, TP {tp} ({tp_pct:+.0f}%), wrong below {stop}, {conf}%'.

5) Cleanup.
- In AgentDesk.score(), when a call becomes open (filled), tp, invalidated, expired, timed_out or cancelled, call signals.remove_owned(f'desk:{c.id}').
- AgentDesk.start() removes desk:* triggers whose call is no longer active.
- Demo/synthetic calls never arm triggers.

6) Frontend: AlertsPanel's Triggers tab shows a 'Desk' badge for owner desk:*, with an 'Open call' link that dispatches ac:desk-select.

**Files**: `backend/app/agent_desk.py`, `backend/app/signal_alerts.py`, `backend/app/main.py`, `frontend/lib/desk.ts`, `frontend/lib/alerts.ts`, `frontend/components/DeskPanel.tsx`, `frontend/components/AlertsPanel.tsx`, `backend/tests/test_agent_desk.py`, `backend/tests/test_signal_alerts.py`

**Tests**

- A new non-shadow 4h call creates exactly one zone trigger on 15m with owner desk:<id>; a shadow call creates none; arm_triggers=False creates none.
- The status moving to open, tp or invalidated removes the trigger.
- Start-up removes an orphan desk:* trigger.
- The fire text includes the call's limit, TP and invalidation.

**Critic: keep**

- Problems: SignalAlert.owner and SignalAlertService.sync_owned (signal_alerts.py l.133, l.428) already exist, so remove_owned can be a thin wrapper. The frontend 'Open call' link should use the workspace pendingFocus, not ac:desk-select (lazy dock mount).

**Critic: modify**

- Problems: As designed, it almost never fires. DeskCall.entry is a limit at the TOP of the buy zone (desk_calls.py:74), and AgentDesk re-scores every CHECK_SECONDS=30 (agent_desk.py:61) on SCORE_RES candles. The moment price touches the zone top, the call becomes 'open'. Step 5 removes the trigger on 'open', which is exactly when price is inside the zone and a 15m confirmation could form.

The ping text 'limit 7.25, TP 8.20, wrong below 7.02, 58%' also attaches the desk's confidence, measured for the limit-at-top entry, to a different entry: the confirmation close with its own suggested stop. That is a misleading number.
- Change: - Keep the trigger armed through 'waiting' and 'open'. Remove it after it fires once, or when the call reaches tp, invalidated, timed_out, expired or cancelled, or when its hold window ends.
- Fire text: '15m confirmation inside the desk's H4 buy zone 7.10–7.25 (close 7.18, suggested stop 7.02). Desk limit {filled at 7.25 | still waiting}.'
- Don't quote the desk's confidence on the confirmation ping; link to the call instead.
- Add a test where a fill happens before the confirmation candle and the trigger still fires.

### `kimi-inspector`: Kimi signal inspector: hover a label to see why it fired, click to act on it

Effort **M** · impact **5/5** · proposed by kimi

> Hover a B+ and see its confluence factor by factor (near S/R ✓, HTF anchor ✓, volume ✗, each with its weight), its tier, Kimi's own target and stop drawn as a bracket, how it ended, and whether Kimi + Agent would take it. One click paper-buys it, logs it to the journal or arms an alert at its entry.

**Design**

1) Backend. kimi_service._signals extends KimiSignal with:
- id = f'{typ}:{dir}:{bar}';
- factors: list[KimiFactor{name, present, weight}]. The engine's Signal.mask bits follow the fac order at kimi_v574.py ~l.919: 'Near S/R', 'Retest', 'Chart pattern', 'Harmonic', 'HTF anchor', 'RSI extreme', 'Drift', 'Volume', 'HTF divergence', 'Multi-oscillator'. Weights come from res.final['factor_weights'];
- tp_price = entry + dir·tp and sl_price = entry − dir·sl (Signal.tp/.sl are price distances). When tp is NaN, use atr·eff_stat_win and flag tp_estimated;
- expiry_time (from bar + exp), resolved_time (from res_bar, null while open) and p_random.
The same change fixes the verdict collision: kimi_agent.signal_filter and kimi_service key verdicts by (bar, typ, dir) instead of bar. schemas.py and types.ts mirror the fields.

2) Hover. AgenticChart gives each Kimi marker id = s.id. In onMove, param.hoveredObjectId (LWC 4.2.3 SeriesMarker.id / MouseEventParams.hoveredObjectId, both checked in the typings) sets kimiTip {x, y, signal}. It renders, like eventTip (~l.1618), as the new KimiSignalCard.tsx:
- label, type, tier and confluence;
- a factor list with ✓/✗ and weight bars;
- result and R;
- the agent's take/skip with its predicted R;
- 'random entries won 38% here' from p_random.

3) Click (subscribeClick + hoveredObjectId) pins the card and calls KimiPrimitive.setBracket({start: confirm_time, end: resolved_time ?? expiry_time, entry, tp, sl}), which draws translucent green (entry→TP) and red (entry→SL) boxes.

4) Card actions for long labels. New lib/kimiPlan.ts kimiPlan(signal) builds a TradePlan from entry/sl/tp.
- Paper buy: placePaperOrders(planToPaperOrders(plan, symbol, sizePlan(...).qty)).
- Log to journal: createJournalEntry(planToJournalEntry(...)) with source 'kimi'. The backend JournalEntry.source already allows 'kimi' but nothing creates such entries.
- Alert at entry: addAlerts cross.
Short labels (B-/Dn) are framed as trim signals and only offer Alert.

5) Labels older than the first loaded candle can't be hovered. The card says 'as of this run' because the confluence of older labels shifts as the window slides. On mobile a tap opens the card.

**Files**: `backend/app/kimi_service.py`, `backend/app/kimi_agent.py`, `backend/app/schemas.py`, `frontend/lib/types.ts`, `frontend/lib/kimiPlan.ts`, `frontend/lib/chart/primitives/KimiPrimitive.ts`, `frontend/components/KimiSignalCard.tsx`, `frontend/components/AgenticChart.tsx`, `backend/tests/test_kimi.py`, `backend/tests/test_kimi_agent.py`

**Tests**

- Ids are unique per (bar, typ, dir).
- Collision regression: two signals on the same bar (B+? and B-?) keep separate verdicts.
- factors has 10 entries with the right names, and present flags match the mask bits.
- tp_price/sl_price are on the correct side for both long and short.
- KimiResponse round-trips through the schema.
- vitest: kimiPlan(long) gives entry/stop/target, and kimiPlan(short) in spot mode gives null.

**Critic: modify**

- Problems: Verified: the verdict collision is real (kimi_agent.signal_filter l.~258 `out.verdicts[s.bar]`, kimi_service._signals reading verdicts[s.bar]). The mask bit order matches fac in kimi_v574.py l.919-922. LWC 4.2.3 typings have SeriesMarker.id and MouseEventParams.hoveredObjectId. Problems: (1) The click design uses chart.subscribeClick, but AgenticChart deliberately avoids subscribeClick (comment at l.1254-1256: LWC turns a fast second click into a dblclick) and handles taps through tapRef. A second click path would fight drawing and level picking. (2) res.final['factor_weights'] holds the current adaptive weights, not the weights when the signal fired, so per-signal weights are misleading. (3) Paper buy, journal and alert duplicate PlanCard. (4) Like eventTip, the hover card is React state set on every mousemove in a 1,649-line component.
- Change: Store param.hoveredObjectId in a ref inside onMove, and in tapRef's crosshair branch open or pin the card when that ref holds a Kimi id. Don't use subscribeClick. Label the weights 'current factor weights'. Render the actions with the shared PlanActions. Keep the backend parts, including the (bar, typ, dir) verdict key.

**Critic: modify**

- Problems: Confirmed:
- Verdicts are keyed by bar (kimi_agent.py:258, kimi_service.py:162).
- The factor order matches fac[] at kimi_v574.py:919.
- MouseEventParams.hoveredObjectId and SeriesMarker.id exist in the installed LWC 4.2.3 typings.

Risks:
(1) The card offers 'Paper buy', 'Log to journal' and 'Alert at entry' on any long label, including ones resolved or expired long ago. That places hindsight trades at stale prices.
(2) Labels are drawn at pivot_bar (kimi_service._signals sets time=times[s.pivot_bar]) but were only confirmed at s.bar. The card shows outcome and R, so a trader reads it as 'this was knowable at the pivot'.
(3) res.final['factor_weights'] are the latest weights. An older signal's confluence was computed with the weights of its own bar, so the weight bars won't sum to its stored confluence.
(4) p_random is the decayed baseline at the end of the run, not 'random entries won 38% here'.
- Change: - Enable actions only on the newest long signal with result 'open' and confirm_time + exp bars still in the future; others show 'historical'.
- Add a line 'label at pivot, confirmed N bars later (time)'.
- Show factors as ✓/✗ plus the stored confluence score; label the weights 'current weights'.
- Phrase p_random as 'recent random-entry baseline: 38%'.
- Keep the bracket, the hover and the (bar, typ, dir) verdict key.

### `kimi-odds-anywhere`: Kimi's reach odds at any price: crosshair, plan targets, alerts and price questions

Effort **M** · impact **4/5** · proposed by kimi

> Hover any price and the crosshair reads 'Kimi 34% within 20 bars'. Every plan target and alert edit shows the same number, and answers to 'what's at 7.15?' include it.

**Design**

1) Backend.
- kimi_service.compute adds KimiResponse.reach = {close, sig, horizon, lean (last forecast end − born), lean_agent (when Kimi + Agent is active), n: rch[40], up: rch[0:20], down: rch[20:40]}, taken from res.final and last_forecast().
- kimi_service.odds(k, price, use_agent=False) ports KimiCooked.level_odds exactly (kimi_v574.py ~l.1245), including the n < 50 logistic fallback.
- ta_agent.price_check gains an optional kimi_odds param. When Kimi facts are loaded (want_kimi), agent.py passes it, which sets facts.price_in_question.kimi_odds_pct.
- NARRATE rule: 'Kimi odds are the chance of reaching the price within N bars, not a win rate.'

2) Frontend.
- lib/kimiOdds.ts holds the same math.
- AgenticChart onMove: when Kimi is on and data.reach exists, a small readout 'Kimi 34% / 20 bars' sits beside the crosshair's price label (absolute div at that y).
- The AgentPanel PlanCard shows 'Kimi reach: T1 41% · T2 18% (within 20 bars, not a win rate)' when the answer's chart has Kimi data. ChartWorkspace stores the values on the message at answer time through activeChart().getKimi()?.reach, a new handle method.
- The AlertsPanel AlertEditor shows the odds for the alert price when cached Kimi data matches its symbol and interval.

Edge cases: more than 5σ√H away reads '<1%'; with Kimi off there is no readout; the agent lean is used only when the forecast correction is active.

**Files**: `backend/app/kimi_service.py`, `backend/app/schemas.py`, `backend/app/ta_agent.py`, `backend/app/agent.py`, `backend/app/llm.py`, `frontend/lib/kimiOdds.ts`, `frontend/lib/types.ts`, `frontend/components/AgenticChart.tsx`, `frontend/components/ChartCell.tsx`, `frontend/components/AgentPanel.tsx`, `frontend/components/AlertsPanel.tsx`, `backend/tests/test_kimi.py`, `backend/tests/test_price_question.py`

**Tests**

- kimi_service.odds equals KimiCooked.level_odds for 10 prices on the BTC fixture, covering both the n ≥ 50 and n < 50 paths.
- reach arrays have length 20.
- price_check includes kimi_odds_pct when given.
- vitest: kimiOdds.ts matches the backend values on a saved KimiResponse fixture to 1e-9.

**Critic: keep**

- Problems: KimiCooked.level_odds exists (kimi_v574.py l.1245) and is only used for S/R and Fib odds inside last_forecast; arbitrary prices are not exposed. Porting it to Python and TypeScript needs the parity test the design includes. Frontend note: the crosshair readout should update a DOM node through a ref, not React state, to avoid re-rendering AgenticChart on every move. ChartWorkspace needs a new handle method (getKimi) to read reach at answer time.

**Critic: modify**

- Problems: The port of level_odds (kimi_v574.py:1245) is faithful. A crosshair readout is genuinely useful for a spot buyer when read as 'odds a limit buy at X fills within N bars'.

On the PlanCard, though, 'Kimi reach: T1 41%' is a touch probability that ignores the stop (price can hit the stop first and then T1). It would sit next to the desk's 'T1 before stop if filled' and the backtest win rate: three different percentages for the same target on one card. That is a recipe for misreading the edge.
- Change: - Keep the crosshair readout, worded 'Kimi: 34% touch within 20 bars'.
- Keep the AlertEditor odds ('chance this alert fires within N bars') and price_in_question.kimi_odds_pct.
- Drop the PlanCard line, or move it into a tooltip worded 'touch odds, stop ignored'.
- NARRATE rule: never put a Kimi reach % next to a win rate or desk odds in the same sentence.

## Discord pings worth opening

Every notification today is one plain `content` string. Each producer POSTs on its own with no 429/Retry-After handling and no allowed_mentions, so '@everyone' in a note pings the server. Nothing tracks whether a message arrived, so Status shows 'connected' while every post fails. A stop being hit looks the same as a market scan. Trick lives in Discord, so these items make each ping a readable card with a chart and a link back into the app, deliver it reliably during the 00:00 UTC close burst, and respect sleep.

### `alert-cards`: Alert cards: Discord embeds, a link back into the app, and a reliable delivery queue

Effort **L** · impact **5/5** · proposed by alerts-automation

> Every ping looks like a trading card: green for buy-side, red for sell or stop, the levels in fields, and one tap opens INJ 4h in the app with that alert in view. Bursts at a candle close arrive in order instead of being silently dropped.

**Design**

1) New backend/app/notify.py.
- Notice dataclass: kind ('price'|'signal'|'trigger'|'desk'|'trade'|'metric'|'brief'|'scan'), symbol, interval, title, description, fields: list[(name, value, inline)], side ('buy'|'sell'|'info'), url, image (bytes|None), priority ('urgent'|'normal'|'low', default 'normal'), and text (the plain fallback, i.e. today's strings).
- Channel.send_notice:
  - Discord sends {embeds: [{title[:256], url, description[:4096], color: 0x22c55e buy / 0xef4444 sell / 0x64748b info, fields (≤25, values ≤1024), timestamp, footer}], allowed_mentions: {parse: []}}.
  - Telegram sends sendMessage with parse_mode=HTML, html.escape'd text and the link.
- The plain-text Discord body also gets allowed_mentions {parse: []}.

2) Delivery queue in AlertService.
- One asyncio.Queue and worker per channel; send_text and send_notice enqueue.
- On 429 the worker sleeps for Discord's JSON retry_after (Telegram: parameters.retry_after) and retries up to 3 times. 5xx gets a 1/2/4 s backoff.
- Notices queued within 2 s are merged into one Discord post (≤10 embeds, ≤6000 chars).
- Per-channel stats {last_ok, last_fail, last_error, sent_24h, failed_24h} go to /api/status and to AlertsPanel's ChannelBar ('Discord: last delivered 14:02 · 2 failed today', amber or red).
- History items get delivered: {discord: bool}. When every channel fails, the scheduled senders (brief, desk, scan) call jobs.fail.

3) Builders next to each producer:
- alerts.notice_for_fire;
- signal_alerts._fire (signal name, close, suggested stop for triggers, note);
- desk_calls.call_notice/outcome_notice (buy zone, limit, TP %, invalidation %, confidence, expected R, paper size);
- trade_manager._announce (action, take %, new stop, R now);
- metric_alerts.check;
- market_scanner.message;
- brief.render_brief_notices (a header plus one embed per section, coins as inline fields).

4) Link back into the app.
- Settings.public_app_url (PUBLIC_APP_URL, a LAN or Tailscale address) gives url = f'{base}/?symbol=INJUSDT&tf=4h&focus=alert:{id}' (or desk:{id}, trade:{id}). It is left out when unset.
- Frontend: on mount, ChartWorkspace calls lib/deepLink.ts parseDeepLink(location.search), runs pickSymbol(symbol, tf), opens the matching tab (alerts, desk or trades), dispatches ac:desk-select or ac:alert-focus, and then history.replaceState removes the query.

Edge cases: a webhook 404 marks the channel failing. A brief over 10 embeds goes out as several posts. Tests can still use send_text.

**Files**: `backend/app/notify.py`, `backend/app/alerts.py`, `backend/app/signal_alerts.py`, `backend/app/desk_calls.py`, `backend/app/agent_desk.py`, `backend/app/trade_manager.py`, `backend/app/metric_alerts.py`, `backend/app/brief.py`, `backend/app/market_scanner.py`, `backend/app/config.py`, `backend/app/main.py`, `frontend/lib/deepLink.ts`, `frontend/components/ChartWorkspace.tsx`, `frontend/components/AlertsPanel.tsx`, `frontend/components/StatusPanel.tsx`, `backend/tests/test_notify.py`, `backend/tests/test_alerts.py`, `backend/tests/test_brief.py`

**Tests**

- The Discord payload has embeds, the colour for its side and allowed_mentions.parse == []; fields over 1024 chars are truncated.
- Queue (httpx.MockTransport):
  - a 429 {retry_after: 0.05} then 200 delivers once, in order;
  - three notices within 2 s go out as one POST with 3 embeds;
  - a 404 records a failure and jobs.fail.
- The Telegram body escapes '<'.
- The URL is present only when PUBLIC_APP_URL is set.
- Each brief post has at most 10 embeds.
- vitest: parseDeepLink('?symbol=INJUSDT&tf=4h&focus=desk:abc') parses correctly.

**Critic: modify**

- Problems: Backend-heavy; the Discord claims (no allowed_mentions, plain content) match alerts.py. Frontend: the deep link dispatches ac:desk-select and ac:alert-focus, but nothing listens yet, and the Desk and Alerts tabs may not be mounted (Dock lazy mount), so the event is lost on a cold page load, which is exactly what a deep link is.
- Change: parseDeepLink should set the workspace pendingFocus {tab, id} that the opened panel consumes on mount, instead of window events.

**Critic: modify**

- Problems: Confirmed:
- Channel.send POSTs {'content': text[:2000]}, with no allowed_mentions, no retry and no 429 handling (alerts.py:190-213).
- send_all and send_long only log failures.

The item is effort L and touches every producer, so the reliability parts (which also fix silent loss) wait behind the cosmetic embed work.
- Change: Split it in two.
- Stage 1 (S, now): allowed_mentions {parse: []} on both bodies; the per-channel queue with Retry-After and 5xx backoff; per-channel delivery stats in /api/status and ChannelBar; jobs.fail when every channel fails.
- Stage 2: Notice and embeds, the colours and the deep link.
- Every card built from synthetic data gets a '[DEMO DATA]' title prefix and a grey colour regardless of side.

### `chart-snapshots`: A chart snapshot on every signal, trigger, desk and trade card

Effort **M** · impact **5/5** · depends on alert-cards · proposed by alerts-automation

> When 'Lost support' fires on INJ 4h, the Discord card shows the last 120 candles, the broken zone, the next support and the firing candle, so you can judge it in two seconds without opening anything.

**Design**

1) New backend/app/chart_render.py using Pillow (add pillow>=10.3 to requirements.txt; lighter and faster than matplotlib).
- render(df, boxes: [(low, high, rgba, label)], lines: [(price, rgb, dashed, label)], markers: [(time, price, 'up'|'down', rgb)], title, width=800, height=420) -> PNG bytes.
- It uses the app's dark palette (bg #0b0e14, up #22c55e, down #ef4444, grid #1f2633), price-axis labels via pricefmt.fmt_price, a last-price tag, and ImageFont.load_default(size=12).
- It runs in asyncio.to_thread behind Semaphore(1), with an LRU of 32 entries keyed by (symbol, interval, last bar time, overlay hash).

2) Producers attach an image for:
- signal fires (Frame df plus the hit's zone);
- zone triggers (the zone plus the suggested stop, dashed red);
- new desk calls (zone box, entry, TP, invalidation, from the desk's closed candles);
- trade advice (entry, stop, targets, advised stop);
- exit-plan rung hits.
Not for repeat price alerts or the brief.

3) Sending.
- Discord: multipart with payload_json plus files[0]=chart.png, and embed.image.url='attachment://chart.png'.
- Telegram: sendPhoto with a caption up to 1024 chars, then the full text if longer.
- The image is skipped when the queue holds more than 5 notices or rendering raises; the card still goes out.

4) History thumbnails: the last 50 PNGs are kept in .cache/snapshots and served at GET /api/alerts/history/{id}/image. The AlertsPanel History rows show a thumbnail that expands on click.

**Files**: `backend/app/chart_render.py`, `backend/requirements.txt`, `backend/app/notify.py`, `backend/app/signal_alerts.py`, `backend/app/agent_desk.py`, `backend/app/trade_manager.py`, `backend/app/alerts.py`, `backend/app/main.py`, `frontend/components/AlertsPanel.tsx`, `backend/tests/test_chart_render.py`

**Tests**

- render() returns PNG bytes (magic header) of the requested size for a synthetic df, and the same input gives the same bytes.
- A cache hit doesn't render again.
- The Discord multipart body has payload_json and the file part; Telegram uses sendPhoto.
- When render raises, the notice is still sent, without an image.
- The history image endpoint returns 404 for an id that has no image.

**Critic: keep**

- Problems: Backend; reviewed lightly. Pillow is not in requirements.txt today. The client-side snapshot (lib/snapshot.ts) can't be reused on the server.

**Critic: keep**

- Problems: Nothing renders charts server-side today (screenshot.py only reads pasted images), and Pillow isn't in requirements.txt.

The image must be built from closed candles only, and from the same frame the signal used. Otherwise the picture can show a later wick than the one the signal fired on.
- Change: Keep. Render from the exact Frame passed to detect() (signal_alerts._frames). Stamp 'as of {close time} close' and a DEMO watermark when the source isn't binance.

### `quiet-hours-router`: Notification priorities, quiet hours with a morning digest, and channels per kind

Effort **M** · impact **4/5** · depends on alert-cards · proposed by alerts-automation

> At night only what matters for your holdings wakes you (a lost support on a coin you hold, a stop hit). Everything else waits for one 'while you slept' card at 08:00, and desk chatter can go to its own channel.

**Design**

1) notify.Router sits between producers and the queue. Its settings live in the db kv table: {quiet: {start: '23:00', end: '08:00', tz}, quiet_min_priority: 'urgent', kinds: {desk: {on, channel}, scan: {on: false}, ...}, mutes: {symbol: until_ts}}.

2) Priorities.
- urgent: trade stop/structure advice, lost_support or at_resistance with owner 'holdings' (coins held), exit-plan invalidation, and price alerts with a new PriceAlert.urgent flag.
- normal: signals, triggers, new desk calls, trade targets.
- low: desk fills and expiries, market scans, metric alerts, repeat fires.

3) Quiet hours.
- Non-urgent notices go into held_notices (db.migrate). At the end of quiet hours they are sent as one digest Notice grouped by coin ('INJ: lost H4 demand 7.10, Kimi B- 4h · SOL: desk call filled'), capped at 25 fields with '+N more'.
- Urgent notices put <@DISCORD_OWNER_ID> in content with allowed_mentions {users: [owner]}, so the phone pings even with the channel muted.
- DISCORD_WEBHOOKS='desk=url,brief=url,trades=url' routes kinds to channels; DISCORD_WEBHOOK_URL stays the default.

4) API: GET/PUT /api/notify/settings, GET /api/notify/held, POST /api/notify/flush, POST /api/notify/mute {symbol, hours}.

5) Frontend: AlertsPanel's ChannelBar gets a 'Delivery' popover with:
- quiet-hours start and end, and a timezone defaulting to the browser's;
- an 'only urgent while quiet' toggle;
- per-kind on/off and channel select;
- active mutes with remove;
- '12 held · Send now'.
History rows show a 'held' badge until they are flushed, and the price alert editor gets an 'Urgent' checkbox.

Edge cases: urgent always goes straight through. The timezone handling reuses brief._tz.

**Files**: `backend/app/notify.py`, `backend/app/alerts.py`, `backend/app/schemas.py`, `backend/app/config.py`, `backend/app/main.py`, `frontend/lib/alerts.ts`, `frontend/components/AlertsPanel.tsx`, `backend/tests/test_notify.py`

**Tests**

- priority_of(notice) table test.
- With quiet hours 23:00–08:00 Europe/Berlin: a normal notice at 02:00 is held, and an urgent one is sent with the owner mention and allowed_mentions.users.
- At 08:00 one digest goes out with grouped lines.
- A mute suppresses its symbol until it expires.
- A desk notice goes to the desk webhook.
- Settings round-trip through the API.

**Critic: keep**

- Problems: Backend-heavy; reviewed lightly. The Delivery popover goes in AlertsPanel's ChannelBar (l.270), which exists.

**Critic: keep**

- Problems: This is sound for a Discord user. The gap: the same daily close can produce near-duplicate urgent notices for one coin from different owners: holdings lost_support, exit invalidation, desk invalidation and trade-manager structure advice. Each one wakes Trick separately.
- Change: Keep. Add coalescing in the Router: notices for the same symbol within 120 s (same bar) merge into one card listing each source, at the highest priority among them.

### `close-confirmed-alerts`: Alerts that fire on a candle close, and repeat alerts that stop chattering

Effort **M** · impact **4/5** · proposed by alerts-automation

> 'Alert me if the 4h closes above 7.95' fires once on the close, not on a 3 a.m. wick, and a repeating alert stops pinging every 5 minutes while price chops around its level.

**Design**

1) Close mode in signal_alerts.py.
- New SignalId 'level_close' and SignalAlert.level: Optional[LevelClose{price_low, price_high (same as low for a line), side: 'above'|'below'|'inside', closes: 1-3, label}].
- Detection runs in the existing per-(symbol, interval) closed-candle path. It fires when the last `closes` closes are beyond the level (or inside the zone) and the close before them was not.
- Text: '4h closed above 7.95 (close 8.03), 2 closes in a row'.
- New POST /api/level-alerts {symbol, interval, price_low, price_high, side, closes, repeat, note}. Synthetic candles never fire it, like the other signals.

2) Hysteresis for touch alerts.
- PriceAlert gains rearm_pct (default 0.3) and rearm_side.
- alerts.evaluate re-arms a repeat alert only once price has gone rearm_pct beyond the level on the far side. This replaces the REPEAT_COOLDOWN_MS-only check, which stays as a 60 s floor.

3) Frontend.
- NewAlertForm and AlertEditor get a 'Fire on' select (Touch / 15m close / 1h close / 4h close / 1d close) and 'closes in a row' (1–3). A close choice creates a level_close alert.
- Dragging the line keeps the mode (PATCH the level fields).
- The Price tab lists touch and close alerts together, with a '4h close' badge.

4) Rule parser: rule_intent maps 'if the (4h|daily|1h) closes (above|below) X' to a new intent field close_alerts: [{price, side, interval}], added to INTENT_SCHEMA. AnalyzeResponse returns it for the client to arm, like trigger_alerts. The level card's right-click menu gets 'Alert on 4h close beyond X'.

**Files**: `backend/app/signal_alerts.py`, `backend/app/alerts.py`, `backend/app/schemas.py`, `backend/app/llm.py`, `backend/app/agent.py`, `backend/app/main.py`, `frontend/lib/alerts.ts`, `frontend/lib/types.ts`, `frontend/components/AlertsPanel.tsx`, `frontend/components/ChartWorkspace.tsx`, `backend/tests/test_signal_alerts.py`, `backend/tests/test_alerts.py`, `backend/evals/intents.jsonl`

**Tests**

- level_close fires only on the bar where N closes beyond the level follow one that wasn't. A wick through with no close beyond doesn't fire. A repeat alert re-arms after a close back on the other side.
- Hysteresis: chop of ±0.1% around the level fires once; going 0.3% beyond and back fires twice.
- rule_intent('alert me if the 4h closes above 7.95') gives close_alerts [{7.95, above, 4h}] (new eval line).
- The API round-trips.

**Critic: keep**

- Problems: Reviewed lightly. NewAlertForm (l.453) and AlertEditor (l.550) exist for the 'Fire on' select. Alert lines dragged on the chart PATCH through onAlertMove (AgenticChart l.1418-1428), so a level_close alert needs its own drag mapping, since it is a signal alert, not a price alert.

**Critic: keep**

- Problems: No level-close alert exists: the SignalId list (signal_alerts.py:59) has no such id, and alerts.evaluate only has the REPEAT_COOLDOWN_MS 5-min cooldown (alerts.py:50,85).

A fixed rearm_pct of 0.3% is too tight for volatile alts and too loose for BTC.
- Change: Keep. Default rearm to max(0.3%, 0.25×ATR(15m)/price), computed when the alert is armed. Existing repeat alerts migrate with that default.

## A chart that feels alive

The app has outgrown its interface. Sixteen dock tabs clip on a 768 px laptop, and AI levels can only be deleted. Drawings can only be moved whole. Right-click 'Alert me at X' waits on the LLM. Desk and trade events never reach the open tab, and the watchlist doesn't know what Trick holds. These five items shorten the core loop (see a level, act on it, hear when it matters) to seconds.

### `level-card`: Click a level to act on it, and make the right-click actions instant

Effort **S** · impact **4/5** · proposed by frontend-ux

> Click the H4 demand box and a card appears: '24.10–24.60 · 3.2% below · fresh · held 2/2', with Alert on entry, Alert on 5m confirmation inside, Ask about it, Copy price and Remove. Right-click 'Alert me at 7.15' takes about 50 ms instead of a model round trip.

**Design**

1) lib/alerts.ts gets alertFromOverlay(o): AlertSpec | null (a box gives a zone spec, a horizontal line gives a cross, a trendline gives null) and triggerDirection(kind, spotOnly).

2) New components/LevelCard.tsx replaces the pickedLevel bar (ChartWorkspace.tsx ~l.1284). It shows the colour swatch, label, range (formatPrice), the distance from price in % with its direction, and tests/held parsed from the label tail (' · 2/2 held'). Actions:
- Alert on entry → addAlerts([alertFromOverlay(o)], symbol).
- Alert on 5m confirmation → addTrigger({symbol, interval: '5m', zone: {source: 'fixed', price_low, price_high, direction, label}, confirm: 'any'}). In spot mode a supply or resistance zone instead gets a 'reached resistance (trim)' cross alert, never a short trigger.
- Ask → askAgent(`What about the ${label} at ${range}?`), with focus kind 'zone' if follow-the-thread has landed.
- Copy price.
- 'Alert on 4h close below' when close-confirmed-alerts exists.
- Remove, only for the chart's own AI overlays; it is hidden for HTF, pinned, desk and panel levels, which can be picked now too.

3) Right-click menu (AgenticChart ~l.1511): new props onQuickAlert(price) and onQuickLine(price).
- onQuickAlert calls addAlerts([{kind: 'cross', price, price_low: null, price_high: null, label: `Line ${formatPrice(price)}`}], symbol) and shows the undo note 'Alert set at X'.
- onQuickLine calls changeDrawings([...drawings, {id: uid(), type: 'hray', points: [{time: lastBarTime, price}]}], 'line'), so it becomes a real drawing you can drag and style.
- Only 'Ask the agent about X' still goes through runAnalysis.

4) The card and DrawingStyleBar render inside the active ChartCell, which is already positioned, instead of over the whole grid. The undo note moves above QuickPrompt so it no longer covers the input.

Edge cases: Esc or a symbol change closes the card; Del still removes the level.

**Files**: `frontend/lib/alerts.ts`, `frontend/components/LevelCard.tsx`, `frontend/components/ChartWorkspace.tsx`, `frontend/components/ChartCell.tsx`, `frontend/components/AgenticChart.tsx`

**Tests**

- vitest: alertFromOverlay for a box, a horizontal line and a trendline; triggerDirection('supply', true) gives a trim cross, never 'short'; parseHeld('H4 Demand · 2/2 held') returns {held: 2, tests: 2}.
- Manual: a right-click alert appears in the Alerts tab with no /api/agent request in the network panel.

**Critic: modify**

- Problems: Mostly confirmed. The pickedLevel bar (ChartWorkspace.tsx ~l.1284) only offers Remove, and the right-click menu (AgenticChart.tsx l.1518-1522) sends 'Alert me at X' and 'Draw a line at X' through onAskAgent → runAnalysis. Partial overlaps: Remove and Del already exist (removePickedLevel). AlertsPanel's Triggers tab can already build a trigger from a chart zone via lib/alerts.ts chartZones(), which already works out long/short from the zone kind, so a new triggerDirection would duplicate it. Risks: (1) Picking is currently limited by `overlays.includes(o)` (ChartWorkspace l.1259). Widening it to all composed overlays would let the card pick alert overlays (kind 'alert', ids 'alert-<id>'), and 'Alert on entry' would then create a duplicate alert on a line that is already an alert. (2) overlayAt (AgenticChart l.1075) tests only y across the whole width, so a box is picked even where it isn't drawn. (3) Moving the card and DrawingStyleBar into ChartCell means threading more callbacks through ActiveProps, and it only matters in grid mode (in 1-chart mode main and the cell are the same area). (4) Parsing ' · 2/2 held' from labels is fragile. The backend writes it at ta_agent.py:637 as `{held}/{tests} held`.
- Change: Exclude kind 'alert' (and 'my_entry') from picking, and limit box hits to the box's time_start..time_end x-range. Make triggerDirection a thin export built on chartZones' existing kind→direction regex. Reuse planTrigger's addTrigger shape for '5m confirmation'. Drop the 'render inside ChartCell' step, or do it with a portal into the active cell's container. Render the actions with the shared PlanActions/alert helpers (see missing). Keep the instant onQuickAlert/onQuickLine. That part is correct and valuable.

**Critic: modify**

- Problems: Confirmed:
- The pickedLevel bar only offers Remove (ChartWorkspace.tsx:1284-1295).
- The right-click 'Alert me at X' and 'Draw a line at X' run through onAskAgent and the full analysis (AgenticChart.tsx:1520-1532).

Parsing tests/held from the label tail (' · 2/2 held') is brittle: labels are free text, and the LLM path or screenshot overlays don't follow that format.
- Change: - Add an optional meta {tests, held, broke, fresh, htf} to _OverlayBase in schemas.py and types.ts, filled by ta_agent where the label is built; LevelCard reads the meta and falls back to hiding the stat.
- Keep the instant right-click actions and the spot-mode trim mapping.

### `drawing-v2`: Drawing tools v2: draggable anchors, touch-sized handles, and a spot position tool

Effort **L** · impact **5/5** · proposed by frontend-ux

> Drag the end of a trendline to the new wick, pull a rectangle's corner, grab lines with a finger on the phone, and press Z to drop a spot position box ('+8.1% / −3.4% · 2.4R · 41 INJ at 1% risk after fees') that paper-buys, logs or arms an alert in one click.

**Design**

A) Anchors.
- DrawingLayerPrimitive.pickHandle(x, y, radius): {id, index} | null for the selected drawing. It measures the distance to each point; a rect also exposes its two implicit corners (index 2 = [p0.time, p1.price], index 3 = [p1.time, p0.price]).
- HIT_PX becomes a radius argument: 14 for e.pointerType === 'touch', otherwise 6.
- AgenticChart's pointer-down checks pickHandle before whole-drawing hits, setting dragRef {id, index, orig}. Move updates only points[index] through toPoint, so magnet snapping applies.
- Shift on a trendline holds the other point's price, or snaps to 45° in pixels. Alt+drag duplicates (duplicateSelected).
- Points stay in a ref and layerRef.setDrawings during the drag; onDrawingsChange runs once on pointerup. That gives one undo step and ends the per-pointermove full-workspace re-render plus localStorage write.

B) Position tool.
- New ToolId/DrawingType 'position' with TOOL_POINTS 2 (entry, stop). The target defaults to entry + 2R and is a draggable third point.
- DrawingLayerPrimitive.drawPosition draws a green box from entry to TP and a red box from entry to stop, from the entry time to +30 bars.
- The label comes from new lib/positionTool.ts positionStats(points, sizing, spotOnly, freeCash?) → {rr, upPct, downPct, qty, riskUsd, feesUsd, invalid}, built on sizePlan with the spot cash cap (see the sizing fix).
- A stop above entry in spot mode draws grey with 'invalid for spot'.
- DrawingToolbar gets the button (Target icon, key 'Z', the only free letter).
- DrawingStyleBar for type 'position':
  - Paper buy: placePaperOrders(planToPaperOrders(plan, symbol, qty));
  - Log to journal: planToJournalEntry plus createJournalEntry;
  - Alert at entry: addAlerts cross;
  - 5m confirmation: addTrigger with a fixed zone from entry to stop, long;
  - Ask the agent: 'Is this a good entry?' with focus kind 'plan'.

Edge cases: the tool is disabled in replay. Ratio charts only get Alert. Dragging needs the crosshair tool, so it doesn't fight long-press or pan.

**Files**: `frontend/lib/chart/primitives/DrawingLayerPrimitive.ts`, `frontend/components/AgenticChart.tsx`, `frontend/lib/types.ts`, `frontend/lib/positionTool.ts`, `frontend/lib/sizing.ts`, `frontend/components/DrawingToolbar.tsx`, `frontend/components/DrawingStyleBar.tsx`, `frontend/components/ChartWorkspace.tsx`, `frontend/components/ShortcutsDialog.tsx`

**Tests**

- vitest:
  - positionStats R, percentages and qty on fixed points; invalid when stop > entry in spot mode; qty capped by freeCash;
  - pickHandle returns rect implicit-corner indexes 2/3;
  - moveRectCorner maps a corner drag back onto the two stored points;
  - trendline Shift lock keeps the price equal.
- Manual: dragging an anchor creates one undo entry; a touch radius of 14 can grab a line on the phone.

**Critic: modify**

- Problems: Confirmed: DrawingLayerPrimitive.pick only returns a drawing id with HIT_PX=6 (no anchor handles), and AgenticChart's pointer move (l.1385-1405) moves all points together. Wrong claim: the design says it will give 'one undo step', but hooks/useUndo.ts already merges same-label changes within COALESCE_MS=700 into one step, so a drag is already one undo entry unless the user pauses. The real cost is that every pointermove calls onDrawingsChange → changeDrawings → usePersistentState stringify, localStorage.setItem and a SYNC event. 'Z' is indeed free (TOOL_HOTKEYS plus panel keys use t,h,f,r,n,p,m,w,a,l,v,q,j,y,x,c,o,e,b,u,d,s,g,k,i). Gaps: (1) TOOL_POINTS['position']=2 but the drawing stores 3 points, so tapRef's generic creation (`pendingRef.length >= TOOL_POINTS`) needs a special case that appends the target. hits()/drawOne() are exhaustive switches that need a 'position' case. (2) positionStats relies on the spot cash cap from the sizing fix, but depends_on is []. (3) Paper buy, log, alert and 5m trigger duplicate AgentPanel's PlanCard/PaperBuyButton (AgentPanel.tsx l.495) and again appear in kimi-inspector and level-card.
- Change: Restate the goal as 'commit drawings once on pointerup' (useUndo already coalesces). Add depends_on: the sizing fix (spotOnly and freeCash cap in lib/sizing.ts, with free cash from manual.cash in fetchAccountPositions). Special-case 'position' creation in tapRef and add it to hits()/drawOne(). Render the position actions through one shared PlanActions component, also used by PlanCard, level-card and the Kimi card. Keep the anchors and touch radius as designed.

**Critic: modify**

- Problems: Confirmed:
- Whole-drawing drag calls onDrawingsChange on every pointermove (AgenticChart.tsx:1398-1411), and HIT_PX=6 is fixed.
- There is no position tool type in ToolId (types.ts:563).

The position tool reuses sizePlan, which today returns leverage = notional/account and can size a spot buy above the account (sizing.ts). Shipping it before the spot-cash cap means it shows impossible spot quantities.

The '5m confirmation: fixed zone from entry to stop' arms a confirmation that can fire right at the stop, which gives a near-zero R entry.
- Change: - Split it: anchors and touch handles (S) now; the position tool after the sizing fix (spotOnly cash cap, no leverage wording).
- For the confirmation zone, use [max(stop + 0.25×(entry−stop), stop), entry] so a confirmation at the stop can't arm a trade with no room.
- Keep Z as the hotkey and one undo entry per drag.

### `command-palette`: Command palette on Ctrl+K: coins, timeframes, tabs, indicators, layouts and the agent

Effort **M** · impact **4/5** · proposed by frontend-ux

> Ctrl+K, type 'inj 4h' and you're there. 'kimi' toggles Kimi, 'desk' opens the desk, 'layout swing' loads a layout, 'replay' starts replay, and anything else becomes 'Ask the agent: …'. Each row shows its shortcut, so you learn them as you go.

**Design**

1) New components/CommandPalette.tsx, a Dialog with ↑/↓, Enter and Esc. Ctrl+K opens it; S and Space keep opening SymbolSearch, which stays for the 'watchlist' and 'compare' modes.

2) Sources, ranked prefix > word start > substring, recent first:
(a) lib/commands.ts parseSymbolTf(query) → {symbol, tf?}. The symbol is matched against the symbol list and the timeframe against TIMEFRAMES values and labels ('4h', 'h4', 'd', 'daily', '1w'). Ratios like 'eth/btc' are supported. Runs pickSymbol(sym, tf). The top 8 symbol rows show price and 24h change from one debounced fetchTickers call.
(b) Recent symbols from a new ac:recent list (max 12), pushed in ChartWorkspace.pickSymbol and setSymbol.
(c) A Command{id, label, group, hotkey?, keywords, run, enabled?} registry built in ChartWorkspace:
- every dock tab (EXTRA_PANELS plus w/a/l, with hotkeys);
- every indicator toggle (the ChartHeader MenuToggle list moves to lib/indicatorsMenu.ts so both use it);
- saved layouts (WORKSPACES_KEY → applyWorkspace);
- settings toggles (log scale, crosshair sync, countdown);
- Replay, Snapshot, Fit chart, Grid 1/2/4, Run desk 1H/4H/1D (runDesk), Scan market 4h, New chat.
(d) The last row is 'Ask the agent: <text>', which runs focusAgent plus askAgent(text).

3) The same registry feeds the ShortcutsDialog and the Dock rail titles (title='Agent desk (V)'); today the 13 tab hotkeys are only visible under '?'.

Edge cases: an empty query shows recent symbols and the top actions. On mobile the palette is full-screen. Disabled commands (replay on a ratio chart) are hidden.

**Files**: `frontend/components/CommandPalette.tsx`, `frontend/lib/commands.ts`, `frontend/lib/indicatorsMenu.ts`, `frontend/components/ChartHeader.tsx`, `frontend/components/ChartWorkspace.tsx`, `frontend/components/Dock.tsx`, `frontend/components/ShortcutsDialog.tsx`

**Tests**

- vitest:
  - parseSymbolTf('inj 4h') → {INJUSDT, 4h}; 'eth/btc d' → a ratio and 1d; 'kimi' → no symbol;
  - rankCommands puts prefix matches before substring matches and recent before others;
  - the registry contains every EXTRA_PANELS id, so a new tab can't be left out.

**Critic: modify**

- Problems: No palette exists, but Ctrl+K is already taken: ChartWorkspace's key handler (l.876-880) maps Ctrl+K to setSearchMode('chart'), and ShortcutsDialog lists 'S, Space or Ctrl+K: Search a coin'. SymbolSearch.tsx already has ratio parsing (ratioOf, QUOTES), INDEXES and POPULAR. A second parser in lib/commands.ts would drift from it. Item 3 (registry → Dock titles) duplicates notification-center item 5 and the Dock fix. 'Run desk' uses runDesk, which can take up to 300 s (lib/desk.ts), and its result has nowhere to show unless DeskPanel is open.
- Change: Move ratioOf/QUOTES/INDEXES out of SymbolSearch into lib/commands.ts and use them from both. Update the ShortcutsDialog line and the Ctrl+K branch in the key handler. Drop the Dock-title step here, since notification-center owns it. Make 'Run desk' open the Desk tab (via pendingFocus) and show the result there. Keep the rest.

**Critic: modify** (says it largely exists already)

- Problems: Ctrl+K already opens SymbolSearch in chart mode (ChartWorkspace.tsx:877), Space and S open it too, and SymbolSearch already resolves ratios like 'eth/btc' (SymbolSearch.tsx:16). A second dialog on the same shortcut duplicates symbol search and splits ranking logic.

The real gaps are timeframe parsing ('inj 4h'), commands, and showing hotkeys in titles.
- Change: Extend SymbolSearch instead of adding CommandPalette.tsx:
- add parseSymbolTf for the 'inj 4h' form;
- add a commands section fed by the registry, plus the 'Ask the agent: …' last row.
Keep the registry feeding ShortcutsDialog and the Dock titles. Lower priority than notification-center and level-card.

### `watchlist-badges`: A watchlist that knows what you hold: holdings, desk, alert and note badges

Effort **M** · impact **4/5** · depends on holdings-truth, desk-on-chart · proposed by frontend-ux, portfolio

> The watchlist shows 'held · +13%' on INJ, a violet dot for the desk's waiting buy, a bell with 2 alerts and a note marker. It can sort 'My holdings first', and [ / ] step through it in the order you see it.

**Design**

1) ChartWorkspace builds badges: Record<symbol, {held?: {qty, avg, pnlPct, earn}, desk?: {status, confidence, interval}, alerts, triggers, note?, exit?}>. New pure lib/watchlistBadges.ts builds it from:
- armed price and signal alerts per symbol;
- coinNotes;
- fetchAccountPositions(false) manual.holdings every 60 s when fetchBinanceKey reports a key (both are already imported in ChartWorkspace);
- fetchDeskCalls('active') every 60 s and on 'ac:desk-changed'.

2) Watchlist.tsx.
- New props badges and onOrder(symbols); it reports its sorted `ordered` list.
- Rows get a second line of chips: the held PnL toned up/down; a violet desk dot with the tooltip 'Desk 4H waiting · 62%'; a Bell with a count; a StickyNote with the note as tooltip.
- SORTS adds 'holdings' ('My holdings first': held coins by value, then manual order). A filter toggle, 'Only coins with something going on', keeps rows that are held, have a desk call or armed alert, or have a zone badge within 1.5%.
- An 'Add my holdings' button appears when held coins are missing from the active list.

3) stepWatchlist (ChartWorkspace.tsx:865) and Alt+↑/↓ use the reported order instead of currentList.symbols, which today jumps around when a sort is active.

Edge cases: no key means no held chips. The row height stays fixed. Polling pauses while the document is hidden.

**Files**: `frontend/lib/watchlistBadges.ts`, `frontend/components/Watchlist.tsx`, `frontend/components/ChartWorkspace.tsx`, `frontend/lib/binance.ts`, `frontend/lib/desk.ts`

**Tests**

- vitest:
  - watchlistBadges(...) gives the expected counts and held PnL per symbol;
  - sortWatchlist(rows, 'holdings', badges) order;
  - nextSymbol(order, current, ±1) wraps at both ends and follows the displayed order;
  - the filter keeps only active rows.

**Critic: modify**

- Problems: Confirmed: stepWatchlist (ChartWorkspace l.865) uses currentList.symbols (manual order) while Watchlist.tsx sorts its own `ordered`, so [ / ] jump around when a sort is active. SORTS has no holdings option. Problems: (1) depends_on holdings-truth is not needed. manual.spot and manual.earn already exist in AccountPositions (lib/binance.ts:184), so held chips work today and can switch to manual.holdings later. (2) ChartWorkspace already polls fetchAccountPositions every 60 s for the my-entry line (l.403-425), and DeskPanel polls the desk every 30 s while hidden-but-mounted. The design adds a third and fourth poller. hooks/usePolled.ts exists and should be the basis. (3) Rows already have two lines (symbol/VOL/sparkline/price, then zone badge or trend/RSI and change), so 'a second line of chips' would break 'row height stays fixed'. (4) Watchlist only mounts after its tab is first opened (Dock lazy mount), so onOrder may never report; stepWatchlist needs the manual-order fallback.
- Change: Remove the holdings-truth dependency: read manual.spot plus manual.earn now and manual.holdings once it exists. Lift positions and active desk calls into one workspace-level store (usePolled-based, paused while document.hidden), shared by the my-entry effect, the badges, desk-on-chart and DeskPanel. Put held/desk/bell/note as small icons inline on line 1 next to VOL. Put held PnL on line 2 in place of the trend text when there is no zone badge. Keep onOrder with the fallback.

**Critic: keep**

- Problems: Confirmed: stepWatchlist (ChartWorkspace.tsx:865) steps through `watchlist`, not the sorted `ordered` list (Watchlist.tsx:157-160), so [ and ] jump around under a sort.

Until holdings-truth lands, the held chips can be built from manual.spot (with the LD-asset caveat).
- Change: Keep. Ship the stepWatchlist order fix on its own right away; it is frontend-only.

### `notification-center`: In-app awareness: toasts for every event, a live tab title with an unread count, and a working 'welcome back'

Effort **S** · impact **4/5** · proposed by frontend-ux, alerts-automation

> When an alert, desk call, trade advice or market alert fires while you're in another tab, the browser tab reads '(2) INJ 24.31 ▲1.2% · 4H' and its icon gets a dot. Clicking the toast opens that coin, and coming back after hours shows what changed.

**Design**

1) hooks/useAlerts.ts.
- Handle msg.type 'desk': skip shadow calls, call a new onDesk, beep at 520 Hz, and dispatch the window event 'ac:desk-changed'. Today agent_desk._notify broadcasts this message and the hook drops it.
- 'desk_scored' only dispatches 'ac:desk-changed'.
- 'metric_snapshot': compare fired ids with the last snapshot and call onMetric for newly fired market alerts.
- Desktop Notification bodies use formatPrice.
- The message-to-toast mapping lives in a pure lib/notifyClient.ts toastFromWs(msg).

2) AlertToasts.
- MessageToast kinds add 'desk' | 'trade' | 'metric', with optional {symbol, interval, tab}.
- The whole toast is clickable: onOpen calls pickSymbol(symbol, interval) and openTab(tab). 'MARKET' symbols only open the tab.
- Non-urgent toasts auto-dismiss after 12 s, paused on hover; at most 4 are shown.
- ChartWorkspace also toasts trade_advice (today it only beeps).

3) Tab title and icon.
- An effect on [symbol, interval, price, change24, unread] sets document.title = `${unread ? `(${unread}) ` : ''}${displaySymbol} ${formatPrice(price)} ${▲|▼}${|chg|.toFixed(1)}% · ${TF}`, throttled to once a second. Nothing sets the title today.
- unread counts toasts that arrive while document.hidden and resets when the tab becomes visible.
- lib/favicon.ts draws a 32×32 canvas candle in the accent colour, adds a red dot when unread > 0, and swaps a <link rel=icon> data URL. app/icon.svg stays the default.
- Add public/manifest.webmanifest and metadata.manifest in layout.tsx so the app can be installed as a PWA window.

4) SinceLastLooked.
- writeSeen only runs while document.visibilityState === 'visible'.
- When the tab becomes visible again, re-read prev and POST /api/changes again if away ≥ MIN_AWAY_S.
- 'seen' is written only after a successful request, so a backend outage doesn't use up the card.

5) Dock rail: overflow-y-auto with a fade mask, plus the hotkey in each title. Today 16 h-9 buttons need about 622 px, so Status and Account clip under 702 px of viewport height.

Edge cases: each browser tab counts its own unread. Notification permission is untouched.

**Files**: `frontend/hooks/useAlerts.ts`, `frontend/lib/notifyClient.ts`, `frontend/lib/favicon.ts`, `frontend/components/AlertToasts.tsx`, `frontend/components/ChartWorkspace.tsx`, `frontend/components/SinceLastLooked.tsx`, `frontend/components/Dock.tsx`, `frontend/app/layout.tsx`, `frontend/public/manifest.webmanifest`

**Tests**

- vitest:
  - titleFor(symbol, price, change, interval, unread) formatting;
  - toastFromWs maps desk, trade_advice and metric messages, skips shadow desk calls, and leaves MARKET without a symbol action;
  - shouldRecheck(prevSeen, now, visible) for SinceLastLooked.
- Manual: a background tab shows '(1)' after a test alert; the rail scrolls at 650 px height.

**Critic: modify**

- Problems: The core claims check out. hooks/useAlerts.ts ignores msg.type 'desk', 'desk_scored' and 'metric_snapshot'. Nothing sets document.title. Toasts never auto-dismiss: AlertToasts.tsx has no timer, though ChartWorkspace already caps them with .slice(-4). SinceLastLooked.tsx calls writeSeen every 120 s even in a hidden tab, so a tab left open in the background never shows the card. Dock.tsx renders 16 h-9 buttons with no overflow (4 built-in tabs plus 12 EXTRA_PANELS). Where the design goes wrong: (1) Every desk, trade, metric (symbol 'MARKET', kind 'signal') and brief event is already broadcast to the client as {type:'history', item} through AlertService.record (agent_desk.py:487, trade_manager.py:410/417, metric_alerts.py:164, brief.py:570). The design adds a separate mapping per message type and diffs metric_snapshot. That is duplicate work, and trade_advice messages carry no symbol (trade_manager.py:412/418), so a toast built from them can't open the coin. (2) agent_desk._notify already returns early for shadow calls, so 'skip shadow calls' does nothing. (3) Clicking a toast that opens a tab and sends ac:desk-select fails the first time. Dock mounts a panel only after its tab has been opened once (the `mounted` set), so the event fires before anything listens. (4) The rail hotkey titles also appear in command-palette item 3 and in the Dock fix. (5) The frontend has no notion of 'urgent', so the rule for which toasts are 'non-urgent' is undefined. (6) public/manifest.webmanifest plus metadata.manifest works, but Next 15's app/manifest.ts is the native route, and installing over a LAN URL needs HTTPS.
- Change: Build toasts in lib/notifyClient.ts toastFromWs from the existing {type:'history'} item for kinds desk, trade, brief and symbol=='MARKET'. Keep fired/signal_fired for price and signal toasts so nothing toasts twice, and drop the metric_snapshot diffing. Add optional `interval` and `ref` (desk call id or trade id) to AlertService.record and AlertHistoryItem, so every toast and history row can call pickSymbol(symbol, interval). Still handle 'desk' and 'desk_scored', but only to dispatch ac:desk-changed. Replace the window CustomEvents sent to dock tabs with a workspace-level pendingFocus {tab, id} state passed through DockPanelProps and consumed on mount. Rule: price and trade toasts stay until dismissed; all others auto-dismiss after 12 s. Use app/manifest.ts. Do the Dock overflow and hotkey titles once, here, and remove them from command-palette and the Dock fix.

**Critic: keep**

- Problems: All of these are confirmed in the code:
- useAlerts.ts has no 'desk' or metric branch (l.216-243), and trade_advice only beeps and notifies, with no toast.
- MessageToast.kind is 'signal' | 'brief' only.
- Nothing sets document.title.
- SinceLastLooked writes 'seen' on mount and every SEEN_EVERY_MS even while the tab is hidden (SinceLastLooked.tsx:52-63), so a background tab never shows the card.
- The Dock nav has no overflow (Dock.tsx:113) for 16 tabs, and Account and Status have no hotkey.
- Change: Keep as designed; it is high value at small effort. Do the toast clicks and the title for desk events first.

## Fixes

Verdicts are from the critics, who traced each in the code (✔ real, ✘ not real, blank not checked).

### [high] A stream that falls back to synthetic never returns to Binance, so price and signal alerts stop firing without any error 

`backend/app/stream_hub.py`

StreamHub._run calls _run_synthetic(), an endless while-True loop, and then `return`, so nothing leads back to _run_binance. This happens whenever binance_usable() is false at a reconnect:
- after any 60 s demotion;
- after Binance's daily 24 h WebSocket drop;
- at boot when the backend starts before the network.
The first-connect failure branch also calls mark_binance_down(), which demotes all of Binance because one stream failed. AlertService._watch and SignalAlertService._watch drop source=='synthetic' messages, so every armed alert on that symbol goes silent while Status shows OK.
Fix:
- _run_synthetic re-checks binance_usable() every 15 s and returns, so _run retries Binance.
- A stream's own connect failure backs off without demoting REST.
- AlertService and SignalAlertService call jobs.fail when a watched stream has been non-binance for more than 2 min.

### [high] One timeout or 429 switches every caller to synthetic candles for 60 s, usually at a candle close 

`backend/app/market_data.py`

get_klines, get_range and list_symbols call mark_binance_down() on any exception that isn't BinanceRejected. At the 1h/4h/1d closes the desk, signal alerts, trade manager, Kimi and the brief all hit REST together, so one slow answer turns the whole close into demo data: the desk returns empty, signal checks are skipped, and the trade manager does nothing. list_symbols also caches FALLBACK_SYMBOLS for an hour (l.256).
Fix:
- A circuit breaker: mark Binance down only after 3 network failures within 30 s, or when /api/v3/ping fails.
- Retry a single failed request once, with jitter.
- On 429/418, pause for Retry-After without ever switching to synthetic.
- Cache the fallback symbol list for 60 s and keep returning the last good list.

### [high] Signal checks lose the candle when the feed blips at the close ✔ ✔

`backend/app/signal_alerts.py`

on_bar_close sets self._done[key] = bar_time before _frames() succeeds. _frames() returns None at once, without retrying, when get_klines answers 'synthetic', and _kimi_signals does the same. That bar is never checked again, because _scheduled/_done already hold it, yet _after_close still reports jobs.ok. A daily Kimi B+ or a lost_support on INJ is then silently lost.
Fix:
- Treat synthetic or missing candles as 'retry later' for about 2 min, longer than the 60 s down window.
- Set _done only after a real check.
- On final failure, call jobs.fail and reset _scheduled so the next kline message re-queues the bar.

- Critic: Verified. on_bar_close sets self._done[key] = bar_time (signal_alerts.py l.666) before _frames(), and returns [] when frames is None.

- Critic: on_bar_close sets _done[key]=bar_time before _frames(). _frames returns None immediately on a synthetic source (l.731), with no retry. _after_close still calls jobs.ok. The 'never arrived' path also reports ok. Confirmed.

### [high] One agent answer over 2000 chars makes every later request in that conversation fail with a raw 422 

`backend/app/schemas.py`

ChatTurn.text has max_length=2000, and ChartWorkspace.runAnalysis (l.579) sends the full text of the last 10 messages. Detailed answers can pass 2000 chars, after which AnalyzeRequest validation fails on every request until New chat. analyzeStream shows the error as JSON.stringify(detail).
Fix:
- A mode='before' validator that truncates ChatTurn.text instead of rejecting it.
- The client sends text.slice(0, 1500).
- The prompt textarea gets maxLength 2000.
- analyzeStream formats pydantic error lists as readable text.

### [high] The app's own follow-up chips wipe the trade plan on the rule path ✔ ✔

`backend/app/llm.py`

Verified:
- rule_intent('What would invalidate this?', previous=<long plan>) gives features ['support_resistance'] with keep_existing False.
- 'Does the daily agree?' sets timeframe 1d.
- 'Is RSI overbought here?' redraws S/R and windows.
merge_overlays (agent.py:120) then keeps only custom kinds, so every plan_* overlay disappears. These are AgentPanel.followUps chips, and the rule path runs whenever the model is off, paused or fails.
Fix: the question-only branch in follow-the-thread. Ship that branch even if the rest of the item slips.

- Critic: Reproduced with rule_intent: all five questions return features with keep_existing False. Also stop ChartWorkspace from overwriting lastIntent with a question-only intent (see follow-the-thread).

- Critic: Reproduced:
- 'What would invalidate this?' → features ['support_resistance'], keep False.
- 'Does the daily agree?' → timeframe 1d.
- 'Is RSI overbought here?' and 'why?' → the default features.
merge_overlays then keeps only custom kinds. Ship it together with the lastIntent carry-forward, or the next 'same again' loses the plan anyway.

### [high] Coins in Simple Earn drop out of the holdings watch, and moving a coin to Earn closes its managed trade 

`backend/app/binance_import.py`

HoldingsWatch.coins reads only manual.spot, so INJ in Earn loses its lost_support/at_resistance alerts at the next 10-minute sync.
open_position_keys (l.794) builds keys only from spot rows worth $5 or more, so subscribing INJ to Earn removes holding:spot:INJ. trade_manager.sync_positions then closes the managed trade with 'no longer open on Binance'.
Fix: holdings-truth (merged spot + Earn rows).

### [medium] Timed market scans can post demo-data setups and short setups to a spot trader's Discord, and repeat the same list 

`backend/app/market_scanner.py`

message() adds '(DEMO DATA)' only when data_source == 'synthetic', but a scan with some synthetic coins is 'mixed' (l.418), and best() doesn't filter rows by data_source. message() also uses best(n) with direction=None, so longs and shorts are mixed even though best_spot() exists. With MARKET_SCAN_SCHEDULE every N minutes, the same setups are re-sent.
Fix:
- Drop synthetic rows when any coin loaded live, and label each row by its own source.
- Never notify from a mixed scan.
- Notify from best_spot by default (setting MARKET_SCAN_NOTIFY_SIDE).
- Skip a symbol+entry already sent in the last N hours.

### [medium] find_symbol reads amounts like '500usdt' as a coin ✔ ✔

`backend/app/symbols.py`

Verified: 'buy INJ with 500usdt' → 500USDT and 'buy 200USDT of INJ' → 200USDT. rule_intent then navigates there, Binance answers 400, and get_klines serves synthetic candles, so the agent draws zones on demo data.
Fix:
- Require a letter in the base (a lookahead like (?=[A-Za-z0-9]*[A-Za-z]) before group 1).
- Check the result against MarketData.list_symbols() before navigating, and fall back to the name scan.

- Critic: Reproduced: 'buy INJ with 500usdt' → 500USDT and 'buy 200USDT of INJ' → 200USDT.

- Critic: Verified: 'buy INJ with 500usdt' → 500USDT and 'buy 200USDT of INJ' → 200USDT. Must land before rules-first-router.

### [medium] The tool planner passes raw symbols, so 'INJ' or 'BTC/USDT' gives synthetic research, or a 502 in binance mode 

`backend/app/agent_loop.py`

_run_tool uses str(args.get('symbol') or chart.symbol).upper() with no normalisation.
- In auto mode a 400 becomes synthetic candles tagged data_source synthetic, and those land in facts['research'].
- In DATA_SOURCE=binance the MarketDataError (a RuntimeError) isn't in plan_with_tools' except tuple, so the request fails.
Fix: rules-first-router step 4 (norm_symbol, a list_symbols check, and a try/except around each tool).

### [medium] The LLM narrator is never told which coin it is describing, or that the data is demo data ✔

`backend/app/agent.py`

narrate_facts (l.915) has no symbol, and llm.narrate sends only CONVERSATION/TODAY/REQUEST/FACTS, so a 7B model often fills in 'Bitcoin'. `source` reaches AnalyzeResponse.data_source but not the facts, so synthetic zones are narrated as real during an outage.
Fix: facts['symbol'], facts['coin'] and facts['data_source'] when synthetic, plus one NARRATE line (grounded-narrator).

- Critic: The narrate and narrate_stream user message is CONVERSATION/TODAY/REQUEST/FACTS only (llm.py:1079,1098), and agent.py sets no facts['symbol'] or data_source. The symbol only reaches the template describe().

### [medium] The average entry mixes quote assets, and BNB-paid fees are left out of every PnL figure 

`backend/app/binance_import.py`

positions() selects fills by base asset only (l.722), so INJBTC fills at about 0.0003 are averaged with INJUSDT fills at about 20. _fee() returns 'not counted' for BNB commissions, so the journal, PnL calendar and coach overstate profits for anyone paying fees in BNB.
Fix: holdings-truth (USD quotes only; BNB priced at the fill hour).

### [medium] Managing an older trade sends a burst of stale advice to Discord ✔

`backend/app/trade_manager.py`

add() calls check(), which replays every closed candle since opened_at (up to LOOKBACK_BARS), and every historical event is _announce()d with notify(). A journal long from 3 days ago on 15m can send several outdated 'Trail the stop…' messages at once, which also feeds the webhook rate limit.
Fix:
- Run the catch-up review quietly: keep the advice and mark old items done.
- Send one summary instead, e.g. 'Caught up 3 days: T1 hit, best stop now 7.42'.

- Critic: add() → check() → review() over up to 300 closed candles, and every new Advice goes through _announce → alerts.notify (trade_manager.py:332,388-389,415-419). Confirmed.

### [medium] Jobs report 'ok' right after failing, so Status never shows the error 

`backend/app/trade_manager.py`

_loop calls jobs.fail inside the per-trade except, then jobs.ok at the end of the same iteration (l.445). sync_positions exceptions are only logged. BriefService._run and HoldingsWatch's loop follow the same pattern.
Fix: count failures per iteration and call jobs.ok only when there were none; tick()/run() return an error the loop can report.

### [medium] Discord messages allow @everyone and @here ✔ ✔

`backend/app/alerts.py`

build_channels sends {'content': text[:2000]} with no allowed_mentions. Notes and LLM-written labels go into the text verbatim, so '@everyone' in a note pings the whole server.
Fix: always send allowed_mentions {'parse': []}. alert-cards keeps this; ship it now regardless.

- Critic: alerts.py has no allowed_mentions anywhere. A one-line fix worth shipping now.

- Critic: build_channels sends {'content': text[:2000]} with no allowed_mentions (alerts.py:212). Ship it now, independent of alert-cards.

### [medium] The desk's repeat window is 48 candles on every timeframe, and watched zones can never be called later 

`backend/app/agent_desk.py`

REPEAT_BARS = 2 × max(ENTRY_BARS) = 48 candles. That is 48 days on 1d, where an entry expires after 10 days. Combined with one call per coin per run, a deeper zone B saved as 'watched' next to called zone A can't be called for 48 candles after A fails, which is exactly the ladder buy a spot trader wants.
Fix:
- Use 2 × ENTRY_BARS[tf], or block only while the earlier record is still waiting.
- Store skip_reason on watched zones.
- Promote a still-waiting watched zone skipped for capacity or a running call, with features['from_watched'], and keep the watched copy out of learning.

### [medium] Desk paper sizing uses Kelly as the position size, ignoring the stop distance and fees 

`backend/app/desk_calls.py`

size_for treats quarter-Kelly as the share of the wallet to put in, so a call with a 10% stop risks 10x what a 1% stop call risks. kelly() uses gross rr: with fee_r 0.3, p 0.5 and rr 2, gross Kelly is 0.25 against about 0.09 net. The paper wallet then stops tracking the R record.
Fix:
- risk_budget = SIZE_SCALE × net Kelly × equity, capped at 1%.
- notional = risk_budget / (risk_pct / 100), then the 15% cap and free cash.
- Use net Kelly in agent_desk's ok check.
- Update test_sizing_scales_with_confidence_and_respects_the_cash.

### [medium] 'What's working now' calls a setup 'working' on 5 zones with no significance test ✔

`backend/app/desk_learning.py`

working_now labels raw lift ≥ 1.2 as 'working' from MIN_RECENT = 5 zones (verified l.306-313), with no shrinkage. 3 hits on 5 zones at 33% odds happens about 21% of the time by chance. agent.py passes this to the narrator for 'what's working?'.
Fix: a Poisson/binomial tail probability against the expected hits; say 'working' only when p < 0.1 and n ≥ 12, otherwise 'too early', and include n.

- Critic: working_now labels lift ≥1.2 'working' from MIN_RECENT=5 (desk_learning.py:293-313), with no shrinkage. P(≥3 hits of 5 | p=0.33) ≈ 0.21. Zones in one bucket are also correlated (same move, same coins), so the binomial p understates chance; use n ≥ 12 as proposed.

### [medium] Kimi + Agent verdicts are keyed by candle, so two signals on one candle share a take/skip ✔

`backend/app/kimi_agent.py`

signal_filter stores out.verdicts[s.bar] (l.258), and kimi_service._signals reads verdicts[s.bar] (l.162). A B+? and a B-? on the same bar, or a DIV and a Pat BO, overwrite each other, so a long can show a short's verdict and be greyed out with ✕.
Fix: key by (bar, typ, dir) in both places and in test_kimi_agent.py (also part of kimi-inspector).

- Critic: out.verdicts[s.bar] (kimi_agent.py:258), read as verdicts[s.bar] (kimi_service.py:162). Confirmed.

### [medium] The 'Direction Kimi / +Agent' row counts flat forecasts as wrong ✔

`backend/app/kimi_agent.py`

forecast_fix appends np.sign(kimi_end) == np.sign(real_end) (l.163). While Kimi's learned gains are off, its line is exactly flat (sign 0), and the reader measured 3,659 of 4,975 forecasts flat on INJ 4h. The panel then shows 'Direction 13% / 54%' to the indicator's author.
Fix:
- Score direction only where |kimi_end| exceeds the node tolerance.
- Report Kimi's call share separately ('called 27%, right 52%').
- Compare +Agent on the same subset.

- Critic: dir_k appends np.sign(kimi_end) == np.sign(real_end) (kimi_agent.py:163). A flat forecast (sign 0) always counts wrong, while +Agent's nudged value is non-zero, so the comparison is unfair. I didn't verify the 3,659/4,975 count; the logic flaw is certain.

### [medium] Bar replay draws today's Kimi Cooked and stacks future labels on the replay candle ✔

`frontend/components/AgenticChart.tsx`

The Kimi effects (l.1155-1211) never read props.replayTime. KimiPrimitive keeps drawing the latest run (S/R born later, today's Fib and forecast). When the replay effect calls candleRef.setData(subset), LWC maps every later marker onto the last replay candle. That is lookahead while practising.
Fix: while replayTime != null, detach the Kimi primitive and filter markers to s.time <= replayTime. 'Kimi as of then' is deferred.

- Critic: The Kimi effects (AgenticChart.tsx:1155-1211) don't read props.replayTime. Markers keep every signal with time ≥ first candle, and the replay effect sets the candle data to the subset (l.873-892), so later markers snap to the last bar. This is lookahead while practising.

### [medium] The higher-timeframe confluence tag lands on almost every zone ✔

`backend/app/ta_agent.py`

htf_zones returns every swing cluster (single-swing ones included) plus every supply/demand zone, and mark_confluence tags any overlap of any size (l.484-491). Measured on the BTC fixture: 80% of drawn zones are tagged. As a result:
- desk_calls' htf/nohtf buckets are nearly all 'htf';
- market_scanner's HTF gate rarely filters;
- plan cards say 'lines up with D1' by default.
Fix:
- Keep S/R with touches ≥ 2 and broke ≤ held, S/D with tests ≤ 1, and pick_nearest(…, 3) per side.
- Require an overlap of at least 0.3 of the thinner zone, with the same polarity.
- Add a version prefix to the desk bucket key so old 'htf' statistics don't mix with new ones.

- Critic: htf_zones returns every cluster plus every supply/demand zone, and mark_confluence tags any overlap of any size (ta_agent.py:473-491). I didn't verify the 80% figure, but the logic makes a high tag rate certain. The bucket version prefix is required, or desk learning mixes the old and new 'htf' meanings.

### [medium] The chart, desk, scanner and backtest draw different zones for the same coin at the same moment 

`backend/app/ta_agent.py`

find_swings uses distance = max(3, min(12, n // 60)) (l.241), and cluster_levels scores recency as last_idx/(n-1). Both depend on how many candles were loaded: 500 for the chart and agent, 300 for the desk, scanner, brief and sell check, 400 for the backtest and top-down. The reader measured the nearest zone differing in 38% of cases. So the desk calls zones that aren't on the chart, and its priors measure zones the user never sees.
Fix: derive the swing distance from the timeframe (or a constant), score recency by exponential decay over absolute bars, and use one ZONE_BARS constant of closed bars in every consumer.

### [medium] supply_demand_zones counts candles instead of visits ✔ ✔

`backend/app/ta_agent.py`

tests = ((l[after] <= hi) & (l[after] >= lo)).sum() (l.406) counts a 3-candle visit as 3 tests, and a wick through the whole zone as 0. That gives labels like 'Demand (fresh) · 1/1 held'. The count feeds plan.zone_fresh, the desk's fresh/tested bucket, the backtest's fresh-only arming and top_down MAX_TESTS.
Fix: count episodes of l <= hi (demand) or h >= lo (supply) after the impulse, as zone_record and patterns._episodes already do.

- Critic: Verified at ta_agent.py l.406/409.

- Critic: tests = ((l[after] <= hi) & (l[after] >= lo)).sum() (ta_agent.py:406). A wick through the whole zone with its low below lo counts 0. Episode counting is the right fix; it changes the fresh/tested buckets, so it needs the same version prefix.

### [low] Trendlines are drawn after price has already closed through them ✔

`backend/app/ta_agent.py`

The trendlines block (l.768-779) draws a line through the last two swings with extend_right and the label 'Descending resistance' or 'Ascending support', without checking the closes after the second anchor. The reader measured 41% of lines already broken on real BTC.
Fix: after time2, drop the line, or relabel it 'broken N bars ago', if any close crosses it by more than 0.1 ATR.

- Critic: The trendlines block (ta_agent.py:768-779) draws the last two swings with extend_right and never checks the closes after time2. The 41% figure is unverified.

### [low] change_24h on daily candles is a 2-day change ✔ ✔

`backend/app/scanner.py`

change_24h takes the open of the first bar with time >= last.time − 86400. On 1d that is yesterday's open, so the result covers two days; on 1w it is the week's open. It feeds the brief's '(x% 24h)', sell_check, ScanResult.change_pct and the tickers fallback.
Fix: use the close of the last bar that closed at or before now − 24h, or the 24h ticker.

- Critic: Verified. scanner.change_24h takes the first bar with time >= last.time − 86400, which is yesterday's bar on 1d.

- Critic: On 1d, last.time is today's open, so ref starts at yesterday's open (scanner.py:25-29). On 4h it covers up to 28h. Confirmed.

### [medium] Paper trading: after 'Sell all', a plan's stop later sells coins from a different buy ✔

`backend/app/paper.py`

simulate_symbol adds to gfills[group] only for sells from that group, and PaperPanel's 'Sell all' places an ungrouped market sell. group_held still reports the plan's coins, its stop and targets stay pending, and after a new manual buy, have = min(book.held, group_held) lets the old stop sell the new coins (the reader reproduced this).
Fix:
- A sell from outside group G debits the groups that own the coins, oldest fill first.
- Cancel a group's sells once it holds nothing and has no pending buy.
- 'Sell all' cancels that coin's pending sells.
- Also cancel a plan's stop and targets when its buy was cancelled before filling.

- Critic: An ungrouped sell has mine=False, so gfills[G] is never debited (paper.py:259-283). A later stop of G sells min(book.held, group_held(G)), which can be the new coins.

### [low] Position sizing shows leverage to a spot-only trader and can suggest buys larger than the cash available 

`frontend/lib/sizing.ts`

sizePlan computes leverage = notional / account, and the plan card prints 'needs 3.2x' (AgentPanel.tsx l.320); orderText adds 'Lev 3.2x'.
Fix:
- Add a spotOnly flag that caps qty at freeCash / (entry × (1 + fee)), taking free cash from the Binance stablecoin balance when a key exists, else the account setting.
- Return effectiveRiskPct and a capped flag ('capped by cash: 0.6% risk').
- Drop the leverage wording in spot mode and hide 'Highest leverage' in Settings.

### [medium] Saves fail silently once localStorage is full ✔ ✔

`frontend/hooks/usePersistentState.ts`

writeStored and the write effect swallow quota errors. ac:chats (50 sessions with overlays, facts and setups) and the never-pruned ac:overlays:SYM:TF keys grow until the ~5 MB quota is hit. After that, drawings and watchlist edits look saved but are gone on reload.
Fix:
- On QuotaExceededError, show a one-time banner.
- Prune ac:overlays:* keys untouched for 30 days.
- Trim stored chats to text only, or move them to IndexedDB (candleCache.ts already uses it).

- Critic: Worse than described, so it should be severity high. AI overlays only reach the chart through localStorage: ChartWorkspace reads overlays from usePersistentState(overlaysKey) and changeOverlays → writeStored. writeStored swallows the quota error and skips the SYNC_EVENT dispatch (usePersistentState.ts l.9-16), so a new agent answer's drawings, plan lines and undo never appear at all, not merely 'gone on reload'. Add an in-memory fallback in writeStored/readStored as well as the pruning.

- Critic: writeStored and the write effects swallow every error (usePersistentState.ts:13-15, 60, 81). ac:overlays:SYM:TF keys are created per symbol and timeframe and never pruned, and MAX_CHATS=50 sessions are stored with their overlays and facts.

### [medium] The dock rail clips its last tabs (Status, Account) on laptop screens 

`frontend/components/Dock.tsx`

The rail (<nav className='flex w-11 … flex-col … py-2'>) has 16 h-9 buttons and no overflow handling, which needs about 622 px below two 36/44 px header bars. On a 1366×768 laptop (about 650 px of viewport) Status, the tab with the 'something is broken' badge, can't be reached.
Fix: overflow-y-auto with a fade, and hotkeys in titles (notification-center).

### [medium] 1000x perpetuals aren't mapped, so PEPE, SHIB, BONK, FLOKI and similar coins show no futures data 

`backend/app/futures_data.py`

funding, open_interest, long_short, walls(futures) and DerivativesService.symbol_snapshot send the spot symbol to fapi as-is (there is no 1000-prefix handling anywhere). That gives 'Binance has no perpetual for PEPEUSDT' in the panel and the agent.
Fix: a spot→perp map from /fapi/v1/exchangeInfo (PERPETUAL, TRADING; 1000/1000000/1M prefixes), cached 6 h, with prices and quantities scaled by the multiplier.

### [low] The on-demand sell check counts the still-open candle ✔ ✔

`backend/app/sell_check.py`

sell_scan uses get_klines, which includes the forming bar, for both the chart timeframe and the daily. So 'closed below it 2 times' can be one close plus the live price, while the sell alerts run on closed_only candles, and the two can disagree.
Fix: apply kimi_service.closed_only to both frames, and mention a live-candle break separately as provisional.

- Critic: sell_check uses market.get_klines with no closed_only (sell_check.py l.196-199).

- Critic: sell_scan uses get_klines for both frames with no closed_only (sell_check.py:196-200), unlike the signal-alert path. Confirmed.

### [low] A failed Kimi fetch isn't retried until the next candle closes ✔ ✔

`frontend/components/AgenticChart.tsx`

The fetch effect (l.1156) sets the error and waits for kimiTick, which only moves on a new candle. After a backend restart a 1d chart shows 'Kimi Cooked unavailable' for up to a day.
Fix: while kimiOn && error, retry with backoff (5 s, 20 s, 60 s, then every 5 min), aborted on a symbol or interval change.

- Critic: Verified. kimiTick is only bumped from the candle-close timer (AgenticChart l.709), and the fetch effect depends on it.

- Critic: The fetch effect depends on kimiTick, which is only bumped by the candle-close timer (AgenticChart.tsx:709,1177). On error nothing re-fetches.

### [low] Signal preview draws sell signals as buy arrows ✔ ✔

`frontend/components/AlertsPanel.tsx`

runPreview's `long` check (l.735) leaves out lost_support and at_resistance (and kimi_any is always treated as long), so previewing 'Lost support (sell)' draws up-arrows below the candles.
Fix: a shared SIGNAL_SIDE map in lib/alerts.ts, with kimi_any taking each hit's direction from the backend preview.

- Critic: Verified: the `long` exclusion list at AlertsPanel l.735 lacks lost_support and at_resistance.

- Critic: runPreview's long check omits lost_support and at_resistance (AlertsPanel.tsx:735), and kimi_any is always treated as long.

### [low] Desk higher-timeframe frames include the still-open candle ✔ ✔

`backend/app/agent_desk.py`

_coin fetches the HTF candles with get_klines and keeps the open D1/W1 bar. The same zone can then land in different htf/agree buckets hour to hour, and no replay can reproduce it.
Fix: apply closed_only(candles, h, now) before candles_to_df (kimi_service already does this).

- Critic: Verified. _coin fetches the HTF frames with get_klines and uses them unfiltered (agent_desk.py l.313-316); only the base frame is cut to `bar`.

- Critic: _coin builds frames from get_klines(symbol, h, CANDLES) with no closed_only (agent_desk.py:313-316), while the base frame is filtered with c.time <= bar.

### [low] Kimi pill times are in UTC while the chart axis uses local time 

`frontend/components/KimiPanel.tsx`

'Latest signals' and 'Patterns' format times with toISOString().slice(5, 16) (l.120, l.145), so a CEST user sees a B+ two hours off from the chart.
Fix: pass layout.timezone and format with Intl.DateTimeFormat (or label the times UTC).

## Gaps the critics found

- The agent's own navigation aborts its streamed answer. ChartWorkspace's market-change effect (l.535-540) aborts analysisCtrl on every symbol/interval change, and only walkingRef exempts the top-down walk. apply() calls setCell(r.navigate) when the 'result' event arrives, before narration. So with qwen narrating, 'Open BTC daily…' (a SUGGESTION), 'Same on daily' (a FOLLOW_UP), 'Open {coin}' and scan→plan on another coin leave an empty bubble with a pulsing cursor. Fix: set a navigatingRef in apply() just as playWalk does, or abort only on user-initiated chart changes.

- The streamed answer is persisted on every token. Each onDelta → setMessages → usePersistentState JSON.stringify of the whole conversation (with overlays and facts) → localStorage.setItem plus a SYNC_EVENT. That causes main-thread jank on long answers and speeds up the quota failure. Keep the streaming text in memory and persist only on done, stop or error.

- A frontend test harness as a separate first item (vitest, the '@/*' alias from tsconfig, node env for lib/*, jsdom only where needed, and an npm test script). About nine items put vitest tests in their plans, but only follow-the-thread adds the runner.

- One shared PlanActions component plus lib/planActions.ts: Paper buy (PaperBuyButton exists in AgentPanel l.495), Log trade, Alert at entry, 5m confirmation (planTrigger exists in ChartWorkspace l.1014) and Ask, with one spot-only guard. The package re-implements these in level-card, drawing-v2's position tool, the kimi-inspector card and exit-plan.

- Workspace-level pendingFocus {tab, id} passed through DockPanelProps instead of window CustomEvents. Dock mounts tabs lazily, so ac:desk-select and ac:alert-focus (desk chips, toasts, deep links, the 'Open call' link) are lost whenever the target tab hasn't been opened in this session.

- One shared, visibility-paused data store for desk calls and account positions. Today ChartWorkspace's my-entry effect polls positions every 60 s and DeskPanel polls every 30 s even while hidden (Dock keeps panels mounted). The package adds useDeskCalls, watchlist-badge polls and a MarketHeader account poll. Build it on hooks/usePolled.ts and add a document.hidden pause there.

- Add interval and ref (desk call id, trade id) to alert history items (AlertService.record / AlertHistoryItem). Toasts, History rows and deep links can then open the right chart; trade_advice WS messages carry no symbol at all.

- An abort leaves a message with streaming:true forever (cursor, no Copy, no meta, excluded from Alt+S). Finalize such messages as stopped in one place for New chat, openChat, the market change, navigation and the new Stop button.

- When alerts go silent because the feed is synthetic (AlertService and SignalAlertService skip synthetic messages; desk, trade manager and signal checks return early), Trick gets no Discord message; at best a Status-tab error. Add an urgent 'Alerts paused since 03:12: Binance feed down' notice after about 3 min, and a 'resumed' notice. This is what a Discord-first trader needs most from the reliability fixes.

- LD-prefixed Simple Earn Flexible balances: Binance's /api/v3/account typically lists flexible Earn as LDINJ or LDUSDT. spot_balances (binance_account.py:309) keeps every asset, and positions() builds '{asset}USDT' rows from them. That can create bogus LDINJUSDT holdings, watch alerts and invalid myTrades calls today, and would double-count after holdings-truth merges Earn. Verify on the live key and map LD<asset> to Earn.

- Notification coalescing per coin per bar. Holdings lost_support, exit-plan invalidation, desk invalidation and trade-manager structure advice can all fire for INJ on the same daily close; today, and in the package, each sends its own ping. Merge them in the router into one card per symbol.

- One probability vocabulary across the plan card, chips and narrator. Desk odds are 'TP before stop, if filled'; Kimi reach is 'touch within N bars, stop ignored'; the track record is a 'backtest win rate'. Define the three once (UI and NARRATE_RULES) and never show two of them for the same target without their qualifiers. Otherwise one card shows three different percentages for T1.

- Carry the previous intent forward after question-only turns. ChartWorkspace.tsx:651 overwrites lastIntent with every answer's intent, so the question-only fix alone still breaks 'same again' or 'same on daily' right after 'what would invalidate this?'.

- Sequencing the package doesn't state:
- find_symbol fix before rules-first-router;
- one ZONE_BARS and swing distance before desk-on-chart and desk-odds-on-plans (otherwise desk and chart zones disagree);
- the spot sizing cap before drawing-v2's position tool;
- close-confirmed-alerts before exit-plan's invalidation alert.

- Frontend test runner: vitest is introduced separately by eight items (follow-the-thread, desk-on-chart, kimi-inspector, kimi-odds-anywhere, alert-cards, level-card, command-palette, notification-center). Land package.json with vitest and `npm test` once, as step zero.

- Given the request to finish the frontend first, these can ship without backend work:
- notification-center, level-card, watchlist order fix, Dock overflow, spot sizing cap and leverage wording, Kimi replay lookahead, Kimi fetch retry, signal preview direction, KimiPanel local time, localStorage quota banner and pruning;
- the ChatTurn client-side slice(0, 1500).
They give visible wins while the backend items wait.

## Deferred ideas

- 'Remember that…' trader profile (memory.py): per-coin notes already reach the agent and the brief; this comes after follow-the-thread so stored memories can be quoted, and it needs a visible forget/edit list in Settings.
- Kimi forward ledger (frozen, live-scored record of every signal): the biggest long-term Kimi gain, but it needs weeks of closes and an engine run at every close; do it after moving Kimi to a process pool with a priority lane.
- Kimi process pool, priority lane and LRU cache: a real CPU win at closes, but invisible to Trick; schedule it alongside the ledger.
- Desk placebo twins and the replayed year of desk history: the right fix for the random-walk baseline, but they change what every lift means, and the replay needs about 2 h of CPU plus heavy Binance load; do both together, after the circuit breaker.
- Desk learning maths (hold-window baseline, coin residual on the bucket, correlated-evidence weighting, learned stop and entry, walk-forward feature model, Platt recalibration, pooled Kimi + Agent): validate each on replay data before it touches live confidence.
- Learned trims (scoring sell and trim signals): needs its own 'right'/'wrong' design and regime features; do it after exit-plan, which gives trims a structure first.
- TA engine additions (top-down verdict, range spring/deviation, trendline engine v2, liquidity pools, squeeze detector, period opens/AVWAP, RS rotation board, detector report card): the engine fixes in this upgrade (HTF confluence, zones depending on candle count, test counts) come first, so new detectors aren't built on shifting zones.
- The full Binance gateway (weight budget, shared in-flight requests, live candle tail from streams): the circuit breaker is in fixes; the rest is a large refactor of the busiest code path.
- New market data feeds (all-market miniTicker radar, spot vs perp flow read, liquidation-flush alert, order flow kept in candles, heatmap from the depth stream, Korea/Coinbase premiums, stablecoin dominance, news tagging from the exchange coin list): each adds load on the shared IP weight, so do them after the gateway.
- Delist guard for coins held: small and valuable, but the delist-schedule endpoint's response on Trick's key is unverified; add it after quiet-hours-router so it has an urgent path.
- Two-way Discord bot (buttons on cards, /ask from the phone): needs a bot token, a long-lived gateway connection and discord.py; build it on the notify.py cards next.
- Alert scorecard, attention-ranked brief and event/news guard: all build on alert-cards and the router; next round of alert work.
- Grid bot health checks, DCA plan reminders, a walk-forward grid planner, coaching on individual sells, and a losing-streak brake: portfolio extras that build on the cockpit.
- Load older history on scroll-left, plus the performance pass (feed store, memoised and lazy-loaded panels, pausing hidden tabs): important but mostly invisible; drawing-v2 already removes the worst re-render (on drag).
- Level-freshness chip, grid upgrades (maximise a cell, 1+2 layout, per-cell timeframe), phone-first bottom nav, hover a price in an answer to highlight it, forecast ghosts, factor scoreboard, 'Kimi as of then' in replay, Kimi Lab, TradingView parity checker, Kimi lines in 'since you last looked', market regime tape, what-if questions on holdings: good ideas that each add less than the 22 chosen; the Kimi replay lookahead itself is in fixes.

## Parked ideas

### `ghost-candles`: Kimi's forecast as ghost candles that real candles print over (later)

Instead of a line redrawn at every close, the forecast is drawn as faded ghost candles for its horizon (about 20 bars,
set per timeframe, as Kimi's horizon already is): each opens at the previous ghost's close and closes on the forecast
path; wicks come from the scenario texture and the band, and fade with distance (only the closes are forecast; the
tooltip says so). A forecast is locked when made and stays for its whole horizon while real candles print on top; the
next starts when it ends (a switch keeps today's redraw-every-close, and the latest fresh forecast can show as a thin
line meanwhile). Each finished forecast gets a score label: real closes inside the ghosts' range, direction right or
not, end error; the last few stay faded under the real candles, from the forecasts kimi_agent already keeps. 'Flat (no
proven edge)' ghosts stay grey and say so. Main pane only at first.
