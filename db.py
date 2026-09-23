"""SQLite storage: dedup state + a queryable log of every purchase found."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sec_purchases (
    accession           TEXT NOT NULL,
    issuer_name         TEXT,
    issuer_cik          TEXT,
    ticker              TEXT,
    owner_name          TEXT,
    owner_cik           TEXT,
    is_officer          INTEGER,
    is_director         INTEGER,
    is_ten_pct_owner    INTEGER,
    officer_title       TEXT,
    transaction_date    TEXT,
    shares              REAL,
    price               REAL,
    value               REAL,
    security_title      TEXT,
    derivative          INTEGER,
    source_url          TEXT,
    found_at            TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (accession, transaction_date, security_title, shares, price)
);

CREATE TABLE IF NOT EXISTS sec_seen_accessions (
    accession TEXT PRIMARY KEY
);

-- Open-market sales (transactionCode "S"), kept only so exit_signal.py can tell
-- when insiders who clustered into a buy later cluster into selling the same
-- stock. Not used by the main purchase-tracking pipeline.
CREATE TABLE IF NOT EXISTS sec_sales (
    accession           TEXT NOT NULL,
    issuer_name         TEXT,
    issuer_cik          TEXT,
    ticker              TEXT,
    owner_name          TEXT,
    owner_cik           TEXT,
    is_director         INTEGER,
    officer_title       TEXT,
    transaction_date    TEXT,
    shares              REAL,
    price               REAL,
    value               REAL,
    source_url          TEXT,
    found_at            TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (accession, transaction_date, shares, price)
);

-- House "house_purchases" holds every PTR transaction type (P/S/S (partial)/E),
-- not just purchases -- txn_type distinguishes them. Kept under this name to
-- avoid a disruptive rename; the main purchase feed filters txn_type == 'P'.
CREATE TABLE IF NOT EXISTS house_purchases (
    doc_id              TEXT NOT NULL,
    member_name         TEXT,
    state_district      TEXT,
    owner               TEXT,
    asset               TEXT,
    ticker              TEXT,
    txn_type            TEXT,
    txn_date            TEXT,
    notification_date   TEXT,
    amount_range        TEXT,
    source_url          TEXT,
    found_at            TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (doc_id, asset, txn_date, amount_range)
);

CREATE TABLE IF NOT EXISTS house_seen_doc_ids (
    doc_id TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS insider_scores (
    owner_cik       TEXT PRIMARY KEY,
    owner_name      TEXT,
    return_pct      REAL,
    market_return_pct REAL,
    num_purchases   INTEGER,
    computed_at     TEXT
);

CREATE TABLE IF NOT EXISTS kv_cache (
    key         TEXT PRIMARY KEY,
    value       REAL,
    computed_at TEXT
);

CREATE TABLE IF NOT EXISTS cluster_alert_state (
    source          TEXT NOT NULL,   -- 'SEC' or 'HOUSE'
    ticker          TEXT NOT NULL,
    last_count      INTEGER,         -- distinct buyers last alerted for this ticker
    last_members    TEXT,            -- comma-joined names, for reference
    last_alert_at   TEXT,
    PRIMARY KEY (source, ticker)
);

-- Historical close price for a ticker on a specific date never changes once past,
-- so this is cached forever (unlike kv_cache, which has a TTL). Used by
-- congress_score.py to estimate return on House PTR purchases (which disclose only
-- a dollar bracket, never the price actually paid).
CREATE TABLE IF NOT EXISTS price_history_cache (
    ticker  TEXT NOT NULL,
    date    TEXT NOT NULL,
    price   REAL,
    PRIMARY KEY (ticker, date)
);

-- BaFin (Germany) Art. 19 MAR "Directors' Dealings" -- the German equivalent of
-- SEC Form 4. See bafin.py. meldepflichtiger_id is BaFin's own ID for one specific
-- notification event (not a stable per-person ID -- the same human/entity name
-- gets a fresh ID per disclosure), so it doubles as our dedup key.
CREATE TABLE IF NOT EXISTS bafin_purchases (
    meldepflichtiger_id TEXT NOT NULL,
    notifier_name       TEXT,
    position            TEXT,
    issuer_name         TEXT,
    issuer_bafin_id     TEXT,
    isin                TEXT,
    instrument_type     TEXT,
    txn_type            TEXT,   -- 'P' or 'S'
    txn_date            TEXT,   -- DD.MM.YYYY as BaFin gives it
    venue               TEXT,
    price_eur           REAL,
    volume_eur          REAL,
    source_url          TEXT,
    found_at            TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (meldepflichtiger_id, issuer_bafin_id, txn_date)
);

CREATE TABLE IF NOT EXISTS bafin_seen_ids (
    meldepflichtiger_id TEXT PRIMARY KEY
);

-- Oslo Børs / Euronext Oslo "Newsweb" Managers' Transactions -- the Norwegian
-- equivalent of SEC Form 4 / BaFin Directors' Dealings. See norway.py. Unlike
-- BaFin's meldepflichtiger_id (needs a composite key), Newsweb's message_id is
-- already a globally-unique per-notification ID, so it's a sufficient dedup key
-- on its own. txn_date is stored as ISO YYYY-MM-DD (from publishedTime), unlike
-- SEC/House/BaFin's own native formats -- already display-ready via datefmt.fmt().
-- currency is usually 'NOK' but occasionally 'SEK'/'EUR'/'USD'/'DKK' for
-- cross-listed issuers; value is shares*price in that native currency and is
-- stored unconverted. Conversion to EUR happens only when a signal is built or
-- a threshold compared (see fx.py), never on the way into this table.
CREATE TABLE IF NOT EXISTS norway_purchases (
    message_id      INTEGER PRIMARY KEY,
    person          TEXT,
    issuer_name     TEXT,
    ticker          TEXT,
    txn_type        TEXT,   -- 'P' or 'S'
    txn_date        TEXT,   -- ISO YYYY-MM-DD
    shares          REAL,
    price           REAL,
    currency        TEXT,
    value           REAL,
    source_url      TEXT,
    found_at        TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS norway_seen_ids (
    message_id INTEGER PRIMARY KEY
);
-- Every signal, recorded as it was sent, with the features it had at that moment.
--
-- backtest.py needs this. Without it, measuring whether signals worked means
-- reconstructing them from purchase rows using today's thresholds and today's
-- code -- which silently rewrites history every time a constant is tuned, and
-- makes the measurement agree with whatever the current settings are. A signal is
-- an event; this is the event log.
CREATE TABLE IF NOT EXISTS signal_journal (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    emitted_at      TEXT DEFAULT (datetime('now')),
    source          TEXT,     -- SEC / HOUSE / SENATE / BAFIN / NORWAY / SWEDEN / SEC13DG
    kind            TEXT,     -- 'cluster' | 'solo' | 'exit' | 'stake'
    ticker          TEXT,
    company         TEXT,
    -- Features as they were when the signal fired.
    buyer_count     INTEGER,
    total_value_eur REAL,
    holder_only     INTEGER,  -- nobody in the cluster was an officer or director
    has_officer     INTEGER,
    position_increase_pct REAL,  -- biggest buyer's purchase as %% of what they already held
    first_buy       INTEGER,  -- someone bought this ticker for the first time in FIRST_BUY_YEARS
    lag_days        REAL,     -- trade date -> disclosure date
    market_cap_eur  REAL,
    value_pct_of_mcap REAL,
    percent_of_class  REAL,   -- 13D/G stakes only
    score           REAL,
    window_start    TEXT,
    window_end      TEXT,
    members         TEXT,     -- JSON array of names
    corroborated_by TEXT      -- JSON array of other source values active on this ticker within the window
);

CREATE INDEX IF NOT EXISTS signal_journal_ticker ON signal_journal(ticker, emitted_at);

-- One row per (ticker, day): every opinion.score() computed, so there's a real
-- record to check months from now -- same reasoning as signal_journal, applied
-- to opinion.py (which didn't exist historically, so there is nothing to
-- retroactively backtest it against; this starts the clock instead). Deduped
-- by day on purpose: re-checking the same ticker three times in one afternoon
-- (dev testing, or someone just asking again) must not inflate n -- what
-- matters for a later evaluation is distinct (ticker, day) observations, not
-- how many times someone happened to ask.
-- Tickers waiting for the scheduled headless-Claude pass (run_claude_analysis.sh)
-- to read their news and add a qualitative layer on top of opinion.py's
-- deterministic score. telegram_bot.py enqueues here right after sending its
-- own fast reply, so the news-reasoning layer follows a bit later rather than
-- blocking the immediate answer -- there is no live-Claude hook inside the
-- unattended bot process itself, see telegram_bot.py's module docstring.
CREATE TABLE IF NOT EXISTS claude_analysis_queue (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker       TEXT NOT NULL,
    requested_at TEXT DEFAULT (datetime('now')),
    processed_at TEXT
);

CREATE TABLE IF NOT EXISTS opinion_journal (
    ticker      TEXT NOT NULL,
    date        TEXT NOT NULL,   -- ISO date
    score       REAL,
    label       TEXT,
    factors     TEXT,            -- JSON [[points, note], ...], as shown at the time
    computed_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (ticker, date)
);

-- Shares outstanding / market cap / listing venue per ticker. Cached because it
-- moves slowly and every signal needs it: EUR 500,000 is a controlling interest in a
-- shell company and a rounding error in a mega-cap, and until now the bot ranked
-- both identically.
CREATE TABLE IF NOT EXISTS company_facts (
    ticker             TEXT PRIMARY KEY,
    shares_outstanding REAL,
    market_cap         REAL,
    currency           TEXT,
    exchange           TEXT,
    fetched_at         TEXT
);

-- US Senate Periodic Transaction Reports (see senate_efd.py). The Senate half of
-- the same STOCK Act disclosures house_purchases holds for the House: same amount
-- brackets, same filing deadline, different delivery system entirely. Columns
-- mirror house_purchases so the cluster/exit logic can be shared, with one
-- deliberate difference -- txn_date is stored as ISO here rather than the House's
-- M/D/YYYY, which sorts and compares correctly as plain text.
--
-- Like that table, this holds every transaction type, not only purchases.
CREATE TABLE IF NOT EXISTS senate_purchases (
    report_id     TEXT NOT NULL,
    member_name   TEXT,
    office        TEXT,
    owner         TEXT,
    asset         TEXT,
    ticker        TEXT,
    txn_type      TEXT,
    txn_date      TEXT,   -- ISO YYYY-MM-DD
    amount_range  TEXT,
    source_url    TEXT,
    found_at      TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (report_id, asset, txn_date, amount_range)
);

CREATE TABLE IF NOT EXISTS senate_seen_report_ids (
    report_id TEXT PRIMARY KEY
);

-- Schedule 13D/13G beneficial-ownership positions (see sec_13dg.py). One row per
-- reporting person per filing: a single 13D routinely covers several affiliated
-- entities holding the same block. percent_of_class is the reason this table
-- exists -- it is the "% of the company" figure cluster.py's flat money threshold
-- was standing in for.
CREATE TABLE IF NOT EXISTS sec_stakes (
    accession        TEXT NOT NULL,
    form_type        TEXT,     -- 'SCHEDULE 13D' / '13D/A' / '13G' / '13G/A'
    issuer_name      TEXT,
    issuer_cik       TEXT,
    cusip            TEXT,
    ticker           TEXT,     -- resolved from issuer_cik via cik_map.py; these forms carry none
    event_date       TEXT,     -- ISO
    person_name      TEXT,
    person_type      TEXT,
    percent_of_class REAL,
    amount_owned     REAL,
    source_url       TEXT,
    found_at         TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (accession, person_name)
);

CREATE TABLE IF NOT EXISTS sec_stake_seen_accessions (
    accession TEXT PRIMARY KEY
);

-- Form 144: notice of an INTENDED sale, filed before the sale and so before the
-- Form 4 that records it (see sec_144.py). Used as an early exit indicator, not as
-- a completed transaction -- filers often sell less than noticed, or not at all.
CREATE TABLE IF NOT EXISTS sec_proposed_sales (
    accession          TEXT NOT NULL,
    issuer_name        TEXT,
    issuer_cik         TEXT,
    ticker             TEXT,
    person_name        TEXT,
    relationship       TEXT,
    security_class     TEXT,
    units_to_sell      REAL,
    market_value       REAL,   -- USD, as filed
    units_outstanding  REAL,
    approx_sale_date   TEXT,   -- ISO
    exchange           TEXT,
    acquisition_nature TEXT,   -- e.g. 'Restricted Stock Vesting', 'Exercise of Stock Option'
    payment_nature     TEXT,
    source_url         TEXT,
    found_at           TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (accession, security_class)
);

CREATE TABLE IF NOT EXISTS sec_144_seen_accessions (
    accession TEXT PRIMARY KEY
);

-- Finansinspektionen's Swedish insider register (marknadssok.fi.se) -- the Swedish
-- equivalent of Form 4 / Directors' Dealings / Managers' Transactions. See
-- sweden.py. Keyed like BaFin on ISIN rather than a ticker (Sweden discloses no
-- ticker), and like the other non-US sources the value stays in its native
-- currency here; conversion to EUR happens only when a signal is built.
--
-- Two columns exist only in this source and are worth keeping: share_program (the
-- transaction came out of a share-incentive plan, i.e. it is compensation rather
-- than a decision to buy) and status ("Aktuell" is the live version of a filing,
-- "Reviderad" a corrected one). Both are excluded from signals downstream, but
-- stored so the raw log still matches the register exactly.
CREATE TABLE IF NOT EXISTS sweden_purchases (
    -- Keyed on the transaction, NOT on the export row: FI republishes a corrected
    -- filing in full and does not reliably retire the original (both versions can
    -- come back marked "Aktuell"), so row-keyed storage double-counts. The latest
    -- publication replaces the earlier one -- see sweden.txn_key.
    txn_key         TEXT PRIMARY KEY,
    row_id          TEXT,
    published       TEXT,
    person          TEXT,
    pdmr            TEXT,   -- the person in a leading position, when the notifier is a related entity
    position        TEXT,
    issuer_name     TEXT,
    isin            TEXT,
    instrument_type TEXT,
    txn_type        TEXT,   -- 'P' / 'S' / an untranslated Swedish label for everything else
    txn_date        TEXT,   -- ISO YYYY-MM-DD
    shares          REAL,
    price           REAL,
    currency        TEXT,
    value           REAL,
    share_program   INTEGER,
    related_party   INTEGER,
    status          TEXT,
    source_url      TEXT,
    found_at        TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sweden_seen_ids (
    row_id TEXT PRIMARY KEY
);

-- Which calendar days have been fully scanned for a date-indexed source (SEC's
-- daily index, Newsweb's date-range list). Without this the bot scans a blind
-- trailing window of N days, so a single missed run -- a failed job, a long
-- weekend, a laptop asleep at 08:00 -- loses those days permanently. With it,
-- each run scans whatever days in its lookback aren't recorded here yet, so a
-- gap backfills itself on the next successful run.
CREATE TABLE IF NOT EXISTS scanned_days (
    source      TEXT NOT NULL,   -- 'SEC' or 'NORWAY'
    date        TEXT NOT NULL,   -- ISO YYYY-MM-DD
    filings     INTEGER,         -- how many index entries that day had (0 = weekend/holiday)
    scanned_at  TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (source, date)
);

-- Company balance-sheet crypto trades from 8-K/6-K prose (crypto_treasury.py). Keyed
-- by the trade rather than the document: one accession routinely states the same
-- trade in both the 8-K body and its press-release exhibit.
CREATE TABLE IF NOT EXISTS crypto_treasury_txns (
    accession      TEXT NOT NULL,
    company        TEXT,
    ticker         TEXT,          -- the company's own ticker (MSTR), not the coin's
    cik            TEXT,
    coin           TEXT NOT NULL, -- BTC / ETH
    side           TEXT NOT NULL, -- P / S
    units          REAL NOT NULL,
    avg_price_usd  REAL,
    total_usd      REAL,
    filed_date     TEXT,          -- ISO
    form           TEXT,
    source_url     TEXT,
    found_at       TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (accession, coin, side, units)
);

CREATE TABLE IF NOT EXISTS crypto_treasury_seen (
    doc_id TEXT PRIMARY KEY      -- EFTS "_id": accession:filename
);

-- Daily issuer-published share count and NAV per spot ETF (crypto_etf.py). Flow is
-- the change in shares between two snapshots, valued at the later NAV.
CREATE TABLE IF NOT EXISTS crypto_etf_snapshots (
    fund                TEXT NOT NULL,
    coin                TEXT NOT NULL,
    as_of               TEXT NOT NULL,   -- ISO, the issuer's own date stamp
    shares_outstanding  REAL NOT NULL,
    nav_usd             REAL NOT NULL,
    found_at            TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (fund, as_of)
);

-- Oslo ticker -> ISIN, from Euronext's instrument search (norway.cached_isin).
-- isin '' = Euronext has no Oslo listing with that symbol (re-checked after a week).
CREATE TABLE IF NOT EXISTS oslo_isins (
    ticker      TEXT PRIMARY KEY,
    isin        TEXT NOT NULL,
    fetched_at  TEXT NOT NULL
);

-- Trading 212's tradable instruments (trading212.py), replaced wholesale on each
-- daily refresh.
CREATE TABLE IF NOT EXISTS t212_instruments (
    ticker      TEXT PRIMARY KEY,    -- Trading 212's own id: AAPL_US_EQ
    isin        TEXT,
    type        TEXT,                -- STOCK / ETF / ...
    short_name  TEXT,
    currency    TEXT
);

-- Exchange wallet balances, one row per wallet per run (crypto_onchain.py).
CREATE TABLE IF NOT EXISTS crypto_wallet_snapshots (
    coin        TEXT NOT NULL,
    address     TEXT NOT NULL,
    label       TEXT,
    balance     REAL NOT NULL,
    taken_at    TEXT NOT NULL,           -- ISO datetime
    PRIMARY KEY (address, taken_at)
);

-- Positions the user reports via Telegram (/bought, /sold) -- positions.py. The bot
-- never reads the brokerage account; this is only what the user tells it.
CREATE TABLE IF NOT EXISTS positions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker            TEXT NOT NULL,
    source            TEXT,
    opened_at         TEXT NOT NULL,              -- ISO date
    entry_price       REAL NOT NULL,
    insiders          TEXT NOT NULL DEFAULT '[]', -- JSON names whose selling closes it
    signal_id         INTEGER,                    -- signal_journal row it came from
    closed_at         TEXT,
    close_reason      TEXT,
    close_alerted_at  TEXT
);

-- The ~250 largest coins by market cap (sources.cached_coins), refreshed daily:
-- how the resolver recognises "PEPE" as a coin.
CREATE TABLE IF NOT EXISTS coin_list (
    symbol   TEXT PRIMARY KEY,
    coin_id  TEXT,
    name     TEXT,
    rank     INTEGER
);

-- Daily closes for the outlook universes (outlook.py), keyed by Yahoo symbol.
CREATE TABLE IF NOT EXISTS price_bars (
    symbol  TEXT NOT NULL,
    date    TEXT NOT NULL,
    close   REAL NOT NULL,
    PRIMARY KEY (symbol, date)
);

-- One row per situation per table ("stock" / "crypto"): how often the price was
-- higher a month later, and the walk-forward check of that number.
CREATE TABLE IF NOT EXISTS outlook_table (
    table_name      TEXT NOT NULL,
    situation       TEXT NOT NULL,
    n               INTEGER NOT NULL,
    up              INTEGER NOT NULL,
    base_rate       REAL,
    oos_n           INTEGER NOT NULL,
    oos_brier_s     REAL,
    oos_brier_base  REAL,
    oos_folds       INTEGER,        -- test years with this situation in them
    oos_fold_wins   INTEGER,        -- of those, years it beat the base rate's Brier
    assets          INTEGER,        -- how many assets the table was counted from
    built_at        TEXT NOT NULL,
    PRIMARY KEY (table_name, situation)
);
"""


# Columns added after a table's first release. CREATE TABLE IF NOT EXISTS in SCHEMA
# above only ever creates a *missing* table -- it silently does nothing for one that
# already exists, so a new column on an existing table needs an entry here to reach
# databases created before it. (table, column, type declaration)
_ADDED_COLUMNS = [
    ("cluster_alert_state", "last_total_value", "REAL"),
    # Fields Form 4 has always carried and this project used to discard. See
    # sec_edgar.InsiderPurchase for what each one is for.
    ("sec_purchases", "is_10b5_1", "INTEGER"),
    ("sec_purchases", "shares_owned_after", "REAL"),
    ("sec_purchases", "ownership_type", "TEXT"),
    ("sec_purchases", "filed_date", "TEXT"),
    ("sec_sales", "is_10b5_1", "INTEGER"),
    ("sec_sales", "filed_date", "TEXT"),
    ("company_facts", "avg_daily_value", "REAL"),
    ("signal_journal", "corroborated_by", "TEXT"),
    ("signal_journal", "tier", "TEXT"),
    ("outlook_table", "assets", "INTEGER"),
    ("outlook_table", "oos_folds", "INTEGER"),
    ("outlook_table", "oos_fold_wins", "INTEGER"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, decl in _ADDED_COLUMNS:
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if existing and column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def sec_seen_accessions(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT accession FROM sec_seen_accessions")}


def mark_sec_accession_seen(conn: sqlite3.Connection, accession: str) -> None:
    conn.execute("INSERT OR IGNORE INTO sec_seen_accessions(accession) VALUES (?)", (accession,))


def save_sec_purchase(conn: sqlite3.Connection, p) -> bool:
    """Returns True if this was a new row (not a duplicate)."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO sec_purchases
           (accession, issuer_name, issuer_cik, ticker, owner_name, owner_cik,
            is_officer, is_director, is_ten_pct_owner, officer_title, transaction_date,
            shares, price, value, security_title, derivative, source_url,
            is_10b5_1, shares_owned_after, ownership_type, filed_date)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            p.accession, p.issuer_name, p.issuer_cik, p.ticker, p.owner_name, p.owner_cik,
            int(p.is_officer), int(p.is_director), int(p.is_ten_pct_owner), p.officer_title,
            p.transaction_date, p.shares, p.price, p.value, p.security_title,
            int(p.derivative), p.source_url,
            int(getattr(p, "is_10b5_1", False)), getattr(p, "shares_owned_after", None),
            getattr(p, "ownership_type", None), getattr(p, "filed_date", ""),
        ),
    )
    return cur.rowcount > 0


def save_sec_sale(conn: sqlite3.Connection, p) -> bool:
    """Returns True if this was a new row (not a duplicate)."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO sec_sales
           (accession, issuer_name, issuer_cik, ticker, owner_name, owner_cik,
            is_director, officer_title, transaction_date, shares, price, value, source_url,
            is_10b5_1, filed_date)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            p.accession, p.issuer_name, p.issuer_cik, p.ticker, p.owner_name, p.owner_cik,
            int(p.is_director), p.officer_title,
            p.transaction_date, p.shares, p.price, p.value, p.source_url,
            int(getattr(p, "is_10b5_1", False)), getattr(p, "filed_date", ""),
        ),
    )
    return cur.rowcount > 0


def house_seen_doc_ids(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT doc_id FROM house_seen_doc_ids")}


def mark_house_doc_seen(conn: sqlite3.Connection, doc_id: str) -> None:
    conn.execute("INSERT OR IGNORE INTO house_seen_doc_ids(doc_id) VALUES (?)", (doc_id,))


def get_price_on(conn: sqlite3.Connection, ticker: str, date: str):
    """Returns cached (price_or_None, True) if we've looked this up before, else
    (None, False) -- the two-tuple distinguishes "cached as unavailable" from
    "never looked up", since a lookup can legitimately fail (delisted, bad ticker)."""
    row = conn.execute(
        "SELECT price FROM price_history_cache WHERE ticker = ? AND date = ?", (ticker, date)
    ).fetchone()
    if row is None:
        return None, False
    return row[0], True


def save_price_on(conn: sqlite3.Connection, ticker: str, date: str, price: float | None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO price_history_cache (ticker, date, price) VALUES (?,?,?)",
        (ticker, date, price),
    )
    conn.commit()


def get_insider_score(conn: sqlite3.Connection, owner_cik: str, max_age_seconds: float):
    """Returns (return_pct, market_return_pct, num_purchases) if cached and fresh, else None."""
    row = conn.execute(
        "SELECT return_pct, market_return_pct, num_purchases, computed_at FROM insider_scores WHERE owner_cik = ?",
        (owner_cik,),
    ).fetchone()
    if not row:
        return None
    import datetime as _dt
    computed_at = _dt.datetime.fromisoformat(row[3])
    if (_dt.datetime.now() - computed_at).total_seconds() > max_age_seconds:
        return None
    return row[0], row[1], row[2]


def save_insider_score(conn: sqlite3.Connection, owner_cik: str, owner_name: str,
                        return_pct: float, market_return_pct: float, num_purchases: int) -> None:
    import datetime as _dt
    conn.execute(
        """INSERT INTO insider_scores (owner_cik, owner_name, return_pct, market_return_pct, num_purchases, computed_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(owner_cik) DO UPDATE SET
             owner_name=excluded.owner_name, return_pct=excluded.return_pct,
             market_return_pct=excluded.market_return_pct, num_purchases=excluded.num_purchases,
             computed_at=excluded.computed_at""",
        (owner_cik, owner_name, return_pct, market_return_pct, num_purchases, _dt.datetime.now().isoformat()),
    )
    conn.commit()


def get_cached_value(conn: sqlite3.Connection, key: str, max_age_seconds: float):
    row = conn.execute("SELECT value, computed_at FROM kv_cache WHERE key = ?", (key,)).fetchone()
    if not row:
        return None
    import datetime as _dt
    computed_at = _dt.datetime.fromisoformat(row[1])
    if (_dt.datetime.now() - computed_at).total_seconds() > max_age_seconds:
        return None
    return row[0]


def save_cached_value(conn: sqlite3.Connection, key: str, value: float) -> None:
    import datetime as _dt
    conn.execute(
        """INSERT INTO kv_cache (key, value, computed_at) VALUES (?,?,?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value, computed_at=excluded.computed_at""",
        (key, value, _dt.datetime.now().isoformat()),
    )
    conn.commit()


def get_alert_state(conn: sqlite3.Connection, source: str, ticker: str) -> dict | None:
    """What was last alerted for this (source, ticker), or None if never alerted.

    Returns count, the *set* of member names, and the total value. Callers need all
    three: headcount alone can't tell a grown cluster from a completely different
    one, because the cluster window is rolling -- old buyers age out of it, so a
    second, unrelated cluster months later can have the same or a smaller count and
    would otherwise be suppressed forever. See cluster.should_alert.
    """
    row = conn.execute(
        "SELECT last_count, last_members, last_total_value FROM cluster_alert_state "
        "WHERE source = ? AND ticker = ?",
        (source, ticker),
    ).fetchone()
    if not row:
        return None
    count, members_raw, total_value = row
    return {"count": count or 0, "members": _decode_members(members_raw, count),
            "total_value": total_value}


def _decode_members(raw: str | None, count: int | None) -> set[str]:
    """Read back a last_members value, tolerating the pre-JSON comma-joined format.

    Names routinely contain commas -- "CASCADE INVESTMENT, L.L.C.",
    "PAINE SCHWARTZ FOOD CHAIN FUND V GP, LTD." -- so comma-joining was lossy: one
    name came back as two, the set comparison never matched, and the signal would
    have re-fired on every single run forever. New rows are JSON. Legacy rows fall
    back to splitting, except where last_count is 1, which settles the ambiguity:
    the whole string is one name, commas and all.
    """
    if not raw:
        return set()
    try:
        decoded = json.loads(raw)
        if isinstance(decoded, list):
            return {str(m) for m in decoded}
    except (ValueError, TypeError):
        pass
    if count == 1:
        return {raw.strip()}
    return {m.strip() for m in raw.split(",") if m.strip()}


def save_cluster_alert_state(conn: sqlite3.Connection, source: str, ticker: str,
                              count: int, members: list[str], total_value: float | None = None) -> None:
    """Records what was just alerted. Members are stored as a JSON array and read
    back by get_alert_state, so they must be plain names -- not formatted display
    lines -- and may safely contain commas."""
    import datetime as _dt
    conn.execute(
        """INSERT INTO cluster_alert_state
             (source, ticker, last_count, last_members, last_total_value, last_alert_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(source, ticker) DO UPDATE SET
             last_count=excluded.last_count, last_members=excluded.last_members,
             last_total_value=excluded.last_total_value,
             last_alert_at=excluded.last_alert_at""",
        (source, ticker, count, json.dumps(list(members), ensure_ascii=False), total_value,
         _dt.datetime.now().isoformat()),
    )
    conn.commit()


def scanned_days(conn: sqlite3.Connection, source: str) -> set[str]:
    """ISO dates already fully scanned for this source (see the scanned_days table)."""
    return {r[0] for r in conn.execute("SELECT date FROM scanned_days WHERE source = ?", (source,))}


def mark_day_scanned(conn: sqlite3.Connection, source: str, date: str, filings: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO scanned_days (source, date, filings) VALUES (?,?,?)",
        (source, date, filings),
    )
    conn.commit()


def save_house_purchase(conn: sqlite3.Connection, t) -> bool:
    cur = conn.execute(
        """INSERT OR IGNORE INTO house_purchases
           (doc_id, member_name, state_district, owner, asset, ticker, txn_type,
            txn_date, notification_date, amount_range, source_url)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            t.doc_id, t.member_name, t.state_district, t.owner, t.asset, t.ticker,
            t.txn_type, t.txn_date, t.notification_date, t.amount_range, t.source_url,
        ),
    )
    return cur.rowcount > 0


def bafin_seen_ids(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT meldepflichtiger_id FROM bafin_seen_ids")}


def mark_bafin_id_seen(conn: sqlite3.Connection, meldepflichtiger_id: str) -> None:
    conn.execute("INSERT OR IGNORE INTO bafin_seen_ids(meldepflichtiger_id) VALUES (?)", (meldepflichtiger_id,))


def save_bafin_purchase(conn: sqlite3.Connection, f, detail, source_url: str) -> bool:
    cur = conn.execute(
        """INSERT OR IGNORE INTO bafin_purchases
           (meldepflichtiger_id, notifier_name, position, issuer_name, issuer_bafin_id,
            isin, instrument_type, txn_type, txn_date, venue, price_eur, volume_eur, source_url)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            f.meldepflichtiger_id, f.notifier_name, f.position, f.issuer_name, f.issuer_bafin_id,
            f.isin, f.instrument_type, f.txn_type, f.txn_date, f.venue,
            detail.price_eur, detail.volume_eur, source_url,
        ),
    )
    return cur.rowcount > 0


def norway_seen_ids(conn: sqlite3.Connection) -> set[int]:
    return {r[0] for r in conn.execute("SELECT message_id FROM norway_seen_ids")}


def mark_norway_id_seen(conn: sqlite3.Connection, message_id: int) -> None:
    conn.execute("INSERT OR IGNORE INTO norway_seen_ids(message_id) VALUES (?)", (message_id,))


def journal_signal(conn: sqlite3.Connection, row: dict) -> None:
    """Append one signal to signal_journal, exactly as it was sent."""
    cols = ("source", "kind", "ticker", "company", "buyer_count", "total_value_eur",
            "holder_only", "has_officer", "position_increase_pct", "first_buy",
            "lag_days", "market_cap_eur", "value_pct_of_mcap", "percent_of_class",
            "score", "window_start", "window_end", "members", "corroborated_by", "tier")
    conn.execute(
        f"INSERT INTO signal_journal ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        tuple(row.get(c) for c in cols),
    )
    conn.commit()


def enqueue_analysis(conn: sqlite3.Connection, ticker: str) -> None:
    """Queue `ticker` for the next scheduled headless-Claude pass. Deduped
    against any already-pending (unprocessed) row for the same ticker, so
    asking about the same name several times before the schedule next fires
    doesn't queue redundant work."""
    exists = conn.execute(
        "SELECT 1 FROM claude_analysis_queue WHERE ticker = ? AND processed_at IS NULL",
        (ticker,),
    ).fetchone()
    if exists:
        return
    conn.execute("INSERT INTO claude_analysis_queue (ticker) VALUES (?)", (ticker,))
    conn.commit()


def pending_analysis(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    """(id, ticker) pairs not yet processed, oldest first."""
    return conn.execute(
        "SELECT id, ticker FROM claude_analysis_queue WHERE processed_at IS NULL "
        "ORDER BY requested_at"
    ).fetchall()


def mark_analysis_processed(conn: sqlite3.Connection, queue_id: int) -> None:
    conn.execute(
        "UPDATE claude_analysis_queue SET processed_at = datetime('now') WHERE id = ?",
        (queue_id,),
    )
    conn.commit()


def journal_opinion(conn: sqlite3.Connection, ticker: str, op: dict) -> None:
    """Record today's opinion.score() for `ticker` -- one row per (ticker, day),
    a later computation the same day replaces the earlier one (REPLACE, not
    IGNORE: the last opinion computed that day is the more complete/current one,
    e.g. if data was briefly unavailable on an earlier check)."""
    import datetime as _dt
    import json
    conn.execute(
        """INSERT OR REPLACE INTO opinion_journal (ticker, date, score, label, factors)
           VALUES (?,?,?,?,?)""",
        (ticker, _dt.date.today().isoformat(), op["score"], op["label"],
         json.dumps(op["factors"], ensure_ascii=False)),
    )
    conn.commit()


def get_company_facts(conn: sqlite3.Connection, ticker: str, max_age_seconds: float):
    row = conn.execute(
        "SELECT shares_outstanding, market_cap, currency, exchange, fetched_at, avg_daily_value "
        "FROM company_facts WHERE ticker = ?", (ticker,)
    ).fetchone()
    if not row:
        return None
    import datetime as _dt
    try:
        fetched = _dt.datetime.fromisoformat(row[4])
    except (TypeError, ValueError):
        return None
    if (_dt.datetime.now() - fetched).total_seconds() > max_age_seconds:
        return None
    return {"shares_outstanding": row[0], "market_cap": row[1], "currency": row[2],
            "exchange": row[3], "avg_daily_value": row[5]}


def save_company_facts(conn: sqlite3.Connection, ticker: str, facts: dict) -> None:
    import datetime as _dt
    conn.execute(
        """INSERT INTO company_facts
             (ticker, shares_outstanding, market_cap, currency, exchange, fetched_at,
              avg_daily_value)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(ticker) DO UPDATE SET
             shares_outstanding=excluded.shares_outstanding, market_cap=excluded.market_cap,
             currency=excluded.currency, exchange=excluded.exchange,
             fetched_at=excluded.fetched_at, avg_daily_value=excluded.avg_daily_value""",
        (ticker, facts.get("shares_outstanding"), facts.get("market_cap"),
         facts.get("currency"), facts.get("exchange"), _dt.datetime.now().isoformat(),
         facts.get("avg_daily_value")),
    )
    conn.commit()


def senate_seen_report_ids(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT report_id FROM senate_seen_report_ids")}


def mark_senate_report_seen(conn: sqlite3.Connection, report_id: str) -> None:
    conn.execute("INSERT OR IGNORE INTO senate_seen_report_ids(report_id) VALUES (?)", (report_id,))


def save_senate_purchase(conn: sqlite3.Connection, t) -> bool:
    cur = conn.execute(
        """INSERT OR IGNORE INTO senate_purchases
           (report_id, member_name, office, owner, asset, ticker, txn_type, txn_date,
            amount_range, source_url)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (t.report_id, t.member_name, t.office, t.owner, t.asset, t.ticker, t.txn_type,
         t.txn_date, t.amount_range, t.source_url),
    )
    return cur.rowcount > 0


def sec_stake_seen_accessions(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT accession FROM sec_stake_seen_accessions")}


def mark_sec_stake_seen(conn: sqlite3.Connection, accession: str) -> None:
    conn.execute("INSERT OR IGNORE INTO sec_stake_seen_accessions(accession) VALUES (?)", (accession,))


def save_sec_stake(conn: sqlite3.Connection, f) -> bool:
    cur = conn.execute(
        """INSERT OR IGNORE INTO sec_stakes
           (accession, form_type, issuer_name, issuer_cik, cusip, ticker, event_date,
            person_name, person_type, percent_of_class, amount_owned, source_url)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f.accession, f.form_type, f.issuer_name, f.issuer_cik, f.cusip, f.ticker,
         f.event_date, f.person_name, f.person_type, f.percent_of_class, f.amount_owned,
         f.source_url),
    )
    return cur.rowcount > 0


def sec_144_seen_accessions(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT accession FROM sec_144_seen_accessions")}


def mark_sec_144_seen(conn: sqlite3.Connection, accession: str) -> None:
    conn.execute("INSERT OR IGNORE INTO sec_144_seen_accessions(accession) VALUES (?)", (accession,))


def save_sec_proposed_sale(conn: sqlite3.Connection, s) -> bool:
    cur = conn.execute(
        """INSERT OR IGNORE INTO sec_proposed_sales
           (accession, issuer_name, issuer_cik, ticker, person_name, relationship,
            security_class, units_to_sell, market_value, units_outstanding,
            approx_sale_date, exchange, acquisition_nature, payment_nature, source_url)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (s.accession, s.issuer_name, s.issuer_cik, s.ticker, s.person_name, s.relationship,
         s.security_class, s.units_to_sell, s.market_value, s.units_outstanding,
         s.approx_sale_date, s.exchange, s.acquisition_nature, s.payment_nature, s.source_url),
    )
    return cur.rowcount > 0


def sweden_seen_ids(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT row_id FROM sweden_seen_ids")}


def mark_sweden_id_seen(conn: sqlite3.Connection, row_id: str) -> None:
    conn.execute("INSERT OR IGNORE INTO sweden_seen_ids(row_id) VALUES (?)", (row_id,))


def save_sweden_purchase(conn: sqlite3.Connection, t: dict) -> bool:
    """t is the dict produced by sweden.parse_row().

    Returns True only for a transaction never stored before. A later publication of
    the same transaction (FI republishes corrections in full) updates the existing
    row and returns False -- nothing new was disclosed, so it must not be counted or
    printed as a find.
    """
    existing = conn.execute(
        "SELECT published FROM sweden_purchases WHERE txn_key = ?", (t["txn_key"],)
    ).fetchone()
    conn.execute(
        """INSERT INTO sweden_purchases
           (txn_key, row_id, published, person, pdmr, position, issuer_name, isin,
            instrument_type, txn_type, txn_date, shares, price, currency, value,
            share_program, related_party, status, source_url)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(txn_key) DO UPDATE SET
             row_id=excluded.row_id, published=excluded.published,
             status=excluded.status, share_program=excluded.share_program,
             related_party=excluded.related_party, position=excluded.position
           WHERE excluded.published > sweden_purchases.published""",
        (
            t["txn_key"], t["row_id"], t["published"], t["person"], t["pdmr"], t["position"],
            t["issuer_name"], t["isin"], t["instrument_type"], t["txn_type"], t["txn_date"],
            t["shares"], t["price"], t["currency"], t["value"], int(t["share_program"]),
            int(t["related_party"]), t["status"], t["source_url"],
        ),
    )
    return existing is None


def save_norway_purchase(conn: sqlite3.Connection, t: dict) -> bool:
    """t is the plain dict yielded by norway.scan_new_filings() (with 'value' added
    by bot.py before calling this) -- not a dataclass, unlike the other sources."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO norway_purchases
           (message_id, person, issuer_name, ticker, txn_type, txn_date, shares,
            price, currency, value, source_url)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            t["message_id"], t["person"], t["issuer_name"], t["ticker"], t["txn_type"],
            t["txn_date"], t["shares"], t["price"], t["currency"], t["value"], t["source_url"],
        ),
    )
    return cur.rowcount > 0


def crypto_treasury_seen(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT doc_id FROM crypto_treasury_seen")}


def mark_crypto_treasury_seen(conn: sqlite3.Connection, doc_id: str) -> None:
    conn.execute("INSERT OR IGNORE INTO crypto_treasury_seen(doc_id) VALUES (?)", (doc_id,))


def save_crypto_treasury_txn(conn: sqlite3.Connection, t) -> bool:
    """t is a crypto_treasury.TreasuryTxn. Returns True if it was a new trade."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO crypto_treasury_txns
           (accession, company, ticker, cik, coin, side, units, avg_price_usd, total_usd,
            filed_date, form, source_url)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (t.accession, t.company, t.ticker, t.cik, t.coin, t.side, t.units,
         t.avg_price_usd, t.total_usd, t.filed_date, t.form, t.source_url),
    )
    return cur.rowcount > 0


def save_crypto_etf_snapshot(conn: sqlite3.Connection, s) -> bool:
    """s is a crypto_etf.Snapshot. Returns True if this (fund, as_of) was new."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO crypto_etf_snapshots
           (fund, coin, as_of, shares_outstanding, nav_usd) VALUES (?,?,?,?,?)""",
        (s.fund, s.coin, s.as_of, s.shares_outstanding, s.nav_usd),
    )
    return cur.rowcount > 0


def save_crypto_wallet_snapshot(conn: sqlite3.Connection, b, taken_at: str) -> None:
    """b is a crypto_onchain.WalletBalance."""
    conn.execute(
        """INSERT OR REPLACE INTO crypto_wallet_snapshots
           (coin, address, label, balance, taken_at) VALUES (?,?,?,?,?)""",
        (b.coin, b.address, b.label, b.balance, taken_at),
    )
