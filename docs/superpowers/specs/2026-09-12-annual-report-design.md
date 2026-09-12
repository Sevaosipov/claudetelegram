# Design: annual-report section for the ticker dossier (`annual_report.py`)

Date: 2026-09-12
Status: approved for implementation planning

## Context

The user asked disclosure-bot to "look and analyse through the annual reports of
the companies that are used as a signal." Brainstormed down from a broad,
multi-jurisdiction ask to a scoped first slice:

- **SEC filers only** for now (10-K / 20-F). Norway's Brønnøysundregistrene has a
  genuinely open, no-auth API and is a natural next phase; Sweden's Bolagsverket
  needs an OAuth2-registered account and Germany's Unternehmensregister has no API
  at all and documents its own coverage gaps — both deferred, each is its own
  source-integration project on the scale of the original BaFin/Norway/Sweden
  build, not an extension of this one.
- **Not an AI-generated summary.** Offered and initially chosen, then dropped once
  the user saw the cost (~$0.20-0.50/filing) and the API-key setup step. Deferred
  indefinitely, not designed here.
- **Structured facts + targeted red-flag detection** ("Tier 2" from the
  brainstorm): financial figures pulled straight from the filing's own XBRL data,
  plus text-search for three specific, standardized disclosures. Fully
  deterministic, no new dependency, no new account.

Validated against a real filing before committing to this shape: WeWork's
2023-03-29 10-K (a genuine, well-documented going-concern case) confirmed the
target going-concern phrase is found with the exact real disclosure text, and
also exposed that naive keyword search on "material weakness" and "restat" both
false-positive on boilerplate present in *every* 10-K (a hypothetical risk-factor
sentence about controls, and the universal Rule 10D-1 cover-page checkbox) — fixed
by requiring specific, standardized disclosure phrasing instead of bare keywords,
re-verified against the same document as a negative control.

## Non-goals (safety boundary — binding)

- **No verdict, no summarization, no interpretation.** Every red flag is a
  verbatim quote of the filing's own text plus a link to the document. Every
  financial figure is the filing's own number, shown with its YoY delta and
  nothing else. Nothing in this module characterizes whether a figure or a
  disclosure is good, bad, material, or actionable.
- **No AI / LLM calls.** Pure deterministic text search and SEC XBRL lookups.
- **Not attached to the daily Telegram signal digest.** Runs only on-demand, when
  a specific ticker's dossier is requested (`research.py` / menu option 2).
  `bot.py`'s scheduled run is untouched by this spec.
- **SEC filers only.** No Norway/Sweden/Germany code in this pass. A ticker with
  no resolvable CIK, or no 10-K/20-F ever filed, gets no section (silent skip,
  same as every other CIK-gated section in `research.py` today).
- **A missing red flag is not a clean bill of health.** The report says so
  explicitly (see Report format) rather than implying completeness.

## Architecture

New standalone module `annual_report.py`, library-only like `tradingview.py` (no
CLI, no `main()`) — its only consumer is `research.py`.

```
annual_report.py
├── find_latest_annual_filing(cik, session=None)  -> dict | None  [SEC submissions API]
├── fetch_filing_text(cik, accession, primary_document)  -> str | None
├── _RED_FLAGS                           constant: (key, phrases, ru_label) tuples
├── find_red_flags(text)                -> list[dict]    [pure text search]
├── _FACT_CONCEPTS                       constant: (key, concept fallback chain, ru_label) tuples
├── annual_financials(cik, form_filter) -> dict | None   [SEC XBRL companyfacts API]
├── build(cik)                          -> dict | None   [orchestrator]
└── format_report(rep)                  -> str            [Russian-language section]
```

`research.py` changes: `build(conn, ticker)` gains `rep["annual_report"] =
annual_report.build(cik)` next to the other CIK-gated sections; `format_report`
appends `annual_report.format_report(rep["annual_report"])` as a new
`termstyle.section(...)`-headed block, positioned immediately after the existing
"Финансы (Yahoo Finance...)" section (`_format_financials`) and before dilution.
Both are company-level financial context, and reading the filing's own XBRL
numbers directly under Yahoo's blended trend lets the two be compared at a
glance -- unlike the person-level insider-transaction lists that follow later.

## Data sources

Both endpoints are already used elsewhere in this project (`research.py`'s
`recent_filings()` and `share_count_history()`), so no new headers/session/rate-limit
handling is needed — reuse `universe.sec_headers()`.

**1. Submissions feed** — `https://data.sec.gov/submissions/CIK{cik:010d}.json`.
`find_latest_annual_filing(cik, session=None)` (optional `requests.Session`, same
signature shape as `fetch_filing_text`/`annual_financials` below, all three
independently testable by injecting a fake session or monkeypatching `requests.get`)
walks `data["filings"]["recent"]` (already in
reverse-chronological order, same assumption `recent_filings()` makes — filings
older than the "recent" window are not reachable, an inherited, pre-existing
limitation, not new to this spec) and returns the first entry whose `form` is in
`_ANNUAL_FORMS = ("10-K", "10-K/A", "20-F", "20-F/A")`:

```python
{
    "form": "10-K", "filed": "2026-03-01", "fiscal_year": 2025,   # from `.get("fy")` sibling array; None if absent
    "accession": "0000320193-26-000012", "primary_document": "aapl-20251227.htm",
}
```

**2. The filing document itself** — `fetch_filing_text` builds
`https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{primary_document}`,
GETs it, strips HTML tags (`re.sub("<[^>]+>", " ", html)`, then collapses HTML
entities and whitespace — verified live against a 7MB real 10-K without incident).
Returns `None` on any request/parse failure, per this project's universal
network-step convention.

**3. XBRL company facts** — `https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json`,
same endpoint `share_count_history()` already calls. `annual_financials` reads
`facts["us-gaap"][concept]["units"]["USD"]`, filters to
`form in form_filter and fp == "FY"`, and keeps the last 2 by `end` date per
concept.

## Red flag detection

```python
_RED_FLAGS = (
    ("going_concern",
     ("substantial doubt about", "substantial doubt regarding"),
     "существенные сомнения в способности продолжать деятельность (going concern)"),
    ("material_weakness",
     ("identified a material weakness", "identified one or more material weaknesses",
      "disclosure controls and procedures were not effective",
      "internal control over financial reporting was not effective"),
     "выявленный существенный недостаток внутреннего контроля"),
    ("restatement",
     ("restated its previously issued financial statements",
      "restatement of our previously issued financial statements",
      "we determined to restate"),
     "пересчёт (restatement) ранее опубликованной отчётности"),
)
```

`find_red_flags(text)`: lower-cases `text` once, then for each `_RED_FLAGS` entry
checks whether *any* of its phrases is a substring (`or` across the tuple — each
phrase is independently specific enough to stand alone, unlike a bare keyword).
On a hit, returns `{"key", "label", "quote"}` where `quote` is the original-case
text sliced ~150 chars before and ~250 after the first match, whitespace-collapsed
— matches the snippet shown for WeWork's real disclosure during validation. No
match across any entry for a flag → it is simply absent from the returned list
(not a `False` entry) — `find_red_flags` returns `[]` when nothing is found,
which the report layer renders as the explicit "nothing found" line below, not as
silence.

**Why these three and not more:** each corresponds to a specific, close-to-boilerplate
legal disclosure phrase (going-concern language is prescribed by ASC 205-40 audit
guidance; "material weakness" and "restatement" are SOX/PCAOB-defined terms with
standardized disclosure phrasing once actually triggered) — which is exactly what
makes keyword search reliable *here* and not reliable for free-form risk-factor
prose in general. This is deliberately not extended to a general risk-factor
scan (that is the deferred Tier 3).

## Financial facts

```python
_FACT_CONCEPTS = (
    ("revenue", ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"), "Выручка"),
    ("net_income", ("NetIncomeLoss",), "Чистая прибыль"),
    ("assets", ("Assets",), "Активы"),
    ("liabilities", ("Liabilities",), "Обязательства"),
    ("operating_cf", ("NetCashProvidedByUsedInOperatingActivities",), "Операционный денежный поток"),
)
```

For each key, try each concept in its fallback chain in order (revenue's tag
changed with ASC 606 adoption around 2018; older filings only have `Revenues`);
use the first one present in the company's facts at all. Keep the last 2 annual
(`form_filter` + `fp == "FY"`) points by `end` date. YoY delta = `(latest - prior)
/ abs(prior) * 100` when both are present, else `None`. A concept entirely absent
(common for 20-F filers on IFRS tags, which use a different XBRL taxonomy
namespace — `ifrs-full`, not mapped in this pass) leaves that field `None`; the
report omits only that line rather than the whole section. `annual_financials`
returns `None` only if the companyfacts fetch itself fails (network error, no CIK)
— an empty-but-successful result (all fields `None`) still renders as a section
with the filing's own header line and a note that no figures were tagged.

## Report format

```
── Годовой отчёт (SEC 10-K/20-F) ─────────────────────────────────────
  Форма 10-K от 2026-03-01 (отчётный год: 2025)

  ┌──────────────────────────────┬──────────────┬──────────┐
  │ Выручка                      │ $416,161 млн │ +11.3% г/г │
  │ Чистая прибыль               │ $112,010 млн │ +18.9% г/г │
  │ Активы                       │ $359,241 млн │          │
  │ Обязательства                │ $285,508 млн │          │
  │ Операционный денежный поток  │ $111,482 млн │          │
  └──────────────────────────────┴──────────────┴──────────┘

  ⚠️ существенные сомнения в способности продолжать деятельность (going concern):
     «...raise substantial doubt about the Company's ability to continue as a
     going concern within one year after the date that these Consolidated
     Financial Statements are issued...»
     Полный документ: https://www.sec.gov/Archives/edgar/data/.../we-20221231.htm

  Текстовый поиск не нашёл упоминаний о выявленном существенном недостатке
  внутреннего контроля или пересчёте отчётности — это не гарантия их
  отсутствия, только то, что стандартные формулировки не встретились.
```

Uses `termstyle.table()` for the financial-figures block (ties into the recent
readability pass rather than hand-padding a new table). The closing caveat names
whichever of the three flags were *not* found (almost always at least one,
realistically) so the reader knows the search ran rather than silently produced
nothing -- omitted entirely only in the on-paper case where all three fired,
since there is then nothing left to caveat as "checked and clear."

`format_report(None) == ""` (no CIK, or no annual filing on file — same silent-skip
convention as every other optional dossier section).

## Files changed

| file | change |
|---|---|
| `annual_report.py` | **new** — everything above |
| `research.py` | `build()`: add `rep["annual_report"]`; `format_report()`: splice in the new section right after the financials block, before dilution |
| `tests/test_annual_report.py` | **new** |
| `tests/fixtures/annual_report_going_concern.htm` | **new** — synthetic, realistic-shaped HTML containing the real going-concern phrase plus the two boilerplate false-positive sentences discovered during validation |
| `tests/fixtures/annual_report_clean.htm` | **new** — synthetic HTML with none of the three red-flag phrases, for the "nothing found" path |
| `README.md` | short note under the dossier section |

No dependency changes — `requests` is already a project dependency, both SEC
endpoints are already called elsewhere.

## Testing

`tests/test_annual_report.py`, fully offline (`requests` calls monkeypatched, no
network):

- **`find_red_flags`** against `annual_report_going_concern.htm`'s stripped text:
  returns exactly one entry (`going_concern`), with a quote containing "substantial
  doubt"; confirms `material_weakness` and `restatement` are *not* triggered by
  the fixture's deliberately-included boilerplate sentences (the false positives
  found during validation).
- **`find_red_flags`** against `annual_report_clean.htm`: returns `[]`.
- **`annual_financials`**: synthetic companyfacts JSON (two `us-gaap` concepts
  present, one absent, one only present as the older `Revenues` tag) →
  correct fallback selection, correct YoY delta, `None` for the absent concept,
  no crash.
- **`annual_financials`** filters out non-FY / non-10-K data points from a
  synthetic facts payload that includes 10-Q rows mixed in with 10-K rows for the
  same concept — the 10-Q rows must not appear in the result.
- **`find_latest_annual_filing`**: synthetic submissions JSON with a mix of forms
  → picks the first `_ANNUAL_FORMS` match, correct field extraction; returns
  `None` when no such form appears at all.
- **`build`**: `None` for a falsy `cik`; `None` when `find_latest_annual_filing`
  finds nothing; monkeypatched happy path returns a dict with both
  `financials`/`red_flags` populated.
- **`format_report`**: renders the filing header, the table, a red flag's quote
  verbatim, and the "checked, nothing found" caveat line naming the checked flags
  when the list is short; `format_report(None) == ""`.
- **No verdict language**: `format_report`'s output, lower-cased, contains none of
  this project's standard forbidden terms (`"buy "`, `"sell "`, `"рекомендуем"`,
  `"стоит купить"`, etc. — same list `test_report_never_renders_a_verdict` already
  checks in `tests/test_research.py`).
- **`research.py` integration**: `format_report(build(conn, ticker))` includes the
  new section header when `annual_report.build` is monkeypatched to return a
  populated dict, and omits it entirely when monkeypatched to return `None`.

Manual: `python -c "import research; print(research.format_report(research.build(db.connect(...), 'AAPL')))"`
against the real database — confirmed reachable during design validation (Apple's
own XBRL facts fetched live, `form: "10-K", fp: "FY"` filtering confirmed correct).

## Verification

```bash
cd ~/Desktop/disclosure-bot && source .venv/bin/activate
pytest tests/ -q                                    # all green
python -c "
import db, research
conn = db.connect('data/disclosures.db')
print(research.format_report(research.build(conn, 'AAPL')))
"                                                     # real dossier, annual-report section present
python bot.py --once --no-telegram                   # unaffected -- annual_report.py has no bot.py caller
grep -rl 'annual_report' *.py                         # only annual_report.py and research.py
```

Success: pytest green; a real ticker's dossier shows the new section with real
XBRL figures and YoY deltas; a ticker with no CIK or no annual filing shows no
section and no error; nothing outside `research.py` imports `annual_report`.
