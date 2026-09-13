# Design: cross-source corroboration signal

Date: 2026-09-13
Status: approved for implementation planning

## Context

Each of this project's signal sources (SEC Form 4 insider clusters, SEC 13D/G
stakes, House/Senate PTR clusters, BaFin, Oslo Børs, Finansinspektionen) is found
and scored in isolation by `cluster.py`'s finders and `score_signal()`. Nothing
currently notices when a ticker draws signals from more than one *independent*
disclosure regime — e.g. an SEC insider cluster and a Senate PTR landing on the
same company within weeks of each other. That fact is visible today only if a
human manually scans `signal_journal` or a ticker's dossier and happens to notice
two different `source` values. This spec adds a small, purely mechanical
detection step that surfaces it explicitly, live in the daily alert and in the
on-demand ticker dossier.

Brainstormed and scoped with the user:

- Surfaces in **both** places: a live alert-time tag (bot.py, same-day
  convergence) and a dossier summary (research.py, full history).
- **Does** feed `score_signal`'s attention ranking, as one more additive
  component alongside the existing per-buyer/officer/freshness/market-cap terms
  — not kept separate the way TradingView's read is (that one is explicitly a
  third party's opinion; this one is the bot's own data).
- **30-day window**, a plain module constant (not a CLI flag — same footing as
  `SEC_WINDOW_DAYS`/`HOUSE_WINDOW_DAYS`, tunable later the way those were).
- **Any signal kind counts on either side**, including exits — a buy-side
  cluster and an exit signal on the same ticker both count as "another source
  had activity here," deliberately *not* framed as agreement (see Non-goals).

## Non-goals (safety boundary — binding)

- **No verdict, no direction claim.** "Corroboration" here means only "another
  independent disclosure regime recorded activity on this ticker within the
  window," never "these sources agree" or "this confirms a buy thesis." Because
  exits count on either side, a corroborating pair can be a buy and a sell —
  the wording used everywhere (alert line, dossier line) must read as
  co-occurrence, not agreement.
- **No new data source.** This reads only from `signal_journal` (already
  populated by every existing finder) and the in-memory signal list a single
  daily run already produces. No new scraping, no new API.
- **No AI/LLM calls.** Pure SQL + set logic, like every other scoring
  component in `cluster.py`.
- **The score bonus is explicitly an unvalidated starting point**, same
  standing as every other weight in `score_signal` — its docstring already
  says as much for the existing components, and this one is added under the
  same disclaimer, not a special case.
- **`SEC` and `SEC13DG` are treated as independent sources, even though they
  sometimes aren't.** `find_corroboration` counts a Form 4 insider cluster
  (`SEC`) and a 13D/G stake filing (`SEC13DG`) on the same ticker as two
  corroborating regimes. A >10%-holder's position change can genuinely
  generate both from the same underlying event, so this specific pair is not
  always truly independent corroboration. Known and accepted for this feature
  — flagged by the final whole-branch review and deliberately not changed
  here; collapsing the pair to one regime would alter scoring semantics and
  needs its own decision, not a silent fix.

## Architecture

```
cluster.py
├── CORROBORATION_WINDOW_DAYS = 30                       new constant
├── W_PER_CORROBORATING_SOURCE, CAP_CORROBORATION        new weight constants
├── ClusterSignal.corroborated_by: list[str] = []        new field
├── StakeSignal.corroborated_by: list[str] = []          new field
├── ExitSignal.corroborated_by: list[str] = []           new field
├── find_corroboration(conn, signals, window_days=...)   new — mutates signals in place
├── score_signal(sig, corroborated_by=None)               changed signature + logic
└── enrich_signals(conn, signals)                         changed: calls find_corroboration first

db.py
└── signal_journal gains a `corroborated_by TEXT` column (JSON list), via
    SCHEMA + _ADDED_COLUMNS (existing migration mechanism); journal_signal()'s
    hardcoded `cols` tuple gains the new column too -- adding a key to the row
    dict alone would silently never be written otherwise

bot.py
└── _signal_features(sig) includes "corroborated_by": json.dumps(...)

telegram_notify.py
└── format_signal / format_exit_signal / format_stake_signal each gain a
    corroboration line, same position/pattern as the existing market_note line

research.py
└── new corroboration_summary(conn, ticker, window_days=...) -> dict
└── build() gains rep["corroboration"]; format_report() renders a one-line
    summary in the existing "Сигналы, которые бот уже присылал" section
```

## Detection: `cluster.find_corroboration`

```python
def find_corroboration(conn, signals: list, window_days: int = CORROBORATION_WINDOW_DAYS) -> None:
```

For each distinct ticker present in `signals`, computes the set of `source`
values seen for that ticker from two places, unioned:

1. **This same run** — every other signal in `signals` (any kind: cluster,
   solo, stake, exit) that shares the ticker.
2. **History** — `SELECT DISTINCT source FROM signal_journal WHERE ticker = ?
   AND emitted_at >= date('now', '-{window_days} days')`, one query per
   distinct ticker in the batch (batches from a single daily run are small —
   dozens, not thousands — so this stays cheap; no N+1 concern at this scale,
   consistent with `marketcap.market_cap_eur`'s per-ticker calls in
   `enrich_signals` already).

For each signal, `sig.corroborated_by` is set to the **sorted list of sources
from that union, excluding the signal's own `.source`**. Empty list (the
default) when nothing else has fired on that ticker. Pure SQL + set logic, no
network — runs alongside the other pre-network steps in `enrich_signals`,
before the market-cap/liquidity enrichment loop that needs the network.

Self-matches are impossible by construction (a source is only ever excluded
from its own union, never compared against itself), and a source appearing
twice (e.g. two SEC insider clusters on the same ticker) collapses to one
entry in the set — corroboration counts *distinct source regimes*, not
duplicate signals from the same regime.

## Scoring: `score_signal`

Current structure has three early-return branches (`StakeSignal`, `ExitSignal`,
the general `ClusterSignal` path). Restructured to `if/elif/else` so there is
one shared tail:

```python
def score_signal(sig, corroborated_by: list[str] | None = None) -> float:
    if hasattr(sig, "percent"):
        score = ...                      # existing StakeSignal logic, unchanged
    elif hasattr(sig, "seller_count"):
        score = ...                      # existing ExitSignal logic, unchanged
    else:
        score = ...                      # existing ClusterSignal logic, unchanged
    if corroborated_by:
        score += min(W_PER_CORROBORATING_SOURCE * len(corroborated_by), CAP_CORROBORATION)
    return round(score, 1)
```

`corroborated_by` defaults to `None` so every existing caller and every
existing `test_scoring.py` test (which constructs signals directly and calls
`score_signal(sig)` with no second argument) keeps working unchanged — this is
purely additive. `score_signal`'s docstring already enumerates the reasoning
behind each component ("several unrelated insiders converging beats one...");
it gains one more sentence in the same voice — a disclosure regime
independently noticing the same ticker is more than that regime noticing it
twice.

Proposed starting weights, on the same scale as the existing ones
(`W_PER_EXTRA_BUYER=12`, `W_HAS_OFFICER=10`, `CAP_MARKET_CAP=40`,
`CAP_POSITION=24`):

```python
W_PER_CORROBORATING_SOURCE = 15.0   # per distinct other source active on this ticker
CAP_CORROBORATION = 30.0            # caps at 2 corroborating sources' worth
```

`enrich_signals(conn, signals)` calls `find_corroboration(conn, signals)`
before the scoring loop, then scores each signal with
`sig.score = score_signal(sig, sig.corroborated_by)`.

`test_every_component_is_bounded`'s existing extreme-input assertion
(`score < 250`) is unaffected since it never sets `corroborated_by`; a new
test covers the bounded case with corroboration included.

## Storage: `signal_journal`

New column, JSON-encoded list of source strings (same convention as the
existing `members` column):

```sql
corroborated_by TEXT   -- JSON array of other source values active on this ticker at emission time
```

Added to `SCHEMA`'s `CREATE TABLE IF NOT EXISTS signal_journal (...)` for
fresh databases, and to `_ADDED_COLUMNS` (`("signal_journal",
"corroborated_by", "TEXT")`) so the existing local `data/disclosures.db`
picks it up via `_migrate()` on next `db.connect()` — the same mechanism
already used for `is_10b5_1`, `avg_daily_value`, etc. Recorded **at emission
time** (inside `_signal_features`), not recomputed later — same rationale the
project already applies to every other journaled feature: `backtest.py`
measures the signal as it was actually sent, not as today's code would
re-derive it.

`journal_signal()`'s `cols` tuple (the hardcoded list of columns it actually
inserts, independent of whatever keys the row dict happens to have) also gains
`"corroborated_by"` — required, not automatic, since that tuple is what
decides which dict keys get written.

## Alert-time surface: `telegram_notify.py`

Each of the three formatters (`format_signal`, `format_exit_signal`,
`format_stake_signal`) already renders an optional `market_note` line in the
same position (right after the signal's own detail lines). A corroboration
line is added immediately before it, same pattern, but **not italicized** —
`market_note` is explicitly a third party's (TradingView's) read; this line is
the bot's own data, so it stays plain text like the rest of the signal body:

```python
corr = getattr(sig, "corroborated_by", None)
if corr:
    lines.append(f"   🔗 Другие источники по этому тикеру: {', '.join(corr)}")
```

The window is already baked into what `corroborated_by` contains by the time
it reaches this layer (`find_corroboration` only ever puts a source in the
list if it was active within the window) — the line doesn't restate the day
count, and `telegram_notify.py` gains no new import to do this (it only reads
an attribute already set on the object). Wording is deliberately "другие
источники" (other sources) + plain co-occurrence framing, never "подтверждают"
(confirm) or "согласны" (agree) — holds the same line the rest of the project
holds. Added identically to all three formatters so buy, stake, and exit
alerts all carry it when applicable.

## Dossier surface: `research.py`

New function, alongside the existing `past_signals`. `research.py` does not
currently import `cluster` (it reads `signal_journal` directly via raw SQL,
same as `past_signals` already does) — it gains `import cluster` for this one
constant, the same way it already imports `annual_report`/`tradingview` for a
handful of calls each, so the 30-day window stays defined in exactly one
place rather than duplicated as a literal in two files:

```python
def corroboration_summary(conn, ticker: str, window_days: int = cluster.CORROBORATION_WINDOW_DAYS) -> dict | None:
```

Queries `signal_journal` for the ticker's distinct `source` values, both
all-time and within the trailing `window_days`. Returns `None` when fewer than
2 distinct sources exist all-time (nothing to report). Otherwise:

```python
{"all_sources": ["BAFIN", "SEC", "SENATE"], "recent_sources": ["SEC", "SENATE"]}
```

`build()` gains `rep["corroboration"] = corroboration_summary(conn, ticker)`.
`format_report()` renders it as one line inside the existing "Сигналы, которые
бот уже присылал" section (only shown when the key is present):

```
Независимые источники по этому тикеру: BAFIN, SEC, SENATE
  (за последние 30 дней: SEC, SENATE)
```

The trailing-window sub-line is only shown when it differs from the all-time
set (i.e. when older sources have aged out) — otherwise the single line
suffices.

## Files changed

| file | change |
|---|---|
| `cluster.py` | new constants, `corroborated_by` field on all 3 signal dataclasses, new `find_corroboration()`, `score_signal()` signature + restructure, `enrich_signals()` calls the new function |
| `db.py` | `signal_journal` schema + `_ADDED_COLUMNS` entry |
| `bot.py` | `_signal_features()` includes `corroborated_by` |
| `telegram_notify.py` | `format_signal`, `format_exit_signal`, `format_stake_signal` each gain the corroboration line |
| `research.py` | new `import cluster`; new `corroboration_summary()`; `build()`/`format_report()` wire it in |
| `tests/test_scoring.py` | `score_signal` with `corroborated_by` — bonus applied, bounded, backward-compatible default |
| `tests/test_signals.py` | `find_corroboration` — same-batch match, journal-history match, window boundary, no self-match, dedup across repeated sources; `enrich_signals` end-to-end |
| `tests/test_telegram_notify.py` | corroboration line rendered/omitted in each of the 3 formatters |
| `tests/test_research.py` | `corroboration_summary` + dossier rendering, including the "fewer than 2 sources → None" and "recent == all-time → single line" cases |
| `README.md` | short bullet, tone matching the existing signal-scoring description |

No new third-party dependencies. One new intra-project import
(`research.py` → `cluster`, for the shared window constant).

## Testing

All offline (SQLite fixtures via `conftest.py`, no network) — matches the
project's existing convention.

- **`find_corroboration`**: two signals in the same batch, different sources,
  same ticker → each gets the other's source in `corroborated_by`; a
  same-source repeat collapses to one entry; a `signal_journal` row outside
  the window is excluded; one inside the window is included; a signal with no
  other activity gets `[]`; an `ExitSignal` and a `ClusterSignal` on the same
  ticker corroborate each other (exits count, per the design decision).
- **`score_signal`**: bonus applied when `corroborated_by` is non-empty and
  scales with (capped) length; unaffected when `None`/`[]` (backward
  compatibility with every existing call site); bounded under extreme inputs
  including a long `corroborated_by` list.
- **`enrich_signals`**: end-to-end — two signals from different sources on one
  ticker come out of `enrich_signals` with non-empty `corroborated_by` and a
  score reflecting the bonus.
- **Journal round-trip**: `_signal_features` → `db.journal_signal` →
  `corroboration_summary` reads it back correctly (extends the existing
  `test_journalled_signal_round_trips` pattern).
- **`telegram_notify`**: each formatter includes the line when
  `corroborated_by` is set, omits it when empty; wording never contains
  agreement language.
- **`corroboration_summary`**: `None` for 0 or 1 distinct sources; both keys
  populated for 2+; `recent_sources` correctly excludes an aged-out source.
- **No verdict language**: extend the project's existing forbidden-terms
  check (`"buy "`, `"sell "`, `"рекомендуем"`, etc.) to cover the new alert
  line and dossier line.

## Verification

```bash
cd ~/Desktop/disclosure-bot && source .venv/bin/activate
pytest tests/ -q                                    # all green, no regressions
python bot.py --once --no-telegram                   # confirms find_corroboration runs without crashing on real data
python -c "
import db, research
conn = db.connect('data/disclosures.db')
print(research.format_report(research.build(conn, 'AAPL')))
"                                                     # dossier renders (or omits) the corroboration line without error
```

Success: pytest green; a synthetic two-source scenario shows the corroboration
line in both the Telegram-format output and the dossier; a single-source
ticker shows neither; no existing test's expected score changes (the new term
is strictly additive and only activates when `corroborated_by` is passed).
