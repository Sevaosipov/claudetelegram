"""Signal strategy v2: which buy signals are "Сильный" (act the same day), which are
"Кандидат" (consider within days), and which don't make the list at all.

Spec: docs/superpowers/specs/2026-09-23-signal-strategy-v2-design.md. The rules are
explicit on purpose -- every kept signal carries the rules it met and missed, so an
alert can say *why* it is strong, and a rule can be retuned without re-deriving a
score. The numbers are starting points, checked by calibrate_strategy.py against
stored history, not findings.

Order of work in select(): cheap filters first (buy side, disclosed recently, on
Trading 212), then enrich_signals -- the step that reaches the network for market
cap and liquidity -- only for what survived, then the tier rules.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import cluster
import crypto
from cluster.roles import INSIDER_ROLES, TOP_EXEC_ROLES

MAX_AGE_DAYS = 3                    # by disclosure date (cluster/recency.py)
FLOOR_MIN_MCAP_EUR = 300e6
FLOOR_MIN_ADV_EUR = 1e6
STRONG_MIN_INSIDERS = 3             # rule (a)
TOP_EXEC_MIN_INSIDERS = 2           # rule (b)
TOP_EXEC_MIN_EUR = 250_000          # rule (b)
CONVICTION_MIN_EUR = 250_000        # rule (c)
CONVICTION_MIN_INCREASE_PCT = 10.0  # rule (c)
MAX_CANDIDATES = 10
CANDIDATE_MIN_SCORE = 50.0
# The same bar as the weekly company-demand floor in cluster/crypto.py, so one
# constant: the two can't drift apart.
CRYPTO_TREASURY_BIG_EUR = cluster.crypto.TREASURY_WEEK_FLOOR_EUR

# Small companies (€50-300M): the band FLOOR_MIN_MCAP_EUR used to drop. Their rule is
# scaled to the company -- purchases of at least HIGH_RISK_MIN_PCT_OF_MCAP percent of
# its value (spec: docs/superpowers/specs/2026-09-28-high-risk-small-companies-design.md).
HIGH_RISK_MIN_MCAP_EUR = 50e6
HIGH_RISK_MIN_ADV_EUR = 100_000
HIGH_RISK_MIN_PCT_OF_MCAP = 0.1         # percent of market value
HIGH_RISK_MIN_INSIDERS = 2
HIGH_RISK_SOURCES = ("SEC", "NORWAY")
# The finders' own bars (€100k cluster total, €500k solo) were set for large
# companies; a small company's 0.1% can be €50k. A second, lower-threshold pass
# feeds only the high-risk rule -- its extra signals never get a main tier.
HIGH_RISK_FINDER_MIN_EUR = 50_000

STRONG, CANDIDATE, CAUTION, HIGH_RISK = "strong", "candidate", "caution", "high_risk"
_ROLE_LABEL = {"ceo": "CEO", "cfo": "CFO", "chair": "Chair"}


@dataclass
class Tiered:
    signal: object
    tier: str
    met: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)


@dataclass
class Selection:
    strong: list[Tiered]
    candidates: list[Tiered]
    t212_checked: bool
    # Exit signals from the input, unfiltered and untiered -- see select()'s
    # docstring for why they bypass recency/T212/size entirely.
    exits: list = field(default_factory=list)
    # Bearish crypto signals (ETF outflows, company sales, coins moving onto
    # exchanges): shown in the menu's Сигналы, journaled, never pushed -- see
    # bot._record_cautions and positions.check_exits.
    cautions: list = field(default_factory=list)
    # Small-company insider buying that clears the size-scaled rule: shown in the
    # menu's Сигналы and traded by the paper books H1/H2, never pushed.
    high_risk: list = field(default_factory=list)


def _short(v: float) -> str:
    for unit, suffix in ((1e9, "млрд"), (1e6, "млн"), (1e3, "тыс")):
        if abs(v) >= unit:
            return f"{v / unit:,.1f} {suffix}"
    return f"{v:,.0f}"


def is_buy_side(sig) -> bool:
    if hasattr(sig, "seller_count"):          # ExitSignal
        return False
    if hasattr(sig, "crypto_kind"):           # CryptoSignal
        return bool(sig.bullish)
    return True


def is_caution(sig) -> bool:
    return hasattr(sig, "crypto_kind") and not sig.bullish


def _stock_tier(sig) -> Tiered | None:
    met, missed = [], []
    is_coin = crypto.is_crypto(sig.ticker)    # a congressional crypto buy
    size_known = True
    if not is_coin:
        cap, adv = getattr(sig, "market_cap_eur", None), getattr(sig, "avg_daily_value", None)
        if (cap is not None and cap < FLOOR_MIN_MCAP_EUR) or \
                (adv is not None and adv < FLOOR_MIN_ADV_EUR):
            return None
        size_known = cap is not None and adv is not None
        if size_known:
            met.append(f"€{_short(cap)} / €{_short(adv)} в день")
        else:
            missed.append("размер неизвестен")

    buyers = getattr(sig, "buyers", None) or []
    insiders = [b for b in buyers if b.role in INSIDER_ROLES]
    tops = [b for b in insiders if b.role in TOP_EXEC_ROLES]
    rules = []
    if len(insiders) >= STRONG_MIN_INSIDERS:
        rules.append(f"{len(insiders)} инсайдера(ов) из руководства")
    big_tops = [b for b in tops if b.total_eur >= TOP_EXEC_MIN_EUR]
    if len(insiders) >= TOP_EXEC_MIN_INSIDERS and big_tops:
        top = big_tops[0]
        rules.append(f"{_ROLE_LABEL[top.role]} среди покупателей (€{_short(top.total_eur)})")
    for b in insiders:
        if (b.role in ("ceo", "cfo") and b.total_eur >= CONVICTION_MIN_EUR
                and (b.increase_pct or 0) >= CONVICTION_MIN_INCREASE_PCT):
            rules.append(f"{_ROLE_LABEL[b.role]} купил на €{_short(b.total_eur)} "
                         f"(+{b.increase_pct:.0f}% к позиции)")
            break
    met += rules
    if not insiders:
        missed.append("покупатели не из руководства компании")
    elif not rules:
        if tops and not big_tops:
            # A top exec is in the cluster but didn't clear rule (b)'s own bar --
            # say so specifically, naming the biggest of them by amount, rather
            # than a generic line, which would otherwise read as "no CEO/CFO/
            # Chair in the cluster" while one is right there in `met`.
            biggest = max(tops, key=lambda b: b.total_eur)
            missed.append(f"{_ROLE_LABEL[biggest.role]} купил только на "
                          f"€{_short(biggest.total_eur)} (< €{_short(TOP_EXEC_MIN_EUR)})")
        elif tops:
            # A top exec IS here and DID clear TOP_EXEC_MIN_EUR -- since rules is
            # still empty, len(insiders) must be < TOP_EXEC_MIN_INSIDERS (i.e. a
            # lone insider: with 2+, big_tops alone would have fired rule (b)
            # above), and rule (c) either doesn't apply to their role (Chair) or
            # their position didn't grow enough. The "bought only €X" line above
            # would be false here -- they cleared the money bar -- so state what
            # the rules need in general instead of claiming this buyer fell short
            # on amount.
            missed.append("один покупатель из руководства: нужно 2+ (с CEO/CFO/Chair "
                          "от €250 тыс) или рост позиции CEO/CFO от 10%")
        else:
            missed.append("нет 3+ инсайдеров, CEO/CFO/Chair с покупкой от €250 тыс "
                          "или крупной покупки CEO/CFO")
    tier = STRONG if (rules and size_known and not is_coin) else CANDIDATE
    return Tiered(sig, tier, met, missed)


def _trend_desc(coin: str, trend: dict) -> str:
    return (f"{coin} {trend['ret_7d']:+.1f}% за 7 дн., "
            f"{'выше' if trend['above_ma20'] else 'ниже'} 20-дн. средней")


def _crypto_tier(conn, sig) -> Tiered | None:
    if not sig.bullish:
        return None
    if sig.crypto_kind == "treasury" and (sig.total_value or 0) < CRYPTO_TREASURY_BIG_EUR:
        return None
    met = ["крупный приток"]
    trend = crypto.price_trend(conn, sig.coin)
    if trend is None:
        return Tiered(sig, CANDIDATE, met, ["цена не проверена"])
    desc = _trend_desc(sig.coin, trend)
    if crypto.trend_confirms(trend):
        return Tiered(sig, STRONG, met + [desc], [])
    return Tiered(sig, CANDIDATE, met, [f"цена не подтверждает: {desc}"])


def _caution_tier(conn, sig) -> Tiered:
    trend = crypto.price_trend(conn, sig.coin)
    if trend is None:
        return Tiered(sig, CAUTION, [], ["цена не проверена"])
    desc = _trend_desc(sig.coin, trend)
    if crypto.trend_confirms_down(trend):
        return Tiered(sig, CAUTION, [f"цена подтверждает: {desc}"], [])
    return Tiered(sig, CAUTION, [], [f"цена не подтверждает: {desc}"])


def _in_high_risk_band(sig) -> bool:
    cap, adv = getattr(sig, "market_cap_eur", None), getattr(sig, "avg_daily_value", None)
    return (sig.source in HIGH_RISK_SOURCES and not crypto.is_crypto(sig.ticker)
            and cap is not None and adv is not None
            and HIGH_RISK_MIN_MCAP_EUR <= cap < FLOOR_MIN_MCAP_EUR
            and adv >= HIGH_RISK_MIN_ADV_EUR)


def _pct_text(pct: float) -> str:
    return f"{pct:.2f}".replace(".", ",")


def _high_risk_tier(sig) -> Tiered | None:
    """A small company: two or more management insiders buying together at least
    HIGH_RISK_MIN_PCT_OF_MCAP of its value, or a CEO/CFO buying that much alone."""
    cap = sig.market_cap_eur
    insiders = [b for b in (getattr(sig, "buyers", None) or []) if b.role in INSIDER_ROLES]
    size = f"€{_short(cap)} / €{_short(sig.avg_daily_value)} в день"
    together = sum(b.total_eur for b in insiders) / cap * 100
    if len(insiders) >= HIGH_RISK_MIN_INSIDERS and together >= HIGH_RISK_MIN_PCT_OF_MCAP:
        return Tiered(sig, HIGH_RISK,
                      [size, f"{len(insiders)} инсайдера(ов) купили вместе {_pct_text(together)}% компании"])
    for b in sorted(insiders, key=lambda b: b.total_eur, reverse=True):
        pct = b.total_eur / cap * 100
        if b.role in ("ceo", "cfo") and pct >= HIGH_RISK_MIN_PCT_OF_MCAP:
            return Tiered(sig, HIGH_RISK, [size, f"{_ROLE_LABEL[b.role]} купил {_pct_text(pct)}% компании"])
    return None


_BUY_SIDE_SOURCES = ("sec", "house", "senate", "bafin", "norway", "sweden", "crypto")

# run_daily.sh's own tuning: the mandatory-filing 5%/13G default is mostly routine
# 13G ownership crossings, not news.
_STAKE_DEFAULTS = {"min_percent": 10.0, "activist_only": True,
                   "new_positions_only": True, "max_age_days": 30}


def buy_side_signals(conn, *, ignore_alert_state: bool = False, sources: dict | None = None,
                      onchain: bool = False, high_risk: bool = True, sec_kwargs: dict | None = None,
                      sweden_kwargs: dict | None = None, stake_kwargs: dict | None = None,
                      cluster_kwargs: dict | None = None) -> list:
    """One definition of the buy-side finder list, shared by bot.run_cluster_pass,
    menu._find_signals and calibrate_strategy._signals -- the three had drifted out
    of sync with each other (stake max_age_days=30 only in menu/calibration;
    on-chain always on in menu, opt-in via --onchain in bot, never run in
    calibration; Senate missing from calibration entirely).

    Runs every buy-side finder -- SEC/House/Senate/BaFin/Norway/Sweden clusters,
    13D/G stakes, crypto treasury purchases and spot-ETF inflows -- plus on-chain
    flows when `onchain` is set. Exit finders (people who bought together later
    selling together) are a different concern -- see exit_signals() below.

    `sources` follows bot._which_sources' shape: a dict of source name -> bool,
    with an optional "stakes" key that gates 13D/G independently of "sec" (bot's
    --forms flag can select SEC forms without 13D/13G). None (the default) runs
    every source, Senate included; a key an explicit dict leaves out means OFF,
    not on.

    `sec_kwargs` and `sweden_kwargs` layer their finder's own extra knobs
    (include_derivatives/insiders_only/include_10b5_1 for SEC;
    include_share_programs/include_derivatives for Sweden) on top of
    `cluster_kwargs` (min_value, solo_threshold), which every cluster finder
    shares. `stake_kwargs` defaults to run_daily.sh's tuning -- see
    _STAKE_DEFAULTS -- rather than find_stake_signals' bare defaults.

    `high_risk` adds a second, lower-threshold pass of the SEC and Oslo finders
    (HIGH_RISK_FINDER_MIN_EUR for both the cluster total and a lone buyer) for the
    small-company rule; its extra signals are tagged `high_risk_only`.
    """
    on = sources if sources is not None else {k: True for k in _BUY_SIDE_SOURCES}
    cluster_kwargs = cluster_kwargs or {}
    sec_kwargs = sec_kwargs or {}
    sweden_kwargs = sweden_kwargs or {}
    stake_kwargs = {**_STAKE_DEFAULTS, **(stake_kwargs or {})}

    # A key this caller's own dict left out means OFF -- only the all-True dict
    # comprehension above (sources=None) means "run everything". `.get(key, True)`
    # used to default a missing key to ON even in an explicit dict, silently
    # running sources the caller never asked for.
    signals = []
    if on.get("sec", False):
        signals += cluster.find_sec_clusters(conn, ignore_alert_state=ignore_alert_state,
                                              **cluster_kwargs, **sec_kwargs)
    if on.get("house", False):
        signals += cluster.find_house_clusters(conn, ignore_alert_state=ignore_alert_state,
                                                **cluster_kwargs)
    if on.get("senate", False):
        signals += cluster.find_senate_clusters(conn, ignore_alert_state=ignore_alert_state,
                                                 **cluster_kwargs)
    if on.get("bafin", False):
        signals += cluster.find_bafin_clusters(conn, ignore_alert_state=ignore_alert_state,
                                                **cluster_kwargs)
    if on.get("norway", False):
        signals += cluster.find_norway_clusters(conn, ignore_alert_state=ignore_alert_state,
                                                 **cluster_kwargs)
    if on.get("sweden", False):
        signals += cluster.find_sweden_clusters(conn, ignore_alert_state=ignore_alert_state,
                                                 **cluster_kwargs, **sweden_kwargs)
    if on.get("stakes", on.get("sec", False)):
        signals += cluster.find_stake_signals(conn, ignore_alert_state=ignore_alert_state,
                                              **stake_kwargs)
    if on.get("crypto", False):
        signals += cluster.find_treasury_signals(conn, ignore_alert_state=ignore_alert_state)
        signals += cluster.find_etf_flow_signals(conn, ignore_alert_state=ignore_alert_state)
    if onchain:
        signals += cluster.find_onchain_signals(conn, ignore_alert_state=ignore_alert_state)
    if high_risk:
        seen = {(s.source, s.ticker) for s in signals}
        low = {**cluster_kwargs, "min_value": HIGH_RISK_FINDER_MIN_EUR,
               "solo_threshold": HIGH_RISK_FINDER_MIN_EUR}
        extra = []
        if on.get("sec", False):
            extra += cluster.find_sec_clusters(conn, ignore_alert_state=ignore_alert_state,
                                               **low, **sec_kwargs)
        if on.get("norway", False):
            extra += cluster.find_norway_clusters(conn, ignore_alert_state=ignore_alert_state, **low)
        for s in extra:
            if (s.source, s.ticker) not in seen:
                s.high_risk_only = True
                signals.append(s)
    return signals


_EXIT_SOURCES = ("sec", "house", "bafin", "norway", "sweden", "senate")


def exit_signals(conn, *, ignore_alert_state: bool = False, sources: dict | None = None) -> list:
    """One definition of the exit-finder list, mirroring buy_side_signals -- people
    who bought a ticker together later selling it together, for SEC, House, BaFin,
    Norway, Sweden and Senate. Follows the same `sources` dict convention as
    buy_side_signals (bot._which_sources' shape); None (the default) runs every
    source, Senate included.
    """
    on = sources if sources is not None else {k: True for k in _EXIT_SOURCES}
    signals = []
    if on.get("sec", False):
        signals += cluster.find_sec_exit_signals(conn, ignore_alert_state=ignore_alert_state)
    if on.get("house", False):
        signals += cluster.find_house_exit_signals(conn, ignore_alert_state=ignore_alert_state)
    if on.get("bafin", False):
        signals += cluster.find_bafin_exit_signals(conn, ignore_alert_state=ignore_alert_state)
    if on.get("norway", False):
        signals += cluster.find_norway_exit_signals(conn, ignore_alert_state=ignore_alert_state)
    if on.get("sweden", False):
        signals += cluster.find_sweden_exit_signals(conn, ignore_alert_state=ignore_alert_state)
    if on.get("senate", False):
        signals += cluster.find_senate_exit_signals(conn, ignore_alert_state=ignore_alert_state)
    return signals


def select(conn, signals: list, t212, today: dt.date | None = None) -> Selection:
    """`t212` is a trading212.Availability, or None when the instrument list isn't
    available -- then stocks aren't filtered and Selection.t212_checked says so.

    ExitSignals in `signals` (hasattr "seller_count") are kept in .exits as-is:
    no recency, Trading 212 or size filter -- they're already deduplicated by the
    exit finders' own alert state (should_alert_exit), the same way should_alert
    dedupes buy-side clusters.
    """
    today = today or dt.date.today()
    since = (today - dt.timedelta(days=MAX_AGE_DAYS)).isoformat()
    exits = [s for s in signals if hasattr(s, "seller_count")]
    raw_cautions = [s for s in signals if is_caution(s)
                    and (cluster.disclosed_on(conn, s) or "") >= since]
    raw_cautions = cluster.enrich_signals(conn, raw_cautions) if raw_cautions else []
    cautions = []
    for s in raw_cautions:
        s.tier = CAUTION
        cautions.append(_caution_tier(conn, s))
    pre = [s for s in signals
           if is_buy_side(s)
           and (cluster.disclosed_on(conn, s) or "") >= since
           and (t212 is None or t212.can_buy(s.ticker, s.source))]
    pre = cluster.enrich_signals(conn, pre) if pre else []
    tiered = []
    for s in pre:
        if hasattr(s, "crypto_kind"):
            t = _crypto_tier(conn, s)
        elif _in_high_risk_band(s):
            t = _high_risk_tier(s)
        elif getattr(s, "high_risk_only", False):
            t = None        # found only by the lower-threshold pass: never a main tier
        else:
            t = _stock_tier(s)
        if t is not None:
            if s.source == "NORWAY" and s.ticker.upper() in getattr(t212, "unchecked", ()):
                t.missed.append("Trading 212 не проверен: не удалось узнать ISIN")
            s.tier = t.tier
            tiered.append(t)
    # A candidate stock below CANDIDATE_MIN_SCORE isn't worth showing -- crypto
    # candidates (CryptoSignal, and CRYPTO: tickers such as congressional crypto
    # clusters) are exempt, and Сильный is never scored against this at all.
    candidates = [t for t in tiered if t.tier == CANDIDATE
                 and (crypto.is_crypto(t.signal.ticker)
                      or getattr(t.signal, "score", 0) >= CANDIDATE_MIN_SCORE)]
    return Selection(
        strong=[t for t in tiered if t.tier == STRONG],
        candidates=candidates[:MAX_CANDIDATES],
        t212_checked=t212 is not None,
        exits=exits,
        cautions=cautions,
        high_risk=[t for t in tiered if t.tier == HIGH_RISK],
    )
