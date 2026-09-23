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
CRYPTO_TREASURY_BIG_EUR = 50e6

STRONG, CANDIDATE = "strong", "candidate"
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
        missed.append("нет 3+ инсайдеров, CEO/CFO/Chair в кластере или крупной покупки CEO/CFO")
    tier = STRONG if (rules and size_known and not is_coin) else CANDIDATE
    return Tiered(sig, tier, met, missed)


def _crypto_tier(conn, sig) -> Tiered | None:
    if not sig.bullish:
        return None
    if sig.crypto_kind == "treasury" and (sig.total_value or 0) < CRYPTO_TREASURY_BIG_EUR:
        return None
    met = ["крупный приток"]
    trend = crypto.price_trend(conn, sig.coin)
    if trend is None:
        return Tiered(sig, CANDIDATE, met, ["цена не проверена"])
    desc = (f"{sig.coin} {trend['ret_7d']:+.1f}% за 7 дн., "
            f"{'выше' if trend['above_ma20'] else 'ниже'} 20-дн. средней")
    if crypto.trend_confirms(trend):
        return Tiered(sig, STRONG, met + [desc], [])
    return Tiered(sig, CANDIDATE, met, [f"цена не подтверждает: {desc}"])


_BUY_SIDE_SOURCES = ("sec", "house", "senate", "bafin", "norway", "sweden", "crypto")

# run_daily.sh's own tuning: the mandatory-filing 5%/13G default is mostly routine
# 13G ownership crossings, not news.
_STAKE_DEFAULTS = {"min_percent": 10.0, "activist_only": True,
                   "new_positions_only": True, "max_age_days": 30}


def buy_side_signals(conn, *, ignore_alert_state: bool = False, sources: dict | None = None,
                      onchain: bool = False, sec_kwargs: dict | None = None,
                      sweden_kwargs: dict | None = None, stake_kwargs: dict | None = None,
                      cluster_kwargs: dict | None = None) -> list:
    """One definition of the buy-side finder list, shared by bot.run_cluster_pass,
    menu._find_signals and calibrate_strategy._signals -- the three had drifted out
    of sync with each other (stake max_age_days=30 only in menu/calibration;
    on-chain always on in menu, opt-in via --onchain-only in bot, never run in
    calibration; Senate missing from calibration entirely).

    Runs every buy-side finder -- SEC/House/Senate/BaFin/Norway/Sweden clusters,
    13D/G stakes, crypto treasury purchases and spot-ETF inflows -- plus on-chain
    flows when `onchain` is set. Exit finders (people who bought together later
    selling together) are a different concern and stay bot.py's own job.

    `sources` follows bot._which_sources' shape: a dict of source name -> bool,
    with an optional "stakes" key that gates 13D/G independently of "sec" (bot's
    --forms flag can select SEC forms without 13D/13G). None (the default) runs
    every source, Senate included.

    `sec_kwargs` and `sweden_kwargs` layer their finder's own extra knobs
    (include_derivatives/insiders_only/include_10b5_1 for SEC;
    include_share_programs/include_derivatives for Sweden) on top of
    `cluster_kwargs` (min_value, solo_threshold), which every cluster finder
    shares. `stake_kwargs` defaults to run_daily.sh's tuning -- see
    _STAKE_DEFAULTS -- rather than find_stake_signals' bare defaults.
    """
    on = sources if sources is not None else {k: True for k in _BUY_SIDE_SOURCES}
    cluster_kwargs = cluster_kwargs or {}
    sec_kwargs = sec_kwargs or {}
    sweden_kwargs = sweden_kwargs or {}
    stake_kwargs = {**_STAKE_DEFAULTS, **(stake_kwargs or {})}

    signals = []
    if on.get("sec", True):
        signals += cluster.find_sec_clusters(conn, ignore_alert_state=ignore_alert_state,
                                              **cluster_kwargs, **sec_kwargs)
    if on.get("house", True):
        signals += cluster.find_house_clusters(conn, ignore_alert_state=ignore_alert_state,
                                                **cluster_kwargs)
    if on.get("senate", True):
        signals += cluster.find_senate_clusters(conn, ignore_alert_state=ignore_alert_state,
                                                 **cluster_kwargs)
    if on.get("bafin", True):
        signals += cluster.find_bafin_clusters(conn, ignore_alert_state=ignore_alert_state,
                                                **cluster_kwargs)
    if on.get("norway", True):
        signals += cluster.find_norway_clusters(conn, ignore_alert_state=ignore_alert_state,
                                                 **cluster_kwargs)
    if on.get("sweden", True):
        signals += cluster.find_sweden_clusters(conn, ignore_alert_state=ignore_alert_state,
                                                 **cluster_kwargs, **sweden_kwargs)
    if on.get("stakes", on.get("sec", True)):
        signals += cluster.find_stake_signals(conn, ignore_alert_state=ignore_alert_state,
                                              **stake_kwargs)
    if on.get("crypto", True):
        signals += cluster.find_treasury_signals(conn, ignore_alert_state=ignore_alert_state)
        signals += cluster.find_etf_flow_signals(conn, ignore_alert_state=ignore_alert_state)
    if onchain:
        signals += cluster.find_onchain_signals(conn, ignore_alert_state=ignore_alert_state)
    return signals


def select(conn, signals: list, t212, today: dt.date | None = None) -> Selection:
    """`t212` is a trading212.Availability, or None when the instrument list isn't
    available -- then stocks aren't filtered and Selection.t212_checked says so."""
    today = today or dt.date.today()
    since = (today - dt.timedelta(days=MAX_AGE_DAYS)).isoformat()
    pre = [s for s in signals
           if is_buy_side(s)
           and (cluster.disclosed_on(conn, s) or "") >= since
           and (t212 is None or t212.can_buy(s.ticker, s.source))]
    pre = cluster.enrich_signals(conn, pre) if pre else []
    tiered = []
    for s in pre:
        t = _crypto_tier(conn, s) if hasattr(s, "crypto_kind") else _stock_tier(s)
        if t is not None:
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
    )
