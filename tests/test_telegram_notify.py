"""telegram_notify.py -- HTML escaping and the html/plain dual rendering of
signal messages. The per-filing line formatters (format_sec_line etc.) are
console-only in this project (bot.py prints them, nothing sends them to
Telegram) and are intentionally left as plain text -- untested here since
they render no markup."""
from __future__ import annotations

from cluster import ClusterSignal, ExitSignal, StakeSignal

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


def test_send_text_still_skips_silently_without_credentials(monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert tn.send_text("hello") is False


# ------------------------------------------------------------ market_note
#
# tradingview.annotate_signals() sets `.market_note` on a signal after the fact
# (or leaves it unset/None on failure); the formatters must render it when
# present and simply omit the line otherwise, in both html and plain mode.

def test_format_signal_renders_a_market_note_in_html_as_italics():
    sig = _cluster()
    sig.market_note = "TradingView +0.60 (Strong Buy) — механический индикатор, не мнение бота"
    text = tn.format_signal(sig, html=True)
    assert "<i>TradingView +0.60 (Strong Buy)" in text
    assert text.rstrip().endswith("не мнение бота</i>")


def test_format_signal_renders_a_market_note_in_plain_mode_without_tags():
    sig = _cluster()
    sig.market_note = "TradingView +0.60 (Strong Buy) — механический индикатор, не мнение бота"
    text = tn.format_signal(sig)
    assert "<i>" not in text
    assert "TradingView +0.60 (Strong Buy)" in text


def test_format_signal_omits_the_note_line_when_absent():
    text = tn.format_signal(_cluster(), html=True)
    assert "TradingView" not in text


def test_format_exit_signal_renders_a_market_note():
    sig = _exit()
    sig.market_note = "TradingView -0.30 (Sell) — механический индикатор, не мнение бота"
    text = tn.format_exit_signal(sig, html=True)
    assert "<i>TradingView -0.30 (Sell)" in text


def test_format_stake_signal_renders_a_market_note_before_the_source_link():
    sig = _stake()
    sig.market_note = "TradingView +0.10 (Buy) — механический индикатор, не мнение бота"
    text = tn.format_stake_signal(sig, html=True)
    lines = text.splitlines()
    note_idx = next(i for i, l in enumerate(lines) if "TradingView" in l)
    link_idx = next(i for i, l in enumerate(lines) if "<a href" in l)
    assert note_idx < link_idx
