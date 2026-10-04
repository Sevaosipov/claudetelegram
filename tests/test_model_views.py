"""The model portfolio's views: the weekly Telegram signals and summary, the scored list, the summary
(paper_report.format_summary / model_stats) and the menu's signals view. Offline -- scores,
reports and books are hand-built."""
from __future__ import annotations

import datetime as dt
import json
import re
import time

import pytest

import db
import model
import model_score
import paper_report
import positions
import telegram_notify as tn
from model import DayReport

TODAY = dt.date(2026, 10, 5)
S, C = model.STOCK_BOOK, model.CRYPTO_BOOK


@pytest.fixture(autouse=True)
def _utc(monkeypatch):
    """The weekly signals read journal timestamps (stored in UTC) as local dates; the tests
    that put a row on the edge of a week need the local day to be the UTC day."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


# ------------------------------------------------------------------ builders
def _stock(ticker="AAA", total=64.0, decision=model_score.BUY, **kw):
    base = dict(ticker=ticker, source="SEC", company=f"{ticker} Corp", signal=None, insiders=52.0,
                triggers=5.0, momentum=7.0, news=0.0, total=total, decision=decision,
                reasons=["3 инсайдера"], block=None, untradeable=None, t212=True, stop_pct=0.10,
                last_close=10.0)
    base.update(kw)
    return model_score.StockScore(**base)


def _coin(coin="BTC", total=60.0, decision=model_score.BUY, **kw):
    base = dict(coin=coin, ticker=f"CRYPTO:{coin}", trend=45.0, flows=15.0, news=0.0, total=total,
                trend_up=True, trend_down=False, caution=None, block=None, decision=decision,
                reasons=["выше 100-дн. средней"], stop_pct=0.20, last_close=100.0)
    base.update(kw)
    return model_score.CoinScore(**base)


def _report(scored=(), value=101_230.0, bench=100_800.0):
    return DayReport(buys=[], sells=[], scored=list(scored), value=value, bench=bench, decisions={})


def _close(ticker="CCC"):
    pos = positions.Position(1, ticker, "SEC", "2026-09-01", 100.0, ["A"], None, None, None, None)
    return positions.CloseAlert(pos, "trailing_stop", "−10% от максимума 100.00", 84.0)


# --------------------------------------------------------------- format_scored
def test_scored_header_and_empty_message():
    text = tn.format_scored([_stock()])
    assert text.splitlines()[0] == "СИГНАЛЫ — оценка модели (покупка от 60, наблюдение 45–59)"
    assert tn.format_scored([]) == "Свежих сигналов за 14 дней нет."


def test_stock_line_has_the_icon_the_total_and_the_four_parts():
    line = tn.format_scored([_stock("AAA", 64.0, model_score.BUY)]).splitlines()[1]
    assert line.startswith("🟢 AAA")
    assert "64" in line and "инсайдеры 52 · поводы 5 · импульс 7 · новости 0" in line


def test_coin_line_has_trend_flows_and_news():
    line = tn.format_scored([_coin("BTC", 60.0)]).splitlines()[1]
    assert line.startswith("🟢 CRYPTO:BTC") and "тренд 45 · потоки 15 · новости 0" in line


def test_icons_by_decision():
    text = tn.format_scored([
        _stock("BUY1", 64.0, model_score.BUY), _stock("WAT1", 50.0, model_score.WATCH),
        _stock("BLK1", 30.0, model_score.BLOCK, block="SEC investigation"),
        _coin("ETH", 30.0, model_score.WATCH)])
    lines = {ln.split()[1]: ln for ln in text.splitlines()[1:]}
    assert lines["BUY1"].startswith("🟢") and lines["WAT1"].startswith("👀")
    assert lines["BLK1"].startswith("⛔") and lines["CRYPTO:ETH"].startswith("👀")


def test_block_and_untradeable_reasons_and_the_t212_label():
    text = tn.format_scored([
        _stock("BLK1", 30.0, model_score.BLOCK, block="SEC investigation"),
        _stock("SML1", 66.0, model_score.WATCH, untradeable="компания меньше €20 млн", t212=False),
        _coin("BTC", 20.0, model_score.BLOCK, block="hack news")])
    lines = {ln.split()[1]: ln for ln in text.splitlines()[1:]}
    assert "— SEC investigation" in lines["BLK1"]
    assert "— компания меньше €20 млн" in lines["SML1"] and "нет на T212" in lines["SML1"]
    assert "— hack news" in lines["CRYPTO:BTC"]
    assert "нет на T212" not in lines["BLK1"]


def test_a_negative_news_part_reads_with_a_minus():
    line = tn.format_scored([_stock(news=-10.0)]).splitlines()[1]
    assert "новости −10" in line


def test_skipped_stocks_go_below_a_line_and_at_most_15():
    scored = [_stock("TOP", 64.0)] + [_stock(f"S{i:02d}", 30.0 - i * 0.1, model_score.SKIP)
                                      for i in range(20)]
    lines = tn.format_scored(scored).splitlines()
    cut = lines.index("Прочие (балл ниже 45):")
    assert any(ln.startswith("🟢 TOP") for ln in lines[:cut])
    below = lines[cut + 1:]
    assert len(below) == 15 and all(ln.startswith("·") for ln in below)
    assert below[0].startswith("· S00")


def test_the_header_and_the_prochie_line_follow_the_models_bars(monkeypatch):
    monkeypatch.setattr(model_score, "STOCK_BUY", 70.0)
    monkeypatch.setattr(model_score, "STOCK_WATCH", 50.0)
    lines = tn.format_scored([_stock("TOP", 74.0), _stock("LOW", 30.0, model_score.SKIP)]).splitlines()
    assert lines[0] == "СИГНАЛЫ — оценка модели (покупка от 70, наблюдение 50–69)"
    assert "Прочие (балл ниже 50):" in lines


def test_no_prochie_line_without_skipped_stocks():
    assert "Прочие" not in tn.format_scored([_stock(), _coin()])


def test_scored_html_is_bold_and_escaped():
    text = tn.format_scored([_stock("A&B", 30.0, model_score.BLOCK, block="<fraud> & co")], html=True)
    assert text.splitlines()[0].startswith("<b>СИГНАЛЫ")
    assert "A&amp;B" in text and "&lt;fraud&gt; &amp; co" in text


# ----------------------------------------------------------------- model_stats
def _start(conn, days_ago):
    model.create_books(conn, TODAY - dt.timedelta(days=days_ago))


def _equity(conn, code, rows):
    """rows: [(days_ago, value, bench)]"""
    for days_ago, value, bench in rows:
        conn.execute("INSERT OR REPLACE INTO paper_equity (book, date, value, cash, bench) "
                     "VALUES (?,?,?,?,?)",
                     (code, (TODAY - dt.timedelta(days=days_ago)).isoformat(), value, 0.0, bench))
    conn.commit()


def _seed_growth(conn, *, bench_end=(71_400.0, 27_000.0)):
    """Together: 100 000 -> 98 000 -> 102 000; the mix: 100 000 -> 90 500 -> its `bench_end` sum."""
    _equity(conn, S, [(100, 70_000, 70_000), (50, 72_000, 66_500), (0, 74_000, bench_end[0])])
    _equity(conn, C, [(100, 30_000, 30_000), (50, 26_000, 24_000), (0, 28_000, bench_end[1])])


def _closed_trades(conn, code, n):
    for i in range(n):
        conn.execute(
            "INSERT INTO paper_positions (book, ticker, source, symbol, currency, fill_date, cost_eur, "
            "net_eur, entry_close, entry_fx, closed_date, close_reason, proceeds_eur) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (code, f"T{i}", "SEC", f"T{i}", "USD", "2026-06-01", 8000, 7980, 10, 1.16, "2026-07-01",
             "стоп", 8100))
    conn.commit()


def _open_position(conn, code, ticker, *, fill="2026-09-25", cost=8_000.0, last=8_400.0, stop=0.10):
    cur = conn.execute(
        "INSERT INTO paper_positions (book, ticker, source, symbol, currency, fill_date, cost_eur, "
        "net_eur, entry_close, entry_fx, reason, last_value, stop_pct, score) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (code, ticker, "SEC", ticker, "USD", fill, cost, cost * 0.998, 10.0, 1.16, "балл 64", last,
         stop, 64.0))
    conn.commit()
    return cur.lastrowid


def _add_archive(conn, n=13):
    """The old books, as an earlier database holds them."""
    for i in range(n):
        conn.execute("INSERT INTO paper_books (code, sleeve, start_date, start_eur, cash_eur, "
                     "bench_symbol) VALUES (?,?,?,?,?,?)",
                     ("R1-E1" if i == 0 else f"OLD-{i}", "stock", "2026-08-01", 80_000.0, 80_000.0,
                      "SPY"))
    conn.commit()


def test_stats_are_none_before_the_first_run(conn):
    assert paper_report.model_stats(conn, TODAY) is None
    conn.execute("INSERT INTO paper_books (code, sleeve, start_date, start_eur, cash_eur, "
                 "bench_symbol) VALUES (?,?,?,?,?,?)", (S, "stock", "2026-09-01", 70_000, 70_000, "SPY"))
    assert paper_report.model_stats(conn, TODAY) is None      # one book is not the model


def test_stats_sum_both_books_and_their_worst_drops(conn):
    _start(conn, 100)
    _seed_growth(conn)
    s = paper_report.model_stats(conn, TODAY)
    assert s["value"] == pytest.approx(102_000) and s["ret"] == pytest.approx(0.02)
    assert s["bench"] == pytest.approx(98_400) and s["bench_ret"] == pytest.approx(-0.016)
    assert s["dd"] == pytest.approx(-0.02) and s["bench_dd"] == pytest.approx(-0.095)
    assert s["day"] == 100 and s["start"] == (TODAY - dt.timedelta(days=100)).isoformat()


def test_stats_use_only_the_days_both_books_have(conn):
    _start(conn, 100)
    _equity(conn, S, [(100, 70_000, 70_000), (50, 72_000, 66_500), (0, 90_000, 90_000)])
    _equity(conn, C, [(100, 30_000, 30_000), (50, 26_000, 24_000)])      # nothing for today yet
    s = paper_report.model_stats(conn, TODAY)
    assert s["value"] == pytest.approx(98_000) and s["bench"] == pytest.approx(90_500)


def test_stats_ignore_days_after_today(conn):
    _start(conn, 100)
    _seed_growth(conn)
    _equity(conn, S, [(-1, 90_000, 90_000)])
    _equity(conn, C, [(-1, 30_000, 30_000)])
    assert paper_report.model_stats(conn, TODAY)["value"] == pytest.approx(102_000)


def test_stats_with_no_common_day_are_the_start_money_and_zeros(conn):
    _start(conn, 3)
    _equity(conn, S, [(0, 71_000, 70_500)])
    s = paper_report.model_stats(conn, TODAY)
    assert s["value"] == pytest.approx(100_000) and s["ret"] == 0 and s["dd"] == 0
    assert s["bench"] == pytest.approx(100_000) and s["bench_ret"] == 0 and s["bench_dd"] == 0


def test_stats_count_trades_and_open_positions_of_both_books(conn):
    _start(conn, 100)
    _seed_growth(conn)
    _closed_trades(conn, S, 3)
    _closed_trades(conn, C, 2)
    _open_position(conn, S, "AAA")
    _open_position(conn, C, "CRYPTO:BTC")
    _open_position(conn, "R1-E1", "OLD")            # an archived book does not count
    s = paper_report.model_stats(conn, TODAY)
    assert (s["trades"], s["open"]) == (5, 2)


def test_status_runs_until_day_182(conn):
    _start(conn, 100)
    _seed_growth(conn)
    assert paper_report.model_stats(conn, TODAY)["status"] == "идёт (день 100 из 182)"


def test_status_after_day_182_waits_for_20_trades(conn):
    _start(conn, 190)
    _seed_growth(conn)
    _closed_trades(conn, S, 5)
    assert paper_report.model_stats(conn, TODAY)["status"] == "идёт (сделок 5 из 20)"


def test_status_passes_when_it_beats_the_mix_with_a_smaller_drop(conn):
    _start(conn, 190)
    _seed_growth(conn)
    _closed_trades(conn, S, 12)
    _closed_trades(conn, C, 8)
    assert paper_report.model_stats(conn, TODAY)["status"] == "пройдено"


def test_status_fails_when_the_mix_did_better(conn):
    _start(conn, 190)
    _seed_growth(conn, bench_end=(80_000.0, 30_000.0))      # the mix ends at 110 000
    _closed_trades(conn, S, 20)
    assert paper_report.model_stats(conn, TODAY)["status"] == "не пройдено"


def test_status_fails_with_a_deeper_drop_than_the_mix(conn):
    _start(conn, 190)
    _equity(conn, S, [(100, 70_000, 70_000), (50, 50_000, 69_000), (0, 75_000, 74_000)])
    _equity(conn, C, [(100, 30_000, 30_000), (50, 30_000, 30_000), (0, 30_000, 30_000)])
    _closed_trades(conn, S, 20)
    s = paper_report.model_stats(conn, TODAY)
    assert s["ret"] > s["bench_ret"] and s["dd"] < s["bench_dd"]
    assert s["status"] == "не пройдено"


# --------------------------------------------------------------- format_summary
def test_summary_before_the_first_run(conn):
    assert (paper_report.format_summary(conn, TODAY)
            == "Модельный портфель ещё не запущен — стартует с первого ежедневного прогона.")


def test_summary_shows_the_model_against_the_mix(conn):
    _start(conn, 47)
    _seed_growth(conn)
    _closed_trades(conn, S, 3)
    lines = paper_report.format_summary(conn, TODAY).splitlines()
    assert lines[0] == "Модельный портфель — день 47 (с 19.08.2026)"
    text = "\n".join(lines)
    assert "смесь 70/30 (S&P 500 / BTC)" in text
    assert "€102 000" in text and "+2,0%" in text and "−1,6%" in text and "+3,6 п.п." in text
    assert "−2,0%" in text and "−9,5%" in text             # the worst drops
    assert "идёт (день 47 из 182)" in text
    assert "Закрытых сделок: 3 · открытых позиций: 0" in text
    assert "Статус: идёт (день 47 из 182)" in text


def test_summary_has_one_line_per_sleeve(conn):
    _start(conn, 47)
    _seed_growth(conn)
    conn.execute("UPDATE paper_books SET cash_eur = 4100 WHERE code = ?", (S,))
    conn.execute("UPDATE paper_books SET cash_eur = 1200 WHERE code = ?", (C,))
    text = paper_report.format_summary(conn, TODAY)
    stock = next(ln for ln in text.splitlines() if "MODEL-S" in ln)
    crypto = next(ln for ln in text.splitlines() if "MODEL-C" in ln)
    assert "Акции" in stock and "€74 000" in stock and "+5,7%" in stock and "S&P 500 +2,0%" in stock
    assert "свободно €4 100" in stock
    assert "Крипто" in crypto and "€28 000" in crypto and "−6,7%" in crypto and "BTC −10,0%" in crypto
    assert "свободно €1 200" in crypto


def test_summary_lists_open_positions_with_days_result_and_stop(conn):
    _start(conn, 47)
    _seed_growth(conn)
    _open_position(conn, S, "AAA", fill="2026-09-25", cost=8_000, last=8_400, stop=0.10)
    _open_position(conn, C, "CRYPTO:BTC", fill="2026-10-01", cost=5_000, last=4_750, stop=0.2)
    text = paper_report.format_summary(conn, TODAY)
    aaa = next(ln for ln in text.splitlines() if "AAA" in ln)
    btc = next(ln for ln in text.splitlines() if ln.lstrip().startswith("BTC "))
    assert "10 дн." in aaa and "+5,0%" in aaa and "стоп −10%" in aaa
    assert "4 дн." in btc and "−5,0%" in btc and "стоп −20%" in btc


def test_summary_says_when_nothing_is_open(conn):
    _start(conn, 5)
    assert "Открытых позиций нет" in paper_report.format_summary(conn, TODAY)


def test_summary_names_the_archived_books(conn):
    _start(conn, 47)
    _add_archive(conn, 13)
    text = paper_report.format_summary(conn, TODAY)
    assert text.splitlines()[-1] == "Архив: 13 прежних книг остановлены — python paper.py R1-E1"


def test_summary_without_archive_has_no_archive_line(conn):
    _start(conn, 47)
    assert "Архив" not in paper_report.format_summary(conn, TODAY)


def test_the_archive_line_uses_the_messages_plural_rule():
    import inspect
    src = inspect.getsource(paper_report)
    assert "telegram_notify._plural(" in src and "model_score._plural" not in src


def test_archive_line_agrees_with_the_count(conn):
    _start(conn, 47)
    _add_archive(conn, 1)
    assert "Архив: 1 прежняя книга остановлена" in paper_report.format_summary(conn, TODAY)


def test_archived_books_do_not_touch_the_model_numbers(conn):
    _start(conn, 47)
    _seed_growth(conn)
    _add_archive(conn, 3)
    _equity(conn, "R1-E1", [(0, 500_000, 400_000)])
    assert "€102 000" in paper_report.format_summary(conn, TODAY)


def test_monthly_summary_adds_each_sleeves_month(conn):
    model.create_books(conn, dt.date(2026, 8, 15))
    for book, rows in ((S, (("2026-08-31", 70_000, 70_000), ("2026-09-30", 71_750, 73_500))),
                       (C, (("2026-08-31", 30_000, 30_000), ("2026-09-30", 30_000, 30_000)))):
        for day, value, bench in rows:
            conn.execute("INSERT INTO paper_equity (book, date, value, cash, bench) "
                         "VALUES (?,?,?,?,?)", (book, day, value, 0.0, bench))
    conn.commit()
    plain = paper_report.format_summary(conn, TODAY)
    assert "за месяц" not in plain
    text = paper_report.format_summary(conn, TODAY, monthly=True)
    stock = next(ln for ln in text.splitlines() if "MODEL-S" in ln)
    crypto = next(ln for ln in text.splitlines() if "MODEL-C" in ln)
    assert "за месяц +2,5%" in stock and "S&P 500 +5,0%" in stock
    assert "за месяц +0,0%" in crypto


def test_html_summary_is_bold_headed_with_escaped_rows_in_pre(conn):
    _start(conn, 47)
    _seed_growth(conn)
    _open_position(conn, S, "A&B")
    text = paper_report.format_summary(conn, TODAY, html=True)
    assert text.startswith("<b>Модельный портфель — день 47 (с 19.08.2026)</b>")
    assert "<pre>" in text and text.count("<pre>") == text.count("</pre>")
    assert "S&amp;P 500" in text and "A&amp;B" in text and "A&B" not in text
    assert "S&P" not in text


# ------------------------------------------------------------ the weekly signals and summary
FRI = dt.date(2026, 10, 9)           # a weekly run's day: the week is 03.10 - 09.10
_TAGS = re.compile(r"</?[a-zA-Z][^>]*>")


def _week_model(conn, *, equity=True):
    """The model, started three weeks before FRI. The books together: 100 000 -> 105 000 a week
    ago -> 107 100 now (the week +2,0%, since the start +7,1%); the mix: 100 000 -> 100 800 now."""
    model.create_books(conn, FRI - dt.timedelta(days=21))
    if not equity:
        return
    for code, rows in ((S, (("2026-09-18", 70_000, 70_000), ("2026-10-02", 73_500, 71_000),
                            ("2026-10-09", 75_000, 72_000))),
                       (C, (("2026-09-18", 30_000, 30_000), ("2026-10-02", 31_500, 28_000),
                            ("2026-10-09", 32_100, 28_800)))):
        for day, value, bench in rows:
            conn.execute("INSERT INTO paper_equity (book, date, value, cash, bench) VALUES (?,?,?,?,?)",
                         (code, day, value, 0.0, bench))
    conn.commit()


def _order(conn, book, ticker, *, created="2026-10-09", status="pending", side="buy", amount=5_355.0,
           reason="балл 64: 3 инсайдера; CEO среди покупателей", stop=0.10, score=64.0, position_id=None):
    """An order of the book; returns its id."""
    cur = conn.execute(
        "INSERT INTO paper_orders (book, ticker, source, side, amount_eur, position_id, reason, created, "
        "status, insiders, stop_pct, score) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (book, ticker, "SEC", side, amount if side == "buy" else None, position_id, reason, created,
         status, "[]", stop, score))
    conn.commit()
    return cur.lastrowid


def _closed(conn, book, ticker, *, closed, fill="2026-09-20", cost=8_000.0, proceeds=8_400.0,
            reason="стоп: −10% от максимума"):
    """A closed position of the book; returns its id."""
    cur = conn.execute(
        "INSERT INTO paper_positions (book, ticker, source, symbol, currency, fill_date, cost_eur, "
        "net_eur, entry_close, entry_fx, closed_date, close_reason, proceeds_eur, last_value) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (book, ticker, "SEC", ticker, "USD", fill, cost, cost, 10.0, 1.16, closed, reason, proceeds, proceeds))
    conn.commit()
    return cur.lastrowid


def _exit_row(conn, ticker, company, sellers, *, emitted="2026-10-08 12:00:00", kind="exit"):
    """A journal row; returns its id."""
    db.journal_signal(conn, {"source": "SEC", "kind": kind, "ticker": ticker, "company": company,
                             "members": json.dumps(sellers, ensure_ascii=False)})
    conn.execute("UPDATE signal_journal SET emitted_at = ? WHERE id = (SELECT MAX(id) FROM signal_journal)",
                 (emitted,))
    conn.commit()
    return conn.execute("SELECT MAX(id) FROM signal_journal").fetchone()[0]


def _signals(conn, report=None):
    return paper_report.week_signals(conn, FRI, report)


def _texts(conn, report=None):
    return [text for _key, text in _signals(conn, report)]


def _summary(conn, report=None, **kw):
    return paper_report.format_week_summary(conn, FRI, report, html=False, **kw)


def test_before_the_model_starts_there_are_no_signals_and_the_summary_says_so(conn):
    _exit_row(conn, "ZZZ", "Exit Corp", ["A", "B"])
    assert paper_report.week_signals(conn, FRI, None) == []
    assert (paper_report.format_week_summary(conn, FRI, None)
            == "Модельный портфель ещё не запущен — стартует с первого ежедневного прогона.")


# ---- 1. a buy
def test_a_buy_is_one_message_with_its_reasons_score_and_stop(conn):
    _week_model(conn)
    oid = _order(conn, S, "AAA", reason="балл 70: 2 инсайдера из руководства; CEO среди покупателей",
                 score=70.0, stop=0.10)
    assert _signals(conn) == [(f"buy:{oid}", "🟢 <b>AAA!</b>: покупка — 2 инсайдера из руководства; "
                                             "CEO среди покупателей; балл 70, стоп −10%")]


def test_a_buy_shows_at_most_two_reasons_and_no_money_or_peak(conn):
    _week_model(conn)
    _order(conn, S, "AAA", reason="балл 64: 3 инсайдера; CEO среди покупателей; первая покупка", amount=5_355.0)
    [text] = _texts(conn)
    assert text == "🟢 <b>AAA!</b>: покупка — 3 инсайдера; CEO среди покупателей; балл 64, стоп −10%"
    assert "€" not in text and "максимума" not in text and "в модели" not in text and "первая" not in text


@pytest.mark.parametrize("reason, score, stop, details", [
    ("балл 64: 3 инсайдера", 64.0, 0.10, "3 инсайдера; балл 64, стоп −10%"),
    ("балл 64: ", 64.0, 0.15, "балл 64, стоп −15%"),                       # no reason: the score opens it
    ("3 инсайдера", None, 0.10, "3 инсайдера, стоп −10%"),                 # an order without a score
    ("балл 64: 3 инсайдера", 64.0, None, "3 инсайдера; балл 64"),          # an order without a stop
    ("", None, None, None)])
def test_a_buy_leaves_out_what_the_order_does_not_have(conn, reason, score, stop, details):
    _week_model(conn)
    _order(conn, S, "AAA", reason=reason, score=score, stop=stop)
    [text] = _texts(conn)
    assert text == "🟢 <b>AAA!</b>: покупка" + (f" — {details}" if details else "")


def test_a_pending_buy_adds_nothing_and_a_filled_one_its_entry(conn):
    _week_model(conn)
    _order(conn, S, "PPP", created="2026-10-09", status="pending", score=70.0)
    _order(conn, S, "AAA", created="2026-10-06", status="filled", score=64.0)
    _open_position(conn, S, "AAA", fill="2026-10-07")
    _closed(conn, S, "AAA", closed="2026-09-01", fill="2026-08-01")      # an earlier holding of it: not the fill
    texts = _texts(conn)
    assert texts == ["🟢 <b>PPP!</b>: покупка — 3 инсайдера; CEO среди покупателей; балл 70, стоп −10%",
                     "🟢 <b>AAA!</b>: покупка — 3 инсайдера; CEO среди покупателей; балл 64, стоп −10%, "
                     "вход 07.10 по 10,00"]


def test_a_buy_the_model_found_not_on_trading_212_says_so(conn):
    _week_model(conn)
    _order(conn, S, "AAA", status="filled", created="2026-10-06")
    _open_position(conn, S, "AAA", fill="2026-10-07")
    _order(conn, S, "BBB")
    _order(conn, S, "CCC")
    model.keep_scores(conn, FRI, [_stock("AAA", 64.0, t212=False), _stock("BBB", 64.0, t212=True)])
    by = {t.split("!")[0].removeprefix("🟢 <b>"): t for t in _texts(conn)}
    assert by["AAA"].endswith("балл 64, стоп −10%, нет на Trading 212, вход 07.10 по 10,00")   # before the entry
    assert "Trading 212" not in by["BBB"] and "Trading 212" not in by["CCC"]      # known to be there, or unknown


def test_the_trading_212_label_is_read_from_the_weeks_scores_and_todays_report(conn):
    _week_model(conn)
    _order(conn, S, "OLD", created="2026-10-06")
    _order(conn, S, "NEW", created="2026-10-09")
    model.keep_scores(conn, dt.date(2026, 10, 6), [_stock("OLD", 64.0, t212=False)])    # another day of the week
    model.keep_scores(conn, dt.date(2026, 9, 1), [_stock("NEW", 64.0, t212=False)])     # not this week
    assert "нет на Trading 212" in _texts(conn)[0] and "Trading 212" not in _texts(conn)[1]
    texts = _texts(conn, _report([_stock("NEW", 64.0, t212=False)]))                    # today's report counts
    assert "нет на Trading 212" in texts[0] and "нет на Trading 212" in texts[1]


def test_a_coin_buy_is_named_by_its_symbol_and_the_buys_go_in_score_order_across_the_books(conn):
    _week_model(conn)
    first = _order(conn, S, "AAA", score=64.0, reason="балл 64: a")
    second = _order(conn, C, "CRYPTO:BTC", score=75.0, stop=0.15, reason="балл 75: выше 100-дн. средней")
    third = _order(conn, S, "BBB", score=70.0, reason="балл 70: b")
    signals = _signals(conn)
    assert [k for k, _t in signals] == [f"buy:{second}", f"buy:{third}", f"buy:{first}"]
    assert signals[0][1] == "🟢 <b>BTC!</b>: покупка — выше 100-дн. средней; балл 75, стоп −15%"
    assert "CRYPTO" not in signals[0][1]


def test_only_buy_orders_created_in_the_week_that_went_through_are_signals(conn):
    _week_model(conn)
    _order(conn, S, "EDGE", created="2026-10-03")                       # the week's first day
    _order(conn, S, "OLD", created="2026-10-02")                        # a day before it
    _order(conn, S, "SKIP", created="2026-10-08", status="skipped")
    _order(conn, S, "GONE", created="2026-10-08", status="cancelled")
    _order(conn, S, "SELL", created="2026-10-08", side="sell", status="filled")    # a sale, not a buy
    [text] = _texts(conn)
    assert "EDGE" in text and not any(n in text for n in ("OLD", "SKIP", "GONE", "SELL"))


# ---- 2. a sale, executed or still waiting
def test_a_sale_is_one_message_with_the_event_the_day_and_the_result_in_percent(conn):
    _week_model(conn)
    won = _closed(conn, S, "CCC", closed="2026-10-07", cost=8_000.0, proceeds=8_400.0)
    lost = _closed(conn, C, "CRYPTO:ETH", closed="2026-10-03", cost=5_000.0, proceeds=4_750.0, reason="тренд вниз")
    _closed(conn, S, "OLD", closed="2026-10-02")                          # closed before the week
    assert _signals(conn) == [
        (f"sell:{lost}", "🔴 <b>ETH!</b>: тренд развернулся вниз — продано 03.10, итог <b>−5,0%</b>"),
        (f"sell:{won}", "🟢 <b>CCC!</b>: сработал стоп −10% — продано 07.10, итог <b>+5,0%</b>")]   # as they closed


def test_a_sale_shows_no_virtual_money(conn):
    _week_model(conn)
    _closed(conn, S, "CCC", closed="2026-10-07", cost=8_000.0, proceeds=7_000.0)
    [text] = _texts(conn)
    assert text == "🔴 <b>CCC!</b>: сработал стоп −10% — продано 07.10, итог <b>−12,5%</b>"
    assert "€" not in text and "(" not in text and "максимума" not in text


@pytest.mark.parametrize("reason, event", [
    ("стоп: −10% от максимума", "сработал стоп −10%"), ("стоп: −15% от максимума", "сработал стоп −15%"),
    ("стоп", "сработал стоп"), ("продаёт инсайдер: Ann Lee — Form 4, 2026-10-01", "продаёт инсайдер"),
    ("активист сократил долю", "активист сократил долю"), ("стоит на месте", "стоит на месте"),
    ("год в позиции", "год в позиции"), ("новости: fraud", "плохие новости"),
    ("тренд вниз", "тренд развернулся вниз"),
    ("осторожно: отток из спот-ETF (€900 млн); цена подтверждает: −6% за 7 дн.", "отток по монете"),
    (None, "продажа"), ("что-то новое", "что-то новое")])
def test_a_model_sales_reason_is_worded_as_an_event(conn, reason, event):
    _week_model(conn)
    _closed(conn, S, "CCC", closed="2026-10-07", reason=reason)
    [text] = _texts(conn)
    assert text.startswith(f"🟢 <b>CCC!</b>: {event} — продано 07.10, итог ")


def test_a_sale_at_the_last_price_for_want_of_quotes_says_so(conn):
    _week_model(conn)
    _closed(conn, S, "CCC", closed="2026-10-07",
            reason="стоп: −10% от максимума (по последней цене: нет котировок)")
    assert _texts(conn) == ["🟢 <b>CCC!</b>: сработал стоп −10% — продано 07.10 по последней цене, "
                            "итог <b>+5,0%</b>"]


def test_a_sale_with_no_proceeds_has_no_result_and_a_red_dot(conn):
    _week_model(conn)
    _closed(conn, S, "CCC", closed="2026-10-07", proceeds=None)
    assert _texts(conn) == ["🔴 <b>CCC!</b>: сработал стоп −10% — продано 07.10"]


def test_a_sale_still_waiting_shows_how_it_stands_now_and_its_dot_follows_the_sign(conn):
    _week_model(conn)
    up = _open_position(conn, S, "DDD", fill="2026-09-25", cost=8_000.0, last=8_160.0)
    down = _open_position(conn, S, "EEE", fill="2026-09-25", cost=8_000.0, last=7_200.0)
    blind = _open_position(conn, S, "FFF", fill="2026-09-25", cost=8_000.0, last=None)
    a = _order(conn, S, "DDD", side="sell", position_id=up, reason="новости: fraud", status="pending")
    b = _order(conn, S, "EEE", side="sell", position_id=down, reason="стоп: −10% от максимума", status="pending")
    c = _order(conn, S, "FFF", side="sell", position_id=blind, reason="год в позиции", status="pending")
    _order(conn, S, "GGG", side="sell", position_id=up, reason="год в позиции", status="filled")   # not waiting
    assert _signals(conn) == [
        (f"sellpending:{a}", "🟢 <b>DDD!</b>: плохие новости — продажа по ближайшему закрытию, "
                             "сейчас <b>+2,0%</b>"),
        (f"sellpending:{b}", "🔴 <b>EEE!</b>: сработал стоп −10% — продажа по ближайшему закрытию, "
                             "сейчас <b>−10,0%</b>"),
        (f"sellpending:{c}", "🔴 <b>FFF!</b>: год в позиции — продажа по ближайшему закрытию")]


def test_a_pending_sale_has_no_virtual_money_either(conn):
    _week_model(conn)
    pid = _open_position(conn, S, "DDD", fill="2026-09-25", cost=8_000.0, last=7_020.0)
    _order(conn, S, "DDD", side="sell", position_id=pid, reason="стоп: −10% от максимума", status="pending")
    [text] = _texts(conn)
    assert "€" not in text and "(" not in text and "максимума" not in text


# ---- 4. a group exit
def test_group_exits_of_the_week_are_one_message_each_naming_the_sellers(conn):
    _week_model(conn)
    zzz = _exit_row(conn, "ZZZ", "Exit Corp", ["Ann Lee", "Bo Chen"], emitted="2026-10-08 12:00:00")
    yyy = _exit_row(conn, "YYY", "Early Inc", ["C D"], emitted="2026-10-03 12:00:00")       # the week's first day
    _exit_row(conn, "OLD", "Old Inc", ["E F"], emitted="2026-10-02 12:00:00")               # before the week
    _exit_row(conn, "CLU", "Cluster Inc", ["G H"], emitted="2026-10-08 12:00:00", kind="cluster")
    assert _signals(conn) == [(f"exit:{zzz}", "🔴 <b>ZZZ!</b>: продают те, кто покупал — Ann Lee, Bo Chen"),
                              (f"exit:{yyy}", "🔴 <b>YYY!</b>: продают те, кто покупал — C D")]   # by journal id


def test_a_ticker_that_exited_twice_in_a_week_is_one_signal_with_its_latest_sellers(conn):
    _week_model(conn)
    _exit_row(conn, "ZZZ", "Exit Corp", ["A"], emitted="2026-10-06 12:00:00")
    latest = _exit_row(conn, "ZZZ", "Exit Corp", ["A", "B"], emitted="2026-10-08 12:00:00")
    assert _signals(conn) == [(f"exit:{latest}", "🔴 <b>ZZZ!</b>: продают те, кто покупал — A, B")]


def test_a_long_seller_list_is_cut_and_one_with_no_names_has_no_details(conn):
    _week_model(conn)
    _exit_row(conn, "ZZZ", "Exit Corp", [f"P{i}" for i in range(9)])
    _exit_row(conn, "YYY", "Nameless", [])
    texts = _texts(conn)
    assert texts[0] == "🔴 <b>ZZZ!</b>: продают те, кто покупал — P0, P1, P2, P3, P4 и ещё 4"
    assert texts[1] == "🔴 <b>YYY!</b>: продают те, кто покупал"


def test_the_signals_come_in_order_buys_sales_pending_sales_exits(conn):
    _week_model(conn)
    pid = _open_position(conn, S, "DDD", fill="2026-09-25")
    _exit_row(conn, "ZZZ", "Exit Corp", ["A"])
    _order(conn, S, "DDD", side="sell", position_id=pid, reason="год в позиции", status="pending")
    _closed(conn, S, "CCC", closed="2026-10-07")
    _order(conn, S, "AAA")
    keys = [k.split(":")[0] for k, _t in _signals(conn)]
    assert keys == ["buy", "sell", "sellpending", "exit"]


def test_the_signal_keys_are_stable_from_one_call_to_the_next(conn):
    _week_model(conn)
    _order(conn, S, "AAA")
    _closed(conn, S, "CCC", closed="2026-10-07")
    assert _signals(conn) == _signals(conn)
    assert all(re.fullmatch(r"(buy|sell|sellpending|exit):\d+", k) for k, _t in _signals(conn))


def test_a_quiet_week_has_no_signals(conn):
    _week_model(conn)
    assert _signals(conn) == []


def test_every_signal_is_one_line_in_html_with_only_bold_and_every_dynamic_part_escaped(conn):
    _week_model(conn)
    _order(conn, S, "A&B", reason="балл 64: <b>bold</b> & co")
    _closed(conn, S, "C<C", closed="2026-10-07", reason="продаёт <инсайдер>: x")
    pid = _open_position(conn, S, "H&H", fill="2026-10-05")
    _order(conn, S, "H&H", side="sell", position_id=pid, reason="что-то <i>новое</i>", status="pending")
    _exit_row(conn, "Z&Z", "Evil <script>alert(1)</script> & Co", ["Ann <i>Lee</i>", "B&B"])
    texts = _texts(conn)
    joined = "\n".join(texts)
    for dynamic in ("A&amp;B", "&lt;b&gt;bold&lt;/b&gt; &amp; co", "C&lt;C", "H&amp;H",
                    "что-то &lt;i&gt;новое&lt;/i&gt;", "Z&amp;Z", "Ann &lt;i&gt;Lee&lt;/i&gt;", "B&amp;B"):
        assert dynamic in joined
    assert len(texts) == 4 and all("\n" not in t for t in texts)
    assert all(set(_TAGS.findall(t)) == {"<b>", "</b>"} for t in texts)


# ---- 7. the weekly summary
def test_the_summary_opens_with_the_week_the_value_the_return_the_week_and_the_mix(conn):
    _week_model(conn)
    assert _summary(conn).splitlines()[0] == (
        "📊 Модель, неделя 03.10–09.10: €107 100 (+7,1% с начала, за неделю +2,0%); смесь 70/30 +0,8%")
    html = paper_report.format_week_summary(conn, FRI, None).splitlines()[0]
    assert html == ("📊 <b>Модель, неделя 03.10–09.10</b>: €107 100 (+7,1% с начала, за неделю +2,0%); "
                    "смесь 70/30 +0,8%")


def test_the_week_return_uses_the_last_value_on_or_before_a_week_ago(conn):
    _week_model(conn)
    conn.execute("DELETE FROM paper_equity WHERE date = '2026-10-02'")
    for code, value in ((S, 73_500), (C, 31_500)):                       # the Thursday before it
        conn.execute("INSERT INTO paper_equity (book, date, value, cash, bench) VALUES (?,?,?,?,?)",
                     (code, "2026-10-01", value, 0.0, None))
    conn.commit()
    assert "за неделю +2,0%" in _summary(conn).splitlines()[0]


def test_with_no_value_a_week_ago_the_week_return_is_left_out(conn):
    _week_model(conn)
    conn.execute("DELETE FROM paper_equity WHERE date <= '2026-10-02'")  # nothing on or before a week ago
    assert "за неделю" not in _summary(conn).splitlines()[0]


def test_the_first_line_leaves_out_what_it_cannot_compute(conn):
    _week_model(conn)
    conn.execute("UPDATE paper_equity SET bench = NULL")
    conn.execute("DELETE FROM paper_equity WHERE date <= '2026-10-02'")
    assert _summary(conn).splitlines()[0] == "📊 Модель, неделя 03.10–09.10: €107 100 (+7,1% с начала)"


def test_the_week_return_needs_both_books(conn):
    _week_model(conn)
    conn.execute("DELETE FROM paper_equity WHERE date <= '2026-10-02' AND book = ?", (C,))
    assert "за неделю" not in _summary(conn).splitlines()[0]


def test_the_summary_lists_what_is_held_with_its_result_best_first(conn):
    _week_model(conn)
    _open_position(conn, S, "AAA", fill="2026-10-05", cost=8_000, last=8_400)
    _open_position(conn, C, "CRYPTO:BTC", fill="2026-10-08", cost=5_000, last=4_750)
    _open_position(conn, S, "ZZZ", fill="2026-10-05", cost=1_000, last=1_012)
    assert _summary(conn).splitlines()[1] == "В портфеле (3): AAA +5,0%, ZZZ +1,2%, BTC −5,0%"
    assert "стоп" not in _summary(conn) and "CRYPTO:" not in _summary(conn)


def test_nothing_held_reads_all_in_cash(conn):
    _week_model(conn)
    assert _summary(conn).splitlines()[1] == "В портфеле: пусто — всё в деньгах"


def test_the_summary_counts_the_weeks_signals(conn):
    _week_model(conn)
    assert _summary(conn).splitlines()[2] == "Сигналов за неделю не было."
    _order(conn, S, "AAA")
    _order(conn, S, "BBB")
    _closed(conn, S, "CCC", closed="2026-10-07")
    assert _summary(conn).splitlines()[2] == "Сигналов за неделю: покупок 2, продаж 1"


def test_a_sale_waiting_counts_as_a_sale_and_a_group_exit_is_counted_apart(conn):
    _week_model(conn)
    pid = _open_position(conn, S, "DDD", fill="2026-09-25")
    _order(conn, S, "DDD", side="sell", position_id=pid, reason="год в позиции", status="pending")
    _closed(conn, S, "CCC", closed="2026-10-07")
    assert _summary(conn).splitlines()[2] == "Сигналов за неделю: покупок 0, продаж 2"
    _exit_row(conn, "ZZZ", "Exit Corp", ["A", "B"])
    assert _summary(conn).splitlines()[2] == "Сигналов за неделю: покупок 0, продаж 2, групповых выходов 1"


def test_a_week_with_only_a_group_exit_is_not_a_week_without_signals(conn):
    _week_model(conn)
    _exit_row(conn, "ZZZ", "Exit Corp", ["A", "B"])
    assert "не было" not in _summary(conn)


def test_the_summary_is_one_short_message_without_the_old_sections(conn):
    _week_model(conn)
    _order(conn, S, "AAA")
    _open_position(conn, S, "HHH", fill="2026-10-05")
    text = paper_report.format_week_summary(conn, FRI, _report([_stock("WAT", 52.0, model_score.WATCH)]))
    assert "\n\n" not in text and len(text.splitlines()) == 3
    for gone in ("Покупки", "Продажи", "👀", "🚨", "📋", "Наблюдение", "WAT", "Портфель:"):
        assert gone not in text


def test_a_failed_model_adds_the_warning_as_the_last_line(conn):
    _week_model(conn)
    assert _summary(conn, model_failed=True).splitlines()[-1] == (
        "⚠️ Модель на этой неделе не отработала — покупок не было.")
    assert paper_report.MODEL_FAILED_WARNING in paper_report.format_week_summary(conn, FRI, None, model_failed=True)
    assert "⚠️" not in _summary(conn) and "⚠️" not in _summary(conn, model_failed=False)


def test_the_summary_html_escapes_the_names_and_uses_only_bold(conn):
    _week_model(conn)
    _open_position(conn, S, "H&H<i>", fill="2026-10-05")
    text = paper_report.format_week_summary(conn, FRI, None)
    assert "H&amp;H&lt;i&gt; +5,0%" in text and set(_TAGS.findall(text)) == {"<b>", "</b>"}
    assert not _TAGS.search(_summary(conn).replace("H&H<i>", ""))             # plain: no tags at all


# ------------------------------------------------- the Trading 212 line of the weekly summary
def _t212_equity(conn, rows, currency="EUR"):
    """The account's daily snapshots as a sync stores them: rows [(ISO date, total value)]."""
    for day, value in rows:
        conn.execute("INSERT OR REPLACE INTO t212_equity (date, total_value, invested_value, invested_cost, "
                     "cash_free, currency) VALUES (?,?,?,?,?,?)", (day, value, None, None, None, currency))
    conn.commit()


def _lines(conn, **kw):
    return _summary(conn, **kw).splitlines()


def test_the_summary_shows_your_trading_212_account_as_a_line_of_its_own(conn):
    _week_model(conn)
    _t212_equity(conn, [("2026-10-02", 12_000.0), ("2026-10-09", 12_345.67)])
    assert _lines(conn)[3] == "Ваш счёт Trading 212: €12 346 (за неделю +2,9%)"
    assert len(_lines(conn)) == 4


def test_the_account_is_valued_on_or_before_today_and_a_week_before(conn):
    """Today is FRI 09.10: the last snapshot on or before it is of 07.10, and the week is measured
    from the last one on or before 02.10, which is of 30.09."""
    _week_model(conn)
    _t212_equity(conn, [("2026-09-30", 10_000.0), ("2026-10-07", 9_000.0), ("2026-10-10", 99_999.0)])
    assert "Ваш счёт Trading 212: €9 000 (за неделю −10,0%)" in _lines(conn)


@pytest.mark.parametrize("rows", [[("2026-10-08", 12_000.0)],                           # nothing that early
                                  [("2026-10-01", 0.0), ("2026-10-08", 12_000.0)]])     # nothing to compare with
def test_the_week_change_is_left_out_when_it_is_not_known(conn, rows):
    _week_model(conn)
    _t212_equity(conn, rows)
    assert _lines(conn)[3] == "Ваш счёт Trading 212: €12 000"


def test_the_trading_212_line_is_left_out_when_the_account_was_last_read_more_than_three_days_ago(conn):
    """The sync has not got through: a value of last week is not «ваш счёт» today."""
    _week_model(conn)
    _t212_equity(conn, [("2026-10-05", 12_000.0)])                 # FRI is 09.10: four days old
    assert "Trading 212" not in _summary(conn)
    _t212_equity(conn, [("2026-10-06", 12_500.0)])                 # three days old: still the account
    assert "Ваш счёт Trading 212: €12 500" in _lines(conn)


def test_the_week_change_needs_a_snapshot_from_about_a_week_ago(conn):
    """The only earlier snapshot is twelve days before 02.10: the change since then is not «за неделю»."""
    _week_model(conn)
    _t212_equity(conn, [("2026-09-20", 10_000.0), ("2026-10-09", 12_000.0)])
    assert "Ваш счёт Trading 212: €12 000" in _lines(conn)
    _t212_equity(conn, [("2026-09-29", 10_000.0)])                 # three days before 02.10: near enough
    assert "Ваш счёт Trading 212: €12 000 (за неделю +20,0%)" in _lines(conn)


def test_without_account_data_there_is_no_trading_212_line(conn):
    _week_model(conn)
    assert "Trading 212" not in _summary(conn)
    _t212_equity(conn, [("2026-10-10", 12_000.0)])                 # only a day after today
    assert "Trading 212" not in _summary(conn)
    conn.execute("INSERT INTO t212_equity (date, total_value) VALUES ('2026-10-05', NULL)")
    assert "Trading 212" not in _summary(conn)


def test_the_trading_212_line_stands_before_the_model_warning(conn):
    _week_model(conn)
    _t212_equity(conn, [("2026-10-09", 12_000.0)])
    lines = _lines(conn, model_failed=True)
    assert lines[-2] == "Ваш счёт Trading 212: €12 000" and lines[-1].startswith("⚠️ Модель")


def test_the_trading_212_line_is_plain_text_in_html_and_names_another_currency(conn):
    _week_model(conn)
    _t212_equity(conn, [("2026-10-02", 10_000.0), ("2026-10-09", 12_000.0)], currency="GBP")
    text = paper_report.format_week_summary(conn, FRI, None)
    assert "\nВаш счёт Trading 212: £12 000 (за неделю +20,0%)" in text
    assert set(_TAGS.findall(text)) == {"<b>", "</b>"}


# ---------------------------------------------------------------------- the menu
def test_menu_signals_print_the_scored_list(conn, capsys, monkeypatch):
    import menu

    monkeypatch.setattr(model, "score_today", lambda c, *a, **k: [_stock("AAA", 64.0), _coin()])
    monkeypatch.setattr("positions.check_exits", lambda c, *a, **k: [_close("CCC")])
    monkeypatch.setattr("positions.last_close", lambda ticker, source=None: 110.0)
    monkeypatch.setattr("prices._closes", lambda symbol, days: [])        # no history to size a stop from
    positions.open_position(conn, "AAA", 100.0)
    menu.show_signals(conn)
    out = capsys.readouterr().out
    assert "СИГНАЛЫ — оценка модели (покупка от 60, наблюдение 45–59)" in out
    assert "🟢 AAA" in out and "CRYPTO:BTC" in out
    assert "🔴 CCC!: сработал стоп — пора продавать" in out     # the pending close alert
    assert "Открытые позиции" in out                            # the /bought positions


def test_menu_prices_a_trading_212_holding_from_its_stored_day_price(conn, capsys, monkeypatch):
    """A holding with no Yahoo listing (keyed by its ISIN) has the sync's prices, also here."""
    import menu

    today = dt.date.today().isoformat()
    monkeypatch.setattr(model, "score_today", lambda c, *a, **k: [])
    monkeypatch.setattr("positions.check_exits", lambda c, *a, **k: [])
    monkeypatch.setattr("positions.last_close", lambda ticker, source=None: None)     # Yahoo has no ISIN
    conn.execute("INSERT INTO positions (ticker, source, opened_at, entry_price, origin, quantity, t212_ticker, "
                 "currency) VALUES ('DE0007164600', 'T212', ?, 100.0, 't212', 5, 'SAPd_EQ', 'EUR')", (today,))
    conn.execute("INSERT INTO t212_prices (ticker, date, price) VALUES ('DE0007164600', ?, 110.0)", (today,))
    conn.commit()
    menu.show_signals(conn)
    out = capsys.readouterr().out
    assert "• SAP: вход 100.00, сейчас 110.00 (+10.0%)" in out and "цена недоступна" not in out
    assert "DE0007164600" not in out                               # by the name its owner knows


def test_menu_signals_survive_a_failing_score(conn, capsys, monkeypatch):
    import menu

    def fail(c, *a, **k):
        raise RuntimeError("offline")
    monkeypatch.setattr(model, "score_today", fail)
    monkeypatch.setattr("positions.check_exits", lambda c, *a, **k: [])
    menu.show_signals(conn)
    assert "offline" in capsys.readouterr().out


def test_menu_signals_use_todays_kept_scores_without_scoring_again(conn, capsys, monkeypatch):
    import menu

    model.keep_scores(conn, dt.date.today(), [_stock("KEPT", 64.0), _coin()])
    monkeypatch.setattr(model, "score_today", lambda *a, **k: 1 / 0)
    monkeypatch.setattr("positions.check_exits", lambda c, *a, **k: [])
    menu.show_signals(conn)
    out = capsys.readouterr().out
    assert "🟢 KEPT 64" in out and "CRYPTO:BTC" in out
    assert "ZeroDivisionError" not in out and "Считаю оценки" not in out


def test_menu_signals_score_afresh_when_only_another_days_scores_are_kept(conn, capsys, monkeypatch):
    import menu

    model.keep_scores(conn, dt.date.today() - dt.timedelta(days=1), [_stock("OLD", 64.0)])
    monkeypatch.setattr(model, "score_today", lambda c, *a, **k: [_stock("NEW", 61.0)])
    monkeypatch.setattr("positions.check_exits", lambda c, *a, **k: [])
    menu.show_signals(conn)
    out = capsys.readouterr().out
    assert "NEW 61" in out and "OLD" not in out and "Считаю оценки" in out


def test_menu_shows_the_model_summary_and_where_to_look_further(conn, capsys):
    import menu
    model.create_books(conn, dt.date.today() - dt.timedelta(days=10))     # the menu reads the clock
    menu.show_paper(conn)
    out = capsys.readouterr().out
    assert "Модельный портфель — день 10" in out
    assert "Подробно: python paper.py MODEL-S (или MODEL-C, R1-E1 …)" in out


def test_menu_labels():
    import inspect

    import menu
    src = inspect.getsource(menu.main)
    assert '"1) Сигналы"' in src and '"3) Модельный портфель"' in src
    assert "Бумажный портфель" not in src

