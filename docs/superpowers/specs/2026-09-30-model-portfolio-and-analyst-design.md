# Model portfolio and the TradingView analyst

Date: 2026-09-30. Approved by the user in chat ("approved, build it"). It replaces the tiered
signals (Сильный / Кандидат / Осторожно / Высокий риск), their filters and the 13 paper books
with one strategy, and adds an analyst that answers questions in Telegram and the terminal
using TradingView through its MCP server.

The bot never places real trades. The model portfolio is virtual.

## Goal

- One strategy for stocks and coins, designed by Claude:
  - every stock or coin gets a **score from 0 to 100**;
  - the score decides whether to buy;
  - volatility decides the position size and the stop;
  - exit rules cut losers and let winners run.
- A **model portfolio** that trades that strategy on paper every day and reports its buys and
  sales in Telegram.
- An **analyst**: the user asks anything in Telegram or the terminal, and a headless Claude
  run answers from:
  - live TradingView data (through the TradingView MCP server);
  - the bot's own data (insiders, the model's score, positions, news).

## Removed

- **Tiers and their filters:**
  - `strategy.select`, `Selection`, the tier rules (`_stock_tier`, `_crypto_tier`, `_caution_tier`, `_high_risk_tier`);
  - the €300M / €1M size floors, the €50–300M high-risk band and its lower-threshold finder pass;
  - the 3-day recency window and the Trading 212 filter, which becomes a label;
  - `CANDIDATE_MIN_SCORE`, the treasury "big" floor and the trend-confirm gate.
- **The tiered digest** (`format_tiered_digest`, `_rule_lines`) and `bot._record_high_risk`.
- **The 13 paper books stop trading.** Their rows stay in the database: `python paper.py R1-E1` still prints one. `paper.run`, `stock_step`, `crypto_step` and the book rules (`EXITS`, R1/R2, C-A/C-B, H1/H2) are deleted.
- **`calibrate_strategy.py` and its tests**, which calibrate the removed tiers.

`strategy.buy_side_signals` and `strategy.exit_signals` stay: they are the list of finders.
The high-risk second pass is deleted from `buy_side_signals`, along with its `high_risk`
parameter. The crypto finders (`cluster/crypto.py`) stay as detectors of unusual flows. The
model reads their output as score points, not as gates.

## 1. The stock score (model_score.py, pure functions)

A stock is scored only when something happened: a buy-side signal disclosed in the **last 14
days** (`MODEL_SIGNAL_DAYS = 14`, by `cluster.disclosed_on`). A signal is re-scored every day
while it is fresh, because its momentum and news change.

### Score = insiders + triggers + momentum + news, clamped to 0–100

**Insiders (0–60)**, for a `ClusterSignal` from SEC, BaFin, Norway or Sweden. "Management"
means buyers whose role is in `cluster.roles.INSIDER_ROLES`.

| What | Points |
|---|---|
| Management buyers: 1 / 2 / 3 / 4+ | 22 / 34 / 42 / 46 |
| A CEO or CFO among them (otherwise a Chair) | +10 (+5) |
| All their purchases as a share of the company's value (`value_pct_of_mcap`): at least 0.01% / 0.05% / 0.2% | +3 / +6 / +10 |
| The biggest buyer's holding grew (`position_increase_pct`) by at least 10% / 30% | +3 / +6 |
| First purchase (`first_buy`) | +4 |

- A holder-only cluster (only >10% holders) gets a flat 15 instead.
- A House or Senate cluster gets 0 here; politicians count under triggers.
- The part is capped at 60.

**Stake conviction**: for a `StakeSignal` (13D/G) the insiders part is replaced by:
- an activist 13D: 30 + 2 × min(percent − 5, 10), so 5% scores 30 and 15% or more scores 50;
- a passive 13G: 15 + min(percent − 5, 10);
- +5 when the stake grew by at least 1 percentage point (`prev_percent` known);
- a cap of 60.

**Triggers (0–20)**, from what else is going on around the same ticker:
- **+15:** an activist 13D on it (another signal in the day's candidates, or a `sec_stakes`
  row with form '13D' and `event_date` in the last 30 days). Not counted when the signal
  itself is that 13D.
- **+6:** a passive 13G of at least 10% under the same rule.
- **+5:** politicians buying it (a House/Senate candidate on the same ticker, or the signal
  itself is one).
- **+5:** `corroborated_by` names any source not already counted above.
- The part is capped at 20.

**Momentum (0–15)**, from daily adjusted closes:
- **6-month return** (126 closes back): above 0 → +5, above +25% → +8.
- **Close above the 200-day average:** +5.
- **1-month return** (21 closes back) above 0: +2.
- A missing window scores 0 for that line. Momentum never blocks a buy.

**News (−30 to +10)**, from headlines of the last 14 days (`sources.news`). Each title is
lower-cased and matched against three fixed lists of phrases in `model_score.py`:
- **Red flags block the buy:**
  - fraud, SEC investigation, subpoena, going concern, bankruptcy, chapter 11,
    restatement, delisting, delisted;
  - share offerings: public offering, secondary offering, private placement, at-the-market.
- **Negative, −10 each, at most −30:** downgrade, cuts guidance, cuts forecast, misses
  estimates, lawsuit, probe, recall, resigns, halts, short seller.
- **Positive, +5 each, at most +10:** upgrade, raises guidance, raises forecast, beats
  estimates, buyback, record revenue, wins contract, fda approval.
- A headline counts once per list, and at most one red flag is reported.

### Decision

| Condition | Decision |
|---|---|
| A red-flag headline | **block** («блок: …») |
| Score of 60 or more, tradeable | **buy** |
| Score of 60 or more, not tradeable | **watch** (with the reason) |
| 45–59 | **watch** |
| Below 45 | **skip** |

**Tradeable** means all of these:
- **a price series:** `paper.listing` resolves the ticker, and there are at least 21 completed closes;
- **market value:** €20M or more (`MIN_MCAP_EUR = 20e6`) when known;
- **daily trading:** €100k or more (`MIN_ADV_EUR = 100_000`) when known;
- **size known:** not both of market value and daily trading unknown («размер неизвестен»).

**Trading 212:** `trading212.availability(conn).can_buy(ticker, source)` false → the label «нет
на T212». It never changes the decision. With no key, there is no label.

**Why these weights:** a management cluster of three with a CEO buying a real stake (42 + 10 +
3–6 + 3) reaches 58–61 before momentum, so it buys on a dip. Two directors buying small amounts
(34) need momentum and good news to reach even the watchlist. An activist 13D alone (30–50)
needs support to reach a buy. Politicians alone never do.

## 2. The coin score

For BTC and ETH now. More coins are the next project.

**Trend (0–60):**
- close above its 100-day average → +15;
- each of the 20-, 60- and 120-day returns above 0 → +15.

**Flows (−20 to +15):**
- **+15:** a bullish `CryptoSignal` (ETF inflow, company buying) on the coin disclosed in the
  last 7 days.
- **−20:** a bearish one in the last 7 days that the price confirms
  (`crypto.trend_confirms_down(trend_fn(conn, coin))`). This is a **caution**.

**News (−30 to +10):**
- same lists as stocks, plus the red flags hack, exploit, stolen and SEC lawsuit;
- the query is `sources.news(assets.crypto_asset(coin))`.

**Trend up** means a close above the 100-day average and at least two of the three returns
above 0.

| Decision | When |
|---|---|
| **buy** | Trend up, score of 60 or more, no caution, no red flag |
| **block** | A red flag or a caution |
| **watch** | Otherwise |

## 3. Sizing and stops

- **Typical daily move:** the mean absolute daily change of the last 20 completed closes.
- **Stop distance:** 3 × typical daily move, clamped to 10–25% for stocks and 15–35% for coins
  (`STOP_MIN/STOP_MAX`).
  - The floor keeps a calm stock from being stopped out by ordinary noise over a months-long
    insider thesis.
  - It's fixed at the buy and stored with the order and the position (`stop_pct`).
- **Position size:** the lowest of these:
  - `RISK_PER_TRADE (0.01) × model value ÷ stop distance`;
  - the per-name cap: a stock 10% of the model value; BTC or ETH 35% of the crypto sleeve's value.
- **Model value** = the stock book's value + the crypto book's value.
- **Order rules:**
  - an order below €500 is skipped («мало»);
  - with less sleeve cash than the size, it buys with what's left if that is at least half,
    otherwise it skips («нет денег»).
- **Stock limits:**
  - at most 12 stock positions, open plus pending («мест нет»);
  - at most 3 in one sector («сектор заполнен»); the sector comes from yfinance
    `info["sector"]` through a seam, and an unknown sector has no limit;
  - no re-buy of a ticker sold in the last 30 days («недавно продан»).
- **Order of buys:** by score, highest first.

## 4. The model portfolio (model.py)

**Two books in the existing paper tables:**

| Book | Sleeve | Starting money | Benchmark |
|---|---|---|---|
| `MODEL-S` | stock | €70,000 | SPY |
| `MODEL-C` | crypto | €30,000 | BTC-USD |

- Their start date is the first run.
- Their sum against the sum of their benchmarks is the model against an un-rebalanced 70/30
  S&P 500 / Bitcoin mix.
- Orders, fills, fees, adjusted-close valuation, FX, the 5-business-day no-price rule and the
  daily snapshot are paper.py's own (unchanged).
- `paper_orders` and `paper_positions` gain the columns `stop_pct REAL` and `score REAL`.

**Daily pass** (`model.run(conn, today, *, fetch=None, news_fn=None, trend_fn=None, sector_fn=None, signals=None, t212=None) -> DayReport`):
1. Create the books once.
2. Fill pending orders at the new close, then mark to market (both books).
3. **Exits** on the latest completed close, first match wins. Each becomes a sell order filled
   at the next close.
   - **Stocks:**
     1. **Trailing stop:** the close is at or below (highest close since the fill) × (1 − `stop_pct`). The text is «стоп: −N% от максимума».
     2. **The reason is gone:**
        - an insider named at the buy sells after the fill (`positions._insider_sale`);
        - or, for a buy made on a 13D/G, a later `sec_stakes` row by the same person on the same ticker shows a smaller `percent_of_class` («активист сократил долю»).
     3. **Dead money:** 60 or more business days held and a return below +5% («стоит на месте»).
     4. **365 days held** («год в позиции»).
     5. **A red-flag headline** on the ticker («новости: …»).
   - **Coins:**
     1. the trailing stop;
     2. **trend down:** the close is below the 100-day average and the 20-day return is below 0 («тренд вниз»);
     3. a price-confirmed caution;
     4. a red-flag headline.
4. **Scoring:**
   - candidates are `strategy.buy_side_signals(conn, ignore_alert_state=True, onchain=False,
     cluster_kwargs={"min_value": 50_000, "solo_threshold": 250_000},
     stake_kwargs={"min_percent": 5.0, "activist_only": False, "new_positions_only": False,
     "max_age_days": 14})`, disclosed in the last 14 days and enriched (`cluster.enrich_signals`);
   - several signals on one ticker → the highest score is kept;
   - news is fetched only for stocks whose score without news is 35 or more;
   - coins are scored every day.
5. **Buys:** decision buy, not held and not pending, sized as in §3.
6. Snapshot both books.

- **Failures:** one sleeve failing is logged and skipped. The pass runs under
  `bot._run_source("MODEL", …)`.
- **Filtered run:** a filtered run (`bot._filtered_run`) skips the model.

**DayReport:**
- `buys` and `sells`: the orders placed today, each with ticker, amount, stop, score, reason
  lines, and the result for a sale;
- `scored`: every `StockScore` and `CoinScore`, highest first;
- `value`, `bench`: the model and the 70/30 mix today.

`model.score_today(conn, …)` computes the scored list without trading (for the menu and the
analyst).

## 5. The daily run and Telegram

`bot.main` after the source passes:
1. **New signals:** the finders run as today (alert state, run_daily.sh's flags).
2. **The model pass**, unless the run is filtered.
3. **The journal:**
   - new signals are enriched, journaled and marked alerted in the same run;
   - the journal's `tier` column holds the model's decision for that ticker that day (buy / watch / block / skip), or `caution` for a bearish crypto signal, or NULL when the model didn't score it;
   - `positions._crypto_caution` keeps reading `tier = 'caution'`.
4. **Your positions** (`/bought`): `positions.check_exits` applies the same exit rules (§4, step 3) to them.
   - Their `stop_pct` is fixed when the position is recorded, from the price history at that moment.
   - The column is added to `positions`. A position without one computes it from the closes up to its open date.
5. **One Telegram message**, sent only when there is at least one buy, sale, close alert or group exit. The alert state and journal are no longer tied to its success.
   - **Heading:** 📊 «Модельный портфель — DD.MM».
   - **🟢 Купить:** ticker, company, amount and share of the model, the stop, the score, 1–3 reason lines, and the «нет на T212» label if it applies. Fills at the next close.
   - **🔴 Продать:** ticker, reason, result so far.
   - **🚪 Ваши позиции:** the close alerts for /bought positions.
   - **🚨 Продают те, кто покупал:** the exit signals, as before.
   - **A last line:** the model's value and return against the mix.
6. **The monthly report** (paper_report) now describes the model.

- **Removed from `bot.py`:** `strategy.select`, the tier journal, `_record_high_risk`, `_send_digest`, `_run_paper` and the `--min-score` / `--min-liquidity` display filters (`keep()`).
- **Kept:** the flags themselves, which still mark a filtered run.
- **Market context:** `tradingview.annotate_signals` is no longer called by the daily run.

## 6. Views

**Menu:**
- **1) Сигналы:**
  - `model.score_today`, printed as one line per scored stock or coin: decision icon, ticker, score, the four parts, the T212 label and the block reason;
  - then the /bought positions and pending close alerts.
- **3) Модельный портфель:**
  - value and return against the mix, worst drop against the mix's, completed trades, open positions (ticker, days, result, distance to the stop), cash per sleeve;
  - the success status: 182 days, beat the mix, a smaller worst drop and at least 20 completed trades;
  - a line naming the archived books.
- **4) Спросить аналитика:** see §7.

**Command line:**
- `python paper.py` prints the model summary;
- `python paper.py MODEL-S` (or any archived code) prints one book.

## 7. The analyst (analyst.py, TradingView MCP)

### Asking

- **Telegram:**
  - a message that is one token resolving to an asset → the ticker analysis (as today, now with TradingView);
  - `/ask <text>`, or any other text → a **question**;
  - `/portfolio` → the model summary, instantly, without Claude;
  - the help text lists all three.
- **Terminal:** `python analyst.py ask "вопрос"`, or menu option 4. It prints the answer and
  sends nothing to Telegram.

### Queue

- `claude_analysis_queue` gains `kind TEXT DEFAULT 'ticker'` and `question TEXT`.
- `db.enqueue_question(conn, text) -> int` adds a row with kind 'question', ticker 'ВОПРОС' and
  the text. It is never deduplicated.
- `pending_analysis` is unchanged: a question shows as (id, 'ВОПРОС').

### Running Claude

- `analyst.claude_command(prompt) -> list[str]` builds the one command both paths use:
  - `~/.local/bin/claude -p PROMPT --permission-mode acceptEdits`;
  - `--allowedTools`: `Bash` plus the TradingView tools `tv_health_check`, `tv_launch`, `chart_get_state`, `chart_set_symbol`, `chart_set_timeframe`, `quote_get`, `data_get_ohlcv`, `symbol_info` and `symbol_search` (each as `mcp__tradingview__<name>`).
- The `PATH` passed to it includes /usr/local/bin and /opt/homebrew/bin, so launchd can start
  the MCP server's `node`.
- **`run_claude_analysis.sh`** calls `python analyst.py process-queue`, which:
  - returns at once when the queue is empty (no Claude run);
  - otherwise runs Claude with `claude_analysis_prompt.txt`.
- **The Telegram bot's synchronous run** has a timeout of 420 seconds. When it fails:
  - for a ticker, the existing fallback applies;
  - for a question, the reply is «Не успел ответить — вопрос в очереди, ответ придёт позже.»

### Context commands (for Claude; they print text)

| Command | Prints |
|---|---|
| `python analyst.py question ID` | The queued question's text |
| `python analyst.py context TICKER` | The model's score breakdown and decision if the ticker was scored today, else «свежего сигнала нет» plus the momentum and news parts; the model's and the user's positions in it; `research.format_brief` |
| `python analyst.py portfolio` | The model summary plus the watchlist (score 45–59) and today's buys |
| `python analyst.py news "QUERY"` | Up to 10 Google News headlines with dates |

- The `TICKER` argument must match `^[A-Z0-9.\-]{1,15}$` or `^\$?[A-Z]{1,6}$`, otherwise it
  exits with an error.

### Method (analyst_method.txt, read by both prompts)

1. **Find the assets the question is about:** at most 3, from the question text, or
   `python analyst.py portfolio` for questions about the portfolio.
2. **Bot context:** `python analyst.py context TICKER` for each.
3. **TradingView:**
   1. `tv_health_check`. If it is not connected: `tv_launch` with `kill_existing: false`, then
      check again. If still down, answer without TradingView and say «TradingView недоступен».
   2. `chart_get_state`: remember the symbol and timeframe.
   3. For each asset:
      - `chart_set_symbol` (EXCHANGE:SYMBOL when known; `symbol_search` when not);
      - `chart_set_timeframe` "D";
      - `quote_get` **without** a symbol argument (with one it can return another symbol), and
        check that the returned symbol matches;
      - `data_get_ohlcv` with `summary: true` and counts 250 and 60;
      - `chart_set_timeframe` "W" with `data_get_ohlcv` summary count 104.
   4. **Restore** the saved symbol and timeframe.
   5. **Never:**
      - `data_get_study_values` (huge output, and it exposes the user's private scripts);
      - indicators, drawings, alerts, Pine, replay, layouts or UI clicks.
4. **Reason:**
   - the trend on the daily and weekly ranges;
   - where the price is in its 3-month and 1-year range;
   - levels: the 3-month low and high and the 1-year high;
   - the model's score and decision, insiders and news;
   - what would change the view.
5. **Answer** in Russian, compactly:
   - bullets;
   - an HTML-bold header;
   - the model's decision and score quoted;
   - price levels only from TradingView or the bot, never invented;
   - one «🎯 Итоговый вердикт:» line;
   - no disclaimers;
   - only `<b>` tags, no bare `<` or `>`.

- **The Telegram prompt** (`claude_analysis_prompt.txt`):
  - processes up to 10 pending rows;
  - kind ticker → the analysis of that ticker;
  - kind question → read the question by id, then answer it;
  - send the answer through `/tmp/claude_analysis_msg.txt` and `telegram_notify.send_text`, and mark the row processed only after a confirmed send;
  - the scope rules stay: no file edits, no other jobs.
- **The terminal prompt** (`claude_ask_prompt.txt`):
  - the question is appended by `analyst.ask`;
  - print the answer, nothing else;
  - the answer is plain text: `<b>` tags are stripped before it's printed.

## 8. Testing (offline)

- **model_score:**
  - every line of every part with boundary values;
  - the caps and the clamp;
  - stake conviction;
  - red flag → block, and news capped;
  - decision thresholds and tradeability, with unknown sizes;
  - the T212 label;
  - coin trend, flows, caution, decision;
  - typical move, the stop clamp, sizing and caps.
- **model:**
  - books created once;
  - buys ranked by score and sized;
  - skips: мало, нет денег, мест нет, сектор заполнен, недавно продан;
  - no duplicate;
  - fills at the next close;
  - each exit: the trailing stop with a peak, an insider sale after the fill, an activist cut, dead money at 60 business days, 365 days, a red flag, trend down, a caution;
  - DayReport contents;
  - a failing sleeve doesn't stop the other;
  - score_today trades nothing.
- **bot:**
  - journal tier = the model's decision;
  - the message is sent only when there's something to say;
  - a filtered run skips the model;
  - no `strategy.select` left.
- **positions:** the new exits on /bought positions, and `stop_pct` stored at open.
- **Views:** the day message (HTML-safe), the scored list, the model summary and archive line,
  the monthly report once per month.
- **Analyst:**
  - `claude_command` contents and PATH;
  - `process-queue` doesn't start Claude on an empty queue;
  - ticker validation;
  - the context, portfolio and question outputs;
  - Telegram routing (a ticker, `/ask`, free text, `/portfolio`) and the question timeout
    fallback;
  - the prompt files name every allowed TradingView tool, forbid `data_get_study_values`, and
    say to restore the chart.
- The existing suite stays green, apart from the deleted tier/book/calibration tests.

## Out of scope

- The 11 more coins (the next project).
- The 20-year SEC history test of the stock rules.
- The parked range design.
- Any real trading.
