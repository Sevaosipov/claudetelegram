"""telegram_notify.py -- HTML escaping and the html/plain dual rendering of
signal messages. The per-filing line formatters (format_sec_line etc.) are
console-only in this project (bot.py prints them, nothing sends them to
Telegram) and are intentionally left as plain text -- untested here since
they render no markup."""
from __future__ import annotations

import re

import pytest

import cluster
from cluster import ClusterSignal, ExitSignal, StakeSignal

import positions
import t212_account as ta
import telegram_notify
import telegram_notify as tn


def _cluster(**overrides):
    base = dict(
        source="SEC", ticker="AAA", company="Acme & Co", buyer_count=2,
        total_value=1_000_000.0, members=["A (CEO) $500,000", "B (CFO) $500,000"],
        window_start="2026-08-01", window_end="2026-08-10",
    )
    base.update(overrides)
    return ClusterSignal(**base)


def _exit(**overrides):
    base = dict(
        source="SEC", ticker="AAA", company="Acme & Co", total_buyers=3,
        seller_count=2, lines=["A: bought $1 -> sold $2"],
    )
    base.update(overrides)
    return ExitSignal(**base)


def _stake(**overrides):
    base = dict(
        source="SEC13DG", ticker="AAA", company="Acme & Co", person="Big Fund <LP>",
        form_type="SCHEDULE 13D", percent=12.5, prev_percent=None,
        amount_owned=1_000_000, event_date="2026-08-01",
        url="https://sec.gov/x?a=1&b=2",
    )
    base.update(overrides)
    return StakeSignal(**base)


def test_esc_escapes_the_three_html_special_characters():
    assert tn._esc("A & B <C> D") == "A &amp; B &lt;C&gt; D"


def test_esc_handles_non_string_input():
    assert tn._esc(42) == "42"


def test_b_wraps_and_escapes_only_in_html_mode():
    assert tn._b("A & B", html=True) == "<b>A &amp; B</b>"
    assert tn._b("A & B", html=False) == "A & B"


def test_format_signal_plain_mode_is_unchanged_by_default():
    """html defaults to False -- every existing console call site (bot.py,
    menu.py) must keep getting exactly the old plain text, tags-free."""
    text = tn.format_signal(_cluster())
    assert "<b>" not in text and "<" not in text
    assert "СИГНАЛ: AAA" in text
    assert "Acme & Co" in text  # unescaped in plain mode


def test_format_signal_html_mode_bolds_the_headline_and_escapes_ampersands():
    text = tn.format_signal(_cluster(), html=True)
    assert text.startswith("🟢 <b>СИГНАЛ: AAA")
    assert "</b>" in text
    assert "Acme &amp; Co" in text
    assert "Acme & Co" not in text  # the raw '&' must not survive unescaped


def test_format_signal_html_mode_escapes_the_holder_only_tag():
    """The literal ' >10%' tag contains a bare '>' that HTML requires escaped."""
    text = tn.format_signal(_cluster(holder_only=True), html=True)
    assert "&gt;10%" in text
    assert " >10%" not in text


def test_format_exit_signal_html_mode():
    text = tn.format_exit_signal(_exit(), html=True)  # source="SEC" -> 🟢, per _SOURCE_ICON
    assert text.startswith("🟢 <b>ВЫХОД: AAA")
    assert "Acme &amp; Co" in text


def test_format_exit_signal_plain_mode_unchanged():
    text = tn.format_exit_signal(_exit())
    assert "<" not in text
    assert "ВЫХОД: AAA" in text


def test_format_stake_signal_html_mode_links_the_source_and_escapes_person_name():
    text = tn.format_stake_signal(_stake(), html=True)
    assert "<b>" in text and "</b>" in text
    assert "Big Fund &lt;LP&gt;" in text
    assert '<a href="https://sec.gov/x?a=1&amp;b=2">источник</a>' in text


def test_format_stake_signal_plain_mode_keeps_a_bare_url_line():
    """Plain mode never escapes -- unlike html mode, which must. The person name
    fixture deliberately contains a raw '<' to prove that point elsewhere; here a
    plain name keeps the test focused on the URL line staying a bare, unlinked URL."""
    text = tn.format_stake_signal(_stake(person="Big Fund"))
    assert "<a href" not in text
    assert "https://sec.gov/x?a=1&b=2" in text.splitlines()[-1]


def test_format_stake_signal_shows_co_filer_count():
    sig = _stake(co_filer_names=["Fund GP LLC", "Individual Manager"])
    text = tn.format_stake_signal(sig)
    assert "(+2 содокладчика)" in text


def test_format_stake_signal_singular_co_filer():
    sig = _stake(co_filer_names=["Fund GP LLC"])
    text = tn.format_stake_signal(sig)
    assert "(+1 содокладчик)" in text and "содокладчика" not in text


def test_format_stake_signal_omits_co_filer_tag_when_solo():
    text = tn.format_stake_signal(_stake())
    assert "содокладчик" not in text


def test_format_any_signal_dispatches_html_flag_to_the_right_formatter():
    assert tn.format_any_signal(_cluster(), html=True) == tn.format_signal(_cluster(), html=True)
    assert tn.format_any_signal(_exit(), html=True) == tn.format_exit_signal(_exit(), html=True)
    assert tn.format_any_signal(_stake(), html=True) == tn.format_stake_signal(_stake(), html=True)


def test_format_signals_digest_renders_every_signal_in_html_and_bolds_the_header():
    text = tn.format_signals_digest([_cluster(), _exit()])
    assert text.startswith("🎯 <b>Новые сигналы:")
    assert "</b>" in text
    assert "<b>СИГНАЛ: AAA" in text
    assert "<b>ВЫХОД: AAA" in text


def test_send_text_uses_html_parse_mode(monkeypatch):
    captured = {}

    class FakeResp:
        def raise_for_status(self):
            pass

    def fake_post(url, data, timeout):
        captured.update(data)
        return FakeResp()

    monkeypatch.setattr(tn.requests, "post", fake_post)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    assert tn.send_text("hello") is True
    assert captured["parse_mode"] == "HTML"


def test_send_text_parts_counts_the_chunks_telegram_took(monkeypatch):
    """(accepted, sent) -- so a caller can tell nothing went out from part of it went out;
    send_text stays a plain bool: True only when every chunk went."""
    class Resp:
        def __init__(self, ok):
            self.ok = ok

        def raise_for_status(self):
            if not self.ok:
                raise tn.requests.HTTPError("400 Client Error: Bad Request")

    outcomes = []
    monkeypatch.setattr(tn.requests, "post", lambda url, data, timeout: Resp(outcomes.pop(0)))
    monkeypatch.setattr(tn, "_chunk", lambda text, size: ["one", "two", "three"])
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    outcomes[:] = [True, False, True]
    assert tn.send_text_parts("x") == (2, 3)
    outcomes[:] = [False, False, False]
    assert tn.send_text_parts("x") == (0, 3)
    outcomes[:] = [True, False, True]
    assert tn.send_text("x") is False
    outcomes[:] = [True, True, True]
    assert tn.send_text("x") is True
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    assert tn.send_text_parts("x") == (0, 0) and tn.send_text("x") is False


def test_send_text_still_skips_silently_without_credentials(monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert tn.send_text("hello") is False


# ------------------------------------------------------ corroborated_by
#
# cluster.find_corroboration() sets `.corroborated_by` on a signal after the
# fact (empty list when nothing else has fired on that ticker); the formatters
# must render it plainly -- it is the bot's own data rather than a third
# party's read -- and simply omit the line when the list is empty.

def test_format_signal_renders_corroborated_by():
    sig = _cluster()
    sig.corroborated_by = ["SENATE", "BAFIN"]
    text = tn.format_signal(sig, html=True)
    assert "SENATE, BAFIN" in text
    assert "<i>SENATE" not in text


def test_format_signal_omits_corroboration_line_when_empty():
    text = tn.format_signal(_cluster(), html=True)
    assert "🔗" not in text


def test_format_exit_signal_renders_corroborated_by():
    sig = _exit()
    sig.corroborated_by = ["SEC"]
    text = tn.format_exit_signal(sig, html=True)
    assert "🔗" in text and "SEC" in text


def test_format_stake_signal_renders_corroborated_by():
    sig = _stake()
    sig.corroborated_by = ["SEC"]
    text = tn.format_stake_signal(sig, html=True)
    assert "🔗" in text and "SEC" in text


def test_corroboration_line_never_claims_agreement():
    sig = _cluster()
    sig.corroborated_by = ["SENATE"]
    text = tn.format_signal(sig, html=True).lower()
    for bad in ("подтверждают", "согласны", "confirms", "agrees"):
        assert bad not in text


def test_format_ticker_backtest_shows_numbers_without_a_small_n_flag():
    """The n<30 "too few to mean anything" flag was removed at the user's
    explicit request (after asking what it meant) -- the raw purchase count
    is still shown ("Покупок в базе: N"), so the reader can judge for
    themselves, but there's no automatic caveat line attached anymore,
    meaningful or not."""
    small = {"ticker": "AAA", "n_purchases": 2,
             "by_horizon": {21: {"n": 2, "median_return": 3.0, "median_excess": 1.5,
                                  "hit_rate": 100.0, "p": 0.5, "meaningful": False}}}
    large = {"ticker": "AAA", "n_purchases": 40,
             "by_horizon": {21: {"n": 40, "median_return": 3.0, "median_excess": 1.5,
                                  "hit_rate": 60.0, "p": 0.03, "meaningful": True}}}
    for result in (small, large):
        text = tn.format_ticker_backtest(result)
        assert "AAA" in text
        assert "меньше 30" not in text and "⚠" not in text


def test_format_ticker_backtest_has_no_unescaped_angle_brackets():
    """send_text() always uses parse_mode HTML -- a stray '<' or '>' outside a
    real tag breaks Telegram's parser with a 400, as happened here once
    before. Guard against it regressing: strip the one legitimate <b>...</b>
    pair and check nothing else remains."""
    result = {"ticker": "AAA", "n_purchases": 2,
              "by_horizon": {21: {"n": 2, "median_return": 3.0, "median_excess": 1.5,
                                   "hit_rate": 100.0, "p": 0.5, "meaningful": False}}}
    text = tn.format_ticker_backtest(result)
    stripped = text.replace("<b>", "").replace("</b>", "")
    assert "<" not in stripped and ">" not in stripped


def test_format_ticker_backtest_empty_when_no_price_history():
    result = {"ticker": "ZZZZ", "n_purchases": 0, "by_horizon": {}}
    text = tn.format_ticker_backtest(result)
    assert "Недостаточно" in text


# ------------------------------------------------ format_condensed (stock path)
def _stock_rep(**overrides):
    import assets
    rep = {"ticker": "BTC", "asset": assets.stock_asset("BTC"), "kind": "stock",
           "opinion": None, "prices": {"current": 42.0}, "analyst": None,
           "insiders": {"buys": [], "sells": []}, "stakes": [], "political": [],
           "tradingview": None, "outlook": {"status": "no_table"}, "sources": {}}
    rep.update(overrides)
    return rep


def test_condensed_stock_reply_shows_the_outlook():
    text = tn.format_condensed(_stock_rep())
    assert "📈 Прогноз на месяц: ещё не готов" in text


def test_condensed_stock_reply_points_to_the_stock_report_not_the_coin():
    """"$BTC" is the Grayscale ETF; a bare "BTC" in the hint would open the coin."""
    assert tn.format_condensed(_stock_rep()).splitlines()[-1] == \
        "Полный отчёт: python research.py '$BTC'"
    rep = _stock_rep(ticker="AAPL")
    del rep["asset"]
    assert tn.format_condensed(rep).splitlines()[-1] == "Полный отчёт: python research.py 'AAPL'"


def test_condensed_stock_reply_names_fallback_sources():
    text = tn.format_condensed(_stock_rep(sources={"prices": "TradingView", "news": "Yahoo"}))
    assert "Источники: цены: TradingView (Yahoo недоступен)" in text
    assert "Источники" not in tn.format_condensed(_stock_rep())


# --------------------------------------------------------- caution signals
#
# A bearish coin signal is journaled as `caution` and reads as an outflow when shown (the
# menu's Сигналы); it is never in Telegram: the weekly signals (paper_report.week_signals) and
# the daily close alerts (bot._send_closes) do not carry it.

def _caution_signal():
    return cluster.CryptoSignal("CRYPTO_ETF", "etf_flow", "CRYPTO:BTC", "спот-ETF США, фондов: 12",
                                False, None, 9e8, "2026-09-24", "2026-09-26",
                                ["3 дн. подряд оттока, всего $1,050 млн"], None, ["k"])


def test_a_bearish_coin_signal_reads_as_an_outflow():
    text = telegram_notify.format_any_signal(_caution_signal(), html=False)
    assert "ОТТОК ИЗ СПОТ-ETF" in text and "3 дн. подряд оттока" in text


# ------------------------------------------------------------------ the signal line
_TAGS = re.compile(r"</?[a-zA-Z][^>]*>")


def test_the_signal_line_is_a_dot_a_bold_name_with_a_bang_the_event_and_the_details():
    assert tn.signal_line("🟢", "GME", "покупка", "балл 70, стоп −10%") == \
        "🟢 <b>GME!</b>: покупка — балл 70, стоп −10%"
    assert tn.signal_line("🟢", "GME", "покупка") == "🟢 <b>GME!</b>: покупка"           # no details, no dash
    assert tn.signal_line("🟢", "GME", "покупка", "") == "🟢 <b>GME!</b>: покупка"


@pytest.mark.parametrize("dot", ["🟢", "🔴", "⚪"])
def test_the_signal_line_takes_its_dot_as_given(dot):
    assert tn.signal_line(dot, "X", "e", "d").startswith(f"{dot} <b>X!</b>: e — d")


def test_the_dots_are_green_for_a_buy_or_a_gain_red_for_a_loss_and_white_for_a_neutral_event():
    assert (tn.DOT_GREEN, tn.DOT_RED, tn.DOT_NEUTRAL) == ("🟢", "🔴", "⚪")


def test_a_result_is_bold_after_the_details_and_the_extra_follows_it():
    assert tn.signal_line("🔴", "GME", "сработал стоп", "пора продавать", result="−€24,10", extra=" (−10,4%)") == \
        "🔴 <b>GME!</b>: сработал стоп — пора продавать, итог <b>−€24,10</b> (−10,4%)"
    assert tn.signal_line("🔴", "GME", "e", "d", result="−9,8%") == "🔴 <b>GME!</b>: e — d, итог <b>−9,8%</b>"
    assert tn.signal_line("🔴", "GME", "e", result="−9,8%") == "🔴 <b>GME!</b>: e, итог <b>−9,8%</b>"
    assert tn.signal_line("🔴", "GME", "e", "d", extra=" (x)") == "🔴 <b>GME!</b>: e — d"      # no result: no extra


def test_the_result_can_carry_another_label():
    assert tn.signal_line("⚪", "GME", "продано", "слежение закрыто", result="+4,1%", label="итог ≈") == \
        "⚪ <b>GME!</b>: продано — слежение закрыто, итог ≈ <b>+4,1%</b>"
    assert tn.signal_line("🔴", "GME", "e", "продажа по ближайшему закрытию", result="−9,8%", label="сейчас") == \
        "🔴 <b>GME!</b>: e — продажа по ближайшему закрытию, сейчас <b>−9,8%</b>"


def test_every_dynamic_part_of_the_line_is_escaped():
    text = tn.signal_line("🔴", "A&B<i>", "ev<b>&", "d&<x>", result="1<2", extra=" (&)", label="ит<ог")
    assert text == ("🔴 <b>A&amp;B&lt;i&gt;!</b>: ev&lt;b&gt;&amp; — d&amp;&lt;x&gt;, "
                    "ит&lt;ог <b>1&lt;2</b> (&amp;)")
    assert set(_TAGS.findall(text)) == {"<b>", "</b>"}


def test_without_html_the_line_has_no_tags_and_nothing_is_escaped():
    text = tn.signal_line("🔴", "A&B", "e<", "d>", result="1<2", extra=" (&)", html=False)
    assert text == "🔴 A&B!: e< — d>, итог 1<2 (&)"
    assert not _TAGS.search(tn.signal_line("🟢", "GME", "покупка", "балл 70", html=False))


@pytest.mark.parametrize("x, currency, text", [
    (8.3, "EUR", "+€8,30"), (-24.1, "EUR", "−€24,10"), (-26.4, "USD", "−$26,40"), (9.5, "GBP", "+£9,50"),
    (8.3, None, "+€8,30"), (-8.3, "CHF", "−8,30 CHF"), (8.3, "SEK", "+8,30 SEK"),
    (1234.5, "EUR", "+€1 234,50"), (0.004, "EUR", "+€0,00"), (-0.004, "EUR", "+€0,00")])
def test_a_profit_or_loss_to_the_cent_has_its_sign_before_the_currency(x, currency, text):
    assert tn.money_cents(x, currency) == text


def test_a_position_result_is_the_money_first_when_the_quantity_is_known():
    assert tn.position_result(23.10, 20.70, 10.0, "USD") == ("−$24,00", " (−10,4%)", False)
    assert tn.position_result(23.10, 24.05, 10.0, "EUR") == ("+€9,50", " (+4,1%)", True)
    assert tn.position_result(23.10, 20.70, None, "USD") == ("−10,4%", None, False)    # no quantity: the percent
    assert tn.position_result(23.10, 23.10, None) == ("+0,0%", None, True)             # break-even is not a loss
    assert tn.position_result(23.10, 23.099, 10.0, "EUR")[::2] == ("−€0,01", False)    # a cent lost is a loss
    assert tn.position_result(23.10, 23.0999, 10.0, "EUR")[::2] == ("+€0,00", True)    # less than a cent is none
    assert tn.position_result(23.10, 23.0999, None)[::2] == ("+0,0%", True)            # «+0,0%» is not a loss


# -------------------------------------------------------------------- close alerts
def _pos(**overrides):
    base = dict(id=1, ticker="AAA", source="SEC", opened_at="2026-09-01", entry_price=100.0, insiders=[],
                signal_id=None, closed_at=None, close_reason=None, close_alerted_at=None)
    base.update(overrides)
    return positions.Position(**base)


def _holding_pos(**overrides):
    """A Trading 212 holding: GME, 10 shares in USD, bought at 23,10."""
    base = dict(ticker="GME", source=None, entry_price=23.10, origin="t212", quantity=10.0,
                t212_ticker="GME_US_EQ", currency="USD", stop_pct=0.10)
    base.update(overrides)
    return _pos(**base)


@pytest.mark.parametrize("trigger, event", [
    ("trailing_stop", "сработал стоп"), ("insider_sell", "продаёт инсайдер"), ("caution", "отток по монете"),
    ("trend_down", "тренд развернулся вниз"), ("dead_money", "стоит на месте"), ("time", "год в позиции"),
    ("news", "плохие новости"), ("activist_cut", "активист сократил долю")])
def test_every_close_trigger_has_its_own_event(trigger, event):
    text = tn.format_close_alert(positions.CloseAlert(_pos(), trigger, "detail", 90.0), html=False)
    assert text.splitlines()[0] == f"🔴 AAA!: {event} — пора продавать: вход 100,00 → сейчас 90,00, итог −10,0%"


# The second line is the alert's own detail, and only three triggers have one worth the room: who sold
# and when, what the outflow was, which headline. For the rest the main line says it all.
_STOP_TEXT = "−10% от максимума 25.80"


@pytest.mark.parametrize("trigger", ["trailing_stop", "time", "dead_money", "trend_down", "activist_cut"])
def test_a_trigger_whose_main_line_says_it_all_is_one_line_whatever_detail_the_alert_has(trigger):
    detail = f"{_STOP_TEXT}; 365 дн. в позиции; доля 9,0% → 6,0%"
    for html in (True, False):
        text = tn.format_close_alert(positions.CloseAlert(_pos(), trigger, detail, 90.0), html=html)
        assert "\n" not in text
        assert not any(gone in text for gone in ("максимума", "25", "365", "доля", "9,0"))


@pytest.mark.parametrize("trigger, detail, shown", [
    ("insider_sell", "Ryan Cohen — Form 4, 2026-10-01", "Ryan Cohen — Form 4, 01.10"),
    ("caution", "отток из спот-ETF (€900 млн); цена подтверждает: -6.2% за 7 дн., ниже 20-дн. средней",
     "отток из спот-ETF (€900 млн); цена подтверждает: −6,2% за 7 дн., ниже 20-дн. средней"),
    ("news", "новости: SEC investigation into the CFO", "новости: SEC investigation into the CFO")])
def test_an_insider_sale_a_caution_and_bad_news_carry_one_detail_line_indented_by_three_spaces(
        trigger, detail, shown):
    text = tn.format_close_alert(positions.CloseAlert(_pos(), trigger, detail, 90.0), html=False)
    first, second = text.split("\n")                                       # two lines, no more
    assert first.startswith("🔴 AAA!: ") and second == f"   {shown}"


@pytest.mark.parametrize("trigger", ["insider_sell", "caution", "news"])
def test_a_detail_that_is_empty_is_no_second_line(trigger):
    for detail in ("", None):
        assert "\n" not in tn.format_close_alert(positions.CloseAlert(_pos(), trigger, detail, 90.0))


@pytest.mark.parametrize("label", ["Form 4", "Form 144", "Oslo", "FI", "BaFin"])
def test_an_insider_sale_says_who_and_when_with_the_date_as_dd_mm(label):
    detail = f"Ann Lee — {label}, 2026-09-30"
    text = tn.format_close_alert(positions.CloseAlert(_pos(), "insider_sell", detail, 90.0), html=False)
    assert text.splitlines()[1] == f"   Ann Lee — {label}, 30.09"


@pytest.mark.parametrize("odd", ["Ann Lee — Form 144, 09/30/2026", "Ann Lee — Oslo, 30.09.2026",
                                 "Ann Lee — Form 4, 2026-13-45", "Ann Lee — Form 4, 2026-9-3",
                                 "Ann Lee — Form 4, 12026-09-301"])
def test_a_date_that_is_not_an_iso_date_is_left_as_it_is(odd):
    text = tn.format_close_alert(positions.CloseAlert(_pos(), "insider_sell", odd, 90.0), html=False)
    assert text.splitlines()[1] == f"   {odd}"


@pytest.mark.parametrize("text, russian", [
    ("-6.2% за 7 дн.", "−6,2% за 7 дн."), ("+3.4%", "+3,4%"), ("(−6.2%)", "(−6,2%)"),
    ("стоп 25.80", "стоп 25,80"), ("0.5", "0,5"), ("12", "12"),
    ("(€1,050 млн)", "(€1 050 млн)"), ("€900,000,000", "€900 000 000"), ("1,050.5", "1 050,5"),
    ("(€900 млн)", "(€900 млн)"), ("9,0% → 6,0%", "9,0% → 6,0%"), ("9,05", "9,05"),     # Russian already
    ("ниже 20-дн. средней", "ниже 20-дн. средней"), ("Form 144", "Form 144"), ("1.2.3", "1.2.3"),
    ("x-6.2", "x-6,2"),                                                              # a hyphen is not a minus
    ("Ryan Cohen — Form 4, 2026-10-01", "Ryan Cohen — Form 4, 01.10"),              # a date is not a decimal
    ("с 2026-10-01 по 2026-10-05: -6.2%", "с 01.10 по 05.10: −6,2%")])
def test_the_detail_line_is_in_the_russian_format(text, russian):
    assert tn.ru_text(text) == russian


def test_the_numbers_of_a_caution_are_russian_at_the_message_level_only():
    detail = "монеты заводят на биржи (€1,050 млн); цена подтверждает: -6.2% за 7 дн., ниже 20-дн. средней"
    alert = positions.CloseAlert(_pos(ticker="CRYPTO:BTC", source="CRYPTO"), "caution", detail, 90.0)
    assert tn.format_close_alert(alert, html=False).splitlines()[1] == (
        "   монеты заводят на биржи (€1 050 млн); цена подтверждает: −6,2% за 7 дн., ниже 20-дн. средней")
    assert alert.detail == detail                                           # the alert itself is untouched


def test_a_news_headline_is_quoted_as_it_is():
    """A headline is somebody else's text: its numbers and dates are not ours to reformat."""
    headline = "новости: Probe of $2.5 million, 1,050 filings and 2026-10-01 hearing"
    alert = positions.CloseAlert(_pos(), "news", headline, 90.0)
    assert tn.format_close_alert(alert, html=False).splitlines()[1] == f"   {headline}"


def test_the_insider_alert_is_one_line_with_who_and_when_indented_under_it():
    alert = positions.CloseAlert(_pos(), "insider_sell", "Ann Lee — Form 4, 2026-09-30", 90.0)
    text = tn.format_close_alert(alert)
    assert text == ("🔴 <b>AAA!</b>: продаёт инсайдер — пора продавать: вход 100,00 → сейчас 90,00, "
                    "итог <b>−10,0%</b>\n   Ann Lee — Form 4, 30.09")
    assert "🚪" not in text and "Ваши позиции" not in text                  # the old layout is gone
    assert tn.format_close_alert(positions.CloseAlert(_pos(), "time", "", 90.0)).count("\n") == 0


def test_a_holding_with_a_quantity_gets_the_money_result_first_in_its_own_currency():
    text = tn.format_close_alert(positions.CloseAlert(_holding_pos(), "trailing_stop",
                                                      "−10% от максимума 25.80", 20.70))
    assert text == ("🔴 <b>GME!</b>: сработал стоп — пора продавать: вход 23,10 → сейчас 20,70, "
                    "итог <b>−$24,00</b> (−10,4%)")                         # the stop level is not repeated
    eur = _holding_pos(ticker="DE0007164600", source="T212", t212_ticker="SAPd_EQ", currency="EUR",
                       entry_price=120.0, quantity=5.0)
    assert tn.format_close_alert(positions.CloseAlert(eur, "time", "d", 108.0), html=False).splitlines()[0] == (
        "🔴 SAP!: год в позиции — пора продавать: вход 120,00 → сейчас 108,00, итог −€60,00 (−10,0%)")


def test_a_position_without_a_quantity_gets_the_percent_alone():
    text = tn.format_close_alert(positions.CloseAlert(_pos(), "trailing_stop", "d", 84.0), html=False)
    assert text.splitlines()[0].endswith("итог −16,0%")


def test_a_gain_is_a_green_dot_and_a_loss_a_red_one():
    gain = tn.format_close_alert(positions.CloseAlert(_pos(), "time", "d", 104.1), html=False)
    assert gain.splitlines()[0] == "🟢 AAA!: год в позиции — пора продавать: вход 100,00 → сейчас 104,10, итог +4,1%"
    even = tn.format_close_alert(positions.CloseAlert(_pos(), "time", "d", 100.0), html=False)
    assert even.startswith("🟢") and even.splitlines()[0].endswith("итог +0,0%")
    held = tn.format_close_alert(positions.CloseAlert(_holding_pos(), "time", "d", 24.05), html=False)
    assert held.splitlines()[0].startswith("🟢 GME!") and held.splitlines()[0].endswith("итог +$9,50 (+4,1%)")


def test_an_alert_without_a_price_gives_the_entry_alone_and_a_red_dot():
    text = tn.format_close_alert(positions.CloseAlert(_pos(), "time", "400 дн. в позиции", None))
    assert text == "🔴 <b>AAA!</b>: год в позиции — пора продавать: вход 100,00"
    news = tn.format_close_alert(positions.CloseAlert(_pos(), "news", "новости: fraud", None), html=False)
    assert news == "🔴 AAA!: плохие новости — пора продавать: вход 100,00\n   новости: fraud"
    assert "итог" not in tn.format_close_alert(positions.CloseAlert(_holding_pos(), "time", "d", None))


def test_a_holding_in_a_currency_with_no_sign_of_its_own_is_shown_with_its_code():
    chf = _holding_pos(ticker="NESN", currency="CHF", t212_ticker="NESNz_EQ")
    text = tn.format_close_alert(positions.CloseAlert(chf, "time", "d", 20.70), html=False)
    assert "итог −24,00 CHF (−10,4%)" in text


def test_caution_close_alert_reads_as_such_and_names_the_coin_by_its_symbol():
    pos = _pos(ticker="CRYPTO:BTC", source="CRYPTO", opened_at="2026-09-20", entry_price=84_500.0)
    alert = positions.CloseAlert(pos, "caution", "отток из спот-ETF (€900 млн); цена "
                                 "подтверждает: -6.2% за 7 дн., ниже 20-дн. средней", 79_900.0)
    first, second = tn.format_close_alert(alert, html=False).splitlines()
    assert first == "🔴 BTC!: отток по монете — пора продавать: вход 84 500,00 → сейчас 79 900,00, итог −5,4%"
    assert second == "   отток из спот-ETF (€900 млн); цена подтверждает: −6,2% за 7 дн., ниже 20-дн. средней"


def test_a_close_alert_names_a_trading_212_holding_by_its_symbol_not_its_isin():
    sap = _holding_pos(ticker="DE0007164600", source="T212", t212_ticker="SAPd_EQ", currency="EUR",
                       entry_price=100.0, quantity=5.0)
    text = tn.format_close_alert(positions.CloseAlert(sap, "trailing_stop", "detail", 90.0))
    assert text.startswith("🔴 <b>SAP!</b>: сработал стоп") and "DE0007164600" not in text
    us = _holding_pos(source=None, currency="USD", entry_price=100.0, quantity=5.0)
    assert tn.format_close_alert(positions.CloseAlert(us, "time", "d", None), html=False) \
        .startswith("🔴 GME!: год в позиции")


def test_a_close_alert_escapes_what_it_shows_and_uses_only_bold():
    pos = _pos(ticker="A&B<i>")
    text = tn.format_close_alert(positions.CloseAlert(pos, "news", "новости: <b>fraud</b> & co", 90.0))
    assert text.startswith("🔴 <b>A&amp;B&lt;i&gt;!</b>: плохие новости")
    assert text.endswith("\n   новости: &lt;b&gt;fraud&lt;/b&gt; &amp; co")
    assert set(_TAGS.findall(text)) == {"<b>", "</b>"}
    plain = tn.format_close_alert(positions.CloseAlert(pos, "news", "x & y", 90.0), html=False)
    assert plain == ("🔴 A&B<i>!: плохие новости — пора продавать: вход 100,00 → сейчас 90,00, итог −10,0%\n"
                     "   x & y")                                               # nothing escaped, no tags added
    sale = tn.format_close_alert(positions.CloseAlert(pos, "insider_sell", "A<i> & Co — Form 4, 2026-10-01", 90.0))
    assert sale.endswith("\n   A&lt;i&gt; &amp; Co — Form 4, 01.10")
    assert set(_TAGS.findall(sale)) == {"<b>", "</b>"}


def test_a_trigger_nobody_named_is_shown_as_it_is():
    text = tn.format_close_alert(positions.CloseAlert(_pos(), "новый", "d", 90.0), html=False)
    assert text.startswith("🔴 AAA!: новый — пора продавать")


# ---------------------------------------------------------------- send_text never shows the token
TOKEN = "123456789:AAH-fake_TOKEN-value_xyz"


def _send_with(monkeypatch, capsys, error):
    """send_text with a fake token and a requests.post that fails with `error`; returns
    (what it returned, everything it printed)."""
    import requests
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")

    def post(url, **kwargs):
        assert TOKEN in url                     # the real call does carry it in the URL
        raise error

    monkeypatch.setattr(requests, "post", post)
    result = telegram_notify.send_text("привет")
    captured = capsys.readouterr()
    return result, captured.out + captured.err


def test_send_text_prints_no_token_when_the_connection_fails(monkeypatch, capsys):
    import requests
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    error = requests.ConnectionError(
        f"HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries exceeded with url: "
        f"/bot{TOKEN}/sendMessage (Caused by NameResolutionError)")
    result, out = _send_with(monkeypatch, capsys, error)
    assert result is False
    assert TOKEN not in out and "AAH-fake" not in out and "123456789" not in out
    assert "[telegram] send failed" in out and "bot<token>/sendMessage" in out
    assert url not in out


def test_send_text_prints_no_token_when_telegram_answers_with_an_error(monkeypatch, capsys):
    import requests
    error = requests.HTTPError(
        f"400 Client Error: Bad Request for url: https://api.telegram.org/bot{TOKEN}/sendMessage")
    result, out = _send_with(monkeypatch, capsys, error)
    assert result is False
    assert TOKEN not in out and "bot<token>/sendMessage" in out


def test_send_text_redacts_a_token_the_url_pattern_would_not_know(monkeypatch, capsys):
    import requests
    odd = "not-a-usual-token"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", odd)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(requests, "post", lambda url, **k: (_ for _ in ()).throw(
        requests.ConnectionError(f"failed: {url}")))
    assert telegram_notify.send_text("x") is False
    out = capsys.readouterr().out
    assert odd not in out and "<token>" in out


def test_redact_leaves_other_text_alone():
    assert telegram_notify._redact("timeout after 15 s", TOKEN) == "timeout after 15 s"
    assert telegram_notify._redact("timeout", None) == "timeout"
    assert telegram_notify._redact(f"a {TOKEN} b", TOKEN) == "a <token> b"


# ------------------------------------------------------------ format_my_portfolio (/portfolio)
def _held(ticker="GME", *, entry=23.10, opened="2026-10-01", insiders=(), stop_pct=0.10, pid=1,
          source=None):
    return positions.Position(pid, ticker, source, opened, entry, list(insiders), None, None, None,
                              None, stop_pct)


def _status(last=24.05, *, entry=23.10, days=3, peak=24.05, stop_pct=0.10, stop_level=21.65,
            to_stop=0.10):
    """A position_status() dict as positions.py builds it (the numbers are handed in whole)."""
    return {"last": last, "result": None if last is None else last / entry - 1, "days": days,
            "peak": peak, "stop_pct": stop_pct, "stop_level": stop_level,
            "to_stop": None if last is None else to_stop}


_SELL_LINE = "Сигнал на продажу придёт сразу. /sold TICKER — закрыть, /model — модельный портфель."


def test_my_portfolio_shows_a_position_in_full():
    rows = [(_held(insiders=["Ryan Cohen", "Alice Smith"]), _status(), True)]
    assert tn.format_my_portfolio(rows) == "\n\n".join([
        "<b>💼 Ваш портфель — 1 позиция</b>",
        "\n".join(["• GME: вход 23,10 (01.10), сейчас 24,05 (+4,1%), 3 дн.",
                   "   стоп 21,65 (−10% от максимума 24,05), до стопа 10,0%",
                   "   слежу за продажами: Ryan Cohen, Alice Smith",
                   "   модель тоже держит"]),
        "Средний результат: +4,1% по 1 позиции\n" + _SELL_LINE])


@pytest.mark.parametrize("n, word", [(1, "позиция"), (2, "позиции"), (4, "позиции"), (5, "позиций"),
                                     (11, "позиций"), (21, "позиция"), (22, "позиции")])
def test_my_portfolio_header_counts_the_positions_in_russian(n, word):
    rows = [(_held(f"T{i}", pid=i), _status(), False) for i in range(n)]
    assert tn.format_my_portfolio(rows).startswith(f"<b>💼 Ваш портфель — {n} {word}</b>\n\n")


@pytest.mark.parametrize("n, word", [(1, "позиции"), (2, "позициям"), (5, "позициям"),
                                     (11, "позициям"), (21, "позиции")])
def test_the_average_line_counts_the_positions_in_russian(n, word):
    rows = [(_held(f"T{i}", pid=i), _status(), False) for i in range(n)]
    assert f"Средний результат: +4,1% по {n} {word}\n" + _SELL_LINE in tn.format_my_portfolio(rows)


def test_the_insiders_line_is_only_there_when_there_are_insiders():
    with_ = tn.format_my_portfolio([(_held(insiders=["Ryan Cohen"]), _status(), False)])
    without = tn.format_my_portfolio([(_held(), _status(), False)])
    assert "\n   слежу за продажами: Ryan Cohen" in with_
    assert "слежу" not in without


def test_the_model_line_is_only_there_when_the_model_holds_it_too():
    text = tn.format_my_portfolio([(_held("GME"), _status(), True),
                                   (_held("BBB", pid=2, opened="2026-10-02"), _status(), False)])
    assert text.count("модель тоже держит") == 1
    gme, bbb = text.split("\n\n")[1:3]
    assert gme.endswith("\n   модель тоже держит") and "модель" not in bbb


def test_a_coin_is_shown_by_its_symbol_with_thousands_and_the_model_line():
    coin = _held("CRYPTO:BTC", entry=60_000.0, opened="2026-09-28", stop_pct=0.15, source="CRYPTO")
    st = _status(61_200.0, entry=60_000.0, peak=61_200.0, stop_pct=0.15, stop_level=52_020.0,
                 to_stop=0.176)
    text = tn.format_my_portfolio([(coin, st, True)])
    assert ("• BTC: вход 60 000,00 (28.09), сейчас 61 200,00 (+2,0%), 3 дн.\n"
            "   стоп 52 020,00 (−15% от максимума 61 200,00), до стопа 17,6%\n"
            "   модель тоже держит") in text
    assert "CRYPTO" not in text


def test_a_position_with_no_price_says_so_and_has_no_stop_line():
    text = tn.format_my_portfolio([(_held(insiders=["Ryan Cohen"]), _status(None), True)])
    assert ("• GME: вход 23,10 (01.10), сейчас — цена недоступна, 3 дн.\n"
            "   слежу за продажами: Ryan Cohen\n   модель тоже держит") in text
    assert "стоп" not in text and "Средний результат" not in text      # nothing to average either
    assert text.endswith(_SELL_LINE)


def test_a_price_below_the_stop_is_said_so():
    """to_stop is how far the price can still fall (a share of the price now): below the stop it is
    negative, and «ниже стопа на» shows the same quantity without its sign."""
    st = _status(90.0, entry=100.0, peak=130.0, stop_level=117.0, to_stop=1 - 117.0 / 90.0)
    text = tn.format_my_portfolio([(_held(entry=100.0), st, False)])
    assert "   стоп 117,00 (−10% от максимума 130,00), ниже стопа на 30,0%" in text
    assert "до стопа" not in text


def test_at_the_stop_level_there_is_nothing_left_to_fall():
    st = _status(117.0, entry=100.0, peak=130.0, stop_level=117.0, to_stop=0.0)
    assert "), до стопа 0,0%" in tn.format_my_portfolio([(_held(entry=100.0), st, False)])


def test_the_average_is_the_equal_weighted_mean_of_the_known_results():
    rows = [(_held("AAA", pid=1, entry=100.0), _status(110.0, entry=100.0), False),      # +10%
            (_held("BBB", pid=2, entry=100.0), _status(95.0, entry=100.0), False),       # -5%
            (_held("CCC", pid=3, entry=100.0), _status(None, entry=100.0), False)]       # no price: left out
    text = tn.format_my_portfolio(rows)
    assert "Средний результат: +2,5% по 2 позициям\n" + _SELL_LINE in text
    assert text.startswith("<b>💼 Ваш портфель — 3 позиции</b>")                          # all three are held


def test_a_loss_has_a_real_minus_sign():
    text = tn.format_my_portfolio([(_held(entry=100.0), _status(95.0, entry=100.0), False)])
    assert "сейчас 95,00 (−5,0%)" in text and "Средний результат: −5,0% по 1 позиции" in text


def test_the_oldest_position_comes_first():
    rows = [(_held("NEW", opened="2026-10-01", pid=2), _status(), False),
            (_held("OLD", opened="2026-09-20", pid=1), _status(), False)]
    text = tn.format_my_portfolio(rows)
    assert text.index("• OLD") < text.index("• NEW")


def test_no_positions_say_how_to_add_one():
    assert tn.format_my_portfolio([]) == (
        "Ваших позиций нет. Купили? /bought TICKER [цена] — например /bought GME 23.10. "
        "Модельный портфель: /model.")


def test_my_portfolio_escapes_every_dynamic_string():
    rows = [(_held("A&B<C>", insiders=["X & <Y>", "Q>R"]), _status(), False)]
    text = tn.format_my_portfolio(rows)
    assert "• A&amp;B&lt;C&gt;:" in text and "слежу за продажами: X &amp; &lt;Y&gt;, Q&gt;R" in text
    assert "<" not in text.replace("<b>", "").replace("</b>", "")      # only the header's bold is markup
    assert ">" not in text.replace("<b>", "").replace("</b>", "")


def test_my_portfolio_in_plain_text_has_no_markup_and_no_escaping():
    rows = [(_held("A&B", insiders=["X & <Y>"]), _status(), False)]
    text = tn.format_my_portfolio(rows, html=False)
    assert text.startswith("💼 Ваш портфель — 1 позиция\n\n• A&B:")
    assert "<b>" not in text and "&amp;" not in text and "слежу за продажами: X & <Y>" in text
    assert tn.format_my_portfolio([], html=False).startswith("Ваших позиций нет.")


def test_my_position_blocks_are_the_positions_alone():
    """The analyst prints these under its own heading: no header, no average, no hint."""
    rows = [(_held("GME", insiders=["Ryan Cohen"]), _status(), True),
            (_held("BBB", pid=2, opened="2026-10-02"), _status(None), False)]
    blocks = tn.my_position_blocks(rows, html=False)
    assert len(blocks) == 2 and blocks[0].startswith("• GME: вход 23,10 (01.10)")
    assert blocks[1] == "• BBB: вход 23,10 (02.10), сейчас — цена недоступна, 3 дн."
    assert tn.my_position_blocks([], html=False) == []


# ------------------------------------------------------------ Trading 212 (/portfolio's first section)
@pytest.mark.parametrize("x, text", [(10.0, "10"), (100.0, "100"), (1234.5, "1 234,5"), (0.52347, "0,5235"),
                                     (2.5, "2,5"), (0.0, "0")])
def test_a_quantity_is_shown_the_russian_way_without_needless_decimals(x, text):
    assert tn.quantity(x) == text


def _t212_holding(name="GME", *, qty=10.0, avg=23.10, price=24.05, currency="USD", pnl=8.30,
                  pnl_currency="EUR", tracked=True, insiders=(), held=False, opened="2026-10-01", days=None,
                  price_date=None, **status):
    """A line of «💼 Trading 212» as t212_account builds it; `tracked`: it has a position (and so a
    status); `days`: since it was bought in Trading 212, when that is known; `price_date`: the day
    of a stored price that is too old to be a price (the status then has none)."""
    pos = _held(name, entry=avg or 1.0, opened=opened, insiders=insiders) if tracked else None
    st = _status(None if price_date else price, entry=avg or 1.0, **status) if tracked else None
    return ta.Holding(name=name, quantity=qty, avg_price=avg, price=price, currency=currency, pnl=pnl,
                      pnl_currency=pnl_currency, position=pos, status=st, model_holds=held, opened=opened,
                      days=days, price_date=price_date)


_LIVE = ta.T212Summary("EUR", 12345.67, 2000.0, 10345.67, 10000.0, 345.67, 12.5)
_ACCOUNT_LINE = "Счёт: €12 346 · вложено €10 000 · P/L +€346 (+3,5%) · свободно €2 000"


def _view(*holdings, summary=_LIVE, **kw):
    return ta.PortfolioView(list(holdings), summary=None if kw.get("error") else summary, **kw)


def test_the_portfolio_opens_with_trading_212_then_what_is_outside_it():
    manual = [(_held("AAA", entry=100.0), _status(110.0, entry=100.0, peak=110.0, stop_level=99.0), False)]
    view = _view(_t212_holding(insiders=["Ryan Cohen"], held=True))
    assert tn.format_my_portfolio(manual, t212=view) == "\n\n".join([
        "<b>💼 Trading 212</b>\n" + _ACCOUNT_LINE,
        "\n".join(["• GME — 10 шт., средняя 23,10, сейчас 24,05 USD (+4,1%), €+8,30",
                   "   стоп 21,65 (−10% от максимума 24,05), до стопа 10,0%",
                   "   слежу за продажами: Ryan Cohen",
                   "   модель тоже держит"]),
        "<b>✍️ Вне Trading 212</b>",
        "\n".join(["• AAA: вход 100,00 (01.10), сейчас 110,00 (+10,0%), 3 дн.",
                   "   стоп 99,00 (−10% от максимума 110,00), до стопа 10,0%"]),
        "Средний результат: +7,1% по 2 позициям\n" + _SELL_LINE])           # (+4,1% and +10,0%) / 2


def test_only_trading_212_holdings_have_no_outside_section():
    text = tn.format_my_portfolio([], t212=_view(_t212_holding()))
    assert "Вне Trading 212" not in text and "Ваш портфель" not in text
    assert text.endswith("Средний результат: +4,1% по 1 позиции\n" + _SELL_LINE)


def test_the_account_line_shows_a_loss_with_a_real_minus_and_leaves_out_what_it_does_not_know():
    losing = ta.T212Summary("EUR", 9880.4, 500.0, 9380.4, 9500.0, -119.6, 0.0)
    assert tn.format_my_portfolio([], t212=_view(_t212_holding(pnl=-8.30, price=22.0), summary=losing)).splitlines()[1:3] \
        == ["Счёт: €9 880 · вложено €9 500 · P/L −€120 (−1,3%) · свободно €500", ""]
    assert "€−8,30" in tn.format_my_portfolio([], t212=_view(_t212_holding(pnl=-8.30, price=22.0)))
    partial = ta.T212Summary(None, 500.0, None, None, None, None, None)
    assert tn.format_my_portfolio([], t212=_view(_t212_holding(), summary=partial)).splitlines()[1] == "Счёт: €500"
    computed = ta.T212Summary("EUR", 1100.0, 100.0, 1000.0, 800.0, None, None)     # P/L from value - cost
    assert "вложено €800 · P/L +€200 (+25,0%)" in tn.format_my_portfolio([], t212=_view(summary=computed))


@pytest.mark.parametrize("currency, total, pnl", [("GBP", "£12 346", "£+8,30"), ("USD", "$12 346", "$+8,30"),
                                                  ("CHF", "12 346 CHF", "+8,30 CHF")])
def test_another_account_currency_is_named(currency, total, pnl):
    summary = ta.T212Summary(currency, 12345.67, None, None, None, None, None)
    text = tn.format_my_portfolio([], t212=_view(_t212_holding(pnl_currency=currency), summary=summary))
    assert f"Счёт: {total}" in text and f", {pnl}" in text


def test_a_holding_trading_212_shows_before_the_bot_tracks_it_has_only_its_line():
    text = tn.format_my_portfolio([], t212=_view(_t212_holding(tracked=False)))
    block = text.split("\n\n")[1]
    assert block == "• GME — 10 шт., средняя 23,10, сейчас 24,05 USD (+4,1%), €+8,30"


def test_a_holding_with_no_price_says_so_and_what_is_not_known_is_left_out():
    h = _t212_holding(price=None, pnl=None)
    block = tn.format_my_portfolio([], t212=_view(h)).split("\n\n")[1]
    assert block == "• GME — 10 шт., средняя 23,10, сейчас — цена недоступна"
    bare = ta.Holding(name="SAP", quantity=None, avg_price=None, price=125.0, currency=None)
    assert tn.format_my_portfolio([], t212=_view(bare)).split("\n\n")[1] == "• SAP — сейчас 125,00"


def test_the_oldest_trading_212_holding_comes_first():
    text = tn.format_my_portfolio([], t212=_view(_t212_holding("NEW", opened="2026-10-01"),
                                                 _t212_holding("OLD", opened="2026-09-20")))
    assert text.index("• OLD") < text.index("• NEW")


def test_when_trading_212_did_not_answer_the_stored_holdings_come_with_the_reason_and_their_time():
    stored = _t212_holding(pnl=9.5, pnl_currency="USD")
    text = tn.format_my_portfolio([], t212=_view(stored, error="HTTP 502", as_of="14:05"))
    head, block = text.split("\n\n")[:2]
    assert head == ("<b>💼 Trading 212</b>\n"
                    "⚠️ Trading 212 не ответил (HTTP 502) — данные на 14:05 последней синхронизации")
    assert block.startswith("• GME — 10 шт., средняя 23,10, сейчас 24,05 USD (+4,1%), $+9,50\n   стоп ")
    assert "Счёт:" not in text
    nothing = tn.format_my_portfolio([], t212=_view(error="ReadTimeout", as_of="14:05"))
    assert nothing.split("\n\n")[0] == "<b>💼 Trading 212</b>\n⚠️ Trading 212 не ответил (ReadTimeout)"


_HINT = ("Создайте в Trading 212 → Настройки → API ключ только для чтения (Portfolio, Account data) "
         "и положите в .env")


def test_a_missing_or_powerless_key_shows_the_reason_and_the_hint():
    no_key = tn.format_my_portfolio([], t212=_view(error="ключ Trading 212 не задан", hint=_HINT))
    assert no_key == "\n\n".join(["<b>💼 Trading 212</b>\n⚠️ Ключ Trading 212 не задан\n" + _HINT,
                                  "Ваших позиций нет. Купили? /bought TICKER [цена] — например "
                                  "/bought GME 23.10. Модельный портфель: /model."])
    forbidden = tn.format_my_portfolio(
        [(_held("AAA"), _status(), False)],
        t212=_view(_t212_holding(), error="ключу Trading 212 не хватает прав: нужны чтение портфеля и счёта",
                   hint=_HINT, as_of="30.09 14:05"))
    assert forbidden.split("\n\n")[0] == (
        "<b>💼 Trading 212</b>\n⚠️ Ключу Trading 212 не хватает прав: нужны чтение портфеля и счёта"
        " — данные на 30.09 14:05 последней синхронизации\n" + _HINT)
    assert "<b>✍️ Вне Trading 212</b>" in forbidden and "• AAA: вход" in forbidden


def test_an_empty_account_and_nothing_bought_is_the_account_line_and_the_empty_text():
    text = tn.format_my_portfolio([], t212=_view())
    assert text == "<b>💼 Trading 212</b>\n" + _ACCOUNT_LINE + "\n\n" + tn._NO_POSITIONS


def test_the_trading_212_section_escapes_every_dynamic_string_and_uses_only_bold():
    h = _t212_holding("A&B<C>", currency="U<S>D", insiders=["X & <Y>"])
    text = tn.format_my_portfolio([], t212=_view(h, error="bad <reason> & co", as_of="14:05"))
    assert "• A&amp;B&lt;C&gt; — " in text and "U&lt;S&gt;D" in text and "X &amp; &lt;Y&gt;" in text
    assert "(bad &lt;reason&gt; &amp; co)" in text
    bare = text.replace("<b>", "").replace("</b>", "")
    assert "<" not in bare and ">" not in bare


def test_the_trading_212_section_in_plain_text_has_no_markup():
    text = tn.format_my_portfolio([(_held("A&B"), _status(), False)], html=False,
                                  t212=_view(_t212_holding("C&D")))
    assert text.startswith("💼 Trading 212\nСчёт: €12 346") and "\n\n✍️ Вне Trading 212\n\n• A&B: вход" in text
    assert "<b>" not in text and "&amp;" not in text and "• C&D — 10 шт." in text


def test_trading_212_blocks_are_the_holdings_alone():
    blocks = tn.t212_blocks([_t212_holding("NEW"), _t212_holding("OLD", opened="2026-09-01")], html=False)
    assert len(blocks) == 2 and blocks[0].startswith("• OLD — 10 шт.") and tn.t212_blocks([], html=False) == []


def test_the_account_line_alone():
    assert tn.format_t212_account(12345.67, 10000.0, 345.67, 2000.0, "EUR") == _ACCOUNT_LINE
    assert tn.format_t212_account(None, None, None, None) == ""
    assert tn.format_t212_account(100.0, 0.0, 0.0, 100.0) == "Счёт: €100 · вложено €0 · P/L +€0 · свободно €100"


@pytest.mark.parametrize("x, currency, text", [(9800.4, "EUR", "€9 800"), (-9800.4, "EUR", "−€9 800"),
                                               (-0.2, "EUR", "€0"), (1234.0, None, "€1 234"),
                                               (1234.0, "USD", "$1 234"), (-5.0, "SEK", "−5 SEK")])
def test_money_in_a_currency(x, currency, text):
    assert tn.money(x, currency) == text
    assert tn.money_eur(9800.4) == "€9 800" and tn.money_eur(-9800.4) == "−€9 800"


def test_a_trading_212_holding_shows_the_days_since_it_was_bought_there():
    block = tn.format_my_portfolio([], t212=_view(_t212_holding(days=5))).split("\n\n")[1]
    assert block.splitlines()[0] == "• GME — 10 шт., средняя 23,10, сейчас 24,05 USD (+4,1%), €+8,30, 5 дн."
    unpriced = tn.format_my_portfolio([], t212=_view(_t212_holding(price=None, pnl=None, days=0)))
    assert "• GME — 10 шт., средняя 23,10, сейчас — цена недоступна, 0 дн." in unpriced
    assert "дн." not in tn.format_my_portfolio([], t212=_view(_t212_holding())).split("\n\n")[1]   # not known


def test_a_holding_that_pre_dates_tracking_shows_its_real_loss_and_a_stop_from_its_floor():
    """Bought at 100 long ago, at 50 when the bot first saw it: −50% is the truth, and the stop is
    measured from 50."""
    legacy = _t212_holding(avg=100.0, price=50.0, pnl=-431.0, days=518, peak=50.0, stop_level=45.0)
    block = tn.format_my_portfolio([], t212=_view(legacy)).split("\n\n")[1]
    assert block.splitlines() == [
        "• GME — 10 шт., средняя 100,00, сейчас 50,00 USD (−50,0%), €−431,00, 518 дн.",
        "   стоп 45,00 (−10% от максимума 50,00), до стопа 10,0%"]


def test_a_stale_stored_price_is_shown_as_the_price_of_its_day_with_no_stop_line():
    """Trading 212 has not answered for days: the last stored price is shown as what it is, the
    price of 28.09, and no stop is read from it."""
    stale = _t212_holding(pnl=9.5, pnl_currency="USD", days=9, price_date="2026-09-28", insiders=["Ryan Cohen"])
    view = _view(stale, _t212_holding("NEW", avg=100.0, price=110.0, opened="2026-10-02"),
                 error="ReadTimeout", as_of="28.09 14:05")
    text = tn.format_my_portfolio([], t212=view)
    gme = next(b for b in text.split("\n\n") if b.startswith("• GME"))
    assert gme.splitlines() == [
        "• GME — 10 шт., средняя 23,10, цена на 28.09: 24,05 USD (+4,1%), $+9,50, 9 дн.",
        "   слежу за продажами: Ryan Cohen"]                        # no «стоп» line: nothing to measure it with
    assert "сейчас 24,05" not in text
    assert "Средний результат: +10,0% по 1 позиции" in text         # a stale result is not averaged in
