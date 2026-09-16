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
        "prices": {"windows": []}, "tradingview": None,
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
