"""menu.py's "Сигналы" view and what it filters on: disclosure recency
(cluster/recency.py) and Trading 212 availability (trading212.py). Offline -- the
instrument list comes from a fake session, and scoring's network step is stubbed."""
from __future__ import annotations

import datetime as dt

import pytest

import cluster
import crypto_treasury as ct
import db
import menu
import trading212
from conftest import add_house_txn, add_sec_purchase

TODAY = dt.date.today()

INSTRUMENTS = [
    {"ticker": "AAPL_US_EQ", "isin": "US0378331005", "type": "STOCK", "shortName": "AAPL",
     "currencyCode": "USD"},
    {"ticker": "BRK_B_US_EQ", "isin": "US0846707026", "type": "STOCK", "shortName": "BRK.B",
     "currencyCode": "USD"},
    {"ticker": "EQNRo_EQ", "isin": "NO0010096985", "type": "STOCK", "shortName": "EQNR",
     "currencyCode": "NOK"},
    {"ticker": "SAPd_EQ", "isin": "DE0007164600", "type": "STOCK", "shortName": "SAP",
     "currencyCode": "EUR"},
    {"ticker": "SPYl_EQ", "isin": "IE00B6YX5C33", "type": "ETF", "shortName": "SPY5",
     "currencyCode": "USD"},
    {"ticker": "XXX_US_EQ", "isin": "US0000000001", "type": "WARRANT", "shortName": "XXX",
     "currencyCode": "USD"},
]


class _Session:
    def __init__(self, payload=INSTRUMENTS, status=200):
        self.payload, self.status, self.calls = payload, status, 0

    def get(self, url, headers=None, timeout=None):
        self.calls += 1
        self.headers = headers
        session = self

        class Resp:
            status_code = session.status

            def raise_for_status(self):
                if session.status >= 400:
                    raise trading212.requests.HTTPError(f"{session.status}")

            def json(self):
                return session.payload
        return Resp()


@pytest.fixture
def keyed(monkeypatch, tmp_path):
    monkeypatch.setattr(trading212, "ENV_FILE", tmp_path / "missing.env")
    monkeypatch.setenv("TRADING212_API_KEY", "key")
    monkeypatch.setenv("TRADING212_API_SECRET", "secret")


# ------------------------------------------------------------------ trading212
def test_availability_matches_us_tickers_isins_and_oslo(conn, keyed):
    t212 = trading212.availability(conn, _Session())
    assert t212.can_buy("AAPL", "SEC") and t212.can_buy("BRK.B", "HOUSE")
    assert t212.can_buy("DE0007164600", "BAFIN")       # ISIN
    assert t212.can_buy("EQNR", "NORWAY")
    assert not t212.can_buy("EQNR", "SEC")            # an Oslo listing isn't a US one
    assert not t212.can_buy("ZZZZ", "SEC")
    assert not t212.can_buy("XXX", "SEC")             # a warrant isn't something to buy here
    assert t212.can_buy("CRYPTO:BTC", "CRYPTO_ETF")   # crypto is never filtered out


def test_uses_basic_auth_with_key_and_secret(conn, keyed):
    session = _Session()
    trading212.availability(conn, session)
    assert session.headers["Authorization"].startswith("Basic ")


def test_instrument_list_is_cached_for_a_day(conn, keyed):
    session = _Session()
    trading212.availability(conn, session)
    trading212.availability(conn, session)
    assert session.calls == 1                         # the API allows one call per 50s


def test_no_key_means_unknown_not_unavailable(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(trading212, "ENV_FILE", tmp_path / "missing.env")
    monkeypatch.delenv("TRADING212_API_KEY", raising=False)
    assert trading212.availability(conn, _Session()) is None


def test_rejected_key_falls_back_to_the_cached_list(conn, keyed):
    trading212.availability(conn, _Session())
    db.save_cached_value(conn, trading212._CACHE_KEY, 1.0)
    conn.execute("UPDATE kv_cache SET computed_at = '2000-01-01T00:00:00' WHERE key = ?",
                 (trading212._CACHE_KEY,))
    t212 = trading212.availability(conn, _Session(payload={"code": "Unauthorized"}, status=401))
    assert t212 is not None and t212.can_buy("AAPL", "SEC")


def test_key_is_read_from_the_env_file(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text('TELEGRAM_CHAT_ID="1"\nexport TRADING212_API_KEY="abc"\n')
    monkeypatch.setattr(trading212, "ENV_FILE", env)
    monkeypatch.delenv("TRADING212_API_KEY", raising=False)
    monkeypatch.delenv("TRADING212_API_SECRET", raising=False)
    assert trading212._auth_headers() == {"Authorization": "abc"}


# -------------------------------------------------------------------- recency
def test_sec_signal_is_dated_by_its_filing(conn):
    add_sec_purchase(conn, "AAA", "Buyer One", 600_000, (TODAY - dt.timedelta(days=9)).isoformat(),
                     filed_date=(TODAY - dt.timedelta(days=1)).isoformat())
    [sig] = cluster.find_sec_clusters(conn)
    assert cluster.disclosed_on(conn, sig) == (TODAY - dt.timedelta(days=1)).isoformat()


def test_house_signal_is_dated_by_when_it_was_found_not_traded(conn):
    """Traded 20 days ago, disclosed today: that's news today."""
    traded = (TODAY - dt.timedelta(days=20)).strftime("%m/%d/%Y")
    for member in ("Member One", "Member Two"):
        add_house_txn(conn, "AAA", member, "$250,001 - $500,000", date=traded)
    [sig] = cluster.find_house_clusters(conn)
    # found_at defaults to SQLite datetime('now'), which is UTC.
    assert cluster.disclosed_on(conn, sig) == dt.datetime.now(dt.timezone.utc).date().isoformat()


def test_crypto_signal_is_dated_by_its_own_filing(conn):
    filed = (TODAY - dt.timedelta(days=2)).isoformat()
    db.save_crypto_treasury_txn(conn, ct.TreasuryTxn(
        "acc", "Acme", "ACME", "1", "BTC", "P", 100, 80_000.0, None, filed, "8-K", "u"))
    [sig] = cluster.find_treasury_signals(conn)
    assert cluster.disclosed_on(conn, sig) == filed


# ------------------------------------------------------------ the menu view
@pytest.fixture
def scored(monkeypatch):
    """enrich_signals reaches the network; here every signal just scores 100 and
    clears the size floors for non-crypto signals."""
    def fake_enrich(conn, signals):
        for s in signals:
            s.score = 100.0
            if not getattr(s, "crypto_kind", None):
                s.market_cap_eur, s.avg_daily_value = 5e9, 5e7
        return signals
    monkeypatch.setattr("cluster.enrich_signals", fake_enrich)


def _shown(capsys) -> str:
    return capsys.readouterr().out


@pytest.fixture
def no_prices(monkeypatch):
    monkeypatch.setattr("positions.last_close", lambda ticker: None)


def test_view_keeps_recent_buyable_stocks_and_crypto(conn, keyed, scored, no_prices, capsys,
                                                      monkeypatch):
    monkeypatch.setattr(trading212, "fetch_instruments", lambda session=None: INSTRUMENTS)
    monkeypatch.setattr("crypto.price_trend", lambda conn, sym: {"ret_7d": 3.0, "above_ma20": True})
    recent = (TODAY - dt.timedelta(days=1)).isoformat()
    for o in ("A", "B", "C"):
        add_sec_purchase(conn, "AAPL", o, 900_000, recent, filed_date=recent)
        add_sec_purchase(conn, "ZZZZ", o, 900_000, recent, filed_date=recent)   # not on T212
    db.save_crypto_treasury_txn(conn, ct.TreasuryTxn(
        "acc", "Acme", "ACME", "1", "BTC", "P", 1000, 80_000.0, None, recent, "8-K", "u"))
    menu.show_signals(conn)
    out = _shown(capsys)
    assert "Сильные" in out and "AAPL" in out and "CRYPTO:BTC" in out and "ZZZZ" not in out


def test_view_drops_signals_disclosed_before_the_window(conn, keyed, scored, no_prices, capsys,
                                                         monkeypatch):
    monkeypatch.setattr(trading212, "fetch_instruments", lambda session=None: INSTRUMENTS)
    add_sec_purchase(conn, "AAPL", "Buyer", 900_000, (TODAY - dt.timedelta(days=10)).isoformat(),
                     filed_date=(TODAY - dt.timedelta(days=5)).isoformat())
    menu.show_signals(conn)
    assert "сигналов нет" in _shown(capsys)


def test_view_without_a_key_says_so_and_keeps_stocks(conn, scored, no_prices, capsys,
                                                      monkeypatch, tmp_path):
    monkeypatch.setattr(trading212, "ENV_FILE", tmp_path / "missing.env")
    monkeypatch.delenv("TRADING212_API_KEY", raising=False)
    recent = (TODAY - dt.timedelta(days=1)).isoformat()
    add_sec_purchase(conn, "ZZZZ", "Buyer", 900_000, recent, filed_date=recent)
    menu.show_signals(conn)
    out = _shown(capsys)
    assert "Trading 212 не проверялся" in out and "ZZZZ" in out


def test_view_lists_open_positions(conn, keyed, scored, capsys, monkeypatch):
    import positions
    monkeypatch.setattr(trading212, "fetch_instruments", lambda session=None: INSTRUMENTS)
    monkeypatch.setattr("positions.last_close", lambda ticker: 110.0)
    positions.open_position(conn, "AAPL", 100.0)
    menu.show_signals(conn)
    assert "Открытые позиции" in _shown(capsys)
