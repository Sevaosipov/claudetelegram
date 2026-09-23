# Signal Strategy v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace "every signal scoring ≥ 35" with two explicit tiers ("Сильный" /
"Кандидат") of recent, Trading 212-buyable buy signals, plus "Закрыть" alerts for
positions the user reports, in both the daily Telegram message and the menu.

**Architecture:** The finders gain structured buyer roles (`cluster/roles.py`).
A new `strategy.py` works in three steps:
1. Prefilters buy-side signals by disclosure recency and Trading 212 availability.
2. Enriches the survivors (market cap, liquidity).
3. Applies the tier rules. For crypto this includes a 7-day / 20-day-average price
   check (`crypto.price_trend`).

A new `positions.py` stores what the user reports via Telegram (`/bought`, `/sold`,
`/positions`) and raises close alerts (insider sale, 90 days, −15%). `bot.py` and
`menu.py` both render `strategy.Selection` + close alerts through one formatter in
`telegram_notify.py`.

**Tech Stack:** Python 3.12, SQLite (`db.py`), yfinance, pytest (offline).
Run tests with `.venv/bin/python -m pytest -q -p no:cacheprovider`.

**Spec:** `docs/superpowers/specs/2026-09-23-signal-strategy-v2-design.md`

## Global Constraints

- Buy-side only: exit signals and bearish crypto never appear as Сильный/Кандидат.
- Floors: on Trading 212; disclosed within 3 days (`cluster.disclosed_on`); market
  cap ≥ €300m and average daily value ≥ €1m. Unknown size → at most Кандидат,
  labelled "размер неизвестен". Size floors don't apply to `CRYPTO:` tickers.
- Сильный (stocks): ≥ 1 insider-role buyer and one of:
  - (a) ≥ 3 insider-role buyers;
  - (b) ≥ 2 insider-role buyers incl. CEO/CFO/Chair;
  - (c) CEO/CFO buying ≥ €250k with ≥ 10% position increase.

  Size must also be known and above the floors.
- Кандидат: all other survivors, top 10 by score.
- Crypto: bullish only; treasury needs ≥ €50m. Сильный if 7-day return > 0 and last
  close > 20-day average; otherwise Кандидат ("цена не подтверждает" / "цена не
  проверена").
- Close alerts (only for `/bought` positions), on the first of:
  - an insider sale after the open date (Form 4 non-10b5-1, Form 144, or FI/Oslo/BaFin
    `S` rows; names compared with `cluster.name_key`);
  - 90 days held;
  - last close ≤ −15% from the entry.

  One alert per position.
- The bot never reads or trades the Trading 212 account.
- The test suite stays offline: stub `cluster.enrich_signals`, price functions and
  `telegram_notify.send_text` in tests.
- Code and comments in English; user-facing strings in Russian, matching existing
  style. Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

---

### Task 1: Structured buyer roles on cluster signals

**Files:**
- Create: `cluster/roles.py`
- Modify: `cluster/common.py` (`ClusterSignal`: new field `buyers`)
- Modify: `cluster/buys.py` (SEC, House/Senate, BaFin, Norway, Sweden finders
  populate `buyers`)
- Modify: `cluster/__init__.py` (re-export)
- Test: `tests/test_roles.py`

**Interfaces:**
- Produces:
  - `cluster.roles.Buyer(name: str, role: str, total_eur: float, increase_pct: float | None = None)`;
  - `INSIDER_ROLES = {"ceo","cfo","chair","officer","director","insider"}`;
  - `TOP_EXEC_ROLES = {"ceo","cfo","chair"}`;
  - `sec_role(title, is_officer, is_director, is_ten_pct) -> str`;
  - `bafin_role(position) -> str`;
  - `sweden_role(position, related_party) -> str`;
  - `ClusterSignal.buyers: list[Buyer]` (default `[]`).

- [ ] **Step 1: Write the failing tests**

`tests/test_roles.py`:
```python
"""Normalised buyer roles (cluster/roles.py) and the finders that attach them."""
from __future__ import annotations

import datetime as dt

import pytest

import cluster
from cluster.roles import bafin_role, sec_role, sweden_role
from conftest import add_bafin_txn, add_house_txn, add_sec_purchase, add_sweden_txn

TODAY = dt.date.today()
RECENT = (TODAY - dt.timedelta(days=2)).isoformat()


@pytest.mark.parametrize("title,officer,director,ten_pct,role", [
    ("Chief Executive Officer", 1, 0, 0, "ceo"),
    ("President and CEO", 1, 0, 0, "ceo"),
    ("Chairman and CEO", 1, 1, 0, "ceo"),          # CEO outranks chair
    ("Chief Financial Officer", 1, 0, 0, "cfo"),
    ("EVP & CFO", 1, 0, 0, "cfo"),
    ("Executive Chairman", 1, 1, 0, "chair"),
    ("See Remarks", 1, 0, 0, "officer"),
    (None, 0, 1, 0, "director"),
    (None, 0, 0, 1, "holder"),
    (None, 0, 0, 0, "other"),
])
def test_sec_role(title, officer, director, ten_pct, role):
    assert sec_role(title, officer, director, ten_pct) == role


@pytest.mark.parametrize("position,role", [
    ("Vorstand", "officer"), ("Vorsitzender des Vorstands", "ceo"),
    ("Aufsichtsrat", "director"), ("Vorsitzender des Aufsichtsrats", "chair"),
    ("in enger Beziehung", "associate"), ("Sonstige Führungsperson", "officer"),
    (None, "officer"),
])
def test_bafin_role(position, role):
    assert bafin_role(position) == role


@pytest.mark.parametrize("position,related,role", [
    ("Verkställande direktör (VD)", False, "ceo"), ("VD", False, "ceo"),
    ("Vice VD", False, "officer"),
    ("Ekonomichef/finanschef/finansdirektör", False, "cfo"),
    ("Styrelseordförande", False, "chair"), ("Styrelseledamot", False, "director"),
    ("Annan ledande befattningshavare", False, "officer"),
    ("Verkställande direktör (VD)", True, "associate"),
])
def test_sweden_role(position, related, role):
    assert sweden_role(position, related) == role


def test_sec_finder_attaches_roles_totals_and_increase(conn):
    add_sec_purchase(conn, "AAA", "Boss", 580_000, RECENT, officer=1, director=0,
                     title="Chief Executive Officer", shares=100, shares_owned_after=500)
    add_sec_purchase(conn, "AAA", "Board", 116_000, RECENT)
    [sig] = cluster.find_sec_clusters(conn)
    by_name = {b.name: b for b in sig.buyers}
    assert by_name["Boss"].role == "ceo" and by_name["Board"].role == "director"
    assert by_name["Boss"].total_eur == pytest.approx(500_000)      # USD at 1.16
    assert by_name["Boss"].increase_pct == pytest.approx(25.0)      # 100 on top of 400


def test_house_buyers_are_other(conn):
    date = (TODAY - dt.timedelta(days=3)).strftime("%m/%d/%Y")
    for m in ("Member One", "Member Two"):
        add_house_txn(conn, "AAA", m, "$250,001 - $500,000", date=date)
    [sig] = cluster.find_house_clusters(conn)
    assert {b.role for b in sig.buyers} == {"other"}


def test_bafin_and_sweden_finders_attach_roles(conn):
    d = (TODAY - dt.timedelta(days=2))
    add_bafin_txn(conn, "DE0000000001", "Chef", 600_000, date=d.strftime("%d.%m.%Y"),
                  position="Vorstand")
    add_sweden_txn(conn, "SE0000000001", "Vd Person", 7_000_000, date=d.isoformat())
    [bafin] = cluster.find_bafin_clusters(conn)
    [swe] = cluster.find_sweden_clusters(conn)
    assert bafin.buyers[0].role == "officer" and swe.buyers[0].role == "ceo"


def test_norway_buyers_are_insiders(conn):
    for i, person in enumerate(("A Person", "B Person")):
        conn.execute(
            "INSERT INTO norway_purchases (message_id, person, issuer_name, ticker, txn_type, "
            "txn_date, shares, price, currency, value, source_url) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (100 + i, person, "Test ASA", "TST", "P", RECENT, 1000, 100.0, "NOK", 2_000_000, "u"))
    conn.commit()
    [sig] = cluster.find_norway_clusters(conn)
    assert {b.role for b in sig.buyers} == {"insider"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_roles.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'cluster.roles'`.

- [ ] **Step 3: Create `cluster/roles.py`**

```python
"""Normalised buyer roles, so the strategy can tell a CEO from a board member from a
fund without re-parsing display strings.

Every source writes roles its own way -- SEC free-text officer titles ("President &
CEO", "See Remarks"), BaFin's German categories, Finansinspektionen's Swedish ones,
Oslo none at all -- and the tier rules in strategy.py need one vocabulary:

    ceo, cfo, chair            the top executives rule (b)/(c) look for
    officer, director          other people who run the company
    insider                    an insider of unstated rank (Oslo)
    holder                     a >10% holder with no other role (SEC)
    associate                  a closely associated person / related party
    other                      not a company insider at all (Congress)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

INSIDER_ROLES = {"ceo", "cfo", "chair", "officer", "director", "insider"}
TOP_EXEC_ROLES = {"ceo", "cfo", "chair"}

_CEO_RE = re.compile(r"chief executive|\bceo\b", re.IGNORECASE)
_CFO_RE = re.compile(r"chief financial|\bcfo\b", re.IGNORECASE)
_CHAIR_RE = re.compile(r"\bchair(man|woman|person)?\b", re.IGNORECASE)
_VD_RE = re.compile(r"\bvd\b|verkställande direktör", re.IGNORECASE)


@dataclass
class Buyer:
    name: str
    role: str
    total_eur: float
    increase_pct: float | None = None   # this buyer's buy vs. what they held (SEC only)


def sec_role(title: str | None, is_officer, is_director, is_ten_pct) -> str:
    """CEO outranks chair ("Chairman and CEO" is a CEO); the relationship flags
    decide when the title says nothing specific ("See Remarks", blank)."""
    t = title or ""
    if _CEO_RE.search(t):
        return "ceo"
    if _CFO_RE.search(t):
        return "cfo"
    if _CHAIR_RE.search(t):
        return "chair"
    if is_officer:
        return "officer"
    if is_director:
        return "director"
    if is_ten_pct:
        return "holder"
    return "other"


def bafin_role(position: str | None) -> str:
    p = (position or "").lower()
    if "enger beziehung" in p:
        return "associate"
    if "vorstand" in p:
        return "ceo" if "vorsitz" in p else "officer"
    if "aufsichtsrat" in p:
        return "chair" if "vorsitz" in p else "director"
    return "officer"   # "Sonstige Führungsperson" and anything unlabelled


def sweden_role(position: str | None, related_party) -> str:
    if related_party:
        return "associate"
    p = position or ""
    low = p.lower()
    if "vice vd" not in low and _VD_RE.search(p):
        return "ceo"
    if any(w in low for w in ("finanschef", "finansdirektör", "ekonomichef", "cfo")):
        return "cfo"
    if "styrelseordförande" in low:
        return "chair"
    if "styrelseledamot" in low:
        return "director"
    return "officer"
```

- [ ] **Step 4: Add the field to `ClusterSignal`** (`cluster/common.py`, end of the dataclass, after `corroborated_by`)

```python
    # One entry per distinct buyer with a normalised role (cluster/roles.py), so the
    # tier rules in strategy.py can ask "is the CEO in this" without parsing
    # `members`' display strings.
    buyers: list = field(default_factory=list)
```

- [ ] **Step 5: Populate `buyers` in the finders** (`cluster/buys.py`)

Add to the imports: `from .roles import Buyer, bafin_role, sec_role, sweden_role`.

SEC (`find_sec_clusters`): in the `signals.append(ClusterSignal(...))` call, add:
```python
            buyers=[Buyer(name, sec_role(o["title"], o["is_officer"], o["is_director"],
                                         o["is_ten_pct"]),
                          o["total"], _position_increase(o))
                    for name, o in by_owner.items()],
```

House/Senate (`find_house_clusters`): add
`buyers=[Buyer(name, "other", m["total"]) for name, m in by_member.items()],`.

BaFin (`find_bafin_clusters`): add
`buyers=[Buyer(name, bafin_role(o["position"]), o["total"]) for name, o in by_notifier.items()],`.

Norway (`find_norway_clusters`): add
`buyers=[Buyer(name, "insider", o["total"]) for name, o in by_person.items()],`.

Sweden (`find_sweden_clusters`):
- add `related_party` as the last selected column:
  `"""SELECT isin, issuer_name, person, position, txn_date, value, currency, related_party`;
- change the loop to
  `for _, issuer_name, person, position, txn_date, value, currency, related in group:`;
- store it in the slot:
  `{"issuer_name": issuer_name, "position": position, "total": 0.0, "related": related}`;
- add to the constructor:
  `buyers=[Buyer(name, sweden_role(o["position"], o["related"]), o["total"]) for name, o in by_person.items()],`.

`dates = [r[4] for r in group]` stays correct, because `txn_date` is still index 4.

- [ ] **Step 6: Re-export** (`cluster/__init__.py`, append)

```python
from .roles import (  # noqa: F401
    INSIDER_ROLES,
    TOP_EXEC_ROLES,
    Buyer,
    bafin_role,
    sec_role,
    sweden_role,
)
```

- [ ] **Step 7: Run the tests**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider`
Expected: all pass (the existing 455 plus the new role tests).

- [ ] **Step 8: Commit**

```bash
git add cluster/roles.py cluster/common.py cluster/buys.py cluster/__init__.py tests/test_roles.py
git commit -m "feat(cluster): structured buyer roles on cluster signals

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Crypto price trend

**Files:**
- Modify: `crypto.py`
- Test: `tests/test_crypto.py` (append)

**Interfaces:**
- Produces:
  - `crypto.price_trend(conn, symbol: str) -> dict | None`, returning
    `{"ret_7d": float (percent), "above_ma20": bool}`;
  - `crypto.trend_confirms(trend: dict | None) -> bool`;
  - `crypto._daily_closes(symbol) -> list[float] | None`, the network seam that tests
    patch.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_crypto.py`)

```python
# --------------------------------------------------------------- price trend
def _closes(monkeypatch, series):
    calls = []
    monkeypatch.setattr(crypto, "_daily_closes", lambda sym: calls.append(sym) or series)
    return calls


def test_rising_price_above_its_average_confirms(conn, monkeypatch):
    _closes(monkeypatch, [100.0] * 20 + [101, 102, 103, 104, 105, 106, 107, 110])
    trend = crypto.price_trend(conn, "BTC")
    assert trend["ret_7d"] == pytest.approx((110 / 101 - 1) * 100)
    assert trend["above_ma20"] and crypto.trend_confirms(trend)


def test_falling_price_does_not_confirm(conn, monkeypatch):
    _closes(monkeypatch, [100.0] * 20 + [99, 98, 97, 96, 95, 94, 93, 92])
    assert not crypto.trend_confirms(crypto.price_trend(conn, "BTC"))


def test_trend_is_cached_between_calls(conn, monkeypatch):
    calls = _closes(monkeypatch, [100.0] * 28)
    crypto.price_trend(conn, "ETH")
    crypto.price_trend(conn, "ETH")
    assert calls == ["ETH"]


def test_no_price_history_means_no_trend(conn, monkeypatch):
    _closes(monkeypatch, None)
    assert crypto.price_trend(conn, "BTC") is None and not crypto.trend_confirms(None)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_crypto.py -k trend`
Expected: FAIL with `AttributeError: module 'crypto' has no attribute '_daily_closes'`.

- [ ] **Step 3: Implement** (append to `crypto.py`)

```python
TREND_TTL_SECONDS = 12 * 3600


def _daily_closes(symbol: str) -> list[float] | None:
    """Daily USD closes, oldest first, or None. Crypto trades every day, so seven
    closes back is seven calendar days back."""
    try:
        import yfinance as yf
        hist = yf.Ticker(f"{symbol.upper()}-USD").history(period="3mo")["Close"].dropna()
    except Exception:
        return None
    closes = [float(x) for x in hist]
    return closes if len(closes) >= 21 else None


def price_trend(conn, symbol: str) -> dict | None:
    """The 7-day return (percent) and whether the last close is above the 20-day
    average -- the check that turns a big crypto inflow into a "Сильный" signal (see
    strategy.py). Cached for half a day; None when there's no price history."""
    symbol = symbol.upper()
    k_ret, k_above = f"crypto_trend_ret7_{symbol}", f"crypto_trend_above20_{symbol}"
    ret = db.get_cached_value(conn, k_ret, TREND_TTL_SECONDS)
    above = db.get_cached_value(conn, k_above, TREND_TTL_SECONDS)
    if ret is not None and above is not None:
        return {"ret_7d": ret, "above_ma20": bool(above)}
    closes = _daily_closes(symbol)
    if not closes or len(closes) < 21:
        return None
    last = closes[-1]
    ret = (last / closes[-8] - 1) * 100
    above = last > sum(closes[-20:]) / 20
    db.save_cached_value(conn, k_ret, ret)
    db.save_cached_value(conn, k_above, 1.0 if above else 0.0)
    return {"ret_7d": ret, "above_ma20": above}


def trend_confirms(trend: dict | None) -> bool:
    return bool(trend) and trend["ret_7d"] > 0 and trend["above_ma20"]
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_crypto.py`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add crypto.py tests/test_crypto.py
git commit -m "feat(crypto): 7-day / 20-day-average price trend check

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: `strategy.py`: floors, tiers, selection

**Files:**
- Create: `strategy.py`
- Test: `tests/test_strategy.py`

**Interfaces:**
- Consumes (from Tasks 1–2):
  - `cluster.roles.INSIDER_ROLES` and `TOP_EXEC_ROLES`;
  - `ClusterSignal.buyers`;
  - `crypto.price_trend` and `crypto.trend_confirms`;
  - `cluster.disclosed_on(conn, sig)`;
  - `trading212.Availability.can_buy(ticker, source)`;
  - `cluster.enrich_signals(conn, signals)`, which sets `market_cap_eur`,
    `avg_daily_value` and `score`, returns the signals sorted by score descending,
    and fills in `total_value` for crypto signals that only have `units`.
- Produces:
  - `strategy.STRONG = "strong"`, `strategy.CANDIDATE = "candidate"`;
  - `strategy.Tiered(signal, tier: str, met: list[str], missed: list[str])`;
  - `strategy.Selection(strong: list[Tiered], candidates: list[Tiered], t212_checked: bool)`;
  - `strategy.select(conn, signals: list, t212, today: dt.date | None = None) -> Selection`,
    which also sets `signal.tier` on every kept signal;
  - `strategy.is_buy_side(sig) -> bool`;
  - the constants `MAX_AGE_DAYS`, `FLOOR_MIN_MCAP_EUR`, `FLOOR_MIN_ADV_EUR`,
    `STRONG_MIN_INSIDERS`, `TOP_EXEC_MIN_INSIDERS`, `CONVICTION_MIN_EUR`,
    `CONVICTION_MIN_INCREASE_PCT`, `MAX_CANDIDATES`, `CRYPTO_TREASURY_BIG_EUR`.

- [ ] **Step 1: Write the failing tests** (`tests/test_strategy.py`)

```python
"""strategy.py: which signals are Сильный, which Кандидат, which don't make it.
Offline: enrich_signals is replaced by `sized`, price trends by a stub."""
from __future__ import annotations

import datetime as dt

import pytest

import cluster
import crypto
import strategy
from conftest import add_house_txn, add_sec_purchase, add_stake

TODAY = dt.date.today()
RECENT = (TODAY - dt.timedelta(days=1)).isoformat()
BIG, LIQUID = 5e9, 50e6


class _T212:
    def __init__(self, missing=()):
        self.missing = set(missing)

    def can_buy(self, ticker, source):
        return ticker not in self.missing


@pytest.fixture
def sized(monkeypatch):
    """Stand-in for enrich_signals: every stock gets the size set here, every signal
    a score equal to its position (so ordering is deterministic)."""
    state = {"cap": BIG, "adv": LIQUID}

    def fake(conn, signals):
        for i, s in enumerate(signals):
            if not getattr(s, "crypto_kind", None):
                s.market_cap_eur, s.avg_daily_value = state["cap"], state["adv"]
            s.score = 100.0 - i
        return signals
    monkeypatch.setattr(cluster, "enrich_signals", fake)
    return state


def _buy(conn, ticker, owner, usd=116_000, **kw):
    add_sec_purchase(conn, ticker, owner, usd, RECENT, filed_date=RECENT, **kw)


def _select(conn, t212=None):
    return strategy.select(conn, cluster.find_sec_clusters(conn) + cluster.find_house_clusters(conn)
                           + cluster.find_stake_signals(conn), t212 or _T212())


def _tiers(sel):
    return ({t.signal.ticker for t in sel.strong}, {t.signal.ticker for t in sel.candidates})


# ---------------------------------------------------------------- stock rules
def test_rule_a_three_insiders_is_strong(conn, sized):
    for o in ("A", "B", "C"):
        _buy(conn, "AAA", o)
    strong, _ = _tiers(_select(conn))
    assert strong == {"AAA"}


def test_rule_b_two_insiders_with_the_ceo_is_strong(conn, sized):
    _buy(conn, "AAA", "Boss", officer=1, director=0, title="Chief Executive Officer")
    _buy(conn, "AAA", "Board")
    [t] = _select(conn).strong
    assert any("CEO" in m for m in t.met)


def test_two_directors_without_a_top_exec_is_a_candidate(conn, sized):
    _buy(conn, "AAA", "Board One")
    _buy(conn, "AAA", "Board Two")
    sel = _select(conn)
    assert _tiers(sel) == (set(), {"AAA"}) and sel.candidates[0].missed


def test_rule_c_ceo_conviction_buy_is_strong(conn, sized):
    _buy(conn, "AAA", "Boss", usd=700_000, officer=1, director=0,
         title="Chief Executive Officer", shares=100, shares_owned_after=500)
    assert _tiers(_select(conn))[0] == {"AAA"}


def test_small_ceo_buy_is_only_a_candidate(conn, sized):
    _buy(conn, "AAA", "Boss", usd=700_000, officer=1, director=0,
         title="Chief Executive Officer", shares=100, shares_owned_after=100_000)   # +0.1%
    assert _tiers(_select(conn)) == (set(), {"AAA"})


def test_holders_only_is_a_candidate(conn, sized):
    for o in ("Fund A", "Fund B", "Fund C"):
        _buy(conn, "AAA", o, usd=700_000, director=0, ten_pct=1)
    assert _tiers(_select(conn)) == (set(), {"AAA"})


def test_congress_is_candidate_only(conn, sized):
    date = (TODAY - dt.timedelta(days=20)).strftime("%m/%d/%Y")
    for m in ("One", "Two", "Three"):
        add_house_txn(conn, "AAA", m, "$250,001 - $500,000", date=date)
    assert _tiers(_select(conn)) == (set(), {"AAA"})


def test_activist_stake_is_candidate_only(conn, sized):
    add_stake(conn, "AAA", "Activist", 12.0)
    assert _tiers(_select(conn)) == (set(), {"AAA"})


# ---------------------------------------------------------------------- floors
def test_below_the_size_floor_is_dropped(conn, sized):
    sized["cap"] = 100e6
    for o in ("A", "B", "C"):
        _buy(conn, "AAA", o)
    assert _tiers(_select(conn)) == (set(), set())


def test_illiquid_is_dropped(conn, sized):
    sized["adv"] = 200_000
    for o in ("A", "B", "C"):
        _buy(conn, "AAA", o)
    assert _tiers(_select(conn)) == (set(), set())


def test_unknown_size_caps_at_candidate(conn, sized):
    sized["cap"] = None
    for o in ("A", "B", "C"):
        _buy(conn, "AAA", o)
    [t] = _select(conn).candidates
    assert "размер неизвестен" in t.missed


def test_old_disclosure_is_dropped(conn, sized):
    old = (TODAY - dt.timedelta(days=6)).isoformat()
    for o in ("A", "B", "C"):
        add_sec_purchase(conn, "AAA", o, 116_000, old, filed_date=old)
    assert _tiers(_select(conn)) == (set(), set())


def test_not_on_trading_212_is_dropped(conn, sized):
    for o in ("A", "B", "C"):
        _buy(conn, "AAA", o)
    assert _tiers(_select(conn, _T212(missing={"AAA"}))) == (set(), set())


def test_without_trading_212_nothing_is_filtered_and_it_says_so(conn, sized):
    for o in ("A", "B", "C"):
        _buy(conn, "AAA", o)
    sel = strategy.select(conn, cluster.find_sec_clusters(conn), None)
    assert not sel.t212_checked and _tiers(sel)[0] == {"AAA"}


def test_candidates_are_capped(conn, sized):
    for i in range(15):
        _buy(conn, f"T{i:02d}", "Board One")
        _buy(conn, f"T{i:02d}", "Board Two")
    assert len(_select(conn).candidates) == strategy.MAX_CANDIDATES


def test_selected_signals_carry_their_tier(conn, sized):
    for o in ("A", "B", "C"):
        _buy(conn, "AAA", o)
    [t] = _select(conn).strong
    assert t.signal.tier == strategy.STRONG


# ---------------------------------------------------------------------- crypto
def _etf(value_eur=5e8, bullish=True):
    return cluster.CryptoSignal("CRYPTO_ETF", "etf_flow", "CRYPTO:BTC", "спот-ETF: IBIT", bullish,
                                None, value_eur, RECENT, RECENT, [], None, ["k"])


def _trend(monkeypatch, trend):
    monkeypatch.setattr(crypto, "price_trend", lambda conn, sym: trend)


def test_crypto_inflow_with_confirming_price_is_strong(conn, sized, monkeypatch):
    _trend(monkeypatch, {"ret_7d": 4.0, "above_ma20": True})
    assert len(strategy.select(conn, [_etf()], _T212()).strong) == 1


def test_crypto_inflow_without_confirmation_is_a_candidate(conn, sized, monkeypatch):
    _trend(monkeypatch, {"ret_7d": -2.0, "above_ma20": False})
    [t] = strategy.select(conn, [_etf()], _T212()).candidates
    assert any("не подтверждает" in m for m in t.missed)


def test_crypto_without_a_price_check_is_a_candidate(conn, sized, monkeypatch):
    _trend(monkeypatch, None)
    [t] = strategy.select(conn, [_etf()], _T212()).candidates
    assert "цена не проверена" in t.missed


def test_crypto_outflow_is_never_listed(conn, sized, monkeypatch):
    _trend(monkeypatch, {"ret_7d": 4.0, "above_ma20": True})
    sel = strategy.select(conn, [_etf(bullish=False)], _T212())
    assert not sel.strong and not sel.candidates


def test_small_treasury_buy_is_not_listed(conn, sized, monkeypatch):
    _trend(monkeypatch, {"ret_7d": 4.0, "above_ma20": True})
    small = cluster.CryptoSignal("CRYPTO_TREASURY", "treasury", "CRYPTO:BTC", "Acme", True,
                                 100, 8e6, RECENT, RECENT, [], None, ["k"])
    sel = strategy.select(conn, [small], _T212())
    assert not sel.strong and not sel.candidates


def test_exit_signals_are_never_listed(conn, sized):
    exit_sig = cluster.ExitSignal(source="SEC", ticker="AAA", company="C", total_buyers=2,
                                  seller_count=2, lines=[], seller_names=["A", "B"])
    sel = strategy.select(conn, [exit_sig], _T212())
    assert not sel.strong and not sel.candidates
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_strategy.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'strategy'`.

- [ ] **Step 3: Implement `strategy.py`**

```python
"""Signal strategy v2: which buy signals are "Сильный" (act the same day), which are
"Кандидат" (consider within days), and which don't make the list at all.

Spec: docs/superpowers/specs/2026-09-23-signal-strategy-v2-design.md. The rules are
explicit on purpose -- every kept signal carries the rules it met and missed, so an
alert can say *why* it is strong, and a rule can be retuned without re-deriving a
score. The numbers are starting points, checked by calibrate_strategy.py against
stored history, not findings.

Order of work in select(): cheap filters first (buy side, disclosed recently, on
Trading 212), then enrich_signals -- the step that reaches the network for market
cap and liquidity -- only for what survived, then the tier rules.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import cluster
import crypto
from cluster.roles import INSIDER_ROLES, TOP_EXEC_ROLES

MAX_AGE_DAYS = 3                    # by disclosure date (cluster/recency.py)
FLOOR_MIN_MCAP_EUR = 300e6
FLOOR_MIN_ADV_EUR = 1e6
STRONG_MIN_INSIDERS = 3             # rule (a)
TOP_EXEC_MIN_INSIDERS = 2           # rule (b)
CONVICTION_MIN_EUR = 250_000        # rule (c)
CONVICTION_MIN_INCREASE_PCT = 10.0  # rule (c)
MAX_CANDIDATES = 10
CRYPTO_TREASURY_BIG_EUR = 50e6

STRONG, CANDIDATE = "strong", "candidate"
_ROLE_LABEL = {"ceo": "CEO", "cfo": "CFO", "chair": "Chair"}


@dataclass
class Tiered:
    signal: object
    tier: str
    met: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)


@dataclass
class Selection:
    strong: list[Tiered]
    candidates: list[Tiered]
    t212_checked: bool


def _short(v: float) -> str:
    for unit, suffix in ((1e9, "млрд"), (1e6, "млн"), (1e3, "тыс")):
        if abs(v) >= unit:
            return f"{v / unit:,.1f} {suffix}"
    return f"{v:,.0f}"


def is_buy_side(sig) -> bool:
    if hasattr(sig, "seller_count"):          # ExitSignal
        return False
    if hasattr(sig, "crypto_kind"):           # CryptoSignal
        return bool(sig.bullish)
    return True


def _stock_tier(sig) -> Tiered | None:
    met, missed = [], []
    is_coin = crypto.is_crypto(sig.ticker)    # a congressional crypto buy
    size_known = True
    if not is_coin:
        cap, adv = getattr(sig, "market_cap_eur", None), getattr(sig, "avg_daily_value", None)
        if (cap is not None and cap < FLOOR_MIN_MCAP_EUR) or \
                (adv is not None and adv < FLOOR_MIN_ADV_EUR):
            return None
        size_known = cap is not None and adv is not None
        if size_known:
            met.append(f"€{_short(cap)} / €{_short(adv)} в день")
        else:
            missed.append("размер неизвестен")

    buyers = getattr(sig, "buyers", None) or []
    insiders = [b for b in buyers if b.role in INSIDER_ROLES]
    tops = [b for b in insiders if b.role in TOP_EXEC_ROLES]
    rules = []
    if len(insiders) >= STRONG_MIN_INSIDERS:
        rules.append(f"{len(insiders)} инсайдера(ов) из руководства")
    if len(insiders) >= TOP_EXEC_MIN_INSIDERS and tops:
        rules.append(f"{_ROLE_LABEL[tops[0].role]} среди покупателей")
    for b in insiders:
        if (b.role in ("ceo", "cfo") and b.total_eur >= CONVICTION_MIN_EUR
                and (b.increase_pct or 0) >= CONVICTION_MIN_INCREASE_PCT):
            rules.append(f"{_ROLE_LABEL[b.role]} купил на €{_short(b.total_eur)} "
                         f"(+{b.increase_pct:.0f}% к позиции)")
            break
    met += rules
    if not insiders:
        missed.append("покупатели не из руководства компании")
    elif not rules:
        missed.append("нет 3+ инсайдеров, CEO/CFO/Chair в кластере или крупной покупки CEO/CFO")
    tier = STRONG if (rules and size_known and not is_coin) else CANDIDATE
    return Tiered(sig, tier, met, missed)


def _crypto_tier(conn, sig) -> Tiered | None:
    if not sig.bullish:
        return None
    if sig.crypto_kind == "treasury" and (sig.total_value or 0) < CRYPTO_TREASURY_BIG_EUR:
        return None
    met = ["крупный приток"]
    trend = crypto.price_trend(conn, sig.coin)
    if trend is None:
        return Tiered(sig, CANDIDATE, met, ["цена не проверена"])
    desc = (f"{sig.coin} {trend['ret_7d']:+.1f}% за 7 дн., "
            f"{'выше' if trend['above_ma20'] else 'ниже'} 20-дн. средней")
    if crypto.trend_confirms(trend):
        return Tiered(sig, STRONG, met + [desc], [])
    return Tiered(sig, CANDIDATE, met, [f"цена не подтверждает: {desc}"])


def select(conn, signals: list, t212, today: dt.date | None = None) -> Selection:
    """`t212` is a trading212.Availability, or None when the instrument list isn't
    available -- then stocks aren't filtered and Selection.t212_checked says so."""
    today = today or dt.date.today()
    since = (today - dt.timedelta(days=MAX_AGE_DAYS)).isoformat()
    pre = [s for s in signals
           if is_buy_side(s)
           and (cluster.disclosed_on(conn, s) or "") >= since
           and (t212 is None or t212.can_buy(s.ticker, s.source))]
    pre = cluster.enrich_signals(conn, pre) if pre else []
    tiered = []
    for s in pre:
        t = _crypto_tier(conn, s) if hasattr(s, "crypto_kind") else _stock_tier(s)
        if t is not None:
            s.tier = t.tier
            tiered.append(t)
    return Selection(
        strong=[t for t in tiered if t.tier == STRONG],
        candidates=[t for t in tiered if t.tier == CANDIDATE][:MAX_CANDIDATES],
        t212_checked=t212 is not None,
    )
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_strategy.py`
Expected: all pass. (Rule (c) test: $700,000 / 1.16 ≈ €603k, above both the €500k solo-buy
threshold and rule (c)'s €250k; 100 shares on top of 400 held is +25%.)

- [ ] **Step 5: Run the full suite, then commit**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider` (expected: all pass)

```bash
git add strategy.py tests/test_strategy.py
git commit -m "feat(strategy): Сильный/Кандидат tiers with floors and explicit rules

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Tier in the journal, and `positions.py`

**Files:**
- Modify: `db.py`:
  - add `("signal_journal", "tier", "TEXT")` to `_ADDED_COLUMNS`;
  - add `"tier"` to `journal_signal`'s `cols`;
  - add a `positions` table to `SCHEMA`.
- Modify: `bot.py` `_signal_features`: add `"tier": getattr(sig, "tier", None),`.
- Create: `positions.py`
- Test: `tests/test_positions.py`

**Interfaces:**
- Consumes: `cluster.name_key`; `crypto.yf_symbol`; a `signal_journal.tier` value of
  `"strong"`, written by `bot._commit_signals` for signals that `strategy.select` tagged
  (Task 7 wires this).
- Produces:
  - `positions.Position(id, ticker, source, opened_at, entry_price, insiders: list[str], signal_id, closed_at, close_reason, close_alerted_at)`;
  - `positions.CloseAlert(position: Position, trigger: str, detail: str, last_price: float | None)`,
    where `trigger` is one of `"insider_sell"`, `"time"`, `"stop_loss"`;
  - `positions.open_position(conn, ticker: str, entry_price: float, today: dt.date | None = None) -> Position`,
    which raises `ValueError` if a position on that ticker is already open;
  - `positions.close_position(conn, ticker: str, reason: str = "manual", today=None) -> Position | None`;
  - `positions.open_positions(conn) -> list[Position]`;
  - `positions.last_close(ticker: str) -> float | None`;
  - `positions.check_exits(conn, today=None, price_fn=last_close) -> list[CloseAlert]`;
  - `positions.mark_alerted(conn, alerts: list[CloseAlert], today=None) -> None`;
  - `EXIT_MAX_DAYS = 90`, `EXIT_STOP_LOSS_PCT = 15.0`.

- [ ] **Step 1: Write the failing tests** (`tests/test_positions.py`)

```python
"""positions.py: what the user reports buying, and when to close it."""
from __future__ import annotations

import datetime as dt
import json

import pytest

import db
import positions
from conftest import add_form_144, add_sec_sale

TODAY = dt.date(2026, 9, 23)


def _strong_journal(conn, ticker, members):
    db.journal_signal(conn, {"source": "SEC", "kind": "cluster", "ticker": ticker,
                             "tier": "strong", "members": json.dumps(members)})


def _open(conn, ticker="AAA", price=100.0, days_ago=5):
    return positions.open_position(conn, ticker, price, today=TODAY - dt.timedelta(days=days_ago))


def _no_price(ticker):
    return None


def test_open_takes_the_insiders_from_the_latest_strong_signal(conn):
    _strong_journal(conn, "AAA", ["Old Buyer"])
    _strong_journal(conn, "AAA", ["Boss Person", "Board Person"])
    pos = _open(conn)
    assert pos.insiders == ["Boss Person", "Board Person"] and pos.signal_id is not None


def test_open_without_a_signal_has_no_insiders(conn):
    assert _open(conn).insiders == []


def test_second_open_on_the_same_ticker_is_refused(conn):
    _open(conn)
    with pytest.raises(ValueError):
        _open(conn)


def test_insider_sale_after_opening_closes(conn):
    _strong_journal(conn, "AAA", ["Boss Person"])
    _open(conn)
    add_sec_sale(conn, "AAA", "PERSON BOSS", 500_000, date=(TODAY - dt.timedelta(days=1)).isoformat())
    [alert] = positions.check_exits(conn, today=TODAY, price_fn=_no_price)
    assert alert.trigger == "insider_sell" and "PERSON BOSS" in alert.detail


def test_sale_before_opening_does_not_count(conn):
    _strong_journal(conn, "AAA", ["Boss Person"])
    add_sec_sale(conn, "AAA", "Boss Person", 500_000, date=(TODAY - dt.timedelta(days=30)).isoformat())
    _open(conn)
    assert positions.check_exits(conn, today=TODAY, price_fn=_no_price) == []


def test_planned_10b5_1_sale_does_not_count(conn):
    _strong_journal(conn, "AAA", ["Boss Person"])
    _open(conn)
    add_sec_sale(conn, "AAA", "Boss Person", 500_000, date=(TODAY - dt.timedelta(days=1)).isoformat())
    conn.execute("UPDATE sec_sales SET is_10b5_1 = 1")
    assert positions.check_exits(conn, today=TODAY, price_fn=_no_price) == []


def test_form_144_notice_counts_as_selling(conn):
    _strong_journal(conn, "AAA", ["Boss Person"])
    _open(conn)
    add_form_144(conn, "AAA", "Boss Person", 900_000, date=(TODAY - dt.timedelta(days=1)).isoformat())
    [alert] = positions.check_exits(conn, today=TODAY, price_fn=_no_price)
    assert alert.trigger == "insider_sell" and "144" in alert.detail


def test_time_limit_closes(conn):
    _open(conn, days_ago=positions.EXIT_MAX_DAYS)
    [alert] = positions.check_exits(conn, today=TODAY, price_fn=_no_price)
    assert alert.trigger == "time"


def test_stop_loss_closes(conn):
    _open(conn, price=100.0)
    [alert] = positions.check_exits(conn, today=TODAY, price_fn=lambda t: 84.9)
    assert alert.trigger == "stop_loss" and alert.last_price == 84.9


def test_small_drawdown_does_not_close(conn):
    _open(conn, price=100.0)
    assert positions.check_exits(conn, today=TODAY, price_fn=lambda t: 90.0) == []


def test_each_position_alerts_once(conn):
    _open(conn, days_ago=positions.EXIT_MAX_DAYS)
    alerts = positions.check_exits(conn, today=TODAY, price_fn=_no_price)
    positions.mark_alerted(conn, alerts, today=TODAY)
    assert positions.check_exits(conn, today=TODAY, price_fn=_no_price) == []


def test_close_removes_it_from_open_positions(conn):
    _open(conn)
    closed = positions.close_position(conn, "aaa", today=TODAY)
    assert closed.close_reason == "manual" and positions.open_positions(conn) == []
    assert positions.close_position(conn, "AAA", today=TODAY) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_positions.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'positions'`.

- [ ] **Step 3: Schema and journal changes** (`db.py`)

Append to `SCHEMA`, just before its closing `"""`:
```sql

-- Positions the user reports via Telegram (/bought, /sold) -- positions.py. The bot
-- never reads the brokerage account; this is only what the user tells it.
CREATE TABLE IF NOT EXISTS positions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker            TEXT NOT NULL,
    source            TEXT,
    opened_at         TEXT NOT NULL,              -- ISO date
    entry_price       REAL NOT NULL,
    insiders          TEXT NOT NULL DEFAULT '[]', -- JSON names whose selling closes it
    signal_id         INTEGER,                    -- signal_journal row it came from
    closed_at         TEXT,
    close_reason      TEXT,
    close_alerted_at  TEXT
);
```

Add to `_ADDED_COLUMNS`: `("signal_journal", "tier", "TEXT"),`.

In `journal_signal`, add `"tier"` to the end of `cols`.

In `bot.py` `_signal_features`, add to the returned dict:
`"tier": getattr(sig, "tier", None),`.

- [ ] **Step 4: Implement `positions.py`**

```python
"""Positions the user reports buying (/bought, /sold in telegram_bot.py), and when
to close them.

Only reported positions are tracked, at the user's own entry price -- the bot never
reads or trades the brokerage account. A close alert fires once per position, on the
first of:

  insider_sell  one of the insiders behind the "Сильный" signal it came from sells
                after the open date -- Form 4 (not a 10b5-1 planned sale), a Form 144
                notice of intent, or a sale row from Oslo, FI or BaFin;
  time          EXIT_MAX_DAYS held -- the horizon insider-buying research looks at;
  stop_loss     the last close is EXIT_STOP_LOSS_PCT or more below the entry.

The alert doesn't close the position; /sold does. The user stays in control.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass

import cluster
import crypto

EXIT_MAX_DAYS = 90
EXIT_STOP_LOSS_PCT = 15.0

# (label, SQL returning (person, sale date) for one issuer key and an ISO since-date).
# BaFin is handled separately: its dates are DD.MM.YYYY and don't compare as text.
_SALE_QUERIES = (
    ("Form 4", "SELECT owner_name, transaction_date FROM sec_sales "
               "WHERE ticker = ? AND transaction_date >= ? AND COALESCE(is_10b5_1, 0) = 0"),
    ("Form 144", "SELECT person_name, COALESCE(NULLIF(approx_sale_date, ''), date(found_at)) "
                 "FROM sec_proposed_sales WHERE ticker = ? "
                 "AND COALESCE(NULLIF(approx_sale_date, ''), date(found_at)) >= ?"),
    ("Oslo", "SELECT person, txn_date FROM norway_purchases "
             "WHERE ticker = ? AND txn_type = 'S' AND txn_date >= ?"),
    ("FI", "SELECT person, txn_date FROM sweden_purchases "
           "WHERE isin = ? AND txn_type = 'S' AND status = 'Aktuell' AND txn_date >= ?"),
)


@dataclass
class Position:
    id: int
    ticker: str
    source: str | None
    opened_at: str
    entry_price: float
    insiders: list[str]
    signal_id: int | None
    closed_at: str | None
    close_reason: str | None
    close_alerted_at: str | None


@dataclass
class CloseAlert:
    position: Position
    trigger: str            # insider_sell / time / stop_loss
    detail: str
    last_price: float | None


_COLS = ("id, ticker, source, opened_at, entry_price, insiders, signal_id, closed_at, "
         "close_reason, close_alerted_at")


def _row(r) -> Position:
    return Position(r[0], r[1], r[2], r[3], r[4], json.loads(r[5] or "[]"), r[6], r[7], r[8], r[9])


def open_positions(conn) -> list[Position]:
    return [_row(r) for r in conn.execute(
        f"SELECT {_COLS} FROM positions WHERE closed_at IS NULL ORDER BY opened_at")]


def open_position(conn, ticker: str, entry_price: float, today: dt.date | None = None) -> Position:
    ticker = ticker.strip().upper()
    if any(p.ticker == ticker for p in open_positions(conn)):
        raise ValueError(f"position in {ticker} is already open")
    sig = conn.execute(
        "SELECT id, source, members FROM signal_journal WHERE ticker = ? AND tier = 'strong' "
        "ORDER BY emitted_at DESC, id DESC LIMIT 1", (ticker,)).fetchone()
    signal_id, source, members = sig if sig else (None, None, "[]")
    conn.execute(
        "INSERT INTO positions (ticker, source, opened_at, entry_price, insiders, signal_id) "
        "VALUES (?,?,?,?,?,?)",
        (ticker, source, (today or dt.date.today()).isoformat(), float(entry_price),
         members or "[]", signal_id))
    conn.commit()
    return next(p for p in open_positions(conn) if p.ticker == ticker)


def close_position(conn, ticker: str, reason: str = "manual",
                   today: dt.date | None = None) -> Position | None:
    ticker = ticker.strip().upper()
    pos = next((p for p in open_positions(conn) if p.ticker == ticker), None)
    if pos is None:
        return None
    pos.closed_at, pos.close_reason = (today or dt.date.today()).isoformat(), reason
    conn.execute("UPDATE positions SET closed_at = ?, close_reason = ? WHERE id = ?",
                 (pos.closed_at, reason, pos.id))
    conn.commit()
    return pos


def last_close(ticker: str) -> float | None:
    """Most recent daily close from Yahoo, or None (an ISIN, a delisting, no network)."""
    try:
        import yfinance as yf
        hist = yf.Ticker(crypto.yf_symbol(ticker)).history(period="5d")["Close"].dropna()
    except Exception:
        return None
    return float(hist.iloc[-1]) if len(hist) else None


def _insider_sale(conn, pos: Position) -> str | None:
    keys = {cluster.name_key(n) for n in pos.insiders}
    if not keys:
        return None
    for label, sql in _SALE_QUERIES:
        for person, date in conn.execute(sql, (pos.ticker, pos.opened_at)):
            if cluster.name_key(person) in keys:
                return f"{person} — {label}, {date}"
    for person, date in conn.execute(
            "SELECT notifier_name, txn_date FROM bafin_purchases WHERE isin = ? AND txn_type = 'S'",
            (pos.ticker,)):
        try:
            iso = dt.datetime.strptime(date, "%d.%m.%Y").date().isoformat()
        except (TypeError, ValueError):
            continue
        if iso >= pos.opened_at and cluster.name_key(person) in keys:
            return f"{person} — BaFin, {iso}"
    return None


def check_exits(conn, today: dt.date | None = None, price_fn=last_close) -> list[CloseAlert]:
    today = today or dt.date.today()
    alerts = []
    for pos in open_positions(conn):
        if pos.close_alerted_at:
            continue
        sale = _insider_sale(conn, pos)
        price = price_fn(pos.ticker)
        if sale:
            alerts.append(CloseAlert(pos, "insider_sell", sale, price))
            continue
        held = (today - dt.date.fromisoformat(pos.opened_at)).days
        if held >= EXIT_MAX_DAYS:
            alerts.append(CloseAlert(pos, "time", f"{held} дн. в позиции", price))
            continue
        if price is None:
            print(f"[positions] no price for {pos.ticker}; stop-loss check skipped today")
            continue
        if price <= pos.entry_price * (1 - EXIT_STOP_LOSS_PCT / 100):
            change = (price / pos.entry_price - 1) * 100
            alerts.append(CloseAlert(pos, "stop_loss", f"{change:+.1f}% от входа", price))
    return alerts


def mark_alerted(conn, alerts: list[CloseAlert], today: dt.date | None = None) -> None:
    stamp = (today or dt.date.today()).isoformat()
    for a in alerts:
        conn.execute("UPDATE positions SET close_alerted_at = ? WHERE id = ?", (stamp, a.position.id))
    conn.commit()
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add db.py bot.py positions.py tests/test_positions.py
git commit -m "feat(positions): reported positions and close alerts; tier in the journal

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Message formatting (three sections, close alerts, positions)

**Files:**
- Modify: `telegram_notify.py`
- Test: `tests/test_tiered_format.py`

**Interfaces:**
- Consumes: `strategy.Selection`/`Tiered` (duck-typed: `.strong`, `.candidates`,
  `.t212_checked`, `.signal`, `.met`, `.missed`); `positions.CloseAlert`/`Position`.
- Produces:
  - `telegram_notify.format_tiered_digest(selection, closes: list, *, html: bool = True) -> str`;
  - `telegram_notify.format_close_alert(alert, *, html: bool = True) -> str`;
  - `telegram_notify.format_positions(positions: list, price_fn) -> str` (plain text).

- [ ] **Step 1: Write the failing tests** (`tests/test_tiered_format.py`)

```python
"""The three-section message: 🔥 Сильные / 👀 Кандидаты / 🚪 Закрыть."""
from __future__ import annotations

import cluster
import positions
import strategy
import telegram_notify


def _sig(ticker="AAA"):
    return cluster.ClusterSignal(source="SEC", ticker=ticker, company="Test Corp", buyer_count=3,
                                 total_value=900_000.0, members=["A (Director) €300,000"],
                                 window_start="2026-09-21", window_end="2026-09-22")


def _pos(ticker="AAA", entry=100.0):
    return positions.Position(1, ticker, "SEC", "2026-09-01", entry, ["A"], None, None, None, None)


def _selection(t212=True):
    strong = [strategy.Tiered(_sig("AAA"), strategy.STRONG, ["3 инсайдера(ов) из руководства"], [])]
    cand = [strategy.Tiered(_sig("BBB"), strategy.CANDIDATE, [], ["размер неизвестен"])]
    return strategy.Selection(strong, cand, t212)


def test_sections_rules_and_closes_appear():
    close = positions.CloseAlert(_pos("CCC"), "stop_loss", "−16.0% от входа", 84.0)
    text = telegram_notify.format_tiered_digest(_selection(), [close])
    assert text.index("Сильные") < text.index("Кандидаты") < text.index("Закрыть")
    assert "✓ 3 инсайдера(ов) из руководства" in text and "✗ размер неизвестен" in text
    assert "CCC" in text and "стоп-лосс" in text


def test_missing_trading_212_check_is_announced():
    assert "Trading 212 не проверялся" in telegram_notify.format_tiered_digest(_selection(False), [])


def test_empty_selection_says_there_is_nothing():
    empty = strategy.Selection([], [], True)
    assert "сигналов нет" in telegram_notify.format_tiered_digest(empty, [], html=False)


def test_plain_text_has_no_html_tags():
    assert "<b>" not in telegram_notify.format_tiered_digest(_selection(), [], html=False)


def test_positions_show_return_and_days():
    text = telegram_notify.format_positions([_pos(entry=100.0)], lambda t: 112.0)
    assert "AAA" in text and "+12.0%" in text
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_tiered_format.py`
Expected: FAIL with `AttributeError: module 'telegram_notify' has no attribute 'format_tiered_digest'`.

- [ ] **Step 3: Implement** (append to `telegram_notify.py`)

```python
_CLOSE_REASON = {"insider_sell": "инсайдеры продают", "time": "срок вышел",
                 "stop_loss": "стоп-лосс"}


def _rule_lines(t, html: bool) -> list[str]:
    lines = []
    if t.met:
        text = " · ".join(f"✓ {m}" for m in t.met)
        lines.append(f"   {_esc(text) if html else text}")
    if t.missed:
        text = " · ".join(f"✗ {m}" for m in t.missed)
        lines.append(f"   {_esc(text) if html else text}")
    return lines


def format_close_alert(alert, *, html: bool = True) -> str:
    pos = alert.position
    reason = _CLOSE_REASON.get(alert.trigger, alert.trigger)
    price = ""
    if alert.last_price:
        change = (alert.last_price / pos.entry_price - 1) * 100
        price = f" · вход {pos.entry_price:,.2f} → {alert.last_price:,.2f} ({change:+.1f}%)"
    head = f"🚪 {pos.ticker} — {reason}"
    detail = f"{alert.detail}{price} · открыта {datefmt.fmt(pos.opened_at)}"
    return f"{_b(head, html)}\n   {_esc(detail) if html else detail}"


def format_tiered_digest(selection, closes: list, *, html: bool = True) -> str:
    """One message: 🔥 Сильные, 👀 Кандидаты, 🚪 Закрыть -- the daily Telegram digest
    and the menu's "Сигналы" view (html=False) both render this."""
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
    if not (selection.strong or selection.candidates or closes):
        parts.append("За последние 3 дня сигналов нет.")
    return "\n\n".join(parts)


def format_positions(positions: list, price_fn) -> str:
    if not positions:
        return "Открытых позиций нет. /bought TICKER [цена] — добавить."
    today = dt.date.today()
    lines = ["Открытые позиции:"]
    for p in positions:
        price = price_fn(p.ticker)
        change = f"{(price / p.entry_price - 1) * 100:+.1f}%" if price else "цена недоступна"
        days = (today - dt.date.fromisoformat(p.opened_at)).days
        lines.append(f"• {p.ticker}: вход {p.entry_price:,.2f}, сейчас "
                     f"{f'{price:,.2f}' if price else '—'} ({change}), {days} дн.")
    return "\n".join(lines)
```

Add `import datetime as dt` to `telegram_notify.py`'s standard-library imports. It isn't
imported there yet, and `format_positions` needs it.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add telegram_notify.py tests/test_tiered_format.py
git commit -m "feat(telegram): three-section digest, close alerts and positions list

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Telegram commands `/bought`, `/sold`, `/positions`

**Files:**
- Modify: `telegram_bot.py`
- Test: `tests/test_telegram_bot.py` (append)

**Interfaces:**
- Consumes: `positions.open_position`, `positions.close_position`,
  `positions.open_positions`, `positions.last_close`; `telegram_notify.format_positions`.
- Produces: `telegram_bot._handle_positions_command(conn, text: str) -> bool`, which
  returns True when it handled the message.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_telegram_bot.py`)

```python
# ------------------------------------------------------- position commands
@pytest.fixture
def replies(monkeypatch):
    sent = []
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: sent.append(msg) or True)
    monkeypatch.setattr("positions.last_close", lambda ticker: 50.0)
    return sent


def test_bought_with_price_opens_a_position(conn, replies):
    import positions
    tb._handle_message(conn, "/bought grab 18.40")
    [pos] = positions.open_positions(conn)
    assert (pos.ticker, pos.entry_price) == ("GRAB", 18.40) and "GRAB" in replies[-1]


def test_bought_without_price_uses_the_last_close(conn, replies):
    import positions
    tb._handle_message(conn, "/bought GRAB")
    assert positions.open_positions(conn)[0].entry_price == 50.0


def test_bought_with_a_bad_price_stores_nothing(conn, replies):
    import positions
    tb._handle_message(conn, "/bought GRAB abc")
    assert positions.open_positions(conn) == [] and "/bought" in replies[-1]


def test_bought_twice_is_refused(conn, replies):
    tb._handle_message(conn, "/bought GRAB 18")
    tb._handle_message(conn, "/bought GRAB 19")
    assert "уже" in replies[-1]


def test_sold_closes_and_unknown_sold_says_so(conn, replies):
    import positions
    tb._handle_message(conn, "/bought GRAB 18")
    tb._handle_message(conn, "/sold GRAB")
    assert positions.open_positions(conn) == []
    tb._handle_message(conn, "/sold GRAB")
    assert "нет открытой" in replies[-1]


def test_positions_lists_open_positions(conn, replies):
    tb._handle_message(conn, "/bought GRAB 40")
    tb._handle_message(conn, "/positions")
    assert "GRAB" in replies[-1] and "+25.0%" in replies[-1]
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_telegram_bot.py -k "bought or sold or positions"`
Expected: FAIL. The existing handler answers unknown `/` commands with HELP_TEXT, so no
position is stored.

- [ ] **Step 3: Implement** (`telegram_bot.py`)

Add `import positions` to the project imports (alphabetically after `import db`).

Add below `_extract_ticker`:
```python
_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{10}$")
POSITIONS_USAGE = ("/bought TICKER [цена] — отметить покупку (без цены — последнее закрытие)\n"
                   "/sold TICKER — отметить продажу\n"
                   "/positions — открытые позиции")


def _position_ticker(arg: str) -> str | None:
    t = arg.strip().lstrip("$").upper()
    return t if (_TICKER_RE.match(t) or _ISIN_RE.match(t)) else None


def _handle_positions_command(conn, text: str) -> bool:
    """/bought, /sold, /positions -- the positions positions.py tracks for close
    alerts. Returns False for anything else."""
    parts = text.split()
    cmd = parts[0].lower().split("@")[0] if parts else ""
    if cmd == "/positions":
        telegram_notify.send_text(telegram_notify.format_positions(
            positions.open_positions(conn), positions.last_close))
        return True
    if cmd not in ("/bought", "/sold"):
        return False
    ticker = _position_ticker(parts[1]) if len(parts) > 1 else None
    if not ticker:
        telegram_notify.send_text(POSITIONS_USAGE)
        return True
    if cmd == "/sold":
        pos = positions.close_position(conn, ticker)
        telegram_notify.send_text(f"Позиция {ticker} закрыта." if pos
                                  else f"По {ticker} нет открытой позиции.")
        return True
    price = None
    if len(parts) > 2:
        try:
            price = float(parts[2].replace(",", "."))
        except ValueError:
            telegram_notify.send_text(POSITIONS_USAGE)
            return True
        if price <= 0:
            telegram_notify.send_text(POSITIONS_USAGE)
            return True
    price = price or positions.last_close(ticker)
    if not price:
        telegram_notify.send_text(f"Не нашёл цену {ticker} — укажите её: /bought {ticker} 12.34")
        return True
    try:
        pos = positions.open_position(conn, ticker, price)
    except ValueError:
        telegram_notify.send_text(f"Позиция {ticker} уже открыта. /sold {ticker}, чтобы закрыть.")
        return True
    who = (f"слежу за продажами: {', '.join(pos.insiders)}" if pos.insiders
           else "сильного сигнала по нему не было — слежу только за сроком и стоп-лоссом")
    telegram_notify.send_text(f"Записал {ticker} по {price:,.2f}; {who}.")
    return True
```

In `_handle_message`, add as the first check after `text = (text or "").strip()`:
```python
    if _handle_positions_command(conn, text):
        return
```

Append to `HELP_TEXT`: `+ "\n" + POSITIONS_USAGE`. `POSITIONS_USAGE` must be defined
above `HELP_TEXT`, so move the constant up next to the other constants.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add telegram_bot.py tests/test_telegram_bot.py
git commit -m "feat(telegram_bot): /bought, /sold and /positions

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Wire tiers and close alerts into the daily run

**Files:**
- Modify: `bot.py`:
  - `run_cluster_pass` returns a `strategy.Selection`;
  - `main`'s send block uses a new `_send_digest`;
  - remove the `--telegram-item-limit` flag and its flood branch.
- Modify: `run_daily.sh`: drop `--min-score 35`.
- Test: `tests/test_signals.py` (append)

**Interfaces:**
- Consumes: `strategy.select`; `trading212.availability`; `positions.check_exits` and
  `positions.mark_alerted`; `telegram_notify.format_tiered_digest`;
  `bot._commit_signals` (existing).
- Produces:
  - `bot.run_cluster_pass(conn, args) -> strategy.Selection`;
  - `bot._send_digest(conn, selection, closes) -> bool`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_signals.py`)

```python
# ------------------------------------------------------- tiered daily digest
def test_send_digest_commits_signals_and_close_alerts_only_on_success(conn, monkeypatch):
    import positions
    import strategy
    RECENT = (TODAY - dt.timedelta(days=1)).isoformat()
    sig = cluster.ClusterSignal(source="SEC", ticker="AAA", company="C", buyer_count=3,
                                total_value=1e6, members=[], window_start=RECENT,
                                window_end=RECENT, member_names=["A", "B", "C"])
    sig.tier = strategy.STRONG
    sel = strategy.Selection([strategy.Tiered(sig, strategy.STRONG, ["x"], [])], [], True)
    pos = positions.open_position(conn, "ZZZ", 10.0, today=TODAY - dt.timedelta(days=100))
    closes = positions.check_exits(conn, price_fn=lambda t: None)

    monkeypatch.setattr("telegram_notify.send_text", lambda msg: False)
    assert bot._send_digest(conn, sel, closes) is False
    assert db.get_alert_state(conn, "SEC", "AAA") is None
    assert positions.check_exits(conn, price_fn=lambda t: None)       # not marked yet

    sent = []
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: sent.append(msg) or True)
    assert bot._send_digest(conn, sel, closes) is True
    assert "Сильные" in sent[0] and "ZZZ" in sent[0]
    assert db.get_alert_state(conn, "SEC", "AAA") is not None
    assert conn.execute("SELECT tier FROM signal_journal").fetchone()[0] == "strong"
    assert positions.check_exits(conn, price_fn=lambda t: None) == []


def test_send_digest_sends_nothing_when_there_is_nothing(conn, monkeypatch):
    import strategy
    sent = []
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: sent.append(msg) or True)
    assert bot._send_digest(conn, strategy.Selection([], [], True), []) is False
    assert sent == []
```

(`TODAY` is already defined at the top of `tests/test_signals.py`.)

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_signals.py -k send_digest`
Expected: FAIL with `AttributeError: module 'bot' has no attribute '_send_digest'`.

- [ ] **Step 3: Implement** (`bot.py`)

Add imports: `import positions`, `import strategy`, `import trading212`.

At the end of `run_cluster_pass`, replace everything from
`# Attach company context (size, liquidity) and rank.` to `return signals` with:
```python
    # Tiers (strategy.py): buy side, disclosed in the last few days, on Trading 212,
    # above the size floors -- enrich_signals runs inside select(), only on what
    # survives the cheap filters.
    selection = strategy.select(conn, signals, trading212.availability(conn))

    def keep(t):
        s = t.signal
        if args.min_score and getattr(s, "score", 0) < args.min_score:
            return False
        adv = getattr(s, "avg_daily_value", None)
        return not (args.min_liquidity and adv is not None and adv < args.min_liquidity)
    selection.strong = [t for t in selection.strong if keep(t)]
    selection.candidates = [t for t in selection.candidates if keep(t)]

    if not args.no_market_context:
        # Last, so it runs only on what will actually be shown.
        tradingview.annotate_signals([t.signal for t in selection.strong + selection.candidates])

    for t in selection.strong + selection.candidates:
        print(f"[{t.tier}] " + telegram_notify.format_any_signal(t.signal))
    return selection
```
Update the function's docstring to say it returns the tiered `strategy.Selection`.

Add below `_commit_signals`:
```python
def _send_digest(conn, selection, closes) -> bool:
    """One Telegram message -- 🔥 Сильные, 👀 Кандидаты, 🚪 Закрыть -- and, only if it
    went through, the bookkeeping: alert state and journal for the signals, and the
    once-only mark for close alerts. A failed send leaves both untouched so the next
    run retries. Nothing to say -> nothing sent, returns False."""
    tiered = selection.strong + selection.candidates
    if not tiered and not closes:
        return False
    if not telegram_notify.send_text(telegram_notify.format_tiered_digest(selection, closes)):
        print(f"[telegram] send failed -- leaving {len(tiered)} signal(s) and "
              f"{len(closes)} close alert(s) for the next run", file=sys.stderr)
        return False
    _commit_signals(conn, [t.signal for t in tiered])
    positions.mark_alerted(conn, closes)
    return True
```

In `main`, replace the block from `signals = run_cluster_pass(conn, args)` through the end of
the `if signals and not args.no_telegram:` block (the flood-limit branch and the digest
branch) with:
```python
        selection = run_cluster_pass(conn, args)
        closes = positions.check_exits(conn)
        for a in closes:
            print(telegram_notify.format_close_alert(a, html=False))
        if not args.no_telegram:
            _send_digest(conn, selection, closes)
        signal_count = len(selection.strong) + len(selection.candidates)
```
and in the `--- poll finished` print, replace `{len(signals)} signal(s)` with
`{signal_count} signal(s), {len(closes)} close alert(s)`.

Remove the `--telegram-item-limit` `add_argument` (it was only used by the removed flood
branch; `grep -n telegram_item_limit bot.py` must return nothing afterwards).

In `run_daily.sh`, change
`python bot.py --once --stake-min-percent 10 --activist-only --new-positions-only --min-score 35`
to
`python bot.py --once --stake-min-percent 10 --activist-only --new-positions-only`.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Smoke run against a scratch copy of the live database** (touches neither
the live DB nor Telegram)

```bash
SP=$(mktemp -d); cp data/disclosures.db "$SP/v.db"
.venv/bin/python - "$SP" <<'EOF'
import sys, pathlib, bot, passes
sp = pathlib.Path(sys.argv[1])
bot.DB_PATH, bot.LOG_PATH, passes.CSV_PATH = sp / "v.db", sp / "b.log", sp / "p.csv"
sys.argv = ["bot.py", "--once", "--sweden-only", "--no-telegram", "--sweden-days", "3"]
bot.main()
EOF
```
Expected: the run ends with `--- poll finished, … signal(s), … close alert(s) ---` and no
traceback; any printed signals are prefixed `[strong]` or `[candidate]`.

- [ ] **Step 6: Commit**

```bash
git add bot.py run_daily.sh tests/test_signals.py
git commit -m "feat(bot): daily digest is the tiered selection plus close alerts

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Menu "Сигналы" on the strategy, and README

**Files:**
- Modify: `menu.py` (`show_signals`; remove `SIGNALS_MIN_SCORE` and `SIGNALS_MAX_AGE_DAYS`)
- Modify: `tests/test_signals_view.py` (the three view tests at the bottom)
- Modify: `README.md` ("Интерактивное меню" section; a new "Позиции" subsection)

**Interfaces:**
- Consumes: `strategy.select`, `trading212.availability`, `positions.check_exits`,
  `positions.open_positions`, `positions.last_close`,
  `telegram_notify.format_tiered_digest`, `telegram_notify.format_positions`.

- [ ] **Step 1: Update the view tests** (`tests/test_signals_view.py`)

Replace the three tests under `# ---- the menu view` with:
```python
@pytest.fixture
def no_prices(monkeypatch):
    monkeypatch.setattr("positions.last_close", lambda ticker: None)


def test_view_keeps_recent_buyable_stocks_and_crypto(conn, keyed, scored, no_prices, capsys,
                                                      monkeypatch):
    monkeypatch.setattr(trading212, "fetch_instruments", lambda session=None: INSTRUMENTS)
    monkeypatch.setattr("crypto.price_trend", lambda conn, sym: {"ret_7d": 3.0, "above_ma20": True})
    recent = (TODAY - dt.timedelta(days=1)).isoformat()
    for o in ("A", "B", "C"):
        add_sec_purchase(conn, "AAPL", o, 900_000, recent, filed_date=recent)
        add_sec_purchase(conn, "ZZZZ", o, 900_000, recent, filed_date=recent)   # not on T212
    db.save_crypto_treasury_txn(conn, ct.TreasuryTxn(
        "acc", "Acme", "ACME", "1", "BTC", "P", 1000, 80_000.0, None, recent, "8-K", "u"))
    menu.show_signals(conn)
    out = _shown(capsys)
    assert "Сильные" in out and "AAPL" in out and "CRYPTO:BTC" in out and "ZZZZ" not in out


def test_view_drops_signals_disclosed_before_the_window(conn, keyed, scored, no_prices, capsys,
                                                         monkeypatch):
    monkeypatch.setattr(trading212, "fetch_instruments", lambda session=None: INSTRUMENTS)
    add_sec_purchase(conn, "AAPL", "Buyer", 900_000, (TODAY - dt.timedelta(days=10)).isoformat(),
                     filed_date=(TODAY - dt.timedelta(days=5)).isoformat())
    menu.show_signals(conn)
    assert "сигналов нет" in _shown(capsys)


def test_view_without_a_key_says_so_and_keeps_stocks(conn, scored, no_prices, capsys,
                                                      monkeypatch, tmp_path):
    monkeypatch.setattr(trading212, "ENV_FILE", tmp_path / "missing.env")
    monkeypatch.delenv("TRADING212_API_KEY", raising=False)
    recent = (TODAY - dt.timedelta(days=1)).isoformat()
    add_sec_purchase(conn, "ZZZZ", "Buyer", 900_000, recent, filed_date=recent)
    menu.show_signals(conn)
    out = _shown(capsys)
    assert "Trading 212 не проверялся" in out and "ZZZZ" in out


def test_view_lists_open_positions(conn, keyed, scored, capsys, monkeypatch):
    import positions
    monkeypatch.setattr(trading212, "fetch_instruments", lambda session=None: INSTRUMENTS)
    monkeypatch.setattr("positions.last_close", lambda ticker: 110.0)
    positions.open_position(conn, "AAPL", 100.0)
    menu.show_signals(conn)
    assert "Открытые позиции" in _shown(capsys)
```
The `scored` fixture already sets `score` on every signal. Also make it set
`market_cap_eur = 5e9` and `avg_daily_value = 5e7` on non-crypto signals, so the size floors
pass:
```python
    def fake_enrich(conn, signals):
        for s in signals:
            s.score = 100.0
            if not getattr(s, "crypto_kind", None):
                s.market_cap_eur, s.avg_daily_value = 5e9, 5e7
        return signals
```
`strategy.select` calls `cluster.enrich_signals`, so change the fixture's
`monkeypatch.setattr(menu.cluster, "enrich_signals", fake_enrich)` to
`monkeypatch.setattr("cluster.enrich_signals", fake_enrich)`.

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_signals_view.py`
Expected: FAIL. The old view prints no "Сильные" section and no positions.

- [ ] **Step 3: Implement** (`menu.py`)

Replace the imports block's project imports with:
```python
import cluster
import db
import positions
import research
import strategy
import telegram_notify
import termstyle
import trading212
```
Delete the `SIGNALS_MIN_SCORE` and `SIGNALS_MAX_AGE_DAYS` definitions and their comments.
Replace `show_signals` with:
```python
def show_signals(conn) -> None:
    """The same selection the daily Telegram digest sends (strategy.py), computed
    fresh and including signals already sent -- this is a browse, not a digest --
    plus pending close alerts and the positions reported with /bought."""
    print()
    print(termstyle.header("СИГНАЛЫ"))
    selection = strategy.select(conn, _find_signals(conn), trading212.availability(conn))
    closes = positions.check_exits(conn)
    print(telegram_notify.format_tiered_digest(selection, closes, html=False))
    print()
    print(telegram_notify.format_positions(positions.open_positions(conn), positions.last_close))
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: README** (`README.md`, "Интерактивное меню" section)

Replace the bullet list that starts «Сигналы» — только сигналы на покупку… and the paragraph
after it with:

```markdown
«Сигналы» — то же, что ежедневный дайджест в Telegram (правила — [strategy.py](strategy.py),
дизайн — docs/superpowers/specs/2026-09-23-signal-strategy-v2-design.md):

- **🔥 Сильные** — действовать можно в тот же день. Акция: покупают люди из
  руководства компании и выполнено одно из правил: 3+ инсайдера; 2+ инсайдера,
  среди них CEO/CFO/председатель; CEO/CFO купил от €250k и нарастил позицию на 10%+.
  Плюс размер известен и выше порогов. Крипто (BTC/ETH): крупный приток, и цена
  его подтверждает (рост за 7 дней и выше 20-дневной средней).
- **👀 Кандидаты** — всё остальное, что прошло пороги, топ-10 по баллу: держатели
  >10%, активисты 13D, Конгресс, европейские инсайдеры, крипто без подтверждения
  ценой.
- **🚪 Закрыть** — по вашим позициям (см. ниже).

Пороги для всех акций: продаётся на Trading 212, раскрыто за последние 3 дня (по
дате раскрытия, не сделки — [cluster/recency.py](cluster/recency.py)),
капитализация от €300 млн и оборот от €1 млн в день. Если размер не определился —
максимум «кандидат». Каждый сигнал показывает, какие правила выполнены (✓) и какие
нет (✗).

### Позиции

Команды Telegram-бота: `/bought TICKER [цена]` (без цены — последнее закрытие),
`/sold TICKER`, `/positions`. Бот не читает и не торгует счёт Trading 212 — только
то, что вы ему сообщили. «Закрыть» приходит один раз, по первому из: продаёт кто-то
из инсайдеров, давших сильный сигнал (Form 4 не по плану 10b5-1, Form 144, продажи
в Осло/FI/BaFin); прошло 90 дней; цена на 15% ниже входа. Позиция закрывается
только командой `/sold`.
```

- [ ] **Step 6: Commit**

```bash
git add menu.py tests/test_signals_view.py README.md
git commit -m "feat(menu): Сигналы shows the tiered selection and open positions

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Calibration replay, then report to the user

**Files:**
- Create: `calibrate_strategy.py`
- Test: `tests/test_calibrate.py`

**Interfaces:**
- Consumes: `strategy.select(conn, signals, t212, today=D)`; the finders in
  `cluster.buys`, `cluster.stakes` and `cluster.crypto`, whose `dt.date.today()` gets
  frozen per replayed day; `trading212.availability`.
- Produces: `calibrate_strategy.replay(conn, start: dt.date, end: dt.date, t212) -> dict[str, dict]`,
  mapping ISO week `"2026-W37"` to `{"strong": [tickers], "candidates": [tickers]}`.
  Each (ticker, tier) is counted once, in the week it first appears.

- [ ] **Step 1: Write the failing test** (`tests/test_calibrate.py`)

```python
"""calibrate_strategy.replay: tiers as they would have been on past days, with rows
disclosed after that day hidden."""
from __future__ import annotations

import datetime as dt

import calibrate_strategy
import cluster
from conftest import add_sec_purchase


def _sized(monkeypatch):
    def fake(conn, signals):
        for s in signals:
            s.score, s.market_cap_eur, s.avg_daily_value = 50.0, 5e9, 5e7
        return signals
    monkeypatch.setattr(cluster, "enrich_signals", fake)


def test_a_strong_signal_is_counted_once_in_the_week_it_appeared(conn, monkeypatch):
    _sized(monkeypatch)
    day = dt.date(2026, 9, 9)                       # ISO week 37
    for o in ("A", "B", "C"):
        add_sec_purchase(conn, "AAA", o, 116_000, day.isoformat(), filed_date=day.isoformat())
    conn.execute("UPDATE sec_purchases SET found_at = ?", (f"{day.isoformat()} 06:00:00",))
    weeks = calibrate_strategy.replay(conn, day - dt.timedelta(days=2), day + dt.timedelta(days=6), None)
    assert weeks["2026-W37"]["strong"] == ["AAA"]
    assert sum(len(w["strong"]) for w in weeks.values()) == 1


def test_rows_disclosed_after_the_replayed_day_are_invisible(conn, monkeypatch):
    _sized(monkeypatch)
    day = dt.date(2026, 9, 9)
    for o in ("A", "B", "C"):
        add_sec_purchase(conn, "AAA", o, 116_000, day.isoformat(), filed_date=day.isoformat())
    conn.execute("UPDATE sec_purchases SET found_at = '2026-09-20 06:00:00'")
    weeks = calibrate_strategy.replay(conn, day, day, None)
    assert all(not w["strong"] for w in weeks.values())
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_calibrate.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'calibrate_strategy'`.

- [ ] **Step 3: Implement `calibrate_strategy.py`**

```python
"""Replay strategy.py's tiers day by day over stored history, to check the rules give
roughly the intended volume (2-3 Сильный, 5-10 Кандидат a week) before trusting them.

For each replayed day D, every source table is shadowed by a TEMP VIEW of the same
name that hides rows disclosed after D (SQLite resolves unqualified names to the temp
schema first), and the finders' "today" is frozen to D. Market caps come from today's
cache -- an approximation, and the report says so. Runs on an in-memory copy of the
database; the file on disk is never written.

    python calibrate_strategy.py              # last 35 days
    python calibrate_strategy.py --days 60
"""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import types
from pathlib import Path

import cluster
import cluster.buys
import cluster.crypto
import cluster.stakes
import db
import strategy
import trading212

DB_PATH = Path(__file__).parent / "data" / "disclosures.db"

# table -> SQL condition (with {d} = the replayed ISO date) for "known by then".
_VISIBLE = {
    "sec_purchases": "date(found_at) <= '{d}' AND COALESCE(NULLIF(filed_date, ''), '{d}') <= '{d}'",
    "house_purchases": "date(found_at) <= '{d}'",
    "senate_purchases": "date(found_at) <= '{d}'",
    "bafin_purchases": "date(found_at) <= '{d}'",
    "norway_purchases": "date(found_at) <= '{d}'",
    "sweden_purchases": "date(found_at) <= '{d}'",
    "sec_stakes": "date(found_at) <= '{d}'",
    "crypto_treasury_txns": "filed_date <= '{d}'",
    "crypto_etf_snapshots": "as_of <= '{d}'",
    "crypto_wallet_snapshots": "date(taken_at) <= '{d}'",
}
_FROZEN_MODULES = (cluster.buys, cluster.stakes, cluster.crypto)


def _frozen_dt(day: dt.date):
    class _Date(dt.date):
        @classmethod
        def today(cls):
            return day
    return types.SimpleNamespace(date=_Date, datetime=dt.datetime, timedelta=dt.timedelta)


def _shadow(conn, day: dt.date) -> None:
    for table, cond in _VISIBLE.items():
        conn.execute(f"DROP VIEW IF EXISTS temp.{table}")
        conn.execute(f"CREATE TEMP VIEW {table} AS SELECT * FROM main.{table} "
                     f"WHERE {cond.format(d=day.isoformat())}")


def _unshadow(conn) -> None:
    for table in _VISIBLE:
        conn.execute(f"DROP VIEW IF EXISTS temp.{table}")


def _signals(conn) -> list:
    return (cluster.find_sec_clusters(conn, ignore_alert_state=True)
            + cluster.find_house_clusters(conn, ignore_alert_state=True)
            + cluster.find_bafin_clusters(conn, ignore_alert_state=True)
            + cluster.find_norway_clusters(conn, ignore_alert_state=True)
            + cluster.find_sweden_clusters(conn, ignore_alert_state=True)
            + cluster.find_stake_signals(conn, min_percent=10.0, activist_only=True,
                                          max_age_days=30, new_positions_only=True,
                                          ignore_alert_state=True)
            + cluster.find_treasury_signals(conn, ignore_alert_state=True)
            + cluster.find_etf_flow_signals(conn, ignore_alert_state=True))


def replay(conn, start: dt.date, end: dt.date, t212) -> dict[str, dict]:
    weeks: dict[str, dict] = {}
    seen: set[tuple] = set()
    originals = [(m, m.dt) for m in _FROZEN_MODULES]
    try:
        day = start
        while day <= end:
            for m in _FROZEN_MODULES:
                m.dt = _frozen_dt(day)
            _shadow(conn, day)
            sel = strategy.select(conn, _signals(conn), t212, today=day)
            iso = day.isocalendar()
            week = weeks.setdefault(f"{iso.year}-W{iso.week:02d}", {"strong": [], "candidates": []})
            for tier, items in (("strong", sel.strong), ("candidates", sel.candidates)):
                for t in items:
                    key = (t.signal.source, t.signal.ticker, tier)
                    if key not in seen:
                        seen.add(key)
                        week[tier].append(t.signal.ticker)
            day += dt.timedelta(days=1)
    finally:
        for m, original in originals:
            m.dt = original
        _unshadow(conn)
    return weeks


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=35)
    args = ap.parse_args()
    mem = db.connect(":memory:")
    src = sqlite3.connect(DB_PATH)
    src.backup(mem)
    src.close()
    t212 = trading212.availability(mem)
    end = dt.date.today()
    weeks = replay(mem, end - dt.timedelta(days=args.days), end, t212)
    print(f"Replay {args.days} days; Trading 212 filter: {'on' if t212 else 'OFF (no key)'}; "
          f"market caps are today's (approximation).\n")
    print(f"{'week':<10} {'strong':>6} {'cand.':>6}  strong tickers")
    for week, w in sorted(weeks.items()):
        print(f"{week:<10} {len(w['strong']):>6} {len(w['candidates']):>6}  "
              f"{', '.join(w['strong'])}")
    print("\nTarget: 2-3 strong and 5-10 candidates a week.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add calibrate_strategy.py tests/test_calibrate.py
git commit -m "feat: calibrate_strategy.py replays the tiers over stored history

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 6: Run the replay and STOP for the user**

Run: `.venv/bin/python calibrate_strategy.py --days 35`

This reaches the network for market caps of tickers not yet cached, and takes a few
minutes the first time. Show the weekly table to the user with a one-line reading
against the 2–3 / 5–10 target.

If the counts are far off, propose specific changes and show the effect of each by re-running
with the constant changed. Candidate changes: `STRONG_MIN_INSIDERS` (3), `CONVICTION_MIN_EUR`
(250k) and `CONVICTION_MIN_INCREASE_PCT` (10). **Do not change the constants in
`strategy.py` until the user approves the numbers**; then commit that change on its own:
`tune(strategy): …`.
