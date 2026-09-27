# Crypto Signals: Quality and Caution — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make crypto buy signals mean something:
- ETF flows come from all US spot funds and are measured against their own recent history;
- company buys become one weekly demand total that signals only when the week is unusual;
- sell-side moves become caution signals, shown in the menu only;
- a held coin gets a close alert when a caution is confirmed by the price.

**Architecture:**
- **ETF flows.** Farside's per-fund daily flows are stored in a new table, `crypto_etf_flows`. The ETF finder in `cluster/crypto.py` reads that table and falls back to the existing issuer snapshots.
- **Company buys.** The treasury finder is rewritten into a weekly company-demand total plus single-sale cautions.
- **Where cautions go.** `strategy.select` puts bearish crypto signals into a new `Selection.cautions`. The daily bot run journals them without sending them. The menu renders them. `positions.check_exits` turns a price-confirmed caution into a coin close alert.

**Tech Stack:** Python 3.12, SQLite, requests, pytest (offline: `tests/conftest.py` blocks all network access).

**Spec:** `docs/superpowers/specs/2026-09-27-crypto-signals-quality-and-caution-design.md`

## Global Constraints

- User-facing text is Russian; code, comments and log lines are English.
- The test suite stays offline. Every new test stubs the network (fetchers, `crypto.price_trend`, `cluster.enrich_signals`) or needs none.
  - Run everything with `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider` from the worktree root.
- **Caution signals are never pushed to Telegram.** The daily digest (`bot._send_digest` → `format_tiered_digest(..., include_cautions=False)`) never includes them. Only the menu passes `include_cautions=True`.
- Telegram text is HTML: escape anything external with `telegram_notify._esc` (the existing formatters already do).
- The stock tier rules and stock stop-loss (15%) are unchanged.
- **ETF thresholds** (spec §1):
  - "unusual" means the top 10% of the previous 126 trading days;
  - a day also needs at least $100M; a streak (3+ same-sign days) also needs at least $250M and is compared with 3-day totals;
  - under 30 stored days, the fixed $400M (day) and $500M (streak) apply;
  - the newest day must be at most 7 days old;
  - Farside counts as stale after 3 **business** days (it has no weekend rows).
- **Company demand** (spec §1):
  - this calendar week (Monday to today) must be above the 90th percentile of the previous 52 complete weeks, with zero weeks counted;
  - it must also reach at least €50M;
  - under 8 weeks of history, only the €50M floor applies;
  - one alert per coin per ISO week;
  - "(впервые)" is marked only when stored history covers at least 365 days.
- **Company sale caution:** any single sale filing of €10M or more, within the last 14 days.
- **Coin close alert:**
  - a caution signal journaled within the last 7 days, on or after the position's open date;
  - and the price confirms it: 7-day return below 0 and the price below the 20-day average;
  - the coin stop-loss is 25%.
- Commits use conventional messages and end with the trailer line `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Decisions this plan makes where the spec left room

- **Staleness in business days.** The spec's "newest stored day older than 3 days" is read as 3 business days. Farside only has trading-day rows, so a calendar count would flip to the fallback every Tuesday morning.
- **Close-alert layout.** The close alert for a coin uses the existing close-alert layout: `🚪 CRYPTO:BTC — сигнал осторожности`, then the detail line with entry → now. The spec's `🔔 Пора закрыть …` line was illustrative; one layout for all close alerts is clearer.
- **"How unusual" text** states the real history length: `больше, чем в 97% из 126 дней` and `больше, чем в 95% из 52 недель`.
- **Treasury-buy heading.** The heading for a treasury buy becomes `🪙 ПОКУПКИ КОМПАНИЙ ЗА НЕДЕЛЮ`. The menu's caution section is headed `⚠️ Осторожно (N)` and reuses the crypto signal layout, plus the ✓/✗ price line.

## File Structure

| File | Change |
|---|---|
| `crypto_etf.py` | + Farside parser and fetcher (`Flow`, `parse_farside`, `fetch_farside`, `FARSIDE_URLS`, `FARSIDE_ALL_URLS`) |
| `db.py` | + `crypto_etf_flows` table, `save_etf_flows`, `etf_flow_count` |
| `passes.py` | `run_crypto_etf_pass` also collects Farside (full history on the first run) |
| `cluster/crypto.py` | ETF finder on Farside with relative thresholds and issuer fallback; treasury finder rewritten into weekly demand plus sale cautions |
| `calibrate_strategy.py` | hide future `crypto_etf_flows` rows during a replay |
| `crypto_treasury.py` | + `backfill()` and a `--backfill DAYS` command |
| `crypto.py` | + `trend_confirms_down` |
| `strategy.py` | + `CAUTION`, `is_caution`, `_caution_tier`, `Selection.cautions` |
| `bot.py` | journal cautions every run (`_record_cautions`) |
| `telegram_notify.py` | treasury heading; `format_tiered_digest(include_cautions=)`; the `caution` close reason |
| `menu.py` | Сигналы shows cautions |
| `positions.py` | the `caution` close trigger; `EXIT_STOP_LOSS_PCT_CRYPTO = 25.0` |
| `README.md` | crypto section and the positions paragraph |
| `tests/fixtures/farside_btc_snippet.html` | new fixture |
| `tests/test_crypto.py`, `tests/test_strategy.py`, `tests/test_positions.py`, `tests/test_telegram_notify.py`, `tests/test_signals_view.py` | tests |

---

### Task 1: Farside flows: parser, storage and collection

**Files:**
- Modify: `crypto_etf.py` (imports at the top; append after `flows()` at the end of the file)
- Modify: `db.py` (SCHEMA, after the `crypto_etf_snapshots` table; functions after `save_crypto_etf_snapshot`)
- Modify: `passes.py` (`run_crypto_etf_pass`)
- Create: `tests/fixtures/farside_btc_snippet.html`
- Test: `tests/test_crypto.py` (ETF section)

**Interfaces:**
- Produces:
  - `crypto_etf.Flow(coin: str, date: str, fund: str, flow_usd: float)`, a frozen dataclass;
  - `crypto_etf.parse_farside(coin: str, raw_html: str) -> list[Flow]`;
  - `crypto_etf.fetch_farside(coin: str, full_history: bool = False, session=None) -> list[Flow]`;
  - `crypto_etf.FARSIDE_URLS: dict[str, str]` and `crypto_etf.FARSIDE_ALL_URLS: dict[str, str]`;
  - `db.save_etf_flows(conn, flows) -> None` and `db.etf_flow_count(conn, coin: str) -> int`;
  - the table `crypto_etf_flows(coin, date, fund, flow_usd, source)`.

- [ ] **Step 1: Create the fixture** `tests/fixtures/farside_btc_snippet.html`. It mirrors the real page layout: two header rows (logos, then fund tickers), a fee row, date rows, and summary rows. Negatives are in parentheses, and `-` means not yet reported:

```html
<html><body>
<table class="nav"><tr><td>menu</td></tr></table>
<table class="etf">
<thead>
<tr bgcolor="#eaeffa">
  <th><span class="tabletext">&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;</span></th>
  <th><div align="center"><img src="/images/etf/blackrock.jpg" alt="Blackrock"></div></th>
  <th><div align="center"><img src="/images/etf/fidelity.jpg" alt="Fidelity"></div></th>
  <th><div align="center"><img src="/images/etf/grayscale.jpg" alt="Grayscale"></div></th>
  <th><span class="tabletext">&nbsp;&nbsp;&nbsp;&nbsp;Total</span></th>
</tr>
<tr>
  <th></th>
  <th><span class="tabletext">&nbsp;&nbsp;&nbsp;&nbsp;IBIT</span></th>
  <th><span class="tabletext">&nbsp;&nbsp;FBTC</span></th>
  <th><span class="tabletext">&nbsp;&nbsp;&nbsp;GBTC</span></th>
  <th></th>
</tr>
<tr><th><span class="tabletext">Fee</span></th><th>0.25%</th><th>0.25%</th><th>1.50%</th><th></th></tr>
</thead>
<tbody>
<tr><td><span class="tabletext">24 Sep 2026</span></td><td>162.6</td><td>12.9</td><td>(4.0)</td><td>171.5</td></tr>
<tr><td><span class="tabletext">25 Sep 2026</span></td><td>1,097.0</td><td>(49.3)</td><td>-</td><td>1,047.7</td></tr>
<tr><td><span class="tabletext">Total</span></td><td>65,282</td><td>11,073</td><td>(27,841)</td><td>48,514</td></tr>
<tr><td><span class="tabletext">Average</span></td><td>96.1</td><td>16.3</td><td>(41.0)</td><td>71.4</td></tr>
</tbody>
</table>
<table class="foot"><tr><td>footer</td></tr></table>
</body></html>
```

- [ ] **Step 2: Write the failing tests.** Add them to `tests/test_crypto.py`, right after `test_flow_is_share_change_times_nav`:

```python
def test_farside_page_parses():
    flows = crypto_etf.parse_farside("BTC", fixture_text("farside_btc_snippet.html"))
    got = sorted((f.coin, f.date, f.fund, f.flow_usd) for f in flows)
    want = sorted([("BTC", "2026-09-24", "IBIT", 162.6e6), ("BTC", "2026-09-24", "FBTC", 12.9e6),
                   ("BTC", "2026-09-24", "GBTC", -4.0e6), ("BTC", "2026-09-25", "IBIT", 1_097.0e6),
                   ("BTC", "2026-09-25", "FBTC", -49.3e6)])
    assert [g[:3] for g in got] == [w[:3] for w in want]
    assert [g[3] for g in got] == pytest.approx([w[3] for w in want])


def test_farside_page_without_a_table_is_empty():
    assert crypto_etf.parse_farside("BTC", "<html>redesigned</html>") == []


def test_etf_flows_are_stored_and_the_newest_day_is_rewritten(conn):
    db.save_etf_flows(conn, [crypto_etf.Flow("BTC", "2026-09-25", "IBIT", 1e6)])
    db.save_etf_flows(conn, [crypto_etf.Flow("BTC", "2026-09-25", "IBIT", 5e6)])   # late funds filled in
    assert conn.execute("SELECT flow_usd FROM crypto_etf_flows").fetchall() == [(5e6,)]
    assert db.etf_flow_count(conn, "BTC") == 1 and db.etf_flow_count(conn, "ETH") == 0


def test_etf_pass_loads_the_full_history_once(conn, monkeypatch):
    import passes
    calls = []
    monkeypatch.setattr(crypto_etf, "fetch_snapshots", lambda: [])

    def fake(coin, full_history=False, session=None):
        calls.append((coin, full_history))
        return [crypto_etf.Flow(coin, "2026-09-25", "IBIT" if coin == "BTC" else "ETHA", 1e6)]
    monkeypatch.setattr(crypto_etf, "fetch_farside", fake)
    passes.run_crypto_etf_pass(conn, None)
    passes.run_crypto_etf_pass(conn, None)
    assert calls == [("BTC", True), ("ETH", True), ("BTC", False), ("ETH", False)]


def test_etf_pass_survives_farside_being_down(conn, monkeypatch):
    import passes
    monkeypatch.setattr(crypto_etf, "fetch_snapshots", lambda: [])

    def down(coin, full_history=False, session=None):
        raise crypto_etf.requests.RequestException("down")
    monkeypatch.setattr(crypto_etf, "fetch_farside", down)
    assert passes.run_crypto_etf_pass(conn, None) == 0
    assert db.etf_flow_count(conn, "BTC") == 0
```

- [ ] **Step 3: Run them to verify they fail**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_crypto.py -k "farside or etf_flows_are_stored or etf_pass"`
Expected: FAIL with `AttributeError: module 'crypto_etf' has no attribute 'parse_farside'`.

- [ ] **Step 4: Implement the parser.** In `crypto_etf.py`, add `import html` to the imports, so they read:

```python
import datetime as dt
import html
import re
from dataclasses import dataclass

import requests
```

Then append at the end of `crypto_etf.py`:

```python
# Farside Investors publishes every US spot fund's daily net flow, in $ millions, in
# one table per coin -- all the funds, where the issuer pages above cover only
# BlackRock's two. Outflows are in parentheses, "-" is a fund that hasn't reported yet.
FARSIDE_URLS = {"BTC": "https://farside.co.uk/btc/", "ETH": "https://farside.co.uk/eth/"}
FARSIDE_ALL_URLS = {"BTC": "https://farside.co.uk/bitcoin-etf-flow-all-data/",
                    "ETH": "https://farside.co.uk/ethereum-etf-flow-all-data/"}
_TABLE_RE = re.compile(r"<table.*?</table>", re.S | re.I)
_ROW_RE = re.compile(r"<tr.*?</tr>", re.S | re.I)
_CELL_RE = re.compile(r"<t[hd][^>]*>(.*?)</t[hd]>", re.S | re.I)
_FUND_RE = re.compile(r"[A-Z]{2,5}")


@dataclass(frozen=True)
class Flow:
    coin: str
    date: str          # ISO
    fund: str
    flow_usd: float    # net, negative for an outflow


def _cells(row: str) -> list[str]:
    return [html.unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in _CELL_RE.findall(row)]


def _farside_amount(text: str) -> float | None:
    t = text.replace(",", "").strip()
    negative = t.startswith("(") and t.endswith(")")
    try:
        value = float(t.strip("()"))
    except ValueError:          # "-", "", anything else: no figure yet
        return None
    return (-value if negative else value) * 1e6


def parse_farside(coin: str, raw_html: str) -> list[Flow]:
    """Every (day, fund) figure in the page's flow table, the largest table on it. The
    fund row is the header row whose second cell is a ticker; fee and summary rows
    (Total, Average, Maximum, Minimum) carry no date and are skipped, as is the
    unnamed Total column."""
    tables = _TABLE_RE.findall(raw_html)
    if not tables:
        return []
    funds: list[str] | None = None
    out = []
    for row in _ROW_RE.findall(max(tables, key=len)):
        cells = _cells(row)
        if funds is None:
            if len(cells) > 2 and _FUND_RE.fullmatch(cells[1]):
                funds = cells
            continue
        try:
            day = dt.datetime.strptime(cells[0], "%d %b %Y").date().isoformat()
        except (ValueError, IndexError):
            continue
        for fund, text in zip(funds[1:], cells[1:]):
            if not _FUND_RE.fullmatch(fund):
                continue
            usd = _farside_amount(text)
            if usd is not None:
                out.append(Flow(coin, day, fund, usd))
    return out


def fetch_farside(coin: str, full_history: bool = False,
                  session: requests.Session | None = None) -> list[Flow]:
    url = (FARSIDE_ALL_URLS if full_history else FARSIDE_URLS)[coin]
    resp = (session or requests.Session()).get(url, headers=_HEADERS, timeout=30)
    resp.raise_for_status()
    return parse_farside(coin, resp.text)
```

- [ ] **Step 5: Add storage.** In `db.py`'s SCHEMA, directly after the `crypto_etf_snapshots` table, add:

```sql
-- Daily net flow per US spot ETF, in $, from Farside (crypto_etf.parse_farside). The
-- newest day is rewritten on every run: funds that report late fill in over the day.
CREATE TABLE IF NOT EXISTS crypto_etf_flows (
    coin      TEXT NOT NULL,
    date      TEXT NOT NULL,   -- ISO
    fund      TEXT NOT NULL,
    flow_usd  REAL NOT NULL,
    source    TEXT NOT NULL DEFAULT 'farside',
    PRIMARY KEY (coin, date, fund)
);
```

Then, after `save_crypto_etf_snapshot`, add:

```python
def save_etf_flows(conn: sqlite3.Connection, flows) -> None:
    """flows are crypto_etf.Flow rows; a (coin, date, fund) already stored is
    overwritten, so a late-reporting fund fills in a day seen earlier."""
    conn.executemany(
        "INSERT OR REPLACE INTO crypto_etf_flows (coin, date, fund, flow_usd, source) "
        "VALUES (?, ?, ?, ?, 'farside')",
        [(f.coin, f.date, f.fund, f.flow_usd) for f in flows])
    conn.commit()


def etf_flow_count(conn: sqlite3.Connection, coin: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM crypto_etf_flows WHERE coin = ?",
                        (coin,)).fetchone()[0]
```

- [ ] **Step 6: Collect in the ETF pass.** In `passes.py`, replace `run_crypto_etf_pass` with:

```python
def run_crypto_etf_pass(conn, args) -> int:
    """Today's issuer-published share count and NAV per covered spot ETF
    (crypto_etf.py), plus every US spot fund's daily flows from Farside -- the whole
    history on the first run for a coin, the recent table after that. Returns how
    many issuer snapshots were new -- zero on a weekend or a second run the same
    day, which is why the liveness check tolerates streaks."""
    new_count = 0
    for snap in crypto_etf.fetch_snapshots():
        if not db.save_crypto_etf_snapshot(conn, snap):
            continue
        new_count += 1
        history = conn.execute(
            "SELECT as_of, shares_outstanding, nav_usd FROM crypto_etf_snapshots "
            "WHERE fund = ? ORDER BY as_of DESC LIMIT 2", (snap.fund,)).fetchall()
        for prev_as_of, as_of, flow in crypto_etf.flows(history):
            print(telegram_notify.format_etf_flow_line(snap.fund, snap.coin, prev_as_of, as_of, flow))
    for coin in crypto_etf.FARSIDE_URLS:
        full = db.etf_flow_count(conn, coin) == 0
        try:
            flows = crypto_etf.fetch_farside(coin, full_history=full)
        except Exception as e:  # Farside down: the finder falls back to the issuer snapshots
            print(f"[CRYPTO] Farside {coin} unavailable: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        db.save_etf_flows(conn, flows)
        print(f"[CRYPTO] Farside {coin}: {len(flows)} fund-day(s)" + (" (full history)" if full else ""))
    conn.commit()
    return new_count
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_crypto.py`
Expected: all pass.

- [ ] **Step 8: Run the full suite, then commit**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider`
Expected: all pass.

```bash
git add crypto_etf.py db.py passes.py tests/fixtures/farside_btc_snippet.html tests/test_crypto.py
git commit -m "feat(crypto_etf): every US spot fund's daily flows from Farside

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: ETF finder on Farside, with relative thresholds and the issuer fallback

**Files:**
- Modify: `cluster/crypto.py` (imports; ETF constants; `_daily_etf_flows`; `find_etf_flow_signals`)
- Modify: `calibrate_strategy.py:34-46` (`_VISIBLE`)
- Test: `tests/test_crypto.py` (ETF section)

**Interfaces:**
- Consumes (from Task 1): the `crypto_etf_flows` table, `db.save_etf_flows`, `crypto_etf.Flow` and `crypto_etf.FARSIDE_URLS`.
- Produces:
  - `cluster.crypto._p90(values: list[float]) -> float` and `cluster.crypto._share_below(value: float, values: list[float]) -> float` (Task 3 uses both);
  - `cluster.crypto.etf_flow_days(conn, today=None) -> dict[str, tuple[str, list[tuple[str, float, list[str]]]]]`, mapping a coin to (source, days), where source is `"farside"` or `"issuer"`;
  - `cluster.daily_etf_flows(conn)` keeps its shape: coin -> `[(day, net_usd, funds)]`;
  - `find_etf_flow_signals(conn, day_flow_usd=..., streak_days=..., streak_min_usd=..., ignore_alert_state=False, today=None)`, where an outflow is a `CryptoSignal` with `bullish=False`.

- [ ] **Step 1: Write the failing tests.** Add them to `tests/test_crypto.py`, after `test_stale_etf_snapshot_is_ignored`:

```python
def _add_farside(conn, coin, flows_musd, fund="IBIT", newest_days_ago=1):
    """One stored day per value (in $m), oldest first, the last one `newest_days_ago` ago."""
    n = len(flows_musd)
    db.save_etf_flows(conn, [crypto_etf.Flow(coin, _days_ago(newest_days_ago + n - 1 - i), fund, m * 1e6)
                             for i, m in enumerate(flows_musd)])


def test_unusual_etf_day_is_measured_against_its_own_history(conn):
    _add_farside(conn, "BTC", [50, -40] * 30 + [300])        # 60 ordinary days, then $300m
    [sig] = cluster.find_etf_flow_signals(conn)
    assert sig.bullish and sig.company == "спот-ETF США, фондов: 1"
    assert "больше, чем в 100% из 60 дней" in sig.details[0]
    assert not any("IBIT/ETHA" in d for d in sig.details)


def test_ordinary_day_in_a_busy_market_is_not(conn):
    _add_farside(conn, "BTC", [300, -250] * 30 + [120])
    assert cluster.find_etf_flow_signals(conn) == []


def test_etf_day_floor_applies_in_a_quiet_market(conn):
    _add_farside(conn, "BTC", [5, -4] * 30 + [60])            # top of its history, but under $100m
    assert cluster.find_etf_flow_signals(conn) == []


def test_unusual_outflow_streak_is_a_bearish_signal(conn):
    _add_farside(conn, "ETH", [-30, 40] * 30 + [-90, -90, -90])
    [sig] = cluster.find_etf_flow_signals(conn)
    assert not sig.bullish and "3 дн. подряд оттока" in sig.details[1]


def test_under_30_days_of_history_uses_the_fixed_thresholds(conn):
    _add_farside(conn, "BTC", [10, -10] * 5 + [450])
    [sig] = cluster.find_etf_flow_signals(conn)
    assert "порог по умолчанию: мало истории" in sig.details


def test_stale_farside_falls_back_to_the_issuer_snapshots(conn):
    _add_farside(conn, "BTC", [50, -40] * 30 + [900], newest_days_ago=10)
    _add_etf(conn, "IBIT", 2, 1_000_000_000)
    _add_etf(conn, "IBIT", 1, 1_010_000_000)                  # +$500m on the issuer page
    [sig] = cluster.find_etf_flow_signals(conn)
    assert "IBIT" in sig.company and "только IBIT/ETHA (Farside недоступен)" in sig.details


def test_dossier_flows_are_summed_across_farside_funds(conn):
    _add_farside(conn, "BTC", [10, 20])
    db.save_etf_flows(conn, [crypto_etf.Flow("BTC", _days_ago(1), "FBTC", 5e6)])
    days = cluster.daily_etf_flows(conn)["BTC"]
    assert days[-1][1] == pytest.approx(25e6) and days[-1][2] == ["FBTC", "IBIT"]


def test_calibration_hides_future_etf_flows():
    import calibrate_strategy
    assert calibrate_strategy._VISIBLE["crypto_etf_flows"] == "date <= '{d}'"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_crypto.py -k "unusual or ordinary_day or floor or under_30 or stale_farside or summed_across or calibration_hides"`
Expected: FAIL. The finder doesn't read `crypto_etf_flows` yet, and `_VISIBLE` has no such key.

- [ ] **Step 3: Implement.** In `cluster/crypto.py`, add `import math` to the imports:

```python
import datetime as dt
import math
from dataclasses import dataclass, field
```

Replace the ETF constants block (from `# Spot-ETF net flow, in USD,` through `ETF_MAX_AGE_DAYS = 7 ...`) with:

```python
# Spot-ETF net flow, in USD, summed across a coin's funds. "Unusual" is judged against
# the coin's own recent days -- the top 10% of the previous ETF_HISTORY_DAYS, with a
# floor -- because the market grows and a fixed dollar bar goes stale. The fixed
# bars apply only while there are fewer than ETF_MIN_HISTORY_DAYS stored days.
ETF_DAY_FLOW_USD = 400e6
ETF_STREAK_DAYS = 3
ETF_STREAK_MIN_USD = 500e6
ETF_MAX_AGE_DAYS = 7       # don't resurface a flow from a day this old
ETF_HISTORY_DAYS = 126     # about six months of trading days
ETF_MIN_HISTORY_DAYS = 30
ETF_DAY_FLOOR_USD = 100e6
ETF_STREAK_FLOOR_USD = 250e6
TOP_DECILE = 0.9
# Farside has trading-day rows only, so its staleness is counted in business days.
FARSIDE_STALE_BUSINESS_DAYS = 3
```

Replace `_daily_etf_flows` and the `daily_etf_flows = _daily_etf_flows` line with:

```python
def _p90(values: list[float]) -> float:
    """Nearest-rank 90th percentile of a non-empty list."""
    s = sorted(values)
    return s[max(0, math.ceil(TOP_DECILE * len(s)) - 1)]


def _share_below(value: float, values: list[float]) -> float:
    return sum(1 for v in values if v < value) / len(values)


def _business_days_since(day: str, today: dt.date) -> int:
    d, n = dt.date.fromisoformat(day), 0
    while d < today:
        n += d.weekday() < 5
        d += dt.timedelta(days=1)
    return n


def _issuer_etf_days(conn) -> dict[str, list[tuple[str, float, list[str]]]]:
    """coin -> [(as_of, net_flow_usd, funds)] oldest first, from the issuer share-count
    snapshots (IBIT/ETHA only)."""
    by_fund: dict[str, list] = {}
    coin_of: dict[str, str] = {}
    for fund, coin, as_of, shares, nav in conn.execute(
            "SELECT fund, coin, as_of, shares_outstanding, nav_usd FROM crypto_etf_snapshots"):
        by_fund.setdefault(fund, []).append((as_of, shares, nav))
        coin_of[fund] = coin
    per_day: dict[str, dict[str, list]] = {}
    for fund, snaps in by_fund.items():
        for _prev, as_of, flow in crypto_etf.flows(snaps):
            slot = per_day.setdefault(coin_of[fund], {}).setdefault(as_of, [0.0, []])
            slot[0] += flow
            slot[1].append(fund)
    return {coin: [(d, v[0], v[1]) for d, v in sorted(days.items())]
            for coin, days in per_day.items()}


def _farside_etf_days(conn) -> dict[str, list[tuple[str, float, list[str]]]]:
    """coin -> [(date, net_flow_usd, funds)] oldest first, summed across every fund."""
    per_day: dict[str, dict[str, list]] = {}
    for coin, day, fund, flow in conn.execute(
            "SELECT coin, date, fund, flow_usd FROM crypto_etf_flows"):
        slot = per_day.setdefault(coin, {}).setdefault(day, [0.0, []])
        slot[0] += flow
        slot[1].append(fund)
    return {coin: [(d, v[0], sorted(v[1])) for d, v in sorted(days.items())]
            for coin, days in per_day.items()}


def etf_flow_days(conn, today: dt.date | None = None) -> dict[str, tuple[str, list]]:
    """coin -> (source, [(day, net_flow_usd, funds)] oldest first). Farside -- every US
    spot fund -- while its newest day is at most FARSIDE_STALE_BUSINESS_DAYS business
    days old; otherwise the issuer snapshots, which cover IBIT and ETHA only."""
    today = today or dt.date.today()
    farside, issuer = _farside_etf_days(conn), _issuer_etf_days(conn)
    out = {}
    for coin in sorted(set(farside) | set(issuer)):
        days = farside.get(coin)
        if days and _business_days_since(days[-1][0], today) <= FARSIDE_STALE_BUSINESS_DAYS:
            out[coin] = ("farside", days)
        elif issuer.get(coin):
            out[coin] = ("issuer", issuer[coin])
        elif days:
            out[coin] = ("farside", days)
    return out


def _daily_etf_flows(conn) -> dict[str, list[tuple[str, float, list[str]]]]:
    """coin -> [(day, net_flow_usd, funds)] oldest first, whichever source is current."""
    return {coin: days for coin, (_src, days) in etf_flow_days(conn).items()}


daily_etf_flows = _daily_etf_flows   # public name for the crypto dossier (crypto_research.py)
```

Replace `find_etf_flow_signals` with:

```python
def find_etf_flow_signals(conn, day_flow_usd: float = ETF_DAY_FLOW_USD,
                          streak_days: int = ETF_STREAK_DAYS,
                          streak_min_usd: float = ETF_STREAK_MIN_USD,
                          ignore_alert_state: bool = False,
                          today: dt.date | None = None) -> list[CryptoSignal]:
    """An unusual day, or an unusual run of same-direction days, judged on the most
    recent day only so an old flow never resurfaces. Unusual = the top 10% of the
    previous ETF_HISTORY_DAYS (a day against days, a streak against 3-day totals),
    and at least the floor; with under ETF_MIN_HISTORY_DAYS of history the fixed
    day_flow_usd / streak_min_usd apply instead. An inflow is a buy signal, an
    outflow a caution signal (strategy.select)."""
    today = today or dt.date.today()
    signals = []
    for coin, (source, days) in etf_flow_days(conn, today).items():
        if not days:
            continue
        last_date, last_flow, funds = days[-1]
        if (today - dt.date.fromisoformat(last_date)).days > ETF_MAX_AGE_DAYS or last_flow == 0:
            continue
        inflow = last_flow > 0
        streak = []
        for d in reversed(days):
            if d[1] == 0 or (d[1] > 0) != inflow:
                break
            streak.append(d)
        streak.reverse()
        streak_total = sum(d[1] for d in streak)

        history = days[:-1][-ETF_HISTORY_DAYS:]
        relative = len(history) >= ETF_MIN_HISTORY_DAYS
        day_sizes = [abs(d[1]) for d in history]
        three_day = [abs(history[i][1] + history[i - 1][1] + history[i - 2][1])
                     for i in range(2, len(history))]
        if relative:
            day_bar = max(_p90(day_sizes), ETF_DAY_FLOOR_USD)
            streak_bar = max(_p90(three_day), ETF_STREAK_FLOOR_USD)
        else:
            day_bar, streak_bar = day_flow_usd, streak_min_usd

        keys, details = [], []
        if abs(last_flow) >= day_bar:
            keys.append(f"{coin}|day|{last_date}")
        if len(streak) >= streak_days and abs(streak_total) >= streak_bar:
            keys.append(f"{coin}|streak|{'in' if inflow else 'out'}|{streak[0][0]}")
            details.append(f"{len(streak)} дн. подряд {'притока' if inflow else 'оттока'}, "
                           f"всего ${abs(streak_total) / 1e6:,.0f} млн")
        if not keys:
            continue
        if not ignore_alert_state and not _is_new(conn, "CRYPTO_ETF", keys):
            continue
        day_line = f"за {last_date}: {'+' if inflow else '−'}${abs(last_flow) / 1e6:,.0f} млн"
        if relative:
            day_line += (f" — больше, чем в {_share_below(abs(last_flow), day_sizes) * 100:.0f}% "
                         f"из {len(history)} дней")
        details.insert(0, day_line)
        if not relative:
            details.append("порог по умолчанию: мало истории")
        if source == "issuer":
            details.append("только IBIT/ETHA (Farside недоступен)")
            company = "спот-ETF: " + ", ".join(sorted(funds))
            url = crypto_etf.FUNDS[sorted(funds)[0]][1]
        else:
            company = f"спот-ETF США, фондов: {len(funds)}"
            url = crypto_etf.FARSIDE_URLS.get(coin)
        headline_total = streak_total if len(streak) >= streak_days else last_flow
        signals.append(CryptoSignal(
            source="CRYPTO_ETF", crypto_kind="etf_flow", ticker=crypto.ticker(coin),
            company=company, bullish=inflow, units=None,
            total_value=fx.to_eur(abs(headline_total), "USD", conn),
            window_start=streak[0][0] if streak else last_date, window_end=last_date,
            details=details, url=url, alert_keys=keys, member_names=sorted(funds),
        ))
    return signals
```

In `calibrate_strategy.py`'s `_VISIBLE`, add a line after `"crypto_etf_snapshots": "as_of <= '{d}'",`:

```python
    "crypto_etf_flows": "date <= '{d}'",
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_crypto.py tests/test_crypto_research.py`
Expected: all pass. The existing issuer-snapshot tests (`test_big_etf_day_is_a_signal`, `test_etf_outflow_streak_fires_once_per_streak` and the others) still pass through the fallback, because an empty `crypto_etf_flows` means "issuer".

- [ ] **Step 5: Run the full suite, then commit**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider`
Expected: all pass.

```bash
git add cluster/crypto.py calibrate_strategy.py tests/test_crypto.py
git commit -m "feat(cluster): ETF flows from every US spot fund, judged against their own history

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Company demand as a weekly total; sales as cautions

**Files:**
- Modify: `cluster/crypto.py` (treasury constants; `find_treasury_signals` replaced)
- Modify: `telegram_notify.py:497` (`_CRYPTO_HEADINGS`)
- Test: `tests/test_crypto.py` (the treasury section plus the tests that use it); `tests/test_signals_view.py` (two tests updated)

**Interfaces:**
- Consumes (from Task 2): `_p90`, `_share_below`, `_is_new`, `CryptoSignal`.
- Produces: `find_treasury_signals(conn, ignore_alert_state: bool = False, today: dt.date | None = None) -> list[CryptoSignal]`. It returns:
  - weekly demand signals (`bullish=True`, `company="компании: …"`, `details[0]` the week total, `details[1]` the buyers line);
  - sale signals (`bullish=False`, `company="Name (TICKER)"`).

- [ ] **Step 1: Rewrite the treasury tests.** In `tests/test_crypto.py`, replace `_add_treasury` and the five tests after it (`test_treasury_purchase_is_a_signal` through `test_treasury_signal_fires_once`) with:

```python
def _add_treasury(conn, units, avg=80_000.0, total=None, side="P", filed=None, acc="acc-1",
                  coin="BTC", company="Acme Corp", cik="1", co_ticker="ACME"):
    db.save_crypto_treasury_txn(conn, ct.TreasuryTxn(
        accession=acc, company=company, ticker=co_ticker, cik=cik, coin=coin, side=side,
        units=units, avg_price_usd=avg, total_usd=total, filed_date=filed or TODAY.isoformat(),
        form="8-K", source_url="https://sec.test/doc"))
    conn.commit()


def _weeks_ago(n: int) -> str:
    """A day inside the calendar week n weeks before this one."""
    return (TODAY - dt.timedelta(weeks=n)).isoformat()


def test_a_big_week_of_company_buying_is_a_signal(conn):
    _add_treasury(conn, 1_000)                                   # $80m this week, no history yet
    [sig] = cluster.find_treasury_signals(conn, today=TODAY)
    assert sig.bullish and sig.ticker == "CRYPTO:BTC" and sig.company == "компании: Acme Corp"
    assert sig.total_value == pytest.approx(80_000_000 / 1.16)
    assert sig.details[0] == "$80.0 млн за неделю"
    assert "порог по умолчанию: мало истории" in sig.details


def test_a_small_week_is_not(conn):
    _add_treasury(conn, 500)                                     # $40m, under €50m
    assert cluster.find_treasury_signals(conn, today=TODAY) == []


def test_a_routine_week_for_a_weekly_buyer_is_not(conn):
    for k in range(1, 21):                                       # $160m every week for 20 weeks
        _add_treasury(conn, 2_000, filed=_weeks_ago(k), acc=f"w{k}")
    _add_treasury(conn, 2_000, acc="now")                        # the same again this week
    assert cluster.find_treasury_signals(conn, today=TODAY) == []


def test_a_week_above_the_usual_says_how_unusual(conn):
    for k in range(1, 21):
        _add_treasury(conn, 2_000, filed=_weeks_ago(k), acc=f"w{k}")
    _add_treasury(conn, 6_000, acc="now")                        # three times the usual week
    [sig] = cluster.find_treasury_signals(conn, today=TODAY)
    assert sig.details[0] == "$480.0 млн за неделю — больше, чем в 100% из 20 недель"


def test_buyers_are_listed_largest_first_and_a_first_time_buyer_is_marked(conn):
    _add_treasury(conn, 1, filed=(TODAY - dt.timedelta(days=400)).isoformat(), acc="old",
                  company="Strategy Inc", cik="2", co_ticker="MSTR")      # history reaches a year back
    _add_treasury(conn, 900, acc="s", company="Strategy Inc", cik="2", co_ticker="MSTR")
    _add_treasury(conn, 300, acc="a")                                     # Acme's first purchase ever
    [sig] = cluster.find_treasury_signals(conn, today=TODAY)
    assert sig.details[1] == "Strategy Inc (MSTR) $72.0 млн · Acme Corp (ACME) $24.0 млн (впервые)"
    assert sig.member_names == ["Strategy Inc", "Acme Corp"]


def test_no_first_time_mark_before_a_year_of_history(conn):
    _add_treasury(conn, 1_000)
    [sig] = cluster.find_treasury_signals(conn, today=TODAY)
    assert "впервые" not in sig.details[1]


def test_units_only_purchase_is_valued_at_the_latest_stated_price(conn):
    _add_treasury(conn, 1, acc="priced")                          # states $80,000 a coin
    _add_treasury(conn, 1_000, avg=None, acc="unpriced")          # no price in the filing
    [sig] = cluster.find_treasury_signals(conn, today=TODAY)
    assert sig.total_value == pytest.approx(1_001 * 80_000 / 1.16)


def test_a_week_alerts_once(conn):
    _add_treasury(conn, 1_000)
    [sig] = cluster.find_treasury_signals(conn, today=TODAY)
    bot._commit_signals(conn, [sig])
    _add_treasury(conn, 1_000, acc="acc-2")                       # more buying the same week
    assert cluster.find_treasury_signals(conn, today=TODAY) == []


def test_last_weeks_buying_is_not_this_weeks_signal(conn):
    _add_treasury(conn, 1_000, filed=_weeks_ago(1))
    assert cluster.find_treasury_signals(conn, today=TODAY) == []


def test_a_large_company_sale_is_a_caution(conn):
    _add_treasury(conn, 200, side="S", filed=_days_ago(3))        # $16m
    [sig] = cluster.find_treasury_signals(conn, today=TODAY)
    assert not sig.bullish and sig.company == "Acme Corp (ACME)"
    assert sig.window_end == _days_ago(3)


def test_a_small_company_sale_is_not(conn):
    _add_treasury(conn, 100, side="S")                            # $8m, under €10m
    assert cluster.find_treasury_signals(conn, today=TODAY) == []


def test_an_old_sale_is_not_resurfaced(conn):
    _add_treasury(conn, 200, side="S", filed=_days_ago(30))
    assert cluster.find_treasury_signals(conn, today=TODAY) == []
```

Update the tests that relied on a $8M purchase being a signal. In `tests/test_crypto.py`:

- in `test_politician_and_company_buying_the_same_coin_corroborate`, change `_add_treasury(conn, 100)` to `_add_treasury(conn, 1_000)`;
- in `test_crypto_signal_journals_with_its_kind`, change `_add_treasury(conn, 100)` to `_add_treasury(conn, 1_000)`;
- replace `test_crypto_signal_formats` with:

```python
@pytest.mark.parametrize("html", [False, True])
def test_crypto_signal_formats(conn, html):
    _add_treasury(conn, 1_000)
    [sig] = cluster.find_treasury_signals(conn)
    text = telegram_notify.format_any_signal(sig, html=html)
    assert "ПОКУПКИ КОМПАНИЙ ЗА НЕДЕЛЮ: CRYPTO:BTC" in text and "1,000 BTC" in text
    assert "1 крипто" in telegram_notify.format_signals_digest([sig])
```

In `tests/test_signals_view.py`:

- replace `test_crypto_signal_is_dated_by_its_own_filing` with:

```python
def test_crypto_signal_is_dated_by_its_own_filing(conn):
    filed = (TODAY - dt.timedelta(days=2)).isoformat()
    db.save_crypto_treasury_txn(conn, ct.TreasuryTxn(
        "acc", "Acme", "ACME", "1", "BTC", "S", 200, 80_000.0, None, filed, "8-K", "u"))
    [sig] = cluster.find_treasury_signals(conn)
    assert cluster.disclosed_on(conn, sig) == filed
```

- in `test_view_keeps_recent_buyable_stocks_and_crypto`, change the treasury row's date from `recent` to `TODAY.isoformat()`, since a weekly total only counts this week's filings:

```python
    db.save_crypto_treasury_txn(conn, ct.TreasuryTxn(
        "acc", "Acme", "ACME", "1", "BTC", "P", 1000, 80_000.0, None, TODAY.isoformat(), "8-K", "u"))
```

- [ ] **Step 2: Run them to verify they fail**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_crypto.py tests/test_signals_view.py`
Expected: FAIL. `find_treasury_signals()` got an unexpected keyword argument `today`, and the old per-filing behaviour is still in place.

- [ ] **Step 3: Implement.** In `cluster/crypto.py`, replace the two treasury constants (`TREASURY_MIN_VALUE_EUR`, `TREASURY_WINDOW_DAYS`) and the comment above them with:

```python
# Company demand: every purchase of a coin filed this calendar week (Monday to
# today), summed across companies. Strategy and a few imitators buy almost every
# week, so a single filing says little -- the week is a signal only when it is above
# what TREASURY_TOP_SHARE of the previous weeks reached, and at least the floor.
TREASURY_WEEK_FLOOR_EUR = 50e6
TREASURY_HISTORY_WEEKS = 52
TREASURY_MIN_HISTORY_WEEKS = 8
TREASURY_FIRST_BUY_COVERAGE_DAYS = 365   # "first purchase ever" needs this much history
# A company selling coins is rare and is news: any single filing from this size is a
# caution signal, for TREASURY_WINDOW_DAYS after it was filed.
TREASURY_SALE_MIN_EUR = 10e6
TREASURY_WINDOW_DAYS = 14
```

Check that nothing else used the removed constant:

Run: `grep -rn "TREASURY_MIN_VALUE_EUR" --include=*.py . | grep -v .venv`
Expected: no output.

Replace `find_treasury_signals` (the whole function) with:

```python
def _monday(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def _usd_short(v: float) -> str:
    for unit, suffix in ((1e9, "млрд"), (1e6, "млн"), (1e3, "тыс")):
        if abs(v) >= unit:
            return f"${v / unit:,.1f} {suffix}"
    return f"${v:,.0f}"


def _treasury_rows(conn) -> list[dict]:
    """Every stored trade, with a dollar value: the filing's own total, else units x
    its average price, else units x the latest average price any filing stated for
    the coin, else None."""
    ref = dict(conn.execute(
        "SELECT coin, avg_price_usd FROM crypto_treasury_txns t WHERE avg_price_usd IS NOT NULL "
        "AND filed_date = (SELECT max(filed_date) FROM crypto_treasury_txns "
        "                  WHERE coin = t.coin AND avg_price_usd IS NOT NULL)").fetchall())
    rows = []
    for acc, company, co_ticker, cik, coin, side, units, avg, total, filed, url in conn.execute(
            "SELECT accession, company, ticker, cik, coin, side, units, avg_price_usd, total_usd, "
            "filed_date, source_url FROM crypto_treasury_txns WHERE filed_date IS NOT NULL"):
        usd = total or (units * avg if avg else (units * ref[coin] if ref.get(coin) else None))
        rows.append({"acc": acc, "company": company, "co_ticker": co_ticker,
                     "who": cik or company, "coin": coin, "side": side, "units": units,
                     "avg": avg, "usd": usd, "filed": dt.date.fromisoformat(filed[:10]),
                     "url": url})
    return rows


def _weekly_demand_signals(conn, rows: list[dict], today: dt.date,
                           ignore_alert_state: bool) -> list[CryptoSignal]:
    monday = _monday(today)
    coverage = min((r["filed"] for r in rows), default=None)
    weeks_covered = (monday - _monday(coverage)).days // 7 if coverage else 0
    n_hist = min(TREASURY_HISTORY_WEEKS, weeks_covered)
    relative = n_hist >= TREASURY_MIN_HISTORY_WEEKS
    first_buy_known = (coverage is not None
                       and (today - coverage).days >= TREASURY_FIRST_BUY_COVERAGE_DAYS)
    signals = []
    for coin in sorted({r["coin"] for r in rows}):
        buys = [r for r in rows if r["coin"] == coin and r["side"] == "P"]
        week = [r for r in buys if monday <= r["filed"] <= today]
        if not week:
            continue
        week_usd = sum(r["usd"] or 0.0 for r in week)
        week_eur = fx.to_eur(week_usd, "USD", conn) if week_usd else 0.0
        if week_eur < TREASURY_WEEK_FLOOR_EUR:
            continue
        hist = []
        for k in range(1, n_hist + 1):
            start = monday - dt.timedelta(weeks=k)
            usd = sum(r["usd"] or 0.0 for r in buys
                      if start <= r["filed"] < start + dt.timedelta(weeks=1))
            hist.append(fx.to_eur(usd, "USD", conn) if usd else 0.0)
        if relative and week_eur <= _p90(hist):
            continue
        iso_year, iso_week, _ = today.isocalendar()
        keys = [f"{coin}|week|{iso_year}-W{iso_week:02d}"]
        if not ignore_alert_state and not _is_new(conn, "CRYPTO_TREASURY", keys):
            continue

        earlier = {r["who"] for r in buys if r["filed"] < monday}
        by_company: dict[str, dict] = {}
        for r in week:
            c = by_company.setdefault(r["who"], {
                "name": f"{r['company']} ({r['co_ticker']})" if r["co_ticker"] else r["company"],
                "plain": r["company"], "usd": 0.0, "url": r["url"]})
            c["usd"] += r["usd"] or 0.0
        ranked = sorted(by_company.items(), key=lambda kv: kv[1]["usd"], reverse=True)
        buyers = []
        for who, c in ranked:
            amount = _usd_short(c["usd"]) if c["usd"] else "сумма неизвестна"
            first = " (впервые)" if first_buy_known and who not in earlier else ""
            buyers.append(f"{c['name']} {amount}{first}")
        head = f"{_usd_short(week_usd)} за неделю"
        if relative:
            head += f" — больше, чем в {_share_below(week_eur, hist) * 100:.0f}% из {len(hist)} недель"
        details = [head, " · ".join(buyers)]
        if not relative:
            details.append("порог по умолчанию: мало истории")
        signals.append(CryptoSignal(
            source="CRYPTO_TREASURY", crypto_kind="treasury", ticker=crypto.ticker(coin),
            company="компании: " + ", ".join(c["plain"] for _w, c in ranked), bullish=True,
            units=sum(r["units"] for r in week), total_value=week_eur,
            window_start=min(r["filed"] for r in week).isoformat(),
            window_end=max(r["filed"] for r in week).isoformat(),
            details=details, url=ranked[0][1]["url"], alert_keys=keys,
            member_names=[c["plain"] for _w, c in ranked],
        ))
    return signals


def _sale_signals(conn, rows: list[dict], today: dt.date,
                  ignore_alert_state: bool) -> list[CryptoSignal]:
    since = today - dt.timedelta(days=TREASURY_WINDOW_DAYS)
    grouped: dict[tuple, dict] = {}
    for r in rows:
        if r["side"] != "S" or r["filed"] < since:
            continue
        g = grouped.setdefault((r["acc"], r["coin"]), {"r": r, "units": 0.0, "usd": 0.0,
                                                       "known": True, "avgs": []})
        g["units"] += r["units"]
        if r["usd"] is None:
            g["known"] = False
        else:
            g["usd"] += r["usd"]
        if r["avg"]:
            g["avgs"].append(r["avg"])
    signals = []
    for (acc, coin), g in grouped.items():
        r = g["r"]
        # Unknown is not small: a sale with no stated value is kept and valued at
        # spot by enrich_signals.
        value_eur = fx.to_eur(g["usd"], "USD", conn) if g["known"] else None
        if value_eur is not None and value_eur < TREASURY_SALE_MIN_EUR:
            continue
        keys = [f"{acc}|{coin}|S"]
        if not ignore_alert_state and not _is_new(conn, "CRYPTO_TREASURY", keys):
            continue
        who = f"{r['company']} ({r['co_ticker']})" if r["co_ticker"] else r["company"]
        details = ([f"средняя цена ${sum(g['avgs']) / len(g['avgs']):,.0f} за {coin}"]
                   if g["avgs"] else [])
        signals.append(CryptoSignal(
            source="CRYPTO_TREASURY", crypto_kind="treasury", ticker=crypto.ticker(coin),
            company=who, bullish=False, units=g["units"], total_value=value_eur,
            window_start=r["filed"].isoformat(), window_end=r["filed"].isoformat(),
            details=details, url=r["url"], alert_keys=keys, member_names=[r["company"]],
        ))
    return signals


def find_treasury_signals(conn, ignore_alert_state: bool = False,
                          today: dt.date | None = None) -> list[CryptoSignal]:
    """Company demand for a coin -- this week's purchases summed across companies,
    a buy signal when the week is unusually large -- plus any single sale of
    TREASURY_SALE_MIN_EUR or more, a caution signal."""
    today = today or dt.date.today()
    rows = _treasury_rows(conn)
    return (_weekly_demand_signals(conn, rows, today, ignore_alert_state)
            + _sale_signals(conn, rows, today, ignore_alert_state))
```

In `telegram_notify.py`, change the first `_CRYPTO_HEADINGS` entry to:

```python
    ("treasury", True): "🪙 ПОКУПКИ КОМПАНИЙ ЗА НЕДЕЛЮ",
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_crypto.py tests/test_signals_view.py tests/test_strategy.py`
Expected: all pass.

- [ ] **Step 5: Run the full suite, then commit**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider`
Expected: all pass.

```bash
git add cluster/crypto.py telegram_notify.py tests/test_crypto.py tests/test_signals_view.py
git commit -m "feat(cluster): company crypto buying as one weekly total; sales as cautions

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: The treasury backfill command

**Files:**
- Modify: `crypto_treasury.py` (append `BACKFILL_SLICE_DAYS`, `backfill`, `main`, and the `__main__` guard at the end of the file)
- Test: `tests/test_crypto.py` (parser section)

**Interfaces:**
- Consumes: the existing `crypto_treasury.scan_new_filings(start, end, seen, session=None, cik_lookup=None)`, which already keeps the SEC pace (`REQUEST_PAUSE_SECONDS`), and `db.crypto_treasury_seen`, `db.save_crypto_treasury_txn` and `db.mark_crypto_treasury_seen`.
- Produces:
  - `crypto_treasury.backfill(conn, days: int, today=None, scan=None, cik_lookup=None) -> int`, returning the number of new trades;
  - `crypto_treasury.main(argv=None) -> int`;
  - the command `python crypto_treasury.py --backfill DAYS`.

- [ ] **Step 1: Write the failing tests.** Add them to `tests/test_crypto.py`, after `test_company_and_ticker_from_display_name`:

```python
def test_backfill_walks_the_range_in_weekly_slices_and_stores_trades(conn):
    slices = []

    def scan(start, end, seen, cik_lookup=None):
        slices.append((start, end))
        if start == dt.date(2026, 9, 1):
            yield "doc-1", [ct.TreasuryTxn("acc-b", "Acme", "ACME", "1", "BTC", "P", 10, 80_000.0,
                                           None, "2026-09-02", "8-K", "u")]
    assert ct.backfill(conn, 20, today=dt.date(2026, 9, 21), scan=scan) == 1
    assert slices == [(dt.date(2026, 9, 1), dt.date(2026, 9, 7)),
                      (dt.date(2026, 9, 8), dt.date(2026, 9, 14)),
                      (dt.date(2026, 9, 15), dt.date(2026, 9, 21))]
    assert "doc-1" in db.crypto_treasury_seen(conn)


def test_backfill_resumes_past_documents_already_read(conn):
    db.mark_crypto_treasury_seen(conn, "doc-1")
    conn.commit()
    seen_args = []

    def scan(start, end, seen, cik_lookup=None):
        seen_args.append(set(seen))
        return iter(())
    ct.backfill(conn, 6, today=dt.date(2026, 9, 21), scan=scan)
    assert seen_args == [{"doc-1"}]


def test_backfill_uses_the_paced_scanner_by_default(conn, monkeypatch):
    calls = []
    monkeypatch.setattr(ct, "scan_new_filings",
                        lambda s, e, seen, cik_lookup=None: calls.append((s, e)) or iter(()))
    ct.backfill(conn, 3, today=dt.date(2026, 9, 21))
    assert calls == [(dt.date(2026, 9, 18), dt.date(2026, 9, 21))]


def test_backfill_command(conn, monkeypatch):
    got = {}
    monkeypatch.setattr(ct, "backfill", lambda c, days, cik_lookup=None: got.setdefault("days", days) and 0)
    monkeypatch.setattr(db, "connect", lambda path: conn)
    monkeypatch.setattr("cik_map.CikMap", lambda: None)
    assert ct.main(["--backfill", "365"]) == 0 and got["days"] == 365
```

- [ ] **Step 2: Run them to verify they fail**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_crypto.py -k backfill`
Expected: FAIL with `AttributeError: module 'crypto_treasury' has no attribute 'backfill'`.

- [ ] **Step 3: Implement.** Append to `crypto_treasury.py`:

```python
BACKFILL_SLICE_DAYS = 7   # EFTS returns at most MAX_PAGES x PAGE_SIZE hits per query


def backfill(conn, days: int, today: dt.date | None = None, scan=None, cik_lookup=None) -> int:
    """Read the past `days` of 8-K/6-K filings, one week-long slice at a time, through
    the same paced scanner and parser the daily run uses. A document already read is
    skipped, and each one is committed as it is read, so an interrupted backfill
    resumes where it stopped. Returns how many new trades were stored."""
    import db
    scan = scan or scan_new_filings
    today = today or dt.date.today()
    seen = db.crypto_treasury_seen(conn)
    new = 0
    start = today - dt.timedelta(days=days)
    while start <= today:
        end = min(start + dt.timedelta(days=BACKFILL_SLICE_DAYS - 1), today)
        print(f"[backfill] {start.isoformat()}..{end.isoformat()}")
        for doc_id, txns in scan(start, end, seen, cik_lookup=cik_lookup):
            for t in txns:
                if db.save_crypto_treasury_txn(conn, t):
                    new += 1
            db.mark_crypto_treasury_seen(conn, doc_id)
            seen.add(doc_id)
            conn.commit()
        start = end + dt.timedelta(days=1)
    return new


def main(argv: list[str] | None = None) -> int:
    import argparse
    from pathlib import Path

    import cik_map
    import db
    ap = argparse.ArgumentParser(description="Company crypto treasury trades from 8-K/6-K filings.")
    ap.add_argument("--backfill", type=int, metavar="DAYS", required=True,
                    help="read this many past days of filings (one-time: 365 gives the weekly "
                         "company-demand signal its year of history)")
    args = ap.parse_args(argv)
    conn = db.connect(Path(__file__).parent / "data" / "disclosures.db")
    try:
        cik_lookup = cik_map.CikMap()
    except Exception as e:
        print(f"[backfill] no CIK->ticker map ({e}); using EDGAR's first-listed ticker")
        cik_lookup = None
    new = backfill(conn, args.backfill, cik_lookup=cik_lookup)
    print(f"[backfill] {new} new trade(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_crypto.py`
Expected: all pass.

- [ ] **Step 5: Run the full suite, then commit**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider`
Expected: all pass.

```bash
git add crypto_treasury.py tests/test_crypto.py
git commit -m "feat(crypto_treasury): resumable --backfill of past filings

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Caution signals: selection, journal and the menu section

**Files:**
- Modify: `crypto.py` (after `trend_confirms`)
- Modify: `strategy.py` (constants, `Selection`, new helpers, `select`)
- Modify: `bot.py` (`run_cluster_pass` prints cautions; new `_record_cautions`; `main` calls it)
- Modify: `telegram_notify.py` (`format_tiered_digest`)
- Modify: `menu.py` (`show_signals`)
- Test: `tests/test_crypto.py`, `tests/test_strategy.py`, `tests/test_telegram_notify.py`, `tests/test_signals_view.py`

**Interfaces:**
- Consumes: bearish `CryptoSignal`s from Tasks 2 and 3 and the existing `crypto.price_trend(conn, symbol) -> {"ret_7d", "above_ma20"} | None`.
- Produces:
  - `crypto.trend_confirms_down(trend) -> bool`;
  - `strategy.CAUTION = "caution"` and `strategy.is_caution(sig) -> bool`;
  - `strategy.Selection.cautions: list[Tiered]`, each with `tier == "caution"` and the signal's `.tier` set to `"caution"`;
  - `bot._record_cautions(conn, selection) -> None`;
  - `telegram_notify.format_tiered_digest(selection, closes, *, html=True, include_cautions=False)`;
  - journal rows with `tier = 'caution'` and `kind` = the crypto kind (Task 6 reads them).

- [ ] **Step 1: Write the failing tests.**

In `tests/test_crypto.py`, add `import strategy` to the imports. Then add, after `test_no_price_history_means_no_trend` at the end:

```python
def test_falling_price_below_its_average_confirms_a_caution(conn, monkeypatch):
    _closes(monkeypatch, [100.0] * 20 + [99, 98, 97, 96, 95, 94, 93, 92])
    assert crypto.trend_confirms_down(crypto.price_trend(conn, "BTC"))


def test_rising_price_does_not_confirm_a_caution(conn, monkeypatch):
    _closes(monkeypatch, [100.0] * 20 + [101, 102, 103, 104, 105, 106, 107, 110])
    assert not crypto.trend_confirms_down(crypto.price_trend(conn, "BTC"))
    assert not crypto.trend_confirms_down(None)


def test_cautions_are_journaled_and_marked_even_though_never_sent(conn):
    _add_treasury(conn, 200, side="S")
    [sig] = cluster.find_treasury_signals(conn, today=TODAY)
    sig.tier = strategy.CAUTION
    bot._record_cautions(conn, strategy.Selection([], [], True, cautions=[strategy.Tiered(sig, "caution")]))
    assert conn.execute("SELECT tier, kind, ticker FROM signal_journal").fetchall() == [
        ("caution", "treasury", "CRYPTO:BTC")]
    assert cluster.find_treasury_signals(conn, today=TODAY) == []
```

In `tests/test_strategy.py`, replace `test_crypto_outflow_is_never_listed` with:

```python
def test_crypto_outflow_is_a_caution_not_a_buy(conn, sized, monkeypatch):
    _trend(monkeypatch, {"ret_7d": -3.0, "above_ma20": False})
    sel = strategy.select(conn, [_etf(bullish=False)], _T212())
    assert not sel.strong and not sel.candidates
    [t] = sel.cautions
    assert t.tier == strategy.CAUTION and t.signal.tier == strategy.CAUTION
    assert any("цена подтверждает" in m for m in t.met)


def test_caution_the_price_does_not_confirm_says_so(conn, sized, monkeypatch):
    _trend(monkeypatch, {"ret_7d": 2.0, "above_ma20": True})
    [t] = strategy.select(conn, [_etf(bullish=False)], _T212()).cautions
    assert not t.met and any("цена не подтверждает" in m for m in t.missed)


def test_caution_without_a_price_says_so(conn, sized, monkeypatch):
    _trend(monkeypatch, None)
    [t] = strategy.select(conn, [_etf(bullish=False)], _T212()).cautions
    assert t.missed == ["цена не проверена"]


def test_old_caution_is_not_listed(conn, sized, monkeypatch):
    _trend(monkeypatch, None)
    old = cluster.CryptoSignal("CRYPTO_ETF", "etf_flow", "CRYPTO:BTC", "x", False, None, 5e8,
                               "2026-01-01", "2026-01-01", [], None, ["k"])
    assert strategy.select(conn, [old], _T212()).cautions == []


def test_inflows_are_not_cautions(conn, sized, monkeypatch):
    _trend(monkeypatch, {"ret_7d": 4.0, "above_ma20": True})
    assert strategy.select(conn, [_etf()], _T212()).cautions == []
```

In `tests/test_telegram_notify.py`, add `import cluster` and `import strategy` to the imports if they're missing. Then append:

```python
def _caution_selection():
    sig = cluster.CryptoSignal("CRYPTO_ETF", "etf_flow", "CRYPTO:BTC", "спот-ETF США, фондов: 12",
                               False, None, 9e8, "2026-09-24", "2026-09-26",
                               ["3 дн. подряд оттока, всего $1,050 млн"], None, ["k"])
    t = strategy.Tiered(sig, strategy.CAUTION,
                        ["цена подтверждает: BTC -6.2% за 7 дн., ниже 20-дн. средней"], [])
    return strategy.Selection(strong=[], candidates=[], t212_checked=True, exits=[], cautions=[t])


def test_cautions_are_listed_when_the_menu_asks():
    text = telegram_notify.format_tiered_digest(_caution_selection(), [], html=False,
                                                include_cautions=True)
    assert "⚠️ Осторожно (1)" in text and "ОТТОК ИЗ СПОТ-ETF" in text and "цена подтверждает" in text
    assert "сигналов нет" not in text


def test_cautions_are_never_in_the_telegram_digest():
    text = telegram_notify.format_tiered_digest(_caution_selection(), [])
    assert "Осторожно" not in text and "ОТТОК" not in text
```

In `tests/test_signals_view.py`, add after `test_view_keeps_recent_buyable_stocks_and_crypto`:

```python
def test_view_shows_crypto_cautions(conn, keyed, scored, no_prices, capsys, monkeypatch):
    monkeypatch.setattr(trading212, "fetch_instruments", lambda session=None: INSTRUMENTS)
    monkeypatch.setattr("crypto.price_trend", lambda conn, sym: {"ret_7d": -4.0, "above_ma20": False})
    db.save_crypto_treasury_txn(conn, ct.TreasuryTxn(
        "acc-s", "Acme", "ACME", "1", "BTC", "S", 200, 80_000.0, None, TODAY.isoformat(), "8-K", "u"))
    menu.show_signals(conn)
    out = _shown(capsys)
    assert "Осторожно (1)" in out and "КОМПАНИЯ ПРОДАЛА" in out and "цена подтверждает" in out
```

- [ ] **Step 2: Run them to verify they fail**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_crypto.py tests/test_strategy.py tests/test_telegram_notify.py tests/test_signals_view.py`
Expected: FAIL. There is no `trend_confirms_down`, no `Selection.cautions`, no `_record_cautions`, and no `include_cautions` keyword.

- [ ] **Step 3: Implement.**

In `crypto.py`, after `trend_confirms`:

```python
def trend_confirms_down(trend: dict | None) -> bool:
    """The mirror of trend_confirms, for a caution signal: down over 7 days and below
    the 20-day average."""
    return bool(trend) and trend["ret_7d"] < 0 and not trend["above_ma20"]
```

In `strategy.py`:

- change the tier constants line to:

```python
STRONG, CANDIDATE, CAUTION = "strong", "candidate", "caution"
```

- add a field at the end of `Selection`, after `exits`:

```python
    # Bearish crypto signals (ETF outflows, company sales, coins moving onto
    # exchanges): shown in the menu's Сигналы, journaled, never pushed -- see
    # bot._record_cautions and positions.check_exits.
    cautions: list = field(default_factory=list)
```

- add after `is_buy_side`:

```python
def is_caution(sig) -> bool:
    return hasattr(sig, "crypto_kind") and not sig.bullish
```

- add after `_crypto_tier`:

```python
def _caution_tier(conn, sig) -> Tiered:
    trend = crypto.price_trend(conn, sig.coin)
    if trend is None:
        return Tiered(sig, CAUTION, [], ["цена не проверена"])
    desc = (f"{sig.coin} {trend['ret_7d']:+.1f}% за 7 дн., "
            f"{'выше' if trend['above_ma20'] else 'ниже'} 20-дн. средней")
    if crypto.trend_confirms_down(trend):
        return Tiered(sig, CAUTION, [f"цена подтверждает: {desc}"], [])
    return Tiered(sig, CAUTION, [], [f"цена не подтверждает: {desc}"])
```

- in `select()`, after the line `exits = [s for s in signals if hasattr(s, "seller_count")]`, add:

```python
    raw_cautions = [s for s in signals if is_caution(s)
                    and (cluster.disclosed_on(conn, s) or "") >= since]
    raw_cautions = cluster.enrich_signals(conn, raw_cautions) if raw_cautions else []
    cautions = []
    for s in raw_cautions:
        s.tier = CAUTION
        cautions.append(_caution_tier(conn, s))
```

  and change the `return Selection(...)` to pass `cautions=cautions`:

```python
    return Selection(
        strong=[t for t in tiered if t.tier == STRONG],
        candidates=candidates[:MAX_CANDIDATES],
        t212_checked=t212 is not None,
        exits=exits,
        cautions=cautions,
    )
```

In `bot.py`:

- in `run_cluster_pass`, after the loop `for t in selection.strong + selection.candidates: print(...)`, add:

```python
    for t in selection.cautions:
        print("[caution] " + telegram_notify.format_any_signal(t.signal))
```

- after `_commit_signals`, add:

```python
def _record_cautions(conn, selection) -> None:
    """Caution signals are never pushed -- the menu's Сигналы shows them -- but they
    are journaled and marked alerted on the run that finds them: a coin position's
    close alert reads them from the journal (positions.check_exits)."""
    _commit_signals(conn, [t.signal for t in selection.cautions])
```

- in `main()`, change

```python
        selection = run_cluster_pass(conn, args)
        closes = positions.check_exits(conn)
```

  to

```python
        selection = run_cluster_pass(conn, args)
        _record_cautions(conn, selection)
        closes = positions.check_exits(conn)
```

In `telegram_notify.py`, replace `format_tiered_digest` with:

```python
def format_tiered_digest(selection, closes: list, *, html: bool = True,
                         include_cautions: bool = False) -> str:
    """One message: 🔥 Сильные, 👀 Кандидаты, 🚪 Закрыть, 🚨 Выходы -- the daily
    Telegram digest and the menu's "Сигналы" view (html=False) both render this.
    Only the menu passes include_cautions: caution signals are never pushed."""
    parts = []
    if not selection.t212_checked:
        parts.append("⚠️ Trading 212 не проверялся (нет ключа в .env) — показаны все акции.")
    if selection.strong:
        parts.append(_b(f"🔥 Сильные ({len(selection.strong)})", html))
        parts += ["\n".join([format_any_signal(t.signal, html=html)] + _rule_lines(t, html))
                  for t in selection.strong]
    if selection.candidates:
        parts.append(_b(f"👀 Кандидаты ({len(selection.candidates)})", html))
        parts += ["\n".join([format_any_signal(t.signal, html=html)] + _rule_lines(t, html))
                  for t in selection.candidates]
    if closes:
        parts.append(_b(f"🚪 Закрыть ({len(closes)})", html))
        parts += [format_close_alert(a, html=html) for a in closes]
    if selection.exits:
        parts.append(_b(f"🚨 Выходы ({len(selection.exits)})", html))
        parts += [format_any_signal(s, html=html) for s in selection.exits]
    cautions = getattr(selection, "cautions", []) if include_cautions else []
    if cautions:
        parts.append(_b(f"⚠️ Осторожно ({len(cautions)})", html))
        parts += ["\n".join([format_any_signal(t.signal, html=html)] + _rule_lines(t, html))
                  for t in cautions]
    if not (selection.strong or selection.candidates or closes or selection.exits or cautions):
        parts.append("За последние 3 дня сигналов нет.")
    return "\n\n".join(parts)
```

In `menu.py`'s `show_signals`, change the print line to:

```python
    print(telegram_notify.format_tiered_digest(selection, closes, html=False, include_cautions=True))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_crypto.py tests/test_strategy.py tests/test_telegram_notify.py tests/test_signals_view.py`
Expected: all pass.

- [ ] **Step 5: Run the full suite, then commit**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider`
Expected: all pass.

```bash
git add crypto.py strategy.py bot.py telegram_notify.py menu.py tests/test_crypto.py tests/test_strategy.py tests/test_telegram_notify.py tests/test_signals_view.py
git commit -m "feat(strategy): crypto caution signals, journaled and shown in the menu only

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Close alerts for held coins, the coin stop-loss, and the README

**Files:**
- Modify: `positions.py` (module docstring, constants, new `_crypto_caution`, `check_exits`)
- Modify: `telegram_notify.py:572-573` (`_CLOSE_REASON`)
- Modify: `README.md` (crypto section and the "Позиции" paragraph)
- Test: `tests/test_positions.py`, `tests/test_telegram_notify.py`

**Interfaces:**
- Consumes (from Task 5): `crypto.trend_confirms_down` and journal rows with `tier = 'caution'`.
- Produces:
  - `positions.check_exits(conn, today=None, price_fn=None, trend_fn=None) -> list[CloseAlert]`, where the new trigger is `"caution"` and `trend_fn(conn, symbol)` defaults to `crypto.price_trend`, resolved at call time;
  - `positions.EXIT_STOP_LOSS_PCT_CRYPTO = 25.0` and `positions.CAUTION_LOOKBACK_DAYS = 7`.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_positions.py`:

```python
# ------------------------------------------------------------- coin positions
_FALLING = {"ret_7d": -6.2, "above_ma20": False}
_RISING = {"ret_7d": 3.0, "above_ma20": True}


def _trend(value):
    return lambda conn, sym: value


def _caution_journal(conn, ticker="CRYPTO:BTC", days_ago=1, kind="etf_flow", value=9e8):
    db.journal_signal(conn, {"source": "CRYPTO_ETF", "kind": kind, "ticker": ticker,
                             "tier": "caution", "total_value_eur": value})
    conn.execute("UPDATE signal_journal SET emitted_at = ? WHERE id = (SELECT max(id) FROM signal_journal)",
                 ((TODAY - dt.timedelta(days=days_ago)).isoformat() + " 12:00:00",))
    conn.commit()


def test_a_caution_confirmed_by_the_price_closes_a_coin(conn):
    _open(conn, "CRYPTO:BTC", 84_500.0, days_ago=5)
    _caution_journal(conn, days_ago=1)
    [alert] = positions.check_exits(conn, today=TODAY, price_fn=lambda t, s=None: 79_900.0,
                                    trend_fn=_trend(_FALLING))
    assert alert.trigger == "caution" and alert.last_price == 79_900.0
    assert "отток из спот-ETF" in alert.detail and "-6.2% за 7 дн." in alert.detail


def test_an_unconfirmed_caution_does_not_close(conn):
    _open(conn, "CRYPTO:BTC", 84_500.0)
    _caution_journal(conn, days_ago=1)
    assert positions.check_exits(conn, today=TODAY, price_fn=lambda t, s=None: 84_000.0,
                                 trend_fn=_trend(_RISING)) == []


def test_a_caution_from_before_the_position_does_not_count(conn):
    _open(conn, "CRYPTO:BTC", 84_500.0, days_ago=2)
    _caution_journal(conn, days_ago=4)
    assert positions.check_exits(conn, today=TODAY, price_fn=lambda t, s=None: 84_000.0,
                                 trend_fn=_trend(_FALLING)) == []


def test_a_caution_the_price_confirms_days_later_still_closes(conn):
    _open(conn, "CRYPTO:BTC", 84_500.0, days_ago=6)
    _caution_journal(conn, days_ago=3)
    [alert] = positions.check_exits(conn, today=TODAY, price_fn=lambda t, s=None: 80_000.0,
                                    trend_fn=_trend(_FALLING))
    assert alert.trigger == "caution"


def test_a_caution_older_than_a_week_does_not_count(conn):
    _open(conn, "CRYPTO:BTC", 84_500.0, days_ago=20)
    _caution_journal(conn, days_ago=8)
    assert positions.check_exits(conn, today=TODAY, price_fn=lambda t, s=None: 84_000.0,
                                 trend_fn=_trend(_FALLING)) == []


def test_a_caution_on_another_coin_does_not_count(conn):
    _open(conn, "CRYPTO:BTC", 84_500.0)
    _caution_journal(conn, ticker="CRYPTO:ETH")
    assert positions.check_exits(conn, today=TODAY, price_fn=lambda t, s=None: 84_000.0,
                                 trend_fn=_trend(_FALLING)) == []


def test_no_price_trend_means_the_caution_waits(conn):
    _open(conn, "CRYPTO:BTC", 84_500.0)
    _caution_journal(conn, days_ago=1)
    assert positions.check_exits(conn, today=TODAY, price_fn=lambda t, s=None: 84_000.0,
                                 trend_fn=_trend(None)) == []


def test_coin_stop_loss_is_25_percent(conn):
    _open(conn, "CRYPTO:BTC", 100.0)
    assert positions.check_exits(conn, today=TODAY, price_fn=lambda t, s=None: 80.0,
                                 trend_fn=_trend(None)) == []
    [alert] = positions.check_exits(conn, today=TODAY, price_fn=lambda t, s=None: 74.9,
                                    trend_fn=_trend(None))
    assert alert.trigger == "stop_loss"
```

Append to `tests/test_telegram_notify.py` (add `import positions` to its imports if it's missing):

```python
def test_caution_close_alert_reads_as_such():
    pos = positions.Position(1, "CRYPTO:BTC", "CRYPTO", "2026-09-20", 84_500.0, [], None,
                             None, None, None)
    alert = positions.CloseAlert(pos, "caution", "отток из спот-ETF (€900,000,000); цена "
                                 "подтверждает: -6.2% за 7 дн., ниже 20-дн. средней", 79_900.0)
    text = telegram_notify.format_close_alert(alert, html=False)
    assert "CRYPTO:BTC — сигнал осторожности" in text and "отток из спот-ETF" in text
```

- [ ] **Step 2: Run them to verify they fail**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_positions.py tests/test_telegram_notify.py`
Expected: FAIL with `TypeError: check_exits() got an unexpected keyword argument 'trend_fn'`.

- [ ] **Step 3: Implement.** In `positions.py`, replace the module docstring's list of triggers with:

```python
"""Positions the user reports buying (/bought, /sold in telegram_bot.py), and when
to close them.

Only reported positions are tracked, at the user's own entry price -- the bot never
reads or trades the brokerage account. A close alert fires once per position, on the
first of:

  insider_sell  one of the insiders behind the "Сильный" signal it came from sells
                after the open date -- Form 4 (not a 10b5-1 planned sale), a Form 144
                notice of intent, or a sale row from Oslo, FI or BaFin;
  caution       (coins) a caution signal on the coin -- ETF outflows, a company
                selling, coins moving onto exchanges -- journaled in the last
                CAUTION_LOOKBACK_DAYS and not before the open date, and the price
                confirms it: down over 7 days and below the 20-day average;
  time          EXIT_MAX_DAYS held -- the horizon insider-buying research looks at;
  stop_loss     the last close is EXIT_STOP_LOSS_PCT (stocks) or
                EXIT_STOP_LOSS_PCT_CRYPTO (coins) or more below the entry.

The alert doesn't close the position; /sold does. The user stays in control.
"""
```

Replace the two exit constants with:

```python
EXIT_MAX_DAYS = 90
EXIT_STOP_LOSS_PCT = 15.0
# Coins swing far more than stocks: 15% is an ordinary month for bitcoin. A fixed 25%
# until the volatility-scaled stop (the bottom of the coin's usual monthly range)
# ships with the range calculation.
EXIT_STOP_LOSS_PCT_CRYPTO = 25.0
CAUTION_LOOKBACK_DAYS = 7
_CAUTION_TEXT = {"etf_flow": "отток из спот-ETF", "treasury": "компания продала монеты",
                 "exchange_flow": "монеты заводят на биржи"}
```

Add before `check_exits`:

```python
def _crypto_caution(conn, pos: Position, today: dt.date, trend_fn) -> str | None:
    """The newest caution signal on this coin from the last CAUTION_LOOKBACK_DAYS, not
    before the position opened, when the price confirms it today -- else None."""
    if not crypto.is_crypto(pos.ticker):
        return None
    since = max(pos.opened_at, (today - dt.timedelta(days=CAUTION_LOOKBACK_DAYS)).isoformat())
    row = conn.execute(
        "SELECT kind, total_value_eur FROM signal_journal WHERE ticker = ? AND tier = 'caution' "
        "AND date(emitted_at) >= ? AND date(emitted_at) <= ? ORDER BY emitted_at DESC, id DESC LIMIT 1",
        (pos.ticker, since, today.isoformat())).fetchone()
    if row is None:
        return None
    trend = trend_fn(conn, crypto.symbol_of(pos.ticker))
    if not crypto.trend_confirms_down(trend):
        return None
    kind, value = row
    what = _CAUTION_TEXT.get(kind, "сигнал осторожности") + (f" (€{value:,.0f})" if value else "")
    return (f"{what}; цена подтверждает: {trend['ret_7d']:+.1f}% за 7 дн., "
            f"ниже 20-дн. средней")
```

Replace `check_exits` with:

```python
def check_exits(conn, today: dt.date | None = None, price_fn=None,
                trend_fn=None) -> list[CloseAlert]:
    today = today or dt.date.today()
    price_fn = price_fn or last_close
    trend_fn = trend_fn or crypto.price_trend
    alerts = []
    for pos in open_positions(conn):
        if pos.close_alerted_at:
            continue
        sale = _insider_sale(conn, pos)
        price = price_fn(pos.ticker, pos.source)
        if sale:
            alerts.append(CloseAlert(pos, "insider_sell", sale, price))
            continue
        caution = _crypto_caution(conn, pos, today, trend_fn)
        if caution:
            alerts.append(CloseAlert(pos, "caution", caution, price))
            continue
        held = (today - dt.date.fromisoformat(pos.opened_at)).days
        if held >= EXIT_MAX_DAYS:
            alerts.append(CloseAlert(pos, "time", f"{held} дн. в позиции", price))
            continue
        if price is None:
            print(f"[positions] no price for {pos.ticker}; stop-loss check skipped today")
            continue
        stop = EXIT_STOP_LOSS_PCT_CRYPTO if crypto.is_crypto(pos.ticker) else EXIT_STOP_LOSS_PCT
        if price <= pos.entry_price * (1 - stop / 100):
            change = (price / pos.entry_price - 1) * 100
            alerts.append(CloseAlert(pos, "stop_loss", f"{change:+.1f}% от входа", price))
    return alerts
```

Also update the `CloseAlert.trigger` comment to `# insider_sell / caution / time / stop_loss`.

In `telegram_notify.py`, replace `_CLOSE_REASON` with:

```python
_CLOSE_REASON = {"insider_sell": "инсайдеры продают", "caution": "сигнал осторожности",
                 "time": "срок вышел", "stop_loss": "стоп-лосс"}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_positions.py tests/test_telegram_notify.py`
Expected: all pass. The existing stock tests (`test_stop_loss_closes` at 15% and the rest) are unchanged.

- [ ] **Step 5: Update the README.** In `README.md`'s "## Крипто" section:

- In the "**Компании (treasury)**" bullet, replace the final sentence `Сигнал — покупка или продажа от €500k.` with:

```markdown
  Покупки складываются в недельную сумму по монете (с понедельника): сигнал —
  когда неделя больше, чем 90% недель за прошлый год, и не меньше €50 млн;
  компания, купившая монету впервые, помечена «(впервые)». Рутинные еженедельные
  покупки Strategy сигналом больше не считаются. Продажа от €10 млн — сигнал
  осторожности. Год истории для «нормы» подгружается один раз:
  `python crypto_treasury.py --backfill 365`.
```

- Replace the whole "**Спот-ETF**" bullet with:

```markdown
- **Спот-ETF** — [crypto_etf.py](crypto_etf.py). Дневные потоки всех американских
  спот-фондов (12 на биткоин, 11 на эфир) — с таблиц Farside; при первом запуске
  подгружается вся история с запуска фондов. «Необычно» считается относительно
  последних 126 торговых дней: день — в верхних 10% и от $100 млн, серия от 3 дней
  в одну сторону — в верхних 10% трёхдневных сумм и от $250 млн. Пока истории
  меньше 30 дней — прежние пороги $400/$500 млн. Если Farside молчит больше 3
  рабочих дней, берутся страницы BlackRock (IBIT и ETHA: изменение числа паёв ×
  NAV), и сигнал пишет «только IBIT/ETHA». Отток — сигнал осторожности.
```

- After the "**Кошельки бирж**" bullet (before the "Балл у крипто-сигнала" paragraph), add:

```markdown
**Сигналы осторожности** — отток из ETF, продажа монет компанией, завод монет на
биржи. В Telegram они отдельно не приходят: их видно в меню «Сигналы» (раздел
«⚠️ Осторожно», за 3 дня, с проверкой цены), они записываются в журнал сигналов,
а по монете, которая у вас в позиции, дают «Закрыть» (см. «Позиции» ниже).
```

- In "### Позиции", replace the sentence that starts `«Закрыть» приходит один раз, по первому из:` and ends `цена на 15% ниже входа.` with:

```markdown
«Закрыть» приходит один раз, по первому из: продаёт кто-то из инсайдеров, давших
сильный сигнал (Form 4 не по плану 10b5-1, Form 144, продажи в Осло/FI/BaFin); для
монеты — сигнал осторожности за последние 7 дней (не раньше открытия позиции),
подтверждённый ценой (минус за 7 дней и ниже 20-дневной средней); прошло 90 дней;
цена ниже входа на 15% (акции) или на 25% (монеты — пока нет стопа по
волатильности монеты).
```

- [ ] **Step 6: Run the full suite, then commit**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider`
Expected: all pass.

```bash
git add positions.py telegram_notify.py README.md tests/test_positions.py tests/test_telegram_notify.py
git commit -m "feat(positions): close a coin on a price-confirmed caution; 25% coin stop-loss

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## After the plan (manual, needs the user's go-ahead)

1. Merge and push.
2. Run `python crypto_treasury.py --backfill 365` once (it takes a while; it can be resumed).
3. Restart the Telegram bot, so the menu and the daily run pick up the changes.
4. The first daily run loads Farside's full history automatically.
