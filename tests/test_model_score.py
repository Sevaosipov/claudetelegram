"""model_score.py: the pure 0-100 score for stocks and coins and the stop distance. Offline and DB-free:
signals are SimpleNamespace stand-ins carrying the same attributes as ClusterSignal / StakeSignal, buyers
are cluster.roles.Buyer."""
from __future__ import annotations

import types

import pytest

import model_score as ms
from cluster.roles import Buyer


# ------------------------------------------------------------------ builders
def _buyers(n, roles=()):
    """n management buyers; `roles` overrides the first ones' roles (rest: director)."""
    out = []
    for i in range(n):
        role = roles[i] if i < len(roles) else "director"
        out.append(Buyer(name=f"P{i}", role=role, total_eur=100_000.0))
    return out


def _cluster(n=1, roles=(), *, source="SEC", pct=None, inc=None, first=False,
             holder_only=False, size=(1e9, 1e6), corroborated=(), **kw):
    """A ClusterSignal stand-in. `size` = (market_cap_eur, avg_daily_value)."""
    sig = types.SimpleNamespace(
        source=source, ticker="AAA", company="Acme Corp",
        buyer_count=n, buyers=_buyers(n, roles), holder_only=holder_only,
        value_pct_of_mcap=pct, position_increase_pct=inc, first_buy=first,
        market_cap_eur=size[0], avg_daily_value=size[1],
        corroborated_by=list(corroborated))
    for k, v in kw.items():
        setattr(sig, k, v)
    return sig


def _stake(form="SCHEDULE 13D", percent=8.0, prev=None, corroborated=(), size=(1e9, 1e6)):
    return types.SimpleNamespace(
        source="SEC13DG", ticker="AAA", company="Acme Corp", person="Fund LP",
        form_type=form, percent=percent, prev_percent=prev,
        is_activist=form.startswith("SCHEDULE 13D"),
        market_cap_eur=size[0], avg_daily_value=size[1],
        corroborated_by=list(corroborated))


def _flat(n=130, price=100.0):
    return [price] * n


def _grow(n, per_step):
    """Closes compounding by `per_step` every day, oldest first."""
    return [100.0 * (1 + per_step) ** i for i in range(n)]


def _titles(*titles):
    return [{"title": t} for t in titles]


# ------------------------------------------------------------------ insider_part
@pytest.mark.parametrize("n,points", [(1, 22), (2, 34), (3, 42), (4, 46), (5, 46)])
def test_insider_count_points(n, points):
    assert ms.insider_part(_cluster(n)).points == points


def test_insider_count_line_is_russian_and_plural_aware():
    assert ms.insider_part(_cluster(3)).lines == ["3 инсайдера из руководства"]
    assert ms.insider_part(_cluster(1)).lines == ["1 инсайдер из руководства"]
    assert ms.insider_part(_cluster(5)).lines == ["5 инсайдеров из руководства"]


def test_insider_only_management_roles_count():
    sig = _cluster(0)
    sig.buyers = [Buyer("A", "director", 1.0), Buyer("B", "associate", 1.0),
                  Buyer("C", "other", 1.0), Buyer("D", "holder", 1.0)]
    assert ms.insider_part(sig).points == 22          # only the director is management


def test_insider_ceo_adds_ten_with_a_line():
    part = ms.insider_part(_cluster(2, roles=["ceo"]))
    assert part.points == 34 + 10
    assert "CEO среди покупателей" in part.lines


def test_insider_cfo_adds_ten_with_a_cfo_line():
    part = ms.insider_part(_cluster(2, roles=["cfo"]))
    assert part.points == 34 + 10
    assert "CFO среди покупателей" in part.lines


def test_insider_chair_alone_adds_five():
    part = ms.insider_part(_cluster(2, roles=["chair"]))
    assert part.points == 34 + 5


def test_insider_ceo_and_chair_together_only_ten():
    part = ms.insider_part(_cluster(3, roles=["ceo", "chair"]))
    assert part.points == 42 + 10


@pytest.mark.parametrize("pct,extra", [(0.009, 0), (0.01, 3), (0.05, 6), (0.2, 10), (5.0, 10)])
def test_insider_value_share_points(pct, extra):
    assert ms.insider_part(_cluster(1, pct=pct)).points == 22 + extra


def test_insider_value_share_line_uses_a_decimal_comma():
    assert "0,06% компании" in ms.insider_part(_cluster(1, pct=0.06)).lines


def test_insider_missing_value_share_adds_nothing():
    assert ms.insider_part(_cluster(1, pct=None)).points == 22


@pytest.mark.parametrize("inc,extra", [(9, 0), (10, 3), (30, 6), (400, 6)])
def test_insider_position_increase_points(inc, extra):
    assert ms.insider_part(_cluster(1, inc=inc)).points == 22 + extra


def test_insider_position_increase_line():
    assert "позиция +35%" in ms.insider_part(_cluster(1, inc=35)).lines


def test_insider_first_buy_adds_four():
    part = ms.insider_part(_cluster(1, first=True))
    assert part.points == 22 + 4
    assert "первая покупка" in part.lines


def test_insider_part_is_capped_at_sixty():
    sig = _cluster(5, roles=["ceo"], pct=0.3, inc=50, first=True)
    assert 46 + 10 + 10 + 6 + 4 == 76
    assert ms.insider_part(sig).points == 60


def test_insider_holder_only_is_a_flat_fifteen():
    part = ms.insider_part(_cluster(3, holder_only=True, pct=1.0, first=True))
    assert part == ms.Part(15, ["только крупные акционеры (>10%)"])


@pytest.mark.parametrize("source", ["HOUSE", "SENATE"])
def test_insider_politicians_get_nothing_here(source):
    assert ms.insider_part(_cluster(3, source=source, pct=1.0, first=True)) == ms.Part(0, [])


def test_insider_without_a_buyers_list_uses_buyer_count_and_no_roles():
    sig = _cluster(0, roles=["ceo"])
    sig.buyers = []
    sig.buyer_count = 3
    part = ms.insider_part(sig)
    assert part.points == 42                       # no CEO points: roles unknown
    assert part.lines == ["3 инсайдера из руководства"]
    del sig.buyers                                  # the attribute missing altogether
    assert ms.insider_part(sig).points == 42


def test_insider_zero_management_scores_zero():
    sig = _cluster(0)
    sig.buyers, sig.buyer_count = [], 0
    assert ms.insider_part(sig) == ms.Part(0, [])


# ------------------------------------------------------------------ stake_part
@pytest.mark.parametrize("percent,points", [(5.0, 30), (8.0, 36), (20.0, 50)])
def test_stake_13d_points(percent, points):
    assert ms.stake_part(_stake("SCHEDULE 13D", percent)).points == points


def test_stake_13d_amendment_with_growth_is_still_an_activist():
    part = ms.stake_part(_stake("SCHEDULE 13D/A", 8.0, prev=6.5))
    assert part.points == 36 + 5
    assert part.lines == ["активист 13D: 8,0%", "доля +1,5 п.п."]


def test_stake_13g_points():
    assert ms.stake_part(_stake("SCHEDULE 13G", 12.0)).points == 22
    assert ms.stake_part(_stake("SCHEDULE 13G", 5.0)).points == 15
    assert ms.stake_part(_stake("SCHEDULE 13G", 30.0)).points == 25


# Real filings the first live run scored as activist buys (2026-09-30): routine amendments by
# holders who already control the company, and a takeover.
@pytest.mark.parametrize("form,percent,prev,points,lines", [
    ("SCHEDULE 13D/A", 18.7, None, 0, ["поправка без роста доли"]),     # IEP-like: no known growth
    ("SCHEDULE 13D/A", 55.7, 53.5, 0, ["контрольный пакет — не сигнал"]),   # BZFD-like
    ("SCHEDULE 13D/A", 19.85, 18.22, 55, ["активист 13D: 19,9%", "доля +1,6 п.п."]),  # TRMD-like
    ("SCHEDULE 13D", 8.0, None, 36, ["активист 13D: 8,0%"]),            # an original 13D
    ("SCHEDULE 13D", 100.0, None, 0, ["контрольный пакет — не сигнал"]),     # CHR-like takeover
])
def test_stake_calibration_on_real_filing_shapes(form, percent, prev, points, lines):
    assert ms.stake_part(_stake(form, percent, prev=prev)) == ms.Part(points, lines)


@pytest.mark.parametrize("form,percent,prev", [
    ("SCHEDULE 13D/A", 8.0, None), ("SCHEDULE 13D/A", 8.0, 7.5), ("SCHEDULE 13G/A", 12.0, None),
    ("SCHEDULE 13G/A", 12.0, 11.2), ("SC 13D/A", 9.0, None), ("SCHEDULE 13D/A", 8.0, 9.0),
])
def test_an_amendment_without_a_point_of_growth_scores_nothing(form, percent, prev):
    assert ms.stake_part(_stake(form, percent, prev=prev)) == ms.Part(0, ["поправка без роста доли"])


def test_an_amendment_growing_by_a_point_despite_float_noise_scores():
    assert ms.stake_part(_stake("SCHEDULE 13G/A", 8.2, prev=7.2)).points == pytest.approx(15 + 3.2 + 5)


@pytest.mark.parametrize("form,prev", [("SCHEDULE 13D", None), ("SCHEDULE 13G", None),
                                       ("SCHEDULE 13D/A", 40.0), ("SCHEDULE 13G/A", 10.0)])
def test_a_controlling_stake_is_not_a_signal_whatever_the_form(form, prev):
    assert ms.stake_part(_stake(form, ms.STAKE_CONTROL_PERCENT, prev=prev)) == \
        ms.Part(0, ["контрольный пакет — не сигнал"])
    assert ms.stake_part(_stake(form.removesuffix("/A"), 49.9)).points > 0
    assert ms.STAKE_CONTROL_PERCENT == 50.0


def test_stake_lines_use_a_decimal_comma():
    assert ms.stake_part(_stake("SCHEDULE 13D", 8.0)).lines == ["активист 13D: 8,0%"]
    assert ms.stake_part(_stake("SCHEDULE 13G", 12.0)).lines == ["13G: 12,0%"]


def test_stake_growth_of_a_point_adds_five():
    part = ms.stake_part(_stake("SCHEDULE 13D", 8.0, prev=6.0))
    assert part.points == 36 + 5
    assert part.lines == ["активист 13D: 8,0%", "доля +2,0 п.п."]


def test_stake_growth_under_a_point_adds_nothing():
    part = ms.stake_part(_stake("SCHEDULE 13D", 8.0, prev=7.5))
    assert part.points == 36
    assert len(part.lines) == 1


def test_stake_growth_of_exactly_a_point_counts_despite_float_noise():
    assert 8.2 - 7.2 < 1.0                          # 0.9999999999999991 in floats
    part = ms.stake_part(_stake("SCHEDULE 13G", 8.2, prev=7.2))
    assert part.points == pytest.approx(15 + 3.2 + 5)


def test_stake_part_never_exceeds_the_cap():
    top = ms.stake_part(_stake("SCHEDULE 13D", 45.0, prev=30.0))
    assert top.points == 55 <= 60


# ------------------------------------------------------------------ trigger_part
def test_triggers_each_flag_alone():
    sig = _cluster(1)
    assert ms.trigger_part(sig, activist=True, passive_big=False, politicians=False) == \
        ms.Part(15, ["рядом активист 13D"])
    assert ms.trigger_part(sig, activist=False, passive_big=True, politicians=False) == \
        ms.Part(6, ["рядом 13G от 10%"])
    assert ms.trigger_part(sig, activist=False, passive_big=False, politicians=True) == \
        ms.Part(5, ["покупают политики"])
    assert ms.trigger_part(sig, activist=False, passive_big=False, politicians=False) == \
        ms.Part(0, [])


def test_triggers_corroboration_adds_five_and_names_the_sources():
    sig = _cluster(1, corroborated=["BAFIN", "SEC13DG"])
    part = ms.trigger_part(sig, activist=False, passive_big=False, politicians=False)
    assert part.points == 5
    assert part.lines == ["подтверждает: BAFIN, SEC13DG"]


def test_triggers_all_flags_and_corroboration_are_capped_at_twenty():
    sig = _cluster(1, corroborated=["NORWAY"])
    part = ms.trigger_part(sig, activist=True, passive_big=True, politicians=True)
    assert 15 + 6 + 5 + 5 == 31
    assert part.points == 20


def test_triggers_politicians_are_not_counted_twice_through_corroboration():
    sig = _cluster(1, corroborated=["HOUSE", "SENATE"])
    part = ms.trigger_part(sig, activist=False, passive_big=False, politicians=True)
    assert part.points == 5                        # only «покупают политики»
    assert part.lines == ["покупают политики"]


def test_triggers_a_new_source_still_corroborates_next_to_politicians():
    sig = _cluster(1, corroborated=["HOUSE", "SEC"])
    part = ms.trigger_part(sig, activist=False, passive_big=False, politicians=True)
    assert part.points == 10
    assert part.lines == ["покупают политики", "подтверждает: SEC"]


def test_triggers_stake_sources_are_not_counted_twice_through_corroboration():
    sig = _cluster(1, corroborated=["SEC13DG"])
    part = ms.trigger_part(sig, activist=True, passive_big=False, politicians=False)
    assert part.points == 15
    part = ms.trigger_part(sig, activist=False, passive_big=True, politicians=False)
    assert part.points == 6


# ------------------------------------------------------------------ momentum_part
def test_momentum_six_month_gain_over_25_percent():
    part = ms.momentum_part([100.0] * 126 + [130.0])
    # 127 closes: closes[-127] = 100, last 130 -> +30%; last month also up
    assert part.points == 8 + 2
    assert "6 мес. +30%" in part.lines


def test_momentum_small_six_month_gain():
    part = ms.momentum_part([100.0] * 126 + [110.0])
    assert part.points == 5 + 2
    assert "6 мес. +10%" in part.lines


def test_momentum_six_month_needs_127_closes():
    part = ms.momentum_part([100.0] * 25 + [130.0])     # 26 closes: only the 1-month leg
    assert part.points == 2


def test_momentum_above_the_200_day_average():
    # 200 closes; the six-month and one-month legs are flat/negative, the last is above
    # the 200-day mean (113.75): exactly the +5 for the average
    closes = [50.0] * 73 + [200.0] + [150.0] * 126
    part = ms.momentum_part(closes)
    assert part.points == 5
    assert part.lines == ["выше 200-дн. средней"]


def test_momentum_below_the_200_day_average_scores_nothing_for_it():
    closes = [150.0] * 126 + [100.0] * 74
    assert ms.momentum_part(closes).points == 0


def test_momentum_one_month_leg_alone():
    part = ms.momentum_part([100.0] * 21 + [101.0])
    assert part.points == 2


def test_momentum_short_history_is_zero():
    assert ms.momentum_part([100.0 + i for i in range(21)]) == ms.Part(0, [])
    assert ms.momentum_part([]) == ms.Part(0, [])


def test_momentum_flat_prices_score_nothing():
    assert ms.momentum_part(_flat(250)).points == 0


def test_momentum_is_capped_at_fifteen():
    part = ms.momentum_part(_grow(250, 0.005))
    assert part.points == 15                       # 8 + 5 + 2
    assert len(part.lines) == 3


# ------------------------------------------------------------------ news_part
def test_news_none_or_empty_is_nothing():
    assert ms.news_part(None) == (ms.Part(0, []), None)
    assert ms.news_part([]) == (ms.Part(0, []), None)


def test_news_items_without_a_title_are_ignored():
    part, red = ms.news_part([{"link": "x"}, {"title": None}, {"title": ""}])
    assert part == ms.Part(0, []) and red is None


def test_news_red_flag_is_returned_and_not_counted_as_negative():
    title = "Company announces $50M public offering"
    part, red = ms.news_part(_titles(title))
    assert red == title
    assert part.points == 0


def test_news_first_red_flag_wins():
    _, red = ms.news_part(_titles("Fine day", "Bankruptcy filed", "Subpoena received"))
    assert red == "Bankruptcy filed"


@pytest.mark.parametrize("phrase", list(ms.RED_FLAGS))
def test_news_every_red_flag_phrase_blocks(phrase):
    _, red = ms.news_part(_titles(f"Acme {phrase.upper()} news"))
    assert red is not None


def test_news_one_title_matching_two_negatives_counts_once():
    part, _ = ms.news_part(_titles("Analyst downgrade after lawsuit"))
    assert part.points == -10


def test_news_two_negative_titles():
    part, _ = ms.news_part(_titles("Analyst downgrade one", "Another downgrade two"))
    assert part.points == -20
    assert part.lines == ["новости: 2 плохие"]


def test_news_negative_is_clamped_at_minus_thirty():
    part, _ = ms.news_part(_titles("downgrade a", "lawsuit b", "recall c", "probe d"))
    assert part.points == -30


def test_news_positive_points_and_clamp():
    part, _ = ms.news_part(_titles("upgrade", "buyback announced", "beats estimates"))
    assert part.points == 10                       # 15, clamped to +10
    assert part.lines == ["новости: 3 хорошие"]


def test_news_mixed_line():
    part, _ = ms.news_part(_titles("downgrade", "upgrade", "raises guidance"))
    assert part.points == -10 + 5 + 5
    assert part.lines == ["новости: 1 плохая, 2 хорошие"]


def test_news_matching_is_case_insensitive():
    part, _ = ms.news_part(_titles("FDA Approval for new drug"))
    assert part.points == 5


def test_news_plain_titles_score_nothing():
    assert ms.news_part(_titles("Acme opens new office")) == (ms.Part(0, []), None)


def test_news_coin_red_flags_only_apply_to_coins():
    title = "Major exchange hack drains hot wallet"
    _, red_coin = ms.news_part(_titles(title), coin=True)
    part, red_stock = ms.news_part(_titles(title), coin=False)
    assert red_coin == title
    assert red_stock is None and part.points == 0


def test_news_sec_lawsuit_is_a_coin_red_flag_not_a_negative():
    title = "SEC lawsuit against exchange"
    part, red = ms.news_part(_titles(title), coin=True)
    assert red == title and part.points == 0
    part, red = ms.news_part(_titles(title), coin=False)
    assert red is None and part.points == -10      # plain «lawsuit»


@pytest.mark.parametrize("phrase", list(ms.COIN_RED_FLAGS))
def test_news_every_coin_red_flag_phrase_blocks_coins_only(phrase):
    title = f"Acme {phrase.upper()} news"
    assert ms.news_part(_titles(title), coin=True)[1] == title
    assert ms.news_part(_titles(title), coin=False)[1] is None


@pytest.mark.parametrize("title", [
    "ETHGlobal hackathon winners announced",
    "Shackleton fund buys ether",
    "Whack-a-mole regulation continues",
    "Exploitation of workers claims dismissed",
])
def test_news_coin_red_flags_need_a_word_boundary(title):
    assert ms.news_part(_titles(title), coin=True) == (ms.Part(0, []), None)


@pytest.mark.parametrize("title", [
    "Exchange hacked for $200M", "DeFi exploit drains pool", "Bridge exploits continue",
    "Hackers drain wallet", "Hack of the exchange", "Funds stolen from exchange",
    "Protocol exploited overnight", "Exploiting a bug drained the pool",
])
def test_news_coin_red_flags_match_their_inflections(title):
    assert ms.news_part(_titles(title), coin=True)[1] == title


def test_news_stock_phrases_need_a_leading_word_boundary():
    # «asphalts» contains «halts»; «reprobe» contains «probe»; «subfraud» is nothing
    assert ms.news_part(_titles("Asphalts prices rise", "Reprobe of the seabed")) == \
        (ms.Part(0, []), None)
    assert ms.news_part(_titles("Subfraud report")) == (ms.Part(0, []), None)


@pytest.mark.parametrize("title,points,red", [
    ("Fraudulent filings alleged", 0, True),          # red flag «fraud» + a suffix
    ("Regulator probes the maker", -10, False),       # «probe» + a suffix
    ("Analyst downgraded the stock", -10, False),
    ("Chapter 11 filing expected", 0, True),
    ("Lawsuits pile up", -10, False),
    ("Upgraded to buy", 5, False),
])
def test_news_stock_phrases_match_their_suffixes(title, points, red):
    part, flag = ms.news_part(_titles(title))
    assert part.points == points
    assert (flag == title) is red


@pytest.mark.parametrize("title", ["Bank rolls out anti-fraud platform", "ANTI-FRAUD tools boost Acme",
                                   "Acme buys an anti-fraud startup"])
def test_news_anti_fraud_is_not_a_fraud_red_flag(title):
    assert ms.news_part(_titles(title)) == (ms.Part(0, []), None)


@pytest.mark.parametrize("title", ["Fraud charges filed against Acme", "Acme accused of accounting fraud",
                                   "Anti-fraud unit uncovers fraud at Acme"])
def test_news_fraud_itself_is_still_a_red_flag(title):
    assert ms.news_part(_titles(title))[1] == title


def test_news_initial_public_offering_is_not_a_share_offering_red_flag():
    part, red = ms.news_part(_titles("Acme files for initial public offering"))
    assert red is None and part.points == 0
    part, red = ms.news_part(_titles("ACME PRICES INITIAL PUBLIC OFFERING"))
    assert red is None


def test_news_other_public_offerings_are_still_red_flags():
    for title in ("Acme launches $50M public offering", "Follow-on public offering priced",
                  "Acme prices secondary public offering"):
        assert ms.news_part(_titles(title))[1] == title


# ------------------------------------------------------------------ typical_move / stop
def test_typical_move_of_alternating_closes():
    closes = [100.0, 101.0] * 10 + [100.0]           # 21 closes -> 20 changes
    assert ms.typical_move(closes) == pytest.approx((0.01 + 1 / 101) / 2, rel=1e-9)
    assert ms.typical_move(closes) == pytest.approx(0.00995, abs=1e-5)


def test_typical_move_needs_21_closes():
    assert ms.typical_move([100.0, 101.0] * 10) is None    # 20 closes
    assert ms.typical_move([]) is None


def test_typical_move_uses_only_the_last_20_changes():
    closes = [1.0, 50.0] + _flat(21)                       # a spike far in the past
    assert ms.typical_move(closes) == pytest.approx(0.0)


def test_stop_distance_floors_a_calm_stock_and_coin():
    calm = _grow(30, 0.001)
    assert ms.stop_distance(calm, "stock") == 0.10
    assert ms.stop_distance(calm, "crypto") == 0.15


def test_stop_distance_caps_a_wild_stock_and_coin():
    wild = _grow(30, 0.20)
    assert ms.stop_distance(wild, "stock") == 0.25
    assert ms.stop_distance(wild, "crypto") == 0.35


def test_stop_distance_in_between_is_three_times_the_move():
    assert ms.stop_distance(_grow(30, 0.05), "stock") == pytest.approx(0.15)
    assert ms.stop_distance(_grow(30, 0.06), "crypto") == pytest.approx(0.18)


def test_stop_distance_needs_history():
    assert ms.stop_distance(_flat(20), "stock") is None


# ------------------------------------------------------------------ untradeable_reason
def test_untradeable_no_price_comes_first():
    sig = _cluster(3, size=(None, None))
    assert ms.untradeable_reason(sig, _flat(20)) == "нет цены"


def test_untradeable_unknown_size():
    assert ms.untradeable_reason(_cluster(3, size=(None, None)), _flat(30)) == "размер неизвестен"


def test_untradeable_size_attributes_missing_altogether():
    sig = _stake()
    del sig.market_cap_eur, sig.avg_daily_value
    assert ms.untradeable_reason(sig, _flat(30)) == "размер неизвестен"


def test_untradeable_small_company():
    assert ms.untradeable_reason(_cluster(3, size=(19.9e6, 1e6)), _flat(30)) == \
        "компания меньше €20 млн"
    assert ms.untradeable_reason(_cluster(3, size=(20e6, 1e6)), _flat(30)) is None


def test_untradeable_thin_trading():
    assert ms.untradeable_reason(_cluster(3, size=(1e9, 99_999)), _flat(30)) == \
        "торгуется меньше €100 тыс. в день"
    assert ms.untradeable_reason(_cluster(3, size=(1e9, 100_000)), _flat(30)) is None


def test_untradeable_one_known_size_is_enough():
    assert ms.untradeable_reason(_cluster(3, size=(1e9, None)), _flat(30)) is None
    assert ms.untradeable_reason(_cluster(3, size=(None, 1e6)), _flat(30)) is None


def test_untradeable_small_company_beats_thin_trading():
    assert ms.untradeable_reason(_cluster(3, size=(1e6, 1.0)), _flat(30)) == \
        "компания меньше €20 млн"


# ------------------------------------------------------------------ score_stock
def _buy_sig(**kw):
    """3 management + CEO + 0.06% + a 12% position increase: 42+10+6+3 = 61, which the
    insiders cap holds at 60 -- exactly the buy bar."""
    return _cluster(3, roles=["ceo"], pct=0.06, inc=12, **kw)


def test_score_stock_buy():
    s = ms.score_stock(_buy_sig(), _flat(), None)
    assert (s.insiders, s.triggers, s.momentum, s.news) == (60, 0, 0, 0)
    assert s.total == 60
    assert s.decision == ms.BUY
    assert s.block is None and s.untradeable is None
    assert s.stop_pct == 0.10
    assert s.last_close == 100.0
    assert (s.ticker, s.source, s.company, s.kind) == ("AAA", "SEC", "Acme Corp", "stock")


def test_score_stock_keeps_the_signal_and_lists_reasons_insiders_first():
    sig = _buy_sig(corroborated=["BAFIN"])
    s = ms.score_stock(sig, _grow(250, 0.005), _titles("upgrade"))
    assert s.signal is sig
    assert s.reasons[0] == "3 инсайдера из руководства"
    assert s.reasons.index("подтверждает: BAFIN") > s.reasons.index("0,06% компании")
    assert s.reasons[-1] == "новости: 1 хорошая"


def test_score_stock_red_flag_blocks_even_a_60_plus_score():
    title = "Acme prices $50M secondary offering"
    s = ms.score_stock(_buy_sig(), _flat(), _titles(title))
    assert s.decision == ms.BLOCK
    assert s.block == title
    assert s.total == 60


def test_score_stock_unknown_size_is_only_watch():
    s = ms.score_stock(_buy_sig(size=(None, None)), _flat(), None)
    assert s.decision == ms.WATCH
    assert s.untradeable == "размер неизвестен"
    assert s.block is None


def test_score_stock_no_price_is_only_watch():
    s = ms.score_stock(_buy_sig(), [], None)
    assert s.decision == ms.WATCH
    assert s.untradeable == "нет цены"
    assert s.stop_pct is None and s.last_close is None


def test_score_stock_two_directors_is_skip():
    s = ms.score_stock(_cluster(2), _flat(), None)
    assert s.total == 34
    assert s.decision == ms.SKIP


def test_score_stock_45_is_watch_and_44_is_skip():
    assert ms.score_stock(_cluster(1, roles=["ceo"], pct=0.05, inc=10, first=True),
                          _flat(), None).total == 45
    assert ms.score_stock(_cluster(1, roles=["ceo"], pct=0.05, inc=10, first=True),
                          _flat(), None).decision == ms.WATCH
    s = ms.score_stock(_cluster(2, pct=0.05, first=True), _flat(), None)   # 34+6+4
    assert s.total == 44 and s.decision == ms.SKIP


def test_score_stock_60_buys_and_59_only_watches():
    sixty = _cluster(2, roles=["ceo"], pct=0.2, inc=30)                    # 34+10+10+6
    s = ms.score_stock(sixty, _flat(), None)
    assert s.total == 60 and s.decision == ms.BUY
    fifty_nine = _cluster(4, roles=["ceo"], pct=0.01)                      # 46+10+3
    s = ms.score_stock(fifty_nine, _flat(), None)
    assert s.total == 59 and s.decision == ms.WATCH
    assert s.untradeable is None


def test_score_stock_total_is_clamped_at_100():
    sig = _cluster(5, roles=["ceo"], pct=0.3, inc=50, first=True, corroborated=["BAFIN"])
    s = ms.score_stock(sig, _grow(250, 0.005), _titles("upgrade", "buyback", "beats estimates"),
                       activist=True, politicians=True)
    assert (s.insiders, s.triggers, s.momentum, s.news) == (60, 20, 15, 10)
    assert s.total == 100


def test_score_stock_total_is_clamped_at_zero():
    s = ms.score_stock(_stake("SCHEDULE 13G", 5.0), _flat(),
                       _titles("downgrade", "lawsuit", "recall", "probe"))
    assert s.news == -30 and s.total == 0
    assert s.decision == ms.SKIP


def test_score_stock_passes_the_t212_label_through():
    assert ms.score_stock(_buy_sig(), _flat(), None, t212=False).t212 is False
    assert ms.score_stock(_buy_sig(), _flat(), None, t212=True).t212 is True
    assert ms.score_stock(_buy_sig(), _flat(), None).t212 is None


def test_score_stock_stake_uses_the_stake_conviction():
    s = ms.score_stock(_stake("SCHEDULE 13D", 8.0, prev=6.0), _flat(), None)
    assert s.insiders == 41
    assert s.reasons[0] == "активист 13D: 8,0%"


def test_score_stock_stake_ignores_its_own_activist_flags():
    s = ms.score_stock(_stake("SCHEDULE 13D", 8.0), _flat(), None,
                       activist=True, passive_big=True, politicians=False)
    assert s.triggers == 0
    s = ms.score_stock(_stake("SCHEDULE 13D", 8.0), _flat(), None, politicians=True)
    assert s.triggers == 5                                # politicians still count


def test_score_stock_politician_signal_gets_the_politicians_trigger():
    sig = _cluster(3, source="HOUSE", pct=1.0, first=True)
    s = ms.score_stock(sig, _flat(), None)                # politicians left False
    assert s.insiders == 0
    assert s.triggers == 5
    assert s.total == 5
    assert s.decision == ms.SKIP
    assert s.reasons == ["покупают политики"]


def test_score_stock_politicians_alone_never_buy():
    s = ms.score_stock(_cluster(3, source="SENATE"), _grow(250, 0.005), _titles("upgrade"))
    assert s.total == 5 + 15 + 5
    assert s.decision == ms.SKIP


def test_score_stock_stop_and_last_close():
    s = ms.score_stock(_buy_sig(), _grow(30, 0.05), None)
    assert s.stop_pct == pytest.approx(0.15)
    assert s.last_close == pytest.approx(100.0 * 1.05 ** 29)


# ------------------------------------------------------------------ coin_trend
def test_coin_trend_needs_121_closes():
    assert ms.coin_trend(_grow(120, 0.01)) is None
    assert ms.coin_trend(_grow(121, 0.01)) is not None


def test_coin_trend_rising_series():
    closes = _grow(150, 0.01)
    t = ms.coin_trend(closes)
    assert t["above_ma100"] is True
    assert t["ret20"] == pytest.approx(closes[-1] / closes[-21] - 1)
    assert t["ret60"] == pytest.approx(closes[-1] / closes[-61] - 1)
    assert t["ret120"] == pytest.approx(closes[-1] / closes[-121] - 1)
    assert t["up"] is True and t["down"] is False


def test_coin_trend_falling_series():
    t = ms.coin_trend(_grow(150, -0.01))
    assert t["above_ma100"] is False
    assert t["up"] is False and t["down"] is True


# above the 100-day mean (102.2) with the 20- and 60-day returns up, the 120-day down
_UP_TWO_OF_THREE = [300.0] * 21 + [100.0] * 79 + [110.0] * 20 + [120.0]
# above the mean (104) but only the 20-day return is up
_UP_ONE_OF_THREE = [300.0] + [100.0] * 59 + [300.0] + [100.0] * 40 + [110.0] * 20


def test_coin_trend_up_with_two_of_three_returns_positive():
    t = ms.coin_trend(_UP_TWO_OF_THREE)
    assert t["above_ma100"] is True
    assert (t["ret20"] > 0, t["ret60"] > 0, t["ret120"] > 0) == (True, True, False)
    assert t["up"] is True


def test_coin_trend_is_not_up_with_only_one_positive_return():
    t = ms.coin_trend(_UP_ONE_OF_THREE)
    assert t["above_ma100"] is True
    assert (t["ret20"] > 0, t["ret60"] > 0, t["ret120"] > 0) == (True, False, False)
    assert t["up"] is False and t["down"] is False


def test_coin_trend_down_needs_below_the_average_and_a_negative_20_day():
    # below the 100-day mean but the last 20 days are up: not "down"
    closes = [300.0] * 60 + [100.0] * 60 + [110.0]
    t = ms.coin_trend(closes)
    assert t["above_ma100"] is False and t["ret20"] > 0
    assert t["down"] is False


# ------------------------------------------------------------------ score_coin
def test_score_coin_rising_is_a_buy():
    c = ms.score_coin("BTC", _grow(150, 0.01), bullish_flow=False, caution=None, headlines=None)
    assert c.trend == 60 and c.flows == 0 and c.news == 0 and c.total == 60
    assert c.trend_up is True and c.trend_down is False
    assert c.decision == ms.BUY
    assert c.block is None and c.caution is None
    assert (c.coin, c.ticker, c.kind) == ("BTC", "CRYPTO:BTC", "crypto")
    assert c.stop_pct == 0.15                            # 1% moves: 3% -> floored
    assert c.last_close == pytest.approx(100.0 * 1.01 ** 149)


def test_score_coin_falling_is_watch():
    c = ms.score_coin("ETH", _grow(150, -0.01), bullish_flow=False, caution=None, headlines=None)
    assert c.trend == 0
    assert c.trend_down is True and c.trend_up is False
    assert c.decision == ms.WATCH


def test_score_coin_bullish_flow_adds_fifteen():
    c = ms.score_coin("BTC", _grow(150, 0.01), bullish_flow=True, caution=None, headlines=None)
    assert c.flows == 15 and c.total == 75


def test_score_coin_uptrend_under_the_bar_is_watch_until_a_flow_lifts_it():
    c = ms.score_coin("BTC", _UP_TWO_OF_THREE, bullish_flow=False, caution=None, headlines=None)
    assert c.trend == 45 and c.trend_up is True
    assert c.decision == ms.WATCH
    c = ms.score_coin("BTC", _UP_TWO_OF_THREE, bullish_flow=True, caution=None, headlines=None)
    assert c.total == 60 and c.decision == ms.BUY


def test_score_coin_high_score_without_an_uptrend_is_not_a_buy():
    # below the 100-day mean but all three returns positive: trend 45, flow 15 -> 60
    closes = [50.0] * 30 + [300.0] * 30 + [50.0] * 40 + [60.0] * 20 + [70.0]
    c = ms.score_coin("BTC", closes, bullish_flow=True, caution=None, headlines=None)
    assert c.trend == 45 and c.total == 60
    assert c.trend_up is False
    assert c.decision == ms.WATCH


def test_score_coin_caution_blocks_a_rising_coin():
    c = ms.score_coin("BTC", _grow(150, 0.01), bullish_flow=False,
                      caution="ETF: отток €300 млн", headlines=None)
    assert c.decision == ms.BLOCK
    assert c.caution == "ETF: отток €300 млн"
    assert c.flows == -20
    assert c.total == 40
    assert "осторожно: ETF: отток €300 млн" in c.reasons
    assert c.block


def test_score_coin_red_flag_blocks():
    title = "Exchange hack drains reserves"
    c = ms.score_coin("BTC", _grow(150, 0.01), bullish_flow=False, caution=None,
                      headlines=_titles(title))
    assert c.decision == ms.BLOCK
    assert c.block == title
    assert f"новости: {title}" in c.reasons


def test_score_coin_news_points_and_lines():
    c = ms.score_coin("BTC", _grow(150, 0.01), bullish_flow=False, caution=None,
                      headlines=_titles("upgrade", "record revenue"))
    assert c.news == 10 and c.total == 70
    assert "новости: 2 хорошие" in c.reasons


def test_score_coin_short_history_is_watch_with_a_reason():
    c = ms.score_coin("BTC", _grow(100, 0.01), bullish_flow=False, caution=None, headlines=None)
    assert c.decision == ms.WATCH
    assert c.reasons == ["мало истории"]
    assert c.trend == 0 and not c.trend_up and not c.trend_down
    assert c.stop_pct is not None                         # 100 closes are enough for a stop


def test_score_coin_short_history_keeps_flows_and_news():
    c = ms.score_coin("BTC", _grow(100, 0.01), bullish_flow=True, caution=None,
                      headlines=_titles("upgrade"))
    assert c.flows == 15 and c.news == 5 and c.total == 20
    assert c.decision == ms.WATCH
    assert c.reasons[0] == "мало истории"


def test_score_coin_total_is_clamped():
    c = ms.score_coin("BTC", _grow(150, -0.01), bullish_flow=False, caution="x",
                      headlines=_titles("downgrade", "lawsuit", "recall", "probe"))
    assert c.total == 0


def test_score_coin_no_closes():
    c = ms.score_coin("BTC", [], bullish_flow=False, caution=None, headlines=None)
    assert c.decision == ms.WATCH
    assert c.stop_pct is None and c.last_close is None


# ------------------------------------------------------------------ constants
def test_the_position_sizing_of_the_virtual_portfolio_is_gone():
    for gone in ("position_size", "RISK_PER_TRADE", "STOCK_CAP", "COIN_CAP", "MIN_ORDER_EUR"):
        assert not hasattr(ms, gone), gone


def test_constants_match_the_spec():
    assert (ms.STOCK_BUY, ms.STOCK_WATCH, ms.COIN_BUY) == (60.0, 45.0, 60.0)
    assert (ms.MIN_MCAP_EUR, ms.MIN_ADV_EUR, ms.MIN_CLOSES) == (20e6, 100_000, 21)
    assert ms.STOP_MULT == 3.0
    assert ms.STOP_MIN == {"stock": 0.10, "crypto": 0.15}
    assert ms.STOP_MAX == {"stock": 0.25, "crypto": 0.35}
    assert (ms.BUY, ms.WATCH, ms.SKIP, ms.BLOCK) == ("buy", "watch", "skip", "block")
