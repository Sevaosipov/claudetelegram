# Model Portfolio and TradingView Analyst Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the tiered signals, their filters and the 13 paper books with one scored strategy traded by a virtual model portfolio, and add a Claude analyst that answers questions in Telegram and the terminal using the TradingView MCP server.

**Architecture:** `model_score.py` holds the pure scoring. `model.py` runs the model portfolio on paper.py's existing order/fill/valuation engine, with two new books: MODEL-S (€70k, stocks) and MODEL-C (€30k, BTC/ETH). `bot.py` journals new signals with the model's decision and sends one daily "model portfolio" message. `analyst.py` builds the headless `claude -p` command, whose allowed tools include the TradingView MCP read and navigation tools, and prints the bot context Claude reads. Telegram and the terminal both go through it.

**Tech Stack:** Python 3.12, SQLite, pytest (offline: tests/conftest.py blocks sockets), launchd, Claude Code headless (`claude -p`), and the TradingView MCP server (user-scoped in ~/.claude.json: `node /Users/sevastians/Tools/tradingview-mcp`).

**Spec:** docs/superpowers/specs/2026-09-30-model-portfolio-and-analyst-design.md

## Global Constraints

- **Run tests with** `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q` from the worktree root `/Users/sevastians/Desktop/disclosure-bot/.worktrees/model-portfolio`. Tests must stay offline: no network, no real `claude` run. Use stubs and seams.
- **The bot never places real trades.** Nothing here may call Trading 212 order endpoints. Never read, print or log `.env` values.
- **Every commit message ends with exactly:** `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
- **User-facing text** (Telegram, menu, CLI) is in Russian. No "not investment advice" disclaimers anywhere.
- **Telegram HTML:** only `<b>`/`<pre>` tags. All dynamic text goes through `telegram_notify._esc`.
- **Match the surrounding code style:** module docstrings that explain why, constants at the top, `from __future__ import annotations`.
- **Every threshold below is a module constant** with the exact value given.

---

### Task 1: Pure scoring — model_score.py

**Files:**
- Create: `model_score.py`
- Test: `tests/test_model_score.py`

**Interfaces:**
- Consumes:
  - signal objects from `cluster` (`ClusterSignal`, `StakeSignal`, `CryptoSignal`) or `types.SimpleNamespace` stand-ins with the same attributes;
  - `cluster.roles.INSIDER_ROLES`.
- Produces (exact names; later tasks import these):

```python
STOCK_BUY = 60.0
STOCK_WATCH = 45.0
COIN_BUY = 60.0
MIN_MCAP_EUR = 20e6
MIN_ADV_EUR = 100_000
MIN_CLOSES = 21
STOP_MULT = 3.0
STOP_MIN = {"stock": 0.10, "crypto": 0.15}
STOP_MAX = {"stock": 0.25, "crypto": 0.35}
RISK_PER_TRADE = 0.01
STOCK_CAP = 0.10          # of the model's value
COIN_CAP = 0.35           # of the crypto sleeve's value
MIN_ORDER_EUR = 500.0
BUY, WATCH, SKIP, BLOCK = "buy", "watch", "skip", "block"

@dataclass
class Part:
    points: float
    lines: list[str]

@dataclass
class StockScore:
    ticker: str
    source: str
    company: str
    signal: object
    insiders: float
    triggers: float
    momentum: float
    news: float
    total: float
    decision: str            # BUY / WATCH / SKIP / BLOCK
    reasons: list[str]       # the parts' lines, insiders first
    block: str | None        # the red-flag headline, when decision == BLOCK
    untradeable: str | None  # why a 60+ score is only WATCH, else None
    t212: bool | None        # False -> label «нет на T212»; None -> not checked
    stop_pct: float | None
    last_close: float | None
    kind: str = "stock"

@dataclass
class CoinScore:
    coin: str                # "BTC"
    ticker: str              # "CRYPTO:BTC"
    trend: float
    flows: float
    news: float
    total: float
    trend_up: bool
    trend_down: bool
    caution: str | None
    block: str | None
    decision: str
    reasons: list[str]
    stop_pct: float | None
    last_close: float | None
    kind: str = "crypto"

def insider_part(sig) -> Part
def stake_part(sig) -> Part
def trigger_part(sig, *, activist: bool, passive_big: bool, politicians: bool) -> Part
def momentum_part(closes: list[float]) -> Part
def news_part(headlines: list[dict] | None, *, coin: bool = False) -> tuple[Part, str | None]
def typical_move(closes: list[float]) -> float | None
def stop_distance(closes: list[float], kind: str) -> float | None
def untradeable_reason(sig, closes: list[float]) -> str | None
def score_stock(sig, closes: list[float], headlines: list[dict] | None, *, activist: bool = False,
                passive_big: bool = False, politicians: bool = False, t212: bool | None = None) -> StockScore
def coin_trend(closes: list[float]) -> dict | None
def score_coin(coin: str, closes: list[float], *, bullish_flow: bool, caution: str | None,
               headlines: list[dict] | None) -> CoinScore
def position_size(model_value: float, sleeve_value: float, stop: float, kind: str) -> float
```

**Rules** (spec §1–§3 are binding; exact values here):

- **`insider_part(sig)`:**
  - A signal whose `source` is "HOUSE" or "SENATE" → `Part(0, [])`.
  - A signal with `holder_only` true → `Part(15, ["только крупные акционеры (>10%)"])`.
  - Otherwise:
    - `mgmt` = buyers in `sig.buyers` whose `.role` is in `INSIDER_ROLES`. With no `buyers` list, fall back to `sig.buyer_count` as the count and no roles.
    - Count points: 1 → 22, 2 → 34, 3 → 42, 4+ → 46. A count of 0 gives 0.
    - A `ceo`/`cfo` among mgmt → +10 with the line "CEO среди покупателей" (use the role label CEO/CFO). Else a `chair` → +5.
    - `value_pct_of_mcap`: ≥0.2 → +10, ≥0.05 → +6, ≥0.01 → +3. It is a percent, so 0.05 means 0.05%.
    - `position_increase_pct`: ≥30 → +6, ≥10 → +3.
    - `first_buy` → +4.
    - Cap at 60.
  - **Lines** (Russian, short), e.g.:
    - «3 инсайдера из руководства»;
    - «CEO среди покупателей»;
    - «0,06% компании»;
    - «позиция +35%»;
    - «первая покупка».
- **`stake_part(sig)`:**
  - For `form_type` starting with "SCHEDULE 13D" (`sig.is_activist`): `30 + 2 * min(max(sig.percent - 5, 0), 10)`.
  - Otherwise: `15 + min(max(sig.percent - 5, 0), 10)`.
  - +5 when `sig.prev_percent is not None and sig.percent - sig.prev_percent >= 1.0`.
  - Cap at 60.
  - Lines: «активист 13D: 8,0%» or «13G: 12,0%», and «доля +2,0 п.п.».
- **`trigger_part`:**
  - `activist` → +15 «рядом активист 13D».
  - `passive_big` → +6 «рядом 13G от 10%».
  - `politicians` → +5 «покупают политики».
  - +5 «подтверждает: <sources>» when `sig.corroborated_by` has any source not already counted. Politicians count as HOUSE/SENATE and activist/13G as SEC13DG.
  - Cap at 20.
  - The caller passes `activist=False` / `passive_big=False` when the signal itself is that stake, and `politicians=False` when the signal itself is House/Senate. `score_stock` handles that: for a `StakeSignal`, `activist` and `passive_big` are forced False; for a House/Senate signal, `politicians` is forced True.
- **`momentum_part(closes)`:** closes are oldest first.
  - `ret6 = closes[-1]/closes[-127]-1` when `len >= 127`: >0.25 → +8 «6 мес. +31%»; >0 → +5.
  - With `len >= 200`, the last close above the mean of the last 200 → +5 «выше 200-дн. средней».
  - `ret1 = closes[-1]/closes[-22]-1` when `len >= 22`: >0 → +2.
  - Cap at 15.
- **`news_part(headlines, coin=False)`:**
  - `headlines` is None or empty → `(Part(0, []), None)`.
  - Each item is a dict with a "title" key. The title is lower-cased and matched against:
    - `RED_FLAGS = ("fraud", "sec investigation", "subpoena", "going concern", "bankruptcy", "chapter 11", "restatement", "delisting", "delisted", "public offering", "secondary offering", "private placement", "at-the-market")`, plus for coins `COIN_RED_FLAGS = ("hack", "exploit", "stolen", "sec lawsuit")`;
    - `NEGATIVE = ("downgrade", "cuts guidance", "cuts forecast", "misses estimates", "lawsuit", "probe", "recall", "resigns", "halts", "short seller")`;
    - `POSITIVE = ("upgrade", "raises guidance", "raises forecast", "beats estimates", "buyback", "record revenue", "wins contract", "fda approval")`.
  - Scoring:
    - The first red-flag title is returned as the second element, and a red-flag title doesn't also count as negative.
    - Each other title counts at most once per list: negative −10, positive +5.
    - Clamp to −30..+10.
    - Lines: «новости: 1 плохая, 2 хорошие».
- **`typical_move(closes)`:** needs `len >= MIN_CLOSES` (21). It is the mean of `abs(c[i]/c[i-1]-1)` over the last 20 changes. Otherwise None.
- **`stop_distance(closes, kind)`:** `min(max(STOP_MULT * typical_move, STOP_MIN[kind]), STOP_MAX[kind])`, or None when `typical_move` is None.
- **`untradeable_reason(sig, closes)`:**
  - `len(closes) < MIN_CLOSES` → «нет цены»;
  - `market_cap_eur` and `avg_daily_value` both None → «размер неизвестен»;
  - cap known and < MIN_MCAP_EUR → «компания меньше €20 млн»;
  - adv known and < MIN_ADV_EUR → «торгуется меньше €100 тыс. в день»;
  - otherwise None.
- **`score_stock`:**
  - The insider part is `stake_part` for objects with a `percent` attribute, else `insider_part`.
  - `total = max(0, min(100, insiders + triggers + momentum + news))`.
  - Decision:
    - a red flag → BLOCK with `block` set;
    - total ≥ STOCK_BUY: BUY if `untradeable_reason` is None, else WATCH with `untradeable` set;
    - total ≥ STOCK_WATCH → WATCH;
    - else SKIP.
  - `stop_pct = stop_distance(closes, "stock")`, and `last_close = closes[-1]` when there are closes.
- **`coin_trend(closes)`:**
  - None when `len < 121`.
  - Otherwise returns `{"above_ma100": last > mean(last 100), "ret20": ..., "ret60": ..., "ret120": ...}` (returns as fractions over 20/60/120 closes back), plus:
    - `"up"`: `above_ma100 and count(ret > 0) >= 2`;
    - `"down"`: `not above_ma100 and ret20 < 0`.
- **`score_coin`:**
  - **Trend:** 15 for above_ma100, plus 15 for each positive return.
  - **Flows:** +15 when `bullish_flow`, −20 when `caution` is set.
  - **News:** `news_part(headlines, coin=True)`.
  - `total` is clamped to 0–100.
  - **Decision:**
    - BLOCK if a red flag or a caution (reason «осторожно: …» or «новости: …»);
    - else BUY if `trend["up"]` and total ≥ COIN_BUY;
    - else WATCH.
    - With `coin_trend` None: decision WATCH with the reason «мало истории».
  - `stop_pct = stop_distance(closes, "crypto")`.
- **`position_size(model_value, sleeve_value, stop, kind)`:**
  - `risk = RISK_PER_TRADE * model_value / stop`;
  - `cap = STOCK_CAP * model_value` for "stock", `COIN_CAP * sleeve_value` for "crypto";
  - returns `min(risk, cap)`.

- [ ] **Step 1: Write the failing tests** in `tests/test_model_score.py`, using `types.SimpleNamespace` signals and `cluster.roles.Buyer`:
  - **Insider part:**
    - counts 1/2/3/4/5 → 22/34/42/46/46;
    - CEO +10; CFO +10; a Chair alone +5; CEO and Chair together only +10;
    - pct 0.009/0.01/0.05/0.2 → 0/3/6/10;
    - increase 9/10/30 → 0/3/6;
    - first_buy +4;
    - cap: 5 insiders + CEO + 0.3% + 50% + first = 46+10+10+6+4 = 76 → 60;
    - holder_only → 15;
    - HOUSE → 0;
    - no buyers list with buyer_count 3 → 42.
  - **Stake part:**
    - 13D 5% → 30, 8% → 36, 20% → 50;
    - 13G 12% → 22;
    - prev 6.0 → 8.0 adds 5; prev 7.5 → 8.0 adds 0;
    - cap 60.
  - **Triggers:**
    - each flag, and all three plus corroboration → 20 (capped);
    - corroboration by "HOUSE" is not counted twice when politicians is true;
    - a stake signal's own activist flag is ignored.
  - **Momentum:**
    - 127 rising closes (+30%) → 8+2;
    - 200 closes with the last above the mean → +5;
    - fewer than 22 closes → 0;
    - cap 15.
  - **News:**
    - None → 0;
    - "Company announces $50M public offering" → red flag;
    - one title matching two negative phrases ("downgrade" and "lawsuit") → −10; two downgrade titles → −20;
    - four negatives → −30;
    - three positives → +10;
    - a coin "exchange hack" → red flag only when coin=True.
  - **`typical_move`:** alternating closes of 100/101 → ≈0.00995; 20 closes → None.
  - **`stop_distance`:** tiny moves → 0.10 (stock) / 0.15 (crypto); big moves → 0.25 / 0.35; a middle value is 3× the move.
  - **`untradeable_reason`:** each case in order.
  - **`score_stock` decisions:**
    - 3 mgmt + CEO + 0.06% + 12% on flat closes (no momentum) → 42+10+6+3 = 61 → BUY;
    - the same with a red-flag title → BLOCK;
    - the same with unknown size → WATCH with `untradeable` «размер неизвестен»;
    - 2 directors, no extras → 34 → SKIP;
    - total 45 → WATCH;
    - total clamped at 100;
    - `t212` passed through.
  - **`coin_trend`/`score_coin`:**
    - a steadily rising series → up, trend 60, BUY;
    - a falling series → down, WATCH;
    - rising with a caution → BLOCK;
    - 100 closes → WATCH «мало истории»;
    - bullish_flow adds 15.
  - **`position_size`:**
    - model 100k, stop 0.10, stock → min(10k, 10k) = 10k;
    - stop 0.25 → 4k;
    - crypto with sleeve 30k and stop 0.15 → min(6.67k, 10.5k).
- [ ] **Step 2:** Run `…/python -m pytest tests/test_model_score.py -q`. Expected: fails (no module).
- [ ] **Step 3:** Implement `model_score.py` exactly as above.
- [ ] **Step 4:** Run the file's tests, then the whole suite. Expected: all pass.
- [ ] **Step 5:** Commit `feat(model): pure stock and coin scoring` with the trailer.

---

### Task 2: The model portfolio engine — model.py

**Files:**
- Create: `model.py`
- Modify:
  - `db.py`: `_ADDED_COLUMNS` gets `("paper_orders", "stop_pct", "REAL")`, `("paper_orders", "score", "REAL")`, `("paper_positions", "stop_pct", "REAL")` and `("paper_positions", "score", "REAL")`;
  - `paper.py`:
    - `place_buy` accepts keyword-only `stop_pct: float | None = None, score: float | None = None` and passes them to `_record`;
    - `_record` stores them;
    - `_fill_buy` copies `stop_pct` and `score` from the order into the position.
- Test: `tests/test_model.py`

**Interfaces:**
- Consumes:
  - Task 1's `model_score` (all names above);
  - `paper.listing`, `paper.Prices`, `paper.fill_orders`, `paper.mark_to_market`, `paper._snapshot`, `paper.place_buy`, `paper.place_sell`, `paper.open_positions`, `paper.pending_orders`, `paper.closed_positions`, `paper.book_value`, `paper.cash`, `paper.close_on_or_before`, `paper.business_days_between`, `paper.PRICE_DAYS`, `paper.history_days`;
  - `positions._insider_sale`;
  - `strategy.buy_side_signals`;
  - `cluster.disclosed_on`, `cluster.enrich_signals`;
  - `crypto.ticker`, `crypto.price_trend`, `crypto.trend_confirms_down`;
  - `sources.news`, `assets.stock_asset`, `assets.crypto_asset`.
- Produces:

```python
STOCK_BOOK, CRYPTO_BOOK = "MODEL-S", "MODEL-C"
BOOKS = (STOCK_BOOK, CRYPTO_BOOK)
STOCK_START_EUR = 70_000.0
CRYPTO_START_EUR = 30_000.0
COINS = ("BTC", "ETH")
MODEL_SIGNAL_DAYS = 14
NEWS_MIN_PRESCORE = 35.0
MAX_STOCK_POSITIONS = 12
MAX_PER_SECTOR = 3
REBUY_COOLDOWN_DAYS = 30
DEAD_MONEY_BDAYS = 60
DEAD_MONEY_MIN_RETURN = 0.05
MAX_HOLD_DAYS = 365
CAUTION_DAYS = 7
STAKE_LOOKBACK_DAYS = 30
FINDER_CLUSTER_KWARGS = {"min_value": 50_000, "solo_threshold": 250_000}
FINDER_STAKE_KWARGS = {"min_percent": 5.0, "activist_only": False,
                       "new_positions_only": False, "max_age_days": 14}

@dataclass
class Trade:
    side: str                 # "buy" | "sell"
    ticker: str
    company: str
    amount_eur: float | None  # buy: the order's amount; sell: the position's last value
    stop_pct: float | None
    score: float | None
    reasons: list[str]        # buy: the score's reasons (max 3); sell: [the exit reason]
    result: float | None      # sell: last_value / cost_eur - 1
    t212: bool | None = None

@dataclass
class DayReport:
    buys: list[Trade]
    sells: list[Trade]
    scored: list              # StockScore and CoinScore, highest total first
    value: float | None       # MODEL-S + MODEL-C value today
    bench: float | None       # the two books' benchmark values summed (the 70/30 mix)
    decisions: dict[str, str] # ticker -> decision, for the journal

def create_books(conn, today: dt.date) -> None
def candidate_signals(conn, today: dt.date) -> list
def score_today(conn, today: dt.date | None = None, *, fetch=None, news_fn=None, trend_fn=None,
                signals=None, t212=None, prices=None) -> list
def stock_exit_reason(conn, pos: dict, bars: list[tuple[str, float]], today: dt.date,
                      headlines: list[dict] | None) -> str | None
def coin_exit_reason(conn, pos: dict, bars: list[tuple[str, float]], today: dt.date,
                     headlines: list[dict] | None, trend_fn) -> str | None
def model_value(conn) -> float
def run(conn, today: dt.date | None = None, *, fetch=None, news_fn=None, trend_fn=None,
        sector_fn=None, signals=None, t212=None) -> DayReport
```

**Behaviour** (spec §4):

- **`create_books`:** `INSERT OR IGNORE` into `paper_books`:
  - MODEL-S: sleeve "stock", start 70k, bench "SPY";
  - MODEL-C: sleeve "crypto", start 30k, bench "BTC-USD".
- **`candidate_signals(conn, today)`:**
  - `strategy.buy_side_signals(conn, ignore_alert_state=True, onchain=False, cluster_kwargs=FINDER_CLUSTER_KWARGS, stake_kwargs=FINDER_STAKE_KWARGS)`;
  - keep signals with `(cluster.disclosed_on(conn, s) or "") >= today - MODEL_SIGNAL_DAYS`;
  - `cluster.enrich_signals` them, then return.
  - Note: `buy_side_signals` still has its `high_risk` parameter until Task 4. Pass `high_risk=False` here; Task 4 removes the argument.
- **Seams** (all default to real network functions, and tests pass stubs):
  - `fetch(symbol, days)` → bars, as in paper;
  - `news_fn(ticker, source)` → list of `{"title": str, "published": str}`. The default uses `sources.news(assets.crypto_asset(coin))` for a CRYPTO: ticker, else `sources.news(assets.stock_asset(paper.listing(ticker, source)[0]))`. It keeps items published in the last 14 days (a published date that can't be parsed is kept) and returns [] on any exception;
  - `trend_fn(conn, coin)` → `crypto.price_trend`;
  - `sector_fn(ticker, source)` → `research._yf_info(symbol).get("sector")` wrapped in try/except, returning None on failure;
  - `t212`: an object with `can_buy(ticker, source)`, or None. The default is `trading212.availability(conn)`, wrapped in try/except → None.
- **`score_today`:**
  - **Candidates:** use `signals` if given (already enriched; skip the finders, recency and enrichment), else `candidate_signals`.
  - **Stock signals:** those that aren't CryptoSignals (no `crypto_kind`), and whose ticker is not a CRYPTO: ticker.
  - **Context flags per ticker, from the candidates plus `sec_stakes`:**
    - `activist`: another candidate on the ticker is a StakeSignal with `is_activist`, or `sec_stakes` has `form_type LIKE '%13D%'` and `event_date >= today-30`;
    - `passive_big`: the same, for 13G with `percent >= 10`;
    - `politicians`: a candidate with source HOUSE or SENATE on the ticker.
  - **Scoring each stock signal:**
    1. closes = `prices.bars(listing[0])` closes, or [] when there's no listing;
    2. prescore with `headlines=None`;
    3. when its total without news is ≥ NEWS_MIN_PRESCORE, re-score with `news_fn`.
  - **Keep the best `StockScore` per ticker.**
  - **Coins:** score each of COINS:
    - closes from `prices.bars("BTC-USD")`, and so on;
    - `bullish_flow`: any candidate CryptoSignal on the coin that is bullish;
    - `caution`: a bearish CryptoSignal on the coin, confirmed by `crypto.trend_confirms_down(trend_fn(conn, coin))`. The text is its `company` + « — цена подтверждает»;
    - headlines from `news_fn`.
  - **Return** everything sorted by `total` desc.
- **`stock_exit_reason`**, first match:
  1. **Trailing stop:**
     - peak = max close in `bars` with date ≥ `pos["fill_date"]`;
     - stop = `pos["stop_pct"]`, or when None `model_score.stop_distance(closes up to fill_date, "stock")`, or 0.15;
     - `bars[-1][1] <= peak * (1 - stop)` → f"стоп: −{stop*100:.0f}% от максимума".
  2. **Insider sale:** `positions._insider_sale(conn, SimpleNamespace(ticker, opened_at=fill_date, insiders=json list))` → «продаёт инсайдер: …».
  3. **Activist cut:** `pos["source"] == "SEC13DG"` and the newest `sec_stakes` row for (ticker, person in insiders) with `event_date > fill_date` has a `percent_of_class` below the newest row on or before `fill_date` → «активист сократил долю».
  4. **Dead money:** `paper.business_days_between(fill_date, today) >= DEAD_MONEY_BDAYS` and `last_value/net_eur - 1 < DEAD_MONEY_MIN_RETURN` → «стоит на месте».
  5. **A year held:** `(today - fill_date).days >= MAX_HOLD_DAYS` → «год в позиции».
  6. **A red flag:** in `headlines` (`model_score.news_part`) → f"новости: {title}".
- **`coin_exit_reason`**, first match:
  1. the trailing stop (default stop 0.25);
  2. `coin_trend(closes)["down"]` → «тренд вниз»;
  3. a caution: `positions._crypto_caution(conn, SimpleNamespace(ticker, opened_at=fill_date), today, trend_fn)` → f"осторожно: {detail}";
  4. a red flag (coin=True).
- **`run`:**
  1. `create_books`; build `prices = paper.Prices(fetch, today)`.
  2. For each book, in `try`/`except` (log to stderr and `conn.rollback()` on failure; the other book continues):
     - `paper.fill_orders`;
     - `paper.mark_to_market`;
     - exits on open positions → `paper.place_sell` plus a `Trade("sell", …, result=last_value/cost_eur-1)`.
     - Headlines are fetched with `news_fn` for each open position.
  3. **Scoring:** `scored = score_today(conn, today, fetch=…, news_fn, trend_fn, signals, t212, prices=prices)`.
  4. **Stock buys**, for StockScores with decision BUY in score order:
     - skip a held or pending ticker silently;
     - skip «недавно продан» if the ticker closed in MODEL-S within REBUY_COOLDOWN_DAYS;
     - skip «мест нет» at MAX_STOCK_POSITIONS open + pending buys;
     - skip «сектор заполнен» when `sector_fn` gives a sector already held or pending MAX_PER_SECTOR times. Pending buys' sectors come from a per-run dict;
     - size = `model_score.position_size(model_value(conn), paper.book_value(conn, STOCK_BOOK), s.stop_pct, "stock")`;
     - size < MIN_ORDER_EUR → skip «мало»;
     - `paper.place_buy(conn, STOCK_BOOK, ticker, source, reason, today, size, min_fraction=0.5, insiders=paper.insiders_of(sig), stop_pct=s.stop_pct, score=s.total)`. The reason is `f"балл {total:.0f}: " + "; ".join(reasons[:3])`.
     - A skip is recorded through `paper._record(… status "skipped", note=…)`, the same as `place_buy`'s own skips.
     - Report a `Trade` only for a result of "pending".
  5. **Coin buys:** CoinScores with decision BUY that aren't held or pending, sized with `position_size(…, paper.book_value(conn, CRYPTO_BOOK), stop, "crypto")`, then `place_buy` on CRYPTO_BOOK with source "CRYPTO".
  6. `paper._snapshot` both books.
  7. **Return the `DayReport`:**
     - `value` = the sum of both books' values;
     - `bench` = the sum of today's `paper_equity.bench` for both books, or None if either is missing;
     - `decisions` = `{s.ticker: s.decision for s in scored}`.
- **`model_value(conn)`** = `paper.book_value` of both books (0 for a book that doesn't exist yet).

- [ ] **Step 1: Write the failing tests** in `tests/test_model.py`:
  - **Setup:**
    - use the `conn` fixture and a `Fetch` stub like tests/test_paper.py's;
    - dates fixed at `TODAY = dt.date(2026, 10, 5)` (a Monday);
    - signals are `SimpleNamespace`s with `ticker`, `source`, `company`, `buyers`, `buyer_count`, `holder_only=False`, `value_pct_of_mcap`, `position_increase_pct`, `first_buy`, `market_cap_eur`, `avg_daily_value`, `corroborated_by=[]` and `member_names`.
  - **Test cases:**
    - books are created once with 70k/30k;
    - a BUY-scoring stock places one pending order:
      - amount = min(1% × 100k / stop, 10k);
      - `stop_pct` and `score` stored;
      - the order fills at the next close on the following run, and the position carries `stop_pct`;
    - two BUY stocks are placed in score order;
    - skips: 13 BUY stocks → 12 placed and one «мест нет»; a sector cap of 3 with `sector_fn` returning "Tech" → the 4th «сектор заполнен»; a ticker closed 10 days ago → «недавно продан»;
    - a tiny model value → «мало»;
    - held → no duplicate order;
    - the trailing stop: fill at 100, peak 130, close 116 with stop 0.10 → sell «стоп: −10% от максимума»; a close of 118 → no sell;
    - an insider sale after the fill → sell (use `conftest.add_sec_sale`); a sale before the fill → no sell;
    - an activist cut (`conftest.add_stake` twice: 9% before the fill, 6% after) → sell;
    - dead money: 61 business days at +2% → sell; at +6% → no sell;
    - 365 days → sell;
    - a red-flag headline from `news_fn` → sell;
    - coins:
      - a rising BTC series with a bullish CryptoSignal → a buy on MODEL-C, sized ≤ 35% of 30k;
      - a falling series with an open BTC position → «тренд вниз»;
    - one sleeve raising (a monkeypatched `paper.fill_orders` raising for MODEL-C only) → MODEL-S still trades, and the error is printed;
    - `score_today` places no orders;
    - `DayReport.decisions` maps tickers to decisions; `value` = the two books' sum.
- [ ] **Step 2:** Run it. Expected: fails.
- [ ] **Step 3:** Implement the `db.py` columns, the `paper.py` keyword additions (existing paper tests must still pass) and `model.py`.
- [ ] **Step 4:** Run the file's tests, then the whole suite. Expected: all pass.
- [ ] **Step 5:** Commit `feat(model): the model portfolio engine on the paper books` with the trailer.

---

### Task 3: Views — day message, scored list, model summary, menu

**Files:**
- Modify:
  - `telegram_notify.py`: add `format_model_day`, `format_scored`, `format_trade`;
  - `paper_report.py`: the model summary; `format_book` generic; the monthly report keyed to the model's start;
  - `menu.py`: options 1 and 3.
- Test:
  - `tests/test_model_views.py` (new);
  - update `tests/test_paper_report.py` and `tests/test_signals_view.py` where they assert the old summary or menu text.

**Interfaces:**
- Consumes: Task 2's `model.DayReport`, `model.Trade`, `model.score_today`, `model.BOOKS`, `model.STOCK_BOOK`, `model.CRYPTO_BOOK`; Task 1's `StockScore`/`CoinScore`.
- Produces:

```python
# telegram_notify.py
def format_trade(t, *, html: bool = True) -> str
def format_model_day(report, closes: list, exits: list, *, html: bool = True,
                     today: dt.date | None = None) -> str | None   # None when nothing to say
def format_scored(scored: list, *, html: bool = False) -> str
# paper_report.py
def model_stats(conn, today: dt.date) -> dict | None   # None before the model's first run
def format_summary(conn, today: dt.date, *, monthly: bool = False, html: bool = False) -> str
def format_book(conn, code: str) -> str
def maybe_send_monthly_report(conn, today: dt.date, send=None) -> bool
```

**Details:**

**`format_model_day`:** returns None when `report` is None or has no buys and no sells, and there are no `closes` and no `exits`. Otherwise:
- header: `📊 Модельный портфель — DD.MM`, bold;
- `🟢 Купить (исполнение по закрытию следующего дня)`, one `format_trade` per buy:
  - «• TICKER — Company: €9 800 (9,8% портфеля), стоп −10% от максимума, балл 64»;
  - then up to 3 reason lines indented with «   »;
  - the label «нет на T212» when `t212 is False`;
- `🔴 Продать`, one line per sale: «• TICKER — reason (результат +8,3%)»;
- `🚪 Ваши позиции`: the existing `format_close_alert` per close;
- `🚨 Продают те, кто покупал`: `format_any_signal` per exit;
- last line: «Портфель: €101 230 (+1,2%), смесь 70/30: +0,8%», computed from `report.value` / `report.bench` against 100 000 (omit the mix part when `bench` is None).

Use a non-breaking thin grouping «9 800», matching the existing `_short_money`/`_eur` helpers where possible, or plain `{:,.0f}` with the comma replaced by a space. Escape every dynamic string.

**`format_scored`:** one line per score:
- icon 🟢 buy / 👀 watch / ⛔ block / · skip;
- ticker, total, and the parts:
  - a stock «инсайдеры 52 · поводы 5 · импульс 7 · новости 0»;
  - a coin «тренд 45 · потоки 15 · новости 0»;
- then «— untradeable/block reason» and «нет на T212».
- Skipped stocks are listed after a line «Прочие (балл ниже 45):», at most 15 of them.
- Header «СИГНАЛЫ — оценка модели (покупка от 60, наблюдение 45–59)».
- Empty → «Свежих сигналов за 14 дней нет.».

**`model_stats(conn, today)`:**
- Needs both model books in `paper_books`; otherwise None.
- value = the latest `paper_equity.value` summed for both books on their latest common date (or the start money);
- bench = the same for `bench`;
- ret = value/100 000 − 1; bench_ret likewise;
- dd = `paper.max_drawdown` of the summed daily series; bench_dd likewise;
- trades = closed positions in both books; open = open positions;
- day = the days since the MODEL-S start;
- status:
  - «идёт (день N из 182)» before day 182;
  - else «идёт (сделок N из 20)» while trades < 20;
  - else «пройдено» / «не пройдено» (ret > bench_ret and dd > bench_dd).

**`format_summary`:**
- «Модельный портфель ещё не запущен — стартует с первого ежедневного прогона.» when `model_stats` is None;
- else:
  - a bold header «Модельный портфель — день N (с DD.MM.YYYY)»;
  - lines for the total against «смесь 70/30 (S&P 500 / BTC)»: return, difference in п.п., worst drops, trades, open positions, status;
  - one line per sleeve (Акции MODEL-S against S&P 500, Крипто MODEL-C against BTC) with value, cash and return;
  - the open positions (ticker, days, result, the stop distance from the peak «до стопа 4,2%»);
  - with `monthly`, each sleeve's month (reuse `_month_return`);
  - a last line «Архив: 13 прежних книг остановлены — python paper.py R1-E1» when any non-model book exists.
- With html, the table rows are in `<pre>` and escaped, as today.

**Other paper_report changes:**
- `format_book(conn, code)` works for any code in `paper_books` (don't require `paper.BOOK_BY_CODE`). The label is the code. The rest is unchanged.
- `maybe_send_monthly_report` uses MODEL-S's `start_date` (not the MIN over all books) and returns False when the model isn't started.
- `paper.main` (the CLI): a code is valid if it's in `paper_books`. The error lists the codes found there.

**Menu:**
- `show_signals`: `model.score_today(conn)` → `format_scored`, then the /bought positions and pending close alerts as today (`positions.check_exits` + `format_close_alert` + `format_positions`). It no longer calls `strategy.select`.
- `show_paper` prints `format_summary`, and «Подробно: python paper.py MODEL-S (или MODEL-C, R1-E1 …)».
- The option labels stay «1) Сигналы», «3) Модельный портфель».

- [ ] **Step 1: Write the failing tests:**
  - `format_model_day`: None when empty; the sections and the order; HTML escaping of a company «A&B <x>»; the «нет на T212» label;
  - `format_scored` icons, the parts and the «Прочие» cut-off;
  - `model_stats`:
    - None before;
    - the combined return and dd from seeded `paper_equity` rows for both books;
    - the three status stages (day < 182; ≥182 with 5 trades; ≥182 with 20 trades, beating / not beating);
  - `format_summary` with archived books present shows the archive line;
  - `format_book` for "MODEL-S" and for an archived code;
  - the monthly report is sent once per month, not in the start month;
  - menu `show_signals` prints the scored header (monkeypatch `model.score_today`).
- [ ] **Step 2:** Run them. Expected: they fail.
- [ ] **Step 3:** Implement. Update the old tests that asserted the 13-book summary to the new model summary, and remove the assertions about removed text.
- [ ] **Step 4:** Run the whole suite. Expected: green.
- [ ] **Step 5:** Commit `feat(model): day message, scored list and model summary` with the trailer.

---

### Task 4: Switch the daily run to the model and remove the old filters

**Files:**
- Modify:
  - `bot.py`;
  - `strategy.py`;
  - `paper.py`: delete the old book trading;
  - `telegram_notify.py`: delete `format_tiered_digest` and `_rule_lines`;
  - `backtest.py` and `crypto.py` only if they import removed names;
  - `README.md` is left to Task 7.
- Delete:
  - `calibrate_strategy.py` and `tests/test_calibrate.py`;
  - `tests/test_tiered_format.py`;
  - the tier/select tests in `tests/test_strategy.py` (keep and adapt the `buy_side_signals`/`exit_signals` tests);
  - the old-book tests in `tests/test_paper.py` (keep the engine tests: fills, fees, values, drawdown, cancel, close-at-last-value; rewrite their `_sel`/`strategy.Selection` helpers to call engine functions directly).
- Test:
  - `tests/test_bot_model.py` (new);
  - adapt `tests/test_signals.py`, `tests/test_oslo_isin.py`, `tests/test_crypto.py` and `tests/test_telegram_notify.py` where they use removed names.

**Interfaces:**
- Consumes: `model.run`, `model.DayReport`, `telegram_notify.format_model_day`, `paper_report.maybe_send_monthly_report`.
- Produces:

```python
# bot.py
def collect_new_signals(conn, args) -> tuple[list, list]   # (buy_side_new, exits_new)
def _run_model(conn, args) -> "model.DayReport | None"
def _journal(conn, signals: list, report) -> None
def _send_day(conn, report, closes: list, exits: list) -> bool
# strategy.py keeps: buy_side_signals (without high_risk), exit_signals, is_buy_side, is_caution
```

**Changes:**

**`strategy.py`:**
- **Delete:**
  - tiers, `Tiered`, `Selection`, `select`;
  - every tier constant, `_ROLE_LABEL`, `_short`;
  - `_stock_tier`, `_trend_desc`, `_crypto_tier`, `_caution_tier`, `_in_high_risk_band`, `_pct_text`, `_high_risk_tier`;
  - the high-risk constants.
- **`buy_side_signals` loses** its `high_risk` parameter and the second pass.
- **Rewrite the module docstring:** this is the finder list; scoring lives in model_score.py and model.py (spec 2026-09-30).
- Keep `STRONG, CANDIDATE, CAUTION, HIGH_RISK` only if something still imports them. Otherwise keep just `CAUTION = "caution"`, which the journal uses.

**`paper.py`:**
- **Delete:**
  - `EXITS`, the `Book` rules and every `R*`/`C-*`/`H*` book definition used only for trading;
  - `stock_signals`, `_buy_reason`, `_analyst_target`, `stock_exit_reason`, `stock_step`;
  - `strong_coins`, `above_trend`, `_recent_strong`, `_rebuy_blocked`, `_caution`, `crypto_step`;
  - `run`, `create_books`;
  - the constants only they use.
- **Keep:**
  - the engine: `listing`, `fee`, `Prices`, `close_on_or_before`, `first_close_after`, `business_days_between`;
  - the order/position helpers, `position_value`, `history_days`, `mark_to_market`, `book_value`, `_record`, `place_buy`, `place_sell`, the fills, `max_drawdown`, `_snapshot`;
  - `insiders_of`;
  - the constants those use (`FEE_*`, `ORDER_MAX_BUSINESS_DAYS`, `PRICE_DAYS`, `SUCCESS_DAYS`, `MIN_STOCK_TRADES`, `_VENUE_CURRENCY`).
- **Update the module docstring:** "the virtual-book engine; the model portfolio (model.py) is its only trader; books from the 2026-09-28 design stay in the database as an archive".

**`telegram_notify.py`:** delete `format_tiered_digest` and `_rule_lines`. Keep `format_close_alert` and `_CLOSE_REASON`.

**`bot.py`:**
- **`collect_new_signals(conn, args)`:**
  - the same finder call as `run_cluster_pass` today (with run_daily's kwargs and `ignore_alert_state=False`, no `high_risk`), plus `strategy.exit_signals` unless `--no-exit-signals`;
  - returns (buy-side, exits);
  - prints one line per new signal (`format_any_signal`);
  - delete `run_cluster_pass`.
- **`_run_model(conn, args)`:** None on a filtered run (`_filtered_run`); else `_run_source("MODEL", model.run, conn)`. When that returns None and Telegram is on, send «⚠️ disclosure-bot: модельный портфель упал в этом прогоне. Логи: data/launchd.err.log».
- **`_journal(conn, signals, report)`:**
  - enrich the buy-side ones (`cluster.enrich_signals`);
  - set `sig.tier` = `"caution"` for bearish CryptoSignals (`strategy.is_caution`), else `report.decisions.get(sig.ticker)` when there is a report, else None;
  - then `_commit_signals(conn, signals)`;
  - exits are committed through `_commit_signals` too (tier None).
- **`_send_day(conn, report, closes, exits)`:**
  - `text = telegram_notify.format_model_day(report, closes, exits)`; None → return False;
  - send; on success `positions.mark_alerted(conn, closes)` and return True;
  - on failure, print to stderr and return False.
- **Delete:** `_record_cautions`, `_record_high_risk`, `_send_digest`, `_run_paper`, the `keep()` display filter, and the `tradingview.annotate_signals` call.
- **`_PAPER_SKIP_FLAGS`** is renamed `_FILTER_FLAGS`, with the same flags.
- **`main()` order after the source passes and warnings:**
  1. `buys, exits = collect_new_signals(conn, args)`
  2. `report = _run_model(conn, args)`
  3. `_journal(conn, buys + exits, report)`
  4. `closes = positions.check_exits(conn)`, printed as today
  5. `if not args.no_telegram: _send_day(...)`, then the monthly report through `_run_source("PAPER_REPORT", paper_report.maybe_send_monthly_report, conn, dt.date.today())` when not a filtered run
  6. `last_successful_run`
  7. the poll-finished line: «N new purchase(s), N new signal(s), N model buy(s), N model sale(s), N close alert(s)»
- **`--help` texts:** the flags that mentioned tiers/digest/filters get neutral wording.

**`menu.py`:** no `strategy.select` left (Task 3 already did this).

**Also:** `grep -rn "strategy.select\|Selection\|Tiered\|format_tiered_digest\|high_risk\|paper.run\|calibrate_strategy" --include='*.py' .` must return nothing outside `docs/`.

- [ ] **Step 1: Write the failing tests** in `tests/test_bot_model.py`:
  - `_journal` sets the tier from `report.decisions`, `caution` for a bearish CryptoSignal, None without a report (stub `cluster.enrich_signals` to identity and `_commit_signals` to record);
  - `_send_day` sends nothing when `format_model_day` returns None; marks closes alerted only after a successful send;
  - `_run_model` returns None on a filtered run without calling `model.run`;
  - `main()`'s order, with the passes stubbed via monkeypatch (optional, if feasible like the existing bot tests).
- [ ] **Step 2:** Run them. Expected: they fail.
- [ ] **Step 3:** Implement the removals and the wiring. Fix or delete the tests that used removed names, as listed above.
- [ ] **Step 4:** Run the whole suite and the grep. Expected: green, no matches.
- [ ] **Step 5:** Commit `feat: the daily run trades the model; tiers, filters and old books removed` with the trailer.

---

### Task 5: Your positions (/bought) get the model's exits

**Files:**
- Modify:
  - `positions.py`;
  - `db.py`: `_ADDED_COLUMNS` gets `("positions", "stop_pct", "REAL")`;
  - `telegram_bot.py`: only the /bought reply text;
  - `telegram_notify.py`: `_CLOSE_REASON` gets the new triggers.
- Test: `tests/test_positions.py` (update and add).

**Interfaces:**
- Consumes: `model_score.stop_distance`, `model_score.coin_trend`, `model_score.news_part`, `model.MAX_HOLD_DAYS`, `model.DEAD_MONEY_BDAYS`, `model.DEAD_MONEY_MIN_RETURN`, `paper.business_days_between`, `paper._closes`.
- Produces:

```python
def open_position(conn, ticker, entry_price, today=None, source=None, *, closes_fn=None) -> Position  # stores stop_pct
def check_exits(conn, today=None, price_fn=None, trend_fn=None, *, closes_fn=None, news_fn=None) -> list[CloseAlert]
# CloseAlert.trigger values: insider_sell / caution / trailing_stop / dead_money / time / trend_down / news
```

**Details:**
- **`Position`** gets `stop_pct: float | None`, which `_COLS` and `_row` include.
- **`closes_fn(ticker, source)`** → a list of (date, close), oldest first. The default uses `paper._closes(yahoo_symbol(ticker, source), paper.PRICE_DAYS)`, and returns [] when there's no symbol or on an exception. `paper` is imported inside the function, to avoid an import cycle.
- **`open_position`** computes `stop_pct = model_score.stop_distance([c for _d, c in closes], "crypto" if crypto.is_crypto(ticker) else "stock")` and stores it (it may be None).
- **`check_exits`**, per open position not yet alerted, first match:
  1. **`insider_sell`**, as today.
  2. **`caution`**: coins, as today.
  3. **`trailing_stop`:**
     - peak = the max close since `opened_at`, including `entry_price`;
     - stop = `pos.stop_pct`, else computed from the closes before `opened_at`, else 0.15 for stocks and 0.25 for coins;
     - `price <= peak * (1 - stop)` → detail f"−{stop*100:.0f}% от максимума {peak:,.2f}".
  4. **`trend_down`**: coins, `model_score.coin_trend(closes)["down"]` → «ниже 100-дн. средней, 20 дн. в минусе».
  5. **`dead_money`**: stocks, `business_days_between(opened_at, today) >= 60` and `price/entry_price - 1 < 0.05` → «60 торговых дней без роста».
  6. **`time`:** `held >= 365` → f"{held} дн. в позиции".
  7. **`news`:** the red flag from `news_part(news_fn(ticker, source), coin=...)` → f"новости: {title}". The default `news_fn` is `model`'s default news function (import inside the function).
- **Delete** `EXIT_MAX_DAYS`, `EXIT_STOP_LOSS_PCT` and `EXIT_STOP_LOSS_PCT_CRYPTO`, updating any reference.
- **`_CLOSE_REASON`** entries: `trailing_stop` «стоп от максимума», `dead_money` «стоит на месте», `time` «год в позиции», `trend_down` «тренд вниз», `news` «плохие новости». Keep `insider_sell` and `caution`.
- **The /bought reply** (telegram_bot) states the stop:
  - «Записал NVDA по 180,00; стоп −10% от максимума; слежу за продажами: …»;
  - when `stop_pct` is None, «стоп — по умолчанию».
  - The old «(стоп-лосс не отслеживается…)» note stays for no market price.

- [ ] **Step 1: Write the failing tests** (stub `closes_fn`/`news_fn`/`price_fn`):
  - `stop_pct` is stored at open;
  - the trailing stop fires from the peak, not the entry; no fire above it;
  - dead money at 61 business days and +2%; no fire at +6%;
  - 365 days;
  - a news red flag;
  - a coin's trend down;
  - insider and caution precedence unchanged;
  - a position without `stop_pct` computes it from the closes before `opened_at`.
- [ ] **Step 2:** Run them. Expected: they fail.
- [ ] **Step 3:** Implement; update the existing positions and telegram_bot tests that asserted the 90-day/15% rules.
- [ ] **Step 4:** Run the whole suite. Expected: green.
- [ ] **Step 5:** Commit `feat(positions): /bought positions use the model's exits` with the trailer.

---

### Task 6: The analyst — analyst.py, queue, prompts, launch script

**Files:**
- Create:
  - `analyst.py`;
  - `analyst_method.txt`;
  - `claude_ask_prompt.txt`.
- Modify:
  - `claude_analysis_prompt.txt` (rewrite);
  - `run_claude_analysis.sh`;
  - `db.py`: the queue columns and `enqueue_question`.
- Test:
  - `tests/test_analyst.py` (new);
  - update `tests/test_claude_prompt.py`.

**Interfaces:**
- Consumes: `model.score_today`, `model.STOCK_BOOK`, `model.CRYPTO_BOOK`, `paper.open_positions`, `positions.open_positions`, `paper_report.format_summary`, `research.build`, `research.format_brief`, `research.NotATicker`, `sources._google_news`, `db.pending_analysis`.
- Produces:

```python
# db.py
_ADDED_COLUMNS += [("claude_analysis_queue", "kind", "TEXT DEFAULT 'ticker'"),
                   ("claude_analysis_queue", "question", "TEXT")]
def enqueue_question(conn, text: str) -> int          # returns the new row id; ticker 'ВОПРОС', kind 'question'
def queued_question(conn, queue_id: int) -> str | None
# analyst.py
CLAUDE_BIN = Path.home() / ".local" / "bin" / "claude"
TV_TOOLS = ("tv_health_check", "tv_launch", "chart_get_state", "chart_set_symbol",
            "chart_set_timeframe", "quote_get", "data_get_ohlcv", "symbol_info", "symbol_search")
ALLOWED_TOOLS = ("Bash",) + tuple(f"mcp__tradingview__{t}" for t in TV_TOOLS)
EXTRA_PATH = ("/usr/local/bin", "/opt/homebrew/bin")
TICKER_RE = re.compile(r"^\$?[A-Z0-9][A-Z0-9.\-]{0,14}$")
def claude_command(prompt: str) -> list[str]
def claude_env(base: dict | None = None) -> dict
def valid_ticker(text: str) -> str | None
def context(conn, ticker: str, *, scored=None) -> str
def portfolio(conn, *, scored=None) -> str
def news(query: str) -> str
def ask(question: str, *, run=None) -> int        # terminal; prints the answer
def process_queue(conn, *, run=None) -> int       # 0 when nothing queued (no Claude run)
def main(argv=None) -> int                        # subcommands: question ID | context TICKER | context --queue ID |
                                                  # portfolio | news QUERY | ask TEXT | process-queue
```

**Details:**

- **`db.py`:**
  - `enqueue_question` inserts `(ticker='ВОПРОС', kind='question', question=text)` and returns `cursor.lastrowid`.
  - `enqueue_analysis` dedupes only against pending rows of kind 'ticker' (`COALESCE(kind,'ticker')='ticker'`).
  - `queued_question` returns the text for a question row, else None.
- **`claude_command(prompt)`:** `[str(CLAUDE_BIN), "-p", prompt, "--permission-mode", "acceptEdits", "--allowedTools", *ALLOWED_TOOLS]`. It's one flag followed by the tool names as separate argv items.
- **`claude_env`:** a copy of `os.environ` (or `base`) with each missing EXTRA_PATH entry prepended to PATH.
- **`valid_ticker`:** strip, upper, `TICKER_RE` → the ticker without a leading "$", else None.
- **`context(conn, ticker, *, scored=None)`:**
  - `scored = scored or model.score_today(conn)`.
  - A match is a score whose `ticker` equals the ticker, or whose coin equals it.
  - **With a match**, print:
    - «МОДЕЛЬ: балл N — решение (покупка/наблюдение/блок/пропуск)»;
    - the parts and reasons, the stop, and T212.
  - **Without one**, «МОДЕЛЬ: свежего сигнала за 14 дней нет».
  - Then:
    - the model's open position in the ticker (MODEL-S/MODEL-C via `paper.open_positions`): fill date, result, stop;
    - the user's /bought position;
    - «ДОСЬЕ:» + `research.format_brief(research.build(conn, ticker))`. It catches `research.NotATicker` («не похоже на тикер») and any other exception («досье недоступно: Type»).
- **`portfolio(conn, *, scored=None)`:**
  - `paper_report.format_summary(conn, today)`;
  - «НАБЛЮДЕНИЕ:» with up to 10 WATCH scores (ticker, total, top reason);
  - «ПОКУПКИ СЕГОДНЯ:» from `paper_orders` created today with side buy on the model books.
- **`news(query)`:** up to 10 `sources._google_news(query)` items as «DD.MM · publisher · title». On failure, «новости недоступны».
- **`process_queue(conn, *, run=subprocess.run)`:**
  - if `db.pending_analysis(conn)` is empty, return 0 without running;
  - else `run(claude_command(PROMPT_TEXT), cwd=BASE_DIR, env=claude_env(), check=False)`, where `PROMPT_TEXT` = `claude_analysis_prompt.txt` read at call time;
  - return its returncode.
- **`ask(question, *, run=subprocess.run)`:**
  - prompt = `claude_ask_prompt.txt` text + "\n\nВОПРОС:\n" + question;
  - `run(claude_command(prompt), cwd=BASE_DIR, env=claude_env(), capture_output=True, text=True)`;
  - print stdout with `<b>`/`</b>` removed;
  - return the returncode.
  - A missing `CLAUDE_BIN` → print «claude не найден: …» and return 127.
- **CLI:**
  - `python analyst.py context TICKER` validates with `valid_ticker` (exit 2 with a message when invalid).
  - `context --queue ID` takes the ticker from `dict(db.pending_analysis(conn))[ID]`, so Claude never types user text into a shell command.
  - `question ID` prints `queued_question`.
  - Every subcommand opens `data/disclosures.db` through `db.connect(BASE_DIR/"data"/"disclosures.db")`.

**`analyst_method.txt`:** spec §7's Method, written as numbered instructions for Claude.
- It names each allowed TradingView tool, the restore step, the ban on `data_get_study_values` and on changing indicators/drawings/alerts/Pine/layouts, and the quote_get-without-symbol rule.
- Each TICKER argument goes in single quotes and must look like a ticker: `python analyst.py context 'NVDA'`.
- **Answer format:** Russian, bullets, an HTML-bold header, the model's decision and score, levels only from TradingView or the bot, one «🎯 Итоговый вердикт:» line, no disclaimers, only `<b>` tags, and no bare < or >.

**`claude_analysis_prompt.txt`** (Telegram queue):
1. `cd ~/Desktop/disclosure-bot && source .venv/bin/activate && set -a && source .env && set +a`
2. `python3 -c "import db; conn=db.connect('data/disclosures.db'); print(db.pending_analysis(conn))"`. If the list is empty, stop.
3. `cat analyst_method.txt` and follow it.
4. For each row (max 10):
   - ticker 'ВОПРОС' → `python analyst.py question <ID>`, then answer it;
   - else → `python analyst.py context --queue <ID>`, then analyse that asset (with TradingView).
5. Write the message to /tmp/claude_analysis_msg.txt and send with the existing `python3 -c "import telegram_notify…send_text…"` block.
6. Mark processed only after `sent: True`, using the existing `db.mark_analysis_processed(conn, <ID>)` block.

Keep the existing scope-discipline paragraph. Only `<ID>` may appear as a placeholder inside `python3 -c "…"` blocks (tests/test_claude_prompt.py).

**`claude_ask_prompt.txt`** (terminal):
- `cd ~/Desktop/disclosure-bot && source .venv/bin/activate && set -a && source .env && set +a`;
- read `analyst_method.txt`;
- answer the question at the end of this prompt;
- print only the answer, with no Telegram send and no file edits.

**`run_claude_analysis.sh`:** keep the header comment and update it. The body:

```bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"
exec ./.venv/bin/python analyst.py process-queue
```

**Tests:**
- `claude_command` has the flags and every `ALLOWED_TOOLS` item;
- `claude_env` adds the paths once;
- `valid_ticker` accepts "NVDA", "$NVDA", "EQNR.OL" and "VOLV-B.ST", and rejects "rm -rf", "NV DA", "a;b" and "";
- `process_queue` with an empty queue → the run isn't called; with a row → called once with `claude_command(...)`, and its argv contains the prompt text;
- `ask` strips `<b>` and returns the returncode; a missing binary → 127;
- `enqueue_question` returns an id, the row has kind 'question', and `pending_analysis` shows (id, 'ВОПРОС');
- `enqueue_analysis` still dedupes tickers but not questions;
- `context` with a stub `scored` containing a StockScore for NVDA prints «МОДЕЛЬ: балл»; without one, «свежего сигнала»;
- `research.build` is monkeypatched to raise `NotATicker` → «не похоже на тикер»;
- CLI `context 'rm -rf'` → exit 2;
- the prompt files:
  - `analyst_method.txt` mentions every `TV_TOOLS` name, "data_get_study_values" (as forbidden) and "chart_get_state" plus a restore instruction;
  - both prompts reference `analyst_method.txt`;
  - the tests/test_claude_prompt.py placeholder rule still holds;
  - `claude_analysis_prompt.txt` contains `analyst.py question <ID>` and `analyst.py context --queue <ID>`.

- [ ] **Step 1:** Write the failing tests.
- [ ] **Step 2:** Run them. Expected: they fail.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the whole suite. Expected: green.
- [ ] **Step 5:** Commit `feat(analyst): Claude analyst with TradingView MCP for Telegram and terminal` with the trailer.

---

### Task 7: Telegram routing, menu option 4, docs

**Files:**
- Modify:
  - `telegram_bot.py`;
  - `menu.py`;
  - `README.md`;
  - `run_daily.sh`, comment only if it mentions tiers/digest.
- Test: `tests/test_telegram_bot.py` (update and add); `tests/test_menu_ask.py` (new, small).

**Interfaces:**
- Consumes: `db.enqueue_question`, `analyst.ask`, `paper_report.format_summary`.
- Produces: `telegram_bot.RUN_ANALYSIS_TIMEOUT = 420`, `telegram_bot._run_analysis(label: str) -> bool` (a shared synchronous runner), and `menu.ask_analyst()`.

**Details:**

**`telegram_bot._handle_message` routing:**
1. The positions commands, as today.
2. `/backtest`, as today.
3. `/portfolio` → `send_text(paper_report.format_summary(conn, today, html=True))`.
4. `/ask TEXT` → a question (empty text → the usage line «/ask ваш вопрос»).
5. `/start`, `/help` or any other `/command` → `HELP_TEXT`.
6. A single token (no whitespace) that `assets.resolve` resolves and that passes the price check → the ticker analysis, as today.
7. Anything else (several words, or one unresolved word) → a question.
   - Replace the «Не похоже на тикер» reply with this.
   - The «Не нашёл такой тикер» reply for a resolved asset with no price stays.

**A question:**
- `qid = db.enqueue_question(conn, text[:2000])`;
- `send_text("Думаю над вопросом… (1–5 мин)")`;
- `_run_analysis(f"question {qid}")`;
- on failure, `send_text("Не успел ответить — вопрос в очереди, ответ придёт позже.")`.

**`_run_analysis(label)`:**
- the existing `subprocess.run([RUN_ANALYSIS_SCRIPT], …, timeout=RUN_ANALYSIS_TIMEOUT)` logic, returning True on returncode 0;
- the ticker path uses it and keeps its research fallback.

**`HELP_TEXT`** gets:
- «Любой вопрос текстом (или /ask …) — ответит аналитик с графиком TradingView и данными бота.»
- «/portfolio — модельный портфель.»

The module docstring mentions questions.

**`menu.py`:**
- option «4) Спросить аналитика»;
- `ask_analyst()` reads a question with `input`, prints «Спрашиваю… (1–5 мин, нужен открытый TradingView)», and calls `analyst.ask(q)` (an empty question returns);
- the invalid-choice message lists 0–4.

**`README.md`:**
- replace the sections describing tiers (Сильный/Кандидат/Осторожно/Высокий риск), the 3-day window, the size floors, T212 filtering and the 13 paper books with:
  - «Модельный портфель»: scoring table, decisions, sizing/stops, exits, books, daily message, menu;
  - «Аналитик (TradingView)»: how to ask in Telegram/terminal, TradingView Desktop must be running (the analyst starts it with the debug port if it's closed), the allowed tools, and that it restores your chart.
- Remove `calibrate_strategy.py` mentions.
- Keep everything else.

**Tests:**
- routing: a single resolvable token → ticker (enqueue_analysis called); "что думаешь про NVDA?" → `enqueue_question` plus a run; "/ask" alone → usage; "/portfolio" → summary sent; an unresolved single word → question;
- a question whose run fails → the fallback text;
- `RUN_ANALYSIS_TIMEOUT == 420`;
- menu `ask_analyst` calls `analyst.ask` with the input (monkeypatch `input` and `analyst.ask`).

- [ ] **Step 1:** Write the failing tests.
- [ ] **Step 2:** Run them. Expected: they fail.
- [ ] **Step 3:** Implement, and update the README.
- [ ] **Step 4:** Run the whole suite. Expected: green.
- [ ] **Step 5:** Commit `feat(telegram): questions to the analyst, /portfolio, menu option 4; docs` with the trailer.
