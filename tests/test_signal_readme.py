"""The README's message examples are what the code renders (specs 2026-10-04-signal-message-style.md and
2026-10-04-remove-model-portfolio.md): change a format without the README, or the README without a format, and
this fails. Offline -- the examples are rendered from the same stub data the README is written from."""
from __future__ import annotations

import datetime as dt
import html
import re
import time
from pathlib import Path

import pytest

import db
import positions
import signals_weekly
import t212_account as ta
import telegram_notify as tn
import weekly

README = Path(weekly.__file__).parent / "README.md"
_TAGS = re.compile(r"</?b>")
FRI = dt.date(2026, 10, 9)


@pytest.fixture(autouse=True)
def _utc(monkeypatch):
    """The group exits read journal timestamps (stored in UTC) as local dates."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _text() -> str:
    return README.read_text(encoding="utf-8")


def _section() -> str:
    text = _text()
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


def _hold(conn, ticker, result, *, alerted=None):
    """A Trading 212 holding bought at 100 whose last stored price is `result` away from it."""
    conn.execute("INSERT INTO positions (ticker, opened_at, entry_price, origin, quantity, t212_ticker, currency, "
                 "close_alerted_at) VALUES (?,?,?,?,?,?,?,?)",
                 (ticker, "2026-09-01", 100.0, "t212", 5.0, f"{ticker}_US_EQ", "USD", alerted))
    conn.execute("INSERT INTO t212_prices (ticker, date, price) VALUES (?,?,?)",
                 (ticker, FRI.isoformat(), round(100.0 * (1 + result), 4)))


def _busy_week(conn):
    """The README's week of 03.10-09.10: the account up 2,9%, 43 positions (the three best and the three worst
    named), two buy signals, one sell alert and one group exit."""
    for day, value in (("2026-10-02", 12_000.0), (FRI.isoformat(), 12_345.67)):
        conn.execute("INSERT INTO t212_equity (date, total_value, currency) VALUES (?,?,'EUR')", (day, value))
    for ticker, result in (("SMCI", 0.31), ("BBD", 0.124), ("GME", 0.081), ("DEF", -0.042), ("ABC", -0.09),
                           ("XYZ", -0.225)):
        _hold(conn, ticker, result, alerted="2026-10-07" if ticker == "XYZ" else None)
    for i in range(37):                                        # the rest: nothing to name
        _hold(conn, f"F{i:02d}", 0.0)
    for ticker in ("NEW", "GME"):
        signals_weekly.record_signal(conn, {"ticker": ticker, "kind": "stock", "score": 70.0}, FRI)
    db.journal_signal(conn, {"source": "SEC", "kind": "exit", "ticker": "ZZZ", "company": "Z Corp",
                             "members": '["Ann Lee"]'})
    conn.execute("UPDATE signal_journal SET emitted_at = '2026-10-08 12:00:00'")
    conn.commit()


def test_the_readme_examples_are_what_the_code_renders(conn):
    pick = dict(ticker="GME", source="SEC", company="GameStop", kind="stock", score=70.0, stop_pct=0.10,
                reasons=["2 инсайдера из руководства", "CEO среди покупателей", "первая покупка"], t212=False)
    stop = positions.CloseAlert(_position(), "trailing_stop", "−10% от максимума 25.80", 20.70)
    insider = positions.CloseAlert(_position(), "insider_sell", "Ryan Cohen — Form 4, 2026-10-01", 20.70)
    holding = ta.T212Position("GME_US_EQ", None, "US36467W1099", "USD", 10.0, 23.10, 24.05,
                              "2026-09-28T14:03:11.000+02:00", 207.3, 199.0, 8.30, "EUR")
    alt = dict(ticker="CRYPTO:SOL", source="CRYPTO", company="SOL", kind="crypto", score=75.0, stop_pct=0.22,
               reasons=["выше 100-дн. средней", "20 дн. +12%", "60 дн. +30%"], t212=None, risk=True)
    state = dict(close=1.2000, ma=1.1500, diff=0.35, atr=0.008)
    expected = [
        weekly.buy_text(pick),
        weekly.buy_text(alt),
        weekly.exit_text("XYZ", ["Anna Lee", "Bo Chen"]),
        tn.format_close_alert(stop),
        tn.format_close_alert(insider),
        ta._new_text("GME", holding, "стоп, продажи инсайдеров, новости"),
        ta._sold_text("GME", _position(), 24.05),
        tn.format_carry_signal("FLAT", "LONG", state, reason="entry", level=1.152),
        tn.format_carry_signal("LONG", "FLAT", dict(state, close=1.16), reason="trailing stop", level=1.167),
    ]
    signals = _blocks()[0]
    assert signals == [_shown(text) for text in expected]


def test_the_readme_summary_examples_are_what_the_code_renders(conn):
    _busy_week(conn)
    _signals, busy, quiet = _blocks()
    assert busy == _shown(weekly.format_summary(conn, FRI, scoring_failed=True)).splitlines()
    assert busy[1].startswith("Позиций 43: лучшие — SMCI +31,0%, BBD +12,4%, GME +8,1%; худшие — XYZ −22,5%")
    fresh = db.connect(":memory:")
    assert quiet == _shown(weekly.format_summary(fresh, FRI)).splitlines()
    assert quiet == ["📊 Неделя 03.10–09.10", "Позиций нет.", "Сигналов за неделю не было."]


def test_the_readme_has_three_example_blocks_and_the_summary_ones_follow_the_signals():
    blocks = _blocks()
    assert len(blocks) == 3 and blocks[0][0].startswith("🟢 GME!: покупка")
    assert blocks[1][0].startswith("📊 Неделя ") and blocks[2][0].startswith("📊 Неделя ")


def test_the_readme_summary_example_has_the_summarys_lines():
    _signals, summary, _quiet = _blocks()
    assert [line.split(" ")[0] for line in summary] == ["📊", "Позиций", "Сигналов", "⚠️"]
    assert re.match(r"📊 Неделя \d\d\.\d\d–\d\d\.\d\d: счёт Trading 212 €[\d ]+ \(за неделю [+−]\d+,\d%\)$", summary[0])
    assert re.match(r"Позиций \d+: лучшие — .+; худшие — .+", summary[1])
    assert re.match(r"Сигналов за неделю: покупок \d+, на продажу \d+, групповых выходов \d+$", summary[2])
    assert summary[3] == weekly.SCORING_FAILED_WARNING


def test_the_readme_has_no_trace_of_the_old_message_layouts():
    text = _text()
    for old in ("🚪", "📤", "Вижу в Trading 212", "Ваши позиции", "«Закрыть»", "🚨 Продают те, кто покупал",
                "format_week"):
        assert old not in text, old


def test_the_readme_has_no_trace_of_the_virtual_portfolio():
    text = _text()
    for gone in ("Модельный портфель", "модельный портфель", "модельного портфеля", "Модельного портфеля",
                 "MODEL-S", "MODEL-C", "python paper.py", "paper_report", "maybe_send_monthly_report",
                 "Месячный отчёт", "месячный отчёт", "месячного отчёта", "смесь 70/30", "смесью 70%",
                 "S&P 500 / BTC", "182 дн", "«Пройдено»", "`/model`", "/model ", "в модели €",
                 "модель тоже держит", "sellpending", "`sell:", "модель покупает", "покупок модели",
                 "модель не торгует", "DayReport", "model.run", "«Книги и проверка»", "1% стоимости модели",
                 "криптокармана", "не больше 12", "в одном секторе"):
        assert gone not in text, gone


def test_the_readme_says_how_the_week_is_picked_sent_and_resumed():
    text = " ".join(_text().split())
    for phrase in ("`weekly_sent_<ГГГГ>-W<нн>`", "`weekly_buys_<ГГГГ>-W<нн>`", "`model_buys_<ГГГГ>-W<нн>`",
                   "`buy:<тикер>`", "`exit:<id записи журнала>`",
                   "досылает только недостающее", "по одному сообщению на сигнал",
                   "строка `buy_signals` пишется после отправки своего сообщения"):
        assert phrase in text, phrase


def test_the_readme_says_which_buys_are_picked():
    text = " ".join(_text().split())
    for phrase in ("не больше пяти новых", "которых у вас нет", "за последние 30 дней",
                   "ваш счёт Trading 212 — единственный портфель", "Бот ничего не покупает и не продаёт",
                   "RESIGNAL_DAYS", "WEEKLY_BUY_LIMIT"):
        assert phrase in text, phrase


def test_the_readme_says_what_the_thirteen_coins_are_and_what_feeds_them():
    text = " ".join(_text().split())
    assert "BTC, ETH** и одиннадцатью альтами — **SOL, XRP, BNB, DOGE, AVAX, HYPE, LTC, ENA, LINK, TRX, SUI**" in text
    for phrase in ("`model.COINS`", "`model.MAJOR_COINS`", "| Тренд (цены) | все тринадцать |",
                   "| Притоки и оттоки спот-ETF | BTC, ETH и SOL |",
                   "| Покупки и продажи компаний (8-K/6-K) | все тринадцать |", "| Новости | все тринадцать |"):
        assert phrase in text, phrase


def test_the_readme_says_how_the_alts_are_held_back():
    text = " ".join(_text().split())
    for phrase in ("фильтр биткоина", "биткоин ниже 100-дн. средней — альты не покупаем",
                   "`coin_trend(закрытия BTC)[\"above_ma100\"]`", "`btc_up`",
                   "`WEEKLY_COIN_LIMIT`", "не больше двух монет", "кончается словами «высокий риск»",
                   "поле `risk`", "мало истории"):
        assert phrase in text, phrase
    from model_score import ALT_GATE_REASON
    from weekly import RISK_TAG
    assert ALT_GATE_REASON in text and RISK_TAG in text


def test_the_readme_says_a_coins_good_headlines_add_no_points():
    text = " ".join(_text().split()).lower()
    for phrase in ("| Новости | −30…0 |", "Хорошие заголовки монете баллов не дают",
                   "«upgrade» в крипто-новостях — обновление сети", "Красные флаги", "по-прежнему блок"):
        assert phrase.lower() in text, phrase
    assert "| новости | −30…+10 | те же списки, что у акций, плюс красные флаги" not in text


def test_the_readme_says_what_the_alts_treasury_and_etf_numbers_are():
    text = " ".join(_text().split())
    for phrase in ("не меньше €50 млн для BTC и ETH (€10 млн для альтов)", "от €5 млн для альтов",
                   "farside.co.uk/sol/", "BSOL, FSOL, GSOL, MSOL, SOEZ, TSOL, VSOL", "страницы «вся история» нет",
                   "| Фиксированный порог дня (истории меньше 30 дней) | $400 млн | $50 млн |",
                   "| Фиксированный порог 3 дней | $500 млн | $100 млн |",
                   "| Пол дня (относительное правило) | $100 млн | $25 млн |", "| Пол 3 дней | $250 млн | $60 млн |",
                   "`python crypto_treasury.py --backfill 365`", "сбой одного запроса", "только если упали все запросы",
                   "min(дней, 200)", "последний завершённый бар"):
        assert phrase.lower() in text.lower(), phrase


def test_the_readme_says_what_the_views_show_now():
    text = " ".join(_text().split())
    for phrase in ("«3) Мой портфель»", "«СИГНАЛЫ НА ПОКУПКУ ЗА 30 ДНЕЙ»",
                   "Таблицы `paper_*` остались в базе как архив"):
        assert phrase in text, phrase
    assert "3) Модельный портфель" not in text


def test_the_readme_says_which_sell_alerts_have_a_second_line_and_how_it_is_written():
    text = " ".join(_section().split())
    for phrase in ("Вторая строка — только у «продаёт инсайдер» (кто и когда), «отток по монете» (текст оттока) "
                   "и «плохие новости» (заголовок)", "остальные сигналы — одна строка",
                   "дата — `ДД.ММ`", "запятая в десятичных", "заголовок новости — как есть"):
        assert phrase in text, phrase
    assert "уровень стопа" not in text and "−10% от максимума 25.80" not in text     # the stop level is not repeated
