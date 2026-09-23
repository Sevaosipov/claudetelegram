"""Cluster-buying signal detection.

The raw purchase feed (universe-filtered SEC + House purchases) is stored in full
in SQLite/CSV for browsing from the terminal. Telegram, however, only gets
*signals*: a ticker where multiple distinct people (insiders, or separately,
members of Congress) bought within a rolling window -- the idea being that several
unrelated buyers converging on the same stock is a stronger, rarer, more legible
signal than any single purchase.

A signal only re-fires when the cluster *grows* past its previously-alerted size
(tracked in db.cluster_alert_state), so the same cluster isn't re-sent every day.
"""

from __future__ import annotations

# Split into submodules by concern; every name, private ones included, is
# re-exported here so `import cluster` and `cluster.<name>` keep working unchanged.
from .common import (  # noqa: F401
    ALERT_VALUE_GROWTH,
    BAFIN_MIN_BUYERS,
    BAFIN_SOLO_THRESHOLD,
    BAFIN_WINDOW_DAYS,
    CAP_BUYERS,
    CAP_CORROBORATION,
    CAP_CRYPTO_SIZE,
    CAP_MARKET_CAP,
    CAP_POSITION,
    CORROBORATION_WINDOW_DAYS,
    ClusterSignal,
    EXIT_LOOKBACK_MONTHS,
    EXIT_MIN_BUYERS,
    EXIT_MIN_SELLERS,
    EXIT_SELL_FRACTION,
    FIRST_BUY_MIN_HISTORY_DAYS,
    FORM_144_MIN_VALUE,
    FRESH_DECAY_DAYS,
    HOUSE_CLUSTER_SPAN_DAYS,
    HOUSE_MIN_BUYERS,
    HOUSE_SOLO_THRESHOLD,
    HOUSE_WINDOW_DAYS,
    ILLIQUID_BELOW_EUR,
    MAX_CREDIBLE_PCT_OF_MCAP,
    MIN_CLUSTER_VALUE,
    NORWAY_MIN_BUYERS,
    NORWAY_SOLO_THRESHOLD,
    NORWAY_WINDOW_DAYS,
    P_ILLIQUID,
    P_UNKNOWN_SIZE,
    SEC_MIN_BUYERS,
    SEC_SOLO_THRESHOLD,
    SEC_WINDOW_DAYS,
    SENATE_CLUSTER_SPAN_DAYS,
    SENATE_MIN_BUYERS,
    SENATE_WINDOW_DAYS,
    STAKE_MIN_INCREASE_PP,
    STAKE_MIN_PERCENT,
    SWEDEN_MIN_BUYERS,
    SWEDEN_SOLO_THRESHOLD,
    SWEDEN_WINDOW_DAYS,
    W_CRYPTO_BASE,
    W_CRYPTO_SIZE,
    W_FIRST_BUY,
    W_FRESH,
    W_FULL_UNWIND,
    W_HAS_OFFICER,
    W_PCT_OF_MARKET_CAP,
    W_PER_CORROBORATING_SOURCE,
    W_PER_EXTRA_BUYER,
    W_POSITION_INCREASE,
    _AMOUNT_RE,
    _ASSET_SUFFIX_RE,
    _JUNK_TICKER_SQL,
    _NAME_PUNCT_RE,
    _NAME_SUFFIXES,
    _REGIME,
    _bracket_to_eur,
    _regime,
    name_key,
    parse_amount_low,
)
from .buys import (  # noqa: F401
    _clean_asset_name,
    _is_first_buy,
    _lag_days,
    _median,
    _position_increase,
    _sec_role,
    _tight_purchase_window,
    find_bafin_clusters,
    find_house_clusters,
    find_norway_clusters,
    find_sec_clusters,
    find_senate_clusters,
    find_sweden_clusters,
)
from .stakes import (  # noqa: F401
    StakeSignal,
    commit_stake_alert,
    find_stake_signals,
)
from .exits import (  # noqa: F401
    ExitSignal,
    commit_exit_alert,
    find_bafin_exit_signals,
    find_house_exit_signals,
    find_norway_exit_signals,
    find_sec_exit_signals,
    find_senate_exit_signals,
    find_sweden_exit_signals,
)
from .alerts import (  # noqa: F401
    commit_alert,
    should_alert,
    should_alert_exit,
)
from .scoring import (  # noqa: F401
    enrich_signals,
    find_corroboration,
    score_signal,
)
from .crypto import (  # noqa: F401
    CryptoSignal,
    commit_crypto_alert,
    find_etf_flow_signals,
    find_onchain_signals,
    find_treasury_signals,
)
from .recency import disclosed_on  # noqa: F401
from .roles import (  # noqa: F401
    INSIDER_ROLES,
    TOP_EXEC_ROLES,
    Buyer,
    bafin_role,
    sec_role,
    sweden_role,
)
