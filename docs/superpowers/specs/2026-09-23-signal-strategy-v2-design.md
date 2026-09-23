# Signal strategy v2: tiers, a Trading 212 buy list, and position exits

Date: 2026-09-23. Piece 1 of 4. The later pieces, each designed separately: (2) an
always-on monitor polling SEC's live filing feed, market data and news; (3) live
price/volume/news confirmation feeding these tiers; (4) a disclosure-independent
"market movers" feed.

## Goal

A short list the user can act on within days, or the same day for the strongest,
holding for weeks to months. Plus a signal to close each position they actually
opened. Target volume: about 2–3 "Сильный" and 5–10 "Кандидат" signals a week.

It replaces today's rule of "every signal scoring ≥ 35" in both the daily Telegram
digest and the menu's "Сигналы" view.

## Tier rules

Scope: buy-side signals only (clusters, big solo buys, 13D/G stakes, crypto
inflows). Exit signals and crypto outflows never appear as buy signals.

### Floors (every stock)

1. On Trading 212 (`trading212.Availability.can_buy`). With no key or no cached
   list, stocks aren't filtered, and the message says so.
2. Disclosed within the last 3 days (`cluster.disclosed_on`).
3. Market cap ≥ €300m and average daily traded value ≥ €1m. If either is
   **unknown** (typically ISIN-keyed German/Swedish issuers), the stock is capped at
   "Кандидат" and labelled "размер неизвестен".

### Stocks

**Сильный**: every one of:
- Buyers include company insiders (officers or directors), not only >10% holders
  (`holder_only` is false).
- Open-market purchases. The existing defaults already exclude 10b5-1 plans and
  derivative rows.
- Size and liquidity known and above the floors.
- At least one of:
  - **(a) Broad cluster**: ≥ 3 distinct insiders in the window.
  - **(b) Top-exec cluster**: ≥ 2 insiders, one of them CEO, CFO or Chair.
  - **(c) Top-exec conviction**: one CEO or CFO buying ≥ €250k that grows their
    holding by ≥ 10% (`position_increase_pct`).

**Кандидат**: every other signal that passes the floors, including >10%-holder
clusters, 13D activist stakes (run_daily's tuning: ≥ 10%, 13D, new positions),
Congress, and European insiders. Ranked by the existing `score_signal`; the top 10
are shown.

Top-exec detection needs structured roles. The finders keep, next to
`member_names`, a parallel `member_roles` list with a normalised role
(`ceo`/`cfo`/`chair`/`officer`/`director`/`holder`/`other`), mapped from:
- SEC `officer_title`: "Chief Executive Officer", "CEO", "President and CEO",
  "Executive Chairman", "Chief Financial Officer", …
- Sweden `position`: "Verkställande direktör (VD)" → ceo,
  "Ekonomichef/finanschef/finansdirektör" → cfo, "Styrelseordförande" → chair.
- BaFin `position`: "Vorstand" → officer, "Aufsichtsrat" → director,
  "in enger Beziehung" (a closely associated person) → other.
- Norway: no role is stored, so every buyer is `other`. Norway signals can
  therefore only reach Сильный through rule (a).

### Crypto (BTC, ETH)

A **big inflow** is any one of:
- a spot-ETF day ≥ $400m;
- a spot-ETF inflow run of ≥ 3 days totalling ≥ $500m;
- a company treasury purchase ≥ €50m;
- with `--onchain`, net exchange withdrawals over `ONCHAIN_MIN_COINS`.

The **price confirms** when the 7-day return is > 0 and the last close is above the
20-day average, from daily Yahoo `BTC-USD`/`ETH-USD` closes cached in
`price_history_cache`.

- **Сильный**: a big inflow whose price confirms.
- **Кандидат**: a big inflow whose price doesn't confirm, or couldn't be checked
  ("цена не проверена").
- Smaller crypto signals don't appear.

### Explanations

Every signal carries the rules it met or missed, rendered as lines like
`✓ 3 инсайдера · ✓ CEO · ✓ €1.2 млрд / €8 млн в день` or
`✗ размер неизвестен`.

## Positions and "Закрыть"

Only positions the user reports are tracked.

Telegram commands (`telegram_bot.py`), available only from the configured chat:
- `/bought TICKER [price]` opens a position. Without a price it uses the last
  close. It stores the members of the most recent Сильный signal on that ticker, if
  there is one; otherwise the position has no insiders to watch.
- `/sold TICKER` closes it (reason: manual).
- `/positions` lists open positions with the return and days held.

The bot never reads or trades the Trading 212 account.

`positions.check_exits(conn)` runs in every daily job. It fires one Закрыть per
position on the **first** of:
1. **Insiders sell.** Any of the position's insiders sells after the open date,
   where 10b5-1 planned sales don't count:
   - `sec_sales`, with `is_10b5_1 = 0`, matched by `owner_name`;
   - `sec_proposed_sales`, a Form 144 filing, matched by `person_name`;
   - `txn_type = 'S'` rows in BaFin, Norway and Sweden.

   Names are compared with `cluster.name_key`.
2. **Time.** 90 days since the open.
3. **Stop-loss.** The last close is ≤ −15% from the entry. If there's no price that
   day, only this check is skipped, and the skip is logged.

The alert names the trigger. The position is marked `close_alerted` and closes only
on `/sold`, so the user stays in control. Parameters are `EXIT_*` constants in
`positions.py`: `EXIT_MAX_DAYS = 90`, `EXIT_STOP_LOSS_PCT = 15`.

Table `positions`: `id, ticker, source, opened_at, entry_price, currency,
insiders (JSON), signal_id (the `signal_journal` row of the Сильный signal it came from,
or NULL), closed_at, close_reason, close_alerted_at`.

## Components

| Unit | Responsibility | Depends on |
|---|---|---|
| `strategy.py` (new) | `classify(conn, signal, t212) -> TierResult(tier, met, missed)`; `select(conn, signals, t212) -> (strong, candidates)` with floors, caps and ordering; the rule constants | `cluster`, `trading212`, `marketcap` (via `enrich_signals`), `crypto` |
| `crypto.price_trend(conn, symbol)` (new) | 7-day return, last close vs 20-day average; daily-cached | `price_history_cache`, yfinance |
| `positions.py` (new) | positions table access; `open/close/list`; `check_exits` | `db`, `cluster.name_key`, price via `crypto.yf_symbol` series |
| finders (`cluster/buys.py`) | add `member_roles` | — |
| `telegram_bot.py` | `/bought`, `/sold`, `/positions` | `positions` |
| `telegram_notify.py` | `format_tiered_digest(strong, candidates, closes)` with 🔥 / 👀 / 🚪 sections and rule lines | — |
| `bot.run_cluster_pass` | finders → `strategy.select` → `positions.check_exits` → one message; commit alert state for what was sent | above |
| `menu.show_signals` | the same three sections plus open positions | above |
| `run_daily.sh` | drop `--min-score 35` (the tiers replace it); keep the stake flags | — |

Data flow for the daily job:

finders → recency, T212 and size floors → `enrich_signals` (only survivors reach the
network) → `strategy.select` → `positions.check_exits` → `format_tiered_digest` →
send → commit alert state and journal.

Dedup is unchanged (`should_alert` / `_is_new`): a signal already sent doesn't
resend unless it grows.

`--min-score` is kept for manual runs but no longer used by run_daily.sh.

## Error handling

- No T212 key or no cached list: stocks aren't filtered, and the message says so.
- Unknown size: capped at Кандидат, labelled.
- Crypto price check fails: capped at Кандидат, labelled "цена не проверена".
- No price for a stop-loss check: that check is skipped for the day; the others
  still run.
- Bad `/bought` input (unknown ticker, non-numeric price): the reply shows usage;
  nothing is stored.
- Nothing qualifies: the digest isn't sent; the menu says so.

## Testing

Offline, in the existing style (`conftest` seeds, a fake `enrich_signals`,
fixed price series):
- each Сильный rule, met and missed;
- each floor, including unknown size capping at Кандидат;
- role mapping for every source;
- crypto: confirmed, unconfirmed, and check failed;
- each close trigger, and a 10b5-1 sale *not* triggering;
- `/bought` with and without a price, `/sold`, `/positions`, bad input;
- the three-section message;
- the menu view with positions.

## Calibration (before shipping)

Replay day by day over stored history with `today` frozen per day: run the finders,
floors and `strategy.select`, using current cached market caps as an approximation.
Count Сильный and Кандидат per week.

History is dense only from mid-August 2026 (about 5 weeks of SEC data; Sweden
mostly September), so the result is a rough check, not a tuning. If the weekly
counts are far from 2–3 and 5–10, adjust rule (a)'s insider count and rule (c)'s
€250k and 10%, show before/after counts, and let the user approve the numbers.

## Out of scope here

- Live polling and alerts the moment something qualifies (piece 2).
- News or price-breakdown exits, and live confirmation (piece 3).
- The market movers feed (piece 4).
- Reading the Trading 212 portfolio.
- Profit targets (not requested).
