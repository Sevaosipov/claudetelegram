"""Measure whether the signals this bot sends actually precede anything.

Until this existed, the project had eight sources, a scoring function and no way to
tell whether any of it worked. That is the wrong order: every threshold in
cluster.py -- how many buyers make a cluster, what counts as a large solo purchase,
how much a stake must grow -- was chosen by reasoning about the domain and then
never checked against an outcome.

What this does: for each recorded signal, take the price at the moment it could
first have been acted on and compare it against the same stock some days later, and
against a benchmark over exactly the same days. Then group by the features recorded
with the signal and show how each group did.

What this is NOT:
  - a claim that any group's past numbers will repeat;
  - a trading strategy, or advice to act on one;
  - meaningful at small n. A group of eight signals over three weeks tells you
    nothing whatever, and the report says so rather than printing a number that
    looks like a finding. See MIN_MEANINGFUL_N and the sign test below.

A further limitation worth stating plainly, because it does not go away by
collecting more rows: these observations are not independent of each other. Several
purchases often land in the same stock within days, and every row in a short sample
shares one market period -- so if the market rose that month, most groups will look
good together. The sign test assumes independent trials and therefore overstates
its confidence here. It becomes trustworthy only across many months and many
distinct names, and even then it is a hint about the past.

Two modes:
    python backtest.py              measure recorded signals (signal_journal)
    python backtest.py --purchases  measure individual SEC insider purchases,
                                    sliced by the same features -- useful before
                                    the journal has accumulated anything
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import statistics
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

# yfinance logs a 404 to stderr for every delisted or renamed ticker it is asked
# about. Those are expected here -- shells get renamed, SPACs disappear -- and the
# lookup already handles them by skipping the row, so the noise only buries the
# report.
import logging
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

import db
import marketcap

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "data" / "disclosures.db"

# Trading days after the signal. Roughly a day, a week, a month, a quarter.
HORIZONS = (1, 5, 21, 63)
BENCHMARK = "SPY"

# Below this, a group's numbers are reported but explicitly not interpreted. This is
# not a rule of thumb about statistics in general -- it is a floor beneath which the
# median of a handful of volatile stocks is indistinguishable from noise.
MIN_MEANINGFUL_N = 30


def _series(ticker: str, start: str):
    """Daily closes from `start`, or None. Cached per process, not per run: prices
    after the signal keep changing, so a persistent cache would go stale."""
    import yfinance as yf
    key = (ticker, start)
    if key in _SERIES_CACHE:
        return _SERIES_CACHE[key]
    out = None
    try:
        hist = yf.Ticker(ticker.replace(".", "-")).history(start=start)["Close"].dropna()
        if len(hist) >= 2:
            out = hist
    except Exception:
        pass
    _SERIES_CACHE[key] = out
    return out


_SERIES_CACHE: dict = {}


def forward_returns(ticker: str, start_date: str, horizons=HORIZONS) -> dict | None:
    """Percentage change from the first close on/after `start_date` to each horizon,
    alongside the benchmark's change over the identical span."""
    prices = _series(ticker, start_date)
    if prices is None:
        return None
    bench = _series(BENCHMARK, start_date)
    entry = float(prices.iloc[0])
    out = {}
    for h in horizons:
        if len(prices) <= h:
            continue
        ret = (float(prices.iloc[h]) / entry - 1) * 100
        excess = None
        if bench is not None and len(bench) > h:
            b = (float(bench.iloc[h]) / float(bench.iloc[0]) - 1) * 100
            excess = ret - b
        out[h] = {"return": ret, "excess": excess}
    return out or None


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


def sign_test_p(wins: int, n: int) -> float | None:
    """Two-sided exact binomial test against a coin flip.

    Included so a group with 7 of 10 positive is not mistaken for a result. No
    correction is made for the many groups compared, which is itself a reason to
    treat anything here as a hint at best.
    """
    if n == 0:
        return None
    k = max(wins, n - wins)
    tail = sum(math.comb(n, i) for i in range(k, n + 1)) / (2 ** n)
    return min(1.0, 2 * tail)


def summarise(rows: list[dict], horizon: int) -> dict | None:
    vals = [r["excess"] for r in rows if r.get("excess") is not None]
    rets = [r["return"] for r in rows if r.get("return") is not None]
    if not vals:
        return None
    wins = sum(1 for v in vals if v > 0)
    return {
        "n": len(vals),
        "median_return": statistics.median(rets) if rets else None,
        "median_excess": statistics.median(vals),
        "hit_rate": wins / len(vals) * 100,
        "p": sign_test_p(wins, len(vals)),
        "meaningful": len(vals) >= MIN_MEANINGFUL_N,
    }


def _bucket_score(score) -> str:
    if score is None:
        return "no score"
    for upper, name in ((20, "<20"), (35, "20-35"), (50, "35-50"), (float("inf"), "50+")):
        if score < upper:
            return name
    return "50+"


def collect_signals(conn, horizons=HORIZONS) -> list[dict]:
    """Recorded signals, each with its outcome attached."""
    rows = conn.execute(
        """SELECT source, kind, ticker, company, buyer_count, total_value_eur,
                  holder_only, first_buy, lag_days, market_cap_eur, value_pct_of_mcap,
                  score, emitted_at
           FROM signal_journal WHERE ticker IS NOT NULL AND ticker != ''
           ORDER BY emitted_at"""
    ).fetchall()
    out = []
    for (source, kind, ticker, company, buyers, value, holder_only, first_buy,
         lag, mcap, pct_mcap, score, emitted) in rows:
        fr = forward_returns(ticker, emitted[:10], horizons)
        if not fr:
            continue
        out.append({
            "source": source, "kind": kind, "ticker": ticker, "buyers": buyers or 1,
            "holder_only": bool(holder_only), "score": score,
            "size": marketcap.size_bucket(mcap), "returns": fr,
        })
    return out


def collect_purchases(conn, horizons=HORIZONS, since_days: int = 365) -> list[dict]:
    """Individual SEC insider purchases with their outcomes.

    Available before the journal has anything in it, and finer-grained: it can ask
    whether a feature matters at the level of a single purchase rather than a whole
    cluster.
    """
    since = (dt.date.today() - dt.timedelta(days=since_days)).isoformat()
    rows = conn.execute(
        """SELECT ticker, owner_name, transaction_date, value, is_officer, is_director,
                  is_ten_pct_owner, derivative, COALESCE(is_10b5_1, 0), filed_date
           FROM sec_purchases
           WHERE transaction_date >= ? AND ticker IS NOT NULL AND ticker != ''
           ORDER BY transaction_date""",
        (since,),
    ).fetchall()
    out = []
    for (ticker, owner, txn_date, value, officer, director, ten_pct, deriv,
         is_10b5_1, filed) in rows:
        # Enter on the disclosure date, not the trade date: the trade is not public
        # until it is filed, so measuring from the trade date would credit the
        # signal with a move nobody could have acted on.
        entry = filed or txn_date
        fr = forward_returns(ticker, entry, horizons)
        if not fr:
            continue
        out.append({
            "ticker": ticker, "owner": owner, "value": value or 0,
            "role": ("officer/director" if (officer or director)
                     else "10% holder" if ten_pct else "other"),
            "derivative": bool(deriv), "is_10b5_1": bool(is_10b5_1),
            "returns": fr,
        })
    return out


def _report(title: str, groups: dict, horizon: int) -> None:
    print(f"\n=== {title} — {horizon} trading day(s) after disclosure ===")
    print(f"{'group':22} {'n':>5} {'median':>9} {'vs bench':>10} {'hit rate':>9} {'p':>7}")
    print("-" * 66)
    for name, rows in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        stats = summarise([r["returns"].get(horizon, {}) for r in rows], horizon)
        if not stats:
            continue
        p = f"{stats['p']:.2f}" if stats["p"] is not None else "-"
        flag = "" if stats["meaningful"] else "  ← too few to read anything into"
        print(f"{name:22} {stats['n']:>5} {stats['median_return']:>8.1f}% "
              f"{stats['median_excess']:>+9.1f}pp {stats['hit_rate']:>8.0f}% {p:>7}{flag}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--purchases", action="store_true",
                     help="measure individual SEC purchases instead of recorded signals")
    ap.add_argument("--horizon", type=int, default=21,
                     help="trading days after disclosure to measure (default 21, ~1 month)")
    ap.add_argument("--days", type=int, default=365,
                     help="how far back to pull purchases from, in --purchases mode")
    args = ap.parse_args()

    conn = db.connect(DB_PATH)
    horizons = tuple(sorted({args.horizon, *HORIZONS}))

    if args.purchases:
        data = collect_purchases(conn, horizons, since_days=args.days)
        print(f"{len(data)} SEC purchases with usable price history")
        if not data:
            print("Nothing to measure yet. Collect some data first: python bot.py --once")
            return
        _report("all purchases", {"all": data}, args.horizon)
        _report("by filer role", _group(data, lambda r: r["role"]), args.horizon)
        _report("common stock vs derivative",
                _group(data, lambda r: "derivative" if r["derivative"] else "common stock"),
                args.horizon)
        _report("10b5-1 plan vs discretionary",
                _group(data, lambda r: "10b5-1 (scheduled)" if r["is_10b5_1"] else "discretionary"),
                args.horizon)
        _report("by purchase size",
                _group(data, lambda r: ("< EUR 100k" if r["value"] < 100_000 else
                                         "EUR 100k-1m" if r["value"] < 1_000_000 else "EUR 1m+")),
                args.horizon)
    else:
        data = collect_signals(conn, horizons)
        print(f"{len(data)} recorded signals with usable price history")
        if not data:
            print("signal_journal is empty -- it only fills as signals fire from now on.\n"
                  "For something measurable today, try: python backtest.py --purchases")
            return
        _report("all signals", {"all": data}, args.horizon)
        _report("by signal kind", _group(data, lambda r: r["kind"]), args.horizon)
        _report("by source", _group(data, lambda r: r["source"]), args.horizon)
        _report("by distinct buyers",
                _group(data, lambda r: "1 (solo)" if r["buyers"] <= 1 else
                                        "2" if r["buyers"] == 2 else "3+"), args.horizon)
        _report("insiders vs holders only",
                _group(data, lambda r: "holders only" if r["holder_only"] else "has officer/director"),
                args.horizon)
        _report("by company size", _group(data, lambda r: r["size"]), args.horizon)
        _report("by score", _group(data, lambda r: _bucket_score(r["score"])), args.horizon)

    span = conn.execute(
        "SELECT min(transaction_date), max(transaction_date) FROM sec_purchases "
        "WHERE transaction_date != ''").fetchone()
    print(f"\nData covers {span[0]} to {span[1]}.")
    print(f"Groups under n={MIN_MEANINGFUL_N} are noise. 'p' is a two-sided sign test against "
          f"a coin flip,\nuncorrected for multiple groups AND assuming independent "
          f"observations -- which these\nare not: purchases cluster in the same names and "
          f"share one market period, so a rising\nmarket lifts every group at once. Treat p "
          f"as a hint, never as a result.\nPast behaviour is not a prediction, and none of "
          f"this is investment advice.")


def _group(rows: list[dict], key) -> dict:
    out: dict[str, list] = {}
    for r in rows:
        out.setdefault(key(r), []).append(r)
    return out


if __name__ == "__main__":
    main()
