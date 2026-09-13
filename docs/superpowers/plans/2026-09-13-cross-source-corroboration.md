# Cross-source corroboration signal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect when a ticker draws signals from more than one independent
disclosure regime within 30 days, feed that into `score_signal`'s attention
ranking, and surface it both in the live Telegram alert and in the
`research.py` ticker dossier.

**Architecture:** A new `cluster.find_corroboration()` mutates each signal's
new `corroborated_by` list field in place, unioning this run's own batch with
recent `signal_journal` history. `score_signal` takes it as an optional
argument and adds a capped bonus. `signal_journal` gains a matching column.
`telegram_notify.py`'s three formatters and `research.py`'s dossier both
render the fact as plain co-occurrence, never agreement.

**Tech Stack:** Pure stdlib + SQLite (existing `db.py` conventions), no new
dependencies.

**Spec:** `docs/superpowers/specs/2026-09-13-cross-source-corroboration-design.md`

## Global Constraints

- **No verdict, no direction claim, ever.** Every rendered line says only that
  another independent disclosure regime had activity on this ticker within
  the window — never "confirms," "agrees," or any buy/sell framing. This
  holds even though exits count on either side (a buy and a sell on the same
  ticker do corroborate each other under this design).
- **No new data source, no network in the detection step.** `find_corroboration`
  reads only `signal_journal` (SQL) and the in-memory signal list already
  produced by this run — no scraping, no new API, no LLM calls.
- **The score bonus is an unvalidated starting point**, same standing as every
  other weight in `score_signal` (its docstring already says as much).
- **Every existing test keeps passing unchanged.** `corroborated_by` and the
  `score_signal` parameter both default to empty/`None`, so nothing that
  doesn't pass them sees any behavior change.

---

### Task 1: Cross-source detection (`cluster.find_corroboration`)

**Files:**
- Modify: `cluster.py` (new constants near line 192, new field on `ClusterSignal` ~line 222, `StakeSignal` ~line 680, `ExitSignal` ~line 954, new function before `enrich_signals` ~line 807)
- Test: `tests/test_signals.py`

**Interfaces:**
- Produces: `CORROBORATION_WINDOW_DAYS: int`, `ClusterSignal.corroborated_by` / `StakeSignal.corroborated_by` / `ExitSignal.corroborated_by: list[str]` (all default `[]`), `find_corroboration(conn, signals: list, window_days: int = CORROBORATION_WINDOW_DAYS) -> None` (mutates `signals` in place)

- [ ] **Step 1: Write the failing tests**

Add `add_senate_txn` to the existing `from conftest import (...)` line at the
top of `tests/test_signals.py` (it currently imports `add_form_144,
add_house_txn, add_sec_purchase, add_sec_sale, add_stake` — add
`add_senate_txn` to that list).

Append to `tests/test_signals.py`:

```python
# --------------------------------------------------------- corroboration
def _journal_row(conn, ticker, source, days_ago=0, kind="cluster"):
    """Insert a signal_journal row with a controlled age, for testing the
    corroboration window boundary -- db.journal_signal() always stamps
    emitted_at as "now", so a backdated row needs raw SQL."""
    when = (TODAY - dt.timedelta(days=days_ago)).isoformat()
    conn.execute(
        "INSERT INTO signal_journal (source, ticker, kind, emitted_at) VALUES (?, ?, ?, ?)",
        (source, ticker, kind, when),
    )
    conn.commit()


def test_corroboration_handles_an_empty_signal_list(conn):
    cluster.find_corroboration(conn, [])  # must not raise


def test_corroboration_matches_another_source_in_the_same_batch(conn):
    add_sec_purchase(conn, "AAA", "Buyer One", 300_000, RECENT)
    add_sec_purchase(conn, "AAA", "Buyer Two", 300_000, RECENT)
    add_senate_txn(conn, "AAA", "Sen. One", "$50,001 - $100,000")
    signals = cluster.find_sec_clusters(conn) + cluster.find_senate_clusters(conn)
    cluster.find_corroboration(conn, signals)
    by_source = {s.source: s.corroborated_by for s in signals}
    assert by_source["SEC"] == ["SENATE"]
    assert by_source["SENATE"] == ["SEC"]


def test_corroboration_matches_journal_history_within_the_window(conn):
    add_sec_purchase(conn, "AAA", "Buyer One", 300_000, RECENT)
    add_sec_purchase(conn, "AAA", "Buyer Two", 300_000, RECENT)
    _journal_row(conn, "AAA", "BAFIN", days_ago=10)
    signals = cluster.find_sec_clusters(conn)
    cluster.find_corroboration(conn, signals)
    assert signals[0].corroborated_by == ["BAFIN"]


def test_corroboration_ignores_journal_history_outside_the_window(conn):
    add_sec_purchase(conn, "AAA", "Buyer One", 300_000, RECENT)
    add_sec_purchase(conn, "AAA", "Buyer Two", 300_000, RECENT)
    _journal_row(conn, "AAA", "BAFIN", days_ago=cluster.CORROBORATION_WINDOW_DAYS + 5)
    signals = cluster.find_sec_clusters(conn)
    cluster.find_corroboration(conn, signals)
    assert signals[0].corroborated_by == []


def test_corroboration_is_empty_with_no_other_activity(conn):
    add_sec_purchase(conn, "AAA", "Buyer One", 300_000, RECENT)
    add_sec_purchase(conn, "AAA", "Buyer Two", 300_000, RECENT)
    signals = cluster.find_sec_clusters(conn)
    cluster.find_corroboration(conn, signals)
    assert signals[0].corroborated_by == []


def test_corroboration_deduplicates_repeated_sources(conn):
    """A second SEC-sourced journal row must not make SEC corroborate itself --
    same disclosure regime, not another one."""
    add_sec_purchase(conn, "AAA", "Buyer One", 300_000, RECENT)
    add_sec_purchase(conn, "AAA", "Buyer Two", 300_000, RECENT)
    _journal_row(conn, "AAA", "SEC", days_ago=5)
    signals = cluster.find_sec_clusters(conn)
    cluster.find_corroboration(conn, signals)
    assert signals[0].corroborated_by == []


def test_corroboration_includes_exit_signals(conn):
    """Exits count on either side -- co-occurrence, not agreement. See
    cluster.find_corroboration's docstring."""
    add_sec_purchase(conn, "AAA", "Buyer One", 300_000, RECENT)
    add_sec_purchase(conn, "AAA", "Buyer Two", 300_000, RECENT)
    _journal_row(conn, "AAA", "HOUSE", days_ago=3, kind="exit")
    signals = cluster.find_sec_clusters(conn)
    cluster.find_corroboration(conn, signals)
    assert signals[0].corroborated_by == ["HOUSE"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/Desktop/disclosure-bot && .venv/bin/python -m pytest tests/test_signals.py -v -k corroboration`
Expected: FAIL with `AttributeError: module 'cluster' has no attribute 'find_corroboration'`

- [ ] **Step 3: Add the constants, the fields, and `find_corroboration` to `cluster.py`**

Insert after the existing `MAX_CREDIBLE_PCT_OF_MCAP = 100.0` line (~line 192)
and before `@dataclass\nclass ClusterSignal:`:

```python
# Corroboration: does another, independent disclosure regime also show activity
# on this ticker recently? Not a claim that sources agree -- an exit on one
# regime and a buy on another both count, see find_corroboration below and the
# README's "Ранжирование сигналов" section.
CORROBORATION_WINDOW_DAYS = 30
W_PER_CORROBORATING_SOURCE = 15.0   # per distinct other source active on this ticker
CAP_CORROBORATION = 30.0            # caps at 2 corroborating sources' worth
```

In `ClusterSignal`, add after the existing `score: float = 0.0` line:

```python
    corroborated_by: list[str] = field(default_factory=list)
```

In `StakeSignal`, add after the existing `url: str` line (before the blank
line and `@property`):

```python
    corroborated_by: list[str] = field(default_factory=list)
```

In `ExitSignal`, add after the existing `seller_names: list[str] =
field(default_factory=list)` line:

```python
    corroborated_by: list[str] = field(default_factory=list)
```

Add this function directly above `def enrich_signals(conn, signals: list) ->
list:` (~line 807):

```python
def find_corroboration(conn, signals: list, window_days: int = CORROBORATION_WINDOW_DAYS) -> None:
    """Which OTHER disclosure-source regimes also show activity on each
    signal's ticker within `window_days` -- this run's own batch, plus
    signal_journal history. Sets `.corroborated_by` on every signal in place
    to the sorted list of those other sources ([] when there are none).

    Not a claim the sources agree: any signal kind counts on either side, so a
    buy-side cluster and an exit signal on the same ticker corroborate each
    other just as two buy-side clusters would -- "another independent regime
    had activity here", nothing about direction. Pure SQL + set logic, no
    network, so it runs before the market-cap/liquidity loop in
    enrich_signals() that does need the network.
    """
    tickers = sorted({sig.ticker for sig in signals})
    if not tickers:
        return

    batch_sources: dict[str, set[str]] = {t: set() for t in tickers}
    for sig in signals:
        batch_sources[sig.ticker].add(sig.source)

    since = (dt.date.today() - dt.timedelta(days=window_days)).isoformat()
    placeholders = ",".join("?" * len(tickers))
    rows = conn.execute(
        f"SELECT DISTINCT ticker, source FROM signal_journal "
        f"WHERE ticker IN ({placeholders}) AND emitted_at >= ?",
        (*tickers, since),
    ).fetchall()
    journal_sources: dict[str, set[str]] = {t: set() for t in tickers}
    for ticker, source in rows:
        journal_sources[ticker].add(source)

    for sig in signals:
        others = (batch_sources[sig.ticker] | journal_sources[sig.ticker]) - {sig.source}
        sig.corroborated_by = sorted(others)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_signals.py -v -k corroboration`
Expected: all PASS

- [ ] **Step 5: Run the full suite to confirm no regressions**

Run: `.venv/bin/python -m pytest -q`
Expected: all PASS (263 + 7 new = 270)

- [ ] **Step 6: Commit**

```bash
git add cluster.py tests/test_signals.py
git commit -m "feat(cluster): detect cross-source signal corroboration"
```

---

### Task 2: Scoring integration (`score_signal` + `enrich_signals`)

**Files:**
- Modify: `cluster.py` `score_signal()` (~line 753-804), `enrich_signals()` (~line 807-829)
- Test: `tests/test_scoring.py`

**Interfaces:**
- Consumes: `find_corroboration`, `CORROBORATION_WINDOW_DAYS`, `W_PER_CORROBORATING_SOURCE`, `CAP_CORROBORATION`, `ClusterSignal.corroborated_by` / `StakeSignal.corroborated_by` / `ExitSignal.corroborated_by` (Task 1)
- Produces: `score_signal(sig, corroborated_by: list[str] | None = None) -> float` (changed signature); `enrich_signals` now sets `.corroborated_by` on every signal before scoring

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_scoring.py`:

```python
# ----------------------------------------------------------- corroboration
def test_corroboration_bonus_is_added_to_the_base_score():
    plain = cluster.score_signal(_signal())
    corroborated = cluster.score_signal(_signal(), corroborated_by=["SENATE"])
    assert corroborated > plain


def test_corroboration_bonus_scales_with_distinct_sources_then_caps():
    one = cluster.score_signal(_signal(), corroborated_by=["SENATE"])
    two = cluster.score_signal(_signal(), corroborated_by=["SENATE", "BAFIN"])
    many = cluster.score_signal(_signal(), corroborated_by=["SENATE", "BAFIN", "NORWAY", "SWEDEN"])
    assert one < two
    assert two == many  # capped


def test_corroboration_defaults_to_no_bonus():
    assert cluster.score_signal(_signal()) == cluster.score_signal(_signal(), corroborated_by=None)
    assert cluster.score_signal(_signal()) == cluster.score_signal(_signal(), corroborated_by=[])


def test_corroboration_bonus_applies_to_stake_and_exit_signals_too():
    stake = cluster.StakeSignal(source="SEC13DG", ticker="A", company="C", person="P",
                                 form_type="SCHEDULE 13D", percent=9.0, prev_percent=None,
                                 amount_owned=1, event_date="", url="")
    assert cluster.score_signal(stake, corroborated_by=["SENATE"]) > cluster.score_signal(stake)
    assert (cluster.score_signal(_exit(), corroborated_by=["SENATE"])
            > cluster.score_signal(_exit()))


def test_corroboration_bonus_stays_within_the_existing_bound(conn):
    """test_every_component_is_bounded's <250 ceiling, plus the capped
    corroboration bonus (30) on top, must still be a sane, bounded number."""
    extreme = _signal(buyer_count=500, value_pct_of_mcap=99.0, position_increase_pct=1000.0,
                      first_buy=True, lag_days=0, market_cap_eur=1e9, avg_daily_value=1e9)
    score = cluster.score_signal(extreme, corroborated_by=["SEC", "SENATE", "HOUSE", "BAFIN", "NORWAY"])
    assert score < 280


def test_enrich_signals_sets_corroboration_and_bonus_score(conn):
    a = _signal(ticker="AAA", source="SEC")
    b = cluster.ClusterSignal(source="SENATE", ticker="AAA", company="Test", buyer_count=1,
                               total_value=100_000, members=[], window_start="", window_end="")
    signals = cluster.enrich_signals(conn, [a, b])
    by_source = {s.source: s for s in signals}
    assert by_source["SEC"].corroborated_by == ["SENATE"]
    assert by_source["SENATE"].corroborated_by == ["SEC"]
    assert by_source["SEC"].score > cluster.score_signal(_signal(ticker="AAA", source="SEC"))
```

`_exit()` and `_signal()` are the existing local factories already defined
earlier in this file (lines 89 and 183) — reuse them, don't redefine.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_scoring.py -v -k corroboration`
Expected: FAIL with `TypeError: score_signal() got an unexpected keyword argument 'corroborated_by'`

- [ ] **Step 3: Restructure `score_signal` and wire `enrich_signals`**

Replace `score_signal`'s body. The three existing branches (`StakeSignal`,
`ExitSignal`, the general `ClusterSignal` path) currently each end with their
own early `return round(score, 1)` — restructure to `if/elif/else` so there is
one shared tail that adds the corroboration bonus regardless of which branch
ran:

```python
def score_signal(sig, corroborated_by: list[str] | None = None) -> float:
    """A number for ORDERING signals by how much attention they deserve.

    This is emphatically not a prediction of return, and a higher score is not a
    claim that something is a better trade. It exists because the last run emitted
    252 signals as a flat list, which is the same as emitting none: whatever was
    most notable was buried among things that were not.

    The components restate the reasoning the tool was built on -- several unrelated
    insiders converging beats one; the people who run a company know more about it
    than a passive holder does; a given sum means more against a small company than
    a large one; a stale disclosure is worth less than a fresh one; an independent
    disclosure regime noticing the same ticker is more than that regime noticing it
    twice (see find_corroboration) -- and the weights are a starting point, not a
    finding. backtest.py exists to check them.
    """
    if hasattr(sig, "percent"):   # StakeSignal
        # For a 13D/G the share of the company IS the headline, so it carries the
        # score directly rather than being one input among several.
        score = min(sig.percent, 50.0) * 2.0
        if sig.is_activist:
            score += 25.0   # 13D means the holder may seek to influence control
        if sig.prev_percent is not None:
            score += min(max(sig.percent - sig.prev_percent, 0.0), 20.0) * 2.0
    elif hasattr(sig, "seller_count"):   # ExitSignal
        # Exit signals carry none of the buy-side context (officer status, % of
        # market cap, freshness) the general path below scores on -- they are a
        # different kind of event. Score on what they do carry: how many sellers,
        # and whether every buyer in the cluster has now sold.
        score = min(W_PER_EXTRA_BUYER * max(0, sig.seller_count - 1), CAP_BUYERS)
        if sig.total_buyers and sig.seller_count >= sig.total_buyers:
            score += W_FULL_UNWIND
    else:
        score = 0.0
        score += min(W_PER_EXTRA_BUYER * max(0, (sig.buyer_count or 1) - 1), CAP_BUYERS)
        if not getattr(sig, "holder_only", False):
            score += W_HAS_OFFICER
        pct_mcap = sig.value_pct_of_mcap
        if pct_mcap and pct_mcap <= MAX_CREDIBLE_PCT_OF_MCAP:
            score += min(W_PCT_OF_MARKET_CAP * (pct_mcap / 0.1), CAP_MARKET_CAP)
        if sig.position_increase_pct:
            score += min(W_POSITION_INCREASE * (sig.position_increase_pct / 25.0), CAP_POSITION)
        if getattr(sig, "first_buy", False):
            score += W_FIRST_BUY
        if sig.lag_days is not None:
            score += W_FRESH * max(0.0, 1.0 - sig.lag_days / FRESH_DECAY_DAYS)
        if sig.market_cap_eur is None:
            score += P_UNKNOWN_SIZE
        if sig.avg_daily_value is not None and sig.avg_daily_value < ILLIQUID_BELOW_EUR:
            score += P_ILLIQUID

    if corroborated_by:
        score += min(W_PER_CORROBORATING_SOURCE * len(corroborated_by), CAP_CORROBORATION)
    return round(score, 1)
```

In `enrich_signals`, add a call to `find_corroboration` before the per-signal
loop, and pass `sig.corroborated_by` into `score_signal`:

```python
def enrich_signals(conn, signals: list) -> list:
    """Attach company context to signals and score them, newest-first by score.

    Split out from the finders on purpose: this is the only part that needs the
    network (market cap and traded volume), so the finders stay pure SQL and the
    test suite stays offline and fast.
    """
    import marketcap

    find_corroboration(conn, signals)

    for sig in signals:
        cap = marketcap.market_cap_eur(conn, sig.ticker)
        facts = marketcap.facts(conn, sig.ticker) or {}
        sig.market_cap_eur = cap
        sig.avg_daily_value = (fx.to_eur(facts["avg_daily_value"], facts.get("currency") or "USD", conn)
                                if facts.get("avg_daily_value") else None)
        total = getattr(sig, "total_value", None)
        pct = (total / cap * 100) if (cap and total) else None
        sig.value_pct_of_mcap = pct
        sig.score = score_signal(sig, sig.corroborated_by)
    signals.sort(key=lambda x: getattr(x, "score", 0.0), reverse=True)
    return signals
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_scoring.py -v`
Expected: all PASS, including every pre-existing test in this file
(`corroborated_by` defaults to `None` everywhere it isn't passed, so no
existing assertion changes)

- [ ] **Step 5: Run the full suite to confirm no regressions**

Run: `.venv/bin/python -m pytest -q`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add cluster.py tests/test_scoring.py
git commit -m "feat(cluster): score signals higher when another source corroborates them"
```

---

### Task 3: Storage — `signal_journal` column and `_signal_features`

**Files:**
- Modify: `db.py` (`SCHEMA`'s `signal_journal` table ~line 175-197, `_ADDED_COLUMNS` ~line 356-367, `journal_signal()`'s `cols` tuple ~line 635-638)
- Modify: `bot.py` `_signal_features()` (~line 624-658)
- Test: `tests/test_scoring.py`

**Interfaces:**
- Consumes: `ClusterSignal.corroborated_by` / etc. (Task 1)
- Produces: `signal_journal.corroborated_by` column (JSON array of strings); `_signal_features(sig)["corroborated_by"]`

- [ ] **Step 1: Write the failing test**

Add `import json` near the top of `tests/test_scoring.py` (alongside the
existing `import datetime as dt`). Append:

```python
def test_corroborated_by_round_trips_through_the_journal(conn):
    import bot
    add_sec_purchase(conn, "AAA", "Buyer One", 300_000, RECENT)
    add_sec_purchase(conn, "AAA", "Buyer Two", 300_000, RECENT)
    sig = cluster.find_sec_clusters(conn)[0]
    sig.corroborated_by = ["SENATE", "BAFIN"]
    db.journal_signal(conn, bot._signal_features(sig))
    row = conn.execute("SELECT corroborated_by FROM signal_journal").fetchone()
    assert json.loads(row[0]) == ["SENATE", "BAFIN"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_scoring.py -v -k round_trips_through_the_journal`
Expected: FAIL — either `sqlite3.OperationalError: table signal_journal has no
column named corroborated_by` (if the dict key made it into `_signal_features`
first) or `assert None == [...]` (if the schema exists but the column isn't in
`journal_signal`'s `cols` tuple). Either failure confirms the gap; both parts
below are required regardless of which one shows up first.

- [ ] **Step 3: Add the column, the migration entry, and the `cols` tuple entry in `db.py`**

In `SCHEMA`, change the end of the `signal_journal` table definition from:

```sql
    window_start    TEXT,
    window_end      TEXT,
    members         TEXT      -- JSON array of names
);
```

to:

```sql
    window_start    TEXT,
    window_end      TEXT,
    members         TEXT,     -- JSON array of names
    corroborated_by TEXT      -- JSON array of other source values active on this ticker within the window
);
```

In `_ADDED_COLUMNS`, add a new entry so existing databases (including the
project's own `data/disclosures.db`) pick up the column via `_migrate()`:

```python
    ("company_facts", "avg_daily_value", "REAL"),
    ("signal_journal", "corroborated_by", "TEXT"),
]
```

In `journal_signal()`, add the new column to the hardcoded `cols` tuple —
required, not automatic, since that tuple (not the row dict's keys) decides
what actually gets inserted:

```python
    cols = ("source", "kind", "ticker", "company", "buyer_count", "total_value_eur",
            "holder_only", "has_officer", "position_increase_pct", "first_buy",
            "lag_days", "market_cap_eur", "value_pct_of_mcap", "percent_of_class",
            "score", "window_start", "window_end", "members", "corroborated_by")
```

- [ ] **Step 4: Add the key to `bot.py`'s `_signal_features`**

Change:

```python
        "window_start": str(getattr(sig, "window_start", "") or ""),
        "window_end": str(getattr(sig, "window_end", "") or ""),
        "members": json.dumps(list(members or []), ensure_ascii=False),
    }
```

to:

```python
        "window_start": str(getattr(sig, "window_start", "") or ""),
        "window_end": str(getattr(sig, "window_end", "") or ""),
        "members": json.dumps(list(members or []), ensure_ascii=False),
        "corroborated_by": json.dumps(getattr(sig, "corroborated_by", None) or [], ensure_ascii=False),
    }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_scoring.py -v`
Expected: all PASS, including `test_journalled_signal_round_trips` (the
pre-existing one) unchanged

- [ ] **Step 6: Run the full suite to confirm no regressions**

Run: `.venv/bin/python -m pytest -q`
Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add db.py bot.py tests/test_scoring.py
git commit -m "feat(db): journal which sources corroborated each signal"
```

---

### Task 4: Live alert surface (`telegram_notify.py`)

**Files:**
- Modify: `telegram_notify.py` `format_signal()` (~line 219-256), `format_exit_signal()` (~line 297-309), `format_stake_signal()` (~line 312-334)
- Test: `tests/test_telegram_notify.py`

**Interfaces:**
- Consumes: `ClusterSignal.corroborated_by` / `StakeSignal.corroborated_by` / `ExitSignal.corroborated_by` (Task 1)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_telegram_notify.py` (the `_cluster()`, `_exit()`,
`_stake()` factories at the top of this file already exist — reuse them):

```python
# ------------------------------------------------------ corroborated_by
#
# cluster.find_corroboration() sets `.corroborated_by` on a signal after the
# fact (empty list when nothing else has fired on that ticker); the formatters
# must render it plainly -- not italicized, unlike market_note, since this is
# the bot's own data rather than a third party's read -- and simply omit the
# line when the list is empty.

def test_format_signal_renders_corroborated_by():
    sig = _cluster()
    sig.corroborated_by = ["SENATE", "BAFIN"]
    text = tn.format_signal(sig, html=True)
    assert "SENATE, BAFIN" in text
    assert "<i>SENATE" not in text


def test_format_signal_omits_corroboration_line_when_empty():
    text = tn.format_signal(_cluster(), html=True)
    assert "🔗" not in text


def test_format_exit_signal_renders_corroborated_by():
    sig = _exit()
    sig.corroborated_by = ["SEC"]
    text = tn.format_exit_signal(sig, html=True)
    assert "🔗" in text and "SEC" in text


def test_format_stake_signal_renders_corroborated_by():
    sig = _stake()
    sig.corroborated_by = ["SEC"]
    text = tn.format_stake_signal(sig, html=True)
    assert "🔗" in text and "SEC" in text


def test_corroboration_line_never_claims_agreement():
    sig = _cluster()
    sig.corroborated_by = ["SENATE"]
    text = tn.format_signal(sig, html=True).lower()
    for bad in ("подтверждают", "согласны", "confirms", "agrees"):
        assert bad not in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_telegram_notify.py -v -k corroborat`
Expected: FAIL — `🔗` not found in any output (the attribute is read via
`getattr` so this fails as a missing-line assertion, not a crash)

- [ ] **Step 3: Add the corroboration line to all three formatters**

In `format_signal`, change:

```python
    note = getattr(sig, "market_note", None)
    if note:
        lines.append(f"   {'<i>' + _esc(note) + '</i>' if html else note}")
    return "\n".join(lines)
```

to:

```python
    corr = getattr(sig, "corroborated_by", None)
    if corr:
        text = f"Другие источники по этому тикеру: {', '.join(corr)}"
        lines.append(f"   🔗 {_esc(text) if html else text}")
    note = getattr(sig, "market_note", None)
    if note:
        lines.append(f"   {'<i>' + _esc(note) + '</i>' if html else note}")
    return "\n".join(lines)
```

In `format_exit_signal`, change:

```python
    for l in sig.lines:
        lines.append(f"   • {_esc(l) if html else l}")
    note = getattr(sig, "market_note", None)
    if note:
        lines.append(f"   {'<i>' + _esc(note) + '</i>' if html else note}")
    return "\n".join(lines)
```

to:

```python
    for l in sig.lines:
        lines.append(f"   • {_esc(l) if html else l}")
    corr = getattr(sig, "corroborated_by", None)
    if corr:
        text = f"Другие источники по этому тикеру: {', '.join(corr)}"
        lines.append(f"   🔗 {_esc(text) if html else text}")
    note = getattr(sig, "market_note", None)
    if note:
        lines.append(f"   {'<i>' + _esc(note) + '</i>' if html else note}")
    return "\n".join(lines)
```

In `format_stake_signal`, change:

```python
    lines.append(f"   {_esc(detail) if html else detail}")
    note = getattr(sig, "market_note", None)
    if note:
        lines.append(f"   {'<i>' + _esc(note) + '</i>' if html else note}")
    if html:
        lines.append(f'   <a href="{_esc(sig.url)}">источник</a>')
```

to:

```python
    lines.append(f"   {_esc(detail) if html else detail}")
    corr = getattr(sig, "corroborated_by", None)
    if corr:
        text = f"Другие источники по этому тикеру: {', '.join(corr)}"
        lines.append(f"   🔗 {_esc(text) if html else text}")
    note = getattr(sig, "market_note", None)
    if note:
        lines.append(f"   {'<i>' + _esc(note) + '</i>' if html else note}")
    if html:
        lines.append(f'   <a href="{_esc(sig.url)}">источник</a>')
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_telegram_notify.py -v`
Expected: all PASS, including every pre-existing test in this file

- [ ] **Step 5: Run the full suite to confirm no regressions**

Run: `.venv/bin/python -m pytest -q`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add telegram_notify.py tests/test_telegram_notify.py
git commit -m "feat(telegram_notify): show cross-source corroboration in alerts"
```

---

### Task 5: Dossier surface (`research.py`)

**Files:**
- Modify: `research.py` (new `import cluster` near line 58-64, new `corroboration_summary()` near `past_signals` ~line 164-169, `build()` ~line 749, `format_report()` ~line 855-859)
- Test: `tests/test_research.py`

**Interfaces:**
- Consumes: `cluster.CORROBORATION_WINDOW_DAYS` (Task 1), `signal_journal.corroborated_by`-bearing rows (Task 3, though this function only reads `source`/`ticker`/`emitted_at`, not the new column itself)
- Produces: `corroboration_summary(conn, ticker: str, window_days: int = cluster.CORROBORATION_WINDOW_DAYS) -> dict | None` (keys: `"all_sources"`, `"recent_sources"`, both `list[str]`); `rep["corroboration"]`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_research.py`:

```python
def test_corroboration_summary_none_with_fewer_than_two_sources(conn):
    db.journal_signal(conn, {"source": "SEC", "kind": "cluster", "ticker": "AAA"})
    assert research.corroboration_summary(conn, "AAA") is None


def test_corroboration_summary_lists_distinct_sources(conn):
    db.journal_signal(conn, {"source": "SEC", "kind": "cluster", "ticker": "AAA"})
    db.journal_signal(conn, {"source": "SENATE", "kind": "cluster", "ticker": "AAA"})
    summary = research.corroboration_summary(conn, "AAA")
    assert summary["all_sources"] == ["SEC", "SENATE"]
    assert summary["recent_sources"] == ["SEC", "SENATE"]


def test_corroboration_summary_excludes_aged_out_sources_from_recent(conn):
    conn.execute(
        "INSERT INTO signal_journal (source, ticker, kind, emitted_at) VALUES (?, ?, ?, ?)",
        ("BAFIN", "AAA", "cluster", "2020-01-01"),
    )
    db.journal_signal(conn, {"source": "SEC", "kind": "cluster", "ticker": "AAA"})
    summary = research.corroboration_summary(conn, "AAA")
    assert summary["all_sources"] == ["BAFIN", "SEC"]
    assert summary["recent_sources"] == ["SEC"]


def test_format_report_shows_the_corroboration_summary_when_present(conn, monkeypatch):
    db.journal_signal(conn, {"source": "SEC", "kind": "cluster", "ticker": "AAPL"})
    monkeypatch.setattr(research, "corroboration_summary",
                        lambda conn, ticker: {"all_sources": ["SEC", "SENATE"],
                                                "recent_sources": ["SEC", "SENATE"]})
    text = research.format_report(research.build(conn, "AAPL"))
    assert "Независимые источники по этому тикеру: SEC, SENATE" in text


def test_format_report_omits_the_corroboration_summary_when_absent(conn, monkeypatch):
    db.journal_signal(conn, {"source": "SEC", "kind": "cluster", "ticker": "AAPL"})
    monkeypatch.setattr(research, "corroboration_summary", lambda conn, ticker: None)
    text = research.format_report(research.build(conn, "AAPL"))
    assert "Независимые источники" not in text
    assert "Сигналы, которые бот уже присылал" in text
```

Note: `test_format_report_shows_the_corroboration_summary_when_present` and
`test_format_report_omits_the_corroboration_summary_when_absent` go through
`research.build(conn, "AAPL")`, same as the existing `annual_report` dossier
tests in this file — this reaches real yfinance/TradingView/SEC network calls
(this test file's own docstring says the network parts are deliberately *not*
covered, but the existing `annual_report` tests already established this
exact pattern; follow it for consistency rather than introduce a second
convention).

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_research.py -v -k corroboration`
Expected: FAIL with `AttributeError: module 'research' has no attribute
'corroboration_summary'`

- [ ] **Step 3: Add `import cluster` and `corroboration_summary()` to `research.py`**

Add to the import block (alongside `annual_report`, `db`, `datefmt`, `fx`,
`marketcap`, `termstyle`, `tradingview`):

```python
import cluster
```

Add directly above `def past_signals(conn, ticker: str) -> list:`:

```python
def corroboration_summary(conn, ticker: str,
                           window_days: int = cluster.CORROBORATION_WINDOW_DAYS) -> dict | None:
    """Distinct disclosure-source regimes that have recorded a signal on this
    ticker, all-time and within the trailing `window_days`. None when fewer
    than 2 distinct sources exist all-time -- nothing to report."""
    all_sources = sorted({r[0] for r in conn.execute(
        "SELECT DISTINCT source FROM signal_journal WHERE ticker = ?", (ticker,),
    ).fetchall()})
    if len(all_sources) < 2:
        return None
    since = (dt.date.today() - dt.timedelta(days=window_days)).isoformat()
    recent_sources = sorted({r[0] for r in conn.execute(
        "SELECT DISTINCT source FROM signal_journal WHERE ticker = ? AND emitted_at >= ?",
        (ticker, since),
    ).fetchall()})
    return {"all_sources": all_sources, "recent_sources": recent_sources}
```

- [ ] **Step 4: Wire it into `build()` and `format_report()`**

In `build()`, change:

```python
        "political": political_trades(conn, ticker),
        "signals": past_signals(conn, ticker),
        "prices": prices,
```

to:

```python
        "political": political_trades(conn, ticker),
        "signals": past_signals(conn, ticker),
        "corroboration": corroboration_summary(conn, ticker),
        "prices": prices,
```

In `format_report()`, change:

```python
    if rep["signals"]:
        L.append("\n" + termstyle.section("Сигналы, которые бот уже присылал"))
        for (when, source, kind, buyers, value, score) in rep["signals"][:8]:
            L.append(f"  {when[:10]}  {source} {kind}  {buyers} чел.  "
                     f"€{value or 0:,.0f}  {score or 0:.0f} баллов")
```

to:

```python
    if rep["signals"]:
        L.append("\n" + termstyle.section("Сигналы, которые бот уже присылал"))
        corr = rep.get("corroboration")
        if corr:
            line = "Независимые источники по этому тикеру: " + ", ".join(corr["all_sources"])
            if corr["recent_sources"] and corr["recent_sources"] != corr["all_sources"]:
                line += (f" (за последние {cluster.CORROBORATION_WINDOW_DAYS} дней: "
                         + ", ".join(corr["recent_sources"]) + ")")
            L.append(f"  {line}")
        for (when, source, kind, buyers, value, score) in rep["signals"][:8]:
            L.append(f"  {when[:10]}  {source} {kind}  {buyers} чел.  "
                     f"€{value or 0:,.0f}  {score or 0:.0f} баллов")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_research.py -v`
Expected: all PASS, including every pre-existing test in this file

- [ ] **Step 6: Run the full suite to confirm no regressions**

Run: `.venv/bin/python -m pytest -q`
Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add research.py tests/test_research.py
git commit -m "feat(research): show cross-source corroboration in the ticker dossier"
```

---

### Task 6: README and final verification

**Files:**
- Modify: `README.md` (`## Ранжирование сигналов` table ~line 376-385, dossier bullet ~line 317)

- [ ] **Step 1: Add a row to the scoring table**

In the `## Ранжирование сигналов` section, change:

```markdown
| Свежесть раскрытия | Form 4 приходит через ~2 дня, PTR Палаты — в среднем через 14 |
| −штраф: неизвестный размер | Неизвестно ≠ мало |
| −штраф: низкая ликвидность | Бумага с оборотом €0.2 млн/день — это другая история, чем €30 млн/день |
```

to:

```markdown
| Свежесть раскрытия | Form 4 приходит через ~2 дня, PTR Палаты — в среднем через 14 |
| Сигнал уже есть от другого источника | Независимая система раскрытия — инсайдеры, политики, другой регулятор — тоже заметила эту бумагу за последние 30 дней |
| −штраф: неизвестный размер | Неизвестно ≠ мало |
| −штраф: низкая ликвидность | Бумага с оборотом €0.2 млн/день — это другая история, чем €30 млн/день |
```

Directly after the existing "Каждая компонента ограничена сверху..."
paragraph (before the "Фильтры: `--min-score N`..." line), add:

```markdown
**Совпадение по источникам — тоже факт, а не мнение.** Если по одной и той же
бумаге за последние 30 дней уже отметилась независимая система раскрытия — не
только SEC, а, например, ещё и Сенат — сигнал получает бонус к баллу
(`cluster.find_corroboration`), а в самом алерте появляется строка «🔗 Другие
источники по этому тикеру: ...». Это не заявление, что источники согласны:
выход одного инсайдера и покупка другого на той же бумаге тоже считаются
совпадением — просто «здесь была активность по независимому каналу», не
больше. Досье по тикеру показывает то же самое за всю историю, в разделе
«Сигналы, которые бот уже присылал».
```

- [ ] **Step 2: Extend the dossier bullet**

Change:

```markdown
- какие сигналы бот уже присылал и с каким баллом;
```

to:

```markdown
- какие сигналы бот уже присылал и с каким баллом, и по скольким независимым
  источникам эта бумага вообще засветилась за последние 30 дней;
```

- [ ] **Step 3: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all PASS, no regressions (baseline before this plan: 263 passed)

- [ ] **Step 4: Manual verification against real data**

```bash
cd ~/Desktop/disclosure-bot && source .venv/bin/activate
python bot.py --once --no-telegram        # confirms find_corroboration runs on real signals without crashing
python -c "
import db, research
conn = db.connect('data/disclosures.db')
print(research.format_report(research.build(conn, 'AAPL')))
"                                          # dossier renders (or omits) the corroboration line without error
grep -rn 'CORROBORATION_WINDOW_DAYS' *.py # confirm cluster.py, research.py agree on one definition
```

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "docs(README): document cross-source corroboration"
```

---

## Manual verification (after all tasks)

```bash
cd ~/Desktop/disclosure-bot && source .venv/bin/activate
pytest tests/ -q                                      # all green
python bot.py --once --no-telegram                     # a real daily run, unaffected in shape, corroboration line
                                                         # appears on any ticker with 2+ active source regimes
python -c "
import db, research
conn = db.connect('data/disclosures.db')
print(research.format_report(research.build(conn, 'AAPL')))
"                                                        # dossier shows the corroboration summary if AAPL qualifies
```

Success: pytest green; `score_signal`'s existing tests are all unchanged in
behavior; a synthetic two-source scenario (used throughout the tests above)
shows the corroboration line in both the Telegram-format output and the
dossier; a single-source ticker shows neither; `signal_journal.corroborated_by`
round-trips correctly; no verdict/agreement language appears anywhere in the
new output.
