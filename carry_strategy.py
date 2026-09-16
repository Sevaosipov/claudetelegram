"""Daily signal check for the EURUSD carry-gated trend strategy -- a Python port
of the validated Pine script (`EURUSD strategy` in the user's TradingView account;
local copy of the forex-daytrader project at ~/forex-daytrader/), run headlessly
once a day so it can alert over the same Telegram pipeline disclosure-bot already
uses, instead of TradingView's own alert panel (app push + email, manual watch).

MECHANISM (frozen, do not re-tune -- see the Pine script's own header for how it
was fitted/validated): go long when EURUSD closes more than 2.5% above its 200-day
average AND the German 2-year yield is above the US 2-year (ECB-vs-Fed policy
divergence favors EUR); short on the mirror image. Exit when price crosses back
through the average ("signal off"), or a 6xATR trailing stop that only ever moves
in the trade's favor. Validated out-of-sample (OANDA 2014-2026): PF 1.917, +20.6%,
15 trades. Still only "best candidate found," not a proven edge -- see the Pine
header's LIMITS section for the full caveats (small sample, Bonferroni-corrected
significance, 3 pair-years).

WHY THIS IS A SEPARATE PORT, NOT A WRAPPER AROUND THE PINE SCRIPT: the Pine
strategy runs inside TradingView Desktop via an interactive MCP session, which
this headless daily job can't drive. This file recomputes the same signal from
scratch against independent free data sources, so it can run unattended next to
the rest of disclosure-bot. Keep the two in sync by hand if the Pine script's
frozen parameters ever change.

WHAT THIS DOES AND DOESN'T DO: like every signal in this project, it states facts
(today's computed state, the price/MA/spread/ATR numbers behind it) and sends a
Telegram alert only when that state CHANGES -- it does not place orders, does not
track a live P&L, and does not tell you whether to take the trade. On a fresh
entry the alert includes the 6xATR stop distance in price terms so a broker-side
trailing-stop order can be set, the same fallback alerts_config.md already
documents for the gold config ("if you can't babysit it hourly, set a broker-side
trailing stop of ~3xATR in points instead") -- this avoids needing a daily "still
holding" ping while a position is open.

DATA SOURCES (all free, no key/login -- verified working 2026-09-16):
    EURUSD price   yfinance "EURUSD=X", daily OHLC
    DE 2y yield    Deutsche Bundesbank API, series BBSSY.D.REN.EUR.A610.000000WT0202.A
                   https://api.statistiken.bundesbank.de/rest/data/BBSSY/...
    US 2y yield    FRED, series DGS2 (Market Yield on 2-Year Treasury CMT)
                   https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS2
FRED has no confirmed free series for Germany's 2-year yield specifically (only
10-year, IRLTLT01DEM156N) -- the Bundesbank's own API is the direct source for
the actual series TradingView's TVC:DE02Y tracks.

Usage:
    python carry_strategy.py --once           # compute today's state, alert on change
    python carry_strategy.py --once --dry-run # print what would happen, send nothing
    python carry_strategy.py --status         # print current persisted state and exit
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import re
import sys
from pathlib import Path

import pandas as pd
import requests

import db
import telegram_notify

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "data" / "disclosures.db"

MA_LEN = 200
BAND_PCT = 2.5
ATR_LEN = 20
TRAIL_ATR = 6.0

BUNDESBANK_URL = "https://api.statistiken.bundesbank.de/rest/data/BBSSY/D.REN.EUR.A610.000000WT0202.A"
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
_TIMEOUT = 20

# kv_cache keys -- all numeric, so the existing get/save_cached_value pair (which
# only ever stored floats for streak counters and timestamps elsewhere in this
# project) covers position state too without a schema change. -1 short / 0 flat /
# 1 long. A ~30-year max_age is "persist indefinitely" in the same style
# run_healthcheck uses float("inf") for last_successful_run.
STATE_SIDE = "carry_eurusd_side"
STATE_TRAIL_STOP = "carry_eurusd_trail_stop"
STATE_EXTREME = "carry_eurusd_extreme"
PERSIST_SECONDS = 30 * 365 * 24 * 3600

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def fetch_de_2y(session: requests.Session | None = None) -> pd.Series:
    """Germany's 2-year Bund yield, daily, from the Bundesbank's own API.

    Parsed by scanning for the first line whose first field is an ISO date,
    rather than a fixed skiprows count -- the response carries 8 metadata lines
    (series title, comment, decimals, ...) before the data starts, and this
    project's own BaFin/Sweden parsers already learned the hard way that a
    fixed offset into someone else's export is exactly the kind of thing that
    breaks silently when the source tweaks a header line.
    """
    session = session or requests.Session()
    resp = session.get(BUNDESBANK_URL, params={"format": "csv", "lang": "en"}, timeout=_TIMEOUT)
    resp.raise_for_status()
    rows = []
    for line in resp.text.splitlines():
        first = line.split(",", 1)[0].strip('"')
        if not _DATE_RE.match(first):
            continue
        parts = line.split(",")
        if len(parts) < 2 or parts[1] in ("", "."):
            continue
        try:
            rows.append((pd.Timestamp(parts[0]), float(parts[1])))
        except ValueError:
            continue
    if not rows:
        raise ValueError("Bundesbank DE 2y response had no parseable data rows")
    s = pd.Series(dict(rows)).sort_index()
    s.name = "de_2y"
    return s


def fetch_us_2y(session: requests.Session | None = None) -> pd.Series:
    """US 2-year Treasury constant-maturity yield, daily, from FRED (DGS2)."""
    session = session or requests.Session()
    resp = session.get(FRED_URL, params={"id": "DGS2"}, timeout=_TIMEOUT)
    resp.raise_for_status()
    df = pd.read_csv(io.StringIO(resp.text))
    df.columns = ["date", "value"]
    df["date"] = pd.to_datetime(df["date"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    s = df.dropna().set_index("date")["value"]
    s.name = "us_2y"
    return s


def fetch_eurusd(period: str = "2y") -> pd.DataFrame:
    """Daily EURUSD OHLC from yfinance, with today's still-forming bar dropped --
    same reasoning as bot.py's _days_to_scan treating "today" as never complete."""
    import yfinance as yf
    raw = yf.download("EURUSD=X", period=period, interval="1d", progress=False, auto_adjust=False)
    if raw is None or raw.empty:
        raise ValueError("yfinance returned no EURUSD data")
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    df = raw[["Open", "High", "Low", "Close"]].rename(columns=str.lower)
    today = pd.Timestamp(dt.date.today())
    if df.index[-1] >= today:
        df = df.iloc[:-1]
    return df


def _wilder_atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int) -> pd.Series:
    """Pine's ta.atr() uses Wilder's RMA, which seeds on the SMA of the first
    `length` true-range values. pandas' .ewm(alpha=1/length, adjust=False) seeds
    on the first observation instead -- the two converge exponentially fast
    (after `length` bars the seed's residual weight is ~(1-1/length)**bars, which
    is already negligible well within our multi-year history), so with 400+ bars
    of warm-up before "today" this is a fine live approximation, not an exact
    replica of a fresh backtest."""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()


def compute_state(price: pd.DataFrame, de_2y: pd.Series, us_2y: pd.Series) -> dict:
    """Today's (last confirmed bar's) signal read: price/MA/band, the carry gate,
    and whether today is a fresh entry cross (evL/evS in the Pine script's terms).
    Mirrors the Pine script's condition definitions exactly; see its header."""
    close, high, low = price["close"], price["high"], price["low"]
    ma = close.rolling(MA_LEN).mean()
    atr = _wilder_atr(high, low, close, ATR_LEN)

    diff = (de_2y.reindex(close.index, method="ffill")
            - us_2y.reindex(close.index, method="ffill"))
    carry_l = diff > 0
    carry_s = diff < 0

    band = BAND_PCT / 100.0
    enter_l = (close > ma * (1 + band)) & carry_l
    enter_s = (close < ma * (1 - band)) & carry_s
    hold_l = (close > ma) & carry_l
    hold_s = (close < ma) & carry_s

    i = -1
    return {
        "date": close.index[i],
        "close": float(close.iloc[i]),
        "ma": float(ma.iloc[i]) if pd.notna(ma.iloc[i]) else None,
        "atr": float(atr.iloc[i]) if pd.notna(atr.iloc[i]) else None,
        "diff": float(diff.iloc[i]) if pd.notna(diff.iloc[i]) else None,
        "high": float(high.iloc[i]),
        "low": float(low.iloc[i]),
        "enter_l": bool(enter_l.iloc[i]) if pd.notna(enter_l.iloc[i]) else False,
        "enter_s": bool(enter_s.iloc[i]) if pd.notna(enter_s.iloc[i]) else False,
        "hold_l": bool(hold_l.iloc[i]) if pd.notna(hold_l.iloc[i]) else False,
        "hold_s": bool(hold_s.iloc[i]) if pd.notna(hold_s.iloc[i]) else False,
        "ev_l": bool(enter_l.iloc[i] and not enter_l.iloc[i - 1]),
        "ev_s": bool(enter_s.iloc[i] and not enter_s.iloc[i - 1]),
    }


def _load_position(conn) -> dict:
    side = db.get_cached_value(conn, STATE_SIDE, PERSIST_SECONDS)
    trail = db.get_cached_value(conn, STATE_TRAIL_STOP, PERSIST_SECONDS)
    extreme = db.get_cached_value(conn, STATE_EXTREME, PERSIST_SECONDS)
    return {"side": int(side) if side is not None else 0,
            "trail_stop": trail, "extreme": extreme}


def _save_position(conn, pos: dict) -> None:
    db.save_cached_value(conn, STATE_SIDE, float(pos["side"]))
    db.save_cached_value(conn, STATE_TRAIL_STOP, pos["trail_stop"] if pos["trail_stop"] is not None else float("nan"))
    db.save_cached_value(conn, STATE_EXTREME, pos["extreme"] if pos["extreme"] is not None else float("nan"))


def step(pos: dict, s: dict) -> tuple[dict, str | None]:
    """One day's position update, mirroring the Pine script's per-bar order:
    entries on a fresh cross, exits on the hold condition dropping or the
    trailing stop being breached, then the trailing stop ratchets. Returns the
    new position and an alert message, or None if nothing changed today."""
    side = pos["side"]
    trail_stop, extreme = pos["trail_stop"], pos["extreme"]
    msg = None

    stopped_out = False
    if side == 1 and trail_stop is not None and s["low"] <= trail_stop:
        stopped_out = True
    elif side == -1 and trail_stop is not None and s["high"] >= trail_stop:
        stopped_out = True

    signal_off = (side == 1 and not s["hold_l"]) or (side == -1 and not s["hold_s"])

    if stopped_out or signal_off:
        reason = "trailing stop" if stopped_out else "signal off"
        exit_price = trail_stop if stopped_out else s["close"]
        msg = telegram_notify.format_carry_signal(
            "LONG" if side == 1 else "SHORT", "FLAT", s, reason=reason, level=exit_price)
        side, trail_stop, extreme = 0, None, None

    if side == 0 and s["ev_l"] and s["atr"]:
        side = 1
        trail_stop = s["close"] - s["atr"] * TRAIL_ATR
        extreme = s["high"]
        msg = telegram_notify.format_carry_signal("FLAT", "LONG", s, reason="entry", level=trail_stop)
    elif side == 0 and s["ev_s"] and s["atr"]:
        side = -1
        trail_stop = s["close"] + s["atr"] * TRAIL_ATR
        extreme = s["low"]
        msg = telegram_notify.format_carry_signal("FLAT", "SHORT", s, reason="entry", level=trail_stop)
    elif side == 1 and s["atr"]:
        extreme = max(extreme, s["high"])
        trail_stop = max(trail_stop, extreme - s["atr"] * TRAIL_ATR)
    elif side == -1 and s["atr"]:
        extreme = min(extreme, s["low"])
        trail_stop = min(trail_stop, extreme + s["atr"] * TRAIL_ATR)

    return {"side": side, "trail_stop": trail_stop, "extreme": extreme}, msg


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="compute today's state once and exit")
    ap.add_argument("--status", action="store_true", help="print persisted position state and exit")
    ap.add_argument("--dry-run", action="store_true", help="print what would be sent, don't call Telegram")
    args = ap.parse_args()

    conn = db.connect(DB_PATH)

    if args.status:
        pos = _load_position(conn)
        label = {1: "LONG", -1: "SHORT", 0: "FLAT"}[pos["side"]]
        print(f"EURUSD carry-gated: {label}"
              + (f"  trail_stop={pos['trail_stop']:.4f}" if pos["trail_stop"] else ""))
        return 0

    try:
        price = fetch_eurusd()
        de_2y = fetch_de_2y()
        us_2y = fetch_us_2y()
    except Exception as e:
        print(f"[carry_strategy] data fetch failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    s = compute_state(price, de_2y, us_2y)
    pos = _load_position(conn)
    new_pos, msg = step(pos, s)

    print(f"[carry_strategy] {s['date'].date()} close={s['close']:.4f} ma={s['ma']:.4f} "
          f"diff={s['diff']:+.2f}pp side {pos['side']} -> {new_pos['side']}")

    if msg:
        print(msg)
        if not args.dry_run:
            telegram_notify.send_text(msg)
    if not args.dry_run:
        _save_position(conn, new_pos)

    return 0


if __name__ == "__main__":
    sys.exit(main())
