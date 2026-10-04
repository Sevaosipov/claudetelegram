"""model.py: the daily scoring pass -- which of the day's signals are worth a word, and how much.

Every fresh stock signal and the thirteen coins get a score (model_score.py's pure functions); score_day
scores them, keeps the day's scores for the menu and the analyst and reports the decisions that
bot.py puts in the journal's `tier` and picks the week's buy signals from (signals_weekly.py).
The bot holds no portfolio -- the user's Trading 212 account is the only one it tracks -- so
nothing here trades, sizes or books a position (spec 2026-10-04-remove-model-portfolio.md).

What stays besides the scoring are the constants and helpers that the exit rules for the
user's own positions (positions.py) read: the stop fallback, the dead-money and one-year limits,
the activist-cut check and the news seam.

Everything that touches the network sits behind a seam (fetch, news_fn, trend_fn, t212), so a test
hands each its own stub.
"""

from __future__ import annotations

import datetime as dt
import sys
import types
from dataclasses import dataclass

import assets
import cluster
import crypto
import db
import model_score
import sources
import strategy
from prices import Prices, listing

COINS = ("BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "AVAX", "HYPE", "LTC", "ENA", "LINK", "TRX", "SUI")
MAJOR_COINS = ("BTC", "ETH")  # the other eleven are "alts": bought only while bitcoin is up, flagged high risk
MODEL_SIGNAL_DAYS = 14        # a stock is scored while its signal was disclosed this recently
NEWS_DAYS = 14
NEWS_MIN_PRESCORE = 35.0      # headlines are fetched only for a stock scoring this without them
CAUTION_DAYS = 7              # a coin's inflow or outflow signal counts this long
STAKE_LOOKBACK_DAYS = 30
ACTIVIST_MIN_PERCENT = 5.0    # a 13D on file counts as an activist nearby from this stake ...
PASSIVE_BIG_PERCENT = 10.0    # ... and a 13G from this one; both below model_score.STAKE_CONTROL_PERCENT
# A stock whose insiders and triggers can't reach the watchlist even with full momentum and
# news is not worth a price or a news fetch.
PRUNE_HEADROOM = model_score.MOMENTUM_CAP + model_score.NEWS_MAX
SCORED_KEY = "model_scored_{day}"   # kv_cache: the day's scores, for the menu and the analyst
PRUNED_REASON = "импульс и новости не считались: до наблюдения не дотянуть"
FINDER_CLUSTER_KWARGS = {"min_value": 50_000, "solo_threshold": 250_000}
FINDER_STAKE_KWARGS = {"min_percent": 5.0, "activist_only": False,
                       "new_positions_only": False, "max_age_days": 14}
_POLITICIANS = ("HOUSE", "SENATE")


@dataclass
class ScoreReport:
    """What score_day gives back: the day's scores, the decision per ticker (for the journal) and whether
    scoring got through. bot.py picks the week's buy signals only from a complete report."""
    scored: list              # StockScore and CoinScore, highest total first
    decisions: dict[str, str]  # ticker -> decision, for the journal
    complete: bool = True     # False when scoring raised (caught and logged): there are no scores then


# ---------------------------------------------------------------- seams
def default_news(ticker: str, source: str | None) -> list[dict]:
    """The last NEWS_DAYS days of headlines on a stock or a coin ({"title", "published"}),
    [] when there are none or the sources fail. A date that can't be read keeps the item."""
    try:
        if crypto.is_crypto(ticker):
            asset = assets.crypto_asset(crypto.symbol_of(ticker))
        else:
            listed = listing(ticker, source)
            if listed is None:
                return []
            asset = assets.stock_asset(listed[0])
        items, _used = sources.news(asset)
        since = (dt.date.today() - dt.timedelta(days=NEWS_DAYS)).isoformat()
        return [i for i in items or [] if _published_since(i, since)]
    except Exception as e:
        print(f"[model] no news for {ticker}: {type(e).__name__}: {e}", file=sys.stderr)
        return []


def _published_since(item: dict, since: str) -> bool:
    try:
        return dt.date.fromisoformat((item.get("published") or "")[:10]).isoformat() >= since
    except ValueError:
        return True


def _default_t212(conn):
    try:
        import trading212
        return trading212.availability(conn)
    except Exception as e:
        print(f"[model] no Trading 212 list: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def _memoised(fn):
    """`fn(ticker, source)` asked once per pair: a held stock that is also a candidate
    would otherwise fetch its headlines (or sector) twice in one run."""
    cache: dict[tuple, object] = {}

    def wrapper(ticker, source):
        key = (ticker, source)
        if key not in cache:
            cache[key] = fn(ticker, source)
        return cache[key]
    return wrapper


# ---------------------------------------------------------------- candidates
def candidate_signals(conn, today: dt.date, *, tickers: set[str] | None = None) -> list:
    """Every buy-side signal disclosed in the last MODEL_SIGNAL_DAYS days, enriched with
    market cap and liquidity. A signal is scored again every day while it is fresh, so
    the alert state is ignored. `tickers` narrows it to those tickers (any case, a leading
    "$" ignored) before the recency check and the enrichment, which fetch per signal: the
    analyst asks about one name at a time."""
    found = strategy.buy_side_signals(
        conn, ignore_alert_state=True, onchain=False,
        cluster_kwargs=FINDER_CLUSTER_KWARGS, stake_kwargs=FINDER_STAKE_KWARGS)
    if tickers is not None:
        wanted = {t.strip().upper().removeprefix("$") for t in tickers}
        found = [s for s in found if (getattr(s, "ticker", None) or "").upper() in wanted]
    since = (today - dt.timedelta(days=MODEL_SIGNAL_DAYS)).isoformat()
    fresh = [s for s in found if (cluster.disclosed_on(conn, s) or "") >= since]
    return cluster.enrich_signals(conn, fresh)


# ---------------------------------------------------------------- scoring
# An original filing (not a /A amendment) of a stake that doesn't control the company.
_NEW_STAKE_SQL = ("SELECT ticker FROM sec_stakes WHERE form_type LIKE ? AND form_type NOT LIKE '%/A%' "
                  "AND percent_of_class >= ? AND percent_of_class < ? AND event_date >= ?")


def _context(conn, candidates: list, today: dt.date) -> tuple[set[str], set[str], set[str]]:
    """The tickers that an activist 13D, a big passive 13G or politicians are buying:
    from today's candidates and from the stake filings of the last STAKE_LOOKBACK_DAYS.
    Only a stake that is itself a signal counts: an original filing below control
    (amendments are mostly routine updates by long-time holders), and a candidate only when
    model_score.stake_part gives it points."""
    since = (today - dt.timedelta(days=STAKE_LOOKBACK_DAYS)).isoformat()
    control = model_score.STAKE_CONTROL_PERCENT
    activist = {r[0] for r in conn.execute(
        _NEW_STAKE_SQL, ("%13D%", ACTIVIST_MIN_PERCENT, control, since))}
    passive = {r[0] for r in conn.execute(
        _NEW_STAKE_SQL, ("%13G%", PASSIVE_BIG_PERCENT, control, since))}
    politicians: set[str] = set()
    for c in candidates:
        if getattr(c, "source", None) in _POLITICIANS:
            politicians.add(c.ticker)
        if hasattr(c, "percent") and model_score.stake_part(c).points > 0:
            if c.is_activist:
                activist.add(c.ticker)
            elif c.percent >= PASSIVE_BIG_PERCENT:
                passive.add(c.ticker)
    return activist, passive, politicians


def _score_stocks(conn, candidates, prices, today, news_fn, t212, prune: bool = True) -> list:
    activist, passive, politicians = _context(conn, candidates, today)
    best: dict[str, model_score.StockScore] = {}
    for sig in candidates:
        if hasattr(sig, "crypto_kind") or crypto.is_crypto(sig.ticker):
            continue
        flags = {"activist": sig.ticker in activist, "passive_big": sig.ticker in passive,
                 "politicians": sig.ticker in politicians,
                 "t212": None if t212 is None else t212.can_buy(sig.ticker, sig.source)}
        score = model_score.score_stock(sig, [], None, **flags)     # insiders and triggers only
        if prune and score.insiders + score.triggers + PRUNE_HEADROOM < model_score.STOCK_WATCH:
            score.reasons.append(PRUNED_REASON)   # its «импульс 0 · новости 0» is not a reading
        else:
            listed = listing(sig.ticker, sig.source)
            closes = [c for _d, c in prices.bars(listed[0])] if listed else []
            score = model_score.score_stock(sig, closes, None, **flags)
            if score.total >= NEWS_MIN_PRESCORE:
                score = model_score.score_stock(sig, closes, news_fn(sig.ticker, sig.source), **flags)
        if sig.ticker not in best or score.total > best[sig.ticker].total:
            best[sig.ticker] = score
    return list(best.values())


def _coin_closes(prices, coin: str) -> list[float]:
    return [c for _d, c in prices.bars(listing(crypto.ticker(coin), "CRYPTO")[0])]


def _score_coins(conn, candidates, prices, today, news_fn, trend_fn) -> list:
    """The thirteen coins' scores. The bitcoin regime filter is applied here: an alt is a BUY only when
    bitcoin's own close is above its 100-day average (model_score.coin_trend), else it is WATCH with
    model_score.ALT_GATE_REASON. BTC and ETH are not gated. Bitcoin with no usable history (under 121
    completed closes) is not "up": the alts wait."""
    since = (today - dt.timedelta(days=CAUTION_DAYS)).isoformat()
    flows = [c for c in candidates if hasattr(c, "crypto_kind") and _flow_is_fresh(c, since)]
    btc_trend = model_score.coin_trend(_coin_closes(prices, "BTC"))
    btc_up = bool(btc_trend and btc_trend["above_ma100"])
    scored = []
    for coin in COINS:
        ticker = crypto.ticker(coin)
        mine = [c for c in flows if c.ticker == ticker]
        bearish = next((c for c in mine if not c.bullish), None)
        caution = None
        if bearish is not None and crypto.trend_confirms_down(trend_fn(conn, coin)):
            caution = f"{bearish.company} — цена подтверждает"
        scored.append(model_score.score_coin(
            coin, _coin_closes(prices, coin), bullish_flow=any(c.bullish for c in mine), caution=caution,
            headlines=news_fn(ticker, "CRYPTO"), btc_up=None if coin in MAJOR_COINS else btc_up))
    return scored


def _flow_is_fresh(sig, since: str) -> bool:
    """A coin flow counts for CAUTION_DAYS from its disclosure date; one with no date
    (a hand-built signal) counts."""
    day = (getattr(sig, "window_end", None) or "")[:10]
    return not day or day >= since


def _kept(s) -> dict:
    """One score as cached_scores gives it back: what the menu and the analyst read."""
    row = {"kind": s.kind, "ticker": s.ticker, "total": s.total, "decision": s.decision,
           "reasons": list(s.reasons), "block": s.block, "stop_pct": s.stop_pct}
    if s.kind == "crypto":
        row.update(company=s.coin, source="CRYPTO", coin=s.coin, trend=s.trend, flows=s.flows,
                   news=s.news, ret60=s.ret60, untradeable=None, t212=None)
    else:
        row.update(company=s.company, source=s.source, insiders=s.insiders, triggers=s.triggers,
                   momentum=s.momentum, news=s.news, untradeable=s.untradeable, t212=s.t212)
    return row


def keep_scores(conn, today: dt.date, scored: list) -> None:
    """Keep the day's scores (kv_cache SCORED_KEY), so the menu and the analyst need not score
    everything again the same day."""
    db.save_cached_json(conn, SCORED_KEY.format(day=today.isoformat()), [_kept(s) for s in scored])


def cached_scores(conn, today: dt.date | None = None) -> list | None:
    """Today's scores as the daily run kept them -- objects with the attributes
    telegram_notify.format_scored and the analyst read -- highest first; None when the model
    has not scored today (another day's scores never count)."""
    today = today or dt.date.today()
    rows = db.get_cached_json(conn, SCORED_KEY.format(day=today.isoformat()))
    if not isinstance(rows, list):
        return None
    return [types.SimpleNamespace(**r) for r in rows if isinstance(r, dict)]


def score_today(conn, today: dt.date | None = None, *, fetch=None, news_fn=None, trend_fn=None,
                signals=None, t212=None, prices=None, prune: bool = True) -> list:
    """Every fresh stock signal and the thirteen coins, scored, highest first. Places nothing.
    `signals` are already-enriched candidates (else the finders run); `t212` is an object
    with can_buy(ticker, source) (else the cached instrument list, if any). `prune=False`
    fetches prices and news even for a stock that can't reach the watchlist (the analyst's
    single-ticker look)."""
    today = today or dt.date.today()
    prices = prices or Prices(fetch, today)
    news_fn = _memoised(news_fn or default_news)
    trend_fn = trend_fn or crypto.price_trend
    if t212 is None:
        t212 = _default_t212(conn)
    candidates = signals if signals is not None else candidate_signals(conn, today)
    scored = (_score_stocks(conn, candidates, prices, today, news_fn, t212, prune)
              + _score_coins(conn, candidates, prices, today, news_fn, trend_fn))
    return sorted(scored, key=lambda s: s.total, reverse=True)


def score_day(conn, today: dt.date | None = None, *, fetch=None, news_fn=None, trend_fn=None,
              signals=None, t212=None) -> ScoreReport:
    """The daily scoring pass: score every fresh stock signal and the thirteen coins (score_today), keep the day's
    scores for the menu and the analyst (keep_scores) and report them. It trades nothing -- there is no
    virtual portfolio -- and touches no table but the kv cache. A scoring that raises is logged and undone:
    the report is then empty and not `complete`. Scores that could not be kept are logged and the pass is
    still complete (the menu and the analyst then score for themselves). The seams are score_today's."""
    today = today or dt.date.today()
    try:
        scored = score_today(conn, today, fetch=fetch, news_fn=news_fn, trend_fn=trend_fn,
                             signals=signals, t212=t212)
    except Exception as e:
        conn.rollback()
        print(f"[model] scoring failed: {type(e).__name__}: {e}", file=sys.stderr)
        return ScoreReport(scored=[], decisions={}, complete=False)
    try:
        keep_scores(conn, today, scored)
    except Exception as e:
        print(f"[model] scores not kept: {type(e).__name__}: {e}", file=sys.stderr)
    return ScoreReport(scored=scored, decisions={s.ticker: s.decision for s in scored})


# ---------------------------------------------------------------- the exit rules positions.py shares
# The exit rules for the user's own positions live in positions.py; these are the constants and the one
# helper they read from here, when they run.
FALLBACK_STOP = {"stock": 0.15, "crypto": 0.25}    # a position with no stop and too little history
DEAD_MONEY_BDAYS = 60
DEAD_MONEY_MIN_RETURN = 0.05
MAX_HOLD_DAYS = 365
STAKE_SOURCE = "SEC13DG"      # a position opened on a 13D/G stake: its filers' later stakes count


def _activist_cut(conn, ticker: str, insiders, opened: str) -> tuple[float, float] | None:
    """(the stake before, the stake since) when one of `insiders` -- the filers of the 13D/G a
    position was bought on -- has reported a smaller stake in `ticker` after `opened` than
    their last one on or before it; else None. The rule behind the activist_cut alert on the user's
    positions (positions._model_exit), which checks the position came from a stake."""
    people = list(insiders or [])
    if not people:
        return None

    def newest(comparison: str) -> float | None:
        row = conn.execute(
            f"SELECT percent_of_class FROM sec_stakes WHERE ticker = ? AND person_name IN "
            f"({','.join('?' * len(people))}) AND event_date {comparison} ? "
            f"AND percent_of_class IS NOT NULL ORDER BY event_date DESC LIMIT 1",
            (ticker, *people, opened)).fetchone()
        return row[0] if row else None

    after, before = newest(">"), newest("<=")
    if after is not None and before is not None and after < before:
        return before, after
    return None
