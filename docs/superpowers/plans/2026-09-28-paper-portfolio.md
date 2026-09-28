# Paper Portfolio Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eleven virtual books trade the bot's own signals on fixed rules, every day, so each candidate strategy builds a paper record from day one. Menu, CLI and a monthly Telegram report show the results against the S&P 500 and Bitcoin.

**Architecture:**
- **`paper.py`, the engine:**
  - books, prices, orders and fills;
  - the stock and crypto rules;
  - the daily `run(conn, selection)`.
- **`paper_report.py`, the views:** statistics, the success line, the summary and per-book text, and the monthly report.
- **Integration:**
  - `bot.main` runs the engine after the digest, through `_run_paper`;
  - `menu.py` gets option 3;
  - four new tables in `db.py`.
- **Prices:** everything is priced from adjusted daily closes through one network seam (`paper._closes`). Tests replace it by passing `fetch=` to `Prices`/`run`.

**Tech Stack:** Python 3.12, SQLite, pytest (offline: `tests/conftest.py` blocks all network access).

**Spec:** `docs/superpowers/specs/2026-09-28-paper-portfolio-design.md`

## Global Constraints

- **Language and tests:**
  - user-facing text is Russian; code, comments and log lines are English;
  - the suite stays offline, and run it with `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider` from the worktree root;
  - commits use conventional messages ending with the line `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **Nothing in this plan places a real order.** It never calls Trading 212 or any broker.
- **Books:**
  - 8 stock books `R1-E1 … R2-E4`, the shadow `R1-E1-AN`, and the crypto books `C-A` and `C-B`;
  - stock books start with €80,000 and crypto books with €20,000;
  - the start date is the first run.
- **Buy rules:**
  - R1 is every non-`CRYPTO:` signal in `selection.strong`;
  - R2 is R1 plus every non-`CRYPTO:` signal in `selection.candidates` with a score of 70 or more.
- **Stock sizing:**
  - 10% of the book's value per buy, with at most 10 positions (open plus pending buys);
  - a partial slice is allowed with at least half a slice of cash; otherwise the buy is skipped as «нет денег»;
  - a full book skips as «мест нет»;
  - no repeat of a ticker that is held or pending.
- **Exits:** each is checked on daily closes, and the first that holds sells.
  - E1: an insider from the signal sells after the buy, 90 days, or −15%.
  - E2: 182 days.
  - E3: 91 days or −15%.
  - E4: E1, or +25%.
  - The shadow `R1-E1-AN` is E1, plus a sale when the close reaches the analyst target recorded at the buy.
- **Crypto:**
  - coins are BTC and ETH, each with a target share of book value ÷ 2;
  - **C-A** buys on a Сильный crypto signal, and sells on a price-confirmed caution (journaled within 7 days and on or after the buy), at 90 days, or at −25%;
  - **C-B** holds while the close is above the 200-day average, or for 30 days after a Сильный signal; a price-confirmed caution sells, and blocks a re-buy for 7 days.
- **Fills:**
  - a decision on day D fills at the first daily close after D;
  - an order with no price for more than 5 business days is cancelled as «не исполнено: нет цены».
- **Costs, per side:** 0.25% for a stock in another currency, 0.10% for a stock in EUR, 0.50% for a coin.
- **Values:**
  - a position is worth net × (latest adjusted close ÷ adjusted close on the fill day) × (FX at the fill ÷ FX now), with both closes from one fresh series;
  - a position with no price keeps its last value.
- **Benchmarks:**
  - SPY for stock books and BTC-USD for crypto books, both in EUR and indexed from the start date;
  - the worst drop is the largest peak-to-later-low fall in the daily values.
- **Success line:**
  - before day 182: «идёт»;
  - after: «пройдено» when the return beats the benchmark's, the worst drop is smaller than the benchmark's, and a stock book has at least 20 completed trades; otherwise «не пройдено»;
  - the shadow always shows «тень».
- **Telegram:**
  - one report on the first run of each month, never in the month the books started;
  - no per-trade messages.

## Decisions this plan makes where the spec left room

- **Unpriceable tickers.** A ticker with no quote at all (a BaFin or FI ISIN) is recorded straight away as a skipped order, «нет котировки». Waiting 5 days would change nothing, because such a ticker can never be priced.
- **Failure warning.** When the paper pass fails, `bot._run_paper` sends its own «бумажный портфель упал» message. The failed-source warning has already been sent at that point in the run, so it can't be reused.
- **FX at the fill** is the rate at the run that fills the order. `fx` keeps no history, and the difference is a day at most.
- **The benchmark index** uses a fresh adjusted series each day. The start close is `close_on_or_before(series, start_date)`, and only the FX at the start is stored, so later dividend adjustments can't skew the comparison.
- **Views live in `paper_report.py`.** `python paper.py [BOOK]` and the menu call into it.

## File Structure

| File | Responsibility |
|---|---|
| `paper.py` (new) | Books, prices, orders and fills, stock rules, crypto rules, `run()`, the CLI entry point |
| `paper_report.py` (new) | `stats`, the success line, `format_summary`, `format_book`, `maybe_send_monthly_report` |
| `db.py` | The tables `paper_books`, `paper_orders`, `paper_positions`, `paper_equity` |
| `bot.py` | `_run_paper(conn, selection, args)`, called in `main()` after the digest |
| `menu.py` | Option «3) Бумажный портфель» (`show_paper`) |
| `README.md` | A «Бумажный портфель» section |
| `tests/test_paper.py` (new) | Engine tests |
| `tests/test_paper_report.py` (new) | View tests |

---

### Task 1: Foundation: books, listing, fees, prices, date helpers

**Files:**
- Create: `paper.py`
- Modify: `db.py` (SCHEMA: add the four tables at the end of the SCHEMA string, before its closing `"""`)
- Test: `tests/test_paper.py`

**Interfaces:**
- Produces:
  - **Constants:** `STOCK_START_EUR`, `CRYPTO_START_EUR`, `SLICE`, `MAX_POSITIONS`, `R2_MIN_SCORE`, `FEE_FOREIGN`, `FEE_EUR`, `FEE_COIN`, `ORDER_MAX_BUSINESS_DAYS`, `PRICE_DAYS`, `CRYPTO_COINS`, `CRYPTO_HOLD_DAYS`, `CRYPTO_STOP`, `TREND_DAYS`, `SIGNAL_HOLD_DAYS`, `REBUY_BLOCK_DAYS`, `SUCCESS_DAYS`, `MIN_STOCK_TRADES`, `STOCK_BENCHMARK`, `CRYPTO_BENCHMARK`, `EXITS`.
  - **Books:** `Book(code, sleeve, buy=None, exit=None, analyst=False, rule=None)`, frozen, with a `.label` property; `BOOKS: tuple[Book, ...]`; `BOOK_BY_CODE: dict[str, Book]`.
  - **Functions:**
    - `listing(ticker, source) -> tuple[str, str] | None`, returning (Yahoo symbol, currency);
    - `fee(ticker, currency) -> float`;
    - `_closes(symbol, days) -> list[tuple[str, float]]`, the network seam;
    - `Prices(fetch=None)` with `.bars(symbol, days=PRICE_DAYS)`;
    - `close_on_or_before(bars, day) -> float | None`;
    - `first_close_after(bars, day) -> tuple[str, float] | None`;
    - `business_days_between(start: str, today: dt.date) -> int`;
    - `create_books(conn, today) -> None`.

- [ ] **Step 1: Write the failing tests.** Create `tests/test_paper.py`:

```python
"""paper.py: the virtual books. Offline -- prices come from a stub fetch, dates are
fixed so weekday arithmetic is deterministic."""
from __future__ import annotations

import datetime as dt
import json
import types

import pytest

import db
import paper
import strategy
from conftest import add_sec_sale

TODAY = dt.date(2026, 10, 5)          # a Monday


def _days(n: int) -> str:
    return (TODAY - dt.timedelta(days=n)).isoformat()


def _bars(closes, end: dt.date = TODAY):
    """Consecutive daily closes ending on `end`, oldest first."""
    n = len(closes)
    return [((end - dt.timedelta(days=n - 1 - i)).isoformat(), float(c)) for i, c in enumerate(closes)]


class Fetch:
    def __init__(self, series: dict):
        self.series, self.calls = series, []

    def __call__(self, symbol, days=None):
        self.calls.append(symbol)
        return self.series.get(symbol, [])


def _sig(ticker="AAA", source="SEC", tier="strong", score=80.0, members=("Jane Doe",)):
    return types.SimpleNamespace(ticker=ticker, source=source, company=f"{ticker} Corp", tier=tier,
                                 score=score, member_names=list(members))


def _sel(strong=(), candidates=()):
    return strategy.Selection(strong=[strategy.Tiered(s, s.tier) for s in strong],
                              candidates=[strategy.Tiered(s, s.tier) for s in candidates],
                              t212_checked=True)


# ------------------------------------------------------------------ foundation
def test_books_are_created_once_with_their_money(conn):
    paper.create_books(conn, TODAY)
    paper.create_books(conn, TODAY + dt.timedelta(days=1))
    rows = conn.execute("SELECT code, sleeve, start_date, start_eur, cash_eur, bench_symbol "
                        "FROM paper_books").fetchall()
    assert len(rows) == 11
    assert ("R1-E1", "stock", TODAY.isoformat(), 80_000.0, 80_000.0, "SPY") in rows
    assert ("R1-E1-AN", "stock", TODAY.isoformat(), 80_000.0, 80_000.0, "SPY") in rows
    assert ("C-B", "crypto", TODAY.isoformat(), 20_000.0, 20_000.0, "BTC-USD") in rows


def test_book_labels():
    assert paper.BOOK_BY_CODE["R2-E3"].label == "R2·E3"
    assert paper.BOOK_BY_CODE["R1-E1-AN"].label == "R1·E1+аналитики"
    assert paper.BOOK_BY_CODE["C-A"].label == "C-A"


@pytest.mark.parametrize("ticker,source,expected", [
    ("AAPL", "SEC", ("AAPL", "USD")),
    ("BRK.B", "HOUSE", ("BRK-B", "USD")),
    ("EQNR", "NORWAY", ("EQNR.OL", "NOK")),
    ("VOLV-B", "SWEDEN", ("VOLV-B.ST", "SEK")),
    ("CRYPTO:BTC", "CRYPTO", ("BTC-USD", "USD")),
    ("DE0007164600", "BAFIN", None),
])
def test_listing(ticker, source, expected):
    assert paper.listing(ticker, source) == expected


def test_fees():
    assert paper.fee("AAPL", "USD") == 0.0025
    assert paper.fee("SAP", "EUR") == 0.0010
    assert paper.fee("CRYPTO:BTC", "USD") == 0.0050


def test_prices_are_fetched_once_and_a_failure_is_empty():
    calls = []

    def fetch(symbol, days):
        calls.append(symbol)
        if symbol == "BAD":
            raise RuntimeError("down")
        return [("2026-10-01", 1.0)]
    p = paper.Prices(fetch)
    assert p.bars("AAA") == p.bars("AAA") == [("2026-10-01", 1.0)]
    assert p.bars("BAD") == [] and calls == ["AAA", "BAD"]


def test_series_helpers():
    bars = [("2026-10-01", 10.0), ("2026-10-02", 11.0), ("2026-10-05", 12.0)]
    assert paper.close_on_or_before(bars, "2026-10-03") == 11.0
    assert paper.close_on_or_before(bars, "2026-09-30") is None
    assert paper.first_close_after(bars, "2026-10-02") == ("2026-10-05", 12.0)
    assert paper.first_close_after(bars, "2026-10-05") is None


def test_business_days_between():
    assert paper.business_days_between("2026-10-02", dt.date(2026, 10, 9)) == 5
    assert paper.business_days_between("2026-10-05", dt.date(2026, 10, 5)) == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_paper.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'paper'`.

- [ ] **Step 3: Add the tables.** In `db.py`, append inside the SCHEMA string, after the last table and before the closing `"""`:

```sql
-- The paper portfolio (paper.py): virtual books that trade the bot's own signals
-- on fixed rules. Nothing here is a real position -- see positions for those.
CREATE TABLE IF NOT EXISTS paper_books (
    code            TEXT PRIMARY KEY,   -- R1-E1 … R2-E4, R1-E1-AN, C-A, C-B
    sleeve          TEXT NOT NULL,      -- stock | crypto
    start_date      TEXT NOT NULL,
    start_eur       REAL NOT NULL,
    cash_eur        REAL NOT NULL,
    bench_symbol    TEXT NOT NULL,      -- SPY | BTC-USD
    bench_start_fx  REAL                -- USD per EUR when the benchmark was first priced
);

CREATE TABLE IF NOT EXISTS paper_orders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    book         TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    source       TEXT,
    side         TEXT NOT NULL,         -- buy | sell
    amount_eur   REAL,                  -- buy: the money set aside for it
    position_id  INTEGER,               -- sell: the position it closes
    reason       TEXT NOT NULL,
    created      TEXT NOT NULL,         -- ISO date the decision was made
    status       TEXT NOT NULL,         -- pending | filled | cancelled | skipped
    note         TEXT,                  -- why it was cancelled or skipped
    insiders     TEXT,                  -- JSON list, buy only
    target       REAL                   -- analyst target at the order, shadow book only
);

CREATE TABLE IF NOT EXISTS paper_positions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    book          TEXT NOT NULL,
    ticker        TEXT NOT NULL,
    source        TEXT,
    symbol        TEXT NOT NULL,        -- the Yahoo symbol it is priced on
    currency      TEXT NOT NULL,
    fill_date     TEXT NOT NULL,
    cost_eur      REAL NOT NULL,        -- money paid, before the buy fee
    net_eur       REAL NOT NULL,        -- money invested, after the buy fee
    entry_close   REAL NOT NULL,
    entry_fx      REAL NOT NULL,        -- units of `currency` per EUR at the fill
    insiders      TEXT,
    target        REAL,
    reason        TEXT,
    last_value    REAL,                 -- EUR at the last priced close
    closed_date   TEXT,
    close_reason  TEXT,
    proceeds_eur  REAL                  -- after the sell fee
);

CREATE TABLE IF NOT EXISTS paper_equity (
    book   TEXT NOT NULL,
    date   TEXT NOT NULL,
    value  REAL NOT NULL,
    cash   REAL NOT NULL,
    bench  REAL,
    PRIMARY KEY (book, date)
);
```

- [ ] **Step 4: Create `paper.py`**

```python
"""The paper portfolio: eleven virtual books that trade the bot's own signals on
fixed rules, so a strategy can prove itself before any real money is involved.
Spec: docs/superpowers/specs/2026-09-28-paper-portfolio-design.md. Nothing here
ever places a real order.

A decision made on day D becomes an order filled at the first daily close after D,
so a book never trades at a price the bot couldn't have acted on. Values are
measured on adjusted closes (dividends and splits included), in EUR.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import types
from dataclasses import dataclass

import assets
import crypto
import db
import fx
import positions
import sources

STOCK_START_EUR = 80_000.0
CRYPTO_START_EUR = 20_000.0
SLICE = 0.10                    # of the book's value, per stock buy
MAX_POSITIONS = 10              # open positions plus pending buys, per stock book
R2_MIN_SCORE = 70.0
FEE_FOREIGN = 0.0025            # Trading 212's 0.15% currency fee plus ~0.10% spread
FEE_EUR = 0.0010
FEE_COIN = 0.0050
ORDER_MAX_BUSINESS_DAYS = 5
PRICE_DAYS = 420                # room for a 200-day average and a 182-day hold
CRYPTO_COINS = ("BTC", "ETH")
CRYPTO_HOLD_DAYS = 90
CRYPTO_STOP = -0.25
TREND_DAYS = 200
SIGNAL_HOLD_DAYS = 30
REBUY_BLOCK_DAYS = 7
SUCCESS_DAYS = 182
MIN_STOCK_TRADES = 20
STOCK_BENCHMARK = "SPY"
CRYPTO_BENCHMARK = "BTC-USD"
_VENUE_CURRENCY = {".OL": "NOK", ".ST": "SEK", ".DE": "EUR"}

# exit -> (days held, stop, take-profit, an insider from the signal selling counts)
EXITS = {
    "E1": (90, -0.15, None, True),
    "E2": (182, None, None, False),
    "E3": (91, -0.15, None, False),
    "E4": (90, -0.15, 0.25, True),
}


@dataclass(frozen=True)
class Book:
    code: str                   # R1-E1 … R2-E4, R1-E1-AN, C-A, C-B
    sleeve: str                 # stock | crypto
    buy: str | None = None      # R1 | R2
    exit: str | None = None     # E1 … E4
    analyst: bool = False       # the analyst-target shadow
    rule: str | None = None     # crypto: A (signals) | B (trend plus signals)

    @property
    def label(self) -> str:
        if self.sleeve == "crypto":
            return self.code
        return f"{self.buy}·{self.exit}" + ("+аналитики" if self.analyst else "")


BOOKS = tuple(
    [Book(f"{r}-{e}", "stock", r, e) for r in ("R1", "R2") for e in ("E1", "E2", "E3", "E4")]
    + [Book("R1-E1-AN", "stock", "R1", "E1", analyst=True),
       Book("C-A", "crypto", rule="A"),
       Book("C-B", "crypto", rule="B")])
BOOK_BY_CODE = {b.code: b for b in BOOKS}


# ---------------------------------------------------------------- listing
def listing(ticker: str, source: str | None) -> tuple[str, str] | None:
    """(Yahoo symbol, currency) a book prices `ticker` on, the same listing a
    /bought position would use -- or None for a ticker with no reliable quote (an
    ISIN from BaFin or Finansinspektionen)."""
    symbol = positions.yahoo_symbol(ticker, source)
    if not symbol:
        return None
    if crypto.is_crypto(ticker):
        return symbol, "USD"
    for suffix, currency in _VENUE_CURRENCY.items():
        if symbol.endswith(suffix):
            return symbol, currency
    return symbol, "USD"


def fee(ticker: str, currency: str) -> float:
    if crypto.is_crypto(ticker):
        return FEE_COIN
    return FEE_EUR if currency == "EUR" else FEE_FOREIGN


# ----------------------------------------------------------------- prices
def _closes(symbol: str, days: int) -> list[tuple[str, float]]:
    """Adjusted daily closes for a Yahoo symbol, oldest first; [] when every source
    fails. The one network seam -- tests hand their own fetch to Prices/run."""
    asset = (assets.crypto_asset(symbol[:-len("-USD")]) if symbol.endswith("-USD")
             else assets.stock_asset(symbol))
    bars, _src = sources.price_history(asset, days)
    return bars or []


class Prices:
    """Each (symbol, days) fetched once per run; a failing fetch is an empty series."""

    def __init__(self, fetch=None):
        self._fetch = fetch or _closes
        self._cache: dict[tuple[str, int], list[tuple[str, float]]] = {}

    def bars(self, symbol: str, days: int = PRICE_DAYS) -> list[tuple[str, float]]:
        key = (symbol, days)
        if key not in self._cache:
            try:
                self._cache[key] = list(self._fetch(symbol, days) or [])
            except Exception as e:
                print(f"[paper] no prices for {symbol}: {type(e).__name__}: {e}", file=sys.stderr)
                self._cache[key] = []
        return self._cache[key]


def close_on_or_before(bars: list[tuple[str, float]], day: str) -> float | None:
    found = None
    for d, c in bars:
        if d > day:
            break
        found = c
    return found


def first_close_after(bars: list[tuple[str, float]], day: str) -> tuple[str, float] | None:
    return next(((d, c) for d, c in bars if d > day), None)


def business_days_between(start: str, today: dt.date) -> int:
    """Weekdays after `start`, up to and including `today`."""
    d, n = dt.date.fromisoformat(start), 0
    while d < today:
        d += dt.timedelta(days=1)
        n += d.weekday() < 5
    return n


# ------------------------------------------------------------------ books
def create_books(conn, today: dt.date) -> None:
    """Every book, once -- the first run is every book's start date."""
    for b in BOOKS:
        start = STOCK_START_EUR if b.sleeve == "stock" else CRYPTO_START_EUR
        bench = STOCK_BENCHMARK if b.sleeve == "stock" else CRYPTO_BENCHMARK
        conn.execute(
            "INSERT OR IGNORE INTO paper_books (code, sleeve, start_date, start_eur, cash_eur, "
            "bench_symbol) VALUES (?,?,?,?,?,?)",
            (b.code, b.sleeve, today.isoformat(), start, start, bench))
    conn.commit()
```

(`json`, `types` and `db` are imported now because Tasks 2–5 in this same file use them.)

- [ ] **Step 5: Run the tests to verify they pass**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_paper.py`
Expected: all pass.

- [ ] **Step 6: Run the full suite, then commit**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider`
Expected: all pass.

```bash
git add paper.py db.py tests/test_paper.py
git commit -m "feat(paper): books, listings, fees and price series for the paper portfolio

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Orders, fills and values

**Files:**
- Modify: `paper.py` (append a new section after `create_books`)
- Test: `tests/test_paper.py` (append)

**Interfaces:**
- Consumes (Task 1): `listing`, `fee`, `Prices`, `close_on_or_before`, `first_close_after`, `business_days_between`, `create_books`, and the tables.
- Produces:
  - **Reads:** `orders(conn, code) -> list[dict]` (all, oldest first); `pending_orders(conn, code)`; `open_positions(conn, code)`; `closed_positions(conn, code)`; `cash(conn, code) -> float`.
  - **Values:** `position_value(pos: dict, bars, fx_now: float) -> float | None`; `mark_to_market(conn, code, prices) -> None`; `book_value(conn, code) -> float`.
  - **Orders:**
    - `place_buy(conn, code, ticker, source, reason, today, amount_eur, *, max_positions=None, min_fraction=0.5, insiders=(), target=None) -> str`, which returns "pending", "skipped" or "duplicate";
    - `place_sell(conn, code, position: dict, reason, today) -> None`;
    - `fill_orders(conn, code, prices, today) -> None`.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_paper.py`:

```python
# ------------------------------------------------------------- orders & fills
def _book(conn, code="R1-E1"):
    paper.create_books(conn, TODAY - dt.timedelta(days=30))
    return code


def test_a_buy_fills_at_the_first_close_after_the_decision(conn):
    code = _book(conn)
    assert paper.place_buy(conn, code, "AAA", "SEC", "Сильный", TODAY - dt.timedelta(days=3),
                           8_000.0, max_positions=10) == "pending"
    prices = paper.Prices(Fetch({"AAA": _bars([100, 110, 120, 130])}))
    paper.fill_orders(conn, code, prices, TODAY)
    [p] = paper.open_positions(conn, code)
    assert p["fill_date"] == _days(2) and p["entry_close"] == 110.0      # not the decision day's 100
    assert p["cost_eur"] == 8_000.0 and p["net_eur"] == pytest.approx(8_000 * (1 - 0.0025))
    assert paper.cash(conn, code) == pytest.approx(72_000.0)
    assert [o["status"] for o in paper.orders(conn, code)] == ["filled"]


def test_value_follows_the_adjusted_close_and_the_currency(conn):
    code = _book(conn)
    paper.place_buy(conn, code, "EQNR", "NORWAY", "Сильный", TODAY - dt.timedelta(days=3),
                    8_000.0, max_positions=10)
    prices = paper.Prices(Fetch({"EQNR.OL": _bars([100, 100, 110, 125])}))
    paper.fill_orders(conn, code, prices, TODAY)
    paper.mark_to_market(conn, code, prices)
    [p] = paper.open_positions(conn, code)
    assert p["currency"] == "NOK" and p["entry_fx"] == pytest.approx(10.74)
    assert p["last_value"] == pytest.approx(p["net_eur"] * 1.25)
    assert paper.book_value(conn, code) == pytest.approx(72_000 + p["net_eur"] * 1.25)


def test_a_sale_fills_at_the_next_close_and_pays_the_fee(conn):
    code = _book(conn)
    paper.place_buy(conn, code, "AAA", "SEC", "Сильный", TODAY - dt.timedelta(days=4),
                    8_000.0, max_positions=10)
    prices = paper.Prices(Fetch({"AAA": _bars([100, 100, 100, 120, 150])}))
    paper.fill_orders(conn, code, prices, TODAY)                # buys at 100 (TODAY-3)
    [p] = paper.open_positions(conn, code)
    paper.place_sell(conn, code, p, "стоп -15%", TODAY - dt.timedelta(days=2))
    paper.place_sell(conn, code, p, "стоп -15%", TODAY - dt.timedelta(days=2))   # no second order
    paper.fill_orders(conn, code, prices, TODAY)                # sells at 120 (TODAY-1)
    assert paper.open_positions(conn, code) == []
    [closed] = paper.closed_positions(conn, code)
    assert closed["closed_date"] == _days(1) and closed["close_reason"] == "стоп -15%"
    assert closed["proceeds_eur"] == pytest.approx(p["net_eur"] * 1.2 * (1 - 0.0025))
    assert paper.cash(conn, code) == pytest.approx(72_000 + closed["proceeds_eur"])
    assert len([o for o in paper.orders(conn, code) if o["side"] == "sell"]) == 1


def test_an_order_with_no_price_for_five_business_days_is_cancelled(conn):
    code = _book(conn)
    paper.place_buy(conn, code, "AAA", "SEC", "Сильный", dt.date(2026, 10, 2), 8_000.0,
                    max_positions=10)
    prices = paper.Prices(Fetch({}))
    paper.fill_orders(conn, code, prices, dt.date(2026, 10, 8))    # 4 business days: waiting
    assert [o["status"] for o in paper.orders(conn, code)] == ["pending"]
    paper.fill_orders(conn, code, prices, dt.date(2026, 10, 12))   # 6: cancelled
    [o] = paper.orders(conn, code)
    assert o["status"] == "cancelled" and o["note"] == "не исполнено: нет цены"


def test_skips_are_recorded_with_why(conn):
    code = _book(conn)
    assert paper.place_buy(conn, code, "DE0007164600", "BAFIN", "Сильный", TODAY, 8_000.0,
                           max_positions=10) == "skipped"
    for i in range(10):
        assert paper.place_buy(conn, code, f"T{i}", "SEC", "Сильный", TODAY, 8_000.0,
                               max_positions=10) == "pending"
    assert paper.place_buy(conn, code, "T10", "SEC", "Сильный", TODAY, 8_000.0,
                           max_positions=10) == "skipped"
    notes = [o["note"] for o in paper.orders(conn, code) if o["status"] == "skipped"]
    assert notes == ["нет котировки", "мест нет"]


def test_a_partial_slice_needs_half_a_slice_of_cash(conn):
    code = _book(conn)
    conn.execute("UPDATE paper_books SET cash_eur = 5000 WHERE code = ?", (code,))
    assert paper.place_buy(conn, code, "AAA", "SEC", "Сильный", TODAY, 8_000.0,
                           max_positions=10) == "pending"
    assert [o["amount_eur"] for o in paper.orders(conn, code)] == [5_000.0]
    assert paper.place_buy(conn, code, "BBB", "SEC", "Сильный", TODAY, 8_000.0,
                           max_positions=10) == "skipped"
    assert paper.orders(conn, code)[-1]["note"] == "нет денег"


def test_a_held_or_pending_ticker_is_not_ordered_again(conn):
    code = _book(conn)
    assert paper.place_buy(conn, code, "AAA", "SEC", "Сильный", TODAY, 8_000.0,
                           max_positions=10) == "pending"
    assert paper.place_buy(conn, code, "AAA", "SEC", "Сильный", TODAY, 8_000.0,
                           max_positions=10) == "duplicate"
    assert len(paper.orders(conn, code)) == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_paper.py`
Expected: FAIL with `AttributeError: module 'paper' has no attribute 'place_buy'`.

- [ ] **Step 3: Implement.** Append to `paper.py`:

```python
# ----------------------------------------------------------- orders & fills
def _rows(conn, sql: str, params: tuple) -> list[dict]:
    cur = conn.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def orders(conn, code: str) -> list[dict]:
    return _rows(conn, "SELECT * FROM paper_orders WHERE book = ? ORDER BY id", (code,))


def pending_orders(conn, code: str) -> list[dict]:
    return _rows(conn, "SELECT * FROM paper_orders WHERE book = ? AND status = 'pending' "
                       "ORDER BY id", (code,))


def open_positions(conn, code: str) -> list[dict]:
    return _rows(conn, "SELECT * FROM paper_positions WHERE book = ? AND closed_date IS NULL "
                       "ORDER BY id", (code,))


def closed_positions(conn, code: str) -> list[dict]:
    return _rows(conn, "SELECT * FROM paper_positions WHERE book = ? AND closed_date IS NOT NULL "
                       "ORDER BY closed_date, id", (code,))


def cash(conn, code: str) -> float:
    return conn.execute("SELECT cash_eur FROM paper_books WHERE code = ?", (code,)).fetchone()[0]


def _add_cash(conn, code: str, delta: float) -> None:
    conn.execute("UPDATE paper_books SET cash_eur = cash_eur + ? WHERE code = ?", (delta, code))


def position_value(pos: dict, bars: list[tuple[str, float]], fx_now: float) -> float | None:
    """EUR value at the latest close: net x (close now / close on the fill day) x
    (FX then / FX now). Both closes come from the same fresh series, so a dividend
    adjustment made since the fill moves them together."""
    if not bars:
        return None
    entry = close_on_or_before(bars, pos["fill_date"])
    if not entry:
        return None
    return pos["net_eur"] * (bars[-1][1] / entry) * (pos["entry_fx"] / fx_now)


def mark_to_market(conn, code: str, prices: Prices) -> None:
    """Revalue every open position; one with no price keeps its last value."""
    for p in open_positions(conn, code):
        value = position_value(p, prices.bars(p["symbol"]), fx.per_eur(p["currency"], conn))
        if value is not None:
            conn.execute("UPDATE paper_positions SET last_value = ? WHERE id = ?", (value, p["id"]))
    conn.commit()


def book_value(conn, code: str) -> float:
    return cash(conn, code) + sum(
        p["last_value"] if p["last_value"] is not None else p["net_eur"]
        for p in open_positions(conn, code))


def _record(conn, code: str, ticker: str, source: str | None, side: str, reason: str,
            today: dt.date, status: str, *, amount: float | None = None,
            position_id: int | None = None, note: str | None = None, insiders=(),
            target: float | None = None) -> None:
    conn.execute(
        "INSERT INTO paper_orders (book, ticker, source, side, amount_eur, position_id, reason, "
        "created, status, note, insiders, target) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (code, ticker, source, side, amount, position_id, reason, today.isoformat(), status, note,
         json.dumps(list(insiders), ensure_ascii=False), target))
    conn.commit()


def place_buy(conn, code: str, ticker: str, source: str | None, reason: str, today: dt.date,
              amount_eur: float, *, max_positions: int | None = None, min_fraction: float = 0.5,
              insiders=(), target: float | None = None) -> str:
    """Queue a buy of `amount_eur`, filled at the next close. Returns "pending",
    "skipped" (recorded, with why) or "duplicate" (not recorded: the book already
    holds the ticker or has a buy pending for it). With less cash than
    `amount_eur`, it buys with what's left if that is at least `min_fraction` of
    the amount."""
    held = {p["ticker"] for p in open_positions(conn, code)}
    buys = [o for o in pending_orders(conn, code) if o["side"] == "buy"]
    if ticker in held or any(o["ticker"] == ticker for o in buys):
        return "duplicate"

    def skip(note: str) -> str:
        _record(conn, code, ticker, source, "buy", reason, today, "skipped", note=note)
        return "skipped"

    if listing(ticker, source) is None:
        return skip("нет котировки")
    if max_positions is not None and len(held) + len(buys) >= max_positions:
        return skip("мест нет")
    available = cash(conn, code) - sum(o["amount_eur"] or 0.0 for o in buys)
    if available >= amount_eur:
        amount = amount_eur
    elif available >= max(amount_eur * min_fraction, 1.0):
        amount = available
    else:
        return skip("нет денег")
    _record(conn, code, ticker, source, "buy", reason, today, "pending", amount=amount,
            insiders=insiders, target=target)
    return "pending"


def place_sell(conn, code: str, position: dict, reason: str, today: dt.date) -> None:
    if any(o["side"] == "sell" and o["position_id"] == position["id"]
           for o in pending_orders(conn, code)):
        return
    _record(conn, code, position["ticker"], position["source"], "sell", reason, today, "pending",
            position_id=position["id"])


def _set_order(conn, order_id: int, status: str, note: str | None = None) -> None:
    conn.execute("UPDATE paper_orders SET status = ?, note = ? WHERE id = ?", (status, note, order_id))


def _fill_buy(conn, code: str, order: dict, symbol: str, currency: str, day: str,
              close: float) -> None:
    amount = min(order["amount_eur"], cash(conn, code))
    if amount <= 0:
        _set_order(conn, order["id"], "cancelled", "нет денег")
        return
    net = amount * (1 - fee(order["ticker"], currency))
    conn.execute(
        "INSERT INTO paper_positions (book, ticker, source, symbol, currency, fill_date, cost_eur, "
        "net_eur, entry_close, entry_fx, insiders, target, reason, last_value) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (code, order["ticker"], order["source"], symbol, currency, day, amount, net, close,
         fx.per_eur(currency, conn), order["insiders"], order["target"], order["reason"], net))
    _add_cash(conn, code, -amount)
    _set_order(conn, order["id"], "filled")


def _fill_sell(conn, code: str, order: dict, bars: list[tuple[str, float]], day: str,
               close: float) -> None:
    [pos] = _rows(conn, "SELECT * FROM paper_positions WHERE id = ?", (order["position_id"],))
    entry = close_on_or_before(bars, pos["fill_date"]) or pos["entry_close"]
    value = pos["net_eur"] * (close / entry) * (pos["entry_fx"] / fx.per_eur(pos["currency"], conn))
    proceeds = value * (1 - fee(pos["ticker"], pos["currency"]))
    conn.execute("UPDATE paper_positions SET closed_date = ?, close_reason = ?, proceeds_eur = ?, "
                 "last_value = ? WHERE id = ?", (day, order["reason"], proceeds, value, pos["id"]))
    _add_cash(conn, code, proceeds)
    _set_order(conn, order["id"], "filled")


def fill_orders(conn, code: str, prices: Prices, today: dt.date) -> None:
    """Fill every pending order whose first close after its decision day is known --
    sales first, since they free cash, then buys in order. An order still without a
    price after ORDER_MAX_BUSINESS_DAYS is cancelled."""
    for order in sorted(pending_orders(conn, code), key=lambda o: (o["side"] != "sell", o["id"])):
        lst = listing(order["ticker"], order["source"])
        bars = prices.bars(lst[0]) if lst else []
        nxt = first_close_after(bars, order["created"])
        if nxt is None:
            if business_days_between(order["created"], today) > ORDER_MAX_BUSINESS_DAYS:
                _set_order(conn, order["id"], "cancelled", "не исполнено: нет цены")
            continue
        day, close = nxt
        if order["side"] == "sell":
            _fill_sell(conn, code, order, bars, day, close)
        else:
            _fill_buy(conn, code, order, lst[0], lst[1], day, close)
    conn.commit()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_paper.py`
Expected: all pass.

- [ ] **Step 5: Run the full suite, then commit**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider`
Expected: all pass.

```bash
git add paper.py tests/test_paper.py
git commit -m "feat(paper): orders filled at the next close, costs, cash and position values

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Stock books: buy rules R1/R2, exits E1–E4 and the analyst shadow

**Files:**
- Modify: `paper.py` (append)
- Test: `tests/test_paper.py` (append)

**Interfaces:**
- Consumes:
  - Task 2: `place_buy`, `place_sell`, `open_positions`, `book_value`, `orders`;
  - Task 1: `EXITS`, `SLICE`, `MAX_POSITIONS`, `R2_MIN_SCORE`, `BOOK_BY_CODE`;
  - existing code: `positions._insider_sale(conn, pos)`, which reads `pos.ticker`, `pos.opened_at` and `pos.insiders`; `research._analyst_raw(asset)`; `research.analyst_view(raw, price)`.
- Produces:
  - `stock_signals(selection, book) -> list`;
  - `insiders_of(sig) -> list[str]`;
  - `_analyst_target(ticker, source) -> float | None`, the network seam for the shadow;
  - `stock_exit_reason(conn, book, pos: dict, bars, today) -> str | None`;
  - `stock_step(conn, book, selection, prices, today) -> None`.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_paper.py`:

```python
# --------------------------------------------------------------- stock books
def _position(conn, code, ticker="AAA", fill_days_ago=10, net=8_000.0, value=None,
              insiders=("Jane Doe",), target=None, source="SEC"):
    conn.execute(
        "INSERT INTO paper_positions (book, ticker, source, symbol, currency, fill_date, cost_eur, "
        "net_eur, entry_close, entry_fx, insiders, target, reason, last_value) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (code, ticker, source, ticker, "USD", _days(fill_days_ago), net, net, 100.0, 1.16,
         json.dumps(list(insiders)), target, "Сильный", value if value is not None else net))
    conn.commit()
    return paper.open_positions(conn, code)[-1]


def _exit(conn, code, pos, bars=None):
    return paper.stock_exit_reason(conn, paper.BOOK_BY_CODE[code], pos, bars or [], TODAY)


def test_r1_takes_strong_stock_signals_and_r2_adds_high_scoring_candidates():
    sel = _sel([_sig("AAA"), _sig("CRYPTO:BTC", source="HOUSE")],
               [_sig("BBB", tier="candidate", score=75), _sig("CCC", tier="candidate", score=60)])
    assert [s.ticker for s in paper.stock_signals(sel, paper.BOOK_BY_CODE["R1-E1"])] == ["AAA"]
    assert [s.ticker for s in paper.stock_signals(sel, paper.BOOK_BY_CODE["R2-E1"])] == ["AAA", "BBB"]


def test_stock_step_orders_a_tenth_of_the_book_with_the_signals_insiders(conn):
    code = _book(conn)
    paper.stock_step(conn, paper.BOOK_BY_CODE[code], _sel([_sig("AAA", members=("Jane Doe", "John Roe"))]),
                     paper.Prices(Fetch({})), TODAY)
    [o] = paper.orders(conn, code)
    assert (o["ticker"], o["side"], o["amount_eur"]) == ("AAA", "buy", 8_000.0)
    assert json.loads(o["insiders"]) == ["Jane Doe", "John Roe"]
    assert o["reason"] == "Сильный: SEC, AAA Corp"


@pytest.mark.parametrize("code,days,expected", [
    ("R1-E1", 89, None), ("R1-E1", 90, "90 дн. в позиции"),
    ("R1-E2", 181, None), ("R1-E2", 182, "182 дн. в позиции"),
    ("R1-E3", 91, "91 дн. в позиции"), ("R1-E4", 90, "90 дн. в позиции"),
])
def test_holding_limits(conn, code, days, expected):
    _book(conn)
    assert _exit(conn, code, _position(conn, code, fill_days_ago=days)) == expected


@pytest.mark.parametrize("code,value,expected", [
    ("R1-E1", 6_799.0, "стоп -15%"), ("R1-E1", 10_001.0, None),
    ("R1-E2", 4_000.0, None),
    ("R1-E3", 6_799.0, "стоп -15%"),
    ("R1-E4", 6_799.0, "стоп -15%"), ("R1-E4", 10_001.0, "цель +25%"),
])
def test_stops_and_profit_targets(conn, code, value, expected):
    _book(conn)
    assert _exit(conn, code, _position(conn, code, value=value)) == expected


def test_an_insider_selling_after_the_buy_closes_e1_and_e4_only(conn):
    _book(conn)
    add_sec_sale(conn, "AAA", "Jane Doe", 500_000, _days(2))
    for code, expected in (("R1-E1", True), ("R1-E4", True), ("R1-E2", False), ("R1-E3", False)):
        reason = _exit(conn, code, _position(conn, code))
        assert (reason or "").startswith("продаёт инсайдер") is expected


def test_an_insider_sale_before_the_buy_does_not_count(conn):
    _book(conn)
    add_sec_sale(conn, "AAA", "Jane Doe", 500_000, _days(20))
    assert _exit(conn, "R1-E1", _position(conn, "R1-E1", fill_days_ago=10)) is None


def test_the_shadow_sells_at_the_analyst_target(conn):
    _book(conn)
    pos = _position(conn, "R1-E1-AN", target=150.0)
    assert _exit(conn, "R1-E1-AN", pos, _bars([140, 151])) == "цель аналитиков 150.00 достигнута"
    assert _exit(conn, "R1-E1-AN", pos, _bars([140, 149])) is None


def test_without_a_target_the_shadow_behaves_like_r1_e1(conn):
    _book(conn)
    pos = _position(conn, "R1-E1-AN", fill_days_ago=90)
    assert _exit(conn, "R1-E1-AN", pos, _bars([140, 151])) == "90 дн. в позиции"


def test_the_shadow_records_the_target_at_the_buy(conn, monkeypatch):
    _book(conn)
    monkeypatch.setattr(paper, "_analyst_target", lambda ticker, source: 150.0)
    paper.stock_step(conn, paper.BOOK_BY_CODE["R1-E1-AN"], _sel([_sig("AAA")]),
                     paper.Prices(Fetch({})), TODAY)
    assert paper.orders(conn, "R1-E1-AN")[0]["target"] == 150.0


def test_stock_step_places_a_sale_when_an_exit_holds(conn):
    _book(conn)
    _position(conn, "R1-E3", fill_days_ago=91)
    paper.stock_step(conn, paper.BOOK_BY_CODE["R1-E3"], _sel(), paper.Prices(Fetch({})), TODAY)
    [o] = paper.orders(conn, "R1-E3")
    assert (o["side"], o["reason"]) == ("sell", "91 дн. в позиции")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_paper.py`
Expected: FAIL with `AttributeError: module 'paper' has no attribute 'stock_signals'`.

- [ ] **Step 3: Implement.** Append to `paper.py`:

```python
# ------------------------------------------------------------ stock books
def stock_signals(selection, book: Book) -> list:
    """R1: the day's Сильный stock signals. R2: plus Кандидаты scoring R2_MIN_SCORE
    or more. A CRYPTO: ticker (a congressional crypto buy) never enters a stock book."""
    sigs = [t.signal for t in selection.strong if not crypto.is_crypto(t.signal.ticker)]
    if book.buy == "R2":
        sigs += [t.signal for t in selection.candidates
                 if not crypto.is_crypto(t.signal.ticker)
                 and (getattr(t.signal, "score", 0) or 0) >= R2_MIN_SCORE]
    return sigs


def insiders_of(sig) -> list[str]:
    names = list(getattr(sig, "member_names", None) or [])
    person = getattr(sig, "person", None)
    return names or ([person] if person else [])


def _buy_reason(sig) -> str:
    tier = "Сильный" if getattr(sig, "tier", None) == "strong" else "Кандидат"
    return f"{tier}: {sig.source}, {getattr(sig, 'company', None) or sig.ticker}"


def _analyst_target(ticker: str, source: str | None) -> float | None:
    """The analysts' consensus target when the shadow book buys, or None. Network seam."""
    try:
        import research
        lst = listing(ticker, source)
        if lst is None:
            return None
        raw, _src = research._analyst_raw(assets.stock_asset(lst[0]))
        view = research.analyst_view(raw, positions.last_close(ticker, source)) if raw else None
        return view.get("target_mean") if view else None
    except Exception as e:
        print(f"[paper] no analyst target for {ticker}: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def stock_exit_reason(conn, book: Book, pos: dict, bars: list[tuple[str, float]],
                      today: dt.date) -> str | None:
    """The first exit that holds for this book's rules, or None."""
    hold, stop, take, insider = EXITS[book.exit]
    if insider:
        sale = positions._insider_sale(conn, types.SimpleNamespace(
            ticker=pos["ticker"], opened_at=pos["fill_date"],
            insiders=json.loads(pos["insiders"] or "[]")))
        if sale:
            return f"продаёт инсайдер: {sale}"
    if book.analyst and pos["target"] and bars and bars[-1][1] >= pos["target"]:
        return f"цель аналитиков {pos['target']:,.2f} достигнута"
    if (today - dt.date.fromisoformat(pos["fill_date"])).days >= hold:
        return f"{hold} дн. в позиции"
    if pos["last_value"] is None:
        return None
    ret = pos["last_value"] / pos["net_eur"] - 1
    if stop is not None and ret <= stop:
        return f"стоп {stop:+.0%}"
    if take is not None and ret >= take:
        return f"цель {take:+.0%}"
    return None


def stock_step(conn, book: Book, selection, prices: Prices, today: dt.date) -> None:
    """Sales for every exit that holds, then a buy per new signal: a tenth of the
    book's value, at most MAX_POSITIONS, half a slice at least."""
    for pos in open_positions(conn, book.code):
        reason = stock_exit_reason(conn, book, pos, prices.bars(pos["symbol"]), today)
        if reason:
            place_sell(conn, book.code, pos, reason, today)
    value = book_value(conn, book.code)
    for sig in stock_signals(selection, book):
        target = _analyst_target(sig.ticker, sig.source) if book.analyst else None
        place_buy(conn, book.code, sig.ticker, sig.source, _buy_reason(sig), today, SLICE * value,
                  max_positions=MAX_POSITIONS, min_fraction=0.5, insiders=insiders_of(sig),
                  target=target)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_paper.py`
Expected: all pass.

- [ ] **Step 5: Run the full suite, then commit**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider`
Expected: all pass.

```bash
git add paper.py tests/test_paper.py
git commit -m "feat(paper): stock books -- R1/R2 buys, E1-E4 exits and the analyst shadow

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Crypto books C-A and C-B

**Files:**
- Modify: `paper.py` (append)
- Test: `tests/test_paper.py` (append)

**Interfaces:**
- Consumes:
  - Tasks 1–2: `place_buy`, `place_sell`, `open_positions`, `book_value`, `listing`, `CRYPTO_*`, `TREND_DAYS`, `SIGNAL_HOLD_DAYS`, `REBUY_BLOCK_DAYS`;
  - existing code: `positions._crypto_caution(conn, pos, today, trend_fn) -> str | None`, which reads `pos.ticker` and `pos.opened_at`; `crypto.ticker(coin)`; `signal_journal`.
- Produces:
  - `strong_coins(selection) -> set[str]`;
  - `above_trend(bars) -> bool | None`;
  - `crypto_step(conn, book, selection, prices, today, trend_fn) -> None`.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_paper.py`:

```python
# -------------------------------------------------------------- crypto books
FALLING = {"ret_7d": -6.0, "above_ma20": False}
RISING = {"ret_7d": 3.0, "above_ma20": True}


def _coin_sig(coin="BTC"):
    return types.SimpleNamespace(ticker=f"CRYPTO:{coin}", source="CRYPTO_ETF", crypto_kind="etf_flow",
                                 coin=coin, tier="strong", company="спот-ETF США", score=100.0)


def _coin_position(conn, code, coin="BTC", fill_days_ago=5, net=10_000.0, value=None):
    conn.execute(
        "INSERT INTO paper_positions (book, ticker, source, symbol, currency, fill_date, cost_eur, "
        "net_eur, entry_close, entry_fx, insiders, reason, last_value) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (code, f"CRYPTO:{coin}", "CRYPTO", f"{coin}-USD", "USD", _days(fill_days_ago), net, net,
         100.0, 1.16, "[]", "Сильный", value if value is not None else net))
    conn.commit()
    return paper.open_positions(conn, code)[-1]


def _journal(conn, coin, tier, days_ago):
    db.journal_signal(conn, {"source": "CRYPTO_ETF", "kind": "etf_flow", "ticker": f"CRYPTO:{coin}",
                             "tier": tier, "total_value_eur": 9e8})
    conn.execute("UPDATE signal_journal SET emitted_at = ? WHERE id = (SELECT max(id) FROM signal_journal)",
                 (_days(days_ago) + " 12:00:00",))
    conn.commit()


def _trend_bars(last, end=TODAY):
    return _bars([100.0] * (paper.TREND_DAYS - 1) + [last], end)


def _crypto(conn, code, selection=None, series=None, trend=None, today=TODAY):
    paper.crypto_step(conn, paper.BOOK_BY_CODE[code], selection or _sel(),
                      paper.Prices(Fetch(series or {})), today, lambda c, s: trend)
    return paper.orders(conn, code)


def test_c_a_buys_the_coins_share_on_a_strong_signal(conn):
    _book(conn)
    [o] = _crypto(conn, "C-A", _sel([_coin_sig("BTC")]))
    assert (o["ticker"], o["side"], o["amount_eur"]) == ("CRYPTO:BTC", "buy", 10_000.0)


def test_c_a_sells_on_a_price_confirmed_caution(conn):
    _book(conn)
    _coin_position(conn, "C-A", "BTC", fill_days_ago=5)
    _journal(conn, "BTC", "caution", days_ago=1)
    [o] = _crypto(conn, "C-A", trend=FALLING)
    assert o["side"] == "sell" and o["reason"].startswith("осторожно: отток из спот-ETF")


def test_c_a_ignores_a_caution_the_price_does_not_confirm(conn):
    _book(conn)
    _coin_position(conn, "C-A", "BTC")
    _journal(conn, "BTC", "caution", days_ago=1)
    assert _crypto(conn, "C-A", trend=RISING) == []


@pytest.mark.parametrize("days,value,expected", [
    (90, None, "90 дн. в позиции"), (10, 7_499.0, "стоп -25%"), (10, 7_600.0, None)])
def test_c_a_time_limit_and_stop(conn, days, value, expected):
    _book(conn)
    _coin_position(conn, "C-A", "BTC", fill_days_ago=days, value=value)
    orders = _crypto(conn, "C-A")
    assert [o["reason"] for o in orders] == ([expected] if expected else [])


def test_c_b_holds_a_coin_above_its_200_day_average(conn):
    _book(conn)
    orders = _crypto(conn, "C-B", series={"BTC-USD": _trend_bars(110), "ETH-USD": _trend_bars(90)})
    assert [(o["ticker"], o["reason"]) for o in orders] == [("CRYPTO:BTC", "выше 200-дн. средней")]


def test_c_b_buys_below_the_average_on_a_strong_signal(conn):
    _book(conn)
    orders = _crypto(conn, "C-B", _sel([_coin_sig("ETH")]),
                     series={"BTC-USD": _trend_bars(90), "ETH-USD": _trend_bars(90)})
    assert [(o["ticker"], o["reason"]) for o in orders] == [("CRYPTO:ETH", "Сильный крипто-сигнал")]


def test_c_b_sells_below_the_average(conn):
    _book(conn)
    _coin_position(conn, "C-B", "BTC")
    [o] = _crypto(conn, "C-B", series={"BTC-USD": _trend_bars(90), "ETH-USD": _trend_bars(90)})
    assert (o["side"], o["reason"]) == ("sell", "ниже 200-дн. средней")


def test_c_b_keeps_a_coin_for_30_days_after_a_strong_signal(conn):
    _book(conn)
    _coin_position(conn, "C-B", "BTC")
    _journal(conn, "BTC", "strong", days_ago=10)
    assert _crypto(conn, "C-B", series={"BTC-USD": _trend_bars(90), "ETH-USD": _trend_bars(90)}) == []


def test_c_b_sells_on_a_caution_and_waits_7_days_before_buying_again(conn):
    _book(conn)
    pos = _coin_position(conn, "C-B", "BTC")
    _journal(conn, "BTC", "caution", days_ago=1)
    [o] = _crypto(conn, "C-B", trend=FALLING,
                  series={"BTC-USD": _trend_bars(110), "ETH-USD": _trend_bars(90)})
    assert o["side"] == "sell" and o["reason"].startswith("осторожно")
    conn.execute("UPDATE paper_positions SET closed_date = ?, close_reason = ? WHERE id = ?",
                 (_days(3), o["reason"], pos["id"]))
    conn.execute("UPDATE paper_orders SET status = 'filled' WHERE id = ?", (o["id"],))
    conn.commit()
    assert len(_crypto(conn, "C-B", series={"BTC-USD": _trend_bars(110), "ETH-USD": _trend_bars(90)})) == 1
    later = TODAY + dt.timedelta(days=5)
    orders = _crypto(conn, "C-B", today=later,
                     series={"BTC-USD": _trend_bars(110, later), "ETH-USD": _trend_bars(90, later)})
    assert orders[-1]["side"] == "buy" and orders[-1]["reason"] == "выше 200-дн. средней"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_paper.py`
Expected: FAIL with `AttributeError: module 'paper' has no attribute 'crypto_step'`.

- [ ] **Step 3: Implement.** Append to `paper.py`:

```python
# ----------------------------------------------------------- crypto books
def strong_coins(selection) -> set[str]:
    """Coins with a Сильный crypto signal today (CryptoSignals only -- congressional
    crypto buys are never Сильный)."""
    return {t.signal.coin for t in selection.strong if hasattr(t.signal, "crypto_kind")}


def above_trend(bars: list[tuple[str, float]]) -> bool | None:
    """Is the last close above the TREND_DAYS average? None with too little history."""
    if len(bars) < TREND_DAYS:
        return None
    closes = [c for _d, c in bars[-TREND_DAYS:]]
    return bars[-1][1] > sum(closes) / TREND_DAYS


def _recent_strong(conn, coin: str, today: dt.date) -> bool:
    since = (today - dt.timedelta(days=SIGNAL_HOLD_DAYS)).isoformat()
    return conn.execute(
        "SELECT 1 FROM signal_journal WHERE ticker = ? AND tier = 'strong' "
        "AND date(emitted_at) >= ? AND date(emitted_at) <= ? LIMIT 1",
        (crypto.ticker(coin), since, today.isoformat())).fetchone() is not None


def _rebuy_blocked(conn, code: str, coin: str, today: dt.date) -> bool:
    since = (today - dt.timedelta(days=REBUY_BLOCK_DAYS)).isoformat()
    return conn.execute(
        "SELECT 1 FROM paper_positions WHERE book = ? AND ticker = ? AND closed_date >= ? "
        "AND close_reason LIKE 'осторожно%' LIMIT 1",
        (code, crypto.ticker(coin), since)).fetchone() is not None


def _caution(conn, pos: dict, today: dt.date, trend_fn) -> str | None:
    detail = positions._crypto_caution(
        conn, types.SimpleNamespace(ticker=pos["ticker"], opened_at=pos["fill_date"]), today, trend_fn)
    return f"осторожно: {detail}" if detail else None


def crypto_step(conn, book: Book, selection, prices: Prices, today: dt.date, trend_fn) -> None:
    """C-A trades the signals: buy on Сильный, sell on a price-confirmed caution, at
    CRYPTO_HOLD_DAYS or at CRYPTO_STOP. C-B holds a coin above its 200-day average or
    for SIGNAL_HOLD_DAYS after a Сильный signal; a caution sells it and blocks a
    re-buy for REBUY_BLOCK_DAYS. Each coin's share is the book value / coin count."""
    signalled = strong_coins(selection)
    held = {p["ticker"]: p for p in open_positions(conn, book.code)}
    share = book_value(conn, book.code) / len(CRYPTO_COINS)
    for coin in CRYPTO_COINS:
        ticker = crypto.ticker(coin)
        pos = held.get(ticker)
        if book.rule == "A":
            if pos:
                reason = _caution(conn, pos, today, trend_fn)
                if not reason and (today - dt.date.fromisoformat(pos["fill_date"])).days >= CRYPTO_HOLD_DAYS:
                    reason = f"{CRYPTO_HOLD_DAYS} дн. в позиции"
                if (not reason and pos["last_value"] is not None
                        and pos["last_value"] / pos["net_eur"] - 1 <= CRYPTO_STOP):
                    reason = f"стоп {CRYPTO_STOP:+.0%}"
                if reason:
                    place_sell(conn, book.code, pos, reason, today)
            elif coin in signalled:
                place_buy(conn, book.code, ticker, "CRYPTO", "Сильный крипто-сигнал", today, share,
                          min_fraction=0.0)
            continue
        trend = above_trend(prices.bars(listing(ticker, "CRYPTO")[0]))
        recent = coin in signalled or _recent_strong(conn, coin, today)
        if pos:
            caution = _caution(conn, pos, today, trend_fn)
            if caution:
                place_sell(conn, book.code, pos, caution, today)
            elif not trend and not recent:
                place_sell(conn, book.code, pos, "ниже 200-дн. средней", today)
        elif (trend or recent) and not _rebuy_blocked(conn, book.code, coin, today):
            reason = "выше 200-дн. средней" if trend else "Сильный крипто-сигнал"
            place_buy(conn, book.code, ticker, "CRYPTO", reason, today, share, min_fraction=0.0)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_paper.py`
Expected: all pass.

- [ ] **Step 5: Run the full suite, then commit**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider`
Expected: all pass.

```bash
git add paper.py tests/test_paper.py
git commit -m "feat(paper): crypto books -- C-A on signals, C-B on the 200-day trend plus signals

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: The daily run, values, benchmarks and the bot hook

**Files:**
- Modify: `paper.py` (append)
- Modify: `bot.py` (add `import paper` next to the other project imports; add `_run_paper` after `_record_cautions`; call it in `main()`)
- Test: `tests/test_paper.py` (append)

**Interfaces:**
- Consumes (Tasks 1–4): `create_books`, `Prices`, `fill_orders`, `mark_to_market`, `stock_step`, `crypto_step`, `book_value`, `cash`, `close_on_or_before`.
- Produces:
  - `max_drawdown(values: list[float]) -> float`, as a negative fraction;
  - `run(conn, selection, today=None, fetch=None, trend_fn=None) -> int`, the number of books that ran;
  - `bot._run_paper(conn, selection, args) -> None`.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_paper.py`:

```python
# --------------------------------------------------------------- daily run
def test_max_drawdown():
    assert paper.max_drawdown([100, 120, 90, 130, 117]) == pytest.approx(-0.25)
    assert paper.max_drawdown([100, 101]) == 0.0 and paper.max_drawdown([]) == 0.0


def _run_day(conn, day, series, selection=None):
    return paper.run(conn, selection or _sel(), today=day, fetch=Fetch(series),
                     trend_fn=lambda c, s: None)


def test_run_opens_books_places_orders_and_fills_them_the_next_day(conn, monkeypatch):
    monkeypatch.setattr(paper, "_analyst_target", lambda ticker, source: None)   # the shadow's network seam
    day1, day2 = TODAY - dt.timedelta(days=1), TODAY
    assert _run_day(conn, day1, {"SPY": _bars([500, 505], day1), "AAA": _bars([100, 100], day1),
                                 "BTC-USD": _trend_bars(110, day1), "ETH-USD": _trend_bars(90, day1)},
                    _sel([_sig("AAA")])) == 11
    assert [o["status"] for o in paper.orders(conn, "R1-E1")] == ["pending"]
    _run_day(conn, day2, {"SPY": _bars([500, 505, 510], day2), "AAA": _bars([100, 100, 120], day2),
                          "BTC-USD": _trend_bars(110, day2), "ETH-USD": _trend_bars(90, day2)})
    [p] = paper.open_positions(conn, "R1-E1")
    assert p["fill_date"] == day2.isoformat() and p["entry_close"] == 120.0
    rows = conn.execute("SELECT date, value, cash, bench FROM paper_equity WHERE book = 'R1-E1' "
                        "ORDER BY date").fetchall()
    assert [r[0] for r in rows] == [day1.isoformat(), day2.isoformat()]
    assert rows[0][1] == pytest.approx(80_000.0) and rows[0][3] == pytest.approx(80_000.0)
    assert rows[1][1] == pytest.approx(72_000.0 + p["net_eur"])
    assert rows[1][3] == pytest.approx(80_000.0 * 510 / 505)
    [btc] = paper.open_positions(conn, "C-B")
    assert btc["ticker"] == "CRYPTO:BTC"


def test_a_book_without_a_benchmark_price_stores_no_benchmark(conn):
    _run_day(conn, TODAY, {})
    assert conn.execute("SELECT value, bench FROM paper_equity WHERE book = 'R1-E1'").fetchone() == \
        (80_000.0, None)


def test_one_failing_book_does_not_stop_the_others(conn, monkeypatch):
    real = paper.crypto_step

    def boom(conn, book, *a, **k):
        if book.code == "C-A":
            raise RuntimeError("bad")
        return real(conn, book, *a, **k)
    monkeypatch.setattr(paper, "crypto_step", boom)
    assert _run_day(conn, TODAY, {}) == 10


def test_bot_warns_when_the_paper_pass_fails(conn, monkeypatch):
    import bot
    sent = []
    monkeypatch.setattr(bot.telegram_notify, "send_text", lambda text: sent.append(text) or True)

    def fail(conn, selection):
        raise RuntimeError("x")
    monkeypatch.setattr(paper, "run", fail)
    bot._run_paper(conn, _sel(), types.SimpleNamespace(no_telegram=False))
    assert len(sent) == 1 and "бумажный портфель" in sent[0]
    monkeypatch.setattr(paper, "run", lambda conn, selection: 11)
    bot._run_paper(conn, _sel(), types.SimpleNamespace(no_telegram=False))
    assert len(sent) == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_paper.py`
Expected: FAIL with `AttributeError: module 'paper' has no attribute 'max_drawdown'`.

- [ ] **Step 3: Implement the run.** Append to `paper.py`:

```python
# -------------------------------------------------------------- daily run
def max_drawdown(values: list[float]) -> float:
    """The largest fall from a peak to a later low, as a negative fraction (0 if none)."""
    peak, worst = None, 0.0
    for v in values:
        peak = v if peak is None else max(peak, v)
        if peak:
            worst = min(worst, v / peak - 1)
    return worst


def _snapshot(conn, code: str, prices: Prices, today: dt.date) -> None:
    """Store today's value and the benchmark indexed from the start date: start
    money x (close now / close on the start date) x (FX at the start / FX now)."""
    start_date, start_eur, symbol, start_fx = conn.execute(
        "SELECT start_date, start_eur, bench_symbol, bench_start_fx FROM paper_books WHERE code = ?",
        (code,)).fetchone()
    days = max(PRICE_DAYS, (today - dt.date.fromisoformat(start_date)).days + 30)
    bars = prices.bars(symbol, days)
    fx_now = fx.per_eur("USD", conn)
    bench = None
    base = close_on_or_before(bars, start_date) or (bars[0][1] if bars else None)
    if bars and base:
        if start_fx is None:
            start_fx = fx_now
            conn.execute("UPDATE paper_books SET bench_start_fx = ? WHERE code = ?", (start_fx, code))
        bench = start_eur * (bars[-1][1] / base) * (start_fx / fx_now)
    conn.execute("INSERT OR REPLACE INTO paper_equity (book, date, value, cash, bench) VALUES (?,?,?,?,?)",
                 (code, today.isoformat(), book_value(conn, code), cash(conn, code), bench))
    conn.commit()


def run(conn, selection, today: dt.date | None = None, fetch=None, trend_fn=None) -> int:
    """One daily pass over every book: fill pending orders at the new close, revalue,
    decide sales and buys (filled at the next close), store the day. One failing book
    is logged and skipped. Returns how many books ran."""
    today = today or dt.date.today()
    trend_fn = trend_fn or crypto.price_trend
    create_books(conn, today)
    prices = Prices(fetch)
    ran = 0
    for book in BOOKS:
        try:
            fill_orders(conn, book.code, prices, today)
            mark_to_market(conn, book.code, prices)
            if book.sleeve == "stock":
                stock_step(conn, book, selection, prices, today)
            else:
                crypto_step(conn, book, selection, prices, today, trend_fn)
            _snapshot(conn, book.code, prices, today)
            ran += 1
        except Exception as e:
            conn.rollback()
            print(f"[paper] {book.code} failed: {type(e).__name__}: {e}", file=sys.stderr)
    return ran
```

- [ ] **Step 4: Hook it into the bot.** In `bot.py`, add `import paper` to the project imports (alphabetically, after `import insider_score`). After `_record_cautions`, add:

```python
def _run_paper(conn, selection, args) -> None:
    """The paper portfolio's daily pass (paper.py) -- after the digest, whether or not
    Telegram is on. It trades virtual books only. A crash is reported like a failed
    source rather than taking the run down."""
    if _run_source("PAPER", paper.run, conn, selection) is None and not args.no_telegram:
        telegram_notify.send_text("⚠️ disclosure-bot: бумажный портфель упал в этом прогоне. "
                                  "Логи: data/launchd.err.log")
```

In `main()`, change

```python
        if not args.no_telegram:
            _send_digest(conn, selection, closes)
        signal_count = len(selection.strong) + len(selection.candidates)
```

to

```python
        if not args.no_telegram:
            _send_digest(conn, selection, closes)
        _run_paper(conn, selection, args)
        signal_count = len(selection.strong) + len(selection.candidates)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_paper.py tests/test_bot.py`
Expected: all pass. If `tests/test_bot.py` doesn't exist, run only `tests/test_paper.py`.

- [ ] **Step 6: Run the full suite, then commit**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider`
Expected: all pass.

```bash
git add paper.py bot.py tests/test_paper.py
git commit -m "feat(paper): the daily run, values against the benchmark, and the bot hook

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Views: success line, summary, book detail, monthly report, CLI, menu and README

**Files:**
- Create: `paper_report.py`
- Modify: `paper.py` (append `main` and the `__main__` guard)
- Modify: `bot.py` (`_run_paper` also sends the monthly report)
- Modify: `menu.py` (option 3)
- Modify: `README.md` (a new section before "## Тесты")
- Test: `tests/test_paper_report.py`

**Interfaces:**
- Consumes (Tasks 1–5): `BOOKS`, `BOOK_BY_CODE`, `SUCCESS_DAYS`, `MIN_STOCK_TRADES`, `max_drawdown`, `open_positions`, `closed_positions`, `orders`.
- Produces:
  - `paper_report.stats(conn, book, today) -> dict`, with keys `day`, `start`, `ret`, `bench_ret`, `dd`, `bench_dd`, `trades`, `open`, `status`;
  - `paper_report.format_summary(conn, today, *, monthly=False, html=False) -> str`;
  - `paper_report.format_book(conn, code) -> str`;
  - `paper_report.maybe_send_monthly_report(conn, today, send=None) -> bool`;
  - `paper.main(argv=None) -> int`;
  - `menu.show_paper(conn) -> None`.

- [ ] **Step 1: Write the failing tests.** Create `tests/test_paper_report.py`:

```python
"""paper_report.py: what the paper portfolio shows -- statistics, the success line,
the menu/CLI text and the monthly Telegram report."""
from __future__ import annotations

import datetime as dt

import pytest

import db
import paper
import paper_report

TODAY = dt.date(2026, 10, 5)


def _start(conn, days_ago):
    paper.create_books(conn, TODAY - dt.timedelta(days=days_ago))


def _equity(conn, code, rows):
    """rows: [(days_ago, value, bench)]"""
    for days_ago, value, bench in rows:
        conn.execute("INSERT OR REPLACE INTO paper_equity (book, date, value, cash, bench) "
                     "VALUES (?,?,?,?,?)",
                     (code, (TODAY - dt.timedelta(days=days_ago)).isoformat(), value, 0.0, bench))
    conn.commit()


def _closed_trades(conn, code, n):
    for i in range(n):
        conn.execute(
            "INSERT INTO paper_positions (book, ticker, source, symbol, currency, fill_date, cost_eur, "
            "net_eur, entry_close, entry_fx, closed_date, close_reason, proceeds_eur) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (code, f"T{i}", "SEC", f"T{i}", "USD", "2026-06-01", 8000, 7980, 10, 1.16, "2026-07-01",
             "90 дн. в позиции", 8100))
    conn.commit()


def test_running_before_day_182(conn):
    _start(conn, 100)
    _equity(conn, "R1-E1", [(100, 80_000, 80_000), (0, 88_000, 84_000)])
    s = paper_report.stats(conn, paper.BOOK_BY_CODE["R1-E1"], TODAY)
    assert (s["day"], s["status"]) == (100, "идёт")
    assert s["ret"] == pytest.approx(0.10) and s["bench_ret"] == pytest.approx(0.05)


def test_passing_needs_a_better_return_a_smaller_drop_and_20_trades(conn):
    _start(conn, 190)
    _equity(conn, "R1-E1", [(190, 80_000, 80_000), (100, 76_000, 64_000), (0, 90_000, 84_000)])
    _closed_trades(conn, "R1-E1", 20)
    assert paper_report.stats(conn, paper.BOOK_BY_CODE["R1-E1"], TODAY)["status"] == "пройдено"


def test_a_stock_book_with_too_few_trades_does_not_pass(conn):
    _start(conn, 190)
    _equity(conn, "R1-E1", [(190, 80_000, 80_000), (100, 76_000, 64_000), (0, 90_000, 84_000)])
    _closed_trades(conn, "R1-E1", 19)
    assert paper_report.stats(conn, paper.BOOK_BY_CODE["R1-E1"], TODAY)["status"] == "не пройдено"


def test_a_crypto_book_needs_no_trade_minimum(conn):
    _start(conn, 190)
    _equity(conn, "C-A", [(190, 20_000, 20_000), (100, 19_000, 15_000), (0, 24_000, 22_000)])
    assert paper_report.stats(conn, paper.BOOK_BY_CODE["C-A"], TODAY)["status"] == "пройдено"


def test_a_deeper_drop_than_the_benchmark_does_not_pass(conn):
    _start(conn, 190)
    _equity(conn, "C-A", [(190, 20_000, 20_000), (100, 10_000, 18_000), (0, 24_000, 22_000)])
    assert paper_report.stats(conn, paper.BOOK_BY_CODE["C-A"], TODAY)["status"] == "не пройдено"


def test_the_shadow_is_always_a_shadow(conn):
    _start(conn, 190)
    assert paper_report.stats(conn, paper.BOOK_BY_CODE["R1-E1-AN"], TODAY)["status"] == "тень"


def test_summary_lists_every_book_under_its_sleeve(conn):
    _start(conn, 47)
    _equity(conn, "R1-E1", [(47, 80_000, 80_000), (0, 84_320, 82_480)])
    text = paper_report.format_summary(conn, TODAY)
    assert "Бумажный портфель — день 47 из 182" in text
    assert "АКЦИИ (S&P 500:" in text and "КРИПТО (BTC:" in text
    assert "R1·E1 " in text and "+5.4%" in text and "(+2.3 п.п.)" in text
    assert "R1·E1+аналитики" in text and "тень" in text and "C-B" in text


def test_summary_before_the_first_run(conn):
    assert "ещё не запущен" in paper_report.format_summary(conn, TODAY)


def test_book_detail_lists_positions_trades_and_skips(conn):
    _start(conn, 30)
    _closed_trades(conn, "R1-E2", 1)
    paper.place_buy(conn, "R1-E2", "DE0007164600", "BAFIN", "Сильный: BAFIN, X", TODAY, 8_000.0,
                    max_positions=10)
    text = paper_report.format_book(conn, "R1-E2")
    assert "R1·E2" in text and "T0" in text and "90 дн. в позиции" in text and "нет котировки" in text


def test_monthly_report_goes_once_a_month_and_not_in_the_start_month(conn):
    sent = []
    paper.create_books(conn, dt.date(2026, 9, 28))
    _equity(conn, "R1-E1", [(7, 80_000, 80_000)])
    assert paper_report.maybe_send_monthly_report(conn, dt.date(2026, 9, 30), sent.append) is False
    assert paper_report.maybe_send_monthly_report(conn, dt.date(2026, 10, 1),
                                                  lambda t: sent.append(t) or True) is True
    assert paper_report.maybe_send_monthly_report(conn, dt.date(2026, 10, 2),
                                                  lambda t: sent.append(t) or True) is False
    assert len(sent) == 1 and "за месяц" in sent[0]


def test_cli_shows_a_book_and_rejects_an_unknown_one(conn, monkeypatch, capsys):
    _start(conn, 10)
    monkeypatch.setattr(db, "connect", lambda path: conn)
    assert paper.main(["R1-E1"]) == 0 and "R1·E1" in capsys.readouterr().out
    assert paper.main(["X-9"]) == 2 and "Нет такой книги" in capsys.readouterr().out


def test_menu_shows_the_paper_portfolio(conn, capsys):
    import menu
    _start(conn, 10)
    menu.show_paper(conn)
    assert "Бумажный портфель" in capsys.readouterr().out
```

Also append to `tests/test_paper.py`, for the report hook in the bot:

```python
def test_bot_sends_the_monthly_report_after_a_good_pass(conn, monkeypatch):
    import bot
    import paper_report
    calls = []
    monkeypatch.setattr(paper, "run", lambda conn, selection: 11)
    monkeypatch.setattr(paper_report, "maybe_send_monthly_report",
                        lambda conn, today: calls.append(today) or True)
    bot._run_paper(conn, _sel(), types.SimpleNamespace(no_telegram=False))
    bot._run_paper(conn, _sel(), types.SimpleNamespace(no_telegram=True))
    assert len(calls) == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_paper_report.py tests/test_paper.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'paper_report'`.

- [ ] **Step 3: Create `paper_report.py`**

```python
"""What the paper portfolio shows: per-book statistics against the benchmark, the
success line, the menu/CLI text and the monthly Telegram report. Spec:
docs/superpowers/specs/2026-09-28-paper-portfolio-design.md, sections 2-3."""
from __future__ import annotations

import datetime as dt

import db
import paper
import telegram_notify

_BENCH_NAME = {"stock": "S&P 500", "crypto": "BTC"}
_REPORT_KEY_TTL = 40 * 86400


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:+.1f}%"


def _book_row(conn, code: str):
    return conn.execute("SELECT start_date, start_eur FROM paper_books WHERE code = ?",
                        (code,)).fetchone()


def _value_on_or_before(conn, code: str, day: dt.date) -> float | None:
    row = conn.execute("SELECT value FROM paper_equity WHERE book = ? AND date <= ? "
                       "ORDER BY date DESC LIMIT 1", (code, day.isoformat())).fetchone()
    return row[0] if row else None


def stats(conn, book: paper.Book, today: dt.date) -> dict:
    start_date, start_eur = _book_row(conn, book.code)
    eq = conn.execute("SELECT value, bench FROM paper_equity WHERE book = ? ORDER BY date",
                      (book.code,)).fetchall()
    values = [v for v, _b in eq]
    benches = [b for _v, b in eq if b is not None]
    value = values[-1] if values else start_eur
    ret = value / start_eur - 1
    bench_ret = benches[-1] / start_eur - 1 if benches else None
    dd = paper.max_drawdown(values)
    bench_dd = paper.max_drawdown(benches) if benches else None
    trades = len(paper.closed_positions(conn, book.code))
    day = (today - dt.date.fromisoformat(start_date)).days
    if book.analyst:
        status = "тень"
    elif day < paper.SUCCESS_DAYS:
        status = "идёт"
    else:
        ok = (bench_ret is not None and ret > bench_ret and bench_dd is not None and dd > bench_dd
              and (book.sleeve == "crypto" or trades >= paper.MIN_STOCK_TRADES))
        status = "пройдено" if ok else "не пройдено"
    return {"day": day, "start": start_date, "ret": ret, "bench_ret": bench_ret, "dd": dd,
            "bench_dd": bench_dd, "trades": trades, "open": len(paper.open_positions(conn, book.code)),
            "status": status}


def _month_return(conn, code: str, today: dt.date) -> float | None:
    """The previous calendar month: value at its end / value at the end of the month
    before it (or the start money)."""
    end = today.replace(day=1) - dt.timedelta(days=1)
    before = end.replace(day=1) - dt.timedelta(days=1)
    v_end = _value_on_or_before(conn, code, end)
    v_begin = _value_on_or_before(conn, code, before) or _book_row(conn, code)[1]
    return v_end / v_begin - 1 if v_end else None


def format_summary(conn, today: dt.date, *, monthly: bool = False, html: bool = False) -> str:
    if conn.execute("SELECT COUNT(*) FROM paper_books").fetchone()[0] == 0:
        return "Бумажный портфель ещё не запущен — он стартует с первого ежедневного прогона."
    first = stats(conn, paper.BOOKS[0], today)
    start = dt.date.fromisoformat(first["start"]).strftime("%d.%m.%Y")
    lines = [telegram_notify._b(f"Бумажный портфель — день {first['day']} из {paper.SUCCESS_DAYS} "
                                f"(с {start})", html)]
    for sleeve, title in (("stock", "АКЦИИ"), ("crypto", "КРИПТО")):
        books = [b for b in paper.BOOKS if b.sleeve == sleeve]
        head = stats(conn, books[0], today)
        lines.append("")
        lines.append(telegram_notify._b(
            f"{title} ({_BENCH_NAME[sleeve]}: {_pct(head['bench_ret'])}, "
            f"худшая просадка {_pct(head['bench_dd'])})", html))
        for b in books:
            s = stats(conn, b, today)
            diff = ("—" if s["bench_ret"] is None
                    else f"{(s['ret'] - s['bench_ret']) * 100:+.1f} п.п.")
            line = (f"{b.label:<16} {_pct(s['ret'])}  ({diff})  просадка {_pct(s['dd'])}  "
                    f"сделок {s['trades']}  позиций {s['open']}  {s['status']}")
            if monthly:
                line += f"  за месяц {_pct(_month_return(conn, b.code, today))}"
            lines.append(telegram_notify._esc(line) if html else line)
    return "\n".join(lines)


def format_book(conn, code: str) -> str:
    book = paper.BOOK_BY_CODE[code]
    lines = [f"{book.label} — открытые позиции:"]
    for p in paper.open_positions(conn, code):
        value = p["last_value"] if p["last_value"] is not None else p["net_eur"]
        lines.append(f"  {p['ticker']:<14} с {p['fill_date']}  €{p['cost_eur']:,.0f} → €{value:,.0f} "
                     f"({_pct(value / p['cost_eur'] - 1)})  {p['reason']}")
    if len(lines) == 1:
        lines.append("  нет")
    lines.append("Сделки:")
    closed = paper.closed_positions(conn, code)
    for p in closed:
        lines.append(f"  {p['ticker']:<14} {p['fill_date']} → {p['closed_date']}  "
                     f"{_pct(p['proceeds_eur'] / p['cost_eur'] - 1)}  куплено: {p['reason']}  "
                     f"продано: {p['close_reason']}")
    if not closed:
        lines.append("  нет")
    skipped = [o for o in paper.orders(conn, code) if o["status"] in ("skipped", "cancelled")]
    if skipped:
        lines.append("Пропущено:")
        lines += [f"  {o['created']}  {o['ticker']:<14} {o['note']}" for o in skipped]
    return "\n".join(lines)


def maybe_send_monthly_report(conn, today: dt.date, send=None) -> bool:
    """The first run of each month sends the summary, with each book's month, once.
    Nothing in the month the books started. True when it was sent."""
    row = conn.execute("SELECT MIN(start_date) FROM paper_books").fetchone()
    if not row or not row[0] or row[0][:7] == today.strftime("%Y-%m"):
        return False
    key = f"paper_report_{today:%Y-%m}"
    if db.get_cached_value(conn, key, _REPORT_KEY_TTL) is not None:
        return False
    send = send or telegram_notify.send_text
    if not send(format_summary(conn, today, monthly=True, html=True)):
        return False
    db.save_cached_value(conn, key, 1.0)
    return True
```

- [ ] **Step 4: Add the CLI to `paper.py`.** Append:

```python
# -------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    """python paper.py         -- every book against its benchmark
       python paper.py R1-E2   -- one book's positions, trades and skips"""
    import argparse
    from pathlib import Path

    import paper_report
    ap = argparse.ArgumentParser(description="Бумажный портфель")
    ap.add_argument("book", nargs="?", help="код книги, например R1-E2 или C-A")
    args = ap.parse_args(argv)
    conn = db.connect(Path(__file__).parent / "data" / "disclosures.db")
    if not args.book:
        print(paper_report.format_summary(conn, dt.date.today()))
        return 0
    code = args.book.upper()
    if code not in BOOK_BY_CODE:
        print(f"Нет такой книги: {args.book}. Есть: {', '.join(BOOK_BY_CODE)}")
        return 2
    print(paper_report.format_book(conn, code))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Send the report from the bot.** In `bot.py`, replace `_run_paper` with:

```python
def _run_paper(conn, selection, args) -> None:
    """The paper portfolio's daily pass (paper.py) -- after the digest, whether or not
    Telegram is on. It trades virtual books only. A crash is reported like a failed
    source rather than taking the run down; on the first good pass of a month the
    monthly report goes out (paper_report.py)."""
    if _run_source("PAPER", paper.run, conn, selection) is None:
        if not args.no_telegram:
            telegram_notify.send_text("⚠️ disclosure-bot: бумажный портфель упал в этом прогоне. "
                                      "Логи: data/launchd.err.log")
        return
    if not args.no_telegram:
        import paper_report
        paper_report.maybe_send_monthly_report(conn, dt.date.today())
```

- [ ] **Step 6: Add the menu option.** In `menu.py`:

- add `import datetime as dt` and `import paper_report` to the imports, if they are missing;
- add before `def main()`:

```python
def show_paper(conn) -> None:
    """The paper portfolio: every virtual book against its benchmark. Details of one
    book: python paper.py R1-E2."""
    print()
    print(paper_report.format_summary(conn, dt.date.today()))
    print("\nПодробно по книге: python paper.py R1-E2 (или любой другой код)")
```

- in `main()`, add `print("3) Бумажный портфель")` after the option-2 line, the branch `elif choice == "3": show_paper(conn)` after the `"2"` branch, and change the fallback text to `"Не понял выбор, введите 0, 1, 2 или 3."`.

- [ ] **Step 7: Update the README.** Add this section to `README.md` directly before the line `## Тесты`:

```markdown
## Бумажный портфель

[paper.py](paper.py) каждый день ведёт 11 виртуальных книг по строгим правилам —
чтобы стратегия доказала себя до реальных денег. Бот ничего не покупает по-настоящему.

- **Акции (8 книг, по €80 000):** правила покупки R1 (только «Сильный») и R2
  («Сильный» + «Кандидат» от 70 баллов) × правила продажи E1 (продаёт инсайдер из
  сигнала / 90 дней / −15%), E2 (6 месяцев без стопа), E3 (3 месяца, стоп −15%),
  E4 (как E1 + фиксация +25%). Каждая покупка — 10% стоимости книги, не больше 10
  позиций.
- **Тень R1·E1+аналитики:** как R1·E1, плюс продажа по целевой цене аналитиков.
  Только для сравнения.
- **Крипто (2 книги, по €20 000):** C-A — покупка на «Сильном» крипто-сигнале,
  продажа по сигналу осторожности, подтверждённому ценой, через 90 дней или при −25%;
  C-B — держит монету выше её 200-дневной средней или 30 дней после «Сильного»
  сигнала. BTC и ETH поровну.
- Сделка исполняется по первому дневному закрытию после решения; издержки 0,25%
  (акция в валюте), 0,10% (акция в евро), 0,50% (монета) за каждую сторону.
  Доходность — по скорректированным ценам (дивиденды и сплиты), в евро.
- Сравнение: акции — с S&P 500 (SPY), крипто — с биткоином. «Пройдено» через 6
  месяцев: доходность выше рынка, худшая просадка меньше, у книги акций не меньше 20
  закрытых сделок.
- Меню → «3) Бумажный портфель»; одна книга подробно: `python paper.py R1-E2`.
  Первого числа каждого месяца — короткий отчёт в Telegram; по отдельным сделкам
  сообщений нет.
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_paper_report.py tests/test_paper.py tests/test_signals_view.py`
Expected: all pass.

- [ ] **Step 9: Run the full suite, then commit**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider`
Expected: all pass.

```bash
git add paper.py paper_report.py bot.py menu.py README.md tests/test_paper.py tests/test_paper_report.py
git commit -m "feat(paper): success line, summary, book detail, monthly report, menu and CLI

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## After the plan (manual, needs the user's go-ahead)

1. Merge and push.
2. Restart the Telegram bot.
3. The next daily run creates the books; that date is every book's start date.
