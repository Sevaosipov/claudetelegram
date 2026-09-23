"""Exit signals: a tracked buy cluster whose members have since sold."""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

import datefmt
import db
import fx

from .common import (
    EXIT_LOOKBACK_MONTHS,
    EXIT_MIN_BUYERS,
    EXIT_MIN_SELLERS,
    EXIT_SELL_FRACTION,
    FORM_144_MIN_VALUE,
    _bracket_to_eur,
    name_key,
)
from .buys import _clean_asset_name
from .alerts import should_alert_exit


@dataclass
class ExitSignal:
    source: str  # "SEC", "HOUSE", "BAFIN" or "NORWAY"
    ticker: str
    company: str
    total_buyers: int
    seller_count: int
    lines: list[str]  # already-formatted "Name: bought $X D1 -> sold $Y D2 (url)" lines
    # Plain seller names, for the same reason ClusterSignal carries member_names.
    seller_names: list[str] = field(default_factory=list)
    corroborated_by: list[str] = field(default_factory=list)
def find_sec_exit_signals(conn, lookback_months: int = EXIT_LOOKBACK_MONTHS,
                           min_buyers: int = EXIT_MIN_BUYERS, min_sellers: int = EXIT_MIN_SELLERS,
                           sell_fraction: float = EXIT_SELL_FRACTION,
                           ignore_alert_state: bool = False, include_form_144: bool = True,
                           min_144_value: float = FORM_144_MIN_VALUE) -> list[ExitSignal]:
    """`include_form_144` also counts a notice of intent to sell (Form 144) as an
    exit. That is the point of collecting those: a 144 is filed *before* the sale,
    and therefore before the Form 4 that records it, so an unwinding cluster shows
    up here weeks earlier than sales alone would reveal it. The signal line says
    which kind of evidence each person contributed, because an intention is not a
    completed sale -- filers do abandon them.
    """
    since = (dt.date.today() - dt.timedelta(days=lookback_months * 30)).isoformat()
    buy_rows = conn.execute(
        "SELECT ticker, issuer_name, owner_name, transaction_date, value, source_url FROM sec_purchases "
        "WHERE transaction_date >= ? AND ticker IS NOT NULL AND ticker != ''",
        (since,),
    ).fetchall()
    sell_rows = conn.execute(
        "SELECT ticker, owner_name, transaction_date, value, source_url FROM sec_sales "
        "WHERE transaction_date >= ? AND ticker IS NOT NULL AND ticker != ''",
        (since,),
    ).fetchall()

    # ticker -> owner_name -> (company, earliest_buy_date, buy_value, buy_url)
    buys: dict[str, dict[str, tuple]] = {}
    for ticker, issuer_name, owner_name, txn_date, value, url in buy_rows:
        d = buys.setdefault(ticker, {})
        if owner_name not in d or txn_date < d[owner_name][1]:
            d[owner_name] = (issuer_name, txn_date, value, url)

    # ticker -> name_key -> [(sell_date, sell_value, sell_url, kind), ...]
    #
    # Keyed by name_key rather than the raw name because Form 4 and Form 144 write
    # the same person's name differently ("CHEN SEAN" vs "Sean Chen"); an exact
    # match would find essentially nothing across the two forms.
    sells: dict[str, dict[str, list]] = {}
    for ticker, owner_name, txn_date, value, url in sell_rows:
        sells.setdefault(ticker, {}).setdefault(name_key(owner_name), []).append(
            (txn_date, value, url, "sale"))

    if include_form_144:
        notice_rows = conn.execute(
            """SELECT ticker, person_name, approx_sale_date, market_value, source_url
               FROM sec_proposed_sales
               WHERE approx_sale_date >= ? AND ticker IS NOT NULL AND ticker != ''
                 AND market_value >= ?""",
            (since, min_144_value),
        ).fetchall()
        for ticker, person, sale_date, value, url in notice_rows:
            sells.setdefault(ticker, {}).setdefault(name_key(person), []).append(
                (sale_date, value, url, "notice"))

    signals = []
    for ticker, buyers in buys.items():
        if len(buyers) < min_buyers:
            continue
        sellers_for_ticker = sells.get(ticker, {})
        matched = []
        for owner_name, (issuer_name, buy_date, buy_value, buy_url) in buyers.items():
            later_sells = sorted(s for s in sellers_for_ticker.get(name_key(owner_name), [])
                                 if s[0] > buy_date)
            if later_sells:
                sell_date, sell_value, sell_url, sell_kind = later_sells[0]
                matched.append((owner_name, buy_date, buy_value, buy_url,
                                 sell_date, sell_value, sell_url, sell_kind))
        if len(matched) < min_sellers or len(matched) < math.ceil(len(buyers) * sell_fraction):
            continue

        seller_names = [m[0] for m in matched]
        if not ignore_alert_state and not should_alert_exit(conn, "SEC", ticker, seller_names):
            continue

        company = next(iter(buyers.values()))[0]
        lines = []
        for owner_name, buy_date, buy_value, buy_url, sell_date, sell_value, sell_url, sell_kind in matched:
            bv = f"€{fx.to_eur(buy_value, 'USD', conn):,.0f}" if buy_value else "?"
            sv = f"€{fx.to_eur(sell_value, 'USD', conn):,.0f}" if sell_value else "?"
            verb = "заявил о продаже (Form 144)" if sell_kind == "notice" else "продал"
            label = "уведомление" if sell_kind == "notice" else "продажа"
            lines.append(f"{owner_name}: купил {bv} {datefmt.fmt(buy_date)} -> {verb} {sv} "
                         f"{datefmt.fmt(sell_date)}\n     покупка: {buy_url}\n     {label}: {sell_url}")

        signals.append(ExitSignal(
            source="SEC", ticker=ticker, company=company, total_buyers=len(buyers),
            seller_count=len(matched), lines=lines, seller_names=seller_names,
        ))
    return signals
def find_house_exit_signals(conn, lookback_months: int = EXIT_LOOKBACK_MONTHS,
                             min_buyers: int = EXIT_MIN_BUYERS, min_sellers: int = EXIT_MIN_SELLERS,
                             sell_fraction: float = EXIT_SELL_FRACTION,
                             ignore_alert_state: bool = False, table: str = "house_purchases",
                             source: str = "HOUSE", date_format: str | None = "%m/%d/%Y") -> list[ExitSignal]:
    """Also serves the Senate, via find_senate_exit_signals -- see find_house_clusters
    for why the two chambers share this code."""
    cutoff = dt.date.today() - dt.timedelta(days=lookback_months * 30)
    rows = conn.execute(
        f"SELECT ticker, asset, member_name, txn_type, txn_date, amount_range, source_url FROM {table} "
        "WHERE ticker IS NOT NULL AND ticker != ''"
    ).fetchall()

    def parse_date(s):
        try:
            return (dt.datetime.strptime(s, date_format).date() if date_format
                    else dt.date.fromisoformat(s))
        except ValueError:
            return None

    # ticker -> member_name -> (asset, earliest_buy_date, buy_amount, buy_url)
    buys: dict[str, dict[str, tuple]] = {}
    # ticker -> member_name -> [(sell_date, sell_amount, sell_url), ...]
    sells: dict[str, dict[str, list]] = {}

    for ticker, asset, member_name, txn_type, txn_date, amount_range, url in rows:
        d = parse_date(txn_date)
        if d is None or d < cutoff:
            continue
        if txn_type == "P":
            slot = buys.setdefault(ticker, {})
            if member_name not in slot or d < slot[member_name][1]:
                slot[member_name] = (asset, d, amount_range, url)
        elif txn_type in ("S", "S (partial)"):
            sells.setdefault(ticker, {}).setdefault(member_name, []).append((d, amount_range, url))

    signals = []
    for ticker, buyers in buys.items():
        if len(buyers) < min_buyers:
            continue
        sellers_for_ticker = sells.get(ticker, {})
        matched = []
        for member_name, (asset, buy_date, buy_amount, buy_url) in buyers.items():
            later_sells = sorted(s for s in sellers_for_ticker.get(member_name, []) if s[0] > buy_date)
            if later_sells:
                sell_date, sell_amount, sell_url = later_sells[0]
                matched.append((member_name, buy_date, buy_amount, buy_url, sell_date, sell_amount, sell_url))
        if len(matched) < min_sellers or len(matched) < math.ceil(len(buyers) * sell_fraction):
            continue

        seller_names = [m[0] for m in matched]
        if not ignore_alert_state and not should_alert_exit(conn, source, ticker, seller_names):
            continue

        company = _clean_asset_name(next(iter(buyers.values()))[0])
        lines = []
        for member_name, buy_date, buy_amount, buy_url, sell_date, sell_amount, sell_url in matched:
            lines.append(
                f"{member_name}: купил {_bracket_to_eur(buy_amount, conn)} {datefmt.fmt(buy_date)} -> "
                f"продал {_bracket_to_eur(sell_amount, conn)} {datefmt.fmt(sell_date)}"
                f"\n     покупка: {buy_url}\n     продажа: {sell_url}"
            )

        signals.append(ExitSignal(
            source=source, ticker=ticker, company=company, total_buyers=len(buyers),
            seller_count=len(matched), lines=lines, seller_names=seller_names,
        ))
    return signals
def find_senate_exit_signals(conn, **kwargs) -> list[ExitSignal]:
    """Senate PTR exit signals. Same logic as the House -- see find_house_exit_signals."""
    return find_house_exit_signals(conn, table="senate_purchases", source="SENATE",
                                    date_format=None, **kwargs)
def find_bafin_exit_signals(conn, lookback_months: int = EXIT_LOOKBACK_MONTHS,
                             min_buyers: int = EXIT_MIN_BUYERS, min_sellers: int = EXIT_MIN_SELLERS,
                             sell_fraction: float = EXIT_SELL_FRACTION,
                             ignore_alert_state: bool = False) -> list[ExitSignal]:
    cutoff = dt.date.today() - dt.timedelta(days=lookback_months * 30)
    rows = conn.execute(
        "SELECT isin, issuer_name, notifier_name, txn_type, txn_date, volume_eur, source_url FROM bafin_purchases "
        "WHERE isin IS NOT NULL AND isin != ''"
    ).fetchall()

    def parse_date(s):
        try:
            return dt.datetime.strptime(s, "%d.%m.%Y").date()
        except ValueError:
            return None

    # isin -> notifier_name -> (issuer_name, earliest_buy_date, buy_value, buy_url)
    buys: dict[str, dict[str, tuple]] = {}
    # isin -> notifier_name -> [(sell_date, sell_value, sell_url), ...]
    sells: dict[str, dict[str, list]] = {}

    for isin, issuer_name, notifier_name, txn_type, txn_date, volume, url in rows:
        d = parse_date(txn_date)
        if d is None or d < cutoff:
            continue
        if txn_type == "P":
            slot = buys.setdefault(isin, {})
            if notifier_name not in slot or d < slot[notifier_name][1]:
                slot[notifier_name] = (issuer_name, d, volume, url)
        elif txn_type == "S":
            sells.setdefault(isin, {}).setdefault(notifier_name, []).append((d, volume, url))

    signals = []
    for isin, buyers in buys.items():
        if len(buyers) < min_buyers:
            continue
        sellers_for_isin = sells.get(isin, {})
        matched = []
        for notifier_name, (issuer_name, buy_date, buy_value, buy_url) in buyers.items():
            later_sells = sorted(s for s in sellers_for_isin.get(notifier_name, []) if s[0] > buy_date)
            if later_sells:
                sell_date, sell_value, sell_url = later_sells[0]
                matched.append((notifier_name, buy_date, buy_value, buy_url, sell_date, sell_value, sell_url))
        if len(matched) < min_sellers or len(matched) < math.ceil(len(buyers) * sell_fraction):
            continue

        seller_names = [m[0] for m in matched]
        if not ignore_alert_state and not should_alert_exit(conn, "BAFIN", isin, seller_names):
            continue

        company = next(iter(buyers.values()))[0]
        lines = []
        for notifier_name, buy_date, buy_value, buy_url, sell_date, sell_value, sell_url in matched:
            bv = f"€{buy_value:,.0f}" if buy_value else "?"
            sv = f"€{sell_value:,.0f}" if sell_value else "?"
            lines.append(
                f"{notifier_name}: купил {bv} {datefmt.fmt(buy_date)} -> продал {sv} {datefmt.fmt(sell_date)}"
                f"\n     покупка: {buy_url}\n     продажа: {sell_url}"
            )

        signals.append(ExitSignal(
            source="BAFIN", ticker=isin, company=company, total_buyers=len(buyers),
            seller_count=len(matched), lines=lines, seller_names=seller_names,
        ))
    return signals
def find_norway_exit_signals(conn, lookback_months: int = EXIT_LOOKBACK_MONTHS,
                              min_buyers: int = EXIT_MIN_BUYERS, min_sellers: int = EXIT_MIN_SELLERS,
                              sell_fraction: float = EXIT_SELL_FRACTION,
                              ignore_alert_state: bool = False) -> list[ExitSignal]:
    # txn_date is already ISO here (unlike House's M/D/Y or BaFin's D.M.Y), so no
    # parse-to-date step is needed before comparing/sorting -- same as SEC's.
    since = (dt.date.today() - dt.timedelta(days=lookback_months * 30)).isoformat()
    rows = conn.execute(
        """SELECT ticker, issuer_name, person, txn_type, txn_date, value, currency, source_url
           FROM norway_purchases
           WHERE txn_date >= ? AND ticker IS NOT NULL AND ticker != ''""",
        (since,),
    ).fetchall()

    # ticker -> person -> (issuer_name, earliest_buy_date, buy_value, buy_currency, buy_url)
    buys: dict[str, dict[str, tuple]] = {}
    # ticker -> person -> [(sell_date, sell_value, sell_currency, sell_url), ...]
    sells: dict[str, dict[str, list]] = {}

    for ticker, issuer_name, person, txn_type, txn_date, value, currency, url in rows:
        if txn_type == "P":
            slot = buys.setdefault(ticker, {})
            if person not in slot or txn_date < slot[person][1]:
                slot[person] = (issuer_name, txn_date, value, currency, url)
        elif txn_type == "S":
            sells.setdefault(ticker, {}).setdefault(person, []).append((txn_date, value, currency, url))

    signals = []
    for ticker, buyers in buys.items():
        if len(buyers) < min_buyers:
            continue
        sellers_for_ticker = sells.get(ticker, {})
        matched = []
        for person, (issuer_name, buy_date, buy_value, buy_currency, buy_url) in buyers.items():
            later_sells = sorted(s for s in sellers_for_ticker.get(person, []) if s[0] > buy_date)
            if later_sells:
                sell_date, sell_value, sell_currency, sell_url = later_sells[0]
                matched.append((person, buy_date, buy_value, buy_currency, buy_url,
                                 sell_date, sell_value, sell_currency, sell_url))
        if len(matched) < min_sellers or len(matched) < math.ceil(len(buyers) * sell_fraction):
            continue

        seller_names = [m[0] for m in matched]
        if not ignore_alert_state and not should_alert_exit(conn, "NORWAY", ticker, seller_names):
            continue

        company = next(iter(buyers.values()))[0]
        lines = []
        for person, buy_date, buy_value, buy_currency, buy_url, sell_date, sell_value, sell_currency, sell_url in matched:
            bv = f"€{fx.to_eur(buy_value, buy_currency, conn):,.0f}" if buy_value else "?"
            sv = f"€{fx.to_eur(sell_value, sell_currency, conn):,.0f}" if sell_value else "?"
            lines.append(f"{person}: купил {bv} {datefmt.fmt(buy_date)} -> продал {sv} {datefmt.fmt(sell_date)}"
                         f"\n     покупка: {buy_url}\n     продажа: {sell_url}")

        signals.append(ExitSignal(
            source="NORWAY", ticker=ticker, company=company, total_buyers=len(buyers),
            seller_count=len(matched), lines=lines, seller_names=seller_names,
        ))
    return signals
def find_sweden_exit_signals(conn, lookback_months: int = EXIT_LOOKBACK_MONTHS,
                              min_buyers: int = EXIT_MIN_BUYERS, min_sellers: int = EXIT_MIN_SELLERS,
                              sell_fraction: float = EXIT_SELL_FRACTION,
                              ignore_alert_state: bool = False) -> list[ExitSignal]:
    """txn_date is already ISO here, so no parse-to-date step is needed before
    comparing or sorting -- same as SEC's and Norway's."""
    since = (dt.date.today() - dt.timedelta(days=lookback_months * 30)).isoformat()
    rows = conn.execute(
        """SELECT isin, issuer_name, person, txn_type, txn_date, value, currency, source_url
           FROM sweden_purchases
           WHERE txn_date >= ? AND isin IS NOT NULL AND isin != '' AND status = 'Aktuell'""",
        (since,),
    ).fetchall()

    # isin -> person -> (issuer_name, earliest_buy_date, buy_value, buy_currency, buy_url)
    buys: dict[str, dict[str, tuple]] = {}
    # isin -> person -> [(sell_date, sell_value, sell_currency, sell_url), ...]
    sells: dict[str, dict[str, list]] = {}

    for isin, issuer_name, person, txn_type, txn_date, value, currency, url in rows:
        if txn_type == "P":
            slot = buys.setdefault(isin, {})
            if person not in slot or txn_date < slot[person][1]:
                slot[person] = (issuer_name, txn_date, value, currency, url)
        elif txn_type == "S":
            sells.setdefault(isin, {}).setdefault(person, []).append((txn_date, value, currency, url))

    signals = []
    for isin, buyers in buys.items():
        if len(buyers) < min_buyers:
            continue
        sellers_for_isin = sells.get(isin, {})
        matched = []
        for person, (issuer_name, buy_date, buy_value, buy_currency, buy_url) in buyers.items():
            later_sells = sorted(s for s in sellers_for_isin.get(person, []) if s[0] > buy_date)
            if later_sells:
                sell_date, sell_value, sell_currency, sell_url = later_sells[0]
                matched.append((person, buy_date, buy_value, buy_currency, buy_url,
                                 sell_date, sell_value, sell_currency, sell_url))
        if len(matched) < min_sellers or len(matched) < math.ceil(len(buyers) * sell_fraction):
            continue

        seller_names = [m[0] for m in matched]
        if not ignore_alert_state and not should_alert_exit(conn, "SWEDEN", isin, seller_names):
            continue

        company = next(iter(buyers.values()))[0]
        lines = []
        for person, buy_date, buy_value, buy_currency, buy_url, sell_date, sell_value, sell_currency, sell_url in matched:
            bv = f"€{fx.to_eur(buy_value, buy_currency, conn):,.0f}" if buy_value else "?"
            sv = f"€{fx.to_eur(sell_value, sell_currency, conn):,.0f}" if sell_value else "?"
            lines.append(f"{person}: купил {bv} {datefmt.fmt(buy_date)} -> продал {sv} {datefmt.fmt(sell_date)}"
                         f"\n     покупка: {buy_url}\n     продажа: {sell_url}")

        signals.append(ExitSignal(
            source="SWEDEN", ticker=isin, company=company, total_buyers=len(buyers),
            seller_count=len(matched), lines=lines, seller_names=seller_names,
        ))
    return signals
def commit_exit_alert(conn, signal: ExitSignal) -> None:
    db.save_cluster_alert_state(conn, f"{signal.source}_EXIT", signal.ticker, signal.seller_count,
                                 signal.seller_names)
