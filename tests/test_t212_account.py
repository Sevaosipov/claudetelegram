"""t212_account.py (spec 2026-10-01-trading212-account-tracking.md): the read-only Trading 212
client, the mandatory safety tests, the sync, /portfolio's live view and the command line.
Offline: every call goes to a stub session or a stub fetch, and conftest keeps any real key out
of the tests."""
from __future__ import annotations

import ast
import base64
import dataclasses
import datetime as dt
import json
import re
import sqlite3
from pathlib import Path

import pytest
import requests

import db
import positions
import t212_account as ta
import trading212

FULL_POSITION = {
    "instrument": {"ticker": "GME_US_EQ", "name": "GameStop", "isin": "US36467W1099", "currency": "USD"},
    "quantity": 10, "quantityAvailableForTrading": 10, "quantityInPies": 0,
    "currentPrice": 24.05, "averagePricePaid": 23.10, "createdAt": "2026-09-28T14:03:11.000+02:00",
    "walletImpact": {"currency": "EUR", "totalCost": 199.0, "currentValue": 207.3,
                     "unrealizedProfitLoss": 8.30, "fxImpact": -0.2},
}
FULL_SUMMARY = {
    "id": 31337, "currency": "EUR", "totalValue": 12345.67,
    "cash": {"availableToTrade": 2000.0, "reservedForOrders": 0.0, "inPies": 0.0},
    "investments": {"currentValue": 10345.67, "totalCost": 10000.0, "realizedProfitLoss": 12.5,
                    "unrealizedProfitLoss": 345.67},
}
KEY, SECRET = "test-key-1234", "test-secret-5678"
READ_URLS = {"https://live.trading212.com/api/v0/equity/metadata/instruments",
             "https://live.trading212.com/api/v0/equity/positions",
             "https://live.trading212.com/api/v0/equity/account/summary"}


class _Resp:
    def __init__(self, status=200, payload=None, *, bad_json=False):
        self.status_code, self.payload, self.bad_json = status, payload, bad_json

    def raise_for_status(self):            # trading212.fetch_instruments' own check
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        if self.bad_json:
            raise requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0)
        return self.payload


class _Session:
    """Answers each GET from `routes` (url -> responses, in turn; the last one repeats) and records
    every call. Any other method is recorded and fails: the client may only read."""

    def __init__(self, routes=None, *, raises=None):
        self.routes = {url: list(answers) for url, answers in (routes or {}).items()}
        self.raises, self.calls, self.headers = raises, [], []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("get", url))
        self.headers.append(headers)
        assert timeout, "a call without a timeout can hang the bot"
        if self.raises is not None:
            raise self.raises
        answers = self.routes[url]
        return answers.pop(0) if len(answers) > 1 else answers[0]

    def __getattr__(self, name):
        def other(*a, **k):
            self.calls.append((name, a[0] if a else k.get("url")))
            raise AssertionError(f"session.{name} called: the client may only GET")
        return other


def _account(positions_payload=None, summary_payload=None):
    return _Session({ta.POSITIONS_URL: [_Resp(200, [FULL_POSITION] if positions_payload is None
                                              else positions_payload)],
                     ta.SUMMARY_URL: [_Resp(200, FULL_SUMMARY if summary_payload is None
                                            else summary_payload)]})


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv("TRADING212_API_KEY", KEY)
    monkeypatch.setenv("TRADING212_API_SECRET", SECRET)


@pytest.fixture
def waits(monkeypatch):
    seen = []
    monkeypatch.setattr(ta, "_sleep", seen.append)
    return seen


# ------------------------------------------------------------------ no real key in the tests
def test_the_tests_never_see_a_real_key():
    """conftest takes the key out of the environment and points trading212 away from .env."""
    assert trading212._auth_headers() is None


# ------------------------------------------------------------------ parsing
def test_a_full_position_payload(keyed):
    [p] = ta.fetch_positions(_account())
    assert p == ta.T212Position(
        t212_ticker="GME_US_EQ", name="GameStop", isin="US36467W1099", currency="USD", quantity=10.0,
        avg_price=23.10, current_price=24.05, created_at="2026-09-28T14:03:11.000+02:00",
        value_eur=207.3, cost_eur=199.0, pnl_eur=8.30, account_currency="EUR")


def test_a_full_summary_payload(keyed):
    s = ta.fetch_summary(_account())
    assert s == ta.T212Summary(currency="EUR", total_value=12345.67, cash_free=2000.0,
                               invested_value=10345.67, invested_cost=10000.0,
                               unrealized_pnl=345.67, realized_pnl=12.5)


def test_a_minimal_payload_leaves_the_rest_none(keyed):
    session = _account([{"instrument": {"ticker": "SAPd_EQ", "isin": "DE0007164600"}, "quantity": 2}], {})
    [p] = ta.fetch_positions(session)
    assert (p.t212_ticker, p.isin, p.quantity) == ("SAPd_EQ", "DE0007164600", 2.0)
    assert (p.name, p.currency, p.avg_price, p.current_price, p.created_at, p.value_eur, p.cost_eur,
            p.pnl_eur, p.account_currency) == (None,) * 9
    assert ta.fetch_summary(session) == ta.T212Summary(None, None, None, None, None, None, None)


def test_missing_or_odd_fields_become_none_never_a_crash(keyed):
    odd = [{"instrument": "not an object", "walletImpact": None, "quantity": "abc", "currentPrice": True,
            "averagePricePaid": float("nan"), "createdAt": 5},
           "not an object either", {}]
    parsed = ta.fetch_positions(_account(odd, {"cash": [], "investments": "x", "totalValue": "12.5"}))
    assert len(parsed) == 3
    assert all(getattr(p, f) is None for p in parsed for f in ta.T212Position.__dataclass_fields__)
    s = ta.fetch_summary(_account(odd, {"cash": [], "investments": "x", "totalValue": "12.5"}))
    assert s.total_value == 12.5 and s.cash_free is None and s.invested_cost is None


@pytest.mark.parametrize("call, payload", [(ta.fetch_positions, {"positions": []}),
                                           (ta.fetch_positions, None),
                                           (ta.fetch_summary, [FULL_SUMMARY]),
                                           (ta.fetch_summary, "summary")])
def test_a_payload_of_the_wrong_type_is_a_value_error(keyed, call, payload):
    session = _Session({ta.POSITIONS_URL: [_Resp(200, payload)], ta.SUMMARY_URL: [_Resp(200, payload)]})
    with pytest.raises(ValueError) as err:
        call(session)
    text = str(err.value)                                   # the payload's type, never the payload
    assert "31337" not in text and "{" not in text and "[" not in text and "summary'" not in text


def test_an_answer_that_is_not_json_is_a_value_error(keyed):
    session = _Session({ta.POSITIONS_URL: [_Resp(200, bad_json=True)]})
    with pytest.raises(ValueError) as err:
        ta.fetch_positions(session)
    assert "<html>" not in str(err.value)


# ------------------------------------------------------------------ errors
def _error(session, call=ta.fetch_positions) -> ta.T212Error:
    with pytest.raises(ta.T212Error) as err:
        call(session)
    return err.value


def test_401_is_a_key_that_does_not_fit(keyed):
    e = _error(_Session({ta.POSITIONS_URL: [_Resp(401, {"code": "Unauthorized"})]}))
    assert str(e) == "ключ Trading 212 не подходит" and not e.needs_key


def test_403_is_a_key_without_the_rights(keyed):
    e = _error(_Session({ta.SUMMARY_URL: [_Resp(403)]}), ta.fetch_summary)
    assert str(e) == "ключу Trading 212 не хватает прав: нужны чтение портфеля и счёта" and e.needs_key


def test_429_is_retried_once_after_5_seconds(keyed, waits):
    session = _Session({ta.POSITIONS_URL: [_Resp(429), _Resp(200, [FULL_POSITION])]})
    assert len(ta.fetch_positions(session)) == 1
    assert waits == [5] and [c[0] for c in session.calls] == ["get", "get"]


def test_429_twice_is_an_error(keyed, waits):
    session = _Session({ta.POSITIONS_URL: [_Resp(429)]})
    e = _error(session)
    assert waits == [5] and len(session.calls) == 2 and "429" in str(e)


@pytest.mark.parametrize("raised, reason", [(requests.ReadTimeout("read timed out, url=x"), "ReadTimeout"),
                                            (requests.ConnectionError("refused"), "ConnectionError")])
def test_a_network_error_is_its_type_name_only(keyed, raised, reason):
    assert str(_error(_Session(raises=raised))) == reason


def test_another_status_is_named_by_its_code(keyed):
    assert str(_error(_Session({ta.POSITIONS_URL: [_Resp(502, "<html>bad gateway</html>")]}))) == "HTTP 502"


def test_no_key_is_an_error_and_nothing_is_called():
    session = _Session({ta.POSITIONS_URL: [_Resp(200, [])]})
    e = _error(session)
    assert str(e) == "ключ Trading 212 не задан" and e.needs_key and session.calls == []


def test_the_key_goes_in_a_basic_auth_header(keyed):
    session = _account()
    ta.fetch_positions(session)
    token = base64.b64encode(f"{KEY}:{SECRET}".encode()).decode()
    assert session.headers == [{"Authorization": f"Basic {token}"}]


def test_no_error_carries_the_key_the_secret_or_the_header(keyed, waits):
    token = base64.b64encode(f"{KEY}:{SECRET}".encode()).decode()
    leaky = requests.exceptions.InvalidHeader(f"Invalid header value: 'Basic {token}' ({KEY}:{SECRET})")
    sessions = [_Session(raises=leaky), _Session(raises=requests.ConnectionError(f"Basic {token}")),
                _Session({ta.POSITIONS_URL: [_Resp(401)]}), _Session({ta.POSITIONS_URL: [_Resp(403)]}),
                _Session({ta.POSITIONS_URL: [_Resp(429)]}), _Session({ta.POSITIONS_URL: [_Resp(500)]})]
    for session in sessions:
        e = _error(session)
        text = f"{e} {e!r} {e.__cause__!r} {e.__context__!r}"
        for secret in (KEY, SECRET, token):
            assert secret not in text


# ------------------------------------------------------------------ instrument -> position key
@pytest.mark.parametrize("t212_ticker, isin, key", [
    ("AAPL_US_EQ", "US0378331005", ("AAPL", None)),                     # a US instrument: its symbol
    ("BRK_B_US_EQ", "US0846707026", ("BRK.B", None)),                   # a class share
    ("SAPd_EQ", "DE0007164600", ("DE0007164600", "T212")),              # anything else: the ISIN
    ("EQNRd_EQ", "no0010096985", ("NO0010096985", "T212")),
    ("VUSAl_EQ", "IE00B3XXRP09", ("IE00B3XXRP09", "T212")),
    ("SAPd_EQ", None, None), ("SAPd_EQ", "not-an-isin", None), (None, None, None)])
def test_the_position_key_of_an_instrument(t212_ticker, isin, key):
    assert ta.position_key(t212_ticker, isin) == key


def test_a_norwegian_isin_maps_to_its_oslo_ticker_for_the_insiders(conn):
    conn.execute("INSERT INTO oslo_isins (ticker, isin, fetched_at) VALUES ('EQNR', 'NO0010096985', "
                 "'2026-09-01T00:00:00')")
    key, source = ta.position_key("EQNRd_EQ", "NO0010096985")
    assert positions.oslo_ticker(conn, key) == "EQNR" and source == positions.T212_SOURCE


# ------------------------------------------------------------------ safety (mandatory)
_SOURCES = [Path(trading212.__file__), Path(ta.__file__)]
_WRITE_CALLS = (".post(", ".put(", ".patch(", ".delete(", "requests.request(", ".request(")
_READ_PATHS = {url.removeprefix("https://live.trading212.com") for url in READ_URLS}


def _violations(source: str) -> list[str]:
    """Everything in a module's source that could write to Trading 212 or reach an endpoint off
    the whitelist: a write call, an orders path, a URL or an API path that is not one of the three
    reads -- in the code, in a comment or in a docstring alike."""
    found = [f"call {call}" for call in _WRITE_CALLS if call in source]
    if "/order" in source:
        found.append("an orders path")
    found += [f"url {url}" for url in re.findall(r"https?://[^\s\"'<>)]+", source) if url not in READ_URLS]
    found += [f"path {path}" for path in re.findall(r"/api/v0/[A-Za-z0-9_/.{}-]*", source)
              if path not in _READ_PATHS]
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            if ("trading212.com" in text or text.startswith(("http://", "https://", "/api", "/equity"))) \
                    and text not in READ_URLS:
                found.append(f"string {text!r}")
    return found


@pytest.mark.parametrize("path", _SOURCES, ids=lambda p: p.name)
def test_the_source_can_only_read_the_whitelisted_urls(path):
    """No .post( / .put( / .patch( / .delete( / requests.request(, every URL string one of the
    whitelisted reads (instruments, positions, account/summary), and no orders path at all."""
    assert _violations(path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("bad", [
    'requests.post(URL, json={"ticker": "GME_US_EQ", "quantity": 1})',
    'session.put(URL)', 'session.patch(URL)', 'session.delete(URL)',
    'requests.request("POST", URL)', 'session.request("DELETE", URL)',
    'URL = "https://live.trading212.com/api/v0/equity/orders/market"',
    'URL = "https://live.trading212.com/api/v0/equity/history/orders"',
    'URL = "https://demo.trading212.com/api/v0/equity/positions"',
    'URL = "https://live.trading212.com/api/v0/equity/pies"',
    'URL = BASE + "/equity/orders/limit"',
    'PATH = "/equity/pies"',
    'PATH = "/api/v0/equity/account/cash"',
    'URL = "https://example.com/collect"',
    '# then call https://live.trading212.com/api/v0/equity/orders'])
def test_the_inspection_catches_a_write_or_a_url_off_the_whitelist(bad):
    assert _violations(f"import requests\n{bad}\n") != []
    assert _violations("import requests\nURL = 'https://live.trading212.com/api/v0/equity/positions'\n"
                       "data = requests.get(URL, headers=h, timeout=20).json()\n") == []


def test_no_other_module_of_the_project_talks_to_trading_212():
    """The two inspected modules are the only ones that name Trading 212's host: nothing else in
    the project can reach it, so nothing else needs the inspection."""
    root = Path(ta.__file__).parent
    files = [f for pattern in ("*.py", "cluster/*.py", "*.sh") for f in root.glob(pattern)]
    assert len(files) > 40                                           # the project's own code was found
    talking = sorted(f.name for f in files if "trading212.com" in f.read_text(encoding="utf-8"))
    assert talking == ["t212_account.py", "trading212.py"]


def test_the_client_only_ever_gets_whitelisted_urls(keyed):
    session = _account()
    ta.fetch_positions(session)
    ta.fetch_summary(session)
    ta.fetch_account(session)
    trading212.fetch_instruments(_Session({trading212.INSTRUMENTS_URL: [_Resp(200, [])]}))
    assert session.calls and all(method == "get" and url in READ_URLS for method, url in session.calls)


def test_the_module_names_its_read_urls():
    assert set(ta.READ_URLS) == READ_URLS


def test_the_instrument_refresh_logs_no_error_text(conn, keyed, capsys):
    """trading212.availability prints why a refresh failed: the type and status only, never the
    exception's own text (a requests error can carry a header)."""
    token = base64.b64encode(f"{KEY}:{SECRET}".encode()).decode()
    trading212.availability(conn, _Session(raises=requests.exceptions.InvalidHeader(f"Basic {token}")))
    out = capsys.readouterr().out
    assert "InvalidHeader" in out and token not in out and KEY not in out


# ------------------------------------------------------------------ sync
NOW = dt.datetime(2026, 10, 1, 14, 5)
SUMMARY = ta.T212Summary("EUR", 12345.67, 2000.0, 10345.67, 10000.0, 345.67, 12.5)
GME_DAYS = [((NOW.date() - dt.timedelta(days=60 - i)).isoformat(), 100.0) for i in range(40)]


def _holding(t212_ticker="GME_US_EQ", isin="US36467W1099", *, qty=10.0, avg=23.10, price=24.05,
             created="2026-09-28T14:03:11.000+02:00", currency="USD", pnl=8.30):
    return ta.T212Position(t212_ticker, None, isin, currency, qty, avg, price, created, None, None, pnl, "EUR")


SAP = dict(t212_ticker="SAPd_EQ", isin="DE0007164600", avg=120.0, price=125.0, currency="EUR")


class _Run:
    """One sync with a stub fetch: the messages it sent and the histories it asked for."""

    def __init__(self, conn):
        self.conn, self.sent, self.histories = conn, [], []

    def __call__(self, *holdings, now=NOW, summary=SUMMARY, fetch=None, notify=None, silent=False):
        def closes(ticker, source=None):
            self.histories.append((ticker, source))
            return GME_DAYS
        return ta.sync(self.conn, fetch=fetch or (lambda: (list(holdings), summary)),
                       notify=notify or (lambda text: self.sent.append(text) or True), now=now,
                       closes_fn=closes, silent=silent)


@pytest.fixture
def run(conn):
    return _Run(conn)


def _synced_before(conn):
    """The account is tracked already: the first-sync message went out, and a holding was stored
    before (it has been sold since). What a sync finds now is new -- not a holding that pre-dates
    tracking, and not something for the first message."""
    db.save_cached_value(conn, ta.SYNCED_ONCE_KEY, 1.0)
    conn.execute("INSERT INTO positions (ticker, opened_at, entry_price, origin, closed_at, close_reason) "
                 "VALUES ('WAS', '2026-01-05', 10.0, 't212', '2026-02-01', ?)", (ta.SOLD_REASON,))
    conn.commit()


def _flag(conn):
    return db.get_cached_value(conn, ta.SYNCED_ONCE_KEY, 10**9)


LEGACY_LINE = "Для уже купленных бумаг правила выхода считаются с сегодняшнего дня."
OLD = "2024-05-01T10:00:00Z"            # bought long before the bot first looked


def _row(conn, ticker):
    return conn.execute("SELECT ticker, source, opened_at, entry_price, insiders, signal_id, stop_pct, origin, "
                        "quantity, t212_ticker, currency, closed_at, close_reason FROM positions "
                        "WHERE ticker = ? ORDER BY id DESC", (ticker,)).fetchone()


def _journal(conn, ticker, members, source="SEC"):
    db.journal_signal(conn, {"source": source, "kind": "cluster", "ticker": ticker, "tier": "buy",
                             "members": json.dumps(members)})


def test_the_first_sync_sends_one_message_for_all_holdings(conn, run):
    result = run(_holding(), _holding(**SAP))
    assert run.sent == ["📥 Слежу за вашими позициями в Trading 212 (2): GME, SAP\n" + LEGACY_LINE]
    assert result.opened == ["GME", "DE0007164600"] and result.error is None and result.at == NOW
    assert {p.ticker: p.origin for p in positions.open_positions(conn)} == {"GME": "t212", "DE0007164600": "t212"}
    assert _flag(conn) is not None
    run(_holding(), _holding(**SAP))
    assert len(run.sent) == 1                                       # nothing new, nothing to say


def test_the_first_message_claims_nothing_it_does_not_watch(conn, run):
    """It lists the holdings and says when their rules start: no «новости», no «инсайдеры» for a
    Frankfurt holding that has neither."""
    run(_holding(), _holding(**SAP))
    assert "новост" not in run.sent[0] and "инсайдер" not in run.sent[0]


def test_an_empty_account_does_not_use_up_the_first_message(conn, run):
    assert run().opened == [] and run.sent == [] and _flag(conn) is None
    run(_holding())                                                 # the first holding: the first message
    assert run.sent == ["📥 Слежу за вашими позициями в Trading 212 (1): GME\n" + LEGACY_LINE]
    assert _flag(conn) is not None


def test_a_new_holding_is_announced_and_opened_with_its_fields(conn, run):
    _synced_before(conn)
    _journal(conn, "GME", ["Ryan Cohen"])
    result = run(_holding())
    assert run.sent == ["📥 Вижу в Trading 212: GME — 10 шт. по 23,10 USD. "
                        "Слежу: стоп, продажи инсайдеров, новости."]
    ticker, source, opened, entry, insiders, signal_id, stop, origin, qty, t212, cur, closed, _ = _row(conn, "GME")
    assert (ticker, source, opened, entry, json.loads(insiders), origin, qty, t212, cur, closed) == \
        ("GME", None, "2026-09-28", 23.10, ["Ryan Cohen"], "t212", 10.0, "GME_US_EQ", "USD", None)
    assert signal_id is not None and stop == 0.10                   # sized like /bought: flat closes
    assert run.histories == [("GME", None)] and result.opened == ["GME"]


def test_a_new_holding_without_its_open_date_opens_today(conn, run):
    _synced_before(conn)
    run(_holding(created=None))
    assert _row(conn, "GME")[2] == NOW.date().isoformat()


def test_a_holding_with_no_yahoo_listing_is_keyed_by_isin_and_sized_on_its_own_prices(conn, run):
    _synced_before(conn)
    run(_holding(**SAP))
    ticker, source, *_ = _row(conn, "DE0007164600")
    assert (ticker, source) == ("DE0007164600", "T212") and run.histories == []     # never Yahoo
    assert run.sent == ["📥 Вижу в Trading 212: SAP — 10 шт. по 120,00 EUR. Слежу: стоп и срок."]
    assert _row(conn, "DE0007164600")[6] is None                   # one day of prices sizes no stop


def test_a_norwegian_holding_watches_the_oslo_insiders(conn, run):
    _synced_before(conn)
    conn.execute("INSERT INTO oslo_isins (ticker, isin, fetched_at) VALUES ('EQNR', 'NO0010096985', "
                 "'2026-09-01T00:00:00')")
    _journal(conn, "EQNR", ["Oslo Boss"], source="NORWAY")
    run(_holding("EQNRd_EQ", "NO0010096985", currency="EUR"))
    assert json.loads(_row(conn, "NO0010096985")[4]) == ["Oslo Boss"]


def test_a_changed_quantity_or_average_is_updated_without_a_message(conn, run):
    _synced_before(conn)
    run(_holding())
    run.sent.clear()
    assert run(_holding()).updated == []                            # nothing changed
    result = run(_holding(qty=15.0, avg=22.0))
    assert result.updated == ["GME"] and result.opened == [] and run.sent == []
    _, _, opened, entry, *_rest = _row(conn, "GME")
    assert (entry, _rest[4], opened) == (22.0, 15.0, "2026-09-28")
    assert len(positions.open_positions(conn)) == 1


def test_a_sold_holding_is_closed_with_a_message(conn, run):
    _synced_before(conn)
    run(_holding(), _holding(**SAP))
    run.sent.clear()
    result = run(_holding(**SAP))
    assert result.closed == ["GME"] and run.sent == ["📤 GME больше нет в Trading 212 — слежение закрыто."]
    assert _row(conn, "GME")[-2:] == (NOW.date().isoformat(), "продано в Trading 212")
    assert [p.ticker for p in positions.open_positions(conn)] == ["DE0007164600"]


@pytest.mark.parametrize("failure", [ta.T212Error("HTTP 502", "status"), ValueError("unexpected positions payload: dict"),
                                     ta.T212Error("ReadTimeout", "network")])
def test_a_failed_fetch_changes_nothing_and_sends_nothing(conn, run, failure):
    _synced_before(conn)
    run(_holding(), _holding(**SAP))
    run.sent.clear()
    before = [conn.execute(f"SELECT * FROM {t}").fetchall() for t in ("positions", "t212_prices", "t212_equity")]

    def fail():
        raise failure
    result = run(fetch=fail, now=NOW + dt.timedelta(days=1))
    assert result.error and result.opened == result.updated == result.closed == [] and run.sent == []
    assert [conn.execute(f"SELECT * FROM {t}").fetchall() for t in ("positions", "t212_prices", "t212_equity")] == before


def test_a_manual_position_in_the_same_name_is_taken_over_quietly(conn, run):
    _synced_before(conn)
    manual = positions.open_position(conn, "GME", 20.0, today=dt.date(2026, 9, 1), closes_fn=lambda t, s=None: [])
    isin = positions.open_position(conn, "DE0007164600", 110.0, today=dt.date(2026, 9, 1), source="BAFIN",
                                   closes_fn=lambda t, s=None: [])
    result = run(_holding(), _holding(**SAP))
    assert run.sent == [] and result.opened == [] and sorted(result.updated) == ["DE0007164600", "GME"]
    by = {p.ticker: p for p in positions.open_positions(conn)}
    assert len(by) == 2 and by["GME"].id == manual.id and by["DE0007164600"].id == isin.id
    assert (by["GME"].origin, by["GME"].quantity, by["GME"].entry_price, by["GME"].t212_ticker,
            by["GME"].currency, by["GME"].opened_at) == ("t212", 10.0, 23.10, "GME_US_EQ", "USD", "2026-09-01")
    assert (by["DE0007164600"].source, by["DE0007164600"].entry_price) == ("T212", 120.0)   # priced from its snapshots now


def test_the_days_price_and_account_rows_are_replaced_within_a_day(conn, run):
    run(_holding(price=24.05), now=NOW.replace(hour=10))
    run(_holding(price=25.00), summary=dataclasses.replace(SUMMARY, total_value=13000.0), now=NOW.replace(hour=16))
    assert conn.execute("SELECT ticker, date, price FROM t212_prices").fetchall() == [("GME", "2026-10-01", 25.0)]
    assert conn.execute("SELECT * FROM t212_equity").fetchall() == \
        [("2026-10-01", 13000.0, 10345.67, 10000.0, 2000.0, "EUR")]
    run(_holding(price=26.00), now=NOW + dt.timedelta(days=1))
    assert conn.execute("SELECT COUNT(*) FROM t212_prices").fetchone() == (2,)
    assert conn.execute("SELECT COUNT(*) FROM t212_equity").fetchone() == (2,)


def test_with_no_key_the_sync_is_a_quiet_no_op_logged_once(conn, monkeypatch, capsys):
    monkeypatch.setattr(ta, "_no_key_logged", False)
    sent = []
    for _ in range(3):
        result = ta.sync(conn, notify=sent.append, now=NOW)
        assert result.error == ta.NO_KEY and result.opened == result.updated == result.closed == []
    assert sent == [] and conn.execute("SELECT COUNT(*) FROM positions").fetchone() == (0,)
    assert conn.execute("SELECT COUNT(*) FROM t212_equity").fetchone() == (0,)
    assert db.get_cached_value(conn, ta.SYNCED_ONCE_KEY, 10**9) is None
    out = capsys.readouterr()
    assert (out.out + out.err).count("Trading 212") == 1


def test_a_failed_notification_does_not_undo_the_sync(conn, run):
    _synced_before(conn)

    def broken(text):
        raise RuntimeError("telegram is down")
    result = run(_holding(), notify=broken)
    assert result.opened == ["GME"] and positions.find_open(conn, "GME").origin == "t212"
    assert run(_holding(), notify=lambda text: False).opened == []            # and it is not opened twice


def test_a_holding_that_cannot_be_keyed_holds_back_the_sold_check(conn, run):
    """An instrument with neither a US ticker nor an ISIN can't be matched: rather than close a
    position it might be, the sync closes nothing this time."""
    _synced_before(conn)
    run(_holding())
    result = run(_holding("ODDd_EQ", None))
    assert result.closed == [] and positions.find_open(conn, "GME") is not None


def test_the_sync_records_when_it_ran(conn, run):
    assert ta.last_sync(conn) is None
    run(_holding())
    assert ta.last_sync(conn) == NOW


def test_a_failure_while_applying_rolls_everything_back(conn, run, monkeypatch):
    _synced_before(conn)

    def boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(ta, "_open_holding", boom)
    with pytest.raises(RuntimeError):
        run(_holding())
    for table in ("t212_prices", "t212_equity"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
    assert positions.open_positions(conn) == []
    assert ta.last_sync(conn) is None and run.sent == []


def test_two_syncs_at_once_do_not_open_the_same_holding_twice(tmp_path):
    """The Telegram bot and the daily run are two processes. While this sync asks Yahoo for the new
    holding's history, the other one opens it: this one must see that before it writes."""
    path = tmp_path / "shared.db"
    mine, other = db.connect(path), db.connect(path)
    for c in (mine, other):
        db.save_cached_value(c, ta.SYNCED_ONCE_KEY, 1.0)
    sent = []

    def while_yahoo_answers(ticker, source=None):
        ta.sync(other, fetch=lambda: ([_holding()], SUMMARY), notify=sent.append, now=NOW,
                closes_fn=lambda t, s=None: [])
        return GME_DAYS
    result = ta.sync(mine, fetch=lambda: ([_holding()], SUMMARY), notify=sent.append, now=NOW,
                     closes_fn=while_yahoo_answers)
    assert result.opened == [] and len(sent) == 1                   # the other one announced it
    assert mine.execute("SELECT COUNT(*) FROM positions WHERE ticker = 'GME'").fetchone() == (1,)
    mine.close()
    other.close()


def test_a_sync_that_cannot_get_the_database_changes_nothing(tmp_path, capsys):
    path = tmp_path / "shared.db"
    mine, other = db.connect(path), db.connect(path)
    mine.execute("PRAGMA busy_timeout = 50")
    other.execute("BEGIN IMMEDIATE")                                # another writer holds it
    with pytest.raises(sqlite3.OperationalError):
        ta.sync(mine, fetch=lambda: ([_holding()], SUMMARY), notify=lambda t: pytest.fail("nothing to say"),
                now=NOW, closes_fn=lambda t, s=None: [])
    other.rollback()
    assert mine.execute("SELECT COUNT(*) FROM positions").fetchone() == (0,)
    assert ta.sync(mine, fetch=lambda: ([_holding()], SUMMARY), notify=lambda t: True, now=NOW,
                   closes_fn=lambda t, s=None: []).opened == ["GME"]          # and the next one goes through
    mine.close()
    other.close()


def test_the_sync_reads_through_the_client_and_only_gets(conn, keyed):
    session = _account()
    result = ta.sync(conn, fetch=lambda: ta.fetch_account(session), notify=lambda t: True, now=NOW,
                     closes_fn=lambda t, s=None: [])
    assert result.opened == ["GME"]
    assert session.calls == [("get", ta.POSITIONS_URL), ("get", ta.SUMMARY_URL)]


def test_a_holding_with_no_price_at_all_waits_for_one(conn, run, capsys):
    result = run(_holding(avg=None, price=None), _holding(**SAP))
    assert result.opened == ["DE0007164600"] and positions.find_open(conn, "GME") is None
    assert run.sent == ["📥 Слежу за вашими позициями в Trading 212 (1): SAP\n" + LEGACY_LINE]
    assert "GME" in capsys.readouterr().err
    run(_holding(avg=23.10, price=24.05), _holding(**SAP))          # the price arrived
    assert positions.find_open(conn, "GME").entry_price == 23.10


def test_a_missing_average_enters_at_the_price_now_and_never_overwrites_an_entry(conn, run):
    _synced_before(conn)
    run(_holding(avg=None, price=24.05))
    assert positions.find_open(conn, "GME").entry_price == 24.05
    assert run(_holding(avg=None, price=30.0)).updated == []       # no average told: the entry stays
    assert positions.find_open(conn, "GME").entry_price == 24.05


def test_the_messages_show_fractional_shares_and_escape_what_trading_212_names(conn, run):
    _synced_before(conn)
    run(_holding("A&Bd_EQ", "DE000A1EWWW0", qty=0.52347, avg=1234.5, currency="EUR"))
    assert run.sent == ["📥 Вижу в Trading 212: A&amp;B — 0,5235 шт. по 1 234,50 EUR. Слежу: стоп и срок."]
    run()
    assert run.sent[-1] == "📤 A&amp;B больше нет в Trading 212 — слежение закрыто."


def test_the_same_isin_held_on_two_exchanges_is_one_position(conn, run):
    _synced_before(conn)
    result = run(_holding("VUSAl_EQ", "IE00B3XXRP09", currency="GBP"), _holding("VUSAd_EQ", "IE00B3XXRP09",
                                                                             currency="EUR"))
    assert result.opened == ["IE00B3XXRP09"] and len(positions.open_positions(conn)) == 1
    assert positions.find_open(conn, "IE00B3XXRP09").t212_ticker == "VUSAl_EQ"


def test_a_sync_opens_no_position_twice_and_reopens_one_bought_back(conn, run):
    _synced_before(conn)
    run(_holding())
    run()                                                           # sold
    run.sent.clear()
    result = run(_holding(created="2026-10-01T09:00:00Z"))          # bought back
    assert result.opened == ["GME"] and len(run.sent) == 1 and run.sent[0].startswith("📥 Вижу")
    rows = conn.execute("SELECT closed_at FROM positions WHERE ticker = 'GME' ORDER BY id").fetchall()
    assert rows == [(NOW.date().isoformat(),), (None,)]


# ---------------------------------------------- R1: holdings that pre-date tracking ("legacy")
def _exits(conn, today, price, closes=()):
    return positions.check_exits(conn, today=today, price_fn=lambda t, s=None: price,
                                 closes_fn=lambda t, s=None: list(closes), news_fn=lambda t, s=None: [])


def _since(*closes):
    """Completed closes from the day tracking began."""
    return [((NOW.date() + dt.timedelta(days=i)).isoformat(), c) for i, c in enumerate(closes)]


def test_a_holding_that_pre_dates_tracking_starts_its_clock_and_its_stop_at_the_first_sync(conn, run):
    run(_holding(created=OLD, avg=100.0, price=50.0))
    pos = positions.find_open(conn, "GME")
    assert (pos.opened_at, pos.entry_price, pos.stop_base, pos.t212_created) == \
        (NOW.date().isoformat(), 100.0, 50.0, "2024-05-01")       # the entry stays Trading 212's average
    assert pos.stop_pct == 0.10 and run.histories == [("GME", None)]   # sized as usual, from the history before


def test_a_legacy_holding_deep_under_water_raises_no_alert_at_the_first_check(conn, run):
    """Bought at 100 two years ago, at 50 now, once at 200: with the old rules it would be stopped
    out (and a year old, and dead money) the day the account is connected."""
    run(_holding(created=OLD, avg=100.0, price=50.0))
    highs = [((NOW.date() - dt.timedelta(days=400 - i)).isoformat(), 200.0) for i in range(300)]
    assert _exits(conn, NOW.date(), 50.0, highs) == []
    assert _exits(conn, NOW.date() + dt.timedelta(days=1), 46.0, highs + _since(50.0)) == []


def test_a_fall_of_the_stop_from_the_peak_since_tracking_fires_for_a_legacy_holding(conn, run):
    run(_holding(created=OLD, avg=100.0, price=50.0))
    later, closes = NOW.date() + dt.timedelta(days=5), _since(50.0, 55.0, 60.0, 58.0)
    assert _exits(conn, later, 54.1, closes) == []
    [alert] = _exits(conn, later, 54.0, closes)
    assert alert.trigger == "trailing_stop" and alert.detail == "−10% от максимума 60.00"


def test_the_year_counts_from_the_tracking_start_for_a_legacy_holding(conn, run):
    import model
    run(_holding(created=OLD, avg=100.0, price=110.0))             # held two years already
    day = NOW.date()
    assert _exits(conn, day, 110.0) == []
    assert _exits(conn, day + dt.timedelta(days=model.MAX_HOLD_DAYS - 1), 110.0) == []
    [alert] = _exits(conn, day + dt.timedelta(days=model.MAX_HOLD_DAYS), 110.0)
    assert alert.trigger == "time" and alert.detail == f"{model.MAX_HOLD_DAYS} дн. в позиции"


def test_dead_money_counts_its_days_from_the_tracking_start_and_its_result_from_the_average(conn, run):
    import model
    import paper
    run(_holding(created=OLD, avg=100.0, price=101.0))             # +1% on the average price paid
    day = NOW.date()
    while paper.business_days_between(NOW.date().isoformat(), day) < model.DEAD_MONEY_BDAYS:
        day += dt.timedelta(days=1)
    assert _exits(conn, day - dt.timedelta(days=1), 101.0) == []
    [alert] = _exits(conn, day, 101.0)
    assert alert.trigger == "dead_money"
    assert _exits(conn, day, 106.0) == []                           # +6% on the average: not dead money


def test_a_holding_first_seen_after_the_first_sync_opens_at_its_trading_212_date(conn, run):
    run(_holding(**SAP))                                            # the first sync: SAP pre-dates tracking
    run(_holding(**SAP), _holding(created="2026-09-28T14:03:11.000+02:00"))
    gme, sap = positions.find_open(conn, "GME"), positions.find_open(conn, "DE0007164600")
    assert (gme.opened_at, gme.entry_price, gme.stop_base, gme.t212_created) == \
        ("2026-09-28", 23.10, None, "2026-09-28")
    assert (sap.opened_at, sap.stop_base) == (NOW.date().isoformat(), 125.0)


def test_legacy_keys_on_whether_a_holding_was_ever_stored_not_on_the_message_flag(conn, run):
    db.save_cached_value(conn, ta.SYNCED_ONCE_KEY, 1.0)             # the flag alone: nothing stored yet
    run(_holding(created=OLD, price=20.0))
    pos = positions.find_open(conn, "GME")
    assert (pos.opened_at, pos.stop_base) == (NOW.date().isoformat(), 20.0)
    assert run.sent == ["📥 Вижу в Trading 212: GME — 10 шт. по 23,10 USD. "
                        "Слежу: стоп, продажи инсайдеров, новости."]       # the flag decides the message only


def test_a_sold_holding_still_counts_as_stored_before(conn, run):
    run(_holding(**SAP))
    run()                                                           # sold: no holding is open any more
    run(_holding(created="2026-09-28T14:03:11.000+02:00"))
    assert positions.find_open(conn, "GME").opened_at == "2026-09-28"


def test_a_bought_position_taken_over_at_the_first_sync_keeps_its_own_clock(conn, run):
    manual = positions.open_position(conn, "GME", 20.0, today=dt.date(2026, 9, 1), closes_fn=lambda t, s=None: [])
    run(_holding(created=OLD))
    pos = positions.find_open(conn, "GME")
    assert (pos.id, pos.origin, pos.opened_at, pos.stop_base, pos.t212_created) == \
        (manual.id, "t212", "2026-09-01", None, "2024-05-01")     # the bot has watched it since /bought


def test_without_a_price_from_trading_212_the_stop_base_is_the_last_close_of_the_history(conn, run):
    run(_holding(created=OLD, avg=140.0, price=None))
    assert positions.find_open(conn, "GME").stop_base == GME_DAYS[-1][1] == 100.0


def test_a_tracked_holding_learns_its_trading_212_date_when_it_is_told_later(conn, run):
    _synced_before(conn)
    run(_holding(created=None))
    assert positions.find_open(conn, "GME").t212_created is None
    run(_holding(created="2026-09-28T14:03:11.000+02:00"))
    pos = positions.find_open(conn, "GME")
    assert (pos.t212_created, pos.opened_at) == ("2026-09-28", NOW.date().isoformat())   # its clock is not moved


# ---------------------------------------------- R2: a message names only what is really watched
_OSLO = dict(t212_ticker="EQNRd_EQ", isin="NO0010096985", avg=25.0, price=26.0, currency="EUR")


def _watched(run, **holding):
    run(_holding(**holding))
    return run.sent[-1].split(". Слежу: ")[1]


def test_a_us_holding_is_watched_for_its_stop_its_insiders_and_its_news(conn, run):
    _synced_before(conn)
    assert _watched(run) == "стоп, продажи инсайдеров, новости."


def test_a_holding_with_no_news_feed_and_no_matched_insiders_is_watched_for_its_stop_and_its_time(conn, run):
    _synced_before(conn)
    _journal(conn, "SAP", ["US Boss"], source="SEC")               # the US SAP: not this listing's insiders
    assert _watched(run, **SAP) == "стоп и срок."


@pytest.mark.parametrize("source", ["BAFIN", "SWEDEN"])
def test_insiders_the_journal_matches_by_isin_are_named(conn, run, source):
    _synced_before(conn)
    _journal(conn, "DE0007164600", ["Vorstand"], source=source)
    assert _watched(run, **SAP) == "стоп и срок, а также продажи инсайдеров."
    assert positions.find_open(conn, "DE0007164600").insiders == ["Vorstand"]


def test_an_oslo_company_is_watched_for_its_oslo_insiders_and_its_oslo_news(conn, run):
    _synced_before(conn)
    conn.execute("INSERT INTO oslo_isins (ticker, isin, fetched_at) VALUES ('EQNR', 'NO0010096985', "
                 "'2026-09-01T00:00:00')")
    _journal(conn, "EQNR", ["Oslo Boss"], source="NORWAY")
    assert _watched(run, **_OSLO) == "стоп и срок, а также продажи инсайдеров и новости."


def test_an_oslo_company_with_no_signal_is_watched_for_its_news_but_not_for_insiders(conn, run):
    _synced_before(conn)
    conn.execute("INSERT INTO oslo_isins (ticker, isin, fetched_at) VALUES ('EQNR', 'NO0010096985', "
                 "'2026-09-01T00:00:00')")
    assert _watched(run, **_OSLO) == "стоп и срок, а также новости."


def test_a_signal_with_no_names_matches_no_insiders(conn, run):
    _synced_before(conn)
    _journal(conn, "DE0007164600", [], source="BAFIN")
    assert _watched(run, **SAP) == "стоп и срок."


# ---------------------------------------------- R3: a silent sync leaves the first message
def test_a_silent_sync_sends_nothing_and_leaves_the_first_message_for_a_notifying_one(conn, run):
    result = run(_holding(created=OLD), _holding(**SAP), silent=True)
    assert result.opened == ["GME", "DE0007164600"] and run.sent == [] and _flag(conn) is None
    assert positions.find_open(conn, "GME").stop_base == 24.05     # the legacy rule does not wait for a message
    result = run(_holding(created=OLD), _holding(**SAP), now=NOW + dt.timedelta(hours=1))
    assert result.opened == [] and _flag(conn) is not None          # nothing new, yet everything is listed
    assert run.sent == ["📥 Слежу за вашими позициями в Trading 212 (2): GME, SAP\n" + LEGACY_LINE]
    run(_holding(created=OLD), _holding(**SAP), now=NOW + dt.timedelta(hours=2))
    assert len(run.sent) == 1                                       # once


def test_the_first_message_names_the_day_the_rules_started_when_it_is_not_today(conn, run):
    run(_holding(created=OLD), silent=True)                         # 01.10
    run(_holding(created=OLD), now=NOW + dt.timedelta(days=2))
    assert run.sent == ["📥 Слежу за вашими позициями в Trading 212 (1): GME\n"
                        "Для уже купленных бумаг правила выхода считаются с 01.10."]


def test_a_holding_bought_between_a_silent_sync_and_the_first_message_is_listed_but_not_legacy(conn, run):
    run(_holding(**SAP), silent=True)
    run(_holding(**SAP), _holding(created="2026-09-30T09:00:00Z"))
    assert run.sent == ["📥 Слежу за вашими позициями в Trading 212 (2): SAP, GME\n" + LEGACY_LINE]
    gme = positions.find_open(conn, "GME")
    assert (gme.opened_at, gme.stop_base) == ("2026-09-30", None)


def test_the_first_message_has_no_legacy_line_when_nothing_pre_dates_tracking(conn, run):
    """Everything the account holds was /bought before: the bot has watched it since."""
    positions.open_position(conn, "GME", 20.0, today=dt.date(2026, 9, 1), closes_fn=lambda t, s=None: [])
    run(_holding())
    assert run.sent == ["📥 Слежу за вашими позициями в Trading 212 (1): GME"]


def test_a_notifying_sync_whose_send_fails_has_still_used_the_first_message(conn, run):
    def broken(text):
        raise RuntimeError("telegram is down")
    run(_holding(), notify=broken)
    assert _flag(conn) is not None                                  # it tried, with notifications on
    run(_holding(), _holding(**SAP))
    assert run.sent == ["📥 Вижу в Trading 212: SAP — 10 шт. по 120,00 EUR. Слежу: стоп и срок."]


def test_a_silent_sync_after_the_first_message_says_nothing_either(conn, run):
    _synced_before(conn)
    assert run(_holding(), silent=True).opened == ["GME"] and run.sent == []
    assert run(silent=True).closed == ["GME"] and run.sent == []
    assert _flag(conn) is not None


def test_a_silent_sync_never_calls_notify(conn):
    result = ta.sync(conn, fetch=lambda: ([_holding()], SUMMARY), now=NOW, closes_fn=lambda t, s=None: [],
                     notify=lambda text: pytest.fail("a silent sync sends nothing"), silent=True)
    assert result.opened == ["GME"]


# ------------------------------------------------------------------ /portfolio's live view
TODAY = NOW.date()


@pytest.fixture
def no_yahoo(monkeypatch):
    """A US holding's stop line reads Yahoo's closes: none here, and no Yahoo price is asked for
    (Trading 212's own price is what /portfolio shows)."""
    import paper
    monkeypatch.setattr(paper, "_closes", lambda symbol, days: [])

    def refuse(symbol):
        raise AssertionError("the price of a Trading 212 holding is Trading 212's")
    monkeypatch.setattr(positions, "_yahoo_close", refuse)


def _view(conn, *holdings, summary=SUMMARY, fetch=None, now=NOW):
    return ta.portfolio_view(conn, now.date(), fetch=fetch or (lambda: (list(holdings), summary)), now=now)


def _fails(error):
    def fetch():
        raise error
    return fetch


def test_the_live_view_is_what_the_account_holds_now_with_each_tracked_holdings_status(conn, run, no_yahoo):
    _synced_before(conn)
    _journal(conn, "GME", ["Ryan Cohen"])
    run(_holding(price=24.05))
    view = _view(conn, _holding(qty=12.0, avg=23.50, price=25.00, pnl=15.5))
    assert (view.error, view.hint, view.as_of, view.summary) == (None, None, None, SUMMARY)
    [h] = view.holdings
    assert (h.name, h.quantity, h.avg_price, h.price, h.currency, h.pnl, h.pnl_currency, h.opened) == \
        ("GME", 12.0, 23.50, 25.00, "USD", 15.5, "EUR", "2026-09-28")     # the account's own figures, now
    assert h.position.ticker == "GME" and h.position.insiders == ["Ryan Cohen"]
    assert h.status["last"] == 25.00 and h.status["stop_pct"] == 0.10     # the stop the sync fixed
    assert h.model_holds is False and h.days == 3                   # bought 28.09, today is 01.10


def test_a_legacy_holding_is_shown_with_its_real_result_and_its_days_since_the_purchase(conn, run, no_yahoo):
    """Its rules count from the tracking start, but what the owner sees is what Trading 212 knows:
    the result on the average price paid and the days since it was bought there."""
    run(_holding(created=OLD, avg=100.0, price=50.0))               # the first sync
    held = (TODAY - dt.date(2024, 5, 1)).days
    [h] = _view(conn, _holding(created=OLD, avg=100.0, price=50.0, pnl=-431.0)).holdings
    assert (h.days, h.opened, h.avg_price, h.price, h.pnl) == (held, "2024-05-01", 100.0, 50.0, -431.0)
    assert (h.status["peak"], h.status["result"]) == (50.0, pytest.approx(-0.50))
    assert h.status["stop_level"] == pytest.approx(45.0)            # the stop is measured from the floor
    [s] = _view(conn, fetch=_fails(ta.T212Error("HTTP 500", "status"))).holdings
    assert (s.days, s.opened, s.avg_price, s.price) == (held, "2024-05-01", 100.0, 50.0)
    assert s.pnl == pytest.approx(-500.0) and s.status["peak"] == 50.0


def test_the_days_are_left_out_when_the_purchase_date_is_not_known(conn, run, no_yahoo):
    run(_holding(created=None))
    assert _view(conn, _holding(created=None)).holdings[0].days is None
    [stored] = _view(conn, fetch=_fails(ta.T212Error("HTTP 500", "status"))).holdings
    assert stored.days is None and stored.opened == TODAY.isoformat()      # ordered by its tracking start then
    assert _view(conn, _holding(created="2026-09-30T08:00:00Z")).holdings[0].days == 1   # told live: known


def test_the_live_view_stores_the_days_prices_and_account_but_is_not_a_sync(conn, run, no_yahoo):
    _synced_before(conn)
    run(_holding(price=24.05), now=NOW.replace(hour=9))
    synced = ta.last_sync(conn)
    view = _view(conn, _holding(price=25.00), _holding(**SAP),
                 summary=dataclasses.replace(SUMMARY, total_value=13000.0))
    assert conn.execute("SELECT ticker, price FROM t212_prices ORDER BY ticker").fetchall() == \
        [("DE0007164600", 125.0), ("GME", 25.0)]
    assert conn.execute("SELECT total_value FROM t212_equity").fetchall() == [(13000.0,)]
    # SAP was bought since the last sync: it is shown, but opening it is the sync's job
    assert [p.ticker for p in positions.open_positions(conn)] == ["GME"] and ta.last_sync(conn) == synced
    sap = next(h for h in view.holdings if h.name == "SAP")
    assert (sap.position, sap.status, sap.quantity, sap.price, sap.currency) == (None, None, 10.0, 125.0, "EUR")


def test_the_live_view_does_not_show_a_holding_that_was_sold_since_the_sync(conn, run, no_yahoo):
    _synced_before(conn)
    run(_holding(), _holding(**SAP))
    view = _view(conn, _holding(**SAP))
    assert [h.name for h in view.holdings] == ["SAP"]
    assert positions.find_open(conn, "GME") is not None            # closing it is the sync's job


def test_when_the_call_fails_the_view_is_the_stored_holdings_with_the_reason_and_their_time(conn, run, no_yahoo):
    _synced_before(conn)
    run(_holding(qty=10.0, avg=23.10, price=24.05), _holding(**SAP))
    view = _view(conn, fetch=_fails(ta.T212Error("HTTP 502", "status")), now=NOW.replace(hour=15, minute=30))
    assert (view.error, view.hint, view.as_of, view.summary) == ("HTTP 502", None, "14:05", None)
    by = {h.name: h for h in view.holdings}
    gme = by["GME"]
    assert (gme.quantity, gme.avg_price, gme.price, gme.currency) == (10.0, 23.10, 24.05, "USD")
    assert gme.pnl == pytest.approx(9.5) and gme.pnl_currency == "USD"      # 10 x (24,05 - 23,10), in its own currency
    assert gme.status["last"] == 24.05 and gme.position.origin == "t212"
    assert (by["SAP"].price, by["SAP"].pnl, by["SAP"].pnl_currency) == (125.0, pytest.approx(50.0), "EUR")
    assert conn.execute("SELECT COUNT(*) FROM t212_equity").fetchone() == (1,)     # nothing new stored


def test_the_stored_datas_time_names_the_day_when_it_is_not_today(conn, run, no_yahoo):
    run(_holding())
    view = _view(conn, fetch=_fails(ta.T212Error("ReadTimeout", "network")), now=NOW + dt.timedelta(days=1))
    assert view.as_of == "01.10 14:05"


def test_before_any_sync_the_stored_data_have_no_time(conn):
    view = _view(conn, fetch=_fails(ta.T212Error("ReadTimeout", "network")))
    assert (view.as_of, view.holdings, view.error) == (None, [], "ReadTimeout")


@pytest.mark.parametrize("error, reason, hint", [
    (ta.T212Error(ta.NO_RIGHTS, "forbidden"), ta.NO_RIGHTS, ta.KEY_HINT),
    (ta.T212Error(ta.NO_KEY, "no_key"), ta.NO_KEY, ta.KEY_HINT),
    (ta.T212Error(ta.BAD_KEY, "unauthorized"), ta.BAD_KEY, None),
    (ValueError("unexpected positions payload: dict with secrets"), "ответ не разобран", None)])
def test_a_key_problem_carries_the_hint_and_a_bad_answer_a_fixed_reason(conn, error, reason, hint):
    view = _view(conn, fetch=_fails(error))
    assert (view.error, view.hint, view.holdings) == (reason, hint, [])


def test_with_no_key_the_view_asks_nothing_and_says_so(conn):
    view = ta.portfolio_view(conn, TODAY, now=NOW)                  # the real client: no key in the tests
    assert (view.error, view.hint) == (ta.NO_KEY, ta.KEY_HINT)


def _model_holds(conn, ticker, source="SEC"):
    conn.execute(
        "INSERT INTO paper_positions (book, ticker, source, symbol, currency, fill_date, cost_eur, "
        "net_eur, entry_close, entry_fx) VALUES ('MODEL-S', ?, ?, ?, 'USD', '2026-09-25', 1000, 998, 40, 1.16)",
        (ticker, source, ticker))
    conn.commit()


def test_the_view_says_when_the_model_holds_the_same_name(conn, run, no_yahoo):
    _synced_before(conn)
    conn.execute("INSERT INTO oslo_isins (ticker, isin, fetched_at) VALUES ('EQNR', 'NO0010096985', "
                 "'2026-09-01T00:00:00')")
    _model_holds(conn, "GME")
    _model_holds(conn, "EQNR", source="NORWAY")                     # the model holds Equinor in Oslo
    held = [_holding(), _holding(**SAP), _holding("EQNRd_EQ", "NO0010096985", currency="EUR")]
    run(*held)
    assert {h.name: h.model_holds for h in _view(conn, *held).holdings} == \
        {"GME": True, "SAP": False, "EQNR": True}
    stored = _view(conn, fetch=_fails(ta.T212Error("HTTP 500", "status")))
    assert {h.name: h.model_holds for h in stored.holdings} == {"GME": True, "SAP": False, "EQNR": True}


def test_the_view_shows_a_holding_it_cannot_key_by_whatever_names_it(conn, no_yahoo):
    odd = ta.T212Position("ODDd_EQ", "Odd Plc", None, "GBX", 3.0, 100.0, 110.0, None, None, None, 1.0, "EUR")
    nameless = ta.T212Position(None, None, None, None, 1.0, 5.0, 6.0, None, None, None, None, None)
    view = _view(conn, odd, nameless)
    assert [h.name for h in view.holdings] == ["ODD", "?"] and all(h.position is None for h in view.holdings)


def test_a_busy_database_does_not_cost_the_live_view(conn, no_yahoo, monkeypatch, capsys):
    """Storing the day's prices is a side job of /portfolio: when the daily run holds the database,
    the account is still shown."""
    import sqlite3

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(ta, "_store_snapshot", locked)
    view = _view(conn, _holding())
    assert view.error is None and [h.name for h in view.holdings] == ["GME"] and view.summary == SUMMARY
    assert "OperationalError" in capsys.readouterr().err


def test_the_live_view_only_gets(conn, keyed, no_yahoo):
    session = _account()
    view = ta.portfolio_view(conn, TODAY, fetch=lambda: ta.fetch_account(session), now=NOW)
    assert view.error is None and session.calls == [("get", ta.POSITIONS_URL), ("get", ta.SUMMARY_URL)]


# ------------------------------------------------------------------ the command line
def _wire(monkeypatch, session):
    """The module's own HTTP entry point answers from `session`: the whole path runs, offline."""
    monkeypatch.setattr(ta.requests, "get", session.get)


def test_check_says_the_key_works_with_the_count_and_the_currency_and_nothing_else(keyed, monkeypatch, capsys):
    session = _account([FULL_POSITION, FULL_POSITION])
    _wire(monkeypatch, session)
    assert ta.main(["--check"]) == 0
    out = capsys.readouterr()
    assert out.out == "Trading 212: доступ есть, позиций 2, валюта EUR\n" and out.err == ""
    assert session.calls == [("get", ta.POSITIONS_URL), ("get", ta.SUMMARY_URL)]


def test_check_prints_nothing_that_identifies_the_account_or_the_key(keyed, monkeypatch, capsys):
    _wire(monkeypatch, _account())
    ta.main(["--check"])
    out = capsys.readouterr()
    token = base64.b64encode(f"{KEY}:{SECRET}".encode()).decode()
    for private in ("31337", "12345", "12 345", "2000", "GME", "GameStop", "US36467W1099", KEY, SECRET, token,
                    "Basic", "Authorization"):
        assert private not in out.out + out.err


@pytest.mark.parametrize("status, line", [
    (401, "Trading 212: доступа нет — ключ Trading 212 не подходит\n"),
    (500, "Trading 212: доступа нет — HTTP 500\n"),
    (403, "Trading 212: доступа нет — ключу Trading 212 не хватает прав: нужны чтение портфеля и счёта\n"
          + ta.KEY_HINT + "\n")])
def test_check_says_why_it_cannot_read_the_account(keyed, monkeypatch, capsys, status, line):
    _wire(monkeypatch, _Session({ta.POSITIONS_URL: [_Resp(status)], ta.SUMMARY_URL: [_Resp(status)]}))
    assert ta.main(["--check"]) == 1
    assert capsys.readouterr().out == line


def test_check_needs_the_account_rights_too(keyed, monkeypatch, capsys):
    """A key that may read the positions but not the account is not enough."""
    _wire(monkeypatch, _Session({ta.POSITIONS_URL: [_Resp(200, [])], ta.SUMMARY_URL: [_Resp(403)]}))
    assert ta.main(["--check"]) == 1 and "не хватает прав" in capsys.readouterr().out


def test_check_without_a_key_says_so_and_calls_nothing(monkeypatch, capsys):
    session = _account()
    _wire(monkeypatch, session)
    assert ta.main(["--check"]) == 1
    assert capsys.readouterr().out == "Trading 212: доступа нет — ключ Trading 212 не задан\n" + ta.KEY_HINT + "\n"
    assert session.calls == []


def test_check_with_an_answer_of_the_wrong_shape(keyed, monkeypatch, capsys):
    _wire(monkeypatch, _account({"positions": [FULL_POSITION]}))
    assert ta.main(["--check"]) == 1
    out = capsys.readouterr().out
    assert out == "Trading 212: доступа нет — ответ не разобран\n" and "GME" not in out


def test_the_sync_command_runs_one_sync_and_sends_nothing(keyed, monkeypatch, capsys, tmp_path):
    session = _account()
    _wire(monkeypatch, session)
    monkeypatch.setattr(ta, "DB_PATH", tmp_path / "data" / "d.db")       # the folder is made
    monkeypatch.setattr(ta.telegram_notify, "send_text", lambda text: pytest.fail("--sync must not send"))
    monkeypatch.setattr(positions, "daily_closes", lambda ticker, source=None: [])   # the stop's history
    assert ta.main(["--sync"]) == 0
    assert capsys.readouterr().out == "Trading 212: синхронизация прошла — открыто 1, обновлено 0, закрыто 0\n"
    stored = db.connect(tmp_path / "data" / "d.db")
    assert [(p.ticker, p.origin) for p in positions.open_positions(stored)] == [("GME", "t212")]
    assert _flag(stored) is None                    # the one-time first message is left for the bot
    stored.close()
    assert session.calls == [("get", ta.POSITIONS_URL), ("get", ta.SUMMARY_URL)]


def test_the_sync_command_says_why_it_did_nothing(keyed, monkeypatch, capsys, tmp_path):
    _wire(monkeypatch, _Session({ta.POSITIONS_URL: [_Resp(401)]}))
    monkeypatch.setattr(ta, "DB_PATH", tmp_path / "d.db")
    assert ta.main(["--sync"]) == 1
    assert capsys.readouterr().out == "Trading 212: синхронизация не прошла — ключ Trading 212 не подходит\n"


@pytest.mark.parametrize("argv", [[], ["--check", "--sync"], ["--order"]])
def test_the_command_line_takes_exactly_one_of_check_and_sync(argv, capsys):
    with pytest.raises(SystemExit) as stop:
        ta.main(argv)
    assert stop.value.code == 2


# ------------------------------------------------------------------ the README
def test_the_readme_says_what_is_read_how_often_and_that_nothing_is_traded():
    readme = (Path(ta.__file__).parent / "README.md").read_text(encoding="utf-8")
    start = readme.index("### Trading 212\n")
    section = " ".join(readme[start:readme.index("\n### ", start + 5)].split())
    for phrase in ("`/equity/positions`", "`/equity/account/summary`", "только чтение",     # what is read
                   "Каждые 15 минут", "ежедневном прогоне",                                # how often
                   "📥 Слежу за вашими позициями", "📥 Вижу в Trading 212", "📤",            # the notifications
                   "`/portfolio` — живой", "💼 Trading 212", "✍️ Вне Trading 212",
                   "python t212_account.py --check",
                   "никогда не размещает"):                                                # no orders, ever
        assert phrase in section, phrase
    key = " ".join(readme[readme.index("### Ключ Trading 212\n"):].split())[:1500]
    assert "Portfolio" in key and "Account data" in key and "не включайте" in key   # a read-only key


def test_the_readme_says_how_a_holding_that_pre_dates_tracking_is_treated():
    readme = (Path(ta.__file__).parent / "README.md").read_text(encoding="utf-8")
    start = readme.index("### Trading 212\n")
    section = " ".join(readme[start:readme.index("\n### ", start + 5)].split())
    for phrase in ("Для уже купленных бумаг правила выхода считаются с сегодняшнего дня.",   # the first message
                   "от цены на день первой синхронизации",                              # the stop's floor
                   "от средней цены покупки",                                           # the result stays real
                   "Слежу: стоп и срок",                                                # R2: only what is watched
                   "не расходует"):                                                     # R3: --sync, --no-telegram
        assert phrase in section, phrase
    assert "может прийти сразу после первой синхронизации" not in section   # no burst of alerts any more
