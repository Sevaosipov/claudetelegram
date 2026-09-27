"""Crypto signals: corporate treasury trades, spot-ETF flows, exchange-wallet flows.

Politicians' crypto purchases need nothing here -- they are House/Senate PTR rows with
a CRYPTO:<SYM> ticker and go through find_house_clusters like any stock. What this
module adds are the three sources that aren't people buying a security: a company
putting coins on its balance sheet, money moving into or out of the spot ETFs, and
coins moving into or out of exchange custody.

All three are keyed by the coin's CRYPTO:<SYM> ticker, so cluster.find_corroboration
links them to each other and to congressional buys of the same coin for free.

Like the other finders these are pure SQL plus fx: coin values that need a live price
(an on-chain move is measured in coins, not money) are filled in by enrich_signals,
the one network step.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

import crypto
import crypto_etf
import db
import fx

# A company adding under this much to its balance sheet is a rounding error in the
# market for the coin, however large it is for the company.
TREASURY_MIN_VALUE_EUR = 500_000
TREASURY_WINDOW_DAYS = 14
# Spot-ETF net flow, in USD, summed across a coin's funds. "Unusual" is judged against
# the coin's own recent days -- the top 10% of the previous ETF_HISTORY_DAYS, with a
# floor -- because the market grows and a fixed dollar bar goes stale. The fixed
# bars apply only while there are fewer than ETF_MIN_HISTORY_DAYS stored days.
ETF_DAY_FLOW_USD = 400e6
ETF_STREAK_DAYS = 3
ETF_STREAK_MIN_USD = 500e6
ETF_MAX_AGE_DAYS = 7       # don't resurface a flow from a day this old
ETF_HISTORY_DAYS = 126     # about six months of trading days
ETF_MIN_HISTORY_DAYS = 30
ETF_DAY_FLOOR_USD = 100e6
ETF_STREAK_FLOOR_USD = 250e6
TOP_DECILE = 0.9
# Farside has trading-day rows only, so its staleness is counted in business days.
FARSIDE_STALE_BUSINESS_DAYS = 3
# Net change across the tracked exchange cold wallets, in coins, over ONCHAIN_HOURS.
ONCHAIN_MIN_COINS = {"BTC": 2_000, "ETH": 40_000}
ONCHAIN_HOURS = 24
# How far a "24h ago" snapshot may miss its mark. Runs are daily-ish, not clockwork.
ONCHAIN_SLACK_HOURS = 8


@dataclass
class CryptoSignal:
    source: str                  # CRYPTO_TREASURY / CRYPTO_ETF / CRYPTO_ONCHAIN
    crypto_kind: str             # "treasury" / "etf_flow" / "exchange_flow"
    ticker: str                  # CRYPTO:BTC
    company: str                 # who or what moved: "Strategy Inc (MSTR)", "IBIT", ...
    bullish: bool                # buying / inflow / coins leaving exchanges
    units: float | None          # coins, when known
    total_value: float | None    # EUR; filled in by enrich_signals when only units are known
    window_start: str
    window_end: str
    details: list[str]
    url: str | None
    # Every key this signal answers to; it fires if any is new, and commit records
    # all of them -- so a streak alerts once, not on every day it continues.
    alert_keys: list[str]
    member_names: list[str] = field(default_factory=list)
    corroborated_by: list[str] = field(default_factory=list)

    @property
    def coin(self) -> str:
        return crypto.symbol_of(self.ticker)


def _is_new(conn, source: str, keys: list[str]) -> bool:
    return any(db.get_alert_state(conn, source, k) is None for k in keys)


def find_treasury_signals(conn, min_value_eur: float = TREASURY_MIN_VALUE_EUR,
                          window_days: int = TREASURY_WINDOW_DAYS,
                          ignore_alert_state: bool = False) -> list[CryptoSignal]:
    """One signal per filing and coin: a company buying (or selling) coins for its
    own balance sheet. A trade whose value couldn't be parsed is kept -- unknown is
    not small -- and valued at spot by enrich_signals."""
    since = (dt.date.today() - dt.timedelta(days=window_days)).isoformat()
    rows = conn.execute(
        """SELECT accession, company, ticker, coin, side, units, avg_price_usd, total_usd,
                  filed_date, source_url
           FROM crypto_treasury_txns WHERE filed_date >= ? ORDER BY filed_date""",
        (since,),
    ).fetchall()
    grouped: dict[tuple, dict] = {}
    for acc, company, co_ticker, coin, side, units, avg, total, filed, url in rows:
        g = grouped.setdefault((acc, coin, side), {
            "company": company, "co_ticker": co_ticker, "units": 0.0, "usd": 0.0,
            "usd_known": True, "avgs": [], "filed": filed, "url": url,
        })
        g["units"] += units
        value = total or (units * avg if avg else None)
        if value is None:
            g["usd_known"] = False
        else:
            g["usd"] += value
        if avg:
            g["avgs"].append(avg)

    signals = []
    for (acc, coin, side), g in grouped.items():
        value_eur = fx.to_eur(g["usd"], "USD", conn) if g["usd_known"] else None
        if value_eur is not None and value_eur < min_value_eur:
            continue
        keys = [f"{acc}|{coin}|{side}"]
        if not ignore_alert_state and not _is_new(conn, "CRYPTO_TREASURY", keys):
            continue
        who = f"{g['company']} ({g['co_ticker']})" if g["co_ticker"] else g["company"]
        details = []
        if g["avgs"]:
            details.append(f"средняя цена ${sum(g['avgs']) / len(g['avgs']):,.0f} за {coin}")
        signals.append(CryptoSignal(
            source="CRYPTO_TREASURY", crypto_kind="treasury", ticker=crypto.ticker(coin),
            company=who, bullish=(side == "P"), units=g["units"], total_value=value_eur,
            window_start=g["filed"], window_end=g["filed"], details=details, url=g["url"],
            alert_keys=keys, member_names=[g["company"]],
        ))
    return signals


def _p90(values: list[float]) -> float:
    """Nearest-rank 90th percentile of a non-empty list."""
    s = sorted(values)
    return s[max(0, math.ceil(TOP_DECILE * len(s)) - 1)]


def _share_below(value: float, values: list[float]) -> float:
    return sum(1 for v in values if v < value) / len(values)


def _business_days_since(day: str, today: dt.date) -> int:
    d, n = dt.date.fromisoformat(day), 0
    while d < today:
        n += d.weekday() < 5
        d += dt.timedelta(days=1)
    return n


def _issuer_etf_days(conn) -> dict[str, list[tuple[str, float, list[str]]]]:
    """coin -> [(as_of, net_flow_usd, funds)] oldest first, from the issuer share-count
    snapshots (IBIT/ETHA only)."""
    by_fund: dict[str, list] = {}
    coin_of: dict[str, str] = {}
    for fund, coin, as_of, shares, nav in conn.execute(
            "SELECT fund, coin, as_of, shares_outstanding, nav_usd FROM crypto_etf_snapshots"):
        by_fund.setdefault(fund, []).append((as_of, shares, nav))
        coin_of[fund] = coin
    per_day: dict[str, dict[str, list]] = {}
    for fund, snaps in by_fund.items():
        for _prev, as_of, flow in crypto_etf.flows(snaps):
            slot = per_day.setdefault(coin_of[fund], {}).setdefault(as_of, [0.0, []])
            slot[0] += flow
            slot[1].append(fund)
    return {coin: [(d, v[0], v[1]) for d, v in sorted(days.items())]
            for coin, days in per_day.items()}


def _farside_etf_days(conn) -> dict[str, list[tuple[str, float, list[str]]]]:
    """coin -> [(date, net_flow_usd, funds)] oldest first, summed across every fund."""
    per_day: dict[str, dict[str, list]] = {}
    for coin, day, fund, flow in conn.execute(
            "SELECT coin, date, fund, flow_usd FROM crypto_etf_flows"):
        slot = per_day.setdefault(coin, {}).setdefault(day, [0.0, []])
        slot[0] += flow
        slot[1].append(fund)
    return {coin: [(d, v[0], sorted(v[1])) for d, v in sorted(days.items())]
            for coin, days in per_day.items()}


def etf_flow_days(conn, today: dt.date | None = None) -> dict[str, tuple[str, list]]:
    """coin -> (source, [(day, net_flow_usd, funds)] oldest first). Farside -- every US
    spot fund -- while its newest day is at most FARSIDE_STALE_BUSINESS_DAYS business
    days old; otherwise the issuer snapshots, which cover IBIT and ETHA only."""
    today = today or dt.date.today()
    farside, issuer = _farside_etf_days(conn), _issuer_etf_days(conn)
    out = {}
    for coin in sorted(set(farside) | set(issuer)):
        days = farside.get(coin)
        if days and _business_days_since(days[-1][0], today) <= FARSIDE_STALE_BUSINESS_DAYS:
            out[coin] = ("farside", days)
        elif issuer.get(coin):
            out[coin] = ("issuer", issuer[coin])
        elif days:
            out[coin] = ("farside", days)
    return out


def _daily_etf_flows(conn) -> dict[str, list[tuple[str, float, list[str]]]]:
    """coin -> [(day, net_flow_usd, funds)] oldest first, whichever source is current."""
    return {coin: days for coin, (_src, days) in etf_flow_days(conn).items()}


daily_etf_flows = _daily_etf_flows   # public name for the crypto dossier (crypto_research.py)


def find_etf_flow_signals(conn, day_flow_usd: float = ETF_DAY_FLOW_USD,
                          streak_days: int = ETF_STREAK_DAYS,
                          streak_min_usd: float = ETF_STREAK_MIN_USD,
                          ignore_alert_state: bool = False,
                          today: dt.date | None = None) -> list[CryptoSignal]:
    """An unusual day, or an unusual run of same-direction days, judged on the most
    recent day only so an old flow never resurfaces. Unusual = the top 10% of the
    previous ETF_HISTORY_DAYS (a day against days, a streak against 3-day totals),
    and at least the floor; with under ETF_MIN_HISTORY_DAYS of history the fixed
    day_flow_usd / streak_min_usd apply instead. An inflow is a buy signal, an
    outflow a caution signal (strategy.select)."""
    today = today or dt.date.today()
    signals = []
    for coin, (source, days) in etf_flow_days(conn, today).items():
        if not days:
            continue
        last_date, last_flow, funds = days[-1]
        if (today - dt.date.fromisoformat(last_date)).days > ETF_MAX_AGE_DAYS or last_flow == 0:
            continue
        inflow = last_flow > 0
        streak = []
        for d in reversed(days):
            if d[1] == 0 or (d[1] > 0) != inflow:
                break
            streak.append(d)
        streak.reverse()
        streak_total = sum(d[1] for d in streak)

        history = days[:-1][-ETF_HISTORY_DAYS:]
        relative = len(history) >= ETF_MIN_HISTORY_DAYS
        day_sizes = [abs(d[1]) for d in history]
        three_day = [abs(history[i][1] + history[i - 1][1] + history[i - 2][1])
                     for i in range(2, len(history))]
        if relative:
            day_bar = max(_p90(day_sizes), ETF_DAY_FLOOR_USD)
            streak_bar = max(_p90(three_day), ETF_STREAK_FLOOR_USD)
        else:
            day_bar, streak_bar = day_flow_usd, streak_min_usd

        keys, details = [], []
        if abs(last_flow) >= day_bar:
            keys.append(f"{coin}|day|{last_date}")
        if len(streak) >= streak_days and abs(streak_total) >= streak_bar:
            keys.append(f"{coin}|streak|{'in' if inflow else 'out'}|{streak[0][0]}")
            details.append(f"{len(streak)} дн. подряд {'притока' if inflow else 'оттока'}, "
                           f"всего ${abs(streak_total) / 1e6:,.0f} млн")
        if not keys:
            continue
        if not ignore_alert_state and not _is_new(conn, "CRYPTO_ETF", keys):
            continue
        day_line = f"за {last_date}: {'+' if inflow else '−'}${abs(last_flow) / 1e6:,.0f} млн"
        if relative:
            day_line += (f" — больше, чем в {_share_below(abs(last_flow), day_sizes) * 100:.0f}% "
                         f"из {len(history)} дней")
        details.insert(0, day_line)
        if not relative:
            details.append("порог по умолчанию: мало истории")
        if source == "issuer":
            details.append("только IBIT/ETHA (Farside недоступен)")
            company = "спот-ETF: " + ", ".join(sorted(funds))
            url = crypto_etf.FUNDS[sorted(funds)[0]][1]
        else:
            company = f"спот-ETF США, фондов: {len(funds)}"
            url = crypto_etf.FARSIDE_URLS.get(coin)
        headline_total = streak_total if len(streak) >= streak_days else last_flow
        signals.append(CryptoSignal(
            source="CRYPTO_ETF", crypto_kind="etf_flow", ticker=crypto.ticker(coin),
            company=company, bullish=inflow, units=None,
            total_value=fx.to_eur(abs(headline_total), "USD", conn),
            window_start=streak[0][0] if streak else last_date, window_end=last_date,
            details=details, url=url, alert_keys=keys, member_names=sorted(funds),
        ))
    return signals


def find_onchain_signals(conn, min_coins: dict[str, float] | None = None,
                         hours: int = ONCHAIN_HOURS,
                         ignore_alert_state: bool = False) -> list[CryptoSignal]:
    """Net change in the tracked exchange cold wallets' balance over ~`hours`,
    comparing each wallet's newest snapshot with its own snapshot closest to
    `hours` earlier. A wallet without such a pair is left out rather than guessed."""
    min_coins = min_coins or ONCHAIN_MIN_COINS
    rows = conn.execute(
        "SELECT coin, address, label, balance, taken_at FROM crypto_wallet_snapshots "
        "ORDER BY taken_at"
    ).fetchall()
    by_wallet: dict[str, list] = {}
    for coin, address, label, balance, taken_at in rows:
        by_wallet.setdefault(address, []).append((dt.datetime.fromisoformat(taken_at),
                                                  balance, coin, label))
    per_coin: dict[str, dict] = {}
    for address, snaps in by_wallet.items():
        latest_t, latest_bal, coin, label = snaps[-1]
        target = latest_t - dt.timedelta(hours=hours)
        earlier = [s for s in snaps[:-1]
                   if abs((s[0] - target).total_seconds()) <= ONCHAIN_SLACK_HOURS * 3600]
        if not earlier:
            continue
        base = min(earlier, key=lambda s: abs((s[0] - target).total_seconds()))
        agg = per_coin.setdefault(coin, {"delta": 0.0, "wallets": [], "start": base[0],
                                         "end": latest_t})
        agg["delta"] += latest_bal - base[1]
        agg["wallets"].append(f"{label}: {latest_bal - base[1]:+,.0f} {coin}")
        agg["start"] = min(agg["start"], base[0])
        agg["end"] = max(agg["end"], latest_t)

    signals = []
    for coin, agg in per_coin.items():
        if abs(agg["delta"]) < min_coins.get(coin, float("inf")):
            continue
        day = agg["end"].date().isoformat()
        keys = [f"{coin}|{day}"]
        if not ignore_alert_state and not _is_new(conn, "CRYPTO_ONCHAIN", keys):
            continue
        outflow = agg["delta"] < 0
        signals.append(CryptoSignal(
            source="CRYPTO_ONCHAIN", crypto_kind="exchange_flow", ticker=crypto.ticker(coin),
            company="кошельки бирж", bullish=outflow, units=abs(agg["delta"]), total_value=None,
            window_start=agg["start"].isoformat(timespec="minutes"),
            window_end=agg["end"].isoformat(timespec="minutes"),
            details=agg["wallets"], url=None, alert_keys=keys, member_names=[],
        ))
    return signals


def commit_crypto_alert(conn, signal: CryptoSignal) -> None:
    for key in signal.alert_keys:
        db.save_cluster_alert_state(conn, signal.source, key, 1, signal.member_names,
                                     signal.total_value)
