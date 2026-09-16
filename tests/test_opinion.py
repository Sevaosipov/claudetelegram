"""opinion.score()'s component logic -- pure functions over research.build()'s
dict shape, no network, same policy as test_tradingview.py/test_research.py."""
from __future__ import annotations

import datetime as dt

import opinion

TODAY = dt.date.today().isoformat()


def _rep(**overrides) -> dict:
    base = {
        "is_isin": False, "insiders": {"buys": [], "sells": []}, "political": [],
        "stakes": [], "annual_report": None, "analyst": None, "ownership": None,
        "prices": {"windows": []}, "tradingview": None, "news": [],
    }
    base.update(overrides)
    return base


def test_isin_never_gets_an_opinion():
    rep = _rep(is_isin=True, insiders={"buys": [("x", "A", "", 1, 0, 0, 1, 0, 0, "u")] * 5, "sells": []},
                analyst={"consensus": "Strong Buy"})
    assert opinion.score(rep) is None


def test_too_few_factors_returns_none():
    # only one contributing factor (insiders) -- below MIN_FACTORS
    rep = _rep(insiders={"buys": [(TODAY, "A", "", 1, 0, 0, 1, 0, 0, "u")], "sells": []})
    assert opinion.score(rep) is None


def test_net_insider_buying_pushes_score_positive():
    rep = _rep(
        insiders={"buys": [(TODAY, "A", "CEO", 1, 1, 0, 1, 0, 0, "u"),
                            (TODAY, "B", "CFO", 0, 1, 0, 1, 0, 0, "u")], "sells": []},
        analyst={"consensus": "Buy", "implied_upside_pct": None, "target_stale": False},
    )
    op = opinion.score(rep)
    assert op is not None
    assert op["score"] > 0
    assert any("Инсайдеры" in note for _, note in op["factors"])


def test_stale_insider_purchases_dont_count():
    old = (dt.date.today() - dt.timedelta(days=opinion.RECENCY_DAYS + 30)).isoformat()
    rep = _rep(
        insiders={"buys": [(old, "A", "", 1, 1, 0, 1, 0, 0, "u")], "sells": []},
        analyst={"consensus": "Hold", "implied_upside_pct": None, "target_stale": False},
        stakes=[(TODAY, "Fund", "SCHEDULE 13D", 6.0, None, "u")],
    )
    op = opinion.score(rep)
    # insider factor shouldn't fire at all (note absent) since the purchase is stale
    assert op is None or not any("Инсайдеры" in note for _, note in op["factors"])


def test_red_flags_are_a_heavy_negative():
    rep = _rep(
        annual_report={"red_flags": [{"key": "going_concern", "label": "going concern"}],
                       "financials": {}},
        analyst={"consensus": "Hold", "implied_upside_pct": None, "target_stale": False},
    )
    op = opinion.score(rep)
    assert op is not None
    pts = next(p for p, note in op["factors"] if "тревожных флаг" in note)
    assert pts == opinion.W_RED_FLAG


def test_stale_analyst_target_is_excluded_from_upside_points():
    rep = _rep(
        analyst={"consensus": "Hold", "implied_upside_pct": 200.0, "target_stale": True},
        stakes=[(TODAY, "Fund", "SCHEDULE 13G", 6.0, None, "u")],
    )
    op = opinion.score(rep)
    note = next(note for _, note in op["factors"] if "Аналитики" in note)
    assert "потенциал" not in note  # upside line only appears when not stale


def test_labels_follow_score_thresholds():
    # exercise the label lookup the same way score() does, without needing a
    # full rep for every band
    def label(total):
        return next(l for cut, l in opinion.LABELS if cut is None or total >= cut)
    assert label(40) == "Покупать"
    assert label(20) == "Скорее покупать"
    assert label(0) == "Держать"
    assert label(-20) == "Скорее избегать"
    assert label(-50) == "Избегать"


def test_format_opinion_includes_every_factor_and_the_disclaimer():
    rep = _rep(
        insiders={"buys": [(TODAY, "A", "CEO", 1, 1, 0, 1, 0, 0, "u")], "sells": []},
        analyst={"consensus": "Strong Buy", "implied_upside_pct": 15.0, "target_stale": False},
    )
    op = opinion.score(rep)
    text = opinion.format_opinion(op)
    assert op["label"] in text
    for _, note in op["factors"]:
        assert note in text
    assert "не лицензированная" in text


def test_format_opinion_empty_on_none():
    assert opinion.format_opinion(None) == ""


def test_news_component_counts_bullish_and_bearish_headlines():
    news = [
        {"title": "Company beats estimates and raises guidance"},
        {"title": "Shares rally on record high demand"},
        {"title": "Regulator opens investigation into pricing"},
        {"title": "Totally unrelated headline about the weather"},
    ]
    pts, note = opinion._news_component(news)
    assert pts > 0  # 2 bullish (beats estimates, raises guidance) vs 1 bearish (investigation)
    assert "бычьих" in note and "медвежьих" in note


def test_news_component_is_naive_about_negation():
    """Documented limitation, not a bug: 'doesn't disappoint' still counts as
    bearish because the heuristic is a plain substring match on 'disappoint'."""
    pts, _ = opinion._news_component([{"title": "Product launch doesn't disappoint fans"}])
    assert pts < 0


def test_news_component_no_matches_returns_none():
    pts, note = opinion._news_component([{"title": "A perfectly neutral headline"}])
    assert pts == 0.0 and note is None


def test_news_component_empty_list_returns_none():
    assert opinion._news_component([]) == (0.0, None)


def test_news_feeds_into_overall_score():
    rep = _rep(
        insiders={"buys": [(TODAY, "A", "CEO", 1, 1, 0, 1, 0, 0, "u")], "sells": []},
        analyst={"consensus": "Hold", "implied_upside_pct": None, "target_stale": False},
        news=[{"title": "Stock plunges after downgrade"}, {"title": "Company warns on outlook"}],
    )
    op = opinion.score(rep)
    note = next(n for _, n in op["factors"] if "Новости" in n)
    assert "0 бычьих, 2 медвежьих" in note
    assert "грубо" in note  # the crudeness caveat travels with every news line, not just the disclaimer
