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


@pytest.fixture(autouse=True)
def _a_fresh_process(monkeypatch):
    """What the module remembers about itself is per process: that it has found no key (_was_keyless)
    and has said so (_no_key_logged). Every test starts as a new process."""
    monkeypatch.setattr(ta, "_was_keyless", False, raising=False)
    monkeypatch.setattr(ta, "_no_key_logged", False)


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
# a write to the API, or a way round the plain `.get(` the other tests watch
_WRITE_CALLS = (".post(", ".put(", ".patch(", ".delete(", "requests.request(", ".request(", ".send(",
                "getattr(", "__import__(")
_READ_PATHS = {url.removeprefix("https://live.trading212.com") for url in READ_URLS}
# anything else that can talk to a server: the two modules may import none of it
_NETWORK_LIBRARIES = {"urllib", "urllib3", "http", "httpx", "httplib2", "aiohttp", "socket", "ssl", "asyncio",
                      "curl_cffi", "pycurl", "websocket", "websockets", "tornado", "ftplib", "smtplib",
                      "xmlrpc", "subprocess", "importlib"}


def _imported(source: str) -> set[str]:
    """The top-level names of everything a module imports."""
    roots = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _violations(source: str) -> list[str]:
    """Everything in a module's source that could write to Trading 212 or reach an endpoint off
    the whitelist: a write call (or `.send(`, `getattr(`, `__import__(`, which could make one
    unseen), an import of a network library other than requests, an orders path, a URL or an API
    path that is not one of the three reads -- in the code, in a comment or in a docstring alike."""
    found = [f"call {call}" for call in _WRITE_CALLS if call in source]
    found += [f"import {name}" for name in sorted(_imported(source) & _NETWORK_LIBRARIES)]
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
    '# then call https://live.trading212.com/api/v0/equity/orders',
    'session.send(prepared)', 'getattr(session, "post")(URL)', '__import__("urllib.request")',
    'import urllib.request', 'from http.client import HTTPSConnection', 'import httpx',
    'import socket', 'import subprocess', 'from importlib import import_module'])
def test_the_inspection_catches_a_write_or_a_url_off_the_whitelist(bad):
    assert _violations(f"import requests\n{bad}\n") != []
    assert _violations("import requests\nURL = 'https://live.trading212.com/api/v0/equity/positions'\n"
                       "data = requests.get(URL, headers=h, timeout=20).json()\n") == []


_TRADING_212_ONLY = ("trading212.com", "_auth_headers", "INSTRUMENTS_URL", "POSITIONS_URL", "SUMMARY_URL",
                     "READ_URLS", "TRADING212_API_KEY", "TRADING212_API_SECRET")


def test_no_other_module_of_the_project_has_the_key_or_a_trading_212_url():
    """Trading 212's host, its URL constants, the helper that builds the auth header and the names
    of the key's variables are referenced in the two inspected modules and nowhere else in the
    project's code (the tests aside): no other module can reach the API or hold the key, so no
    other module needs the inspection."""
    root = Path(ta.__file__).parent
    outside = {"tests", ".venv", ".worktrees", ".superpowers", ".git", "data", "docs"}
    files = [f for pattern in ("*.py", "*.sh") for f in root.rglob(pattern)
             if not outside & set(f.relative_to(root).parts)]
    assert len(files) > 40 and any(f.parent.name == "cluster" for f in files)     # every package, too
    for token in _TRADING_212_ONLY:
        named = sorted(f.name for f in files if token in f.read_text(encoding="utf-8"))
        assert set(named) <= {"t212_account.py", "trading212.py"}, f"{token} is in {named}"
    assert sorted(f.name for f in files if "trading212.com" in f.read_text(encoding="utf-8")) == \
        ["t212_account.py", "trading212.py"]


@pytest.mark.parametrize("path", _SOURCES, ids=lambda p: p.name)
def test_the_two_modules_reach_the_network_through_requests_alone(path):
    roots = _imported(path.read_text(encoding="utf-8"))
    assert "requests" in roots and not roots & _NETWORK_LIBRARIES


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
def _flat(price=100.0):
    """Forty flat closes, the last one three weeks before NOW: a calm stock at `price`."""
    return [((NOW.date() - dt.timedelta(days=60 - i)).isoformat(), price) for i in range(40)]


GME_DAYS = _flat()


def _holding(t212_ticker="GME_US_EQ", isin="US36467W1099", *, qty=10.0, avg=23.10, price=24.05,
             created="2026-09-28T14:03:11.000+02:00", currency="USD", pnl=8.30, value=100.0):
    """`value`: what the holding is worth in the account's currency (walletImpact.currentValue)."""
    return ta.T212Position(t212_ticker, None, isin, currency, qty, avg, price, created, value, None, pnl, "EUR")


def _agreeing(holdings):
    """The account summary that goes with a list: the holdings' value is what is invested."""
    return dataclasses.replace(SUMMARY, invested_value=sum(h.value_eur or 0.0 for h in holdings))


SAP = dict(t212_ticker="SAPd_EQ", isin="DE0007164600", avg=120.0, price=125.0, currency="EUR")


class _Run:
    """One sync with a stub fetch: the messages it sent and the histories it asked for.

    Yahoo is a stub too: by default a calm history at the holding's own Trading 212 price (so the
    symbol is taken for the same instrument); a test sets `yahoo` (symbol -> closes) for its own."""

    def __init__(self, conn):
        self.conn, self.sent, self.histories, self.yahoo = conn, [], [], None

    def __call__(self, *holdings, now=NOW, summary=None, fetch=None, notify=None, silent=False):
        """`summary`: the account summary of the same sync; by default one that agrees with the
        list (so what is missing from the list was sold)."""
        prices = {}
        for h in holdings:
            key = ta.position_key(h.t212_ticker, h.isin, self.conn)
            if key is not None and key[1] is None:
                prices[key[0]] = h.current_price

        def closes(ticker, source=None):
            self.histories.append((ticker, source))
            if self.yahoo is not None:
                return self.yahoo(ticker)
            return _flat(prices.get(ticker) or 100.0)
        summary = _agreeing(holdings) if summary is None else summary
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
    assert run.sent == ["⚪ <b>GME!</b>: куплено в Trading 212 — 10 шт. по 23,10 USD, "
                        "слежу: стоп, продажи инсайдеров, новости"]
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
    assert run.sent == ["⚪ <b>SAP!</b>: куплено в Trading 212 — 10 шт. по 120,00 EUR, слежу: стоп и срок"]
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
    assert result.closed == ["GME"] and run.sent == ["⚪ <b>GME!</b>: продано в Trading 212 — слежение закрыто, итог ≈ <b>+$9,50</b> (+4,1%)"]
    assert _row(conn, "GME")[-2:] == (NOW.date().isoformat(), "продано в Trading 212")
    assert [p.ticker for p in positions.open_positions(conn)] == ["DE0007164600"]


def _sold_message(conn, run, *, bought, later=(), now=None, **kw):
    """The message of GME's sale: a sync that lists `bought` (and SAP, which stays), then one without GME."""
    _synced_before(conn)
    run(bought, _holding(**SAP), **kw)
    run.sent.clear()
    run(_holding(**SAP), *later, **({"now": now} if now else {}))
    [text] = run.sent
    return text


def test_a_sold_message_gives_the_result_on_the_last_price_known_money_first_with_a_quantity(conn, run):
    text = _sold_message(conn, run, bought=_holding())
    assert text == "⚪ <b>GME!</b>: продано в Trading 212 — слежение закрыто, итог ≈ <b>+$9,50</b> (+4,1%)"


def test_a_sold_message_for_a_loss_in_euros_and_the_dot_stays_white(conn, run):
    text = _sold_message(conn, run, bought=_holding(avg=120.0, price=108.0, currency="EUR"))
    assert text == "⚪ <b>GME!</b>: продано в Trading 212 — слежение закрыто, итог ≈ <b>−€120,00</b> (−10,0%)"
    assert text.startswith("⚪")


def test_a_sold_message_for_a_holding_without_a_quantity_gives_the_percent_alone(conn, run):
    text = _sold_message(conn, run, bought=_holding(qty=None))
    assert text == "⚪ <b>GME!</b>: продано в Trading 212 — слежение закрыто, итог ≈ <b>+4,1%</b>"


def test_a_sold_message_has_no_result_without_a_last_price(conn, run):
    text = _sold_message(conn, run, bought=_holding(price=None))
    assert text == "⚪ <b>GME!</b>: продано в Trading 212 — слежение закрыто"


def test_a_sold_message_has_no_result_when_the_last_price_is_stale(conn, run):
    """The sync had not got through for days: a price that old is no price (positions.t212_price)."""
    text = _sold_message(conn, run, bought=_holding(), now=NOW + dt.timedelta(days=5))
    assert text == "⚪ <b>GME!</b>: продано в Trading 212 — слежение закрыто"


def test_a_sold_message_takes_the_quantity_the_account_last_had(conn, run):
    _synced_before(conn)
    run(_holding(), _holding(**SAP))
    run(_holding(qty=4.0, price=25.0), _holding(**SAP), now=NOW + dt.timedelta(hours=1))   # trimmed, repriced
    run.sent.clear()
    run(_holding(**SAP), now=NOW + dt.timedelta(hours=2))
    assert run.sent == ["⚪ <b>GME!</b>: продано в Trading 212 — слежение закрыто, итог ≈ <b>+$7,60</b> (+8,2%)"]


def test_the_new_message_names_what_it_knows_of_the_lot_and_what_it_watches():
    h = ta.T212Position("GME_US_EQ", None, None, "USD", 10.0, 23.10, 24.05, None, None, None, None, None)
    assert ta._new_text("GME", h, "стоп, срок и новости") == (
        "⚪ <b>GME!</b>: куплено в Trading 212 — 10 шт. по 23,10 USD, слежу: стоп, срок и новости")
    no_currency = dataclasses.replace(h, currency=None)
    assert ta._new_text("GME", no_currency, "стоп") == "⚪ <b>GME!</b>: куплено в Trading 212 — 10 шт. по 23,10, слежу: стоп"
    only_quantity = dataclasses.replace(h, avg_price=None, current_price=None)
    assert ta._new_text("GME", only_quantity, "стоп") == "⚪ <b>GME!</b>: куплено в Trading 212 — 10 шт., слежу: стоп"
    only_price = dataclasses.replace(h, quantity=None)
    assert ta._new_text("GME", only_price, "стоп") == "⚪ <b>GME!</b>: куплено в Trading 212 — по 23,10 USD, слежу: стоп"
    nothing = dataclasses.replace(h, quantity=None, avg_price=None, current_price=None)
    assert ta._new_text("GME", nothing, "стоп") == "⚪ <b>GME!</b>: куплено в Trading 212 — слежу: стоп"


def test_the_notices_escape_the_name_and_the_currency_and_use_only_bold():
    h = ta.T212Position("X_US_EQ", None, None, "U<S>D", 1.0, 2.0, 2.0, None, None, None, None, None)
    new = ta._new_text("A&B<i>", h, "стоп")
    assert new == "⚪ <b>A&amp;B&lt;i&gt;!</b>: куплено в Trading 212 — 1 шт. по 2,00 U&lt;S&gt;D, слежу: стоп"
    sold = ta._sold_text("A&B<i>")
    assert sold == "⚪ <b>A&amp;B&lt;i&gt;!</b>: продано в Trading 212 — слежение закрыто"


def test_the_first_sync_message_and_the_warnings_keep_their_text():
    assert ta._first_text(["GME", "SAP"], None, "2026-10-01") == "📥 Слежу за вашими позициями в Trading 212 (2): GME, SAP"
    assert ta._first_text(["GME"], "2026-10-01", "2026-10-01") == (
        "📥 Слежу за вашими позициями в Trading 212 (1): GME\n"
        "Для уже купленных бумаг правила выхода считаются с сегодняшнего дня.")
    assert ta.LIST_GAP_WARNING == ("⚠️ Trading 212: список позиций не сходится со счётом уже сутки — "
                                   "продажи не отмечаю.")
    assert ta.KEY_REMOVED.startswith("Ключ Trading 212 убран — слежение за счётом остановлено.")


@pytest.mark.parametrize("failure", [ta.T212Error("HTTP 502", "status"), ValueError("unexpected positions payload: dict"),
                                     ta.T212Error("ReadTimeout", "network")])
def test_a_failed_fetch_changes_nothing_and_sends_nothing(conn, run, failure):
    _synced_before(conn)
    run(_holding(), _holding(**SAP))
    run.sent.clear()
    before = [conn.execute(f"SELECT * FROM {t}").fetchall() for t in ("positions", "t212_prices", "t212_equity")]

    def fail():
        raise failure
    result = run(fetch=fail, now=NOW + dt.timedelta(hours=1))      # (a day of this is another matter: I3)
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
                closes_fn=lambda t, s=None: _flat(24.0))
        return _flat(24.0)
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
                now=NOW, closes_fn=lambda t, s=None: _flat(24.0))
    other.rollback()
    assert mine.execute("SELECT COUNT(*) FROM positions").fetchone() == (0,)
    assert ta.sync(mine, fetch=lambda: ([_holding()], SUMMARY), notify=lambda t: True, now=NOW,
                   closes_fn=lambda t, s=None: _flat(24.0)).opened == ["GME"]   # and the next one goes through
    mine.close()
    other.close()


def test_the_sync_reads_through_the_client_and_only_gets(conn, keyed):
    session = _account()
    result = ta.sync(conn, fetch=lambda: ta.fetch_account(session), notify=lambda t: True, now=NOW,
                     closes_fn=lambda t, s=None: _flat(24.0))
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
    run(_holding("A&Bd_EQ", "DE000A1EWWW0", qty=0.52347, avg=1234.5, price=1250.0, currency="EUR"))
    assert run.sent == ["⚪ <b>A&amp;B!</b>: куплено в Trading 212 — 0,5235 шт. по 1 234,50 EUR, "
                        "слежу: стоп и срок"]
    run()
    assert run.sent[-1] == ("⚪ <b>A&amp;B!</b>: продано в Trading 212 — слежение закрыто, "
                            "итог ≈ <b>+€8,11</b> (+1,3%)")


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
    assert result.opened == ["GME"] and len(run.sent) == 1 and run.sent[0].startswith("⚪ <b>GME!</b>: куплено")
    rows = conn.execute("SELECT closed_at FROM positions WHERE ticker = 'GME' ORDER BY id").fetchall()
    assert rows == [(NOW.date().isoformat(),), (None,)]


# ---------------------------------------------- I1: a sale is believed only when the list adds up
def _invested(amount):
    return dataclasses.replace(SUMMARY, invested_value=amount)


def _tables(conn):
    return [conn.execute(f"SELECT * FROM {t}").fetchall() for t in ("positions", "t212_prices", "t212_equity")]


def test_an_empty_list_while_money_is_invested_is_a_bad_answer(conn, run, capsys):
    _synced_before(conn)
    run(_holding(), _holding(**SAP))
    run.sent.clear()
    before, synced = _tables(conn), ta.last_sync(conn)
    result = run(summary=_invested(200.0), now=NOW + dt.timedelta(hours=1))
    assert result.error == "пустой список позиций при вложенных средствах"
    assert result.closed == result.opened == result.updated == [] and run.sent == []
    assert _tables(conn) == before and ta.last_sync(conn) == synced     # nothing changed: not a sync that got through
    assert "пустой список" in capsys.readouterr().err


def test_a_genuinely_empty_account_closes_what_was_held(conn, run):
    _synced_before(conn)
    run(_holding(), _holding(**SAP))
    run.sent.clear()
    result = run(summary=_invested(0.0))                            # nothing invested, nothing listed
    assert sorted(result.closed) == ["DE0007164600", "GME"] and result.error is None
    assert sorted(run.sent) == ["⚪ <b>GME!</b>: продано в Trading 212 — слежение закрыто, итог ≈ <b>+$9,50</b> (+4,1%)",
                                "⚪ <b>SAP!</b>: продано в Trading 212 — слежение закрыто, итог ≈ <b>+€50,00</b> (+4,2%)"]


@pytest.mark.parametrize("invested, closes", [(0.0, True), (0.004, True), (5.0, True), (5.01, False), (200.0, False)])
def test_a_float_residue_after_selling_everything_does_not_block_the_close(conn, run, invested, closes):
    """F3: the empty-list guard refuses only when more than LIST_TOLERANCE_MONEY is invested. A residue
    of a cent or two in the summary after the last sale is an empty account, not a bad answer: it must
    not keep the sold positions open for ever."""
    _synced_before(conn)
    run(_holding(), _holding(**SAP))
    run.sent.clear()
    result = run(summary=_invested(invested), now=NOW + dt.timedelta(hours=1))
    if closes:
        assert sorted(result.closed) == ["DE0007164600", "GME"] and result.error is None
        assert positions.open_positions(conn) == [] and len(run.sent) == 2
    else:
        assert result.error == ta.EMPTY_LIST and result.closed == [] and run.sent == []
        assert len(positions.open_positions(conn)) == 2


def test_a_partial_list_closes_nothing_but_still_opens_and_updates(conn, run, capsys):
    """The summary says €300 is invested and the list adds up to €200: GME is not on it, but that
    is the list's fault. What it does show is applied."""
    _synced_before(conn)
    run(_holding(), _holding(**SAP))
    run.sent.clear()
    capsys.readouterr()
    nvda = _holding("NVDA_US_EQ", "US67066G1040", avg=100.0, price=110.0)
    result = run(_holding(**dict(SAP, qty=7.0)), nvda, summary=_invested(300.0))
    assert result.closed == [] and result.held == ["GME"]
    assert result.note == "список позиций не сходится со сводкой счёта"
    assert result.updated == ["DE0007164600"] and result.opened == ["NVDA"]
    assert positions.find_open(conn, "GME") is not None and positions.find_open(conn, "DE0007164600").quantity == 7.0
    assert [text.split(": ")[1].split(" — ")[0] for text in run.sent] == ["куплено в Trading 212"]   # NVDA, no sale
    err = capsys.readouterr().err
    assert err.count("disagree") == 1 and "33%" in err              # one line, and no amount in it
    assert "300" not in err and "200" not in err


@pytest.mark.parametrize("invested, listed, trusted", [
    (1000.0, 985.0, True), (1000.0, 979.0, False),                  # within / beyond 2 %
    (1000.0, 1015.0, True), (1000.0, 1021.0, False),
    (100.0, 96.0, True), (100.0, 94.0, False),                      # a small account: within / beyond €5
    (0.0, 4.0, True), (0.0, 6.0, False)])
def test_the_list_is_trusted_within_two_percent_or_five_euros_of_the_summary(conn, run, invested, listed, trusted):
    _synced_before(conn)
    run(_holding(), _holding(**SAP))
    result = run(_holding(**dict(SAP, value=listed)), summary=_invested(invested))
    assert (result.closed == ["GME"]) is trusted and (result.held == ["GME"]) is not trusted


def _minutes(n):
    return NOW + dt.timedelta(minutes=n)


def test_without_a_summary_value_a_sale_needs_two_syncs_in_a_row(conn, run):
    _synced_before(conn)
    run(_holding(), _holding(**SAP))
    run.sent.clear()
    first = run(_holding(**SAP), summary=_invested(None), now=_minutes(15))
    assert first.closed == [] and first.held == ["GME"] and run.sent == []
    assert first.note == ta.SALE_UNCONFIRMED == "список нельзя сверить со сводкой: продажу подтвердит следующая синхронизация"
    second = run(_holding(**SAP), summary=_invested(None), now=_minutes(30))
    assert second.closed == ["GME"] and second.held == []
    assert run.sent == ["⚪ <b>GME!</b>: продано в Trading 212 — слежение закрыто, итог ≈ <b>+$9,50</b> (+4,1%)"]


def test_a_holding_that_is_back_in_between_starts_the_count_again(conn, run):
    _synced_before(conn)
    run(_holding(), _holding(**SAP))
    unknown = _invested(None)
    assert run(_holding(**SAP), summary=unknown, now=_minutes(15)).held == ["GME"]
    assert run(_holding(), _holding(**SAP), summary=unknown, now=_minutes(30)).held == []   # it is there again
    assert run(_holding(**SAP), summary=unknown, now=_minutes(45)).closed == []             # the first miss, again
    assert run(_holding(**SAP), summary=unknown, now=_minutes(60)).closed == ["GME"]


def test_a_failed_sync_between_two_misses_is_not_a_sync(conn, run):
    _synced_before(conn)
    run(_holding(), _holding(**SAP))
    unknown = _invested(None)
    run(_holding(**SAP), summary=unknown, now=_minutes(15))

    def down():
        raise ta.T212Error("HTTP 502", "status")
    assert run(fetch=down, now=_minutes(30)).error == "HTTP 502"
    assert run(_holding(**SAP), summary=unknown, now=_minutes(45)).closed == ["GME"]    # two successful ones in a row


def test_a_holding_without_its_value_cannot_vouch_for_the_list(conn, run):
    """The summary is there, but one holding has no value: the sum can't be checked, so a sale
    waits for the next sync like it does with no summary."""
    _synced_before(conn)
    run(_holding(), _holding(**SAP))
    partial = (_holding(**dict(SAP, value=None)),)
    assert run(*partial, summary=_invested(100.0), now=_minutes(15)).closed == []
    assert run(*partial, summary=_invested(100.0), now=_minutes(30)).closed == ["GME"]


def test_a_list_that_disagrees_does_not_count_as_a_miss_nor_clear_one(conn, run):
    _synced_before(conn)
    run(_holding(), _holding(**SAP))
    assert run(_holding(**SAP), summary=_invested(None), now=_minutes(15)).held == ["GME"]   # the first miss
    assert run(_holding(**SAP), summary=_invested(500.0), now=_minutes(30)).closed == []     # a list that does not add up
    assert run(_holding(**SAP), summary=_invested(None), now=_minutes(45)).closed == ["GME"]  # the second miss


def test_a_list_that_adds_up_closes_at_once_and_forgets_the_pending_miss(conn, run):
    _synced_before(conn)
    run(_holding(), _holding(**SAP))
    run(_holding(**SAP), summary=_invested(None), now=_minutes(15))
    assert run(_holding(**SAP), now=_minutes(30)).closed == ["GME"]             # verified: no waiting
    assert db.get_cached_json(conn, ta.MISSING_KEY) == {}


# ---------------------------------------------- F6: the second miss counts only ten minutes after the first
def _two_holdings_and_one_missing(conn, run):
    _synced_before(conn)
    run(_holding(), _holding(**SAP))
    run.sent.clear()


def test_the_second_miss_needs_to_be_ten_minutes_after_the_first(conn, run):
    """The daily run and the bot loop are separate processes: two syncs seconds apart can meet one bad
    answer. 2 s apart is one miss and no close; 15 minutes apart is two."""
    _two_holdings_and_one_missing(conn, run)
    unknown = _invested(None)
    first = run(_holding(**SAP), summary=unknown, now=_minutes(60))
    soon = run(_holding(**SAP), summary=unknown, now=_minutes(60) + dt.timedelta(seconds=2))
    assert first.closed == soon.closed == [] and soon.held == ["GME"] and soon.note == ta.SALE_UNCONFIRMED
    assert positions.find_open(conn, "GME") is not None and run.sent == []
    later = run(_holding(**SAP), summary=unknown, now=_minutes(75))
    assert later.closed == ["GME"] and later.held == []
    assert run.sent == ["⚪ <b>GME!</b>: продано в Trading 212 — слежение закрыто, итог ≈ <b>+$9,50</b> (+4,1%)"]


@pytest.mark.parametrize("seconds, closes", [(2, False), (599, False), (600, True), (901, True)])
def test_the_close_needs_the_second_miss_at_least_ten_minutes_later(conn, run, seconds, closes):
    _two_holdings_and_one_missing(conn, run)
    unknown = _invested(None)
    run(_holding(**SAP), summary=unknown, now=_minutes(60))
    second = run(_holding(**SAP), summary=unknown, now=_minutes(60) + dt.timedelta(seconds=seconds))
    assert (second.closed == ["GME"]) is closes


def test_the_two_sync_rule_keeps_the_time_of_the_first_miss(conn, run):
    """A second miss too soon does not start the count again: the third sync, 11 minutes after the first
    miss and 5 after the second, closes."""
    _two_holdings_and_one_missing(conn, run)
    unknown = _invested(None)
    assert run(_holding(**SAP), summary=unknown, now=_minutes(60)).closed == []
    assert run(_holding(**SAP), summary=unknown, now=_minutes(66)).closed == []
    assert run(_holding(**SAP), summary=unknown, now=_minutes(71)).closed == ["GME"]


def test_a_miss_kept_by_an_older_version_as_a_bare_id_is_a_first_miss_now(conn, run):
    """MISSING_KEY once held a list of ids, with no time: it is never read as an old miss."""
    _two_holdings_and_one_missing(conn, run)
    gme = positions.find_open(conn, "GME")
    db.save_cached_json(conn, ta.MISSING_KEY, [gme.id])
    unknown = _invested(None)
    assert run(_holding(**SAP), summary=unknown, now=_minutes(60)).closed == []
    assert run(_holding(**SAP), summary=unknown, now=_minutes(75)).closed == ["GME"]


def test_a_first_miss_dated_ahead_of_the_clock_is_read_as_a_miss_of_now(conn, run):
    """A clock set back must not hold a sale back until the old time comes round again."""
    _two_holdings_and_one_missing(conn, run)
    gme = positions.find_open(conn, "GME")
    db.save_cached_json(conn, ta.MISSING_KEY, {str(gme.id): (NOW + dt.timedelta(days=3)).timestamp()})
    unknown = _invested(None)
    assert run(_holding(**SAP), summary=unknown, now=_minutes(60)).closed == []
    assert run(_holding(**SAP), summary=unknown, now=_minutes(75)).closed == ["GME"]


# ---------------------------------------------- F5: another wallet currency than the summary's: cannot check
@pytest.mark.parametrize("wallet, account, agrees", [
    ("EUR", "EUR", False), ("eur", "EUR", False), (None, "EUR", False), ("EUR", None, False),   # comparable
    ("USD", "EUR", None), ("GBP", "eur", None)])                    # not the same currency: cannot be told
def test_a_list_is_compared_with_the_summary_only_in_one_currency(wallet, account, agrees):
    holdings = [dataclasses.replace(_holding(value=100.0), account_currency=wallet)]
    summary = dataclasses.replace(_invested(5000.0), currency=account)
    assert ta._list_agrees(holdings, summary) is agrees
    assert (ta._list_gap(holdings, summary) is None) is (agrees is None)


def test_a_wallet_currency_other_than_the_summarys_falls_to_the_two_sync_rule_and_is_no_disagreement(conn, run):
    _two_holdings_and_one_missing(conn, run)
    usd = dataclasses.replace(_holding(**SAP), account_currency="USD")
    first = run(usd, summary=_invested(5000.0), now=_minutes(60))   # the numbers are far apart: other currencies
    assert first.agrees is None and first.closed == [] and first.held == ["GME"]
    assert first.note == ta.SALE_UNCONFIRMED                        # not «не сходится со сводкой»
    second = run(usd, summary=_invested(5000.0), now=_minutes(75))
    assert second.closed == ["GME"] and second.held == []


def test_check_cannot_compare_a_list_in_another_currency_with_the_summary(keyed, monkeypatch, capsys):
    held = _payload("SAPd_EQ", "DE0007164600")
    held["walletImpact"]["currency"] = "USD"                        # the summary is in EUR
    lines = _check_line(monkeypatch, capsys, [held], dict(FULL_SUMMARY, investments={"currentValue": 400.0}))
    assert lines[1].endswith("список и сводку не сверить")


# ---------------------------------------------- I1: a holding that returns is restored
def test_a_holding_that_vanishes_and_returns_keeps_its_clock_and_its_stop_base(conn, run):
    """It pre-dates tracking: opened at the first sync, its stop measured from 50. Closed by a sync
    that did not list it, it comes back -- as itself, not as a new position bought two years ago."""
    run(_holding(created=OLD, avg=100.0, price=50.0))               # the first sync: legacy
    first = positions.find_open(conn, "GME")
    conn.execute("UPDATE positions SET close_alerted_at = '2026-10-01' WHERE id = ?", (first.id,))
    conn.commit()
    assert run(now=NOW + dt.timedelta(hours=1)).closed == ["GME"]
    run.sent.clear()
    result = run(_holding(created=OLD, avg=100.0, price=48.0, qty=12.0), now=NOW + dt.timedelta(days=3))
    assert result.opened == [] and result.updated == ["GME"] and run.sent == []      # no «📥»
    back = positions.find_open(conn, "GME")
    assert (back.id, back.opened_at, back.stop_base, back.stop_pct, back.t212_created) == \
        (first.id, NOW.date().isoformat(), 50.0, first.stop_pct, "2024-05-01")
    assert (back.closed_at, back.close_reason, back.close_alerted_at, back.quantity) == \
        (None, None, "2026-10-01", 12.0)
    assert conn.execute("SELECT COUNT(*) FROM positions WHERE ticker = 'GME'").fetchone() == (1,)


def test_a_restored_holding_is_named_in_the_first_message_as_its_own_row_has_it(conn, run):
    """Stored under its ISIN while Yahoo did not know the symbol, sold, and back when Yahoo does:
    it is the stored row again -- still keyed by its ISIN -- and is listed once, by its name."""
    run.yahoo = lambda ticker: _closes(500.0)                       # another instrument's price
    run(_holding(), silent=True)                                    # keyed US36467W1099; no message yet
    run(silent=True, now=NOW + dt.timedelta(hours=1))               # gone
    run.yahoo = None
    result = run(_holding(), now=NOW + dt.timedelta(hours=2))       # back, and the first sync that may notify
    assert result.updated == ["US36467W1099"] and result.opened == []
    assert run.sent == ["📥 Слежу за вашими позициями в Trading 212 (1): GME\n" + LEGACY_LINE]
    assert [p.ticker for p in positions.open_positions(conn)] == ["US36467W1099"]


def test_a_restored_holding_brings_no_burst_of_alerts(conn, run):
    run(_holding(created=OLD, avg=100.0, price=50.0))
    run(now=NOW + dt.timedelta(hours=1))
    run(_holding(created=OLD, avg=100.0, price=50.0), now=NOW + dt.timedelta(days=1))
    day = NOW.date() + dt.timedelta(days=1)
    highs = [((NOW.date() - dt.timedelta(days=400 - i)).isoformat(), 200.0) for i in range(300)]
    assert _exits(conn, day, 50.0, highs) == []                     # no stop, no year, no dead money


def test_a_holding_bought_back_on_another_day_is_a_new_position(conn, run):
    _synced_before(conn)
    run(_holding())                                                 # bought 28.09
    run()
    run.sent.clear()
    result = run(_holding(created="2026-10-01T09:00:00Z"))
    assert result.opened == ["GME"] and run.sent[0].startswith("⚪ <b>GME!</b>: куплено")
    assert conn.execute("SELECT COUNT(*) FROM positions WHERE ticker = 'GME'").fetchone() == (2,)


def test_a_returning_holding_with_no_purchase_date_is_opened_anew(conn, run):
    """Without the date there is nothing to tell the same holding from a new purchase: an old
    row -- with its old clock and an alert already used -- is not woken up for it."""
    _synced_before(conn)
    run(_holding(created=None))
    run()
    assert run(_holding(created=None)).opened == ["GME"]
    assert conn.execute("SELECT COUNT(*) FROM positions WHERE ticker = 'GME'").fetchone() == (2,)


def test_a_bought_position_recorded_meanwhile_is_taken_over_rather_than_the_old_row_restored(conn, run):
    _synced_before(conn)
    run(_holding())
    run()
    manual = positions.open_position(conn, "GME", 20.0, today=dt.date(2026, 10, 1), closes_fn=lambda t, s=None: [])
    result = run(_holding())
    assert result.updated == ["GME"] and positions.find_open(conn, "GME").id == manual.id
    assert len(positions.open_positions(conn)) == 1


# ---------------------------------------------- I2: the market symbol of a US instrument
FB = dict(t212_ticker="FB_US_EQ", isin="US30303M1027", avg=300.0, price=500.0)     # Meta, under its old code


def _instrument(conn, ticker, short_name, isin="US30303M1027"):
    """A row of the cached instrument list (trading212.availability keeps it)."""
    conn.execute("INSERT OR REPLACE INTO t212_instruments (ticker, isin, type, short_name, currency) "
                 "VALUES (?,?,?,?,?)", (ticker, isin, "STOCK", short_name, "USD"))
    conn.commit()


def _closes(*values, ends_days_ago=1):
    """Yahoo's closes on consecutive days, the last one `ends_days_ago` days before NOW."""
    start = NOW.date() - dt.timedelta(days=ends_days_ago + len(values) - 1)
    return [((start + dt.timedelta(days=i)).isoformat(), v) for i, v in enumerate(values)]


@pytest.mark.parametrize("short_name, key", [("META", ("META", None)), (" meta ", ("META", None)),
                                             ("", ("FB", None)), (None, ("FB", None))])
def test_a_us_instruments_market_symbol_comes_from_the_instrument_list(conn, short_name, key):
    """Trading 212's _US_EQ codes are old for one instrument in five (FB_US_EQ is Meta)."""
    if short_name is not None:
        _instrument(conn, "FB_US_EQ", short_name)
    assert ta.position_key("FB_US_EQ", "US30303M1027", conn) == key
    assert ta.position_key("FB_US_EQ", "US30303M1027") == ("FB", None)      # no database: the code itself


def test_the_instrument_list_keeps_a_class_share_and_leaves_other_instruments_alone(conn):
    _instrument(conn, "BRK_B_US_EQ", "BRK.B", isin="US0846707026")
    _instrument(conn, "SAPd_EQ", "SAP", isin="DE0007164600")
    assert ta.position_key("BRK_B_US_EQ", "US0846707026", conn) == ("BRK.B", None)
    assert ta.position_key("SAPd_EQ", "DE0007164600", conn) == ("DE0007164600", "T212")


def test_a_holding_under_an_old_code_is_opened_and_priced_under_its_market_symbol(conn, run):
    _synced_before(conn)
    _instrument(conn, "FB_US_EQ", "META")
    result = run(_holding(**FB))
    pos = positions.find_open(conn, "META")
    assert result.opened == ["META"] and (pos.t212_ticker, pos.source) == ("FB_US_EQ", None)
    assert run.histories == [("META", None)]                        # Yahoo is asked for META, not FB
    assert run.sent[0].startswith("⚪ <b>META!</b>: куплено в Trading 212 — ")
    assert conn.execute("SELECT ticker FROM t212_prices").fetchall() == [("META",)]


def test_without_an_instrument_row_the_code_is_the_symbol(conn, run):
    _synced_before(conn)
    assert run(_holding(**FB)).opened == ["FB"] and run.histories == [("FB", None)]


@pytest.mark.parametrize("yahoo, by_isin", [
    ([], False),                                                    # no history at all: an outage is not a verdict
    (_closes(24.0, ends_days_ago=0), False),                        # only today's bar: no completed close to compare
    (_closes(480.0, 500.0), True),                                  # a series that exists, and is another instrument's
    (_closes(19.0), True), (_closes(29.0), True),                   # more than 20 % away
    (_closes(20.5), False), (_closes(28.0), False),                 # within 20 %
    (_closes(500.0, 24.0), False)])                                 # the LAST completed close counts
def test_a_us_holding_is_keyed_by_its_isin_only_when_yahoos_series_is_another_instruments(conn, run, yahoo, by_isin):
    """K1: Yahoo returning nothing (it is down, or does not know the symbol) must not key the holding
    by its ISIN for good. Only a series that exists and is far from Trading 212's price does."""
    _synced_before(conn)
    run.yahoo = lambda ticker: yahoo
    result = run(_holding())                                        # GME, 24,05 at Trading 212
    [pos] = positions.open_positions(conn)
    if by_isin:
        assert result.opened == ["US36467W1099"]
        assert (pos.ticker, pos.source, pos.t212_ticker) == ("US36467W1099", "T212", "GME_US_EQ")
        assert conn.execute("SELECT ticker, price FROM t212_prices").fetchall() == [("US36467W1099", 24.05)]
        assert run.sent[-1].startswith("⚪ <b>GME!</b>: куплено в Trading 212 — ")    # still named by its code
        assert run.sent[-1].endswith("слежу: стоп и срок")          # and watched as what it now is
    else:
        assert result.opened == ["GME"] and (pos.ticker, pos.source) == ("GME", None)


def test_a_us_holding_with_no_price_from_trading_212_keeps_its_symbol_when_yahoo_knows_it(conn, run):
    _synced_before(conn)
    run.yahoo = lambda ticker: _closes(500.0)                       # nothing to compare it with
    assert run(_holding(price=None)).opened == ["GME"]


def test_a_us_holding_with_no_isin_to_fall_back_on_keeps_its_symbol(conn, run):
    _synced_before(conn)
    run.yahoo = lambda ticker: _closes(500.0)                       # another instrument's, but no ISIN to key it by
    assert run(_holding("GME_US_EQ", None)).opened == ["GME"]


@pytest.fixture
def yahoo_has_nothing(monkeypatch):
    """Yahoo is down, or does not know the symbol: no history and no last close for anything."""
    import paper
    monkeypatch.setattr(paper, "_closes", lambda symbol, days: [])
    monkeypatch.setattr(positions, "_yahoo_close", lambda symbol: None)


def test_a_us_holding_yahoo_has_nothing_for_keeps_its_symbol_and_is_priced_from_the_day_prices(
        conn, run, yahoo_has_nothing):
    """K1: keyed GME all the same (its insiders and its news are GME's), and its stop still works --
    on the prices the sync stores."""
    _synced_before(conn)
    _journal(conn, "GME", ["Ryan Cohen"])
    run.yahoo = lambda ticker: []
    run(_holding(avg=100.0, price=100.0))
    pos = positions.find_open(conn, "GME")
    assert (pos.source, pos.t212_ticker, pos.insiders) == (None, "GME_US_EQ", ["Ryan Cohen"])
    assert run.sent[-1].endswith("слежу: стоп, продажи инсайдеров, новости")
    run(_holding(avg=100.0, price=130.0), now=NOW + dt.timedelta(days=1))
    run(_holding(avg=100.0, price=110.0), now=NOW + dt.timedelta(days=2))
    day = NOW.date() + dt.timedelta(days=2)
    [alert] = positions.check_exits(conn, today=day, news_fn=lambda t, s=None: [])
    assert alert.trigger == "trailing_stop" and alert.last_price == 110.0
    assert alert.detail == "−15% от максимума 130.00"               # the peak: yesterday's stored price


def test_a_us_holding_with_no_yahoo_history_sizes_its_stop_from_the_day_prices_it_has(conn, run):
    _synced_before(conn)
    for day, price in _flat(24.0):                                  # stored while it was held before
        conn.execute("INSERT INTO t212_prices (ticker, date, price) VALUES ('GME', ?, ?)", (day, price))
    conn.commit()
    run.yahoo = lambda ticker: []
    run(_holding())
    assert positions.find_open(conn, "GME").stop_pct == 0.10


def test_a_tracked_position_is_found_by_its_stored_trading_212_id_not_by_the_key(conn, run):
    """Tracked as FB before the instrument list had the rename. The list now says META: it is the
    same holding -- no second position, no «куплено», no «продано»."""
    _synced_before(conn)
    run(_holding(**FB))
    _instrument(conn, "FB_US_EQ", "META")
    run.sent.clear()
    result = run(_holding(**dict(FB, qty=11.0, price=510.0)), now=NOW + dt.timedelta(hours=1))
    assert (result.opened, result.closed, result.updated, run.sent) == ([], [], ["FB"], [])
    [pos] = positions.open_positions(conn)
    assert (pos.ticker, pos.t212_ticker, pos.quantity) == ("FB", "FB_US_EQ", 11.0)
    assert conn.execute("SELECT ticker, price FROM t212_prices").fetchall() == [("FB", 510.0)]   # its own ticker


def test_a_holding_keyed_by_its_isin_at_open_stays_one_position_when_yahoo_agrees_later(conn, run):
    _synced_before(conn)
    run.yahoo = lambda ticker: _closes(500.0)                       # another instrument's price: keyed by its ISIN
    run(_holding())
    run.yahoo = None
    result = run(_holding(), now=NOW + dt.timedelta(hours=1))
    assert (result.opened, result.closed) == ([], [])
    assert [p.ticker for p in positions.open_positions(conn)] == ["US36467W1099"]


def test_one_isin_on_two_exchanges_stays_one_position_whichever_is_listed_first(conn, run):
    _synced_before(conn)
    london = _holding("VUSAl_EQ", "IE00B3XXRP09", currency="GBP", price=80.0)
    xetra = _holding("VUSAd_EQ", "IE00B3XXRP09", currency="EUR", price=95.0)
    run(london, xetra)
    result = run(xetra, london, now=NOW + dt.timedelta(hours=1))    # the other way round
    assert (result.opened, result.closed) == ([], [])
    [pos] = positions.open_positions(conn)
    assert pos.t212_ticker == "VUSAl_EQ"
    assert conn.execute("SELECT price FROM t212_prices").fetchall() == [(80.0,)]     # the tracked listing's price


def test_a_position_stored_without_its_trading_212_id_is_matched_by_its_key_and_learns_the_id(conn, run):
    _synced_before(conn)
    run(ta.T212Position(None, None, "DE0007164600", "EUR", 10.0, 120.0, 125.0, None, 100.0, None, 5.0, "EUR"))
    assert positions.find_open(conn, "DE0007164600").t212_ticker is None
    result = run(_holding(**SAP))
    assert (result.opened, result.closed) == ([], [])
    assert positions.find_open(conn, "DE0007164600").t212_ticker == "SAPd_EQ"


def test_a_sold_holding_is_told_from_the_list_by_its_trading_212_id(conn, run):
    _synced_before(conn)
    run(_holding(**FB), _holding(**SAP))
    _instrument(conn, "FB_US_EQ", "META")                           # the derived key changes; the id does not
    result = run(_holding(**FB))
    assert result.closed == ["DE0007164600"] and positions.find_open(conn, "FB") is not None


@pytest.mark.parametrize("t212_ticker, name", [("GME_US_EQ", "GME"), ("BRK_B_US_EQ", "BRK.B"),
                                               ("SAPd_EQ", "SAP"), (None, "US36467W1099")])
def test_a_holding_keyed_by_its_isin_is_named_by_its_trading_212_symbol(t212_ticker, name):
    assert positions.name_of("US36467W1099", "T212", t212_ticker) == name


# ---------------------------------------------- I3: a sync that keeps failing is not silent
def _down(reason="HTTP 502", kind="status"):
    def fetch():
        raise ta.T212Error(reason, kind)
    return fetch


def _bad_payload():
    raise ValueError("unexpected positions payload: dict")


def _empty_but_invested():
    return [], dataclasses.replace(SUMMARY, invested_value=500.0)


def _silent_for_a_day(reason="HTTP 502"):
    return f"⚠️ Trading 212 не отвечает уже сутки ({reason}): позиции не обновляются."


def _hours(n):
    return NOW + dt.timedelta(hours=n)


def test_a_sync_that_has_failed_for_a_day_says_so_once_a_day(conn, run):
    run(_holding())                                                 # a sync got through at NOW (01.10 14:05)
    run.sent.clear()
    assert run(fetch=_down(), now=_hours(23)).error == "HTTP 502" and run.sent == []   # not a day yet
    run(fetch=_down(), now=_hours(25))                              # 02.10 15:05
    assert run.sent == [_silent_for_a_day()]
    run(fetch=_down(), now=_hours(26))                              # the same day: said once
    assert run.sent == [_silent_for_a_day()]
    run(fetch=_down("ReadTimeout", "network"), now=_hours(49))      # 03.10: again, with that day's reason
    assert run.sent == [_silent_for_a_day(), _silent_for_a_day("ReadTimeout")]


def test_there_is_nothing_to_miss_before_a_sync_ever_got_through(conn, run):
    run(fetch=_down(), now=NOW)
    run(fetch=_down(), now=NOW + dt.timedelta(days=30))
    assert run.sent == []


def test_a_sync_that_gets_through_again_ends_the_warning(conn, run):
    run(_holding())
    run(fetch=_down(), now=_hours(25))
    run(_holding(), now=_hours(26))                                 # it is back
    run.sent.clear()
    run(fetch=_down(), now=_hours(27))                              # an hour without one is not a day
    assert run.sent == []


@pytest.mark.parametrize("fetch, reason", [
    (_down("ключ Trading 212 не подходит", "unauthorized"), "ключ Trading 212 не подходит"),
    (_down(ta.NO_RIGHTS, "forbidden"), ta.NO_RIGHTS),
    (_down("ReadTimeout", "network"), "ReadTimeout"),
    (_bad_payload, "ответ не разобран"),
    (_empty_but_invested, "пустой список позиций при вложенных средствах")])
def test_the_warning_gives_the_reason_whatever_kind_of_failure_it_is(conn, run, fetch, reason):
    run(_holding())
    run.sent.clear()
    assert run(fetch=fetch, now=_hours(25)).error == reason
    assert run.sent == [_silent_for_a_day(reason)]


def test_a_silent_sync_neither_warns_nor_uses_up_the_days_warning(conn, run):
    run(_holding())
    run.sent.clear()
    run(fetch=_down(), now=_hours(25), silent=True)                 # --sync: nothing is sent
    assert run.sent == []
    run(fetch=_down(), now=_hours(26))
    assert run.sent == [_silent_for_a_day()]


def test_a_warning_that_did_not_go_out_is_tried_again_at_the_next_sync(conn, run):
    run(_holding())
    run.sent.clear()
    def broken(text):
        raise RuntimeError("telegram is down")
    run(fetch=_down(), now=_hours(25), notify=lambda text: False)   # Telegram did not take it
    run(fetch=_down(), now=_hours(26), notify=broken)
    run(fetch=_down(), now=_hours(27))
    assert run.sent == [_silent_for_a_day()]


# ---------------------------------------------- K2: a key removed on purpose is not an outage
KEY_REMOVED = ("Ключ Trading 212 убран — слежение за счётом остановлено. "
               "Позиции из Trading 212 остаются в /portfolio по последним данным.")
_no_key = _down("ключ Trading 212 не задан", "no_key")


def test_a_removed_key_is_said_once_and_then_the_bot_stays_quiet(conn, run):
    run(_holding())                                                 # tracked, with a key
    run.sent.clear()
    for hours in (2, 3, 25, 26, 49, 24 * 30):
        assert run(fetch=_no_key, now=_hours(hours)).error == ta.NO_KEY
    assert run.sent == [KEY_REMOVED]                                # once -- and no daily «не отвечает»
    assert positions.find_open(conn, "GME") is not None             # the positions stay as they were


def test_with_no_key_and_no_sync_ever_nothing_is_said(conn, run):
    run(fetch=_no_key)
    run(fetch=_no_key, now=_hours(48))
    assert run.sent == []


def test_a_key_that_is_configured_again_clears_the_mark(conn, run):
    """This process found no key itself (a sync of its own said so), so a sync of its own that gets
    through is the key back."""
    run(_holding())
    run(fetch=_no_key, now=_hours(2))
    assert db.get_cached_value(conn, ta.KEY_REMOVED_KEY, 10**9) is not None
    run(_holding(), now=_hours(3))                                  # the key is back, and works
    assert db.get_cached_value(conn, ta.KEY_REMOVED_KEY, 10**9) is None
    run.sent.clear()
    run(fetch=_no_key, now=_hours(5))                               # removed again: said again
    run(fetch=_no_key, now=_hours(6))
    assert run.sent == [KEY_REMOVED]


def test_a_key_that_is_back_but_fails_is_an_outage_again(conn, run):
    """A key is configured, so the account is meant to be tracked: an API error is an API error."""
    run(_holding())
    run(fetch=_no_key, now=_hours(2))
    run.sent.clear()
    run(fetch=_down(ta.BAD_KEY, "unauthorized"), now=_hours(26))    # a day since the last sync that got through
    assert run.sent == [_silent_for_a_day(ta.BAD_KEY)]
    assert db.get_cached_value(conn, ta.KEY_REMOVED_KEY, 10**9) is None
    run(fetch=_no_key, now=_hours(27))                              # and removed once more
    assert run.sent == [_silent_for_a_day(ta.BAD_KEY), KEY_REMOVED]


def test_a_silent_sync_with_no_key_leaves_the_message_for_one_that_may_notify(conn, run):
    run(_holding())
    run.sent.clear()
    run(fetch=_no_key, now=_hours(2), silent=True)                  # --sync: says nothing, marks nothing
    assert run.sent == []
    run(fetch=_no_key, now=_hours(3))
    assert run.sent == [KEY_REMOVED]


def test_the_removed_key_message_is_tried_again_when_it_did_not_go_out(conn, run):
    def broken(text):
        raise RuntimeError("telegram is down")
    run(_holding())
    run.sent.clear()
    run(fetch=_no_key, now=_hours(2), notify=lambda text: False)
    run(fetch=_no_key, now=_hours(3), notify=broken)
    run(fetch=_no_key, now=_hours(4))
    run(fetch=_no_key, now=_hours(5))
    assert run.sent == [KEY_REMOVED]


def test_a_failing_sync_writes_nothing_when_there_is_no_mark_to_clear(tmp_path):
    """During an outage the sync fails every 15 minutes: it must not take the database's write lock
    each time for a mark that is not there (the daily run may be holding it)."""
    path = tmp_path / "shared.db"
    mine, other = db.connect(path), db.connect(path)
    mine.execute("PRAGMA busy_timeout = 50")
    other.execute("BEGIN IMMEDIATE")                                # another writer holds the database
    result = ta.sync(mine, fetch=_down("HTTP 502", "status"), notify=lambda text: True, now=NOW)
    assert result.error == "HTTP 502"                               # no OperationalError: nothing was written
    other.rollback()
    mine.close()
    other.close()


@pytest.mark.parametrize("failure", [("ключ Trading 212 не подходит", "unauthorized"), (ta.NO_RIGHTS, "forbidden"),
                                     ("ReadTimeout", "network"), ("HTTP 502", "status")])
def test_an_api_error_keeps_the_daily_warning(conn, run, failure):
    run(_holding())
    run.sent.clear()
    for hours in (25, 49):
        run(fetch=_down(*failure), now=_hours(hours))
    assert run.sent == [_silent_for_a_day(failure[0])] * 2


def test_the_real_client_without_a_key_after_a_sync_says_the_key_was_removed(conn, run):
    run(_holding())
    run.sent.clear()
    result = ta.sync(conn, notify=lambda text: run.sent.append(text) or True, now=_hours(2))   # no key in the tests
    assert result.error == ta.NO_KEY and run.sent == [KEY_REMOVED]


def test_the_result_says_what_kind_of_failure_it_was(conn, run):
    assert run(fetch=_down(ta.BAD_KEY, "unauthorized")).error_kind == "unauthorized"
    assert run(fetch=_down(ta.NO_RIGHTS, "forbidden")).error_kind == "forbidden"
    assert run(fetch=_down("ReadTimeout", "network")).error_kind == "network"
    assert run(_holding()).error_kind is None


# ---------------------------------------------- F1: a key taken out of .env is said once, in the real two-process setup
class _Process:
    """One process of the real setup, with its own memory: the module remembers, per process, that it has
    found no key (ta._was_keyless). `with process:` runs that process's syncs; a new _Process is a new
    process (the daily run is a fresh one every day, the Telegram agent lives on)."""

    def __init__(self):
        self.memory = {"_was_keyless": False, "_no_key_logged": False}

    def __enter__(self):
        for name, value in self.memory.items():
            setattr(ta, name, value)
        return self

    def __exit__(self, *exc):
        for name in self.memory:
            self.memory[name] = getattr(ta, name)


def _mark(conn):
    return db.get_cached_value(conn, ta.KEY_REMOVED_KEY, 10**9)


def test_a_key_is_not_called_removed_while_a_sync_got_through_in_the_last_hour(conn, run):
    """A sync that got through means some process still holds a key (the Telegram agent keeps the one it was
    started with): nothing is removed, so nothing is said and nothing is marked."""
    run(_holding())
    run.sent.clear()
    run(fetch=_no_key, now=NOW + dt.timedelta(minutes=59))
    assert run.sent == [] and _mark(conn) is None
    run(fetch=_no_key, now=NOW + dt.timedelta(minutes=61))          # an hour without one: it is removed
    assert run.sent == [KEY_REMOVED] and _mark(conn) is not None


@pytest.mark.parametrize("agent_sleeps_at_night", [False, True])
def test_two_processes_say_the_key_was_removed_at_most_once_over_several_days(conn, run, agent_sleeps_at_night):
    """The real setup. The Telegram agent was started with the key in its environment and still has it: it
    syncs every 15 minutes and its syncs get through. The daily run starts after the key was taken out of
    .env and finds none. The agent's syncs used to clear the mark, so the daily run said it every day.
    Asleep at night, the agent leaves a gap before the daily run: then it is said, once, and stays said."""
    agent = _Process()
    with agent:
        run(_holding())
    run.sent.clear()
    for day in range(1, 6):
        midnight = dt.datetime.combine(NOW.date() + dt.timedelta(days=day), dt.time())
        events = [(midnight + dt.timedelta(minutes=15 * q), "agent") for q in range(96)
                  if not (agent_sleeps_at_night and q < 32)]            # asleep until 08:00
        events.append((midnight + dt.timedelta(hours=7, minutes=30), "daily run"))
        for at, who in sorted(events):
            if who == "agent":
                with agent:
                    run(_holding(), now=at)
            else:
                with _Process():                                        # a new process each day, with no key
                    run(fetch=_no_key, now=at)
    assert run.sent == ([KEY_REMOVED] if agent_sleeps_at_night else [])


def test_a_sync_of_a_process_that_was_never_keyless_does_not_clear_the_mark(conn, run):
    """Another process said the key was removed. This one has always had its key: its syncs, whether they
    get through or fail, are no sign that the key was put back in .env."""
    run(_holding())
    db.save_cached_value(conn, ta.KEY_REMOVED_KEY, 1.0)             # said by the other process
    run(_holding(), now=_hours(2))
    run(fetch=_down(), now=_hours(3))
    assert _mark(conn) is not None


def test_a_process_that_was_keyless_clears_the_mark_by_a_sync_that_reaches_trading_212_and_fails(conn, run):
    run(_holding())
    run(fetch=_no_key, now=_hours(2))
    assert _mark(conn) is not None
    run(fetch=_down(ta.BAD_KEY, "unauthorized"), now=_hours(3))     # a key is there now (it does not fit)
    assert _mark(conn) is None


# ---------------------------------------------- F4: a list that keeps disagreeing with the summary is not silent
LIST_GAP_WARNING = "⚠️ Trading 212: список позиций не сходится со счётом уже сутки — продажи не отмечаю."


def _off(run, hours, **kw):
    """A sync whose list (two holdings, EUR 200) is nowhere near the summary (EUR 1000 invested)."""
    return run(_holding(), _holding(**SAP), summary=_invested(1000.0), now=_hours(hours), **kw)


def test_a_list_that_has_disagreed_for_a_day_says_so_once_a_day(conn, run):
    run(_holding(), _holding(**SAP))                                # 01.10 14:05: the list adds up
    run.sent.clear()
    for hours in (1, 6, 12, 24):                                    # the run of disagreement begins at 15:05
        assert _off(run, hours).agrees is False
    assert run.sent == []                                           # 23 hours of it: not a day yet
    _off(run, 25)                                                   # 02.10 15:05: a day
    assert run.sent == [LIST_GAP_WARNING]
    assert db.get_cached_value(conn, "t212_gap_2026-10-02", 10**9) is not None       # the day's mark
    _off(run, 26)
    _off(run, 33)                                                   # the same day, 23:05: said once
    assert run.sent == [LIST_GAP_WARNING]
    _off(run, 34)                                                   # 03.10 00:05: another day, another warning
    assert run.sent == [LIST_GAP_WARNING] * 2
    assert positions.find_open(conn, "GME") is not None             # and nothing was closed meanwhile


def test_a_list_that_adds_up_again_ends_the_run_of_disagreement(conn, run):
    run(_holding(), _holding(**SAP))
    run.sent.clear()
    _off(run, 1)
    _off(run, 13)
    assert run(_holding(), _holding(**SAP), now=_hours(14)).agrees is True
    _off(run, 15)                                                   # a new run begins
    _off(run, 26)                                                   # 25 hours after the first run began, 11 after this
    assert run.sent == []
    _off(run, 40)
    assert run.sent == [LIST_GAP_WARNING]


def test_a_list_that_never_added_up_is_not_warned_about(conn, run):
    """Only after a success history: on first contact `--check` says «расходятся»; a list that has never
    added up is not a list that stopped doing so."""
    _synced_before(conn)
    for hours in (0, 12, 25, 49, 73):
        assert _off(run, hours).agrees is False
    assert LIST_GAP_WARNING not in run.sent


def test_a_list_that_cannot_be_checked_neither_warns_nor_ends_the_run(conn, run):
    run(_holding(), _holding(**SAP))
    run.sent.clear()
    _off(run, 1)

    def unknown(hours):
        return run(_holding(), _holding(**SAP), summary=_invested(None), now=_hours(hours))
    assert unknown(12).agrees is None
    _off(run, 25)                                                   # the run is still the one begun at 1 h
    assert run.sent == [LIST_GAP_WARNING]
    unknown(49)                                                     # a new day, but no evidence: no warning
    assert run.sent == [LIST_GAP_WARNING]
    _off(run, 50)
    assert run.sent == [LIST_GAP_WARNING] * 2


def test_a_silent_sync_neither_warns_about_the_list_nor_uses_up_the_days_warning(conn, run):
    run(_holding(), _holding(**SAP))
    run.sent.clear()
    _off(run, 1)
    _off(run, 26, silent=True)                                      # --sync: nothing is sent
    assert run.sent == []
    _off(run, 27)
    assert run.sent == [LIST_GAP_WARNING]


# ---------------------------------------------- F8: a sync that raises while applying is a failed sync
def _crashes(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk full: secret text")
    monkeypatch.setattr(ta, "_apply", boom)


def test_a_sync_that_raises_while_applying_counts_for_the_days_warning(conn, run, monkeypatch):
    """F8: a failure inside _apply is recorded like a failed fetch -- after a day without a sync that got
    through the owner is told, once a day, with the error's type and never its text -- and the error still
    goes up to the caller (the bot loop logs it, the daily run reports it)."""
    run(_holding())                                                 # got through at NOW
    run.sent.clear()
    _crashes(monkeypatch)
    with pytest.raises(RuntimeError):
        run(_holding(), now=_hours(23))
    assert run.sent == []                                           # not a day yet
    with pytest.raises(RuntimeError):
        run(_holding(), now=_hours(25))
    assert run.sent == [_silent_for_a_day("сбой синхронизации: RuntimeError")]
    with pytest.raises(RuntimeError):
        run(_holding(), now=_hours(26))                             # the same day: said once
    assert len(run.sent) == 1 and "secret" not in run.sent[0]
    assert positions.find_open(conn, "GME") is not None             # and nothing of it was kept


def test_a_sync_that_raises_while_applying_is_ended_by_one_that_gets_through(conn, run, monkeypatch):
    run(_holding())
    with monkeypatch.context() as broken:
        _crashes(broken)
        with pytest.raises(RuntimeError):
            run(_holding(), now=_hours(25))
    run(_holding(), now=_hours(26))                                 # it works again
    run.sent.clear()
    with monkeypatch.context() as broken:
        _crashes(broken)
        with pytest.raises(RuntimeError):
            run(_holding(), now=_hours(27))                         # an hour without one is not a day
    assert run.sent == []


def test_a_silent_sync_that_raises_while_applying_says_nothing(conn, run, monkeypatch):
    run(_holding())
    run.sent.clear()
    _crashes(monkeypatch)
    with pytest.raises(RuntimeError):
        run(_holding(), now=_hours(25), silent=True)
    assert run.sent == []


def test_a_sync_that_raises_before_any_got_through_says_nothing(conn, run, monkeypatch):
    _crashes(monkeypatch)
    with pytest.raises(RuntimeError):
        run(_holding(), now=_hours(48))
    assert run.sent == []


def test_a_failure_to_record_the_failure_does_not_hide_the_error(conn, run, monkeypatch, capsys):
    run(_holding())
    _crashes(monkeypatch)

    def cannot(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(ta, "_failed", cannot)
    with pytest.raises(RuntimeError, match="disk full"):             # the sync's own error is the one raised
        run(_holding(), now=_hours(25))
    assert "OperationalError" in capsys.readouterr().err


# ---------------------------------------------- F9: a warning's mark is written before it is sent
def _tracked_two(run):
    run(_holding(), _holding(**SAP))


def _list_off_since_an_hour_in(run):
    _tracked_two(run)
    _off(run, 1)


# name -> (the prefix of its mark in kv, what a sync has to meet before it, the sync that says it
# `hours` after the first one that could: all of them tell the same warning every day, so any later
# hour does as well)
WARNINGS = {
    "silent for a day": ("t212_silent_", lambda run: run(_holding()),
                         lambda run, hours, **kw: run(fetch=_down(), now=_hours(25 + hours), **kw)),
    "key removed": ("t212_key_removed", lambda run: run(_holding()),
                    lambda run, hours, **kw: run(fetch=_no_key, now=_hours(2 + hours), **kw)),
    "list gap": ("t212_gap_", _list_off_since_an_hour_in,
                 lambda run, hours, **kw: _off(run, 25 + hours, **kw)),
}
ALL_WARNINGS = (_silent_for_a_day(), KEY_REMOVED, LIST_GAP_WARNING)


def _said(run):
    return [text for text in run.sent if text in ALL_WARNINGS]


@pytest.mark.parametrize("name", WARNINGS)
def test_a_warnings_mark_is_stored_before_the_warning_goes_out(conn, run, name):
    prefix, setup, trigger = WARNINGS[name]
    marks = []

    def spy(text):
        marks.append(conn.execute("SELECT COUNT(*) FROM kv_cache WHERE key LIKE ?", (prefix + "%",)).fetchone()[0])
        return True
    setup(run)
    trigger(run, 0, notify=spy)
    assert marks == [1]


@pytest.mark.parametrize("name", WARNINGS)
def test_a_warning_whose_mark_cannot_be_written_is_not_sent_and_not_repeated(conn, run, monkeypatch, name):
    """A failing mark write -- a full disk, a database in trouble -- must not turn into a warning sent at
    every sync: nothing goes out until the mark can be stored, and then it goes out once."""
    prefix, setup, trigger = WARNINGS[name]
    real = db.save_cached_value

    def failing(c, key, value, **kw):
        if key.startswith(prefix):
            raise sqlite3.OperationalError("disk I/O error")
        return real(c, key, value, **kw)
    setup(run)
    monkeypatch.setattr(db, "save_cached_value", failing)
    for hours in (0, 1, 2):                                         # sync after sync, the same trouble
        trigger(run, hours)
    assert _said(run) == []
    monkeypatch.setattr(db, "save_cached_value", real)             # the trouble is over
    for hours in (3, 4):
        trigger(run, hours)
    assert len(_said(run)) == 1


def _shared(tmp_path):
    path = tmp_path / "shared.db"
    mine, other = db.connect(path), db.connect(path)
    mine.execute("PRAGMA busy_timeout = 50")
    return mine, other


def _got_through(conn, now=NOW):
    holding = _holding()
    ta.sync(conn, fetch=lambda: ([holding], _agreeing([holding])), notify=lambda t: True, now=now,
            closes_fn=lambda t, s=None: _flat(24.0))


@pytest.mark.parametrize("fetch, hours, text", [
    (_down(), 25, _silent_for_a_day()), (_no_key, 2, KEY_REMOVED)], ids=["silent for a day", "key removed"])
def test_a_locked_database_does_not_repeat_a_warning(tmp_path, fetch, hours, text):
    """F9: the daily run holds the database for a while. The warning used to go out first and then raise
    at its mark, so every sync -- every 15 minutes -- sent it again for as long as the lock lasted. Now
    nothing is sent while the mark cannot be written, and no sync raises for it."""
    mine, other = _shared(tmp_path)
    _got_through(mine)
    sent = []
    other.execute("BEGIN IMMEDIATE")                                # the other process holds the database
    for quarter in range(4):
        result = ta.sync(mine, fetch=fetch, notify=lambda t: sent.append(t) or True,
                         now=_hours(hours) + dt.timedelta(minutes=15 * quarter))
        assert result.error                                         # failed as before, with no OperationalError
    assert sent == []
    other.rollback()                                                # the lock is released
    for later in (1, 2, 3):
        ta.sync(mine, fetch=fetch, notify=lambda t: sent.append(t) or True, now=_hours(hours + later))
    assert sent == [text]                                           # once
    mine.close()
    other.close()


def test_a_message_whose_mark_cannot_be_given_back_is_not_repeated(tmp_path):
    """Telegram refuses the warning; meanwhile the other process takes the database, so the mark cannot be
    given back for a retry. It stays, and the warning is not repeated."""
    mine, other = _shared(tmp_path)
    _got_through(mine)

    def refused(text):
        other.execute("BEGIN IMMEDIATE")
        return False
    ta.sync(mine, fetch=_down(), notify=refused, now=_hours(25))
    other.rollback()
    sent = []
    ta.sync(mine, fetch=_down(), notify=lambda t: sent.append(t) or True, now=_hours(26))
    assert sent == []
    mine.close()
    other.close()


# ---------------------------------------------- M3: a price of zero is not a price
def test_a_price_of_zero_is_not_stored_and_drives_no_stop(conn, run):
    _synced_before(conn)
    run(_holding(**SAP))                                            # 125,00
    run(_holding(**dict(SAP, price=0.0)), now=NOW + dt.timedelta(days=1))
    assert conn.execute("SELECT date, price FROM t212_prices").fetchall() == [("2026-10-01", 125.0)]
    day = NOW.date() + dt.timedelta(days=1)
    assert positions.last_close("DE0007164600", "T212", conn=conn, today=day) == 125.0
    assert positions.check_exits(conn, today=day, news_fn=lambda t, s=None: []) == []


def test_a_zero_already_in_the_day_prices_is_not_read_as_one(conn):
    conn.execute("INSERT INTO t212_prices (ticker, date, price) VALUES ('X', '2026-10-01', 12.0)")
    conn.execute("INSERT INTO t212_prices (ticker, date, price) VALUES ('X', '2026-10-02', 0.0)")
    assert positions.t212_closes(conn, "X") == [("2026-10-01", 12.0)]


def test_a_holding_that_pre_dates_tracking_and_is_priced_at_zero_takes_its_floor_from_the_history(conn, run):
    run.yahoo = lambda ticker: _flat(60.0)
    run(_holding(created=OLD, avg=100.0, price=0.0))
    pos = positions.find_open(conn, "GME")
    assert pos.stop_base == 60.0 and conn.execute("SELECT COUNT(*) FROM t212_prices").fetchone() == (0,)


def test_the_live_view_shows_no_price_for_a_price_of_zero(conn, run, no_yahoo):
    _synced_before(conn)
    run(_holding())
    [h] = _view(conn, _holding(price=0.0)).holdings
    assert h.price is None and h.status["last"] is None


# ---------------------------------------------- M4: a take-over needs the same listing
ADR = dict(t212_ticker="EQNR_US_EQ", isin="US29446M1027", avg=25.0, price=26.0)      # Equinor in New York


def test_a_us_instrument_does_not_take_over_a_position_bought_on_oslo(conn, run):
    """EQNR in Oslo and EQNR in New York are two listings: the /bought one stays the owner's own,
    and the account's holding is tracked beside it, keyed by its ISIN."""
    _synced_before(conn)
    oslo = positions.open_position(conn, "EQNR", 270.0, today=dt.date(2026, 9, 1), source="NORWAY",
                                   closes_fn=lambda t, s=None: [])
    result = run(_holding(**ADR))
    assert result.opened == ["US29446M1027"] and result.updated == []
    by = {p.ticker: p for p in positions.open_positions(conn)}
    assert (by["EQNR"].id, by["EQNR"].origin, by["EQNR"].source, by["EQNR"].entry_price) == \
        (oslo.id, "manual", "NORWAY", 270.0)
    assert (by["US29446M1027"].origin, by["US29446M1027"].source, by["US29446M1027"].t212_ticker) == \
        ("t212", "T212", "EQNR_US_EQ")
    assert run.sent[0].startswith("⚪ <b>EQNR!</b>: куплено в Trading 212 — ") and run.histories == []


@pytest.mark.parametrize("source", ["NORWAY", "SWEDEN"])
def test_with_no_isin_to_key_it_by_such_a_holding_is_left_untracked_and_blocks_no_sale(conn, run, source, capsys):
    _synced_before(conn)
    run(_holding(**SAP))
    positions.open_position(conn, "EQNR", 270.0, today=dt.date(2026, 9, 1), source=source,
                            closes_fn=lambda t, s=None: [])
    result = run(_holding("EQNR_US_EQ", None, avg=25.0, price=26.0))
    assert result.opened == [] and result.closed == ["DE0007164600"]        # SAP was sold: still seen
    assert positions.find_open(conn, "EQNR").origin == "manual"
    assert "another exchange" in capsys.readouterr().err


@pytest.mark.parametrize("source", [None, "SEC", "HOUSE", "SEC13DG"])
def test_a_us_instrument_takes_over_a_position_bought_as_a_us_stock(conn, run, source):
    _synced_before(conn)
    manual = positions.open_position(conn, "GME", 20.0, today=dt.date(2026, 9, 1), source=source,
                                     closes_fn=lambda t, s=None: [])
    result = run(_holding())
    pos = positions.find_open(conn, "GME")
    assert result.updated == ["GME"] and (pos.id, pos.origin, pos.source) == (manual.id, "t212", source)


@pytest.mark.parametrize("source", ["BAFIN", "SWEDEN", None])
def test_a_holding_takes_over_a_position_bought_under_the_same_isin_whatever_named_it(conn, run, source):
    _synced_before(conn)
    manual = positions.open_position(conn, "DE0007164600", 110.0, today=dt.date(2026, 9, 1), source=source,
                                     closes_fn=lambda t, s=None: [])
    run(_holding(**SAP))
    pos = positions.find_open(conn, "DE0007164600")
    assert (pos.id, pos.origin, pos.source) == (manual.id, "t212", "T212")


def test_a_frankfurt_holding_does_not_take_over_the_us_stock_of_the_same_letters(conn, run):
    _synced_before(conn)
    us = positions.open_position(conn, "SAP", 250.0, today=dt.date(2026, 9, 1), closes_fn=lambda t, s=None: [])
    assert run(_holding(**SAP)).opened == ["DE0007164600"]
    assert positions.find_open(conn, "SAP").id == us.id and positions.find_open(conn, "SAP").origin == "manual"


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
    assert run.sent == ["⚪ <b>GME!</b>: куплено в Trading 212 — 10 шт. по 23,10 USD, "
                        "слежу: стоп, срок и новости"]              # the flag decides the message only


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
    return run.sent[-1].split(", слежу: ")[1]


def test_a_us_holding_with_insiders_is_watched_for_its_stop_their_sales_and_its_news(conn, run):
    _synced_before(conn)
    _journal(conn, "GME", ["Ryan Cohen"])
    assert _watched(run) == "стоп, продажи инсайдеров, новости"


def test_a_us_holding_with_no_matched_insiders_is_not_said_to_be_watched_for_their_sales(conn, run):
    _synced_before(conn)
    _journal(conn, "AAPL", ["Tim Cook"])                            # another stock's insiders
    assert _watched(run) == "стоп, срок и новости"


def test_a_holding_with_no_news_feed_and_no_matched_insiders_is_watched_for_its_stop_and_its_time(conn, run):
    _synced_before(conn)
    _journal(conn, "SAP", ["US Boss"], source="SEC")               # the US SAP: not this listing's insiders
    assert _watched(run, **SAP) == "стоп и срок"


@pytest.mark.parametrize("source", ["BAFIN", "SWEDEN"])
def test_insiders_the_journal_matches_by_isin_are_named(conn, run, source):
    _synced_before(conn)
    _journal(conn, "DE0007164600", ["Vorstand"], source=source)
    assert _watched(run, **SAP) == "стоп и срок, а также продажи инсайдеров"
    assert positions.find_open(conn, "DE0007164600").insiders == ["Vorstand"]


def test_an_oslo_company_is_watched_for_its_oslo_insiders_and_its_oslo_news(conn, run):
    _synced_before(conn)
    conn.execute("INSERT INTO oslo_isins (ticker, isin, fetched_at) VALUES ('EQNR', 'NO0010096985', "
                 "'2026-09-01T00:00:00')")
    _journal(conn, "EQNR", ["Oslo Boss"], source="NORWAY")
    assert _watched(run, **_OSLO) == "стоп и срок, а также продажи инсайдеров и новости"


def test_an_oslo_company_with_no_signal_is_watched_for_its_news_but_not_for_insiders(conn, run):
    _synced_before(conn)
    conn.execute("INSERT INTO oslo_isins (ticker, isin, fetched_at) VALUES ('EQNR', 'NO0010096985', "
                 "'2026-09-01T00:00:00')")
    assert _watched(run, **_OSLO) == "стоп и срок, а также новости"


def test_a_signal_with_no_names_matches_no_insiders(conn, run):
    _synced_before(conn)
    _journal(conn, "DE0007164600", [], source="BAFIN")
    assert _watched(run, **SAP) == "стоп и срок"


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
    assert run.sent == ["⚪ <b>SAP!</b>: куплено в Trading 212 — 10 шт. по 120,00 EUR, слежу: стоп и срок"]


def test_a_silent_sync_after_the_first_message_says_nothing_either(conn, run):
    _synced_before(conn)
    assert run(_holding(), silent=True).opened == ["GME"] and run.sent == []
    assert run(silent=True).closed == ["GME"] and run.sent == []
    assert _flag(conn) is not None


def test_a_silent_sync_never_calls_notify(conn):
    result = ta.sync(conn, fetch=lambda: ([_holding()], SUMMARY), now=NOW, closes_fn=lambda t, s=None: _flat(24.0),
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


def test_the_live_view_finds_a_tracked_holding_by_its_trading_212_id(conn, run, no_yahoo):
    _synced_before(conn)
    run(_holding(**FB))                                             # tracked as FB
    _instrument(conn, "FB_US_EQ", "META")
    [h] = _view(conn, _holding(**dict(FB, price=510.0))).holdings
    assert h.position is not None and (h.position.ticker, h.name) == ("FB", "FB")
    assert conn.execute("SELECT ticker, price FROM t212_prices").fetchall() == [("FB", 510.0)]


def test_the_live_view_names_a_holding_it_does_not_track_yet_by_its_market_symbol(conn, no_yahoo):
    _instrument(conn, "FB_US_EQ", "META")
    [h] = _view(conn, _holding(**FB)).holdings
    assert (h.name, h.position) == ("META", None)


def test_a_stored_price_older_than_three_days_is_shown_with_its_date_and_drives_no_stop(conn, run, no_yahoo):
    """No sync for four days: the last stored price is not a price any more. It is shown as the
    price of its day, and no stop is read from it."""
    _synced_before(conn)
    run(_holding())                                                 # 01.10, at 24,05
    down = _fails(ta.T212Error("ReadTimeout", "network"))
    [fresh] = _view(conn, fetch=down, now=NOW + dt.timedelta(days=3)).holdings
    assert (fresh.price, fresh.price_date, fresh.status["last"]) == (24.05, None, 24.05)
    [stale] = _view(conn, fetch=down, now=NOW + dt.timedelta(days=4)).holdings
    assert (stale.price, stale.price_date, stale.status["last"]) == (24.05, "2026-10-01", None)
    assert stale.status["to_stop"] is None


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


ISIN_ON_TWO_EXCHANGES = "IE00B3XXRP09"


@pytest.mark.parametrize("tracked_first", [True, False])
def test_the_live_view_does_not_overwrite_a_tracked_holdings_day_price_with_another_listings(
        conn, run, no_yahoo, tracked_first):
    """F2: one ISIN held on two exchanges is one position, priced by the listing the sync tracks (the
    first one listed then). The live call stores that listing's price under it, in either order of the
    list -- not the other exchange's, which is another currency."""
    _synced_before(conn)
    london = _holding("VUSAl_EQ", "IE00B3XXRP09", currency="GBP", price=80.0)
    xetra = _holding("VUSAd_EQ", "IE00B3XXRP09", currency="EUR", price=95.0)
    run(london, xetra)                                              # tracked: London, listed first
    assert [p.t212_ticker for p in positions.open_positions(conn)] == ["VUSAl_EQ"]
    now_london, now_xetra = (dataclasses.replace(london, current_price=81.0),
                             dataclasses.replace(xetra, current_price=96.0))
    _view(conn, *((now_london, now_xetra) if tracked_first else (now_xetra, now_london)))
    assert conn.execute("SELECT ticker, price FROM t212_prices").fetchall() == [(ISIN_ON_TWO_EXCHANGES, 81.0)]


def test_the_live_view_stores_one_price_for_two_listings_of_an_isin_the_sync_does_not_track_yet(conn, no_yahoo):
    """Nothing tracks this ISIN yet: the first listing on the list is the one the sync will track, and its
    is the price stored -- the second listing's does not replace it."""
    london = _holding("VUSAl_EQ", "IE00B3XXRP09", currency="GBP", price=80.0)
    xetra = _holding("VUSAd_EQ", "IE00B3XXRP09", currency="EUR", price=95.0)
    _view(conn, london, xetra)
    assert conn.execute("SELECT price FROM t212_prices").fetchall() == [(80.0,)]


def test_the_live_view_does_not_store_a_price_under_a_position_that_is_not_that_holdings(conn, no_yahoo):
    """A /bought position keyed by this ISIN is somebody's already: an untracked holding of the same key
    does not write under it (the sync takes it over and stores its price then)."""
    positions.open_position(conn, "IE00B3XXRP09", 70.0, today=dt.date(2026, 9, 1), source="BAFIN",
                            closes_fn=lambda t, s=None: [])
    _view(conn, _holding("VUSAd_EQ", "IE00B3XXRP09", currency="EUR", price=95.0))
    assert conn.execute("SELECT COUNT(*) FROM t212_prices").fetchone() == (0,)


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
    assert out.out.splitlines()[0] == "Trading 212: доступ есть, позиций 2, валюта EUR" and out.err == ""
    assert session.calls == [("get", ta.POSITIONS_URL), ("get", ta.SUMMARY_URL)]


def _check_line(monkeypatch, capsys, positions_payload, summary_payload=None):
    _wire(monkeypatch, _account(positions_payload, summary_payload))
    assert ta.main(["--check"]) == 0
    return capsys.readouterr().out.splitlines()


def _payload(ticker, isin, price=10.0, value=100.0):
    return {"instrument": {"ticker": ticker, "isin": isin, "currency": "EUR"}, "quantity": 1,
            "currentPrice": price, "averagePricePaid": 9.0, "walletImpact": {"currentValue": value}}


def test_check_counts_how_the_holdings_are_keyed_and_priced(keyed, monkeypatch, capsys):
    held = [_payload("GME_US_EQ", "US36467W1099"), _payload("FB_US_EQ", "US30303M1027", price=0),
            _payload("SAPd_EQ", "DE0007164600"), _payload("ODDd_EQ", None, price=None)]
    lines = _check_line(monkeypatch, capsys, held, dict(FULL_SUMMARY, investments={"currentValue": 400.0}))
    assert lines == ["Trading 212: доступ есть, позиций 4, валюта EUR",
                     "Ключи: США — 2, ISIN — 1, без ключа — 1; с ценой — 2 из 4; список и сводка сходятся"]


def test_check_says_when_the_list_and_the_summary_disagree_or_cannot_be_compared(keyed, monkeypatch, capsys):
    two = [_payload("GME_US_EQ", "US36467W1099"), _payload("SAPd_EQ", "DE0007164600")]
    lines = _check_line(monkeypatch, capsys, two, dict(FULL_SUMMARY, investments={"currentValue": 400.0}))
    assert lines[1] == "Ключи: США — 1, ISIN — 1; с ценой — 2 из 2; список и сводка расходятся на 50%"
    lines = _check_line(monkeypatch, capsys, two, dict(FULL_SUMMARY, investments={}))
    assert lines[1] == "Ключи: США — 1, ISIN — 1; с ценой — 2 из 2; список и сводку не сверить"
    lines = _check_line(monkeypatch, capsys, [], dict(FULL_SUMMARY, investments={"currentValue": 0}))
    assert lines[1] == "Ключи: США — 0, ISIN — 0; с ценой — 0 из 0; список и сводка сходятся"


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
    monkeypatch.setattr(positions, "daily_closes", lambda ticker, source=None: _flat(24.0))   # Yahoo's GME
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


def test_the_readme_claims_of_the_safety_tests_only_what_they_prove():
    readme = (Path(ta.__file__).parent / "README.md").read_text(encoding="utf-8")
    start = readme.index("### Trading 212\n")
    section = " ".join(readme[start:readme.index("\n### ", start + 5)].split())
    assert "Главная защита — ключ без прав на заявки" in section
    assert "не доказательство" in section                           # an inspection of the text, not a proof
    for phrase in ("`.send(`", "`getattr(`", "кроме `requests`"):
        assert phrase in section, phrase
    assert "торговать нечем" not in section and "падают, если в них появится хоть один вызов" not in section
    assert "сплит" in section                                       # M10: one false stop after a stock split


def test_the_readme_says_what_happens_when_yahoo_has_nothing_for_a_us_holding():
    readme = (Path(ta.__file__).parent / "README.md").read_text(encoding="utf-8")
    start = readme.index("### Trading 212\n")
    section = " ".join(readme[start:readme.index("\n### ", start + 5)].split())
    assert "Если Yahoo не ответил или не знает тикер, бумага остаётся под своим тикером" in section
    assert "если Yahoo не знает такого тикера или" not in section    # that no longer keys it by its ISIN


def test_the_readme_says_a_removed_key_is_said_once():
    readme = (Path(ta.__file__).parent / "README.md").read_text(encoding="utf-8")
    start = readme.index("### Trading 212\n")
    section = " ".join(readme[start:readme.index("\n### ", start + 5)].split())
    assert "«Ключ Trading 212 убран — слежение за счётом остановлено." in section
    assert "один раз" in section and "пока ключ не появится снова" in section


def test_the_readme_says_to_restart_the_telegram_agent_after_the_key_changes():
    """F1: the agent keeps the key it was started with; the daily run reads .env afresh."""
    readme = (Path(ta.__file__).parent / "README.md").read_text(encoding="utf-8")
    key = " ".join(readme[readme.index("### Ключ Trading 212\n"):].split())[:2500]
    assert "launchctl kickstart -k gui/$(id -u)/com.disclosurebot.telegram" in key
    start = readme.index("### Trading 212\n")
    section = " ".join(readme[start:readme.index("\n### ", start + 5)].split())
    assert "перезапустите Telegram-бота" in section and "за последний час" in section


def test_the_readme_says_an_empty_list_is_a_bad_answer_only_above_five_euros():
    """F3."""
    readme = (Path(ta.__file__).parent / "README.md").read_text(encoding="utf-8")
    start = readme.index("### Trading 212\n")
    section = " ".join(readme[start:readme.index("\n### ", start + 5)].split())
    assert "Пустой список, когда во вложениях больше €5, — плохой ответ" in section


def test_the_readme_says_what_a_list_that_cannot_be_checked_and_one_that_keeps_disagreeing_do():
    """F4, F5, F6."""
    readme = (Path(ta.__file__).parent / "README.md").read_text(encoding="utf-8")
    start = readme.index("### Trading 212\n")
    section = " ".join(readme[start:readme.index("\n### ", start + 5)].split())
    assert LIST_GAP_WARNING in section and "а раньше сходился" in section                       # F4
    assert "в другой валюте, чем в сводке" in section                                           # F5
    assert "не раньше чем через 10 минут после первой" in section                              # F6


def test_the_readme_says_how_a_foreign_yahoo_series_a_failed_sync_and_a_locked_database_are_treated():
    """F7, F8, F9."""
    readme = (Path(ta.__file__).parent / "README.md").read_text(encoding="utf-8")
    start = readme.index("### Trading 212\n")
    section = " ".join(readme[start:readme.index("\n### ", start + 5)].split())
    assert "расходится с сохранённой свежей ценой Trading 212 больше чем на 20%" in section          # F7
    assert "и про сбой самой синхронизации" in section                                              # F8
    assert "помечается в базе до отправки" in section                                               # F9


# ---------------------------------------------- F10: stale text
def test_the_bots_comment_says_a_holding_is_matched_by_its_ticker_or_its_stored_code_only():
    """telegram_bot used to say a holding was also found by the name it is shown under: it never is
    (positions.find_holding: the position's ticker, or the symbol of the Trading 212 code stored with it)."""
    source = (Path(ta.__file__).parent / "telegram_bot.py").read_text(encoding="utf-8")
    start = source.index("# A ticker the Trading 212 account holds")
    comment = " ".join(source[start:source.index("held = positions.find_open(", start)].replace("#", " ").split())
    assert "by the name the holding is shown under" not in comment          # the stale claim
    assert "the position's own ticker" in comment and "t212_ticker" in comment
    assert "not by the name" in comment and "keyed by its ISIN" in comment


def test_the_readme_split_note_applies_to_any_holding_priced_by_trading_212():
    readme = (Path(ta.__file__).parent / "README.md").read_text(encoding="utf-8")
    note = " ".join(readme[readme.index("**Сплит акций.**"):readme.index("### Ключ Trading 212")].split())
    assert "Для любой бумаги, которая оценивается по ценам Trading 212" in note
    assert "хранится по ISIN" in note and "американская" in note       # the ones it covers
    assert "сплит" in note and "ложное" in note
