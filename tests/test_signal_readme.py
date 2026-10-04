"""The README's message examples are what the code renders (spec 2026-10-04-signal-message-style.md):
change a format without the README, or the README without a format, and this fails. Offline -- the
examples are rendered from the same stub data the README is written from."""
from __future__ import annotations

import html
import re
from pathlib import Path

import paper_report
import positions
import t212_account as ta
import telegram_notify as tn

README = Path(paper_report.__file__).parent / "README.md"
_TAGS = re.compile(r"</?b>")


def _section() -> str:
    text = README.read_text(encoding="utf-8")
    start = text.index("### Как выглядят сообщения\n")
    return text[start:text.index("\n### ", start + 5)]


def _blocks() -> list[list[str]]:
    """The section's ```text blocks as lists of messages (an indented line continues the one above)."""
    blocks = []
    for body in re.findall(r"```text\n(.*?)```", _section(), flags=re.S):
        messages: list[str] = []
        for line in body.splitlines():
            if line.startswith("   ") and messages:
                messages[-1] += "\n" + line
            else:
                messages.append(line)
        blocks.append(messages)
    return blocks


def _shown(text: str) -> str:
    """What Telegram shows of an HTML message."""
    return html.unescape(_TAGS.sub("", text))


def _position(**overrides):
    base = dict(id=1, ticker="GME", source=None, opened_at="2026-09-28", entry_price=23.10, insiders=[],
                signal_id=None, closed_at=None, close_reason=None, close_alerted_at=None, stop_pct=0.10,
                origin="t212", quantity=10.0, t212_ticker="GME_US_EQ", currency="USD")
    base.update(overrides)
    return positions.Position(**base)


def test_the_readme_examples_are_what_the_code_renders(conn):
    conn.execute("INSERT INTO paper_positions (book, ticker, source, symbol, currency, fill_date, cost_eur, "
                 "net_eur, entry_close, entry_fx, last_value) VALUES ('MODEL-S', 'GME', 'SEC', 'GME', 'USD', "
                 "'2026-09-25', 8000, 8000, 23.1, 1.16, 7216)")
    pid = conn.execute("SELECT id FROM paper_positions").fetchone()[0]
    buy = dict(id=1, book="MODEL-S", ticker="GME", status="pending", score=70.0, stop_pct=0.10,
               created="2026-10-09", reason="балл 70: 2 инсайдера из руководства; CEO среди покупателей")
    sale = dict(id=1, ticker="GME", close_reason="стоп: −10% от максимума", closed_date="2026-10-07",
                cost_eur=8000.0, proceeds_eur=7216.0)
    waiting = dict(position_id=pid, ticker="GME", reason="стоп: −10% от максимума")
    stop = positions.CloseAlert(_position(), "trailing_stop", "−10% от максимума 25.80", 20.70)
    insider = positions.CloseAlert(_position(), "insider_sell", "Ryan Cohen — Form 4, 2026-10-01", 20.70)
    holding = ta.T212Position("GME_US_EQ", None, "US36467W1099", "USD", 10.0, 23.10, 24.05,
                              "2026-09-28T14:03:11.000+02:00", 207.3, 199.0, 8.30, "EUR")
    state = dict(close=1.2000, ma=1.1500, diff=0.35, atr=0.008)
    expected = [
        paper_report._buy_text(conn, buy, {"GME": False}),
        paper_report._sale_text(sale),
        paper_report._pending_sale_text(conn, waiting),
        paper_report._exit_text("XYZ", ["Anna Lee", "Bo Chen"]),
        tn.format_close_alert(stop),
        tn.format_close_alert(insider),
        ta._new_text("GME", holding, "стоп, продажи инсайдеров, новости"),
        ta._sold_text("GME", _position(), 24.05),
        tn.format_carry_signal("FLAT", "LONG", state, reason="entry", level=1.152),
        tn.format_carry_signal("LONG", "FLAT", dict(state, close=1.16), reason="trailing stop", level=1.167),
    ]
    signals, _summary = _blocks()
    assert signals == [_shown(text) for text in expected]


def test_the_readme_summary_example_has_the_summarys_lines():
    _signals, summary = _blocks()
    assert [line.split(": ")[0].split(" (")[0].split(" ")[0] for line in summary] == [
        "📊", "В", "Сигналов", "Ваш", "⚠️"]
    assert summary[0].startswith("📊 Модель, неделя ") and " с начала, за неделю " in summary[0]
    assert "; смесь 70/30 " in summary[0] and re.match(r"В портфеле \(\d+\): ", summary[1])
    assert re.match(r"Сигналов за неделю: покупок \d+, продаж \d+, групповых выходов \d+$", summary[2])
    assert re.match(r"Ваш счёт Trading 212: €[\d ]+ \(за неделю [+−]\d+,\d%\)$", summary[3])
    assert summary[4] == paper_report.MODEL_FAILED_WARNING


def test_the_readme_has_no_trace_of_the_old_message_layouts():
    text = README.read_text(encoding="utf-8")
    for old in ("🚪", "📤", "Вижу в Trading 212", "Ваши позиции", "«Закрыть»", "🚨 Продают те, кто покупал",
                "format_week"):
        assert old not in text, old


def test_the_readme_says_how_the_week_is_sent_and_resumed():
    text = " ".join(README.read_text(encoding="utf-8").split())
    for phrase in ("`weekly_sent_<ГГГГ>-W<нн>`", "`buy:<id заказа>`", "`sell:<id позиции>`",
                   "`sellpending:<id заказа>`", "`exit:<id записи журнала>`",
                   "досылает только недостающее", "Месячный отчёт", "сразу после недельной сводки",
                   "по одному сообщению на сигнал"):
        assert phrase in text, phrase


def test_the_readme_says_which_sell_alerts_have_a_second_line_and_how_it_is_written():
    text = " ".join(_section().split())
    for phrase in ("Вторая строка — только у «продаёт инсайдер» (кто и когда), «отток по монете» (текст оттока) "
                   "и «плохие новости» (заголовок)", "остальные сигналы — одна строка",
                   "дата — `ДД.ММ`", "запятая в десятичных", "заголовок новости — как есть"):
        assert phrase in text, phrase
    assert "уровень стопа" not in text and "−10% от максимума 25.80" not in text     # the stop level is not repeated
