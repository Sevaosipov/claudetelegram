"""Ordering signals by how much attention they deserve, and the company context
(size, liquidity, cross-source corroboration) that ordering needs."""

from __future__ import annotations

import datetime as dt

import fx

from .common import (
    CAP_BUYERS,
    CAP_CORROBORATION,
    CAP_MARKET_CAP,
    CAP_POSITION,
    CORROBORATION_WINDOW_DAYS,
    FRESH_DECAY_DAYS,
    ILLIQUID_BELOW_EUR,
    MAX_CREDIBLE_PCT_OF_MCAP,
    P_ILLIQUID,
    P_UNKNOWN_SIZE,
    W_FIRST_BUY,
    W_FRESH,
    W_FULL_UNWIND,
    W_HAS_OFFICER,
    W_PCT_OF_MARKET_CAP,
    W_PER_CORROBORATING_SOURCE,
    W_PER_EXTRA_BUYER,
    W_POSITION_INCREASE,
    _regime,
)


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

    Asymmetric on purpose: the batch half sees every signal enrich_signals was
    handed, including ones bot.py's --min-score/--min-liquidity will later
    filter out and never send, while the history half only ever sees signals
    that were actually journaled (i.e. actually sent) -- "another regime had
    activity" still holds true either way, so this doesn't change the result,
    just what counted toward it.

    "Source" and "regime" aren't always the same thing -- see _REGIME: SEC and
    SEC13DG collapse to one regime, and HOUSE/SENATE collapse to another, so
    e.g. a Form 4 cluster and a same-ticker 13D/G stake, or a House PTR and a
    Senate PTR, don't corroborate each other. The stored/displayed source name
    is still the real one (e.g. "SEC13DG", "SENATE"), only the independence
    check is regime-based.
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
        all_sources = batch_sources[sig.ticker] | journal_sources[sig.ticker]
        own_regime = _regime(sig.source)
        sig.corroborated_by = sorted(s for s in all_sources if _regime(s) != own_regime)
def enrich_signals(conn, signals: list) -> list:
    """Attach company context to signals and score them, newest-first by score.

    Split out from the finders on purpose: this is the only part that needs the
    network (market cap and traded volume), so the finders stay pure SQL and the
    test suite stays offline and fast.
    """
    import marketcap

    find_corroboration(conn, signals)

    for sig in signals:
        cap = marketcap.market_cap_eur(conn, sig.ticker, sig.source)
        facts = marketcap.facts(conn, sig.ticker, sig.source) or {}
        sig.market_cap_eur = cap
        sig.avg_daily_value = (fx.to_eur(facts["avg_daily_value"], facts.get("currency") or "USD", conn)
                                if facts.get("avg_daily_value") else None)
        total = getattr(sig, "total_value", None)
        pct = (total / cap * 100) if (cap and total) else None
        # Keep an implausible ratio out of the score but still visible in the
        # journal, so backtest.py can see how often the data goes wrong.
        sig.value_pct_of_mcap = pct
        sig.score = score_signal(sig, sig.corroborated_by)
    signals.sort(key=lambda x: getattr(x, "score", 0.0), reverse=True)
    return signals
