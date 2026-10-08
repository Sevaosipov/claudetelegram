"""cfd/rates.py: the monthly OECD 3-month interbank rates of H5 (CARRY-FX) from FRED's fredgraph.csv --
the parser, the publication rule (month M is usable from the 15th of M+1; a missing month carries the
last value forward), the cache and the fetch seam. Offline: the fetch is always a fake, and the one
default (`requests`) is tested with a fake `requests.get`."""
from __future__ import annotations

import datetime as dt
import json

import pytest

from cfd import data, rates

D = dt.date

CSV = """observation_date,IR3TIB01USM156N
2005-12-01,4.38
2006-01-01,4.55
2006-02-01,4.78
2006-03-01,.
2006-04-01,4.95
"""


class FakeFred:
    """A fetch seam: series id -> csv text; records its calls; ids in `fail` raise."""

    def __init__(self, texts=None, fail=()):
        self.texts = texts or {}
        self.fail = set(fail)
        self.calls: list[str] = []

    def __call__(self, series_id):
        self.calls.append(series_id)
        if series_id in self.fail:
            raise RuntimeError("FRED is down")
        return self.texts.get(series_id, CSV)


def series_from(text=CSV, currency="USD"):
    return rates.RateSeries(currency, rates.SERIES[currency], tuple(rates.parse_fredgraph(text)))


# ------------------------------------------------------------------ the pre-registered series
def test_the_series_are_the_eight_oecd_3_month_interbank_series_of_the_preregistration():
    assert rates.SERIES == {
        "USD": "IR3TIB01USM156N", "EUR": "IR3TIB01EZM156N", "GBP": "IR3TIB01GBM156N",
        "JPY": "IR3TIB01JPM156N", "AUD": "IR3TIB01AUM156N", "CAD": "IR3TIB01CAM156N",
        "CHF": "IR3TIB01CHM156N", "NZD": "IR3TIB01NZM156N"}


def test_every_currency_of_the_fx_universe_has_a_series():
    from cfd import instruments as ins
    needed = {c for i in ins.FX_UNIVERSE for c in (i.base, i.quote)}
    assert needed == set(rates.SERIES)


def test_the_url_is_fredgraph_csv_and_the_publication_day_is_the_15th():
    assert rates.FRED_URL == "https://fred.stlouisfed.org/graph/fredgraph.csv"
    assert rates.PUBLICATION_DAY == 15


# ------------------------------------------------------------------ the publication rule
@pytest.mark.parametrize("month, usable", [
    (D(2006, 1, 1), D(2006, 2, 15)),
    (D(2006, 11, 1), D(2006, 12, 15)),
    (D(2006, 12, 1), D(2007, 1, 15)),          # December is published in January of the next year
    (D(2008, 2, 1), D(2008, 3, 15)),           # whatever day of the month the observation carries
    (D(2008, 2, 29), D(2008, 3, 15)),
])
def test_a_month_is_usable_from_the_15th_of_the_next_month(month, usable):
    assert rates.available_from(month) == usable


def test_a_value_is_used_from_the_15th_not_before():
    s = series_from()
    # December 2005 (4.38) is published on 2006-01-15
    assert s.on(D(2006, 1, 14)) is None
    assert s.on(D(2006, 1, 15)) == 4.38
    # January 2006 (4.55) on 2006-02-15
    assert s.on(D(2006, 2, 14)) == 4.38
    assert s.on(D(2006, 2, 15)) == 4.55
    # February (4.78) on 2006-03-15
    assert s.on(D(2006, 3, 14)) == 4.55
    assert s.on(D(2006, 3, 15)) == 4.78


def test_a_missing_month_carries_the_last_value_forward():
    s = series_from()
    # March 2006 is "." (it would have been published on 04-15): February's 4.78 stays in force
    assert s.on(D(2006, 4, 14)) == 4.78
    assert s.on(D(2006, 4, 15)) == 4.78
    assert s.on(D(2006, 5, 14)) == 4.78
    # April (4.95) is published on 2006-05-15
    assert s.on(D(2006, 5, 15)) == 4.95


def test_after_the_last_observation_the_last_value_carries_forward_indefinitely():
    s = series_from()
    assert s.on(D(2006, 6, 30)) == 4.95
    assert s.on(D(2030, 1, 1)) == 4.95


def test_before_any_observation_is_usable_there_is_no_rate():
    assert series_from().on(D(1999, 1, 1)) is None
    assert rates.RateSeries("USD", "x", ()).on(D(2020, 1, 1)) is None


def test_a_datetime_is_read_as_its_calendar_date():
    s = series_from()
    assert s.on(dt.datetime(2006, 2, 15, 0, 0, tzinfo=dt.timezone.utc)) == 4.55
    assert s.on(dt.datetime(2006, 2, 14, 23, 59, tzinfo=dt.timezone.utc)) == 4.38


def test_a_run_of_missing_months_keeps_the_last_real_value():
    text = ("observation_date,X\n2006-01-01,1.5\n2006-02-01,.\n2006-03-01,\n2006-04-01,.\n"
            "2006-05-01,2.5\n")
    s = series_from(text)
    assert s.on(D(2006, 2, 15)) == 1.5
    assert s.on(D(2006, 5, 20)) == 1.5          # Feb, Mar, Apr are all missing
    assert s.on(D(2006, 6, 14)) == 1.5
    assert s.on(D(2006, 6, 15)) == 2.5


def test_the_rate_in_force_never_depends_on_a_later_publication():
    s = series_from()
    # whatever is appended later, the value on an earlier day does not change (no look-ahead)
    longer = series_from(CSV + "2006-05-01,9.99\n2006-06-01,8.88\n")
    for day in (D(2006, 1, 15), D(2006, 3, 20), D(2006, 5, 14), D(2006, 6, 14)):
        assert s.on(day) == longer.on(day)
    assert longer.on(D(2006, 6, 15)) == 9.99


# ------------------------------------------------------------------ parsing fredgraph.csv
def test_parse_reads_month_value_pairs_sorted_by_month():
    assert rates.parse_fredgraph(CSV) == [
        (D(2005, 12, 1), 4.38), (D(2006, 1, 1), 4.55), (D(2006, 2, 1), 4.78), (D(2006, 4, 1), 4.95)]


def test_parse_skips_dot_and_empty_values():
    text = "DATE,X\n2006-01-01,1.0\n2006-02-01,.\n2006-03-01,\n2006-04-01,   \n2006-05-01,2.0\n"
    assert rates.parse_fredgraph(text) == [(D(2006, 1, 1), 1.0), (D(2006, 5, 1), 2.0)]


def test_parse_accepts_both_header_names_and_a_bom_and_windows_line_ends():
    for header in ("observation_date,X", "DATE,X"):
        assert rates.parse_fredgraph(f"{header}\n2006-01-01,1.5\n") == [(D(2006, 1, 1), 1.5)]
    assert rates.parse_fredgraph("﻿DATE,X\r\n2006-01-01,1.5\r\n2006-02-01,1.6\r\n") == [
        (D(2006, 1, 1), 1.5), (D(2006, 2, 1), 1.6)]


def test_parse_keeps_negative_rates_and_zero():
    # the euro, franc and yen had negative 3-month rates for years
    text = "DATE,X\n2016-01-01,-0.25\n2016-02-01,0\n2016-03-01,-0.5\n"
    assert [v for _, v in rates.parse_fredgraph(text)] == [-0.25, 0.0, -0.5]


def test_parse_sorts_unordered_rows_and_lets_the_later_row_of_a_month_win():
    text = "DATE,X\n2006-03-01,3.0\n2006-01-01,1.0\n2006-01-01,1.1\n2006-02-01,2.0\n"
    assert rates.parse_fredgraph(text) == [(D(2006, 1, 1), 1.1), (D(2006, 2, 1), 2.0),
                                           (D(2006, 3, 1), 3.0)]


def test_parse_ignores_junk_rows_and_non_finite_values():
    text = ("DATE,X\nnot-a-date,1.0\n2006-01-01\n\n2006-02-01,abc\n2006-03-01,nan\n2006-04-01,inf\n"
            "2006-05-01,2.5\n")
    assert rates.parse_fredgraph(text) == [(D(2006, 5, 1), 2.5)]


def test_parse_of_nothing_is_empty():
    assert rates.parse_fredgraph("") == []
    assert rates.parse_fredgraph("<html>rate limited</html>") == []
    assert rates.parse_fredgraph("DATE,X\n") == []


# ------------------------------------------------------------------ the Rates lookup
def test_rate_on_looks_up_a_currency_by_name_and_applies_the_rule():
    r = rates.Rates({"USD": series_from()})
    assert r.rate_on("USD", D(2006, 2, 15)) == 4.55
    assert r.rate_on("USD", D(2006, 2, 14)) == 4.38


def test_rate_on_is_none_for_a_currency_that_did_not_load_and_an_error_for_a_typo():
    r = rates.Rates({"USD": series_from()}, errors={"EUR": "RuntimeError: down"})
    assert r.rate_on("EUR", D(2006, 2, 15)) is None
    with pytest.raises(KeyError):
        r.rate_on("EURO", D(2006, 2, 15))


# ------------------------------------------------------------------ loading, the fetch seam and the cache
def test_load_rates_fetches_each_series_once_and_builds_the_lookup(tmp_path):
    fetch = FakeFred({"IR3TIB01USM156N": CSV, "IR3TIB01EZM156N": "DATE,X\n2006-01-01,2.5\n"})
    r = rates.load_rates(("USD", "EUR"), fetch=fetch, cache_dir=tmp_path)
    assert sorted(fetch.calls) == ["IR3TIB01EZM156N", "IR3TIB01USM156N"]
    assert r.rate_on("USD", D(2006, 2, 15)) == 4.55
    assert r.rate_on("EUR", D(2006, 2, 15)) == 2.5
    assert r.errors == {}
    assert set(r.series) == {"USD", "EUR"}


def test_load_rates_defaults_to_all_eight_currencies(tmp_path):
    fetch = FakeFred()
    r = rates.load_rates(fetch=fetch, cache_dir=tmp_path)
    assert sorted(fetch.calls) == sorted(rates.SERIES.values())
    assert set(r.series) == set(rates.SERIES)


def test_a_second_load_reads_the_cache_and_does_not_fetch(tmp_path):
    first = FakeFred()
    rates.load_rates(("USD",), fetch=first, cache_dir=tmp_path)
    assert (tmp_path / "fred_IR3TIB01USM156N.json").exists()
    second = FakeFred()
    r = rates.load_rates(("USD",), fetch=second, cache_dir=tmp_path)
    assert second.calls == []
    assert r.rate_on("USD", D(2006, 2, 15)) == 4.55


def test_refresh_fetches_again_and_overwrites_the_cache(tmp_path):
    rates.load_rates(("USD",), fetch=FakeFred(), cache_dir=tmp_path)
    newer = FakeFred({"IR3TIB01USM156N": "DATE,X\n2006-01-01,7.0\n"})
    r = rates.load_rates(("USD",), fetch=newer, cache_dir=tmp_path, refresh=True)
    assert newer.calls == ["IR3TIB01USM156N"]
    assert r.rate_on("USD", D(2006, 2, 15)) == 7.0
    # and what is cached now is the new text
    again = FakeFred()
    assert rates.load_rates(("USD",), fetch=again, cache_dir=tmp_path).rate_on("USD", D(2006, 2, 15)) == 7.0
    assert again.calls == []


def test_the_cache_file_is_strict_json_with_the_text_and_when_it_was_fetched(tmp_path):
    rates.load_rates(("USD",), fetch=FakeFred(), cache_dir=tmp_path)
    payload = json.loads((tmp_path / "fred_IR3TIB01USM156N.json").read_text())
    assert payload["series"] == "IR3TIB01USM156N" and payload["currency"] == "USD"
    assert payload["csv"] == CSV
    stamp = dt.datetime.fromisoformat(payload["fetched_at"])
    assert stamp.tzinfo is not None
    assert rates.cache_stamp(tmp_path, "USD") == payload["fetched_at"]
    assert rates.cache_stamp(tmp_path, "EUR") is None
    assert not list(tmp_path.glob("*.tmp"))


def test_the_cache_directory_is_created_when_missing(tmp_path):
    target = tmp_path / "deep" / "cache"
    rates.load_rates(("USD",), fetch=FakeFred(), cache_dir=target)
    assert (target / "fred_IR3TIB01USM156N.json").exists()


def test_a_failing_series_is_logged_and_left_out_while_the_others_load(tmp_path):
    fetch = FakeFred(fail={"IR3TIB01EZM156N"})
    log: list[str] = []
    r = rates.load_rates(("USD", "EUR", "GBP"), fetch=fetch, cache_dir=tmp_path, log=log.append)
    assert set(r.series) == {"USD", "GBP"}
    assert list(r.errors) == ["EUR"] and "RuntimeError" in r.errors["EUR"]
    assert len(log) == 1 and "EUR" in log[0]
    assert r.rate_on("EUR", D(2006, 2, 15)) is None
    assert not (tmp_path / "fred_IR3TIB01EZM156N.json").exists()        # nothing cached for a failure
    retry = FakeFred()
    r2 = rates.load_rates(("EUR",), fetch=retry, cache_dir=tmp_path)
    assert retry.calls == ["IR3TIB01EZM156N"] and "EUR" in r2.series


@pytest.mark.parametrize("text", ["", "<html>Too many requests</html>", "DATE,X\n2006-01-01,.\n"])
def test_a_reply_without_a_single_observation_is_an_error_and_is_not_cached(tmp_path, text):
    fetch = FakeFred({"IR3TIB01USM156N": text})
    r = rates.load_rates(("USD",), fetch=fetch, cache_dir=tmp_path, log=lambda m: None)
    assert "USD" in r.errors and "USD" not in r.series
    assert not list(tmp_path.glob("fred_*"))


def test_a_corrupt_or_empty_cache_file_is_fetched_again(tmp_path):
    path = tmp_path / "fred_IR3TIB01USM156N.json"
    for junk in ("{not json", json.dumps({"csv": ""}), json.dumps({"series": "x"}), "[]"):
        path.write_text(junk)
        fetch = FakeFred()
        r = rates.load_rates(("USD",), fetch=fetch, cache_dir=tmp_path)
        assert fetch.calls == ["IR3TIB01USM156N"] and "USD" in r.series
        assert json.loads(path.read_text())["csv"] == CSV


def test_the_default_cache_directory_is_the_one_the_bars_use():
    assert rates.CACHE_DIR == data.CACHE_DIR
    assert rates.CACHE_DIR.parts[-2:] == ("data", "cfd_cache")


# ------------------------------------------------------------------ the default fetch: requests
class FakeResponse:
    def __init__(self, text="DATE,X\n2006-01-01,1.0\n", error=None):
        self.text = text
        self._error = error
        self.raised = False

    def raise_for_status(self):
        self.raised = True
        if self._error:
            raise self._error


def test_the_default_fetch_asks_fredgraph_csv_for_the_series_id(monkeypatch):
    import requests
    seen = {}
    response = FakeResponse("DATE,X\n2006-01-01,1.0\n")

    def fake_get(url, params=None, timeout=None, **kw):
        seen.update(url=url, params=params, timeout=timeout)
        return response
    monkeypatch.setattr(requests, "get", fake_get)
    assert rates.fred_fetch("IR3TIB01JPM156N") == "DATE,X\n2006-01-01,1.0\n"
    assert seen == {"url": "https://fred.stlouisfed.org/graph/fredgraph.csv",
                    "params": {"id": "IR3TIB01JPM156N"}, "timeout": 20}
    assert response.raised


def test_the_default_fetch_raises_on_an_http_error(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: FakeResponse(error=requests.HTTPError("503")))
    with pytest.raises(requests.HTTPError):
        rates.fred_fetch("IR3TIB01USM156N")


def test_load_rates_uses_the_default_fetch_when_none_is_given(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(rates, "fred_fetch", lambda series_id: calls.append(series_id) or CSV)
    r = rates.load_rates(("USD",), cache_dir=tmp_path)
    assert calls == ["IR3TIB01USM156N"] and "USD" in r.series
