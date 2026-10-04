# Remove the virtual model portfolio; track only the real account

Date: 2026-10-04. Requested by the user: "remove agent trading, only track my real trading 212 account. no more starting
balance of imaginary EUR 100 000 account".

It is built on top of docs/superpowers/specs/2026-10-04-signal-message-style.md (same branch) and supersedes that
spec's model-buy / model-sale / weekly-summary parts where they differ.

## What goes

- **The virtual portfolio and everything built on it:**
  - the MODEL-S / MODEL-C books, their virtual orders, fills, positions, sales, snapshots and benchmark (the 70/30
    mix);
  - position sizing and its caps (1 % risk, 10 % per stock, 12 stocks, 3 per sector, 35 % per coin, the €500
    minimum);
  - the stock and coin re-buy cooldowns of the books;
  - the success line (182 days, 20 trades) and the monthly report.
- **Commands and views:**
  - the Telegram `/model` command;
  - the menu's «Модельный портфель»;
  - `python paper.py`;
  - the «модель тоже держит» note;
  - the analyst's model-portfolio section.
- **Code:**
  - `paper_report.py` and the order/book/fill/equity parts of `paper.py`. The generic price helpers are still used
    (`listing`, `fee` if still used, `_closes`, `Prices`, `close_on_or_before`, `first_close_after`,
    `business_days_between`, `history_days` if used, `PRICE_DAYS`, `_VENUE_CURRENCY`). Move them to a new `prices.py`
    and delete `paper.py`.
  - in `model.py`: books, `create_books`, `model_value`, `Trade`, the trading half of `run`, `stock_exit_reason`,
    `coin_exit_reason`, the sell/buy/sleeve/bench helpers, and the sizing constants;
  - in `model_score.py`: `position_size`, `RISK_PER_TRADE`, `STOCK_CAP`, `COIN_CAP`, `MIN_ORDER_EUR`.
- **The database is not touched.** The paper tables and their rows stay where they are. Nothing reads or writes
  them any more. The CREATE TABLE statements stay in `db.SCHEMA`, so old databases and tests keep working.

## What stays

- **Scoring** (`model_score.py`, `model.candidate_signals`, `model.score_today`, the cached day scores, the
  journal's `tier` = the model's decision): unchanged. It still runs on every full daily run.
- **The exit rules for the user's real positions** (`positions.py`): unchanged. They keep using the constants in
  `model.py` (`MAX_HOLD_DAYS`, `DEAD_MONEY_BDAYS`, `DEAD_MONEY_MIN_RETURN`, `FALLBACK_STOP`), `model.default_news`
  and the activist-cut check.
- **Trading 212 tracking**, `/portfolio`, `/bought`, `/sold`, ticker lookups, `/ask`, and the analyst.

## New: weekly buy signals without a portfolio

**`model.score_day(conn, today=None, *, fetch=None, news_fn=None, trend_fn=None, signals=None, t212=None) ->
ScoreReport`** replaces `model.run`. It scores, stores the day's scores (as `run` did), and returns
`ScoreReport(scored, decisions, complete)`. `complete` is False when scoring raised (caught and logged). It doesn't
trade.

**`buy_signals` table:** `id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, source TEXT, company TEXT, kind TEXT
(stock|crypto), score REAL, stop_pct REAL, reasons TEXT (JSON), t212 INTEGER, sent_at TEXT (ISO date)`.

**`signals_weekly.pick_buys(conn, today, scored) -> list`** (a new small module; or put it in bot.py if it stays
under ~80 lines). It returns the scores to signal this week:
- only decision BUY, highest total first;
- not a name the user already holds: an open row in `positions`, compared by ticker; a coin by its `CRYPTO:` ticker;
  an Oslo/Stockholm listing with its source too;
- not a ticker with a `buy_signals` row in the last `RESIGNAL_DAYS = 30` days;
- at most `WEEKLY_BUY_LIMIT = 5`.

**The message** (style spec §1, as amended):
`🟢 <b>GME!</b>: покупка — 2 инсайдера из руководства; CEO среди покупателей; балл 70, стоп −10%`, plus
«, нет на Trading 212» when the score's `t212 is False`. There is no fill text any more.

**Weekly run** (`bot`): the weekly keys and the Fri–Sun window stay.
1. `report = model.score_day(...)` runs on every full run, as scoring did.
2. On a weekly run with a complete report, pick the buys once for the week and store the picked list in kv
   (`weekly_buys_<week>`, the JSON of what the message needs). A retry sends the same picks. Set the week's buys key.
3. Send, in order:
   1. each picked buy (key `buy:<ticker>`). After its message went out, insert its `buy_signals` row;
   2. the week's group exits (key `exit:<journal id>`), as the style spec says;
   3. the weekly summary, last.
   - The per-week sent-keys list and the "stop on a failed send, resume next run" rule are as in the style spec.
   - There are no model-sale messages.
4. The message waits for a complete scoring pass. On Sunday it goes out regardless, with the warning line.

**The weekly summary** (`weekly.format_summary(conn, today, *, html=True, scoring_failed=False)`), about the real
account:

```
📊 <b>Неделя 03.10–09.10</b>: счёт Trading 212 €12 346 (за неделю +2,9%)
Позиций 43: лучшие — SMCI +31,0%, BBD +12,4%, GME +8,1%; худшие — XYZ −22,5%, ABC −9,0%, DEF −4,2%
Сигналов за неделю: покупок 2, на продажу 1, групповых выходов 1
⚠️ Оценка сигналов на этой неделе не отработала — покупок не было.     ← only with scoring_failed
```

- **Line 1:** the newest `t212_equity` value, when it isn't older than 3 days, with the week change when a value a
  week earlier exists. With no Trading 212 data, the line is «📊 <b>Неделя 03.10–09.10</b>».
- **Line 2:** the open positions (Trading 212 and manual).
  - The result is since purchase: the last known price vs the entry price, using the stored data only. No network
    call.
  - It shows the top 3 and the bottom 3 by result. With fewer than 6 priced positions, list them all once.
  - With no positions: «Позиций нет.»
- **Line 3:** the counts for the week:
  - buy signals sent;
  - sell alerts: `positions.close_alerted_at` in the week;
  - group exits journaled.
  - With all three zero: «Сигналов за неделю не было.»
- **The monthly report is removed** (no `maybe_send_monthly_report`).

## Views

- **Telegram:**
  - `/model` is removed. Like any unknown command, it answers with the help text.
  - HELP_TEXT drops the model lines. It says:
    - «Сигналы на покупку приходят по пятницам, сигнал на продажу по вашим позициям — сразу.»
    - «/portfolio — ваш счёт Trading 212 и позиции /bought.»
- **`/portfolio`:** no «модель тоже держит» line.
- **Menu:**
  - «1) Сигналы»: the scored list, as today;
  - «2) Досье…»;
  - «3) Мой портфель»: the `/portfolio` text, plain;
  - «4) Спросить аналитика»;
  - «0) Выход».
- **Analyst:**
  - `portfolio` prints «ВАШИ ПОЗИЦИИ» (as today), then «СИГНАЛЫ НА ПОКУПКУ ЗА 30 ДНЕЙ» (from `buy_signals`: date,
    ticker, score), then «НАБЛЮДЕНИЕ» (WATCH scores, up to 10);
  - `context TICKER` keeps the model's score part and drops the model's-position part;
  - analyst_method.txt and the prompts no longer mention a model portfolio. The bot "оценивает сигналы", it does
    not hold positions.
- **README:**
  - Remove the model-portfolio material: books, sizing, caps, benchmark, the success line, the monthly report,
    `/model`, `python paper.py`.
  - Describe: scoring → Friday buy signals (at most 5 new ones, not already held, not repeated for 30 days) → your
    Trading 212 account is the only portfolio → sell alerts.

## Tests

- **Delete** the tests of removed behaviour: tests/test_paper.py's engine tests, tests/test_paper_report.py, the
  portfolio/trading tests in tests/test_model.py and tests/test_model_views.py. Move the price-helper tests to
  tests/test_prices.py.
- **Keep or adapt** every test of kept behaviour: scoring, cached scores, journal tiers, positions' exits, Trading
  212, the analyst, telegram routing, and the signal style.
- **New tests:**
  - `score_day` scores and stores, trades nothing (no `paper_*` rows written), and reports `complete`;
  - `pick_buys`: BUY only, the order, the limit of 5, skipping a held name (a T212 holding, a manual one, a coin, an
    Oslo listing vs a US name), and the 30-day re-signal rule (29 days → skipped, 31 → allowed);
  - the weekly flow:
    - picks are stored once and re-sent after a failed send without re-picking;
    - a `buy_signals` row only after its message went out;
    - the summary last;
    - a second run in the week sends nothing;
    - an incomplete scoring pass on Friday → nothing sent, and Saturday retries;
    - Sunday sends with the warning;
  - the summary: the account line (fresh / stale / none), the week change, best/worst with ≥6 and with <6 positions,
    no positions, the counts, the quiet text;
  - `/model` → help; `/portfolio` without the model note; menu option 3; the analyst sections;
  - a repository-wide check that nothing imports `paper` or `paper_report`, and that no code writes to the
    `paper_*` tables.
