"""The finder list, and what kind of signal each finder makes.

buy_side_signals and exit_signals are the one definition of which finders run: SEC, House,
Senate, BaFin, Norway and Sweden clusters, 13D/G stakes, crypto treasury and ETF flows, and
the groups that bought together and later sell together. Their callers are bot.collect_new_signals
(the daily run) and model.candidate_signals (the model portfolio).

A finder only detects. Nothing here decides what is worth acting on: scoring lives in
model_score.py (pure functions) and model.py (the portfolio) -- spec
docs/superpowers/specs/2026-09-30-model-portfolio-and-analyst-design.md. The tiers, size floors,
recency window and Trading 212 filter that once sat here are gone.
"""
from __future__ import annotations

import cluster

# The journal's tier for a bearish crypto signal (an ETF outflow, a company selling, coins
# moving onto exchanges): shown by the menu, never traded, and read by
# positions._crypto_caution.
CAUTION = "caution"


def is_buy_side(sig) -> bool:
    if hasattr(sig, "seller_count"):          # ExitSignal
        return False
    if hasattr(sig, "crypto_kind"):           # CryptoSignal
        return bool(sig.bullish)
    return True


def is_caution(sig) -> bool:
    return hasattr(sig, "crypto_kind") and not sig.bullish


_BUY_SIDE_SOURCES = ("sec", "house", "senate", "bafin", "norway", "sweden", "crypto")

# run_daily.sh's own tuning: the mandatory-filing 5%/13G default is mostly routine
# 13G ownership crossings, not news.
_STAKE_DEFAULTS = {"min_percent": 10.0, "activist_only": True,
                   "new_positions_only": True, "max_age_days": 30}


def buy_side_signals(conn, *, ignore_alert_state: bool = False, sources: dict | None = None,
                      onchain: bool = False, sec_kwargs: dict | None = None,
                      sweden_kwargs: dict | None = None, stake_kwargs: dict | None = None,
                      cluster_kwargs: dict | None = None) -> list:
    """One definition of the buy-side finder list, shared by its two callers:
    bot.collect_new_signals (the daily run) and model.candidate_signals (the model
    portfolio) -- separate copies had drifted out of sync (stake max_age_days, on-chain
    on or off, Senate missing).

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
