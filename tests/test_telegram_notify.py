"""telegram_notify.py -- HTML escaping and the html/plain dual rendering of
signal messages. The per-filing line formatters (format_sec_line etc.) are
console-only in this project (bot.py prints them, nothing sends them to
Telegram) and are intentionally left as plain text -- untested here since
they render no markup."""
from __future__ import annotations

import pytest

import cluster
from cluster import ClusterSignal, ExitSignal, StakeSignal

import positions
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
# menu's Сигналы); it is never in Telegram: the weekly message (paper_report.format_week) and
# the daily close alerts (bot._send_closes) do not carry signals.

def _caution_signal():
    return cluster.CryptoSignal("CRYPTO_ETF", "etf_flow", "CRYPTO:BTC", "спот-ETF США, фондов: 12",
                                False, None, 9e8, "2026-09-24", "2026-09-26",
                                ["3 дн. подряд оттока, всего $1,050 млн"], None, ["k"])


def test_a_bearish_coin_signal_reads_as_an_outflow():
    text = telegram_notify.format_any_signal(_caution_signal(), html=False)
    assert "ОТТОК ИЗ СПОТ-ETF" in text and "3 дн. подряд оттока" in text


@pytest.mark.parametrize("trigger, reason", [
    ("insider_sell", "инсайдеры продают"), ("caution", "сигнал осторожности"),
    ("trailing_stop", "стоп от максимума"), ("dead_money", "стоит на месте"),
    ("time", "год в позиции"), ("trend_down", "тренд вниз"), ("news", "плохие новости")])
def test_every_close_trigger_has_its_own_reason(trigger, reason):
    pos = positions.Position(1, "AAA", "SEC", "2026-09-01", 100.0, [], None, None, None, None)
    text = telegram_notify.format_close_alert(positions.CloseAlert(pos, trigger, "detail", 90.0), html=False)
    assert text.startswith(f"🚪 AAA — {reason}\n")


def test_caution_close_alert_reads_as_such():
    pos = positions.Position(1, "CRYPTO:BTC", "CRYPTO", "2026-09-20", 84_500.0, [], None,
                             None, None, None)
    alert = positions.CloseAlert(pos, "caution", "отток из спот-ETF (€900,000,000); цена "
                                 "подтверждает: -6.2% за 7 дн., ниже 20-дн. средней", 79_900.0)
    text = telegram_notify.format_close_alert(alert, html=False)
    assert "CRYPTO:BTC — сигнал осторожности" in text and "отток из спот-ETF" in text


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
