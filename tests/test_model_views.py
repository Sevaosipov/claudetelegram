"""The model portfolio's views: the weekly Telegram message, the scored list, the summary
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
    """The weekly message reads journal timestamps (stored in UTC) as local dates; the tests
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


# ----------------------------------------------------------------- format_week
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
    conn.execute(
        "INSERT INTO paper_orders (book, ticker, source, side, amount_eur, position_id, reason, created, "
        "status, insiders, stop_pct, score) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (book, ticker, "SEC", side, amount if side == "buy" else None, position_id, reason, created,
         status, "[]", stop, score))
    conn.commit()


def _closed(conn, book, ticker, *, closed, fill="2026-09-20", cost=8_000.0, proceeds=8_400.0,
            reason="стоп: −10% от максимума"):
    cur = conn.execute(
        "INSERT INTO paper_positions (book, ticker, source, symbol, currency, fill_date, cost_eur, "
        "net_eur, entry_close, entry_fx, closed_date, close_reason, proceeds_eur, last_value) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (book, ticker, "SEC", ticker, "USD", fill, cost, cost, 10.0, 1.16, closed, reason, proceeds, proceeds))
    conn.commit()
    return cur.lastrowid


def _exit_row(conn, ticker, company, sellers, *, emitted="2026-10-08 12:00:00", kind="exit"):
    db.journal_signal(conn, {"source": "SEC", "kind": kind, "ticker": ticker, "company": company,
                             "members": json.dumps(sellers, ensure_ascii=False)})
    conn.execute("UPDATE signal_journal SET emitted_at = ? WHERE id = (SELECT MAX(id) FROM signal_journal)",
                 (emitted,))
    conn.commit()


def _week(conn, report=None, **kw):
    return paper_report.format_week(conn, FRI, report, html=False, **kw)


def _section(text: str, header: str) -> list[str]:
    """The lines of the block that starts with `header` (blocks are separated by a blank line)."""
    for block in text.split("\n\n"):
        if block.startswith(header):
            return block.splitlines()
    raise AssertionError(f"no section {header!r} in:\n{text}")


def test_the_week_before_the_model_starts_says_so(conn):
    assert (paper_report.format_week(conn, FRI, None)
            == "Модельный портфель ещё не запущен — стартует с первого ежедневного прогона.")


def test_the_week_message_is_headed_by_the_week_in_bold(conn):
    _week_model(conn)
    assert _week(conn).splitlines()[0] == "📊 Модельный портфель — неделя 03.10–09.10"
    assert paper_report.format_week(conn, FRI, None).startswith(
        "<b>📊 Модельный портфель — неделя 03.10–09.10</b>")


def test_the_weeks_buys_come_from_both_books_with_amount_share_stop_score_and_reason(conn):
    _week_model(conn)
    _order(conn, S, "AAA", created="2026-10-09", amount=5_355.0)
    _order(conn, C, "CRYPTO:BTC", created="2026-10-06", amount=3_210.0, stop=0.15, score=75.0,
           reason="балл 75: выше 100-дн. средней")
    buys = _section(_week(conn), "🟢 Покупки")
    assert buys[0] == "🟢 Покупки"
    assert buys[1] == "• AAA: €5 355 (5,0% портфеля), стоп −10% от максимума, балл 64"
    assert buys[2] == "   3 инсайдера; CEO среди покупателей"         # the order's reason, its «балл» is above
    assert buys[3] == "   исполнится по закрытию ближайшего торгового дня"
    assert buys[4] == "• BTC: €3 210 (3,0% портфеля), стоп −15% от максимума, балл 75"   # a coin by its symbol
    assert buys[5] == "   выше 100-дн. средней"


def test_a_filled_buy_shows_its_fill_date_and_entry_close(conn):
    _week_model(conn)
    _order(conn, S, "AAA", created="2026-10-06", status="filled")
    _open_position(conn, S, "AAA", fill="2026-10-07")
    _closed(conn, S, "AAA", closed="2026-09-01", fill="2026-08-01")      # an earlier holding of it: not the fill
    buys = _section(_week(conn), "🟢 Покупки")
    assert "   исполнен 07.10 по закрытию 10.00 USD" in buys
    assert not any("исполнится" in ln for ln in buys)


def test_only_buy_orders_created_in_the_week_that_went_through_are_listed(conn):
    _week_model(conn)
    _order(conn, S, "EDGE", created="2026-10-03")                       # the week's first day
    _order(conn, S, "OLD", created="2026-10-02")                        # a day before it
    _order(conn, S, "SKIP", created="2026-10-08", status="skipped")
    _order(conn, S, "GONE", created="2026-10-08", status="cancelled")
    _order(conn, S, "SELL", created="2026-10-08", side="sell")
    text = _week(conn)
    assert "EDGE" in text
    for name in ("OLD", "SKIP", "GONE"):
        assert name not in text
    assert "SELL" not in "\n".join(_section(text, "🟢 Покупки"))


def test_without_buys_there_is_no_buys_section(conn):
    _week_model(conn)
    assert "🟢" not in _week(conn)


def test_the_weeks_sales_show_reason_and_result_and_the_pending_ones_wait(conn):
    _week_model(conn)
    _closed(conn, S, "CCC", closed="2026-10-07", cost=8_000.0, proceeds=8_400.0)
    _closed(conn, C, "CRYPTO:ETH", closed="2026-10-03", cost=5_000.0, proceeds=4_750.0, reason="тренд вниз")
    _closed(conn, S, "OLD", closed="2026-10-02")                          # closed before the week
    pid = _open_position(conn, S, "DDD", fill="2026-09-25", cost=8_000.0, last=8_160.0)
    _order(conn, S, "DDD", side="sell", position_id=pid, reason="новости: fraud", status="pending")
    sales = _section(_week(conn), "🔴 Продажи")
    assert sales == ["🔴 Продажи",
                     "• ETH — тренд вниз (результат −5,0%)",           # in the order they closed
                     "• CCC — стоп: −10% от максимума (результат +5,0%)",
                     "• DDD — новости: fraud (ждёт исполнения, сейчас +2,0%)"]


def test_without_sales_there_is_no_sales_section(conn):
    _week_model(conn)
    _closed(conn, S, "OLD", closed="2026-10-02")
    assert "🔴" not in _week(conn)


def test_open_positions_show_days_result_and_stop(conn):
    _week_model(conn)
    _open_position(conn, S, "AAA", fill="2026-10-05", cost=8_000, last=8_400, stop=0.10)
    _open_position(conn, C, "CRYPTO:BTC", fill="2026-10-08", cost=5_000, last=4_750, stop=0.2)
    held = _section(_week(conn), "📋 В портфеле")
    assert held[0] == "📋 В портфеле"
    aaa = next(ln for ln in held if "AAA" in ln)
    btc = next(ln for ln in held if "BTC" in ln and "CRYPTO:" not in ln)
    assert "4 дн." in aaa and "+5,0%" in aaa and "стоп −10%" in aaa
    assert "1 дн." in btc and "−5,0%" in btc and "стоп −20%" in btc


def test_no_open_positions_reads_all_in_cash(conn):
    _week_model(conn)
    assert _section(_week(conn), "📋 В портфеле") == ["📋 В портфеле", "пусто — всё в деньгах"]


def test_watch_lists_up_to_five_watched_scores_with_their_top_reason(conn):
    _week_model(conn)
    scored = [_stock("BUY1", 64.0, model_score.BUY), _stock("SKP1", 30.0, model_score.SKIP),
              _stock("BLK1", 40.0, model_score.BLOCK)]
    scored += [_stock(f"W{i}", 58.0 - i, model_score.WATCH, reasons=[f"причина {i}", "вторая"])
               for i in range(7)]
    scored += [_coin("ETH", 50.0, model_score.WATCH, reasons=["выше 100-дн. средней"])]
    watch = _section(_week(conn, _report(scored)), "👀 Наблюдение")
    assert watch[0] == "👀 Наблюдение"
    assert len(watch) == 6                                               # the header and five
    assert watch[1] == "• W0 — балл 58: причина 0"
    assert [ln.split()[1] for ln in watch[1:]] == ["W0", "W1", "W2", "W3", "W4"]     # highest first
    text = "\n".join(watch)
    assert "BUY1" not in text and "SKP1" not in text and "BLK1" not in text and "вторая" not in text


def test_watch_without_a_report_reads_todays_kept_scores(conn):
    _week_model(conn)
    model.keep_scores(conn, FRI, [_stock("KEPT", 52.0, model_score.WATCH, reasons=["r"]),
                                  _coin("ETH", 50.0, model_score.WATCH)])
    watch = _section(_week(conn), "👀 Наблюдение")
    assert watch[1] == "• KEPT — балл 52: r" and watch[2].startswith("• ETH — балл 50")


def test_watch_ignores_the_scores_of_another_day_and_is_left_out_when_empty(conn):
    _week_model(conn)
    model.keep_scores(conn, FRI - dt.timedelta(days=1), [_stock("OLD", 52.0, model_score.WATCH)])
    assert "👀" not in _week(conn)
    assert "👀" not in _week(conn, _report([_stock("BUY1", 64.0, model_score.BUY)]))


def test_group_exits_of_the_week_name_the_sellers(conn):
    _week_model(conn)
    _exit_row(conn, "ZZZ", "Exit Corp", ["Ann Lee", "Bo Chen"], emitted="2026-10-08 12:00:00")
    _exit_row(conn, "YYY", "Early Inc", ["C D"], emitted="2026-10-03 12:00:00")           # the week's first day
    _exit_row(conn, "OLD", "Old Inc", ["E F"], emitted="2026-10-02 12:00:00")             # before the week
    _exit_row(conn, "CLU", "Cluster Inc", ["G H"], emitted="2026-10-08 12:00:00", kind="cluster")
    exits = _section(_week(conn), "🚨 Продают те, кто покупал")
    assert exits[0] == "🚨 Продают те, кто покупал"
    assert "• ZZZ — Exit Corp: Ann Lee, Bo Chen" in exits and "• YYY — Early Inc: C D" in exits
    assert not any("OLD" in ln or "CLU" in ln for ln in exits)


def test_a_ticker_that_exited_twice_in_a_week_is_listed_once_with_its_latest_sellers(conn):
    _week_model(conn)
    _exit_row(conn, "ZZZ", "Exit Corp", ["A"], emitted="2026-10-06 12:00:00")
    _exit_row(conn, "ZZZ", "Exit Corp", ["A", "B"], emitted="2026-10-08 12:00:00")
    exits = _section(_week(conn), "🚨 Продают те, кто покупал")
    assert exits[1:] == ["• ZZZ — Exit Corp: A, B"]


def test_a_long_seller_list_is_cut(conn):
    _week_model(conn)
    _exit_row(conn, "ZZZ", "Exit Corp", [f"P{i}" for i in range(9)])
    assert _section(_week(conn), "🚨 Продают те, кто покупал")[1] == (
        "• ZZZ — Exit Corp: P0, P1, P2, P3, P4 и ещё 4")


def test_without_exits_there_is_no_exits_section(conn):
    _week_model(conn)
    assert "🚨" not in _week(conn)


def test_the_last_line_has_the_value_the_return_the_week_and_the_mix(conn):
    _week_model(conn)
    assert _week(conn).splitlines()[-1] == (
        "Портфель: €107 100 (+7,1% с начала), за неделю +2,0%; смесь 70/30: +0,8% с начала")


def test_the_week_return_uses_the_last_value_on_or_before_a_week_ago(conn):
    _week_model(conn)
    conn.execute("DELETE FROM paper_equity WHERE date = '2026-10-02'")
    for code, value in ((S, 73_500), (C, 31_500)):                       # the Thursday before it
        conn.execute("INSERT INTO paper_equity (book, date, value, cash, bench) VALUES (?,?,?,?,?)",
                     (code, "2026-10-01", value, 0.0, None))
    conn.commit()
    assert "за неделю +2,0%" in _week(conn).splitlines()[-1]


def test_with_no_value_a_week_ago_the_week_return_is_left_out(conn):
    _week_model(conn)
    conn.execute("DELETE FROM paper_equity WHERE date <= '2026-10-02'")  # nothing on or before a week ago
    assert "за неделю" not in _week(conn).splitlines()[-1]


def test_the_last_line_leaves_out_what_it_cannot_compute(conn):
    _week_model(conn)
    conn.execute("UPDATE paper_equity SET bench = NULL")
    conn.execute("DELETE FROM paper_equity WHERE date <= '2026-10-02'")
    assert _week(conn).splitlines()[-1] == "Портфель: €107 100 (+7,1% с начала)"


def test_the_week_return_needs_both_books(conn):
    _week_model(conn)
    conn.execute("DELETE FROM paper_equity WHERE date <= '2026-10-02' AND book = ?", (C,))
    assert "за неделю" not in _week(conn).splitlines()[-1]


def test_a_quiet_week_is_still_a_report(conn):
    _week_model(conn)
    text = _week(conn)
    assert "Сделок за неделю нет." in text and "пусто — всё в деньгах" in text
    for icon in ("🟢", "🔴", "👀", "🚨"):
        assert icon not in text
    assert text.splitlines()[0].startswith("📊 Модельный портфель — неделя")
    assert text.splitlines()[-1].startswith("Портфель: €107 100")


def test_a_week_with_trades_does_not_say_there_were_none(conn):
    _week_model(conn)
    _order(conn, S, "AAA")
    assert "Сделок за неделю нет" not in _week(conn)
    _closed(conn, S, "CCC", closed="2026-10-07")
    assert "Сделок за неделю нет" not in _week(conn)


WARNING = "⚠️ Модель на этой неделе не отработала — покупок не было."


def test_a_week_the_model_failed_carries_a_warning_just_before_the_portfolio_line(conn):
    _week_model(conn)
    blocks = _week(conn, model_failed=True).split("\n\n")
    assert blocks[-2] == WARNING and blocks[-1].startswith("Портфель: €107 100")
    assert "<b>" not in WARNING and WARNING in paper_report.format_week(conn, FRI, None, model_failed=True)


def test_a_normal_week_has_no_warning(conn):
    _week_model(conn)
    assert "⚠️" not in _week(conn) and "⚠️" not in _week(conn, model_failed=False)


def test_the_sections_come_in_order(conn):
    _week_model(conn)
    _order(conn, S, "AAA")
    _closed(conn, S, "CCC", closed="2026-10-07")
    _open_position(conn, S, "HHH", fill="2026-10-05")
    _exit_row(conn, "ZZZ", "Exit Corp", ["A", "B"])
    text = _week(conn, _report([_stock("WAT", 52.0, model_score.WATCH)]))
    order = ["📊 Модельный портфель", "🟢 Покупки", "🔴 Продажи", "📋 В портфеле", "👀 Наблюдение",
             "🚨 Продают те, кто покупал", "Портфель: €"]
    idx = [text.index(h) for h in order]
    assert idx == sorted(idx)


def test_html_week_is_escaped_and_uses_only_bold_and_pre(conn):
    _week_model(conn)
    _order(conn, S, "A&B", reason="балл 64: <b>bold</b> & co")
    _closed(conn, S, "C<C", closed="2026-10-07", reason="продаёт <инсайдер>")
    _open_position(conn, S, "H&H", fill="2026-10-05")
    _exit_row(conn, "Z&Z", "Evil <script>alert(1)</script> & Co", ["Ann <i>Lee</i>", "B&B"])
    text = paper_report.format_week(conn, FRI, _report([_stock("W&W", 52.0, model_score.WATCH,
                                                             reasons=["a < b & c"])]))
    for dynamic in ("A&amp;B", "&lt;b&gt;bold&lt;/b&gt; &amp; co", "C&lt;C", "&lt;инсайдер&gt;", "H&amp;H",
                    "Z&amp;Z", "Evil &lt;script&gt;alert(1)&lt;/script&gt; &amp; Co",
                    "Ann &lt;i&gt;Lee&lt;/i&gt;", "B&amp;B", "W&amp;W", "a &lt; b &amp; c"):
        assert dynamic in text
    assert set(_TAGS.findall(text)) <= {"<b>", "</b>", "<pre>", "</pre>"}
    assert text.count("<pre>") == text.count("</pre>") == 1
    assert "<b>🟢 Покупки</b>" in text and "<b>📋 В портфеле</b>" in text


def test_the_pre_block_has_no_blank_line_so_a_split_cannot_cut_it(conn):
    _week_model(conn)
    for i in range(4):
        _open_position(conn, S, f"H{i}", fill="2026-10-05")
    text = paper_report.format_week(conn, FRI, None)
    inside = text[text.index("<pre>"):text.index("</pre>")]
    assert "\n\n" not in inside


def test_plain_week_has_no_tags(conn):
    _week_model(conn)
    _open_position(conn, S, "HHH", fill="2026-10-05")
    assert not _TAGS.search(_week(conn))


# ------------------------------------------------- the Trading 212 line of the weekly message
def _t212_equity(conn, rows, currency="EUR"):
    """The account's daily snapshots as a sync stores them: rows [(ISO date, total value)]."""
    for day, value in rows:
        conn.execute("INSERT OR REPLACE INTO t212_equity (date, total_value, invested_value, invested_cost, "
                     "cash_free, currency) VALUES (?,?,?,?,?,?)", (day, value, None, None, None, currency))
    conn.commit()


def test_the_week_shows_your_trading_212_account_just_before_the_portfolio_line(conn):
    _week_model(conn)
    _t212_equity(conn, [("2026-10-02", 12_000.0), ("2026-10-09", 12_345.67)])
    blocks = _week(conn).split("\n\n")
    assert blocks[-2] == "Ваш счёт Trading 212: €12 346 (за неделю +2,9%)"
    assert blocks[-1].startswith("Портфель: €107 100")


def test_the_account_is_valued_on_or_before_today_and_a_week_before(conn):
    """Today is FRI 09.10: the last snapshot on or before it is of 07.10, and the week is measured
    from the last one on or before 02.10, which is of 30.09."""
    _week_model(conn)
    _t212_equity(conn, [("2026-09-30", 10_000.0), ("2026-10-07", 9_000.0), ("2026-10-10", 99_999.0)])
    assert "Ваш счёт Trading 212: €9 000 (за неделю −10,0%)" in _week(conn).split("\n\n")


@pytest.mark.parametrize("rows", [[("2026-10-08", 12_000.0)],                           # nothing that early
                                  [("2026-10-01", 0.0), ("2026-10-08", 12_000.0)]])     # nothing to compare with
def test_the_week_change_is_left_out_when_it_is_not_known(conn, rows):
    _week_model(conn)
    _t212_equity(conn, rows)
    blocks = _week(conn).split("\n\n")
    assert blocks[-2] == "Ваш счёт Trading 212: €12 000" and "за неделю" not in blocks[-2]


def test_without_account_data_there_is_no_trading_212_line(conn):
    _week_model(conn)
    assert "Trading 212" not in _week(conn)
    _t212_equity(conn, [("2026-10-10", 12_000.0)])                 # only a day after today
    assert "Trading 212" not in _week(conn)
    conn.execute("INSERT INTO t212_equity (date, total_value) VALUES ('2026-10-05', NULL)")
    assert "Trading 212" not in _week(conn)


def test_the_trading_212_line_stands_between_the_model_warning_and_the_portfolio_line(conn):
    _week_model(conn)
    _t212_equity(conn, [("2026-10-09", 12_000.0)])
    blocks = _week(conn, model_failed=True).split("\n\n")
    assert blocks[-3] == WARNING and blocks[-2] == "Ваш счёт Trading 212: €12 000"
    assert blocks[-1].startswith("Портфель: €107 100")


def test_the_trading_212_line_is_plain_text_in_html_and_names_another_currency(conn):
    _week_model(conn)
    _t212_equity(conn, [("2026-10-02", 10_000.0), ("2026-10-09", 12_000.0)], currency="GBP")
    text = paper_report.format_week(conn, FRI, None)
    assert "\n\nВаш счёт Trading 212: £12 000 (за неделю +20,0%)\n\nПортфель: " in text
    assert set(_TAGS.findall(text)) <= {"<b>", "</b>", "<pre>", "</pre>"}


# ---------------------------------------------------------------------- the menu
def test_menu_signals_print_the_scored_list(conn, capsys, monkeypatch):
    import menu

    monkeypatch.setattr(model, "score_today", lambda c, *a, **k: [_stock("AAA", 64.0), _coin()])
    monkeypatch.setattr("positions.check_exits", lambda c, *a, **k: [_close("CCC")])
    monkeypatch.setattr("positions.last_close", lambda ticker, source=None: 110.0)
    monkeypatch.setattr("paper._closes", lambda symbol, days: [])        # no history to size a stop from
    positions.open_position(conn, "AAA", 100.0)
    menu.show_signals(conn)
    out = capsys.readouterr().out
    assert "СИГНАЛЫ — оценка модели (покупка от 60, наблюдение 45–59)" in out
    assert "🟢 AAA" in out and "CRYPTO:BTC" in out
    assert "CCC" in out and "стоп от максимума" in out         # the pending close alert
    assert "Открытые позиции" in out                            # the /bought positions


def test_menu_prices_a_trading_212_holding_from_its_stored_day_price(conn, capsys, monkeypatch):
    """A holding with no Yahoo listing (keyed by its ISIN) has the sync's prices, also here."""
    import menu

    today = dt.date.today().isoformat()
    monkeypatch.setattr(model, "score_today", lambda c, *a, **k: [])
    monkeypatch.setattr("positions.check_exits", lambda c, *a, **k: [])
    monkeypatch.setattr("positions.last_close", lambda ticker, source=None, conn=None: 110.0 if conn else None)
    conn.execute("INSERT INTO positions (ticker, source, opened_at, entry_price, origin, quantity, t212_ticker, "
                 "currency) VALUES ('DE0007164600', 'T212', ?, 100.0, 't212', 5, 'SAPd_EQ', 'EUR')", (today,))
    conn.commit()
    menu.show_signals(conn)
    out = capsys.readouterr().out
    assert "сейчас 110.00 (+10.0%)" in out and "цена недоступна" not in out


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

