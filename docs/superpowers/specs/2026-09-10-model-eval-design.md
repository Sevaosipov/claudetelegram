# Design: predictive-skill evaluation (`model_eval.py`)

Date: 2026-09-10
Status: approved for implementation planning

## Context

`disclosure-bot` collects disclosed stock purchases from eight sources, ranks them
with a hand-tuned score (`cluster.score_signal`), and measures raw historical
outcomes by feature bucket (`backtest.py`). What it has never done is ask the
question directly: **do the features it records carry any out-of-sample predictive
skill?** Every threshold and weight in `cluster.py` was chosen by reasoning about
the domain and never checked against a held-out outcome.

The user asked for a forecast and a buy/sell verdict. Those were declined — a
per-ticker directional call handed to a person is personalized investment advice
regardless of framing. This is the research-legitimate version of the same
question: fit a model to predict "does this name beat the benchmark over the next
month", evaluate it with walk-forward cross-validation, and report its skill
honestly. The expected and valuable result, on this data, is "little or no edge" —
and knowing that is worth more than not knowing it.

## Non-goals (safety boundary — binding)

- **No live scoring.** `model_eval.py` never attaches a probability to a current or
  future signal, never writes to `signal_journal`, never touches `bot.py` or the
  Telegram digest. It reads the database and yfinance, prints a report, exits.
- **No persisted model.** The model is refit on every run for evaluation only. No
  pickle, no `predict(ticker)` entry point, no artifact that could be pointed at a
  live signal later.
- **Nothing in the project imports it.** `model_eval.py` imports *from* the project;
  no project module imports it. Enforced by a test.
- **No verdict, no forecast in the output.** The report states measured historical
  skill with sample sizes attached to every number. It does not say "buy", "sell",
  "will rise", or name a target price.

## Architecture

New standalone module `model_eval.py`, structured like `backtest.py` (which is also
standalone and network-touching, and stays separate for the same reason).

```
model_eval.py
├── data:      backtest.collect_political_trades()          [new, in backtest.py]
│              backtest.collect_purchases(..., extended_features=True)  [param added]
├── features:  build_feature_frame(rows, corpus)  -> (X: DataFrame, y: array, dates)
├── split:     walk_forward_folds(dates, n_folds, min_test) -> list[(train_idx, test_idx)]
├── model:     make_pipeline(corpus)  -> sklearn Pipeline  (×2: LR, GBT)
├── evaluate:  run_walk_forward(X, y, dates, ...) -> metrics dict
├── gate:      check_sufficiency(rows, n_folds, min_rows, min_test) -> str | None
├── report:    format_report(corpus, metrics | abort_msg) -> str
├── run:       run(conn, corpus, horizon, folds, ...) -> str   [orchestrator]
└── main:      CLI wrapper around run()
```

`run()` is the single orchestrator: collect → gate → (abort or evaluate) →
format. Both `main()` (CLI) and `menu.show_model_eval()` call it and print the
returned string. Nothing else is public.

Data collectors live in `backtest.py` (next to the existing `collect_purchases` /
`collect_signals`) so both tools share one definition of "a labeled disclosure
row". `model_eval.py` imports them. There is no separate `collect_insider_purchases`:
`backtest.collect_purchases` gains a keyword-only `extended_features=False`, and
`model_eval` passes `True` to get the model feature columns without slowing down
`backtest.py`'s own `--purchases` mode.

## Data & labels

One row per disclosed **purchase**. Entry point is the **disclosure date**, never
the trade date — the trade is not public until filed, and a model entering earlier
would be scored on a move nobody could act on. This matches
`backtest.collect_purchases`, which already enters on `filed_date`.

**Label:** `forward_returns(ticker, disclosure_date, [H])["excess"]` at horizon `H`
(default 21 trading days). `1` if `excess > 0`, else `0`. Rows where
`forward_returns` returns `None` (unresolvable ticker, or fewer than `H` trading
days of history after the disclosure date) are dropped — no label, not a zero.

### Corpus A — political trades (`collect_political_trades`)

Source: `house_purchases` + `senate_purchases`, `txn_type = 'P'`, ticker present and
not junk (reuse `cluster._JUNK_TICKER_SQL`), disclosure date present.

**Real volume (checked against the DB 2026-09-10):** ~240 House `P` rows with a
ticker and a `notification_date`, spanning **2025-02 to 2026-09**, from only **20
distinct members** — the PDF parser misses tickers on many filings and cannot read
scanned ones at all. Month density is lumpy: 24–32/month through mid-2025, then
2–7/month recently. So the political corpus **just barely** clears the gate with
the lowered defaults below (it does not clear the original `min_test=20`).

**Performance:** every row needs a forward price series to be labelled, so a
political run fetches a few hundred yfinance histories (many shared, all cached in
`backtest._SERIES_CACHE` for the process). Expect a minute or two cold.

**House only in practice.** `house_purchases.notification_date` is populated for
every row and is the disclosure date. `senate_purchases` has **no
disclosure-date column** (only `txn_date`) and is currently empty (eFD blocked), so
Senate rows are dropped until that scraper records a disclosure date. The `chamber`
feature stays in the design for when Senate data exists; until then it is a
constant column, which the one-hot encoder handles as a no-op.

Date handling: `house_purchases.txn_date` and `.notification_date` are `M/D/YYYY`.
Normalise to ISO (pattern already in `research.european_activity`).

Features (all knowable at the disclosure date `D`):

| feature | definition |
|---|---|
| `log_amount` | `log10(cluster.parse_amount_low(amount_range) + 1)` — bracket floor |
| `chamber` | `"house"` / `"senate"` (categorical) |
| `lag_days` | disclosure date − trade date, in days |
| `cluster_size` | distinct *other* members with a `P` in the same ticker whose disclosure date is within `[D − 30d, D]` — backward-looking window, no future disclosures. Build one `ticker -> [(disclosure_date, member)]` index for the corpus, not a query per row |
| `member_prior_hitrate` | that member's fraction of label=1 over their earlier `P` trades that were both (a) disclosed before `D` and (b) whose own `H`-day outcome window had closed by `D` — approximated as *earlier trade disclosed at least `2·H` calendar days before `D`*. Derived entirely from the already-computed in-memory label set — no extra price fetches. NaN when the member has no such history; median-imputed by the pipeline |
| `mcap_bucket` | `marketcap.size_bucket(marketcap.market_cap_eur(conn, ticker))` — categorical (`nano`…`mega`, `unknown`), one-hot encoded, not ordinal. Uses *current* market cap — see Known limitations |
| `price_vs_spy_63d` | ticker return over the 63 trading days **ending at** `D`, minus SPY over the same span. Needs a backward-looking helper (`_return_before(ticker, date, days)`) — `backtest._series` only fetches forward from a date |

### Corpus B — insider purchases

`backtest.collect_purchases(..., extended_features=True)`. `sec_purchases`, code
`P`, `derivative = 0`, `COALESCE(is_10b5_1,0) = 0`, ticker resolvable. **Fails the
gate today** (20 labeled rows at H=21, one eligible month). Note `is_first_buy`
will be a constant `False` until `cluster.FIRST_BUY_MIN_HISTORY_DAYS` (180) of SEC
history exists — a harmless constant column, honest about the feature not being
computable yet.

| feature | definition |
|---|---|
| `role` | `"officer_director"` / `"ten_pct"` / `"other"` (categorical) |
| `log_value` | `log10((value or 0) + 1)` |
| `is_first_buy` | reuse `cluster._is_first_buy` logic (nobody in the cluster bought this ticker before) |
| `position_increase_pct` | reuse `cluster._position_increase` (buy as % of prior holding; capped) |
| `lag_days` | `filed_date − transaction_date` |
| `cluster_size` | distinct other owners with a `P` in the same ticker, disclosure date within `[D − 14d, D]` |
| `mcap_bucket` | as Corpus A |
| `price_vs_spy_63d` | as Corpus A |

## Walk-forward evaluation

1. Sort rows by disclosure date. Group by calendar month (`disclosure_date[:7]`).
2. `eligible` = months with ≥ `min_test` rows (default **12**). Require
   `len(eligible) ≥ n_folds` **and** at least 2 distinct months earlier than the
   first eligible test month (enough to train on).
3. Test folds = the last `n_folds` eligible months. A sparse month is never a test
   fold, but its rows still count toward training any *later* fold. Each fold
   trains on **all rows disclosed before its test month** (expanding window — at
   this data scale there is no regime-shift argument strong enough to justify
   discarding history). Rows after the last test fold's month are unused.
   Concretely, with `folds=3`, `min_test=12`, and eligible months
   `[…, 2025-06, 2025-07, 2025-08]` (2025-05 had 9 rows, skipped as a test fold):
   fold 1 trains on everything before 2025-06 (2025-05 included) and tests 2025-06;
   fold 2 trains before 2025-07, tests 2025-07; fold 3 trains before 2025-08, tests
   2025-08.
4. Total labeled rows must be ≥ `min_rows` (default **80**). Otherwise → gate
   abort (below).
5. Pool the out-of-sample predictions across all test folds. Compute pooled
   metrics; also keep per-fold AUC to show variance. Retain the last fold's fitted
   pipelines for the feature-importance step.

`check_sufficiency` runs first and returns an abort message string (or `None`).
Abort message names what is missing and estimates when re-running would be
worthwhile from the disclosure-date span and current row rate:

```
INSUFFICIENT DATA for insiders.
  20 labeled rows (need >=80); 3 eligible months of >=12 rows (need >=3), only 1.
  Coverage since 2026-07; at ~10 rows/month, re-run around 2026-12.
```

## Models

scikit-learn (new dependency — user-approved). Both are run and compared:

- `LogisticRegression(class_weight="balanced", max_iter=1000)` — the interpretable
  baseline. Standardized coefficients are read directly for feature effect.
- `HistGradientBoostingClassifier(max_depth=3, max_iter=200, learning_rate=0.05,
  early_stopping=True, random_state=0)` — the "does non-linearity help" check.
  Expected to overfit at this n; that is itself an instructive result and the
  report says so when its train AUC sits far above its test AUC.

Preprocessing in an sklearn `Pipeline` via `ColumnTransformer`:
- numeric → `SimpleImputer(strategy="median")` + `StandardScaler`
- categorical → `SimpleImputer(strategy="most_frequent")` + `OneHotEncoder(handle_unknown="ignore")`

**Baseline model:** the same walk-forward, but a one-feature logistic regression on
the single obvious number — `log_amount` (politicians) / `log_value` (insiders).
Report its pooled AUC only (a one-feature model's Brier is not informative). It
answers "did LR/GBT beat just ranking by the one number everyone would look at."

## Metrics & report

Pooled out-of-sample, every number carrying its `n`:

- **ROC AUC** — headline. Also per-fold, to show variance.
- **Brier score** — are the probabilities meaningful or only a ranking.
- **Calibration** — a short text table: predicted-probability decile → actual hit
  rate.
- **Top-decile precision** — of the 10% of rows the model was most confident on,
  what fraction were label=1, versus the base rate. The practically meaningful
  number.
- **Base rate** — fraction of all rows that beat SPY. The "predict yes always"
  benchmark.

**Interpretation line, chosen by the pooled AUC:**
- AUC ∈ [0.45, 0.55] → `">>> No detectable edge. AUC ~0.5 is a coin flip."`
- AUC > 0.55 → `">>> Nominal edge, but N folds of ~M rows -- treat with suspicion
  until the sample is several times larger. This is not a forecast."`
- AUC < 0.45 → `">>> Worse than chance -- almost certainly noise at this n."`

**Feature effect:** LR standardized coefficients (sign + magnitude, sorted),
labelled "interpretation only, fit on all rows". Permutation importance for GBT on
the final fold's test set.

**Footer** (always): `"Evaluates historical skill of these features. Not a forecast,
not investment advice, cannot be pointed at a live signal. Small samples --
see the n on every line."`

Report style matches `backtest._report` (plain text, 72-col rules, stdout).

## Interface

```
python model_eval.py [politicians|insiders|both]     # default: both
    --horizon 21        trading days to the outcome
    --folds 3           walk-forward test months
    --min-rows 80       gate: total labeled rows
    --min-test 12       gate: rows an eligible test month needs
```

`menu.py`: new option `6) Оценка предсказательной силы (walk-forward модель)`,
calling `model_eval.run(conn, corpus, ...)` and printing the report. Sub-prompt for
corpus + horizon, defaults on Enter.

## Dependencies

`scikit-learn` → `requirements.txt` and `requirements-dev.txt`. Regenerate
`requirements.lock` (adds scipy, joblib, threadpoolctl; ~40 MB). numpy 2.5 and
pandas 3.0 are already present.

## Known limitations (stated in the report and README)

- Market cap is **current**, not as-of-disclosure — no historical shares-outstanding
  data. A company now "mega" may have been "small" then.
- Political amounts are **bracket floors**, not figures.
- **Survivorship**: delisted/renamed tickers drop out of yfinance and bias the set
  toward survivors.
- One horizon per run.
- Even the political corpus is thin once split — ~150 labelled rows from **20
  members**, test folds of ~12–30 rows, evaluated on 2025 because recent months are
  too sparse to test on. The report never hides this; the interpretation line and
  the per-fold AUC spread carry it.
- Gate defaults were lowered from the first draft (`min_rows` 100→80, `min_test`
  20→12) after the real House volume turned out far below the spec's initial
  assumption. This buys a marginal evaluation now rather than none; it does not
  make the sample adequate.

## Files changed

| file | change |
|---|---|
| `model_eval.py` | **new** — features, split, pipeline, evaluate, gate, report, `run`, `main` |
| `backtest.py` | add `collect_political_trades()` and `_return_before()`; add a keyword-only `extended_features=False` to `collect_purchases` that, when True, attaches the model feature columns (existing keys and `--purchases` behaviour unchanged) |
| `menu.py` | option 6 + `show_model_eval()` |
| `requirements.txt`, `requirements-dev.txt`, `requirements.lock` | add scikit-learn |
| `tests/test_model_eval.py` | **new** |
| `tests/conftest.py` | add `add_house_txn` helper (only `add_senate_txn` exists today) |
| `README.md` | new section after "Проверка сигналов на истории (backtest)" |

## Testing

`tests/test_model_eval.py`, fully offline (synthetic frames, no network, no sklearn
fit on real data):

- **feature extraction** — synthetic rows → expected feature dict per corpus.
- **leakage check** — `member_prior_hitrate` for a member's first trade is NaN;
  for a later trade with `H=21` it uses only earlier trades disclosed ≥ 42 calendar
  days before `D`; a trade of the same member disclosed 10 days earlier does **not**
  contribute; and it is computed from the in-memory labels without any extra fetch.
- **walk-forward splitter** — rows across 6 months, `folds=3` → 3 folds, expanding
  train, test months are the last 3, no index appears in both train and test of a
  fold, no train index has a later date than its test fold's start.
- **data gate** — 50 rows → abort string; 150 rows across 5 months with ≥20 in the
  last 3 → `None` (proceed).
- **metric aggregation** — hand-built predictions + labels → known AUC, Brier,
  top-decile precision.
- **interpretation line** — AUC 0.50 → "no detectable edge"; 0.62 → suspicion
  wording; 0.40 → "worse than chance".
- **isolation** — `grep -rl "model_eval" *.py` returns only `model_eval.py`
  (implemented in-test by scanning module source).

Manual: `python model_eval.py politicians` (real report or clean abort),
`python model_eval.py insiders` (gate abort with the re-run estimate),
`python model_eval.py` (both), `python bot.py --once --no-telegram` (no regression).

## Verification

```bash
cd ~/Desktop/disclosure-bot && source .venv/bin/activate
pip install -r requirements.txt && pytest tests/ -q          # all green
python model_eval.py politicians                             # report with n on every line
python model_eval.py insiders                                # "INSUFFICIENT DATA ... re-run around ..."
python model_eval.py                                         # both corpora
grep -rl "model_eval" *.py | grep -v '^model_eval\.py$'      # empty
python bot.py --once --sec-only --forms 4 --no-telegram      # unaffected
python menu.py  # option 6 -> report
```

Success: pytest green; the political report prints a pooled AUC with per-fold
variance, a calibration table, feature coefficients, and an interpretation line
matched to the AUC (evaluated on 2025 test folds); the insider path aborts cleanly
with a dated re-run estimate; nothing in the project imports `model_eval`;
`backtest.py --purchases` output is byte-for-byte unchanged.

Note: with ~150 political rows the pooled AUC will almost certainly land in
[0.45, 0.55] and the report will say "no detectable edge". That is the expected
and correct outcome, not a failure of the build.
