"""t212_account.py: the read-only Trading 212 client (spec 2026-10-01-trading212-account-tracking.md).
Offline: every call goes to a stub session, and conftest keeps any real key out of the tests."""
from __future__ import annotations

import ast
import base64
import re
from pathlib import Path

import pytest
import requests

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


@pytest.mark.parametrize("path", _SOURCES, ids=lambda p: p.name)
def test_the_source_has_no_write_call(path):
    source = path.read_text(encoding="utf-8")
    for call in (".post(", ".put(", ".patch(", ".delete(", "requests.request(", ".request("):
        assert call not in source, f"{path.name} contains {call}"


@pytest.mark.parametrize("path", _SOURCES, ids=lambda p: p.name)
def test_every_url_in_the_source_is_a_whitelisted_read(path):
    source = path.read_text(encoding="utf-8")
    assert "/orders" not in source and "/order" not in source
    for url in re.findall(r"https?://[^\s\"'<>)]+", source):
        assert url in READ_URLS, url
    paths = {u.removeprefix("https://live.trading212.com") for u in READ_URLS}
    for found in re.findall(r"/api/v0/[A-Za-z0-9_/.{}-]*", source):
        assert found in paths, found
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            if "trading212.com" in text or text.startswith(("http://", "https://", "/api", "/equity")):
                assert text in READ_URLS, text


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
