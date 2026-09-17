"""Per-ticker dossier.

The parts that read the database are covered here; the parts that reach the network
(price history, news, SEC filing lists) are not, on purpose -- a test suite that
needs Yahoo Finance to be up is a test suite that fails for reasons unrelated to the
code. The ISIN path happens to be entirely offline, so it is tested end to end.
"""
from __future__ import annotations

import pytest

import db
import opinion
import research
from conftest import (add_bafin_txn, add_sec_purchase, add_sec_sale, add_stake,
                      add_sweden_txn)


def test_insider_activity_returns_buys_and_sells(conn):
    add_sec_purchase(conn, "AAA", "Buyer", 500_000, "2026-09-01")
    add_sec_sale(conn, "AAA", "Seller", 300_000, "2026-09-02")
    activity = research.insider_activity(conn, "AAA")
    assert len(activity["buys"]) == 1 and len(activity["sells"]) == 1


def test_insider_activity_is_scoped_to_the_ticker(conn):
    add_sec_purchase(conn, "AAA", "Buyer", 500_000)
    add_sec_purchase(conn, "BBB", "Buyer", 500_000)
    assert len(research.insider_activity(conn, "AAA")["buys"]) == 1


def test_european_activity_finds_an_issuer_by_isin(conn):
    """Two of the three European sources identify an issuer by ISIN, so asking with
    one has to work -- otherwise those sources are unreachable from here."""
    add_bafin_txn(conn, "DE0007190001", "Kastanis, Vaios", 4_200_240)
    add_sweden_txn(conn, "SE0000653230", "Adam Rodman", 6_819_697)
    assert len(research.european_activity(conn, "DE0007190001")) == 1
    assert len(research.european_activity(conn, "SE0000653230")) == 1
    assert research.european_activity(conn, "DE9999999999") == []


def test_european_activity_sorts_across_mixed_date_formats(conn):
    """BaFin writes DD.MM.YYYY and the others write ISO. Sorted as plain strings,
    "01.09.2026" would sort before "2026-08-01" and the ordering would be nonsense."""
    add_bafin_txn(conn, "XX0000000001", "Older", 1_000, date="01.03.2026")
    add_bafin_txn(conn, "XX0000000001", "Newer", 1_000, date="01.09.2026")
    rows = research.european_activity(conn, "XX0000000001")
    assert [r[1] for r in rows] == ["Newer", "Older"]


def test_revised_swedish_filings_are_excluded(conn):
    add_sweden_txn(conn, "SE0000000001", "Live", 100_000, status="Aktuell")
    add_sweden_txn(conn, "SE0000000001", "Superseded", 100_000, status="Reviderad")
    names = [r[1] for r in research.european_activity(conn, "SE0000000001")]
    assert names == ["Live"]


def test_stakes_and_proposed_sales_are_collected(conn):
    add_stake(conn, "AAA", "Big Fund", 12.5)
    assert len(research.stakes(conn, "AAA")) == 1
    assert research.proposed_sales(conn, "AAA") == []


def test_isin_report_skips_what_it_cannot_verify(conn):
    """yfinance will return a price series for an ISIN, but nothing confirms the
    series belongs to that issuer. Showing a different company's chart under this
    heading would be worse than showing none."""
    add_bafin_txn(conn, "DE0007190001", "Kastanis, Vaios", 4_200_240,
                  issuer="Schulte-Schlagbaum AG")
    rep = research.build(conn, "DE0007190001")
    assert rep["is_isin"] is True
    assert rep["prices"]["windows"] == [] and rep["prices"]["current"] is None
    assert rep["news"] == [] and rep["filings"] == [] and rep["analyst"] is None
    assert rep["name"] == "Schulte-Schlagbaum AG", "issuer name should come from the local regulator"


def test_isin_report_renders_the_european_rows(conn):
    add_bafin_txn(conn, "DE0007190001", "Kastanis, Vaios", 4_200_240)
    text = research.format_report(research.build(conn, "DE0007190001"))
    assert "Kastanis, Vaios" in text
    assert "ISIN, а не тикер" in text


def test_isin_report_has_no_opinion_and_says_why(conn):
    """opinion.score() refuses an ISIN report outright (build() skips financials/
    annual-report/analyst for one entirely -- see research.build). The dossier
    should say plainly why no opinion appears, not just silently omit it."""
    add_bafin_txn(conn, "DE0007190001", "Someone", 1_000_000)
    rep = research.build(conn, "DE0007190001")
    assert rep["opinion"] is None
    text = research.format_report(rep)
    assert "опиниона нет" in text
    # Case-insensitive: the TradingView section legitimately relays TV's own
    # "Strong Buy" label, but only ever as a quoted third-party term with its
    # caveat -- never as the bot's own marketing-style line.
    low = text.lower()
    for verdict in ("рекомендуем", "стоит купить", "мы считаем", "наш прогноз", "целевая цена бота"):
        assert verdict not in low


def _minimal_rep(**overrides) -> dict:
    """A synthetic build()-shaped dict, bypassing build() itself -- build() makes
    several live network calls (price, financials, annual report, filings,
    news) even for a non-ISIN ticker, and this test file's own policy (see its
    module docstring) is to keep network-dependent code out of the test suite.
    format_report() and opinion.score() are both pure functions over this
    shape, so constructing it directly is the offline-testable path."""
    base = {
        "ticker": "AAPL", "cik": None, "is_isin": False, "name": None,
        "industry": None, "market_cap_eur": None, "size": None,
        "avg_daily_value": None, "exchange": None,
        "insiders": {"buys": [], "sells": []}, "european": [], "stakes": [],
        "proposed_sales": [], "political": [], "signals": [], "corroboration": None,
        "prices": {"windows": [], "current": None}, "analyst": None,
        "tradingview": None, "financials": None, "annual_report": None,
        "dilution": None, "ownership": None, "short": None, "earnings": None,
        "filings": [], "news": [],
    }
    base.update(overrides)
    return base


def test_report_with_an_opinion_shows_the_disclaimer_not_marketing_language():
    """When there IS enough to score, the opinion section renders -- but the
    surrounding disclaimer must still avoid marketing-style verdict phrasing,
    same forbidden list as the no-opinion case above."""
    rep = _minimal_rep(
        insiders={"buys": [("2026-09-01", "Buyer One", "CEO", 1, 1, 0, 500000, 0, 0, "u"),
                            ("2026-09-02", "Buyer Two", "CFO", 0, 1, 0, 500000, 0, 0, "u")],
                   "sells": []},
        analyst={"consensus": "Buy", "analyst_count": 10, "thin": False,
                 "counts": {"sb": 2, "b": 5, "h": 2, "s": 1, "ss": 0}, "trend": None,
                 "target_mean": 200.0, "target_high": 220.0, "target_low": 180.0,
                 "target_stale": False, "implied_upside_pct": 20.0,
                 "implied_upside_wide": False, "recent_actions": []},
    )
    rep["opinion"] = opinion.score(rep)
    assert rep["opinion"] is not None
    assert rep["opinion"]["label"] in dict(opinion.LABELS).values()
    text = research.format_report(rep)
    assert "ОПИНИОН" in text
    low = text.lower()
    for verdict in ("рекомендуем", "стоит купить", "мы считаем", "наш прогноз", "целевая цена бота"):
        assert verdict not in low


# ------------------------------------------------------------ analyst coverage
#
# analyst_view is a pure function: it takes the raw dicts a thin fetch wrapper
# pulls from yfinance and a current price from our OWN price history, and returns a
# structured view or None. The fetch itself is not tested (it needs the network);
# every judgement the section makes -- consensus, trend, staleness, thin coverage --
# lives here where it can be checked offline.

def _recs(now, three_mo_ago=None):
    """Build a recommendations list: newest period first, as yfinance returns it."""
    rows = [{"period": "0m", **now}]
    if three_mo_ago is not None:
        rows += [{"period": "-1m", **now}, {"period": "-2m", **now},
                 {"period": "-3m", **three_mo_ago}]
    return rows


BUY_HEAVY = {"strongBuy": 6, "buy": 18, "hold": 3, "sell": 1, "strongSell": 0}
HOLD_HEAVY = {"strongBuy": 1, "buy": 3, "hold": 18, "sell": 4, "strongSell": 2}


def test_analyst_view_is_none_without_coverage():
    """analyst_price_targets returns just {'current': X} for an uncovered name, and
    the recommendation table is all zeros. Nothing to show."""
    raw = {"price_targets": {"current": 20.0}, "recommendations": [], "upgrades_downgrades": []}
    assert research.analyst_view(raw, 20.0) is None


def test_analyst_view_reports_a_buy_consensus():
    raw = {"price_targets": {"mean": 120.0, "high": 150.0, "low": 90.0, "current": 100.0},
           "recommendations": _recs(BUY_HEAVY), "upgrades_downgrades": []}
    view = research.analyst_view(raw, 100.0)
    assert view["consensus"] == "Buy"
    assert view["analyst_count"] == 28
    assert view["thin"] is False


def test_analyst_view_flags_thin_coverage():
    raw = {"price_targets": {"mean": 120.0, "current": 100.0},
           "recommendations": _recs({"strongBuy": 1, "buy": 1, "hold": 0, "sell": 0, "strongSell": 0}),
           "upgrades_downgrades": []}
    assert research.analyst_view(raw, 100.0)["thin"] is True


def test_analyst_view_detects_a_shift_toward_buying():
    raw = {"price_targets": {"mean": 120.0, "current": 100.0},
           "recommendations": _recs(BUY_HEAVY, three_mo_ago=HOLD_HEAVY),
           "upgrades_downgrades": []}
    assert research.analyst_view(raw, 100.0)["trend"] == "позитивнее"


def test_analyst_view_detects_a_shift_toward_caution():
    raw = {"price_targets": {"mean": 120.0, "current": 100.0},
           "recommendations": _recs(HOLD_HEAVY, three_mo_ago=BUY_HEAVY),
           "upgrades_downgrades": []}
    assert research.analyst_view(raw, 100.0)["trend"] == "осторожнее"


def test_analyst_view_reports_no_meaningful_trend_change():
    raw = {"price_targets": {"mean": 120.0, "current": 100.0},
           "recommendations": _recs(BUY_HEAVY, three_mo_ago=BUY_HEAVY),
           "upgrades_downgrades": []}
    assert research.analyst_view(raw, 100.0)["trend"] is None


def test_analyst_view_computes_implied_upside_against_our_own_price():
    """The 'current' price comes from our 2-year history, not from yfinance's
    analyst blob -- that blob is stale for small and renamed names."""
    raw = {"price_targets": {"mean": 120.0, "high": 150.0, "low": 90.0, "current": 999.0},
           "recommendations": _recs(BUY_HEAVY), "upgrades_downgrades": []}
    view = research.analyst_view(raw, 100.0)   # our real price is 100, not yfinance's 999
    assert view["implied_upside_pct"] == pytest.approx(20.0)


def test_analyst_view_suppresses_a_target_that_disagrees_wildly_with_reality():
    """One held ticker reports an analyst target 2.4x its real price. A target
    an order of magnitude off the actual price is stale data, not a forecast."""
    raw = {"price_targets": {"mean": 900.0, "current": 900.0},
           "recommendations": _recs(BUY_HEAVY), "upgrades_downgrades": []}
    view = research.analyst_view(raw, 100.0)   # target 900 vs real price 100 -> 9x
    assert view["target_stale"] is True
    assert view["implied_upside_pct"] is None
    assert view["target_mean"] is None


def test_analyst_view_keeps_only_the_most_recent_firm_actions():
    actions = [{"date": f"2026-09-{d:02d}", "firm": f"Firm {d}", "to_grade": "Buy",
                "action": "main"} for d in range(1, 11)]
    raw = {"price_targets": {"mean": 120.0, "current": 100.0},
           "recommendations": _recs(BUY_HEAVY), "upgrades_downgrades": actions}
    view = research.analyst_view(raw, 100.0)
    assert len(view["recent_actions"]) <= 4
    assert view["recent_actions"][0]["firm"] == "Firm 1"


def test_isin_report_has_no_analyst_section(conn):
    """Same reasoning as price and news: an ISIN doesn't resolve to a tradeable
    symbol, so analyst data for it can't be trusted to be the right company."""
    add_bafin_txn(conn, "DE0007190001", "Someone", 1_000_000)
    rep = research.build(conn, "DE0007190001")
    assert rep["analyst"] is None


def test_analyst_section_carries_the_attribution_and_no_bot_verdict(conn):
    """The "targets run optimistic" methodology hedge was removed at the
    user's explicit request; the attribution -- this is sell-side's number,
    not the bot's -- stays, same reasoning as the TradingView section (see
    test_tradingview.py), since opinion.py has its own real bot opinion now."""
    view = {"consensus": "Buy", "analyst_count": 20, "thin": False,
            "counts": {"sb": 5, "b": 10, "h": 4, "s": 1, "ss": 0}, "trend": "позитивнее",
            "target_mean": 130.0, "target_high": 160.0, "target_low": 100.0,
            "target_stale": False, "implied_upside_pct": 30.0, "recent_actions": []}
    text = research._format_analyst(view)
    assert "не прогноз бота" in text
    for verdict in ("РЕКОМЕНДУЕМ", "СТОИТ КУПИТЬ", "покупайте", "наш прогноз"):
        assert verdict not in text


def test_analyst_view_notes_an_unusually_wide_implied_upside():
    """A target 2.4x the price is not stale enough to drop, but '+142%' with no
    context reads as a promise. It gets a note pointing at the dates."""
    raw = {"price_targets": {"mean": 240.0, "high": 300.0, "low": 200.0, "current": 240.0},
           "recommendations": _recs(BUY_HEAVY), "upgrades_downgrades": []}
    view = research.analyst_view(raw, 100.0)
    assert view["implied_upside_wide"] is True
    assert "большой разрыв" in research._format_analyst(view)


def test_analyst_view_does_not_note_a_normal_upside():
    raw = {"price_targets": {"mean": 115.0, "current": 100.0},
           "recommendations": _recs(BUY_HEAVY), "upgrades_downgrades": []}
    assert research.analyst_view(raw, 100.0)["implied_upside_wide"] is False


# --------------------------------------------------- financials / dilution / etc
#
# Same split as the rest of research.py: the fetch functions hit the network and
# are not tested; the pure format functions below carry the logic (trend
# detection, buyback-vs-dilution, rising-vs-falling short interest) and are checked
# with synthetic inputs.

def test_financials_trend_arrows():
    data = {"years": [2025, 2024, 2023, 2022],   # newest-first, as yfinance gives it
            "revenue": [3.1e9, 2.9e9, 2.5e9, 2.2e9],
            "net_income": [1.2e9, 1.1e9, 1.1e9, 0.87e9],
            "operating_income": [None, None, None, None],
            "fcf": [1.5e9, 1.4e9, 1.1e9, 1.0e9],
            "cash": [0.5e9, 0.4e9, 0.45e9, 0.99e9],
            "debt": [6.3e9, 4.6e9, 4.6e9, 4.6e9]}
    text = research._format_financials(data)
    assert "2022" in text and "2025" in text
    lines = {ln.split()[0]: ln for ln in text.splitlines() if ln.strip().startswith(("Выручка", "Кэш", "Долг"))}
    assert lines["Выручка"].endswith("↑")   # 2.2bn -> 3.1bn
    assert lines["Кэш"].endswith("↓")       # 0.99bn -> 0.5bn
    assert "Оп. прибыль" not in text        # all-None row is dropped


def test_dilution_detects_buyback_and_dilution():
    buyback = [{"end": "2025-03-31", "shares": 77.6e6}, {"end": "2026-06-30", "shares": 72.7e6}]
    assert "выкуп" in research._format_dilution(buyback)
    dilution = [{"end": "2025-03-31", "shares": 100e6}, {"end": "2026-06-30", "shares": 130e6}]
    assert "размытие" in research._format_dilution(dilution)
    flat = [{"end": "2025-03-31", "shares": 100e6}, {"end": "2026-06-30", "shares": 100.3e6}]
    assert "не менял" in research._format_dilution(flat)


def test_ownership_shows_holders_and_quarterly_change():
    data = {"insiders_pct": 0.036, "institutions_pct": 0.958, "institutions_count": 1584,
            "top_holders": [{"name": "Blackrock Inc.", "pct": 0.075, "pct_change": -0.047, "date": "2026-06-30"},
                            {"name": "Vanguard", "pct": 0.064, "pct_change": None, "date": "2026-06-30"}]}
    text = research._format_ownership(data)
    assert "инсайдеры 3.6%" in text and "институционалы 95.8%" in text and "1584 фондов" in text
    assert "Blackrock Inc." in text and "-4.7% за квартал" in text
    assert "Vanguard" in text  # holder with no pct_change still listed


def test_short_interest_direction():
    rising = {"pct_of_float": 0.132, "shares": 55e6, "prior": 50e6, "days_to_cover": 8.9}
    assert "выросли" in research._format_short(rising) and "13.2%" in research._format_short(rising)
    falling = {"pct_of_float": 0.02, "shares": 48e6, "prior": 55e6, "days_to_cover": 1.4}
    assert "снизились" in research._format_short(falling)


def test_earnings_shows_next_date_and_recent_surprises():
    data = {"next": {"date": "2026-10-20", "estimate": 5.02, "reported": None, "surprise": None},
            "recent": [{"date": "2026-07-21", "estimate": 4.98, "reported": 4.94, "surprise": -0.84},
                       {"date": "2026-04-21", "estimate": 4.46, "reported": 4.55, "surprise": 2.13}]}
    text = research._format_earnings(data)
    assert "Следующий отчёт: 2026-10-20" in text and "прогноз EPS 5.02" in text
    assert "2026-07-21  EPS 4.94  (-0.8% к прогнозу)" in text


def test_new_sections_absent_from_isin_report(conn):
    add_bafin_txn(conn, "DE0007190001", "Someone", 1_000_000)
    rep = research.build(conn, "DE0007190001")
    assert rep["financials"] is None and rep["dilution"] is None
    assert rep["ownership"] is None and rep["short"] is None and rep["earnings"] is None
    # TradingView is the one new source that runs for an ISIN (it resolves them);
    # whether it returns data depends on TV coverage, so only assert the key exists.
    assert "tradingview" in rep


def test_build_includes_annual_report_for_a_us_ticker(conn, monkeypatch):
    monkeypatch.setattr(research.annual_report, "build", lambda cik, session=None: {"marker": True})
    rep = research.build(conn, "AAPL")
    assert rep["annual_report"] == {"marker": True}


def test_build_skips_annual_report_for_an_isin(conn, monkeypatch):
    """ISINs already skip every other CIK-dependent section for the same
    reason (no confirmed CIK to key the lookup on)."""
    add_bafin_txn(conn, "DE0007190001", "Someone", 1_000_000)
    rep = research.build(conn, "DE0007190001")
    assert rep["annual_report"] is None


def test_format_report_includes_the_annual_report_section_when_present(conn, monkeypatch):
    monkeypatch.setattr(research.annual_report, "build", lambda cik, session=None: {"marker": True})
    monkeypatch.setattr(research.annual_report, "format_report",
                        lambda rep: "ANNUAL-REPORT-MARKER-TEXT" if rep else "")
    text = research.format_report(research.build(conn, "AAPL"))
    assert "ANNUAL-REPORT-MARKER-TEXT" in text


def test_format_report_omits_the_annual_report_section_when_absent(conn, monkeypatch):
    monkeypatch.setattr(research.annual_report, "build", lambda cik, session=None: None)
    text = research.format_report(research.build(conn, "AAPL"))
    assert "ANNUAL-REPORT-MARKER-TEXT" not in text
    assert "Годовой отчёт" not in text


def test_corroboration_summary_none_with_fewer_than_two_sources(conn):
    db.journal_signal(conn, {"source": "SEC", "kind": "cluster", "ticker": "AAA"})
    assert research.corroboration_summary(conn, "AAA") is None


def test_corroboration_summary_lists_distinct_sources(conn):
    db.journal_signal(conn, {"source": "SEC", "kind": "cluster", "ticker": "AAA"})
    db.journal_signal(conn, {"source": "SENATE", "kind": "cluster", "ticker": "AAA"})
    summary = research.corroboration_summary(conn, "AAA")
    assert summary["all_sources"] == ["SEC", "SENATE"]
    assert summary["recent_sources"] == ["SEC", "SENATE"]


def test_corroboration_summary_excludes_aged_out_sources_from_recent(conn):
    conn.execute(
        "INSERT INTO signal_journal (source, ticker, kind, emitted_at) VALUES (?, ?, ?, ?)",
        ("BAFIN", "AAA", "cluster", "2020-01-01"),
    )
    db.journal_signal(conn, {"source": "SEC", "kind": "cluster", "ticker": "AAA"})
    summary = research.corroboration_summary(conn, "AAA")
    assert summary["all_sources"] == ["BAFIN", "SEC"]
    assert summary["recent_sources"] == ["SEC"]


def test_format_report_shows_the_corroboration_summary_when_present(conn, monkeypatch):
    db.journal_signal(conn, {"source": "SEC", "kind": "cluster", "ticker": "AAPL"})
    monkeypatch.setattr(research, "corroboration_summary",
                        lambda conn, ticker: {"all_sources": ["SEC", "SENATE"],
                                                "recent_sources": ["SEC", "SENATE"]})
    text = research.format_report(research.build(conn, "AAPL"))
    assert "Независимые источники по этому тикеру: SEC, SENATE" in text
    # When recent_sources == all_sources, the sub-line must be suppressed (no redundancy)
    assert "за последние" not in text


def test_format_report_omits_the_corroboration_summary_when_absent(conn, monkeypatch):
    db.journal_signal(conn, {"source": "SEC", "kind": "cluster", "ticker": "AAPL"})
    monkeypatch.setattr(research, "corroboration_summary", lambda conn, ticker: None)
    text = research.format_report(research.build(conn, "AAPL"))
    assert "Независимые источники" not in text
    assert "Сигналы, которые бот уже присылал" in text


def test_format_report_shows_recent_sources_subline_when_differs(conn, monkeypatch):
    """When recent_sources != all_sources, the '(за последние N дней: ...)'
    sub-line must appear with the recent sources list."""
    db.journal_signal(conn, {"source": "SEC", "kind": "cluster", "ticker": "AAPL"})
    monkeypatch.setattr(research, "corroboration_summary",
                        lambda conn, ticker: {"all_sources": ["BAFIN", "SEC", "SENATE"],
                                                "recent_sources": ["SEC", "SENATE"]})
    text = research.format_report(research.build(conn, "AAPL"))
    # All-time sources line must appear
    assert "Независимые источники по этому тикеру: BAFIN, SEC, SENATE" in text
    # Recent sources sub-line must appear with correct content
    assert "за последние 30 дней: SEC, SENATE" in text


def test_corroboration_line_never_renders_a_verdict(conn, monkeypatch):
    """The dossier's corroboration line says only "another source had activity
    here" -- same discipline as test_report_never_renders_a_verdict above, but
    for the corroboration line specifically (the design spec's Testing section
    calls for extending the forbidden-terms check to this line too).

    Same forbidden-terms list as test_report_never_renders_a_verdict above, not
    the generic "buy "/"sell "/"target price" one used elsewhere in the project
    -- research.build() legitimately pulls in an analyst-consensus section that
    quotes TradingView/analyst ratings verbatim ("Buy", "td cowen -- buy",
    target prices), always as a caveated third-party quote, never as the bot's
    own line. Those terms alone are not a defect; the ones below are."""
    db.journal_signal(conn, {"source": "SEC", "kind": "cluster", "ticker": "AAPL"})
    db.journal_signal(conn, {"source": "SENATE", "kind": "cluster", "ticker": "AAPL"})
    monkeypatch.setattr(research, "corroboration_summary",
                        lambda conn, ticker: {"all_sources": ["SEC", "SENATE"],
                                                "recent_sources": ["SEC", "SENATE"]})
    text = research.format_report(research.build(conn, "AAPL")).lower()
    assert "независимые источники по этому тикеру" in text  # sanity: line actually rendered
    for bad in ("рекомендуем", "стоит купить", "мы считаем", "наш прогноз", "целевая цена бота"):
        assert bad not in text


def test_format_entry_target_shows_both_when_available():
    rep = {"prices": {"current": 123.45},
           "analyst": {"target_mean": 150.0, "implied_upside_pct": 20.0, "target_stale": False}}
    text = research.format_entry_target(rep)
    assert "123.45" in text
    assert "150.00" in text and "+20%" in text


def test_format_entry_target_skips_a_stale_analyst_target():
    """Same rule as the dossier's own analyst section: a stale target (way off
    the real price) is dropped rather than shown, real number or not."""
    rep = {"prices": {"current": 100.0},
           "analyst": {"target_mean": 500.0, "implied_upside_pct": None, "target_stale": True}}
    text = research.format_entry_target(rep)
    assert "100.00" in text and "500.00" not in text


def test_format_entry_target_price_only_when_no_analyst_coverage():
    rep = {"prices": {"current": 100.0}, "analyst": None}
    text = research.format_entry_target(rep)
    assert "100.00" in text and "цель" not in text.lower()


def test_format_entry_target_empty_when_nothing_available():
    assert research.format_entry_target({"prices": {"current": None}, "analyst": None}) == ""
    assert research.format_entry_target({"prices": {}, "analyst": None}) == ""
