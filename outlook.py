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
import re
import sys

import pandas as pd

import assets
import sources
import tradingview

HORIZON = {"stock": 21, "crypto": 30}
LOOKBACK = 252
MIN_HISTORY = LOOKBACK + 21            # 273 observations
VOL_WINDOW = 21
HIGH_VOL_QUANTILE = 0.75
# An edge (has_edge) needs all four: enough out-of-sample observations, spread over
# enough test years, a better Brier score than the base rate in most of those years,
# and a skill above rounding noise overall.
MIN_OOS = 50
MIN_OOS_FOLDS = 3
MIN_FOLD_WIN_SHARE = 0.6
MIN_BRIER_SKILL = 0.005
MIN_OWN = 30
OOS_START_OFFSET_YEARS = 4  # Test fold starts at the table's 5th year: years[0] + 4
POOLED = "*"
TABLE_MAX_AGE_DAYS = 7
FULL_HISTORY_DAYS = 15 * 365 + 10
TOP_UP_OVERLAP_DAYS = 30
SPLIT_TOLERANCE = 0.01          # an overlap day off by more than 1% means history was rescaled
LOOKUP_DAYS = 800
CRYPTO_UNIVERSE_TOP = 50
CRYPTO_MIN_HISTORY_DAYS = 730
# Majors run at 2-5% a day; a coin pegged to a dollar, a fund or gold moves ~0.1%.
PEGGED_DAILY_STD = 0.005
_COIN_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,10}$")
# Copies and wrappers of another asset (WBTC, stETH), and tokenised dollars, funds and
# gold: no price of their own to count, and they would inflate n.
_NOT_A_FREE_COIN = ("wrapped", "staked", "bridged", "restaked", "liquid staking", "usd",
                    "dollar", "treasury", "gold", "yield", "fund")
_TREND = {"up": "тренд вверх", "down": "тренд вниз", "mixed": "тренд смешанный"}
_MOVE = {"strong_up": "месяц сильный рост", "strong_down": "месяц сильное падение",
         "flat": "месяц без резких движений"}
_VOL = {"high": "волатильность высокая", "normal": "волатильность обычная",
        POOLED: "(без учёта волатильности)"}


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
    observations so no two overlap. The year is that of the label date (t + h), when
    the outcome is known: a training fold (years < Y) then never peeks into year Y."""
    if len(bars) < MIN_HISTORY + h:
        return []
    closes = pd.Series([c for _d, c in bars], dtype=float)
    keys = situation_series(closes, h)
    out = []
    for t in range(MIN_HISTORY - 1, len(bars) - h, h):
        key = keys.iloc[t]
        if pd.isna(key):
            continue
        out.append((int(bars[t + h][0][:4]), key, bool(closes.iloc[t + h] > closes.iloc[t])))
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
        fold: dict[str, list] = {}       # this year's n and summed Brier: situation, base
        for k, u in test:
            n, ups = counts.get(k, (0, 0))
            p = ups / n if n else base
            f = fold.setdefault(k, [0, 0.0, 0.0])
            f[0] += 1
            f[1] += (p - u) ** 2
            f[2] += (base - u) ** 2
        for k, (n, brier_s, brier_base) in fold.items():
            a = acc.setdefault(k, {"oos_n": 0, "oos_brier_s": 0.0, "oos_brier_base": 0.0,
                                   "oos_folds": 0, "oos_fold_wins": 0})
            a["oos_n"] += n
            a["oos_brier_s"] += brier_s
            a["oos_brier_base"] += brier_base
            a["oos_folds"] += 1
            a["oos_fold_wins"] += brier_s < brier_base
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
    empty = {"oos_n": 0, "oos_brier_s": None, "oos_brier_base": None, "oos_folds": 0,
             "oos_fold_wins": 0}
    return {k: {"n": n, "up": ups, "base_rate": base, **wf.get(k, empty)}
            for k, (n, ups) in counts.items()}


def build_table(obs: list[tuple[int, str, bool]]) -> dict:
    """Full situations plus volatility-pooled ones (for the TradingView fallback)."""
    rows = _table(obs)
    rows.update(_table([(y, pooled(k), u) for y, k, u in obs]))
    return rows


def has_edge(row: dict) -> bool:
    """Spec §2's walk-forward check, made stricter: in simulated noise tables with a
    date-wide market factor, "Brier better than the base rate, n >= 50" passed 19-25%
    of situations with no real effect; these four together pass at most ~6% and still
    find ~73% of real +8-point effects. A row from before folds were stored has none."""
    n, folds, wins = (row.get(k) or 0 for k in ("oos_n", "oos_folds", "oos_fold_wins"))
    brier_s, brier_base = row.get("oos_brier_s"), row.get("oos_brier_base")
    if n < MIN_OOS or folds < MIN_OOS_FOLDS or wins / folds < MIN_FOLD_WIN_SHARE:
        return False
    return bool(brier_s is not None and brier_base and 1 - brier_s / brier_base >= MIN_BRIER_SKILL)


# ------------------------------------------------------------------- storage
def save_table(conn, name: str, rows: dict, n_assets: int | None) -> None:
    """`n_assets`: how many assets the observations came from (the message names it)."""
    built_at = dt.datetime.now().isoformat(timespec="seconds")
    conn.execute("DELETE FROM outlook_table WHERE table_name = ?", (name,))
    conn.executemany(
        "INSERT INTO outlook_table (table_name, situation, n, up, base_rate, oos_n, "
        "oos_brier_s, oos_brier_base, oos_folds, oos_fold_wins, assets, built_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(name, k, r["n"], r["up"], r["base_rate"], r["oos_n"], r["oos_brier_s"],
          r["oos_brier_base"], r["oos_folds"], r["oos_fold_wins"], n_assets, built_at)
         for k, r in rows.items()])
    conn.commit()


def load_table(conn, name: str) -> tuple[dict, str | None]:
    rows = conn.execute(
        "SELECT situation, n, up, base_rate, oos_n, oos_brier_s, oos_brier_base, oos_folds, "
        "oos_fold_wins, assets, built_at FROM outlook_table WHERE table_name = ?",
        (name,)).fetchall()
    table = {k: {"n": n, "up": up, "base_rate": b, "oos_n": on, "oos_brier_s": bs,
                 "oos_brier_base": bb, "oos_folds": of, "oos_fold_wins": ow, "assets": na}
             for k, n, up, b, on, bs, bb, of, ow, na, _at in rows}
    return table, (rows[0][-1] if rows else None)


def save_bars(conn, symbol: str, bars: list[tuple[str, float]]) -> None:
    conn.executemany("INSERT OR REPLACE INTO price_bars (symbol, date, close) VALUES (?,?,?)",
                     [(symbol, d, c) for d, c in bars])
    conn.commit()


def load_bars(conn, symbol: str) -> list[tuple[str, float]]:
    return [(d, c) for d, c in conn.execute(
        "SELECT date, close FROM price_bars WHERE symbol = ? ORDER BY date", (symbol,))]


# ------------------------------------------------------------------------ refresh
def _universe(conn, kind: str) -> list[str]:
    """Yahoo symbols of the assets a table is built from."""
    if kind == "stock":
        import universe
        return sorted({t.replace(".", "-") for t in universe.load()["tickers"]})
    coins = [r for r in sources.cached_coins(conn)
             if r[0] not in sources.STABLECOINS and r[3] is not None
             and _COIN_SYMBOL_RE.match(r[0])
             and not any(w in (r[2] or "").lower() for w in _NOT_A_FREE_COIN)]
    return [f"{sym}-USD" for sym, *_rest in sorted(coins, key=lambda r: r[3])[:CRYPTO_UNIVERSE_TOP]]


def _pegged(bars: list[tuple[str, float]]) -> bool:
    rets = pd.Series([c for _d, c in bars[-365:]], dtype=float).pct_change().dropna()
    return len(rets) > 0 and rets.std(ddof=0) < PEGGED_DAILY_STD


def _asset_for(kind: str, yahoo_symbol: str):
    return (assets.stock_asset(yahoo_symbol) if kind == "stock"
            else assets.crypto_asset(yahoo_symbol[:-len("-USD")]))


def _closed_bars(asset, days: int) -> list[tuple[str, float]]:
    """Daily closes without today's (UTC) bar: it is still moving, and stored it would
    differ from the final close and trip the rescale check at the next top-up."""
    bars, _src = sources.price_history(asset, days)
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    return [(d, c) for d, c in bars or [] if d < today]


def _top_up(conn, asset) -> None:
    """Fetch only the new days -- or everything again when the overlap shows the
    history was rescaled (a split or dividend adjustment)."""
    stored = load_bars(conn, asset.yahoo)
    if not stored:
        bars = _closed_bars(asset, FULL_HISTORY_DAYS)
        if bars:
            save_bars(conn, asset.yahoo, bars)
        return
    last = dt.date.fromisoformat(stored[-1][0])
    days = (dt.date.today() - last).days + TOP_UP_OVERLAP_DAYS
    bars = _closed_bars(asset, days)
    if not bars:
        return
    old = dict(stored)
    rescaled = any(d in old and abs(c / old[d] - 1) > SPLIT_TOLERANCE for d, c in bars)
    if rescaled:
        bars = _closed_bars(asset, FULL_HISTORY_DAYS)
        if not bars:
            return
        # The delete and the reinsert must land together: if the write after the
        # delete fails, roll back so the old rows survive rather than sitting
        # deleted-but-uncommitted until some other symbol's later commit makes the
        # loss permanent (refresh() only logs this exception and moves on).
        try:
            conn.execute("DELETE FROM price_bars WHERE symbol = ?", (asset.yahoo,))
            save_bars(conn, asset.yahoo, bars)
        except Exception:
            conn.rollback()
            raise
        return
    save_bars(conn, asset.yahoo, bars)


def refresh(conn, kinds=("stock", "crypto")) -> None:
    for kind in kinds:
        try:
            symbols = _universe(conn, kind)
        except Exception as e:  # e.g. universe.load() failing must not stop the other kind
            print(f"[outlook] {kind} universe: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        obs, n_assets = [], 0
        for symbol in symbols:
            asset = _asset_for(kind, symbol)
            try:
                _top_up(conn, asset)
            except Exception as e:  # one symbol failing must not stop the table
                print(f"[outlook] {symbol}: {type(e).__name__}: {e}", file=sys.stderr)
            bars = load_bars(conn, asset.yahoo)
            if kind == "crypto" and (len(bars) < CRYPTO_MIN_HISTORY_DAYS or _pegged(bars)):
                continue
            own = observations(bars, HORIZON[kind])
            obs += own
            n_assets += bool(own)
        if obs:
            save_table(conn, kind, build_table(obs), n_assets)
            print(f"[outlook] {kind}: {len(obs)} observations from {n_assets} assets")


def refresh_if_stale(conn) -> None:
    stale = []
    for kind in ("stock", "crypto"):
        _rows, built_at = load_table(conn, kind)
        if (built_at is None or dt.datetime.now() - dt.datetime.fromisoformat(built_at)
                > dt.timedelta(days=TABLE_MAX_AGE_DAYS)):
            stale.append(kind)
    if stale:
        refresh(conn, stale)


# ------------------------------------------------------------------------- lookup
def lookup(conn, asset, bars=None, source=None) -> dict:
    """`bars` / `source`: the caller's own daily closes for the asset, reused when
    they are long enough to place it (MIN_HISTORY) instead of fetching them again."""
    if asset.is_isin:
        return {"status": "no_prices"}
    kind, h = asset.kind, HORIZON[asset.kind]
    table, _built_at = load_table(conn, kind)
    if not table:
        return {"status": "no_table"}
    if not bars or len(bars) < MIN_HISTORY:
        bars, source = sources.price_history(asset, LOOKUP_DAYS)
    key = None
    if bars and len(bars) >= MIN_HISTORY:
        key = situation([c for _d, c in bars], h)
    is_pooled = False
    if key is None:
        snap = tradingview.fetch_snapshot(asset.tradingview or asset.symbol)
        key = situation_from_tv(snap, h) if snap else None
        if key is not None:
            is_pooled, source = True, "TradingView"
    if key is None:
        return {"status": "short_history" if bars else "no_prices"}
    row = table.get(key, {})
    result = {"status": "ok", "table": kind, "situation": key, "pooled": is_pooled,
              "source": source, "n": row.get("n", 0),
              "p": row["up"] / row["n"] if row.get("n") else None,
              "base_rate": next(iter(table.values()))["base_rate"],
              "assets": next(iter(table.values())).get("assets"),
              "edge": has_edge(row), "european": kind == "stock" and asset.exchange is not None}
    if not is_pooled:
        own = [up for _y, k, up in observations(load_bars(conn, asset.yahoo), h) if k == key]
        if len(own) >= MIN_OWN:
            result["own_n"], result["own_p"] = len(own), sum(own) / len(own)
    return result


def describe(key: str) -> str:
    trend, move, vol = key.split("|")
    return f"{_TREND[trend]} · {_MOVE[move]} · {_VOL[vol]}"


def _pct0(x: float) -> str:
    return f"{x * 100:.0f}%"


def format_outlook(result: dict, symbol: str) -> str:
    status = result.get("status")
    if status == "no_table":
        return "📈 Прогноз на месяц: ещё не готов (таблица строится раз в неделю)"
    if status == "no_prices":
        return "📈 Прогноз на месяц: нет свежих цен"
    if status == "short_history":
        return "📈 Прогноз на месяц: мало истории для прогноза"
    usual = f"обычно {_pct0(result['base_rate'])}"
    if result["edge"] and result["p"] is not None:
        n = f"{result['n']:,}".replace(",", " ")
        head = (f"📈 Прогноз на месяц: рост в {_pct0(result['p'])} похожих ситуаций "
                f"(n = {n}) · {usual} · проверено на истории")
    else:
        head = f"📈 Прогноз на месяц: нет преимущества над базовой частотой ({usual})"
    lines = [head, f"   ситуация: {describe(result['situation'])}"]
    if result.get("own_n"):
        lines.append(f"   у самой {symbol} в такой ситуации: {_pct0(result['own_p'])} "
                     f"(n = {result['own_n']})")
    notes = []
    if result.get("european"):
        notes.append("таблица по акциям США")
    if result.get("table") == "crypto":
        coins = result.get("assets")
        notes.append(f"по {coins} крупнейшим монетам" if coins else "по крупнейшим монетам")
    if result.get("source") not in (None, "Yahoo"):
        notes.append(f"цены: {result['source']}")
    lines.append("   частота в прошлом, не гарантия" + ("".join(f" · {n}" for n in notes)))
    return "\n".join(lines)


def main() -> int:
    import argparse
    import db
    from pathlib import Path
    ap = argparse.ArgumentParser(description=__doc__)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--rebuild", action="store_true", help="refetch and rebuild both tables now")
    group.add_argument("--refresh-if-stale", action="store_true",
                       help="rebuild a table only when it is older than 7 days")
    args = ap.parse_args()
    conn = db.connect(Path(__file__).parent / "data" / "disclosures.db")
    if args.rebuild:
        refresh(conn)
    else:
        refresh_if_stale(conn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
