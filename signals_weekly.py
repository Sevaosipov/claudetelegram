"""signals_weekly.py: which buy signals the weekly run sends (spec 2026-10-04-remove-model-portfolio.md;
the coin limit, which coins and the high-risk flag: 2026-10-04-more-coins.md and its Amendment).

The bot holds no portfolio: it scores every fresh signal and, once a week, tells the user which of
the scores say BUY. The user's own Trading 212 account is the only portfolio, so a signal is worth
sending only for a name that is not in it, and only once a month for any one ticker; and of the five
a week at most two are coins, whose places are reserved -- BTC and ETH first, then the alts by score
and, at equal scores, by their 60-day return (coin_priority) -- the stocks filling the rest. pick_buys
makes that choice from the day's scores; the weekly run (bot.py) keeps the picks, sends one message
each (weekly.py words them: the stocks by score first, then the coins) and writes a buy_signals row
for every message that went out (record_signal).
"""
from __future__ import annotations

import datetime as dt
import json
import math

import crypto
import model
import model_score
import positions

RESIGNAL_DAYS = 30          # a ticker signalled in the last this many days is not signalled again
WEEKLY_BUY_LIMIT = 5        # at most this many new buy signals a week
WEEKLY_COIN_LIMIT = 2       # ... and at most this many of them coins: the two highest-scoring
MAX_REASONS = 3             # the reasons kept with a signal (its message shows the first two)


def held_names(conn) -> set[tuple[str, str, str]]:
    """What the user holds now, as positions._asset_key names it: every open position of either origin
    (/bought or the Trading 212 account) -- a coin by its symbol, a stock by its ticker, an Oslo or
    Stockholm listing with its venue (Oslo's NRC is not the US NRC). A Trading 212 holding keyed by its
    ISIN is also held under the name the signals use: the Oslo ticker of an Oslo listing, and the market
    symbol of a US instrument (one Yahoo could not price, so the sync keyed it by ISIN)."""
    held = set()
    for pos in positions.open_positions(conn):
        held.add(positions._asset_key(pos.ticker, pos.source))
        if pos.source != positions.T212_SOURCE:
            continue
        oslo = positions.oslo_ticker(conn, pos.ticker)
        if oslo:
            held.add(positions._asset_key(oslo, "NORWAY"))
        us = _us_market_symbol(conn, pos.t212_ticker)
        if us:
            held.add(positions._asset_key(us, None))
    return held


def _us_market_symbol(conn, t212_ticker: str | None) -> str:
    """The symbol a US Trading 212 instrument trades under (META for FB_US_EQ), "" for any other."""
    import t212_account
    return t212_account._market_symbol(conn, t212_ticker)


def recently_signalled(conn, today: dt.date) -> set[str]:
    """The tickers with a buy_signals row sent in the last RESIGNAL_DAYS days (the day itself and the
    day RESIGNAL_DAYS ago included)."""
    since = (today - dt.timedelta(days=RESIGNAL_DAYS)).isoformat()
    return {r[0] for r in conn.execute("SELECT DISTINCT ticker FROM buy_signals WHERE sent_at >= ?", (since,))}


def _symbol(s) -> str:
    return getattr(s, "coin", None) or crypto.symbol_of(s.ticker)


def is_alt(s) -> bool:
    """Whether a score is an alt: a coin that is not BTC or ETH (model.MAJOR_COINS)."""
    return s.kind == "crypto" and _symbol(s) not in model.MAJOR_COINS


def coin_priority(s) -> tuple:
    """How a coin ranks for one of the week's WEEKLY_COIN_LIMIT coin places, lowest first: BTC and ETH
    before every alt (model.MAJOR_COINS, in that order), then the alts by score, equal scores by the
    coin's 60-day return (`ret60`), the highest first. A score with no return -- under 121 closes, or
    one kept before the return was -- sorts after every alt that has one; equals stay as they came."""
    symbol = _symbol(s)
    if symbol in model.MAJOR_COINS:
        return (0, model.MAJOR_COINS.index(symbol), 0.0, 0.0)
    ret60 = getattr(s, "ret60", None)
    known = isinstance(ret60, (int, float)) and math.isfinite(ret60)
    return (1, 0, -s.total, -ret60 if known else math.inf)


def pick_buys(conn, today: dt.date, scored: list, *, skip=None) -> list:
    """The scores to signal this week: only a BUY, not a name the user holds (held_names), not a ticker
    signalled in the last RESIGNAL_DAYS days (recently_signalled), at most WEEKLY_BUY_LIMIT.

    Coin and stock scores are on different scales, so the coin places are reserved: first up to
    WEEKLY_COIN_LIMIT of the eligible coin BUYs, in coin_priority order -- BTC and ETH when they are
    free, whatever the alts score, then the alts by score, equal scores by the 60-day return -- and then
    the remaining places (WEEKLY_BUY_LIMIT less the coins picked) go to the stock BUYs by score (equal
    scores as they came). With no eligible coin the stocks take all five; with few stocks there are
    fewer picks, never a third coin. A coin that is skipped (held, signalled lately, not a BUY) uses up
    no place: BTC out, the place goes to ETH, and the other to the best alt.

    One order for sending: the stocks, best score first, then the coins in coin_priority order. The
    scores themselves are returned -- StockScore, CoinScore or the kept-score objects
    model.cached_scores gives. Reads the database, writes nothing.

    `skip(score) -> bool` leaves a stock out for a reason of the caller's (a company being bought out:
    bot._pick_week). It is asked in score order and only until the stock places are filled, so a check
    that costs a request is made for a handful of names; the place of a stock left out goes to the next."""
    held, recent = held_names(conn), recently_signalled(conn, today)
    free: list = []
    seen: set[str] = set()
    for s in sorted((s for s in scored if s.decision == model_score.BUY), key=lambda s: s.total, reverse=True):
        key = positions._asset_key(s.ticker, getattr(s, "source", None))
        if key in held or s.ticker in recent or s.ticker in seen:
            continue
        seen.add(s.ticker)
        free.append(s)
    coins = sorted((s for s in free if s.kind == "crypto"), key=coin_priority)[:WEEKLY_COIN_LIMIT]
    stocks: list = []
    for s in (s for s in free if s.kind != "crypto"):
        if len(stocks) >= WEEKLY_BUY_LIMIT - len(coins):
            break
        if skip is None or not skip(s):
            stocks.append(s)
    return stocks + coins


def pick_record(s) -> dict:
    """A pick as what its message and its buy_signals row need -- plain JSON, so the week's picks can be
    kept in kv and sent again by a retry without scoring again. A coin has its symbol for a company and
    CRYPTO for a source. An alt's record also has `"risk": True` (its message ends «высокий риск»); no
    other pick -- BTC, ETH, a stock -- has the key, and neither has a pick kept before it existed."""
    crypto_kind = s.kind == "crypto"
    record = {"ticker": s.ticker,
              "source": getattr(s, "source", None) or ("CRYPTO" if crypto_kind else None),
              "company": getattr(s, "company", None) or getattr(s, "coin", None),
              "kind": s.kind, "score": s.total, "stop_pct": s.stop_pct,
              "reasons": list(s.reasons)[:MAX_REASONS], "t212": getattr(s, "t212", None)}
    if is_alt(s):
        record["risk"] = True
    return record


def record_signal(conn, pick: dict, today: dt.date, *, commit: bool = True) -> None:
    """Write the buy_signals row of a pick whose message went out today. `commit=False` leaves it in the
    caller's open transaction (bot.py commits it together with the week's sent list)."""
    t212 = pick.get("t212")
    conn.execute(
        "INSERT INTO buy_signals (ticker, source, company, kind, score, stop_pct, reasons, t212, sent_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (pick["ticker"], pick.get("source"), pick.get("company"), pick.get("kind"), pick.get("score"),
         pick.get("stop_pct"), json.dumps(pick.get("reasons") or [], ensure_ascii=False),
         None if t212 is None else int(bool(t212)), today.isoformat()))
    if commit:
        conn.commit()
