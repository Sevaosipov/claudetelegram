"""model.py: the model portfolio -- one virtual portfolio that trades model_score's strategy
on paper.py's engine (spec 2026-09-30, section 4).

Two paper books hold it: MODEL-S (stocks, EUR 70k, against SPY) and MODEL-C (BTC and ETH,
EUR 30k, against BTC-USD). Orders, fills at the next close, fees, FX, adjusted-close
valuation and the daily snapshot are paper.py's own; this module only decides what to trade.

The daily pass (run) fills and revalues both books, sells what an exit rule says, scores
every fresh signal and both coins (score_today, which places nothing, so the menu and the
analyst can use it too), buys what scores BUY -- sized by its own stop -- and snapshots. One
sleeve failing is logged and skipped; the other carries on.

Everything that touches the network sits behind a seam (fetch, news_fn, trend_fn,
sector_fn, t212), so a test hands each its own stub.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import functools
import json
import sys
import types
from collections.abc import Callable
from dataclasses import dataclass

import assets
import cluster
import crypto
import model_score
import paper
import positions
import sources
import strategy

STOCK_BOOK, CRYPTO_BOOK = "MODEL-S", "MODEL-C"
BOOKS = (STOCK_BOOK, CRYPTO_BOOK)
STOCK_START_EUR = 70_000.0
CRYPTO_START_EUR = 30_000.0
COINS = ("BTC", "ETH")
MODEL_SIGNAL_DAYS = 14        # a stock is scored while its signal was disclosed this recently
NEWS_DAYS = 14
NEWS_MIN_PRESCORE = 35.0      # headlines are fetched only for a stock scoring this without them
MAX_STOCK_POSITIONS = 12      # open plus pending buys
MAX_PER_SECTOR = 3
REBUY_COOLDOWN_DAYS = 30
COIN_REBUY_COOLDOWN_DAYS = 7  # a coin's trend can stay up through a stop-out: don't buy it straight back
DEAD_MONEY_BDAYS = 60
DEAD_MONEY_MIN_RETURN = 0.05
MAX_HOLD_DAYS = 365
CAUTION_DAYS = 7              # a coin's inflow or outflow signal counts this long
STAKE_LOOKBACK_DAYS = 30
PASSIVE_BIG_PERCENT = 10.0    # a 13G at least this large counts as a trigger
MIN_FILL_FRACTION = 0.5       # a buy with less cash than its size goes ahead if the cash is this much of it
FALLBACK_STOP = {"stock": 0.15, "crypto": 0.25}    # a position with no stop and too little history
FINDER_CLUSTER_KWARGS = {"min_value": 50_000, "solo_threshold": 250_000}
FINDER_STAKE_KWARGS = {"min_percent": 5.0, "activist_only": False,
                       "new_positions_only": False, "max_age_days": 14}
_POLITICIANS = ("HOUSE", "SENATE")
# (code, sleeve, starting money, benchmark symbol): the 70/30 mix of S&P 500 and Bitcoin
_BOOK_SPECS = ((STOCK_BOOK, "stock", STOCK_START_EUR, "SPY"),
               (CRYPTO_BOOK, "crypto", CRYPTO_START_EUR, "BTC-USD"))


@dataclass
class Trade:
    side: str                 # "buy" | "sell"
    ticker: str
    company: str
    amount_eur: float | None  # buy: the order's amount; sell: the position's last value
    stop_pct: float | None
    score: float | None
    reasons: list[str]        # buy: the score's reasons (max 3); sell: [the exit reason]
    result: float | None      # sell: last_value / cost_eur - 1
    t212: bool | None = None


@dataclass
class DayReport:
    buys: list[Trade]
    sells: list[Trade]
    scored: list              # StockScore and CoinScore, highest total first
    value: float | None       # MODEL-S + MODEL-C value today
    bench: float | None       # the two books' benchmark values summed (the 70/30 mix)
    decisions: dict[str, str]  # ticker -> decision, for the journal


# ---------------------------------------------------------------- seams
def default_news(ticker: str, source: str | None) -> list[dict]:
    """The last NEWS_DAYS days of headlines on a stock or a coin ({"title", "published"}),
    [] when there are none or the sources fail. A date that can't be read keeps the item."""
    try:
        if crypto.is_crypto(ticker):
            asset = assets.crypto_asset(crypto.symbol_of(ticker))
        else:
            listed = paper.listing(ticker, source)
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


def _default_sector(ticker: str, source: str | None) -> str | None:
    """The company's sector according to yfinance, or None (no listing, no data, no network).
    Asked on the listing's own Yahoo symbol: research._yf_info would turn EQNR.OL into EQNR-OL."""
    try:
        import yfinance as yf
        listed = paper.listing(ticker, source)
        return ((yf.Ticker(listed[0]).info or {}).get("sector") or None) if listed else None
    except Exception as e:
        print(f"[model] no sector for {ticker}: {type(e).__name__}: {e}", file=sys.stderr)
        return None


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


# ---------------------------------------------------------------- books & candidates
def create_books(conn, today: dt.date) -> None:
    """Both books, once -- the first run is their start date."""
    for code, sleeve, start, bench in _BOOK_SPECS:
        conn.execute(
            "INSERT OR IGNORE INTO paper_books (code, sleeve, start_date, start_eur, cash_eur, "
            "bench_symbol) VALUES (?,?,?,?,?,?)", (code, sleeve, today.isoformat(), start, start, bench))
    conn.commit()


def model_value(conn) -> float:
    """Both books' value; a book that doesn't exist yet is worth nothing."""
    return sum(paper.book_value(conn, code) for code in BOOKS
               if conn.execute("SELECT 1 FROM paper_books WHERE code = ?", (code,)).fetchone())


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
def _context(conn, candidates: list, today: dt.date) -> tuple[set[str], set[str], set[str]]:
    """The tickers that an activist 13D, a big passive 13G or politicians are buying:
    from today's candidates and from the stake filings of the last STAKE_LOOKBACK_DAYS."""
    since = (today - dt.timedelta(days=STAKE_LOOKBACK_DAYS)).isoformat()
    activist = {r[0] for r in conn.execute(
        "SELECT ticker FROM sec_stakes WHERE form_type LIKE '%13D%' AND event_date >= ?", (since,))}
    passive = {r[0] for r in conn.execute(
        "SELECT ticker FROM sec_stakes WHERE form_type LIKE '%13G%' AND percent_of_class >= ? "
        "AND event_date >= ?", (PASSIVE_BIG_PERCENT, since))}
    politicians: set[str] = set()
    for c in candidates:
        if getattr(c, "source", None) in _POLITICIANS:
            politicians.add(c.ticker)
        if hasattr(c, "percent"):
            if c.is_activist:
                activist.add(c.ticker)
            elif c.percent >= PASSIVE_BIG_PERCENT:
                passive.add(c.ticker)
    return activist, passive, politicians


def _score_stocks(conn, candidates, prices, today, news_fn, t212) -> list:
    activist, passive, politicians = _context(conn, candidates, today)
    best: dict[str, model_score.StockScore] = {}
    for sig in candidates:
        if hasattr(sig, "crypto_kind") or crypto.is_crypto(sig.ticker):
            continue
        listed = paper.listing(sig.ticker, sig.source)
        closes = [c for _d, c in prices.bars(listed[0])] if listed else []
        flags = {"activist": sig.ticker in activist, "passive_big": sig.ticker in passive,
                 "politicians": sig.ticker in politicians,
                 "t212": None if t212 is None else t212.can_buy(sig.ticker, sig.source)}
        score = model_score.score_stock(sig, closes, None, **flags)
        if score.total >= NEWS_MIN_PRESCORE:
            score = model_score.score_stock(sig, closes, news_fn(sig.ticker, sig.source), **flags)
        if sig.ticker not in best or score.total > best[sig.ticker].total:
            best[sig.ticker] = score
    return list(best.values())


def _score_coins(conn, candidates, prices, today, news_fn, trend_fn) -> list:
    since = (today - dt.timedelta(days=CAUTION_DAYS)).isoformat()
    flows = [c for c in candidates if hasattr(c, "crypto_kind") and _flow_is_fresh(c, since)]
    scored = []
    for coin in COINS:
        ticker = crypto.ticker(coin)
        mine = [c for c in flows if c.ticker == ticker]
        bearish = next((c for c in mine if not c.bullish), None)
        caution = None
        if bearish is not None and crypto.trend_confirms_down(trend_fn(conn, coin)):
            caution = f"{bearish.company} — цена подтверждает"
        closes = [c for _d, c in prices.bars(paper.listing(ticker, "CRYPTO")[0])]
        scored.append(model_score.score_coin(
            coin, closes, bullish_flow=any(c.bullish for c in mine), caution=caution,
            headlines=news_fn(ticker, "CRYPTO")))
    return scored


def _flow_is_fresh(sig, since: str) -> bool:
    """A coin flow counts for CAUTION_DAYS from its disclosure date; one with no date
    (a hand-built signal) counts."""
    day = (getattr(sig, "window_end", None) or "")[:10]
    return not day or day >= since


def score_today(conn, today: dt.date | None = None, *, fetch=None, news_fn=None, trend_fn=None,
                signals=None, t212=None, prices=None) -> list:
    """Every fresh stock signal and both coins, scored, highest first. Places nothing.
    `signals` are already-enriched candidates (else the finders run); `t212` is an object
    with can_buy(ticker, source) (else the cached instrument list, if any)."""
    today = today or dt.date.today()
    prices = prices or paper.Prices(fetch, today)
    news_fn = _memoised(news_fn or default_news)
    trend_fn = trend_fn or crypto.price_trend
    if t212 is None:
        t212 = _default_t212(conn)
    candidates = signals if signals is not None else candidate_signals(conn, today)
    scored = (_score_stocks(conn, candidates, prices, today, news_fn, t212)
              + _score_coins(conn, candidates, prices, today, news_fn, trend_fn))
    return sorted(scored, key=lambda s: s.total, reverse=True)


# ---------------------------------------------------------------- exits
def _trailing_stop(pos: dict, bars: list[tuple[str, float]], kind: str) -> str | None:
    """The close is `stop_pct` or more below the highest close since the fill. A position
    without a stored stop takes the one its own history before the fill would have given."""
    since_fill = [c for d, c in bars if d >= pos["fill_date"]]
    if not since_fill:
        return None
    stop = pos["stop_pct"]
    if stop is None:
        stop = (model_score.stop_distance([c for d, c in bars if d <= pos["fill_date"]], kind)
                or FALLBACK_STOP[kind])
    if bars[-1][1] <= max(since_fill) * (1 - stop):
        return f"стоп: −{stop * 100:.0f}% от максимума"
    return None


def _activist_cut(conn, pos: dict) -> bool:
    """A position bought on a 13D/G whose filer has since reported a smaller stake."""
    people = json.loads(pos["insiders"] or "[]")
    if pos["source"] != "SEC13DG" or not people:
        return False

    def newest(comparison: str) -> float | None:
        row = conn.execute(
            f"SELECT percent_of_class FROM sec_stakes WHERE ticker = ? AND person_name IN "
            f"({','.join('?' * len(people))}) AND event_date {comparison} ? "
            f"AND percent_of_class IS NOT NULL ORDER BY event_date DESC LIMIT 1",
            (pos["ticker"], *people, pos["fill_date"])).fetchone()
        return row[0] if row else None

    after, before = newest(">"), newest("<=")
    return after is not None and before is not None and after < before


def _news_red_flag(headlines, *, coin: bool = False) -> str | None:
    """The first red-flag headline. `headlines` may be a function returning them, called
    only now, so the earlier rules can decide without a news fetch."""
    if callable(headlines):
        headlines = headlines()
    return model_score.news_part(headlines, coin=coin)[1]


def stock_exit_reason(conn, pos: dict, bars: list[tuple[str, float]], today: dt.date,
                      headlines: list[dict] | None | Callable[[], list[dict]]) -> str | None:
    """Why a stock position should be sold, or None. The first rule that holds wins."""
    reason = _trailing_stop(pos, bars, "stock")
    if reason:
        return reason
    sale = positions._insider_sale(conn, types.SimpleNamespace(
        ticker=pos["ticker"], opened_at=pos["fill_date"], insiders=json.loads(pos["insiders"] or "[]")))
    if sale:
        return f"продаёт инсайдер: {sale}"
    if _activist_cut(conn, pos):
        return "активист сократил долю"
    if (pos["last_value"] is not None
            and paper.business_days_between(pos["fill_date"], today) >= DEAD_MONEY_BDAYS
            and pos["last_value"] / pos["net_eur"] - 1 < DEAD_MONEY_MIN_RETURN):
        return "стоит на месте"
    if (today - dt.date.fromisoformat(pos["fill_date"])).days >= MAX_HOLD_DAYS:
        return "год в позиции"
    red = _news_red_flag(headlines)
    return f"новости: {red}" if red else None


def coin_exit_reason(conn, pos: dict, bars: list[tuple[str, float]], today: dt.date,
                     headlines: list[dict] | None | Callable[[], list[dict]],
                     trend_fn) -> str | None:
    """Why a coin position should be sold, or None. The first rule that holds wins."""
    reason = _trailing_stop(pos, bars, "crypto")
    if reason:
        return reason
    trend = model_score.coin_trend([c for _d, c in bars])
    if trend and trend["down"]:
        return "тренд вниз"
    caution = positions._crypto_caution(
        conn, types.SimpleNamespace(ticker=pos["ticker"], opened_at=pos["fill_date"]), today, trend_fn)
    if caution:
        return f"осторожно: {caution}"
    red = _news_red_flag(headlines, coin=True)
    return f"новости: {red}" if red else None


# ---------------------------------------------------------------- the daily pass
@contextlib.contextmanager
def _sleeve(conn, code: str, failed: set[str]):
    """A failure inside is logged and undone, and marks the sleeve failed for the rest of
    the pass: the other sleeve carries on."""
    try:
        yield
    except Exception as e:
        conn.rollback()
        failed.add(code)
        print(f"[model] {code} failed: {type(e).__name__}: {e}", file=sys.stderr)


def _sell_exits(conn, code, prices, today, news_fn, trend_fn, sells: list[Trade]) -> None:
    """A sale order for every open position an exit rule fires on (one already being
    sold is left alone, and not reported again)."""
    selling = {o["position_id"] for o in paper.pending_orders(conn, code) if o["side"] == "sell"}
    for pos in paper.open_positions(conn, code):
        if pos["id"] in selling:
            continue
        bars = prices.bars(pos["symbol"], paper.history_days(pos["fill_date"], today))
        headlines = functools.partial(news_fn, pos["ticker"], pos["source"])   # fetched last, if at all
        if code == STOCK_BOOK:
            reason = stock_exit_reason(conn, pos, bars, today, headlines)
        else:
            reason = coin_exit_reason(conn, pos, bars, today, headlines, trend_fn)
        if not reason:
            continue
        paper.place_sell(conn, code, pos, reason, today)
        value = pos["last_value"] if pos["last_value"] is not None else pos["net_eur"]
        sells.append(Trade("sell", pos["ticker"], crypto.symbol_of(pos["ticker"]), value, pos["stop_pct"],
                           pos["score"], [reason], value / pos["cost_eur"] - 1))


def _reason(s) -> str:
    return f"балл {s.total:.0f}: " + "; ".join(s.reasons[:3])


def _skip(conn, code: str, s, source: str, today: dt.date, note: str) -> None:
    paper._record(conn, code, s.ticker, source, "buy", _reason(s), today, "skipped", note=note,
                  stop_pct=s.stop_pct, score=s.total)


def _order_buy(conn, code: str, s, source: str, kind: str, today: dt.date,
               insiders=()) -> float | None:
    """Size a buy from the stop and queue it. The order's amount when it is pending, else
    None (recorded as skipped: «мало» here, «нет денег» or «нет котировки» by place_buy)."""
    size = model_score.position_size(model_value(conn), paper.book_value(conn, code), s.stop_pct, kind)
    if size < model_score.MIN_ORDER_EUR:
        _skip(conn, code, s, source, today, "мало")
        return None
    result = paper.place_buy(conn, code, s.ticker, source, _reason(s), today, size,
                             min_fraction=MIN_FILL_FRACTION,
                             insiders=insiders, stop_pct=s.stop_pct, score=s.total)
    return paper.pending_orders(conn, code)[-1]["amount_eur"] if result == "pending" else None


def _pending_buys(conn, code: str) -> list[dict]:
    return [o for o in paper.pending_orders(conn, code) if o["side"] == "buy"]


def _recently_sold(conn, code: str, today: dt.date, days: int) -> set[str]:
    cutoff = (today - dt.timedelta(days=days)).isoformat()
    return {p["ticker"] for p in paper.closed_positions(conn, code) if p["closed_date"] >= cutoff}


def _buy_stocks(conn, scored, today, sector_of, buys: list[Trade]) -> None:
    recently_sold = _recently_sold(conn, STOCK_BOOK, today, REBUY_COOLDOWN_DAYS)
    for s in scored:
        if s.kind != "stock" or s.decision != model_score.BUY:
            continue
        held, pending = paper.open_positions(conn, STOCK_BOOK), _pending_buys(conn, STOCK_BOOK)
        if s.ticker in {p["ticker"] for p in held + pending}:
            continue
        note = None
        if s.ticker in recently_sold:
            note = "недавно продан"
        elif len(held) + len(pending) >= MAX_STOCK_POSITIONS:
            note = "мест нет"
        elif _sector_full(sector_of, s, held + pending):
            note = "сектор заполнен"
        if note:
            _skip(conn, STOCK_BOOK, s, s.source, today, note)
            continue
        amount = _order_buy(conn, STOCK_BOOK, s, s.source, "stock", today, paper.insiders_of(s.signal))
        if amount is not None:
            buys.append(Trade("buy", s.ticker, s.company, amount, s.stop_pct, s.total,
                              s.reasons[:3], None, s.t212))


def _sector_full(sector_of, s, holdings: list[dict]) -> bool:
    """MAX_PER_SECTOR held or pending names already share this stock's sector (an
    unknown sector has no limit)."""
    sector = sector_of(s.ticker, s.source)
    if sector is None:
        return False
    return sum(sector_of(h["ticker"], h["source"]) == sector for h in holdings) >= MAX_PER_SECTOR


def _buy_coins(conn, scored, today, buys: list[Trade]) -> None:
    recently_sold = _recently_sold(conn, CRYPTO_BOOK, today, COIN_REBUY_COOLDOWN_DAYS)
    for s in scored:
        if s.kind != "crypto" or s.decision != model_score.BUY:
            continue
        held = paper.open_positions(conn, CRYPTO_BOOK) + _pending_buys(conn, CRYPTO_BOOK)
        if s.ticker in {p["ticker"] for p in held}:
            continue
        if s.ticker in recently_sold:
            _skip(conn, CRYPTO_BOOK, s, "CRYPTO", today, "недавно продан")
            continue
        amount = _order_buy(conn, CRYPTO_BOOK, s, "CRYPTO", "crypto", today)
        if amount is not None:
            buys.append(Trade("buy", s.ticker, s.coin, amount, s.stop_pct, s.total,
                              s.reasons[:3], None))


def _bench_today(conn, today: dt.date) -> float | None:
    """The two books' benchmarks summed, or None when either has none for today."""
    values = [conn.execute("SELECT bench FROM paper_equity WHERE book = ? AND date = ?",
                           (code, today.isoformat())).fetchone() for code in BOOKS]
    if any(v is None or v[0] is None for v in values):
        return None
    return sum(v[0] for v in values)


def run(conn, today: dt.date | None = None, *, fetch=None, news_fn=None, trend_fn=None,
        sector_fn=None, signals=None, t212=None) -> DayReport:
    """One daily pass over both books (see the module docstring). Returns what was
    ordered today, everything scored, and where the model stands against its benchmark."""
    today = today or dt.date.today()
    news_fn = _memoised(news_fn or default_news)
    trend_fn = trend_fn or crypto.price_trend
    sector_of = _memoised(sector_fn or _default_sector)
    create_books(conn, today)
    prices = paper.Prices(fetch, today)
    buys: list[Trade] = []
    sells: list[Trade] = []
    failed: set[str] = set()

    for code in BOOKS:
        with _sleeve(conn, code, failed):
            paper.fill_orders(conn, code, prices, today)
            paper.mark_to_market(conn, code, prices, today)
            _sell_exits(conn, code, prices, today, news_fn, trend_fn, sells)

    try:
        scored = score_today(conn, today, fetch=fetch, news_fn=news_fn, trend_fn=trend_fn,
                             signals=signals, t212=t212, prices=prices)
    except Exception as e:      # no scores means no buys, but the books are still stamped
        conn.rollback()
        print(f"[model] scoring failed: {type(e).__name__}: {e}", file=sys.stderr)
        scored = []

    if STOCK_BOOK not in failed:
        with _sleeve(conn, STOCK_BOOK, failed):
            _buy_stocks(conn, scored, today, sector_of, buys)
    if CRYPTO_BOOK not in failed:
        with _sleeve(conn, CRYPTO_BOOK, failed):
            _buy_coins(conn, scored, today, buys)
    for code in BOOKS:
        if code not in failed:
            with _sleeve(conn, code, failed):
                paper._snapshot(conn, code, prices, today)

    return DayReport(buys=buys, sells=sells, scored=scored, value=model_value(conn),
                     bench=_bench_today(conn, today), decisions={s.ticker: s.decision for s in scored})
