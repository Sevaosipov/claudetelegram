"""chart_check.py: the chart verdict by fixed rules, the same every time, and Claude's review of it."""
from __future__ import annotations

import pytest

import chart_check as cc


def rising(n=260, start=10.0, step=0.02):
    return [start + i * step for i in range(n)]


def healthy():
    """A long rise, a 5 % dip and a partial recovery: above both averages, RSI cooled, off the high."""
    out = [10.0 + 0.08 * i for i in range(240)]
    out += [out[-1] - 0.142 * i for i in range(1, 11)]
    for i in range(10):
        out.append(out[-1] + (0.10 if i % 2 == 0 else -0.02))
    return out


def test_too_little_history_is_no_facts_and_a_neutral_verdict():
    assert cc.facts(rising(59)) is None
    assert cc.verdict(None) == (cc.NEUTRAL, "мало истории цен")


def test_a_steady_rise_above_both_averages_is_for():
    f = cc.facts(healthy())
    assert f.last > f.sma50 > f.sma200 and f.rsi < 75 and f.last < f.high_252 * 0.97
    assert cc.verdict(f) == (cc.FOR, "цена выше 50- и 200-дн. средних")


def test_a_trend_at_its_high_or_overbought_is_neutral():
    assert cc.verdict(cc.facts(rising()))[0] == cc.NEUTRAL                    # RSI 100, at the 1-year high
    assert "RSI" in cc.verdict(cc.facts(rising()))[1]


def test_under_the_200_day_average_is_against():
    closes = rising(200) + [13.0 - i * 0.1 for i in range(60)]
    assert cc.verdict(cc.facts(closes)) == (cc.AGAINST, "цена ниже 200-дн. средней")


def test_a_price_standing_still_after_a_jump_is_against_whatever_the_averages_say():
    closes = [20.0 + (i % 3) * 0.05 for i in range(240)] + [23.4, 29.0] + [29.0 + (i % 2) * 0.1 for i in range(8)]
    f = cc.facts(closes)
    assert f.pinned and f.jump == pytest.approx(29.0 / 23.4 - 1)
    assert cc.verdict(f) == (cc.AGAINST, "после скачка +24% цена стоит на месте")
    moved_on = closes[:-8] + [29.0 * (1 + 0.02 * i) for i in range(1, 9)]     # a jump the price kept running from
    assert not cc.facts(moved_on).pinned


def test_a_deep_fall_under_the_50_day_is_against_before_there_are_200_bars():
    closes = [10.0 + i * 0.05 for i in range(80)] + [14.0 - i * 0.25 for i in range(20)]
    f = cc.facts(closes)
    assert f.sma200 is None and f.last < f.sma50 and f.last / f.high_60 - 1 <= -0.20
    assert cc.verdict(f)[0] == cc.AGAINST


def test_a_sell_mirrors_a_buy():
    falling = [40.0 - c for c in healthy()]
    assert cc.verdict(cc.facts(falling, "short"), "short") == (cc.FOR, "цена ниже 50- и 200-дн. средних")
    assert cc.verdict(cc.facts(rising(), "short"), "short")[0] == cc.AGAINST


def test_the_same_closes_give_the_same_verdict_every_time():
    closes = healthy()
    assert len({cc.verdict(cc.facts(closes)) for _ in range(20)}) == 1


# ------------------------------------------------------------------ the line
def test_claudes_agreeing_note_is_the_line():
    assert cc.compose(cc.FOR, "x", "график за — цена 29,1 выше 50-дн. 25,9") == "График за — цена 29,1 выше 50-дн. 25,9"


def test_a_changed_verdict_says_what_the_rules_had_said():
    assert cc.compose(cc.FOR, "x", "график против — оферта выкупа по 8,50 держит цену") == (
        "График против (по правилам: за) — оферта выкупа по 8,50 держит цену")


@pytest.mark.parametrize("note", [None, "", "график не прочитан", "вот мой анализ"])
def test_without_a_usable_note_the_rules_verdict_stands_alone_and_is_marked(note):
    assert cc.compose(cc.AGAINST, "цена ниже 200-дн. средней", note) == (
        "График против (по правилам, без Claude) — цена ниже 200-дн. средней")


def test_the_check_gives_claude_the_rules_numbers_and_asks_twice_at_most(capsys):
    closes = [(f"d{i}", c) for i, c in enumerate(healthy())]
    asked = []

    def silent(name, side, facts):
        asked.append((name, side, facts))
        return None
    line = cc.line("RXO", "RXO", "SEC", "long", "Балл: 65", closes_fn=lambda t, s: closes, note_fn=silent)
    assert line == "График за (по правилам, без Claude) — цена выше 50- и 200-дн. средних"
    assert len(asked) == 2 and asked[0][:2] == ("RXO", "покупка")
    assert asked[0][2].startswith("Балл: 65\nВердикт по правилам бота: график за (цена выше 50- и 200-дн. средних)")
    assert "RSI(14):" in asked[0][2] and "Диапазон 60 дней:" in asked[0][2]

    answers = iter(["график не прочитан", "график нейтрален — отчёт 14.10"])
    assert cc.line("RXO", "RXO", "SEC", "long", closes_fn=lambda t, s: closes,
                   note_fn=lambda *a: next(answers)) == "График нейтрален (по правилам: за) — отчёт 14.10"


def test_a_failing_price_or_review_never_raises(capsys):
    def boom(*a):
        raise RuntimeError("down")
    assert cc.line("RXO", "RXO", "SEC", "long", closes_fn=boom, note_fn=lambda *a: None) is None
    assert cc.line("RXO", "RXO", "SEC", "long", closes_fn=boom,
                   note_fn=lambda *a: "график против — оферта") == "График против (по правилам: нейтрален) — оферта"
    closes = [(f"d{i}", c) for i, c in enumerate(rising())]
    assert cc.line("RXO", "RXO", "SEC", "long", closes_fn=lambda t, s: closes, note_fn=boom).startswith(
        "График нейтрален (по правилам, без Claude)")
    assert "no price history" in capsys.readouterr().err
