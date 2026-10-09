"""takeover.py: a company being bought out, told from its SEC filing list and its price."""
from __future__ import annotations

import datetime as dt
from collections import Counter

import pytest

import takeover

TODAY = dt.date(2026, 10, 9)


@pytest.mark.parametrize("forms", [{"SC14D9C": 2, "SC TO-C": 1, "8-K": 4}, {"SC TO-T": 1}, {"SC 14D9/A": 3},
                                   {"DEFM14A": 1, "425": 9}, {"SC 13E3": 1}])
def test_a_form_only_a_target_files_makes_a_target(forms):
    found = takeover.judge(Counter(forms), 0.0)
    assert found.kind == takeover.TARGET and found.text.startswith("идёт выкуп компании (формы SEC: ")


def test_the_merger_form_with_a_jump_is_the_target_and_without_one_a_party_to_the_deal():
    rxo = takeover.judge(Counter({"425": 21, "8-K": 4}), 0.24)
    assert (rxo.kind, rxo.text) == (takeover.TARGET, "идёт выкуп компании (форма 425 ×21, скачок цены +24% за день)")
    chrw = takeover.judge(Counter({"425": 14, "8-K": 4}), 0.04)
    assert (chrw.kind, chrw.text) == (takeover.DEAL, "компания участвует в слиянии (форма 425 ×14)")


@pytest.mark.parametrize("forms", [{"4": 24, "144": 6, "8-K": 1}, {"425": 2}, {"SC TO-I": 1, "SC TO-C": 1},
                                   {"PREM14A": 1}, {}])
def test_ordinary_filings_an_issuers_own_tender_and_a_stray_425_are_nothing(forms):
    assert takeover.judge(Counter(forms), 0.5) is None
    assert takeover.judge(None, 0.5) is None


def test_the_biggest_jump_is_the_largest_one_day_rise_in_the_window():
    closes = [("2026-01-05", 10.0), ("2026-01-06", 20.0),                  # +100 %, before the window
              ("2026-08-03", 20.0), ("2026-08-04", 21.0), ("2026-09-01", 26.25), ("2026-09-02", 25.0)]
    assert takeover.biggest_jump(closes, TODAY) == pytest.approx(0.25)
    assert takeover.biggest_jump([], TODAY) == 0.0 and takeover.biggest_jump([("2026-10-01", 5.0)], TODAY) == 0.0


def test_check_reads_the_price_only_when_the_merger_form_needs_it():
    asked = []

    def closes(ticker, source):
        asked.append(ticker)
        return [("2026-09-01", 23.4), ("2026-09-02", 29.0)]
    assert takeover.check("SSTI", "SEC13DG", TODAY, forms_fn=lambda t, d: Counter({"SC14D9C": 2}),
                          closes_fn=closes).kind == takeover.TARGET
    assert asked == []
    assert takeover.check("RXO", "SEC13DG", TODAY, forms_fn=lambda t, d: Counter({"425": 21}),
                          closes_fn=closes).kind == takeover.TARGET
    assert asked == ["RXO"]
    assert takeover.check("AAPL", None, TODAY, forms_fn=lambda t, d: Counter({"4": 24}), closes_fn=closes) is None


def test_what_cannot_be_checked_and_a_failed_lookup_are_not_a_finding(capsys):
    def boom(ticker, today):
        raise RuntimeError("edgar is down")
    assert takeover.check("EQNR", "NORWAY", TODAY, forms_fn=boom) is None           # not an SEC filer: not asked
    assert takeover.check("CRYPTO:BTC", None, TODAY, forms_fn=boom) is None
    assert capsys.readouterr().err == ""
    assert takeover.check("RXO", "SEC", TODAY, forms_fn=boom) is None
    assert "RXO: not checked: RuntimeError" in capsys.readouterr().err
    assert takeover.check("NOCIK", "SEC", TODAY, forms_fn=lambda t, d: None) is None
