"""The one-month outlook: how often the price was higher a month later in situations
like today's (spec §2). A historical frequency with its sample size and a walk-forward
check -- not a fitted model and not a promise.

A situation is three coarse facts from daily closes -- trend (vs the 50/200-day
averages), last month's move (against the asset's own normal monthly swing) and
volatility (vs its own past year) -- 18 in all. Tables count them across many assets
and years, one observation per asset per month so overlapping days don't inflate n.
"""
from __future__ import annotations

import datetime as dt
import math

import pandas as pd

HORIZON = {"stock": 21, "crypto": 30}
LOOKBACK = 252
MIN_HISTORY = LOOKBACK + 21            # 273 observations
VOL_WINDOW = 21
HIGH_VOL_QUANTILE = 0.75
MIN_OOS = 50
MIN_OWN = 30
OOS_START_OFFSET_YEARS = 4  # Test fold starts at the table's 5th year: years[0] + 4
POOLED = "*"


# ------------------------------------------------------------------ situations
def situation_series(closes: pd.Series, h: int) -> pd.Series:
    c = pd.Series(closes, dtype=float).reset_index(drop=True)
    sma50, sma200 = c.rolling(50).mean(), c.rolling(200).mean()
    trend = pd.Series("mixed", index=c.index)
    trend[(c > sma50) & (sma50 > sma200)] = "up"
    trend[(c < sma50) & (sma50 < sma200)] = "down"
    rets = c.pct_change()
    sigma_m = rets.rolling(LOOKBACK).std(ddof=0) * math.sqrt(h)
    r = c / c.shift(h) - 1
    move = pd.Series("flat", index=c.index)
    move[r > sigma_m] = "strong_up"
    move[r < -sigma_m] = "strong_down"
    vol = rets.rolling(VOL_WINDOW).std(ddof=0)
    threshold = vol.rolling(LOOKBACK).quantile(HIGH_VOL_QUANTILE)
    vol_state = pd.Series("normal", index=c.index)
    # A hair of tolerance: equal rolling values can differ by float noise.
    vol_state[vol > threshold * (1 + 1e-9)] = "high"
    keys = trend + "|" + move + "|" + vol_state
    valid = ((c.index >= MIN_HISTORY - 1) & sma200.notna() & sigma_m.notna()
             & threshold.notna())
    return keys.where(valid)


def situation(closes: list[float], h: int) -> str | None:
    if len(closes) < MIN_HISTORY:
        return None
    key = situation_series(pd.Series(closes), h).iloc[-1]
    return None if pd.isna(key) else key


def situation_from_tv(snapshot: dict, h: int) -> str | None:
    fields = [snapshot.get(k) for k in ("close", "SMA50", "SMA200", "Perf.1M", "Volatility.M")]
    if any(v is None for v in fields):
        return None
    close, sma50, sma200, perf, vol_m = (float(v) for v in fields)
    trend = ("up" if close > sma50 > sma200 else "down" if close < sma50 < sma200 else "mixed")
    sigma_m = vol_m / 100 * math.sqrt(h)
    r = perf / 100
    move = "strong_up" if r > sigma_m else "strong_down" if r < -sigma_m else "flat"
    return f"{trend}|{move}|{POOLED}"


def pooled(key: str) -> str:
    return key.rsplit("|", 1)[0] + "|" + POOLED


# ------------------------------------------------------------------- tables
def observations(bars: list[tuple[str, float]], h: int) -> list[tuple[int, str, bool]]:
    """(year, situation, price higher h observations later), sampled every h
    observations so no two overlap."""
    if len(bars) < MIN_HISTORY + h:
        return []
    closes = pd.Series([c for _d, c in bars], dtype=float)
    keys = situation_series(closes, h)
    out = []
    for t in range(MIN_HISTORY - 1, len(bars) - h, h):
        key = keys.iloc[t]
        if pd.isna(key):
            continue
        out.append((int(bars[t][0][:4]), key, bool(closes.iloc[t + h] > closes.iloc[t])))
    return out


def _walk_forward(obs: list[tuple[int, str, bool]]) -> dict:
    years = sorted({y for y, _k, _u in obs})
    if not years:
        return {}
    acc: dict[str, dict] = {}
    for year in range(years[0] + OOS_START_OFFSET_YEARS, years[-1]):
        train = [(k, u) for y, k, u in obs if y < year]
        test = [(k, u) for y, k, u in obs if y == year]
        if not train or not test:
            continue
        base = sum(u for _k, u in train) / len(train)
        counts: dict[str, list[int]] = {}
        for k, u in train:
            c = counts.setdefault(k, [0, 0])
            c[0] += 1
            c[1] += u
        for k, u in test:
            n, ups = counts.get(k, (0, 0))
            p = ups / n if n else base
            a = acc.setdefault(k, {"oos_n": 0, "oos_brier_s": 0.0, "oos_brier_base": 0.0})
            a["oos_n"] += 1
            a["oos_brier_s"] += (p - u) ** 2
            a["oos_brier_base"] += (base - u) ** 2
    for a in acc.values():
        a["oos_brier_s"] /= a["oos_n"]
        a["oos_brier_base"] /= a["oos_n"]
    return acc


def _table(obs: list[tuple[int, str, bool]]) -> dict:
    counts: dict[str, list[int]] = {}
    for _y, k, u in obs:
        c = counts.setdefault(k, [0, 0])
        c[0] += 1
        c[1] += u
    base = sum(u for _y, _k, u in obs) / len(obs) if obs else None
    wf = _walk_forward(obs)
    empty = {"oos_n": 0, "oos_brier_s": None, "oos_brier_base": None}
    return {k: {"n": n, "up": ups, "base_rate": base, **wf.get(k, empty)}
            for k, (n, ups) in counts.items()}


def build_table(obs: list[tuple[int, str, bool]]) -> dict:
    """Full situations plus volatility-pooled ones (for the TradingView fallback)."""
    rows = _table(obs)
    rows.update(_table([(y, pooled(k), u) for y, k, u in obs]))
    return rows


def has_edge(row: dict) -> bool:
    return (row.get("oos_n", 0) >= MIN_OOS and row.get("oos_brier_s") is not None
            and row["oos_brier_s"] < row["oos_brier_base"])


# ------------------------------------------------------------------- storage
def save_table(conn, name: str, rows: dict) -> None:
    built_at = dt.datetime.now().isoformat(timespec="seconds")
    conn.execute("DELETE FROM outlook_table WHERE table_name = ?", (name,))
    conn.executemany(
        "INSERT INTO outlook_table (table_name, situation, n, up, base_rate, oos_n, "
        "oos_brier_s, oos_brier_base, built_at) VALUES (?,?,?,?,?,?,?,?,?)",
        [(name, k, r["n"], r["up"], r["base_rate"], r["oos_n"], r["oos_brier_s"],
          r["oos_brier_base"], built_at) for k, r in rows.items()])
    conn.commit()


def load_table(conn, name: str) -> tuple[dict, str | None]:
    rows = conn.execute(
        "SELECT situation, n, up, base_rate, oos_n, oos_brier_s, oos_brier_base, built_at "
        "FROM outlook_table WHERE table_name = ?", (name,)).fetchall()
    table = {k: {"n": n, "up": up, "base_rate": b, "oos_n": on, "oos_brier_s": bs,
                 "oos_brier_base": bb} for k, n, up, b, on, bs, bb, _at in rows}
    return table, (rows[0][7] if rows else None)


def save_bars(conn, symbol: str, bars: list[tuple[str, float]]) -> None:
    conn.executemany("INSERT OR REPLACE INTO price_bars (symbol, date, close) VALUES (?,?,?)",
                     [(symbol, d, c) for d, c in bars])
    conn.commit()


def load_bars(conn, symbol: str) -> list[tuple[str, float]]:
    return [(d, c) for d, c in conn.execute(
        "SELECT date, close FROM price_bars WHERE symbol = ? ORDER BY date", (symbol,))]
