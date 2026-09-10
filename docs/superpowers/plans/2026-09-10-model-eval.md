# Predictive-skill evaluation (`model_eval.py`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a standalone `model_eval.py` that fits a classifier to predict "does this disclosed purchase's stock beat SPY over the next month" and evaluates it with walk-forward cross-validation, reporting out-of-sample skill honestly and never emitting a per-ticker call.

**Architecture:** New standalone module `model_eval.py` (network-touching, like `backtest.py`, and separate for the same reason). Two labelled-row collectors live in `backtest.py` and are shared. `model_eval` turns rows into a feature matrix, splits by calendar month into expanding-window walk-forward folds, fits logistic regression + gradient boosting + a one-feature baseline, and prints a plain-text report. Nothing in the project imports `model_eval`.

**Tech Stack:** Python 3.12, scikit-learn (new), numpy + pandas (present), sqlite3, yfinance (present).

**Spec:** `docs/superpowers/specs/2026-09-10-model-eval-design.md` — read it alongside this plan.

## Global Constraints

- **Python 3.12+** (`.python-version` pins 3.12.14).
- **Tests are 100% offline** — no network, no sklearn fit on real DB data. Synthetic frames and monkeypatched fetchers only. The existing suite (`pytest tests/ -q`) must stay green and fast.
- **Safety boundary (binding):** `model_eval.py` never scores a live/current signal, never writes `signal_journal`, never touches `bot.py` or Telegram, never persists a model. No project module imports it (enforced by a test). The report contains no "buy"/"sell"/"will rise"/target-price line.
- **New dependency:** `scikit-learn` added to `requirements.txt` and `requirements-dev.txt`. Regenerating `requirements.lock` is best-effort (see Task 1).
- **`backtest.py --purchases` output must not change** — the new `extended_features` parameter defaults to `False`.
- **Report body is English**, matching `backtest._report` (plain text, ~72-col rules, stdout). Only the `menu.py` prompt is Russian.
- **Entry point is the disclosure date, never the trade date.**
- **Gate defaults:** `--min-rows 80`, `--min-test 12`, `--folds 3`, `--horizon 21`.
- Not a git repo. If you want the commit steps to run, `git init` first; otherwise skip every "Commit" step. Commit trailer: follow the session's current attribution reminder.

---

### Task 1: scikit-learn dependency + `add_house_txn` test helper

**Files:**
- Modify: `requirements.txt`
- Modify: `requirements-dev.txt`
- Modify: `requirements.lock` (best-effort)
- Modify: `tests/conftest.py` (append a helper)

**Interfaces:**
- Consumes: nothing.
- Produces: `sklearn` importable in the venv; `conftest.add_house_txn(conn, ticker, member, amount_range, date="09/03/2026", notification_date="09/20/2026", txn_type="P")` — inserts one `house_purchases` row. Dates are `M/D/YYYY` (House format).

- [ ] **Step 1: Add the dependency lines**

`requirements.txt` — append:
```
# model_eval.py: logistic regression + gradient boosting for walk-forward
# evaluation of predictive skill. Pulls scipy, joblib, threadpoolctl.
scikit-learn>=1.6
```
`requirements-dev.txt` — it currently reads `-r requirements.txt` then `pytest>=8.0`; no change needed (it inherits the line above). Leave it, but confirm it still starts with `-r requirements.txt`.

- [ ] **Step 2: Install and verify**

Run:
```bash
cd ~/Desktop/disclosure-bot && .venv/bin/pip install 'scikit-learn>=1.6'
.venv/bin/python -c "import sklearn, scipy, joblib; print(sklearn.__version__)"
```
Expected: a version string ≥ 1.5, no error.

- [ ] **Step 3: Regenerate the lock file (best-effort)**

Run:
```bash
cd ~/Desktop/disclosure-bot && .venv/bin/pip freeze > /tmp/freeze.txt && head -3 requirements.lock
```
Then hand-merge: keep the existing header comment block of `requirements.lock`, replace the pinned package list below it with the contents of `/tmp/freeze.txt` (which now includes `scikit-learn`, `scipy`, `joblib`, `threadpoolctl`). If the lock file's format is just `name==version` lines, append the four new pins:
```
joblib==<version from freeze>
scikit-learn==<version from freeze>
scipy==<version from freeze>
threadpoolctl==<version from freeze>
```
Keep it sorted if the file is sorted. This step is best-effort — the venv is already correct; the lock is documentation.

- [ ] **Step 4: Add the `add_house_txn` conftest helper**

Append to `tests/conftest.py` (after `add_senate_txn`):
```python
def add_house_txn(conn, ticker, member, amount_range, date="09/03/2026",
                  notification_date="09/20/2026", txn_type="P", asset=None,
                  state_district="CA-12", doc_id=None):
    """House PTR row. Dates are M/D/YYYY here (House PDF format), unlike every
    other table."""
    conn.execute(
        """INSERT INTO house_purchases
           (doc_id, member_name, state_district, owner, asset, ticker, txn_type,
            txn_date, notification_date, amount_range, source_url)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (doc_id or f"doc-{ticker}-{member}-{date}-{amount_range}", member,
         state_district, "Self", asset or f"{ticker} Inc", ticker, txn_type,
         date, notification_date, amount_range,
         f"https://example.test/house/{ticker}"),
    )
    conn.commit()
```

- [ ] **Step 5: Verify the helper works**

Run:
```bash
cd ~/Desktop/disclosure-bot && .venv/bin/python -c "
import tests.conftest as c
" 2>/dev/null; .venv/bin/python -m pytest tests/ -q
```
Expected: existing suite still green (135 passed), no collection error from conftest.

- [ ] **Step 6: Commit**

```bash
git add requirements.txt requirements.lock tests/conftest.py
git commit -m "build: add scikit-learn for model_eval; add_house_txn test helper"
```

---

### Task 2: backtest price helpers — `_excess_return` and `_return_before`

**Files:**
- Modify: `backtest.py` (add two functions near `_series` / `forward_returns`)
- Test: `tests/test_backtest_helpers.py` (new)

**Interfaces:**
- Consumes: `backtest.BENCHMARK`, `backtest._SERIES_CACHE`.
- Produces:
  - `backtest._excess_return(prices, bench, days: int) -> float | None` — pure. `prices` and `bench` are pandas Series (or None). Returns `(prices pct change over the last `days` bars) − (bench pct change over the same bars)`, or `None` if either series is missing or shorter than `days + 1`.
  - `backtest._return_before(ticker: str, date: str, days: int = 63) -> float | None` — network wrapper; fetches a wide window ending at `date` and calls `_excess_return`. Cached in `_SERIES_CACHE` under key `("_before", ticker, date, days)`. Not unit-tested (network).

- [ ] **Step 1: Write the failing test**

`tests/test_backtest_helpers.py`:
```python
"""backtest.py's small price helpers. The network wrappers (_series,
_return_before) are not tested; the pure math (_excess_return) is."""
from __future__ import annotations

import pandas as pd
import pytest

import backtest


def _series(values):
    return pd.Series(values, dtype=float)


def test_excess_return_is_ticker_minus_benchmark_over_the_window():
    prices = _series([100, 101, 102, 110])   # +10% over 3 bars
    bench = _series([100, 100, 100, 104])     # +4% over 3 bars
    assert backtest._excess_return(prices, bench, days=3) == pytest.approx(6.0)


def test_excess_return_none_when_a_series_is_missing():
    assert backtest._excess_return(None, _series([1, 2, 3, 4]), days=3) is None
    assert backtest._excess_return(_series([1, 2, 3, 4]), None, days=3) is None


def test_excess_return_none_when_history_too_short():
    prices = _series([100, 110])
    bench = _series([100, 104])
    assert backtest._excess_return(prices, bench, days=3) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/Desktop/disclosure-bot && .venv/bin/python -m pytest tests/test_backtest_helpers.py -q`
Expected: FAIL — `AttributeError: module 'backtest' has no attribute '_excess_return'`.

- [ ] **Step 3: Implement both helpers**

In `backtest.py`, immediately after the `forward_returns` function:
```python
def _excess_return(prices, bench, days: int) -> float | None:
    """Ticker return over the last `days` bars minus the benchmark's, in points.
    None if either series is absent or has <= `days` bars."""
    if prices is None or bench is None or len(prices) <= days or len(bench) <= days:
        return None
    r = (float(prices.iloc[-1]) / float(prices.iloc[-1 - days]) - 1) * 100
    b = (float(bench.iloc[-1]) / float(bench.iloc[-1 - days]) - 1) * 100
    return r - b


def _return_before(ticker: str, date: str, days: int = 63) -> float | None:
    """Ticker vs benchmark over the `days` trading days ENDING at `date`.

    `_series` only fetches forward from a date, so this fetches a window ending at
    `date` and slices it. Cached like `_series` -- prices before a past date don't
    change, but the process cache is fine and consistent with the rest of the file.
    """
    import yfinance as yf
    key = ("_before", ticker, date, days)
    if key in _SERIES_CACHE:
        return _SERIES_CACHE[key]
    out = None
    try:
        start = (dt.date.fromisoformat(date) - dt.timedelta(days=days * 2 + 40)).isoformat()
        end = (dt.date.fromisoformat(date) + dt.timedelta(days=1)).isoformat()
        px = yf.Ticker(ticker.replace(".", "-")).history(start=start, end=end)["Close"].dropna()
        spy = yf.Ticker(BENCHMARK).history(start=start, end=end)["Close"].dropna()
        out = _excess_return(px, spy, days)
    except Exception:
        pass
    _SERIES_CACHE[key] = out
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/Desktop/disclosure-bot && .venv/bin/python -m pytest tests/test_backtest_helpers.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Regression check**

Run: `cd ~/Desktop/disclosure-bot && .venv/bin/python -m pytest tests/ -q`
Expected: full suite green.

- [ ] **Step 6: Commit**

```bash
git add backtest.py tests/test_backtest_helpers.py
git commit -m "feat(backtest): _excess_return / _return_before price helpers"
```

---

### Task 3: `backtest.collect_political_trades()`

**Files:**
- Modify: `backtest.py` (add `_mdy_to_iso` + `collect_political_trades`; import `cluster`)
- Test: `tests/test_backtest_helpers.py` (extend)

**Interfaces:**
- Consumes: `backtest.forward_returns`, `cluster._JUNK_TICKER_SQL`, `conftest.add_house_txn`.
- Produces:
  - `backtest._mdy_to_iso(s: str | None) -> str | None` — `"9/3/2026"` or `"09/03/2026"` → `"2026-09-03"`, else `None`.
  - `backtest.collect_political_trades(conn, horizon: int = 21, since_days: int = 1200) -> list[dict]` — one dict per labelled House `P` row: keys `ticker, member, chamber, disclosure_date` (ISO), `trade_date` (ISO), `amount_range` (str), `label` (0/1). Drops rows with no ticker/junk ticker, no `notification_date`, disclosure older than `since_days`, or no computable `horizon`-day excess return. `chamber` is always `"house"` for now (Senate has no disclosure-date column).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_backtest_helpers.py`:
```python
from conftest import add_house_txn


@pytest.mark.parametrize("raw,iso", [
    ("9/3/2026", "2026-09-03"), ("09/03/2026", "2026-09-03"),
    ("12/31/2025", "2025-12-31"), ("", None), ("not a date", None), (None, None),
])
def test_mdy_to_iso(raw, iso):
    assert backtest._mdy_to_iso(raw) == iso


def test_collect_political_trades_labels_by_beat_or_miss(conn, monkeypatch):
    add_house_txn(conn, "AAA", "Rep A", "$1,001 - $15,000",
                  date="06/01/2025", notification_date="06/15/2025")
    add_house_txn(conn, "BBB", "Rep B", "$50,001 - $100,000",
                  date="06/01/2025", notification_date="06/15/2025")

    def fake_forward(ticker, start, horizons):
        return {21: {"return": 5.0, "excess": 3.0 if ticker == "AAA" else -2.0}}
    monkeypatch.setattr(backtest, "forward_returns", fake_forward)

    rows = backtest.collect_political_trades(conn, horizon=21, since_days=100000)
    by_ticker = {r["ticker"]: r for r in rows}
    assert by_ticker["AAA"]["label"] == 1
    assert by_ticker["BBB"]["label"] == 0
    assert by_ticker["AAA"]["disclosure_date"] == "2025-06-15"
    assert by_ticker["AAA"]["trade_date"] == "2025-06-01"
    assert by_ticker["AAA"]["chamber"] == "house"


def test_collect_political_trades_drops_unlabelable_and_junk(conn, monkeypatch):
    add_house_txn(conn, "AAA", "Rep A", "$1,001 - $15,000", notification_date="06/15/2025")
    add_house_txn(conn, "NONE", "Rep C", "$1,001 - $15,000", notification_date="06/15/2025")
    add_house_txn(conn, "DDD", "Rep D", "$1,001 - $15,000", notification_date="")

    monkeypatch.setattr(backtest, "forward_returns",
                        lambda t, s, h: {21: {"return": 1.0, "excess": 1.0}} if t == "AAA" else None)
    rows = backtest.collect_political_trades(conn, horizon=21, since_days=100000)
    assert [r["ticker"] for r in rows] == ["AAA"]   # NONE = junk, DDD = no disclosure date, others unlabelable
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd ~/Desktop/disclosure-bot && .venv/bin/python -m pytest tests/test_backtest_helpers.py -q`
Expected: FAIL — `_mdy_to_iso` / `collect_political_trades` not defined.

- [ ] **Step 3: Implement**

In `backtest.py`: add `import cluster` to the import block (after `import db`). Then add, next to `collect_purchases`:
```python
def _mdy_to_iso(s: str | None) -> str | None:
    """House dates are M/D/YYYY (zero-padded or not) -> ISO, else None."""
    try:
        return dt.datetime.strptime((s or "").strip(), "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


def collect_political_trades(conn, horizon: int = 21, since_days: int = 1200) -> list[dict]:
    """House PTR *purchases* with a beat-the-benchmark label at `horizon` trading
    days after the DISCLOSURE date. See model_eval.py.

    House only in practice: `house_purchases.notification_date` is the disclosure
    date; `senate_purchases` has no disclosure-date column, so Senate rows are
    excluded until that scraper records one.
    """
    since = (dt.date.today() - dt.timedelta(days=since_days)).isoformat()
    rows = conn.execute(
        f"""SELECT ticker, member_name, txn_date, notification_date, amount_range
            FROM house_purchases
            WHERE txn_type = 'P' AND ticker IS NOT NULL AND ticker != ''
              AND notification_date != '' AND {cluster._JUNK_TICKER_SQL}""",
    ).fetchall()
    out = []
    for ticker, member, txn_date, notif_date, amount_range in rows:
        disc = _mdy_to_iso(notif_date)
        if not disc or disc < since:
            continue
        fr = forward_returns(ticker, disc, (horizon,))
        if not fr or horizon not in fr or fr[horizon].get("excess") is None:
            continue
        out.append({
            "ticker": ticker,
            "member": member,
            "chamber": "house",
            "disclosure_date": disc,
            "trade_date": _mdy_to_iso(txn_date) or disc,
            "amount_range": amount_range or "",
            "label": 1 if fr[horizon]["excess"] > 0 else 0,
        })
    return out
```

- [ ] **Step 4: Run to verify pass**

Run: `cd ~/Desktop/disclosure-bot && .venv/bin/python -m pytest tests/test_backtest_helpers.py -q`
Expected: PASS.

- [ ] **Step 5: Regression + a real smoke check**

Run:
```bash
cd ~/Desktop/disclosure-bot && .venv/bin/python -m pytest tests/ -q
.venv/bin/python -c "
import warnings, logging; warnings.filterwarnings('ignore'); logging.getLogger('yfinance').setLevel(logging.CRITICAL)
import db, backtest
c = db.connect('data/disclosures.db')
r = backtest.collect_political_trades(c, horizon=21)
print(len(r), 'labelled political rows; base rate', round(sum(x['label'] for x in r)/max(len(r),1), 2))
"
```
Expected: suite green; ~120–170 rows, base rate near 0.5. (Slow — fetches price history.)

- [ ] **Step 6: Commit**

```bash
git add backtest.py tests/test_backtest_helpers.py
git commit -m "feat(backtest): collect_political_trades — labelled House PTR purchases"
```

---

### Task 4: `collect_purchases(*, extended_features=False)`

**Files:**
- Modify: `backtest.py` (`collect_purchases` gets a keyword-only param)
- Test: `tests/test_backtest_helpers.py` (extend)

**Interfaces:**
- Consumes: existing `collect_purchases` internals, `conftest.add_sec_purchase`.
- Produces: `backtest.collect_purchases(conn, horizons=HORIZONS, since_days=365, *, extended_features=False)`. Unchanged when `extended_features=False`. When `True`: each returned row additionally has `disclosure_date` (ISO, = `filed or transaction_date`), `label` (0/1 from `horizons[0]`'s excess), `owner` (already present), `shares` (float, 0 if null), `owned_after` (float or None); rows lacking `horizons[0]` or its `excess` are dropped.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_backtest_helpers.py`:
```python
from conftest import add_sec_purchase


def test_collect_purchases_default_shape_unchanged(conn, monkeypatch):
    add_sec_purchase(conn, "AAA", "Buyer", 200_000, "2026-09-01")
    monkeypatch.setattr(backtest, "forward_returns",
                        lambda t, s, h: {1: {"return": 2.0, "excess": 1.0},
                                          21: {"return": 3.0, "excess": 2.0}})
    rows = backtest.collect_purchases(conn, (1, 21), since_days=100000)
    assert rows and "returns" in rows[0]
    assert "label" not in rows[0] and "disclosure_date" not in rows[0]


def test_collect_purchases_extended_adds_label_and_features(conn, monkeypatch):
    add_sec_purchase(conn, "AAA", "Winner", 200_000, "2026-09-01",
                     shares=100, shares_owned_after=1100, filed_date="2026-09-03")
    add_sec_purchase(conn, "BBB", "Loser", 200_000, "2026-09-01",
                     shares=50, shares_owned_after=50, filed_date="2026-09-02")
    monkeypatch.setattr(backtest, "forward_returns",
                        lambda t, s, h: {21: {"return": 3.0,
                                              "excess": 4.0 if t == "AAA" else -1.0}})
    rows = {r["owner"]: r for r in
            backtest.collect_purchases(conn, (21,), since_days=100000, extended_features=True)}
    assert rows["Winner"]["label"] == 1 and rows["Loser"]["label"] == 0
    assert rows["Winner"]["disclosure_date"] == "2026-09-03"
    assert rows["Winner"]["trade_date"] == "2026-09-01"
    assert rows["Winner"]["shares"] == 100 and rows["Winner"]["owned_after"] == 1100
    assert rows["Loser"]["owned_after"] == 50
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd ~/Desktop/disclosure-bot && .venv/bin/python -m pytest tests/test_backtest_helpers.py -k collect_purchases -q`
Expected: FAIL — `extended_features` is an unexpected keyword / `label` missing.

- [ ] **Step 3: Implement**

Replace `collect_purchases` in `backtest.py` with:
```python
def collect_purchases(conn, horizons=HORIZONS, since_days: int = 365, *,
                       extended_features: bool = False) -> list[dict]:
    """Individual SEC insider purchases with their outcomes.

    `extended_features=True` is for model_eval: it additionally selects the raw
    fields the model features need (shares / shares_owned_after), attaches a single
    binary `label` from `horizons[0]`, and an ISO `disclosure_date`. It does not
    change the default (`--purchases`) output at all.
    """
    since = (dt.date.today() - dt.timedelta(days=since_days)).isoformat()
    cols = ("ticker, owner_name, transaction_date, value, is_officer, is_director, "
            "is_ten_pct_owner, derivative, COALESCE(is_10b5_1, 0), filed_date")
    if extended_features:
        cols += ", shares, shares_owned_after"
    rows = conn.execute(
        f"""SELECT {cols} FROM sec_purchases
            WHERE transaction_date >= ? AND ticker IS NOT NULL AND ticker != ''
            ORDER BY transaction_date""",
        (since,),
    ).fetchall()
    out = []
    for row in rows:
        (ticker, owner, txn_date, value, officer, director, ten_pct, deriv,
         is_10b5_1, filed) = row[:10]
        entry = filed or txn_date
        fr = forward_returns(ticker, entry, horizons)
        if not fr:
            continue
        rec = {
            "ticker": ticker, "owner": owner, "value": value or 0,
            "role": ("officer/director" if (officer or director)
                     else "10% holder" if ten_pct else "other"),
            "derivative": bool(deriv), "is_10b5_1": bool(is_10b5_1),
            "returns": fr,
        }
        if extended_features:
            h = horizons[0]
            if h not in fr or fr[h].get("excess") is None:
                continue
            shares, owned_after = row[10], row[11]
            rec.update(
                disclosure_date=entry,
                trade_date=txn_date,          # raw transaction_date, for lag_days
                label=1 if fr[h]["excess"] > 0 else 0,
                shares=float(shares or 0),
                owned_after=None if owned_after is None else float(owned_after),
            )
        out.append(rec)
    return out
```

- [ ] **Step 4: Run to verify pass**

Run: `cd ~/Desktop/disclosure-bot && .venv/bin/python -m pytest tests/test_backtest_helpers.py -q`
Expected: PASS.

- [ ] **Step 5: Prove `--purchases` is unchanged**

Run:
```bash
cd ~/Desktop/disclosure-bot && .venv/bin/python -m pytest tests/ -q
.venv/bin/python backtest.py --purchases --horizon 5 2>/dev/null | md5sum
```
Expected: suite green. Record the md5; it must match a run from before this task (or eyeball that the report still prints the same group tables).

- [ ] **Step 6: Commit**

```bash
git add backtest.py tests/test_backtest_helpers.py
git commit -m "feat(backtest): collect_purchases extended_features for model_eval"
```

---

### Task 5: `model_eval.build_feature_frame()` + cross-row features

**Files:**
- Create: `model_eval.py` (module skeleton + `build_feature_frame`, `_cluster_counts`, `_prior_hitrate`, `_COLUMNS`)
- Test: `tests/test_model_eval.py` (new)

**Interfaces:**
- Consumes: `backtest._return_before`, `cluster._position_increase`, `cluster._is_first_buy`, `marketcap.market_cap_eur`, `marketcap.size_bucket`.
- Produces:
  - `model_eval._COLUMNS: dict[str, tuple[list[str], list[str], str]]` — `corpus -> (numeric_cols, categorical_cols, baseline_col)`.
  - `model_eval._cluster_counts(rows, window_days, id_key) -> list[int]` — pure. For each row, the number of *distinct other* `id_key` values among rows with the same `ticker` whose `disclosure_date` is in `[D − window_days, D]`.
  - `model_eval._prior_hitrate(rows, horizon, id_key) -> list[float]` — pure. For each row, the mean `label` of earlier rows with the same `id_key` whose `disclosure_date` ≤ `D − 2·horizon` calendar days. `float("nan")` when there are none.
  - `model_eval.build_feature_frame(conn, rows, corpus, horizon) -> tuple[pd.DataFrame, np.ndarray, list[str]]` — `(X, y, dates)`. `X` columns are exactly `_COLUMNS[corpus][0] + _COLUMNS[corpus][1]`. `y` is the `label` array. `dates` is the parallel list of `disclosure_date` strings.

- [ ] **Step 1: Write the failing tests**

`tests/test_model_eval.py`:
```python
"""model_eval.py -- offline. No sklearn fit on real data, no network.
Synthetic row lists and monkeypatched fetchers throughout."""
from __future__ import annotations

import math

import numpy as np
import pytest

import model_eval


def _prow(ticker, member, disc, label, amount="$1,001 - $15,000"):
    return {"ticker": ticker, "member": member, "chamber": "house",
            "disclosure_date": disc, "trade_date": disc, "amount_range": amount,
            "label": label}


def test_cluster_counts_is_backward_looking_and_excludes_self():
    rows = [
        _prow("AAA", "Rep A", "2025-06-01", 1),
        _prow("AAA", "Rep B", "2025-06-10", 0),   # 1 other (Rep A) in the prior 30d
        _prow("AAA", "Rep A", "2025-06-20", 1),   # others = {Rep B} -> 1 (self excluded)
        _prow("AAA", "Rep C", "2025-09-01", 0),   # A/B trades are >30d back -> 0
    ]
    assert model_eval._cluster_counts(rows, window_days=30, id_key="member") == [0, 1, 1, 0]


def test_prior_hitrate_only_uses_settled_earlier_trades():
    # Chronological on purpose: the feature is date-based (a member's trades whose
    # own 2*horizon-day settle window closed before D), so order in the list must
    # not matter -- these rows happen to be sorted, but the logic does not rely on it.
    rows = [
        _prow("T1", "Rep A", "2025-01-01", 1),
        _prow("T2", "Rep A", "2025-02-01", 0),   # Jan is 31d back (< 2*21=42) -> not settled -> nan
        _prow("T3", "Rep A", "2025-03-01", 1),   # Jan settled (59d), Feb not (28d) -> mean(1) = 1.0
        _prow("T4", "Rep A", "2025-06-01", 0),   # Jan/Feb/Mar all settled -> mean(1,0,1) = 2/3
        _prow("T5", "Rep Z", "2025-06-01", 1),   # no prior Rep Z trade -> nan
    ]
    hr = model_eval._prior_hitrate(rows, horizon=21, id_key="member")
    assert math.isnan(hr[0])
    assert math.isnan(hr[1])
    assert hr[2] == pytest.approx(1.0)
    assert hr[3] == pytest.approx(2 / 3)
    assert math.isnan(hr[4])


def test_prior_hitrate_excludes_a_trade_disclosed_10_days_earlier():
    rows = [
        _prow("AAA", "Rep A", "2025-06-01", 1),
        _prow("BBB", "Rep A", "2025-06-11", 0),   # only 10 days later -> not settled -> nan
    ]
    hr = model_eval._prior_hitrate(rows, horizon=21, id_key="member")
    assert math.isnan(hr[1])


def test_build_feature_frame_politicians_shape(conn, monkeypatch):
    monkeypatch.setattr(model_eval.backtest, "_return_before", lambda t, d, days=63: 1.5)
    monkeypatch.setattr(model_eval.marketcap, "market_cap_eur", lambda c, t: 1e9)
    rows = [_prow("AAA", "Rep A", "2025-06-01", 1),
            _prow("BBB", "Rep B", "2025-06-02", 0),
            _prow("AAA", "Rep C", "2025-06-15", 1)]
    X, y, dates = model_eval.build_feature_frame(conn, rows, "politicians", horizon=21)
    num, cat, _ = model_eval._COLUMNS["politicians"]
    assert list(X.columns) == num + cat
    assert len(X) == 3 and list(y) == [1, 0, 1]
    assert dates == ["2025-06-01", "2025-06-02", "2025-06-15"]
    assert X["log_amount"].iloc[0] > 0
    assert X["mcap_bucket"].iloc[0] in ("small", "mid")


def test_build_feature_frame_insiders_shape(conn, monkeypatch):
    from conftest import add_sec_purchase
    monkeypatch.setattr(model_eval.backtest, "_return_before", lambda t, d, days=63: -2.0)
    monkeypatch.setattr(model_eval.marketcap, "market_cap_eur", lambda c, t: 5e9)
    rows = [
        {"ticker": "AAA", "owner": "Buyer One", "value": 250_000, "role": "officer/director",
         "disclosure_date": "2026-09-03", "trade_date": "2026-09-01",
         "shares": 100.0, "owned_after": 1100.0, "label": 1},
        {"ticker": "AAA", "owner": "Buyer Two", "value": 90_000, "role": "10% holder",
         "disclosure_date": "2026-09-04", "trade_date": "2026-09-02",
         "shares": 500.0, "owned_after": 500.0, "label": 0},
    ]
    X, y, dates = model_eval.build_feature_frame(conn, rows, "insiders", horizon=21)
    num, cat, _ = model_eval._COLUMNS["insiders"]
    assert list(X.columns) == num + cat
    assert list(y) == [1, 0]
    assert X["lag_days"].iloc[0] == 2
    assert X["position_increase_pct"].iloc[0] == pytest.approx(10.0)   # 100 into 1000 held
    assert X["position_increase_pct"].iloc[1] == pytest.approx(100.0)  # brand-new position
    assert X["role"].iloc[0] == "officer/director"
    assert X["is_first_buy"].iloc[0] in (0.0, 1.0)
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd ~/Desktop/disclosure-bot && .venv/bin/python -m pytest tests/test_model_eval.py -q`
Expected: FAIL — `No module named 'model_eval'`.

- [ ] **Step 3: Create the module skeleton + implement the three functions**

`model_eval.py`:
```python
"""Predictive-skill evaluation for disclosure-bot's signal features.

Fits a classifier to predict "does this disclosed purchase's stock beat SPY over
the next month" and evaluates it with walk-forward cross-validation. Reports
out-of-sample skill with the sample size on every line.

WHAT THIS IS NOT (binding -- see the spec):
  - No live scoring. Never attaches a probability to a current or future signal,
    never writes signal_journal, never touches bot.py or Telegram.
  - No persisted model. Refit every run, evaluation only. No predict(ticker).
  - Nothing in the project imports this module (there is a test for that).
  - No verdict, no forecast, no target price in the output.

The expected result on this project's data is "no detectable edge" -- a few
hundred rows from ~20 members is not enough to find one. Knowing that is the point.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import math
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

import numpy as np
import pandas as pd

import backtest
import cluster
import db
import marketcap

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "data" / "disclosures.db"

# corpus -> (numeric columns, categorical columns, the one-feature baseline column)
_COLUMNS = {
    "politicians": (
        ["log_amount", "lag_days", "cluster_size", "member_prior_hitrate", "price_vs_spy_63d"],
        ["chamber", "mcap_bucket"],
        "log_amount",
    ),
    "insiders": (
        ["log_value", "is_first_buy", "position_increase_pct", "lag_days",
         "cluster_size", "price_vs_spy_63d"],
        ["role", "mcap_bucket"],
        "log_value",
    ),
}

_CLUSTER_WINDOW = {"politicians": 30, "insiders": 14}
_ID_KEY = {"politicians": "member", "insiders": "owner"}


def _days_between(a: str, b: str) -> int:
    return (dt.date.fromisoformat(a) - dt.date.fromisoformat(b)).days


def _cluster_counts(rows: list[dict], window_days: int, id_key: str) -> list[int]:
    """For each row: distinct *other* id_key values buying the same ticker within
    [D - window_days, D]. Backward-looking -- never counts a later disclosure."""
    by_ticker: dict[str, list[tuple[str, str]]] = {}
    for r in rows:
        by_ticker.setdefault(r["ticker"], []).append((r["disclosure_date"], r[id_key]))
    out = []
    for r in rows:
        d, me = r["disclosure_date"], r[id_key]
        others = {
            who for (when, who) in by_ticker[r["ticker"]]
            if who != me and 0 <= _days_between(d, when) <= window_days
        }
        out.append(len(others))
    return out


def _prior_hitrate(rows: list[dict], horizon: int, id_key: str) -> list[float]:
    """For each row: mean label of earlier same-id rows whose own outcome window
    had settled by D. 'Settled' is approximated as disclosed at least 2*horizon
    calendar days before D -- strictly leak-free. nan when there are none."""
    settle = horizon * 2
    by_id: dict[str, list[tuple[str, int]]] = {}
    for r in rows:
        by_id.setdefault(r[id_key], []).append((r["disclosure_date"], r["label"]))
    out = []
    for r in rows:
        d = r["disclosure_date"]
        prior = [lab for (when, lab) in by_id[r[id_key]] if _days_between(d, when) >= settle]
        out.append(sum(prior) / len(prior) if prior else math.nan)
    return out


def build_feature_frame(conn, rows: list[dict], corpus: str, horizon: int):
    """rows -> (X: DataFrame, y: ndarray, dates: list[str]). Column order is
    exactly _COLUMNS[corpus] numeric then categorical."""
    numeric, categorical, _ = _COLUMNS[corpus]
    window = _CLUSTER_WINDOW[corpus]
    id_key = _ID_KEY[corpus]

    cluster_size = _cluster_counts(rows, window, id_key)
    prior_hr = _prior_hitrate(rows, horizon, id_key) if "member_prior_hitrate" in numeric else None

    records = []
    for i, r in enumerate(rows):
        d = r["disclosure_date"]
        feat = {
            "lag_days": _days_between(d, r["trade_date"]),   # both corpora carry trade_date
            "cluster_size": cluster_size[i],
            "price_vs_spy_63d": backtest._return_before(r["ticker"], d, 63),
            "mcap_bucket": marketcap.size_bucket(marketcap.market_cap_eur(conn, r["ticker"])),
        }
        if corpus == "politicians":
            feat["log_amount"] = math.log10(cluster.parse_amount_low(r["amount_range"]) + 1)
            feat["chamber"] = r["chamber"]
            feat["member_prior_hitrate"] = prior_hr[i]
        else:
            feat["log_value"] = math.log10((r.get("value") or 0) + 1)
            feat["role"] = r["role"]
            feat["is_first_buy"] = float(
                cluster._is_first_buy(conn, r["ticker"], [r["owner"]], d))
            feat["position_increase_pct"] = cluster._position_increase(
                {"shares": r.get("shares"), "owned_after": r.get("owned_after")})
        records.append(feat)

    X = pd.DataFrame(records, columns=numeric + categorical)
    y = np.array([r["label"] for r in rows], dtype=int)
    dates = [r["disclosure_date"] for r in rows]
    return X, y, dates
```

- [ ] **Step 4: Run to verify pass**

Run: `cd ~/Desktop/disclosure-bot && .venv/bin/python -m pytest tests/test_model_eval.py -q`
Expected: PASS (5 passed — 3 helper tests + both `build_feature_frame` shape tests).

- [ ] **Step 5: Regression**

Run: `cd ~/Desktop/disclosure-bot && .venv/bin/python -m pytest tests/ -q`
Expected: full suite green.

- [ ] **Step 6: Commit**

```bash
git add model_eval.py tests/test_model_eval.py
git commit -m "feat(model_eval): build_feature_frame + cross-row features"
```

---

### Task 6: `walk_forward_folds()` + `check_sufficiency()`

**Files:**
- Modify: `model_eval.py`
- Test: `tests/test_model_eval.py` (extend)

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `model_eval._add_months(ym: str, k: int) -> str` — `"2026-07", 5 -> "2026-12"`.
  - `model_eval.walk_forward_folds(dates: list[str], n_folds: int = 3, min_test: int = 12) -> list[tuple[list[int], list[int]]] | None` — `eligible` = months with ≥ `min_test` rows; needs `len(eligible) ≥ n_folds` and ≥ 2 distinct months before the first test month. Returns folds `(train_idx, test_idx)` oldest first, expanding train, or `None` if it can't be built.
  - `model_eval.check_sufficiency(dates, n_folds, min_rows, min_test, corpus, coverage_start: str) -> str | None` — abort message or `None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_model_eval.py`:
```python
def _dates(spec: dict[str, int]) -> list[str]:
    out = []
    for month, n in spec.items():
        out += [f"{month}-{(i % 27) + 1:02d}" for i in range(n)]
    return sorted(out)


def test_walk_forward_folds_expanding_no_overlap():
    dates = _dates({"2025-03": 15, "2025-04": 15, "2025-05": 15,
                    "2025-06": 15, "2025-07": 15})
    folds = model_eval.walk_forward_folds(dates, n_folds=3, min_test=12)
    assert folds is not None and len(folds) == 3
    for train_idx, test_idx in folds:
        assert not (set(train_idx) & set(test_idx))
        latest_train = max(dates[i] for i in train_idx)
        earliest_test = min(dates[i] for i in test_idx)
        assert latest_train[:7] < earliest_test[:7]      # train strictly before test month
    # test months are the last 3
    assert {min(dates[i] for i in f[1])[:7] for f in folds} == {"2025-05", "2025-06", "2025-07"}


def test_walk_forward_folds_skips_a_sparse_month_as_a_test_fold():
    dates = _dates({"2025-02": 15, "2025-03": 15, "2025-04": 15,
                    "2025-05": 15, "2025-06": 15, "2025-07": 4})   # July too thin
    folds = model_eval.walk_forward_folds(dates, n_folds=3, min_test=12)
    test_months = {min(dates[i] for i in f[1])[:7] for f in folds}
    assert test_months == {"2025-04", "2025-05", "2025-06"}   # July excluded


def test_walk_forward_folds_none_when_not_enough_months():
    dates = _dates({"2025-06": 30, "2025-07": 30})
    assert model_eval.walk_forward_folds(dates, n_folds=3, min_test=12) is None


def test_check_sufficiency_flags_too_few_rows():
    dates = _dates({"2026-07": 10, "2026-08": 10})
    msg = model_eval.check_sufficiency(dates, 3, 80, 12, "insiders", "2026-07")
    assert msg is not None and "INSUFFICIENT DATA for insiders" in msg
    assert "re-run around" in msg


def test_check_sufficiency_passes_a_healthy_political_set():
    dates = _dates({"2025-03": 20, "2025-04": 20, "2025-05": 20,
                    "2025-06": 20, "2025-07": 20})
    assert model_eval.check_sufficiency(dates, 3, 80, 12, "politicians", "2025-03") is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd ~/Desktop/disclosure-bot && .venv/bin/python -m pytest tests/test_model_eval.py -k "walk_forward or sufficiency" -q`
Expected: FAIL — functions not defined.

- [ ] **Step 3: Implement**

Add to `model_eval.py`:
```python
def _add_months(ym: str, k: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    total = y * 12 + (m - 1) + k
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def _months_of(dates: list[str]) -> list[str]:
    return sorted({d[:7] for d in dates})


def walk_forward_folds(dates: list[str], n_folds: int = 3, min_test: int = 12):
    """Expanding-window walk-forward folds by calendar month. Test folds are the
    last `n_folds` months that each have >= min_test rows; sparse months can still
    train a later fold but are never a test fold. None if it can't be built."""
    months = _months_of(dates)
    counts = {m: sum(1 for d in dates if d[:7] == m) for m in months}
    eligible = [m for m in months if counts[m] >= min_test]
    if len(eligible) < n_folds:
        return None
    test_months = eligible[-n_folds:]
    if sum(1 for m in months if m < test_months[0]) < 2:
        return None
    folds = []
    for tm in test_months:
        train_idx = [i for i, d in enumerate(dates) if d[:7] < tm]
        test_idx = [i for i, d in enumerate(dates) if d[:7] == tm]
        if not train_idx or not test_idx:
            return None
        folds.append((train_idx, test_idx))
    return folds


def check_sufficiency(dates: list[str], n_folds: int, min_rows: int, min_test: int,
                       corpus: str, coverage_start: str) -> str | None:
    n = len(dates)
    months = _months_of(dates)
    counts = {m: sum(1 for d in dates if d[:7] == m) for m in months}
    eligible = [m for m in months if counts[m] >= min_test]

    problems = []
    if n < min_rows:
        problems.append(f"{n} labelled rows (need >={min_rows})")
    if len(eligible) < n_folds:
        problems.append(f"{len(eligible)} eligible months of >={min_test} rows "
                        f"(need >={n_folds})")
    elif sum(1 for m in months if m < eligible[-n_folds]) < 2:
        problems.append("not enough history before the first test month")
    if not problems:
        return None

    lines = [f"INSUFFICIENT DATA for {corpus}.", "  " + "; ".join(problems) + "."]
    if months:
        rate = n / len(months)
        need = max(0, min_rows - n)
        eta_k = math.ceil(need / rate) if rate else 0
        if eta_k:
            lines.append(f"  Coverage since {coverage_start}; at ~{rate:.0f} rows/month, "
                         f"re-run around {_add_months(months[-1], eta_k)}.")
    return "\n".join(lines)
```

- [ ] **Step 4: Run to verify pass**

Run: `cd ~/Desktop/disclosure-bot && .venv/bin/python -m pytest tests/test_model_eval.py -q`
Expected: PASS.

- [ ] **Step 5: Regression**

Run: `cd ~/Desktop/disclosure-bot && .venv/bin/python -m pytest tests/ -q`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add model_eval.py tests/test_model_eval.py
git commit -m "feat(model_eval): walk_forward_folds + check_sufficiency gate"
```

---

### Task 7: metric helpers + `_interpret()`

**Files:**
- Modify: `model_eval.py`
- Test: `tests/test_model_eval.py` (extend)

**Interfaces:**
- Consumes: `sklearn.metrics.roc_auc_score`, `sklearn.metrics.brier_score_loss`.
- Produces:
  - `model_eval.pooled_auc(y_true, y_score) -> float | None` — `None` if `y_true` has one class.
  - `model_eval.brier(y_true, y_prob) -> float`.
  - `model_eval.calibration_deciles(y_true, y_prob) -> list[dict]` — up to 10 dicts `{bucket: "0.0-0.1", n, predicted, actual}` for non-empty probability deciles.
  - `model_eval.top_decile_precision(y_true, y_prob) -> float | None` — fraction of label=1 among the top 10% by `y_prob` (min 1 row); `None` if empty.
  - `model_eval._interpret(auc: float | None, n: int, folds: int) -> str` — the `>>>` line.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_model_eval.py`:
```python
def test_pooled_auc_perfect_and_none():
    assert model_eval.pooled_auc([0, 0, 1, 1], [0.1, 0.2, 0.8, 0.9]) == pytest.approx(1.0)
    assert model_eval.pooled_auc([1, 1, 1], [0.3, 0.6, 0.9]) is None


def test_top_decile_precision():
    y = [0] * 18 + [1, 1]
    p = [0.1] * 18 + [0.9, 0.95]        # the two 1s are the top decile
    assert model_eval.top_decile_precision(y, p) == pytest.approx(1.0)


def test_calibration_deciles_group_by_predicted_probability():
    y = [0, 0, 1, 1]
    p = [0.05, 0.05, 0.95, 0.95]
    dec = model_eval.calibration_deciles(y, p)
    lo = next(d for d in dec if d["bucket"] == "0.0-0.1")
    hi = next(d for d in dec if d["bucket"] == "0.9-1.0")
    assert lo["actual"] == pytest.approx(0.0) and hi["actual"] == pytest.approx(1.0)


@pytest.mark.parametrize("auc,fragment", [
    (0.50, "No detectable edge"), (0.53, "No detectable edge"),
    (0.62, "Nominal edge"), (0.38, "Worse than chance"), (None, "label variety"),
])
def test_interpret_bands(auc, fragment):
    assert fragment in model_eval._interpret(auc, n=150, folds=3)
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd ~/Desktop/disclosure-bot && .venv/bin/python -m pytest tests/test_model_eval.py -k "auc or decile or calibration or interpret" -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

Add to `model_eval.py`:
```python
def pooled_auc(y_true, y_score) -> float | None:
    from sklearn.metrics import roc_auc_score
    y_true = list(y_true)
    if len(set(y_true)) < 2:
        return None
    return float(roc_auc_score(y_true, list(y_score)))


def brier(y_true, y_prob) -> float:
    from sklearn.metrics import brier_score_loss
    return float(brier_score_loss(list(y_true), list(y_prob)))


def calibration_deciles(y_true, y_prob) -> list[dict]:
    y_true, y_prob = list(y_true), list(y_prob)
    out = []
    for b in range(10):
        lo, hi = b / 10, (b + 1) / 10
        idx = [i for i, p in enumerate(y_prob)
               if (lo <= p < hi) or (b == 9 and p == 1.0)]
        if not idx:
            continue
        out.append({
            "bucket": f"{lo:.1f}-{hi:.1f}",
            "n": len(idx),
            "predicted": sum(y_prob[i] for i in idx) / len(idx),
            "actual": sum(y_true[i] for i in idx) / len(idx),
        })
    return out


def top_decile_precision(y_true, y_prob) -> float | None:
    y_true, y_prob = list(y_true), list(y_prob)
    if not y_true:
        return None
    k = max(1, len(y_true) // 10)
    top = sorted(range(len(y_prob)), key=lambda i: y_prob[i], reverse=True)[:k]
    return sum(y_true[i] for i in top) / k


def _interpret(auc: float | None, n: int, folds: int) -> str:
    if auc is None:
        return ">>> Not enough label variety in the test folds to score."
    if 0.45 <= auc <= 0.55:
        return ">>> No detectable edge. AUC ~0.5 is a coin flip."
    if auc > 0.55:
        return (f">>> Nominal edge, but {folds} folds of ~{max(n // folds, 1)} rows -- "
                f"treat with suspicion until the sample is several times larger. "
                f"This is not a forecast.")
    return ">>> Worse than chance -- almost certainly noise at this n."
```

- [ ] **Step 4: Run to verify pass**

Run: `cd ~/Desktop/disclosure-bot && .venv/bin/python -m pytest tests/test_model_eval.py -q`
Expected: PASS.

- [ ] **Step 5: Regression**

Run: `cd ~/Desktop/disclosure-bot && .venv/bin/python -m pytest tests/ -q`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add model_eval.py tests/test_model_eval.py
git commit -m "feat(model_eval): metric helpers + interpretation bands"
```

---

### Task 8: `make_pipeline()` + `run_walk_forward()`

**Files:**
- Modify: `model_eval.py`
- Test: `tests/test_model_eval.py` (extend)

**Interfaces:**
- Consumes: `sklearn` (pipeline, compose, impute, preprocessing, linear_model, ensemble, inspection), `model_eval.pooled_auc / brier / calibration_deciles / top_decile_precision`.
- Produces:
  - `model_eval.make_pipeline(numeric: list[str], categorical: list[str], kind: str) -> sklearn.pipeline.Pipeline` — `kind` in `{"lr", "gbt", "baseline"}`. `baseline` = LR on `numeric` only (caller passes just the baseline column).
  - `model_eval.run_walk_forward(X, y, dates, folds, corpus) -> dict` — keys: `n` (pooled test rows), `base_rate`, `models` (`{"lr": {...}, "gbt": {...}, "baseline": {...}}` each with `auc`, `brier` (not for baseline), `per_fold_auc`, `train_auc`), `calibration` (from pooled LR probs), `top_decile` (LR), `lr_coefficients` (`list[tuple[str, float]]`, sorted by `|coef|` desc), `gbt_importance` (`list[tuple[str, float]]` from permutation importance on the last fold's test set).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_model_eval.py`:
```python
def _synthetic_frame(n=90, signal=True):
    rng = np.random.default_rng(0)
    months = ["2025-04", "2025-05", "2025-06", "2025-07", "2025-08"]
    dates, feat, y = [], [], []
    for i in range(n):
        m = months[i % len(months)]
        dates.append(f"{m}-{(i % 27) + 1:02d}")
        strong = rng.normal()
        noise = rng.normal()
        label = 1 if (strong if signal else rng.normal()) > 0 else 0
        feat.append({"log_amount": strong, "lag_days": noise, "cluster_size": 0,
                     "member_prior_hitrate": float("nan"), "price_vs_spy_63d": noise,
                     "chamber": "house", "mcap_bucket": "small"})
        y.append(label)
    X = pd.DataFrame(feat, columns=model_eval._COLUMNS["politicians"][0]
                                  + model_eval._COLUMNS["politicians"][1])
    return X, np.array(y), sorted(dates)


def test_run_walk_forward_recovers_a_planted_signal():
    X, y, dates = _synthetic_frame(signal=True)
    folds = model_eval.walk_forward_folds(dates, n_folds=3, min_test=12)
    res = model_eval.run_walk_forward(X, y, dates, folds, "politicians")
    assert res["models"]["lr"]["auc"] > 0.75          # log_amount ~ label by construction
    assert len(res["models"]["lr"]["per_fold_auc"]) == 3
    assert 0.0 <= res["base_rate"] <= 1.0
    assert res["lr_coefficients"][0][0] in X.columns
    assert res["calibration"] and "n" in res["calibration"][0]


def test_run_walk_forward_finds_nothing_in_noise():
    X, y, dates = _synthetic_frame(signal=False)
    folds = model_eval.walk_forward_folds(dates, n_folds=3, min_test=12)
    res = model_eval.run_walk_forward(X, y, dates, folds, "politicians")
    assert 0.30 < res["models"]["lr"]["auc"] < 0.70   # ~coin flip
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd ~/Desktop/disclosure-bot && .venv/bin/python -m pytest tests/test_model_eval.py -k run_walk_forward -q`
Expected: FAIL — `make_pipeline` / `run_walk_forward` not defined.

- [ ] **Step 3: Implement**

Add to `model_eval.py`:
```python
def make_pipeline(numeric: list[str], categorical: list[str], kind: str):
    from sklearn.compose import ColumnTransformer
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import OneHotEncoder, StandardScaler

    num = Pipeline([("impute", SimpleImputer(strategy="median", keep_empty_features=True)),
                    ("scale", StandardScaler())])
    transformers = [("num", num, numeric)]
    if categorical:
        cat = Pipeline([("impute", SimpleImputer(strategy="most_frequent")),
                        ("oh", OneHotEncoder(handle_unknown="ignore"))])
        transformers.append(("cat", cat, categorical))
    pre = ColumnTransformer(transformers)

    if kind == "gbt":
        clf = HistGradientBoostingClassifier(max_depth=3, max_iter=200,
                                             learning_rate=0.05, early_stopping=True,
                                             random_state=0)
    else:   # "lr" and "baseline"
        clf = LogisticRegression(class_weight="balanced", max_iter=1000)
    return Pipeline([("pre", pre), ("clf", clf)])


def _feature_names_out(pipe, numeric, categorical) -> list[str]:
    try:
        return list(pipe.named_steps["pre"].get_feature_names_out())
    except Exception:
        return numeric + categorical


def run_walk_forward(X, y, dates, folds, corpus: str) -> dict:
    from sklearn.inspection import permutation_importance

    numeric, categorical, baseline_col = _COLUMNS[corpus]
    specs = {
        "lr": (numeric, categorical, "lr"),
        "gbt": (numeric, categorical, "gbt"),
        "baseline": ([baseline_col], [], "baseline"),
    }
    result = {"n": 0, "base_rate": float(np.mean(y)), "models": {}}
    pooled_lr_true, pooled_lr_prob = [], []
    last_lr = last_gbt = None
    last_test_idx = folds[-1][1]

    for name, (num, cat, kind) in specs.items():
        per_fold, train_aucs, p_true, p_prob = [], [], [], []
        for train_idx, test_idx in folds:
            pipe = make_pipeline(num, cat, kind)
            pipe.fit(X.iloc[train_idx], y[train_idx])
            prob = pipe.predict_proba(X.iloc[test_idx])[:, 1]
            per_fold.append(pooled_auc(y[test_idx], prob))
            train_aucs.append(pooled_auc(y[train_idx],
                                          pipe.predict_proba(X.iloc[train_idx])[:, 1]))
            p_true.extend(y[test_idx].tolist())
            p_prob.extend(prob.tolist())
        valid_train = [a for a in train_aucs if a is not None]
        entry = {
            "auc": pooled_auc(p_true, p_prob),
            "per_fold_auc": per_fold,
            "train_auc": float(np.mean(valid_train)) if valid_train else None,
        }
        if name != "baseline":
            entry["brier"] = brier(p_true, p_prob)
        result["models"][name] = entry
        if name == "lr":
            pooled_lr_true, pooled_lr_prob = p_true, p_prob
            last_lr = pipe
        if name == "gbt":
            last_gbt = pipe

    result["n"] = len(pooled_lr_true)
    result["calibration"] = calibration_deciles(pooled_lr_true, pooled_lr_prob)
    result["top_decile"] = top_decile_precision(pooled_lr_true, pooled_lr_prob)

    # LR coefficients -- fit once on all rows, interpretation only.
    full = make_pipeline(numeric, categorical, "lr")
    full.fit(X, y)
    names = _feature_names_out(full, numeric, categorical)
    coefs = full.named_steps["clf"].coef_[0]
    result["lr_coefficients"] = sorted(
        zip(names, (float(c) for c in coefs)), key=lambda kv: abs(kv[1]), reverse=True)

    # GBT permutation importance on the last fold's test set.
    try:
        imp = permutation_importance(last_gbt, X.iloc[last_test_idx], y[last_test_idx],
                                     n_repeats=10, random_state=0, scoring="roc_auc")
        result["gbt_importance"] = sorted(
            zip(numeric + categorical, (float(v) for v in imp.importances_mean)),
            key=lambda kv: kv[1], reverse=True)
    except Exception:
        result["gbt_importance"] = []
    return result
```

- [ ] **Step 4: Run to verify pass**

Run: `cd ~/Desktop/disclosure-bot && .venv/bin/python -m pytest tests/test_model_eval.py -k run_walk_forward -q`
Expected: PASS (2 passed). If the planted-signal AUC is borderline, bump `_synthetic_frame` `n` to 120 — do not weaken the assertion.

- [ ] **Step 5: Regression**

Run: `cd ~/Desktop/disclosure-bot && .venv/bin/python -m pytest tests/ -q`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add model_eval.py tests/test_model_eval.py
git commit -m "feat(model_eval): make_pipeline + run_walk_forward"
```

---

### Task 9: `format_report()`

**Files:**
- Modify: `model_eval.py`
- Test: `tests/test_model_eval.py` (extend)

**Interfaces:**
- Consumes: `model_eval._interpret`, the `run_walk_forward` result dict shape.
- Produces: `model_eval.format_report(corpus: str, result: dict | str, horizon: int, folds: int) -> str`. When `result` is a string (abort message) → header + the message + footer. When a dict → the full report. Always ends with the fixed footer and contains no "buy"/"sell"/"forecast"/"target price" phrasing about a specific ticker.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_model_eval.py`:
```python
_FAKE_METRICS = {
    "n": 60, "base_rate": 0.48,
    "models": {
        "lr": {"auc": 0.52, "brier": 0.249, "per_fold_auc": [0.49, 0.58, 0.50], "train_auc": 0.71},
        "gbt": {"auc": 0.50, "brier": 0.255, "per_fold_auc": [0.47, 0.55, 0.49], "train_auc": 0.95},
        "baseline": {"auc": 0.51, "per_fold_auc": [0.50, 0.53, 0.50], "train_auc": 0.55},
    },
    "calibration": [{"bucket": "0.4-0.5", "n": 30, "predicted": 0.45, "actual": 0.47},
                    {"bucket": "0.5-0.6", "n": 30, "predicted": 0.55, "actual": 0.50}],
    "top_decile": 0.50,
    "lr_coefficients": [("num__log_amount", 0.31), ("cat__chamber_house", -0.02)],
    "gbt_importance": [("log_amount", 0.01), ("lag_days", -0.00)],
}


def test_format_report_renders_metrics_and_interpretation():
    text = model_eval.format_report("politicians", _FAKE_METRICS, horizon=21, folds=3)
    assert "MODEL EVALUATION" in text and "politicians" in text
    assert "0.52" in text and "Base rate" in text
    assert "No detectable edge" in text          # AUC 0.52 -> coin-flip band
    assert "Per-fold AUC (LR): 0.49, 0.58, 0.50" in text
    assert "num__log_amount" in text
    assert "not investment advice" in text.lower()


def test_format_report_passes_through_an_abort_message():
    text = model_eval.format_report("insiders", "INSUFFICIENT DATA for insiders.\n  20 rows.",
                                     horizon=21, folds=3)
    assert "INSUFFICIENT DATA for insiders." in text
    assert "not investment advice" in text.lower()


def test_format_report_has_no_ticker_verdict_language():
    text = model_eval.format_report("politicians", _FAKE_METRICS, horizon=21, folds=3).lower()
    for bad in ("buy ", "sell ", "will rise", "will fall", "target price", "forecast:"):
        assert bad not in text
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd ~/Desktop/disclosure-bot && .venv/bin/python -m pytest tests/test_model_eval.py -k format_report -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

Add to `model_eval.py`:
```python
_TITLES = {"politicians": "political trades (House PTRs)",
           "insiders": "SEC Form 4 insider purchases"}

_FOOTER = (
    "Evaluates the historical skill of these features with walk-forward folds. It "
    "does not\nforecast, is not investment advice, and cannot be pointed at a live "
    "signal. The\nsample is small -- read the n on every line, and the per-fold "
    "spread."
)


def _header(corpus: str, horizon: int) -> str:
    return ("=" * 72 + "\n"
            f"  MODEL EVALUATION -- {_TITLES.get(corpus, corpus)}\n"
            f"  Do the recorded features predict beating SPY over {horizon} "
            f"trading days?\n"
            + "=" * 72)


def format_report(corpus: str, result, horizon: int, folds: int) -> str:
    if isinstance(result, str):
        return f"{_header(corpus, horizon)}\n\n{result}\n\n{_FOOTER}"

    m = result
    L = [_header(corpus, horizon)]
    L.append(f"  Labelled rows pooled over {folds} test folds: {m['n']}   "
             f"Base rate (beat SPY): {m['base_rate'] * 100:.0f}%")
    L.append("")
    L.append(f"  {'':22}{'AUC':>7}{'Brier':>9}{'top-decile':>13}")
    L.append("  " + "-" * 50)
    rows = [("lr", "Logistic regression"), ("gbt", "Gradient boosting"),
            ("baseline", "Baseline (amount only)")]
    for key, label in rows:
        e = m["models"][key]
        auc = f"{e['auc']:.2f}" if e["auc"] is not None else "  -"
        brier = f"{e['brier']:.3f}" if "brier" in e else "    -"
        td = f"{m['top_decile'] * 100:.0f}%" if key == "lr" and m["top_decile"] is not None else "-"
        L.append(f"  {label:22}{auc:>7}{brier:>9}{td:>13}")
    lr = m["models"]["lr"]
    L.append("")
    L.append("  Per-fold AUC (LR): " + ", ".join(f"{a:.2f}" if a is not None else "-"
                                                  for a in lr["per_fold_auc"]))
    if lr.get("train_auc") is not None and lr["auc"] is not None and lr["train_auc"] - lr["auc"] > 0.15:
        L.append(f"  (LR train AUC {lr['train_auc']:.2f} vs test {lr['auc']:.2f} -- overfitting)")
    gbt = m["models"]["gbt"]
    if gbt.get("train_auc") is not None and gbt["auc"] is not None and gbt["train_auc"] - gbt["auc"] > 0.15:
        L.append(f"  (GBT train AUC {gbt['train_auc']:.2f} vs test {gbt['auc']:.2f} -- overfitting, "
                 f"as expected at this n)")
    L.append("")
    L.append("  " + _interpret(lr["auc"], m["n"], folds))
    L.append("")
    L.append("  Calibration (predicted probability -> actual hit rate):")
    for d in m["calibration"]:
        L.append(f"    {d['bucket']:>9}  n={d['n']:<4}  predicted {d['predicted']:.2f}  "
                 f"actual {d['actual']:.2f}")
    L.append("")
    L.append("  LR standardized coefficients (interpretation only, fit on all rows):")
    for name, coef in m["lr_coefficients"][:12]:
        L.append(f"    {coef:+.2f}  {name}")
    if m.get("gbt_importance"):
        L.append("")
        L.append("  GBT permutation importance (last fold test set, AUC drop):")
        for name, val in m["gbt_importance"][:8]:
            L.append(f"    {val:+.3f}  {name}")
    L.append("")
    L.append(_FOOTER)
    return "\n".join(L)
```

- [ ] **Step 4: Run to verify pass**

Run: `cd ~/Desktop/disclosure-bot && .venv/bin/python -m pytest tests/test_model_eval.py -k format_report -q`
Expected: PASS.

- [ ] **Step 5: Regression**

Run: `cd ~/Desktop/disclosure-bot && .venv/bin/python -m pytest tests/ -q`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add model_eval.py tests/test_model_eval.py
git commit -m "feat(model_eval): format_report"
```

---

### Task 10: `run()` + `main()` + isolation test + menu + README

**Files:**
- Modify: `model_eval.py` (add `run`, `main`, `__main__` guard)
- Modify: `menu.py` (option 6 + `show_model_eval`)
- Modify: `README.md` (new section)
- Test: `tests/test_model_eval.py` (extend — `run` orchestration + isolation)

**Interfaces:**
- Consumes: everything above; `backtest.collect_political_trades`, `backtest.collect_purchases`, `db.connect`.
- Produces:
  - `model_eval.run(conn, corpus: str, horizon: int = 21, folds: int = 3, min_rows: int = 80, min_test: int = 12) -> str` — orchestrates collect → gate → (abort | evaluate) → format. `corpus == "both"` returns the two reports joined by a blank line.
  - `model_eval.main()` — argparse CLI: positional `corpus` (`politicians|insiders|both`, default `both`), `--horizon 21`, `--folds 3`, `--min-rows 80`, `--min-test 12`.
  - `menu.show_model_eval(conn)` — Russian prompt, calls `model_eval.run`, prints.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_model_eval.py`:
```python
import pathlib


def test_run_aborts_cleanly_when_data_is_thin(conn, monkeypatch):
    monkeypatch.setattr(model_eval.backtest, "collect_political_trades",
                        lambda c, horizon=21, since_days=1200: [
                            _prow("AAA", "Rep A", "2026-08-01", 1),
                            _prow("BBB", "Rep B", "2026-08-02", 0)])
    monkeypatch.setattr(model_eval.backtest, "_return_before", lambda t, d, days=63: 0.0)
    monkeypatch.setattr(model_eval.marketcap, "market_cap_eur", lambda c, t: 1e9)
    out = model_eval.run(conn, "politicians")
    assert "INSUFFICIENT DATA for politicians" in out
    assert "not investment advice" in out.lower()


def test_run_produces_a_report_on_a_healthy_synthetic_corpus(conn, monkeypatch):
    months = ["2025-03", "2025-04", "2025-05", "2025-06", "2025-07"]
    rows = []
    for i in range(100):
        m = months[i % 5]
        rows.append(_prow(f"T{i % 7}", f"Rep {i % 6}", f"{m}-{(i % 27) + 1:02d}", i % 2,
                          amount="$50,001 - $100,000" if i % 2 else "$1,001 - $15,000"))
    monkeypatch.setattr(model_eval.backtest, "collect_political_trades",
                        lambda c, horizon=21, since_days=1200: rows)
    monkeypatch.setattr(model_eval.backtest, "_return_before", lambda t, d, days=63: 0.0)
    monkeypatch.setattr(model_eval.marketcap, "market_cap_eur", lambda c, t: 1e9)
    out = model_eval.run(conn, "politicians")
    assert "MODEL EVALUATION" in out and "Base rate" in out
    assert "Per-fold AUC (LR):" in out


def test_no_project_module_imports_model_eval():
    root = pathlib.Path(__file__).resolve().parent.parent
    offenders = [p.name for p in root.glob("*.py")
                 if p.name != "model_eval.py" and "model_eval" in p.read_text()]
    assert offenders == [], f"these modules reference model_eval: {offenders}"
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd ~/Desktop/disclosure-bot && .venv/bin/python -m pytest tests/test_model_eval.py -k "run_ or imports_model_eval" -q`
Expected: FAIL — `run` not defined (and the isolation test passes trivially for now, which is fine).

- [ ] **Step 3: Implement `run` + `main`**

Add to `model_eval.py`:
```python
def run(conn, corpus: str, horizon: int = 21, folds: int = 3,
        min_rows: int = 80, min_test: int = 12) -> str:
    if corpus == "both":
        return "\n\n".join(
            run(conn, c, horizon, folds, min_rows, min_test)
            for c in ("politicians", "insiders"))

    if corpus == "politicians":
        rows = backtest.collect_political_trades(conn, horizon=horizon)
    else:
        rows = backtest.collect_purchases(conn, (horizon,), since_days=1200,
                                          extended_features=True)
    if not rows:
        return format_report(corpus, f"INSUFFICIENT DATA for {corpus}.\n  "
                             f"No labelled rows at all.", horizon, folds)

    X, y, dates = build_feature_frame(conn, rows, corpus, horizon)
    coverage_start = min(dates)[:7]
    abort = check_sufficiency(dates, folds, min_rows, min_test, corpus, coverage_start)
    if abort:
        return format_report(corpus, abort, horizon, folds)

    fold_idx = walk_forward_folds(dates, folds, min_test)
    if fold_idx is None:
        return format_report(corpus, f"INSUFFICIENT DATA for {corpus}.\n  "
                             f"Could not build {folds} walk-forward folds.", horizon, folds)
    metrics = run_walk_forward(X, y, dates, fold_idx, corpus)
    return format_report(corpus, metrics, horizon, len(fold_idx))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("corpus", nargs="?", default="both",
                    choices=["politicians", "insiders", "both"])
    ap.add_argument("--horizon", type=int, default=21,
                    help="trading days after disclosure to measure (default 21)")
    ap.add_argument("--folds", type=int, default=3, help="walk-forward test months")
    ap.add_argument("--min-rows", type=int, default=80, help="gate: total labelled rows")
    ap.add_argument("--min-test", type=int, default=12,
                    help="gate: rows an eligible test month needs")
    args = ap.parse_args()
    conn = db.connect(DB_PATH)
    print(run(conn, args.corpus, args.horizon, args.folds, args.min_rows, args.min_test))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run to verify pass**

Run: `cd ~/Desktop/disclosure-bot && .venv/bin/python -m pytest tests/test_model_eval.py -q`
Expected: PASS (whole file green).

- [ ] **Step 5: Wire the menu**

In `menu.py`: add `import model_eval` to the import block. Add the prompt line — change
```python
        print("5) Досье по тикеру (всё, что известно + новости и отчётность)")
        print("0) Выход")
```
to
```python
        print("5) Досье по тикеру (всё, что известно + новости и отчётность)")
        print("6) Оценка предсказательной силы (walk-forward модель)")
        print("0) Выход")
```
Add the branch after `elif choice == "5":`:
```python
        elif choice == "6":
            show_model_eval(conn)
```
Add the function (near `show_backtest`):
```python
def show_model_eval(conn) -> None:
    """Walk-forward оценка того, есть ли у признаков предсказательная сила.
    Не прогноз и не рекомендация — см. подпись внизу отчёта."""
    corpus = (input("Корпус [politicians / insiders / both] (Enter = both): ").strip()
              or "both")
    if corpus not in ("politicians", "insiders", "both"):
        print("Не понял корпус.")
        return
    h = input("Горизонт в торговых днях (Enter = 21): ").strip()
    horizon = int(h) if h.isdigit() else 21
    print("Считаю (нужны исторические цены — политический прогон может занять минуту)...")
    print(model_eval.run(conn, corpus, horizon))
```
Also update the fallback line `print("Не понял выбор, введите 0, 1, 2, 3, 4 или 5.")` → `... 5 или 6.`

- [ ] **Step 6: Verify the menu wiring and isolation still holds**

Run:
```bash
cd ~/Desktop/disclosure-bot && .venv/bin/python -c "import menu, model_eval; print('ok')"
.venv/bin/python -m pytest tests/test_model_eval.py::test_no_project_module_imports_model_eval -q
```
Expected: the import prints `ok`; the isolation test **fails** now (menu.py imports model_eval).

Resolve: the isolation rule is "no module imports `model_eval` *at module scope*". `menu.py` is the one allowed consumer via the same pattern it uses for `research`/`backtest`. Update the isolation test to allowlist `menu.py`:
```python
def test_no_project_module_imports_model_eval():
    root = pathlib.Path(__file__).resolve().parent.parent
    allowed = {"model_eval.py", "menu.py"}
    offenders = [p.name for p in root.glob("*.py")
                 if p.name not in allowed and "model_eval" in p.read_text()]
    assert offenders == [], f"these modules reference model_eval: {offenders}"
```
Re-run the isolation test → PASS. (`menu.py` importing it is fine — the binding rule is that `bot.py` / the signal path / the Telegram digest never touch it, and they don't.)

- [ ] **Step 7: Full regression + live smoke**

Run:
```bash
cd ~/Desktop/disclosure-bot && .venv/bin/python -m pytest tests/ -q
.venv/bin/python model_eval.py insiders                 # expect INSUFFICIENT DATA + re-run estimate
.venv/bin/python model_eval.py politicians              # expect a real report (slow, ~1-2 min)
.venv/bin/python bot.py --once --sec-only --forms 4 --no-telegram   # unaffected
```
Expected: suite green; insiders aborts cleanly; politicians prints a pooled AUC (almost certainly ~0.5 with "No detectable edge"), per-fold AUCs, calibration, coefficients, footer; bot run unaffected.

- [ ] **Step 8: README section**

In `README.md`, after the "## Проверка сигналов на истории (backtest)" section, add:
```markdown
## Оценка предсказательной силы (walk-forward модель)

`backtest.py` меряет, что было с ценой после сигналов, по группам признаков.
`model_eval.py` идёт дальше и задаёт вопрос прямо: **есть ли у этих признаков
предсказательная сила вне выборки?**

```bash
python model_eval.py politicians    # House PTR-покупки — оценивается сейчас (данные 2025)
python model_eval.py insiders       # SEC Form 4 — «мало данных» ещё несколько месяцев
python model_eval.py                # оба
python model_eval.py --horizon 5 --folds 4
```

Что делается: логистическая регрессия и градиентный бустинг обучаются
предсказывать «обгонит ли бумага S&P за месяц» по признакам, которые бот и так
пишет (сумма/роль/размер компании/лаг раскрытия/размер кластера/цена до сделки).
Оценка — **walk-forward**: обучаемся на месяцах 1…N, проверяемся на N+1, катимся
вперёд, без заглядывания. В отчёте — AUC вне выборки с разбросом по фолдам,
калибровка, коэффициенты, и строка-вывод, привязанная к AUC.

**Жёсткий порог данных.** Ниже минимума (80 размеченных строк, 3 фолда по ≥12
строк) отчёт не печатает метрики, а пишет «мало данных, вернитесь примерно
тогда-то». Инсайдерский корпус этот порог сейчас не проходит (6 недель истории).
Политический — едва проходит на данных 2025 года (~150 строк от 20 конгрессменов),
и почти наверняка покажет AUC ≈ 0.5 и «нет заметного преимущества». Это ожидаемый
и правильный результат: несколько сотен сделок — этого не хватит, чтобы найти
преимущество, если оно вообще есть.

Чего здесь **нет** и не будет: сохранённой модели, оценки живого сигнала, вывода
«покупать» по конкретному тикеру. Модель переобучается каждый прогон только ради
оценки. Ни один модуль проекта её не импортирует (кроме `menu.py`), и есть тест,
который это проверяет.
```
Then update the menu block in the README ("## Интерактивное меню") to add line `6) Оценка предсказательной силы (walk-forward модель)`.

- [ ] **Step 9: Final full check**

Run: `cd ~/Desktop/disclosure-bot && .venv/bin/python -m pytest tests/ -q && .venv/bin/python -c "import backtest, bot, cluster, menu, model_eval, research; print('all import')"`
Expected: green; all import.

- [ ] **Step 10: Commit**

```bash
git add model_eval.py menu.py README.md tests/test_model_eval.py
git commit -m "feat(model_eval): run/main orchestration, menu option 6, docs"
```

---

## Self-Review

**1. Spec coverage.**

| Spec section | Task |
|---|---|
| Non-goals: no live scoring / no persisted model | Design of `run`/`run_walk_forward` (refit each run); Task 10 |
| Non-goals: nothing imports it | Task 10 Step 6 isolation test (allowlists `menu.py`) |
| Non-goals: no verdict/forecast in output | Task 9 `test_format_report_has_no_ticker_verdict_language` |
| Architecture: `run()` orchestrator, `main`, menu both call it | Task 10 |
| Data collectors in `backtest.py`, shared | Tasks 3, 4 |
| Label = beat SPY at H, entered on disclosure date | Tasks 3, 4 |
| Corpus A (politicians, House-only, junk-ticker filter, perf note) | Task 3 |
| Corpus B (`extended_features`, `is_first_buy` constant note) | Task 4 |
| Features incl. `member_prior_hitrate` leak rule (2·H), `cluster_size` index, `price_vs_spy_63d` backward helper | Tasks 2, 5 |
| Walk-forward: month groups, eligible dense months, expanding train, concrete example | Task 6 |
| Gate defaults 80 / 12 / 3, abort message with re-run estimate | Task 6 |
| Models: LR + GBT with pinned hyperparameters, one-feature baseline LR | Task 8 |
| Metrics: AUC + per-fold, Brier, calibration deciles, top-decile precision, base rate | Tasks 7, 8 |
| Interpretation line by AUC band | Task 7 |
| Feature effect: LR coefs (fit on all rows), GBT permutation importance last fold | Task 8 |
| Footer text | Task 9 |
| Report style matches `backtest._report` (English, stdout) | Task 9 |
| Interface: positional corpus + 4 flags | Task 10 |
| Menu option 6, Russian prompt | Task 10 |
| Dependencies: sklearn to requirements + lock | Task 1 |
| Known limitations stated in README | Task 10 Step 8 |
| Testing list (feature extraction, leakage, splitter, gate, metrics, interpretation, isolation) | Tasks 2–10 tests |
| Verification: `--purchases` byte-identical | Task 4 Step 5 |

No gaps.

**2. Placeholder scan.** No "TBD", no "add error handling", no "write tests for the above" without code. Every code step carries the full implementation.

**3. Type consistency.**
- `collect_political_trades` row keys (`ticker, member, chamber, disclosure_date, trade_date, amount_range, label`) — produced in Task 3, consumed in Task 5 `build_feature_frame` and Task 10 tests via `_prow`. Match.
- `collect_purchases(..., extended_features=True)` row adds `disclosure_date, label, shares, owned_after` — Task 4 produces, Task 5 consumes (`r.get("shares")`, `r.get("owned_after")`, `r["role"]`, `r.get("value")`). Match. (Note: `value` is already on the base row; `owner` too.)
- `_COLUMNS[corpus]` is `(numeric, categorical, baseline_col)` everywhere (Tasks 5, 8).
- `run_walk_forward` result dict keys (`n, base_rate, models{lr,gbt,baseline}, calibration, top_decile, lr_coefficients, gbt_importance`) — produced Task 8, consumed by `format_report` Task 9 and its `_FAKE_METRICS`. Match — `format_report` also reads `models[k]["per_fold_auc"]` and `["train_auc"]`, both set in Task 8.
- `walk_forward_folds` returns `list[tuple[list[int], list[int]]] | None` — Task 6 produces, Task 8 & 10 consume. Match.
- `format_report(corpus, result, horizon, folds)` signature — Task 9 defines, Task 10 `run` calls with 4 args. Match.

No inconsistencies.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-09-10-model-eval.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**
