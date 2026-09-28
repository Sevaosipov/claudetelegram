# High Risk: Small-Company Insider Buying — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep insider buying in companies worth €50–300M, which today's floors throw away, and judge it by rules scaled to the company's size. Show it in the menu as «🎲 Высокий риск», and trade it in two new paper books, H1 and H2.

**Architecture:**
- **`strategy.py`:**
  - it gains a small-company band and rule (`_high_risk_tier`);
  - it adds a list `Selection.high_risk`;
  - it runs a second, lower-threshold pass of the SEC and Oslo finders in `buy_side_signals`. The finders' own €100k/€500k bars were set for large companies, so without it a small company's 0.1% (often €50–300k) never becomes a signal.
- **`bot.py`:** journals the high-risk signals every run (like cautions).
- **`telegram_notify` / `menu`:** a menu-only section.
- **`paper.py` / `paper_report.py`:** two books in a new `small` sleeve, measured against IWM.

**Tech Stack:** Python 3.12, SQLite, pytest (offline: `tests/conftest.py` blocks all network access).

**Spec:** `docs/superpowers/specs/2026-09-28-high-risk-small-companies-design.md`

## Global Constraints

- **Language and tests:**
  - user-facing text is Russian; code, comments and log lines are English;
  - the suite stays offline, so every new test stubs sizes and prices or needs none;
  - run it with `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider` from the worktree root.
- **Commits:** conventional messages ending with the line `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **The band:**
  - market value at least **€50M** and under **€300M**;
  - at least **€100k traded per day**;
  - an unknown market value or trading volume does not qualify;
  - sources are `SEC` and `NORWAY` only; no politicians, stakes, coins or ISIN-keyed BaFin/FI;
  - the main signals' filters still apply: buy side, disclosed within 3 days, and buyable on Trading 212.
- **The rule.** Management insiders are buyers whose role is in `cluster.roles.INSIDER_ROLES`. A signal qualifies when:
  - (1) two or more of them together bought at least **0.1%** of the market value; or
  - (2) a CEO or CFO (role `ceo` or `cfo`) bought at least **0.1%** on their own.
- **Tier and rule line:**
  - the tier is `high_risk`;
  - the ✓ line states the percentage with a comma decimal, e.g. «CEO купил 0,15% компании» or «2 инсайдера(ов) купили вместе 0,12% компании»;
  - signals at €300M and above go through the main tier rules, unchanged.
- **Where the signals go:**
  - they are journaled with tier `high_risk` on the run that finds them, and marked alerted;
  - the menu's Сигналы shows «🎲 Высокий риск (N)» after Кандидаты;
  - the Telegram digest never includes them.
- **Books:**
  - H1 and H2 are in the sleeve `small`, each with €20,000;
  - each buy is 20% of the book's value, at most 5 positions;
  - H1 sells at 182 days held and has no stop-loss;
  - H2 sells at 91 days held, −30%, or +50%;
  - the benchmark is **IWM**;
  - the success line is the stock books' own (20 trades, «идёт (сделок N из 20)» until then);
  - existing databases get the books on the next run, with their own start date.
- **Views:**
  - a third group, «ВЫСОКИЙ РИСК (Russell 2000: x%, худшая просадка y%)», whose header adds «с дд.мм.гггг» when its start differs from the portfolio header's;
  - `python paper.py H1`.

## Decisions this plan makes where the spec left room

- **The lower-threshold finder pass.** It uses a €50k cluster total and a €50k solo bar (0.1% of a €50M company). Its signals are tagged `high_risk_only`, and `select()` never gives them a main tier.
- **Duplicates.** A cluster both passes find is kept once, the main pass's copy, and is routed by size like any other.
- **The view before the books exist.** Books that `paper.py` defines but the database doesn't hold yet (before the next run creates them) are left out of the view, rather than crashing it.

## File Structure

| File | Change |
|---|---|
| `strategy.py` | High-risk constants, `HIGH_RISK` tier, `Selection.high_risk`, `_in_high_risk_band`, `_high_risk_tier`, routing in `select()`, the lower-threshold pass in `buy_side_signals` |
| `bot.py` | `_record_high_risk`, called in `main()` next to `_record_cautions` |
| `telegram_notify.py` | `format_tiered_digest(..., include_high_risk=False)` |
| `menu.py` | `show_signals` passes `include_high_risk=True` |
| `paper.py` | The `small` sleeve: constants, exits H1/H2, books, `create_books`, `stock_signals`, `stock_step`, `run` |
| `paper_report.py` | The «ВЫСОКИЙ РИСК» group, the trade minimum for `small`, books missing from the database skipped |
| `README.md` | The high-risk signal and the two books |
| `tests/test_strategy.py`, `tests/test_signals_view.py`, `tests/test_telegram_notify.py`, `tests/test_paper.py`, `tests/test_paper_report.py` | Tests |

---

### Task 1: The small-company signal in `strategy.py`

**Files:**
- Modify: `strategy.py` (constants block near the top; the tier-names line; `Selection`; new helpers after `_caution_tier`; `buy_side_signals`; `select`)
- Test: `tests/test_strategy.py`

**Interfaces:**
- Produces:
  - **Constants:** `strategy.HIGH_RISK = "high_risk"`, `HIGH_RISK_MIN_MCAP_EUR`, `HIGH_RISK_MIN_ADV_EUR`, `HIGH_RISK_MIN_PCT_OF_MCAP`, `HIGH_RISK_MIN_INSIDERS`, `HIGH_RISK_SOURCES`, `HIGH_RISK_FINDER_MIN_EUR`.
  - **Selection:** `Selection.high_risk: list[Tiered]`, each with `tier == "high_risk"`; the signal's `.tier` is set to `"high_risk"`.
  - **Finders:** `buy_side_signals(..., high_risk: bool = True)` adds the lower-threshold SEC/NORWAY pass. Its extra signals carry `high_risk_only = True`.

- [ ] **Step 1: Write the failing tests.** Add `import types` to the imports at the top of `tests/test_strategy.py`, then append:

```python
# ---------------------------------------------------------------- high risk
def _hr(sel):
    return {t.signal.ticker for t in sel.high_risk}


def _select_all(conn, t212=None):
    """Both finder passes, as the bot runs them."""
    return strategy.select(conn, strategy.buy_side_signals(conn, sources={"sec": True}),
                           t212 or _T212())


def _small(sized, cap=100e6, adv=500_000):
    sized["cap"], sized["adv"] = cap, adv


def test_two_directors_buying_0_12_percent_of_a_small_company_is_high_risk(conn, sized):
    _small(sized)
    for o in ("A", "B"):
        _buy(conn, "AAA", o, usd=69_600)            # €60k each: €120k = 0.12% of €100M
    sel = _select_all(conn)
    assert _hr(sel) == {"AAA"} and _tiers(sel) == (set(), set())
    [t] = sel.high_risk
    assert t.tier == strategy.HIGH_RISK and t.signal.tier == strategy.HIGH_RISK
    assert "2 инсайдера(ов) купили вместе 0,12% компании" in t.met


def test_below_0_1_percent_is_not_high_risk(conn, sized):
    _small(sized)
    for o in ("A", "B"):
        _buy(conn, "AAA", o, usd=52_200)            # €45k each: €90k = 0.09%
    assert _hr(_select_all(conn)) == set()


def test_a_ceo_buying_alone_is_found_by_the_lower_threshold_pass(conn, sized):
    _small(sized, cap=80e6)
    _buy(conn, "BBB", "Pat Chief", usd=139_200, officer=1, director=0,
         title="Chief Executive Officer")           # €120k = 0.15% of €80M, below the €500k solo bar
    sel = _select_all(conn)
    [t] = sel.high_risk
    assert t.signal.ticker == "BBB" and "CEO купил 0,15% компании" in t.met


def test_a_lone_director_is_not_high_risk(conn, sized):
    _small(sized, cap=80e6)
    _buy(conn, "BBB", "Dee Rector", usd=139_200)
    assert _hr(_select_all(conn)) == set()


@pytest.mark.parametrize("cap,adv,expected", [
    (49e6, 2e6, None), (50e6, 2e6, "high_risk"), (299e6, 2e6, "high_risk"),
    (300e6, 2e6, "strong"), (100e6, 99_000, None),
])
def test_the_small_company_band(conn, sized, cap, adv, expected):
    _small(sized, cap=cap, adv=adv)
    for o in ("A", "B", "C"):
        _buy(conn, "AAA", o, usd=348_000)           # €300k each, three directors
    sel = _select_all(conn)
    got = "high_risk" if _hr(sel) else "strong" if sel.strong else None
    assert got == expected


def test_an_unknown_size_is_not_high_risk(conn, sized):
    sized["cap"] = None
    for o in ("A", "B", "C"):
        _buy(conn, "AAA", o)
    assert _hr(_select_all(conn)) == set()


def test_politicians_are_not_high_risk(conn, sized):
    _small(sized)
    for member in ("Member One", "Member Two"):
        add_house_txn(conn, "AAA", member, "$250,001 - $500,000",
                      date=(TODAY - dt.timedelta(days=3)).strftime("%m/%d/%Y"))
    assert _hr(_select(conn)) == set()


def test_trading212_and_recency_apply_to_high_risk(conn, sized):
    _small(sized)
    for o in ("A", "B"):
        _buy(conn, "AAA", o, usd=69_600)
        add_sec_purchase(conn, "OLD", o, 69_600, (TODAY - dt.timedelta(days=20)).isoformat(),
                         filed_date=(TODAY - dt.timedelta(days=10)).isoformat())
    assert _hr(_select_all(conn, _T212(missing={"AAA"}))) == set()


def test_the_lower_threshold_pass_uses_its_own_bars(conn, monkeypatch):
    calls = _recording_finders(monkeypatch)
    strategy.buy_side_signals(conn, sources={"sec": True, "norway": True}, ignore_alert_state=True)
    sec = [kw for name, kw in calls if name == "find_sec_clusters"]
    nor = [kw for name, kw in calls if name == "find_norway_clusters"]
    assert len(sec) == 2 and len(nor) == 2
    for kw in (sec[1], nor[1]):
        assert kw["min_value"] == kw["solo_threshold"] == strategy.HIGH_RISK_FINDER_MIN_EUR
        assert kw["ignore_alert_state"] is True


def test_the_lower_threshold_pass_can_be_turned_off(conn, monkeypatch):
    calls = _recording_finders(monkeypatch)
    strategy.buy_side_signals(conn, sources={"sec": True}, high_risk=False)
    assert [name for name, _ in calls].count("find_sec_clusters") == 1


def test_a_cluster_both_passes_find_is_kept_once_and_untagged(conn, monkeypatch):
    def sec(conn, **kw):
        if kw.get("solo_threshold") == strategy.HIGH_RISK_FINDER_MIN_EUR:
            return [types.SimpleNamespace(source="SEC", ticker="AAA"),
                    types.SimpleNamespace(source="SEC", ticker="BBB")]
        return [types.SimpleNamespace(source="SEC", ticker="AAA")]
    monkeypatch.setattr(cluster, "find_sec_clusters", sec)
    monkeypatch.setattr(cluster, "find_stake_signals", lambda conn, **kw: [])
    sigs = strategy.buy_side_signals(conn, sources={"sec": True})
    assert [s.ticker for s in sigs] == ["AAA", "BBB"]
    assert not getattr(sigs[0], "high_risk_only", False) and sigs[1].high_risk_only is True


def test_a_lower_pass_signal_never_gets_a_main_tier(conn, sized):
    sized["cap"], sized["adv"] = BIG, LIQUID
    _buy(conn, "BIG", "Pat Chief", usd=139_200, officer=1, director=0,
         title="Chief Executive Officer")           # €120k solo: only the lower pass finds it
    sel = _select_all(conn)
    assert _tiers(sel) == (set(), set()) and _hr(sel) == set()
```

In the same file, change `test_buy_side_signals_forwards_cluster_and_source_specific_kwargs` so it reads each finder's **first** call. The lower-threshold pass now calls `find_sec_clusters` a second time. Replace the line `by_name = dict(calls)` with:

```python
    by_name = {}
    for name, kw in calls:
        by_name.setdefault(name, kw)
```

Also add `add_sec_purchase` to the `from conftest import …` line if it isn't already imported; it already imports `add_house_txn` and `add_sec_purchase`.

- [ ] **Step 2: Run them to verify they fail**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_strategy.py`
Expected: FAIL with `AttributeError: 'Selection' object has no attribute 'high_risk'`, and a `buy_side_signals()` error on the unexpected keyword `high_risk`.

- [ ] **Step 3: Implement.** In `strategy.py`:

- after `CANDIDATE_MIN_SCORE = 50.0`, add:

```python
# Small companies (€50-300M): the band FLOOR_MIN_MCAP_EUR used to drop. Their rule is
# scaled to the company -- purchases of at least HIGH_RISK_MIN_PCT_OF_MCAP percent of
# its value (spec: docs/superpowers/specs/2026-09-28-high-risk-small-companies-design.md).
HIGH_RISK_MIN_MCAP_EUR = 50e6
HIGH_RISK_MIN_ADV_EUR = 100_000
HIGH_RISK_MIN_PCT_OF_MCAP = 0.1         # percent of market value
HIGH_RISK_MIN_INSIDERS = 2
HIGH_RISK_SOURCES = ("SEC", "NORWAY")
# The finders' own bars (€100k cluster total, €500k solo) were set for large
# companies; a small company's 0.1% can be €50k. A second, lower-threshold pass
# feeds only the high-risk rule -- its extra signals never get a main tier.
HIGH_RISK_FINDER_MIN_EUR = 50_000
```

- change the tier names line to:

```python
STRONG, CANDIDATE, CAUTION, HIGH_RISK = "strong", "candidate", "caution", "high_risk"
```

- in `Selection`, after `cautions`, add:

```python
    # Small-company insider buying that clears the size-scaled rule: shown in the
    # menu's Сигналы and traded by the paper books H1/H2, never pushed.
    high_risk: list = field(default_factory=list)
```

- after `_caution_tier`, add:

```python
def _in_high_risk_band(sig) -> bool:
    cap, adv = getattr(sig, "market_cap_eur", None), getattr(sig, "avg_daily_value", None)
    return (sig.source in HIGH_RISK_SOURCES and not crypto.is_crypto(sig.ticker)
            and cap is not None and adv is not None
            and HIGH_RISK_MIN_MCAP_EUR <= cap < FLOOR_MIN_MCAP_EUR
            and adv >= HIGH_RISK_MIN_ADV_EUR)


def _pct_text(pct: float) -> str:
    return f"{pct:.2f}".replace(".", ",")


def _high_risk_tier(sig) -> Tiered | None:
    """A small company: two or more management insiders buying together at least
    HIGH_RISK_MIN_PCT_OF_MCAP of its value, or a CEO/CFO buying that much alone."""
    cap = sig.market_cap_eur
    insiders = [b for b in (getattr(sig, "buyers", None) or []) if b.role in INSIDER_ROLES]
    size = f"€{_short(cap)} / €{_short(sig.avg_daily_value)} в день"
    together = sum(b.total_eur for b in insiders) / cap * 100
    if len(insiders) >= HIGH_RISK_MIN_INSIDERS and together >= HIGH_RISK_MIN_PCT_OF_MCAP:
        return Tiered(sig, HIGH_RISK,
                      [size, f"{len(insiders)} инсайдера(ов) купили вместе {_pct_text(together)}% компании"])
    for b in sorted(insiders, key=lambda b: b.total_eur, reverse=True):
        pct = b.total_eur / cap * 100
        if b.role in ("ceo", "cfo") and pct >= HIGH_RISK_MIN_PCT_OF_MCAP:
            return Tiered(sig, HIGH_RISK, [size, f"{_ROLE_LABEL[b.role]} купил {_pct_text(pct)}% компании"])
    return None
```

- in `buy_side_signals`, add the keyword parameter `high_risk: bool = True` after `onchain: bool = False`, and add this sentence to its docstring:

```
    `high_risk` adds a second, lower-threshold pass of the SEC and Oslo finders
    (HIGH_RISK_FINDER_MIN_EUR for both the cluster total and a lone buyer) for the
    small-company rule; its extra signals are tagged `high_risk_only`.
```

  Then, just before the final `return signals`, add:

```python
    if high_risk:
        seen = {(s.source, s.ticker) for s in signals}
        low = {**cluster_kwargs, "min_value": HIGH_RISK_FINDER_MIN_EUR,
               "solo_threshold": HIGH_RISK_FINDER_MIN_EUR}
        extra = []
        if on.get("sec", False):
            extra += cluster.find_sec_clusters(conn, ignore_alert_state=ignore_alert_state,
                                               **low, **sec_kwargs)
        if on.get("norway", False):
            extra += cluster.find_norway_clusters(conn, ignore_alert_state=ignore_alert_state, **low)
        for s in extra:
            if (s.source, s.ticker) not in seen:
                s.high_risk_only = True
                signals.append(s)
```

- in `select()`, replace the tiering loop

```python
    for s in pre:
        t = _crypto_tier(conn, s) if hasattr(s, "crypto_kind") else _stock_tier(s)
        if t is not None:
```

  with

```python
    for s in pre:
        if hasattr(s, "crypto_kind"):
            t = _crypto_tier(conn, s)
        elif _in_high_risk_band(s):
            t = _high_risk_tier(s)
        elif getattr(s, "high_risk_only", False):
            t = None        # found only by the lower-threshold pass: never a main tier
        else:
            t = _stock_tier(s)
        if t is not None:
```

  and pass the new list in the `return Selection(...)`:

```python
    return Selection(
        strong=[t for t in tiered if t.tier == STRONG],
        candidates=candidates[:MAX_CANDIDATES],
        t212_checked=t212 is not None,
        exits=exits,
        cautions=cautions,
        high_risk=[t for t in tiered if t.tier == HIGH_RISK],
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_strategy.py`
Expected: all pass, including the existing floor tests (`test_below_the_size_floor_is_dropped` still sees no Сильный/Кандидат).

- [ ] **Step 5: Run the full suite, then commit**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider`
Expected: all pass.

```bash
git add strategy.py tests/test_strategy.py
git commit -m "feat(strategy): small-company insider buying as a high-risk signal

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Journal the signals and show them in the menu only

**Files:**
- Modify: `bot.py` (a new `_record_high_risk` after `_record_cautions`; call it in `main()`)
- Modify: `telegram_notify.py` (`format_tiered_digest`)
- Modify: `menu.py` (`show_signals`)
- Test: `tests/test_strategy.py`, `tests/test_telegram_notify.py`, `tests/test_signals_view.py`

**Interfaces:**
- Consumes (Task 1): `Selection.high_risk`, `strategy.HIGH_RISK`.
- Produces: `bot._record_high_risk(conn, selection) -> None`; `telegram_notify.format_tiered_digest(selection, closes, *, html=True, include_cautions=False, include_high_risk=False)`.

- [ ] **Step 1: Write the failing tests.**

Append to `tests/test_strategy.py`:

```python
def test_high_risk_signals_are_journaled_once_and_not_again(conn, sized):
    import bot
    _small(sized, cap=80e6)
    _buy(conn, "BBB", "Pat Chief", usd=139_200, officer=1, director=0,
         title="Chief Executive Officer")
    sel = strategy.select(conn, strategy.buy_side_signals(conn, sources={"sec": True}), None)
    bot._record_high_risk(conn, sel)
    assert conn.execute("SELECT ticker, tier FROM signal_journal").fetchall() == [("BBB", "high_risk")]
    again = strategy.select(conn, strategy.buy_side_signals(conn, sources={"sec": True}), None)
    assert again.high_risk == []
```

Append to `tests/test_telegram_notify.py` (it already imports `strategy`):

```python
def test_high_risk_signals_never_enter_the_telegram_digest():
    sel = strategy.Selection(strong=[], candidates=[], t212_checked=True,
                             high_risk=[strategy.Tiered(object(), strategy.HIGH_RISK, ["x"])])
    text = telegram_notify.format_tiered_digest(sel, [])
    assert "Высокий риск" not in text and "сигналов нет" in text
```

Append to `tests/test_signals_view.py` (it already has the `keyed`, `no_prices` fixtures and `INSTRUMENTS`):

```python
def test_view_shows_small_company_signals(conn, keyed, no_prices, capsys, monkeypatch):
    monkeypatch.setattr(trading212, "fetch_instruments", lambda session=None: INSTRUMENTS)

    def small(conn, signals):
        for s in signals:
            s.score, s.market_cap_eur, s.avg_daily_value = 100.0, 80e6, 500_000
        return signals
    monkeypatch.setattr("cluster.enrich_signals", small)
    recent = (TODAY - dt.timedelta(days=1)).isoformat()
    add_sec_purchase(conn, "AAPL", "Pat Chief", 139_200, recent, filed_date=recent, officer=1,
                     director=0, title="Chief Executive Officer")
    menu.show_signals(conn)
    out = _shown(capsys)
    assert "🎲 Высокий риск (1)" in out and "CEO купил 0,15% компании" in out
```

- [ ] **Step 2: Run them to verify they fail**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_strategy.py tests/test_telegram_notify.py tests/test_signals_view.py`
Expected: FAIL. There is no `_record_high_risk`, and the menu has no «Высокий риск» section. (The digest test may already pass, since nothing renders `high_risk` yet; that's fine, it guards the default.)

- [ ] **Step 3: Implement.**

In `bot.py`, after `_record_cautions`:

```python
def _record_high_risk(conn, selection) -> None:
    """Small-company signals are never pushed while their paper books are on trial --
    the menu's Сигналы shows them -- but they are journaled and marked alerted on the
    run that finds them, so history can measure them and they don't repeat."""
    _commit_signals(conn, [t.signal for t in selection.high_risk])
```

and in `main()` change

```python
        _record_cautions(conn, selection)
```

to

```python
        _record_cautions(conn, selection)
        _record_high_risk(conn, selection)
```

In `telegram_notify.py`, change `format_tiered_digest`:
- change its signature to `def format_tiered_digest(selection, closes: list, *, html: bool = True, include_cautions: bool = False, include_high_risk: bool = False) -> str:`;
- add a docstring sentence: "Only the menu passes include_high_risk: small-company signals are not pushed while their books are on trial.";
- right after the `if selection.candidates:` block, add:

```python
    high_risk = getattr(selection, "high_risk", []) if include_high_risk else []
    if high_risk:
        parts.append(_b(f"🎲 Высокий риск ({len(high_risk)})", html))
        parts += ["\n".join([format_any_signal(t.signal, html=html)] + _rule_lines(t, html))
                  for t in high_risk]
```

- and change the "nothing to show" condition to:

```python
    if not (selection.strong or selection.candidates or high_risk or closes
            or selection.exits or cautions):
```

In `menu.py`'s `show_signals`, change the print line to:

```python
    print(telegram_notify.format_tiered_digest(selection, closes, html=False, include_cautions=True,
                                               include_high_risk=True))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_strategy.py tests/test_telegram_notify.py tests/test_signals_view.py`
Expected: all pass.

- [ ] **Step 5: Run the full suite, then commit**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider`
Expected: all pass.

```bash
git add bot.py telegram_notify.py menu.py tests/test_strategy.py tests/test_telegram_notify.py tests/test_signals_view.py
git commit -m "feat: journal small-company signals and show them in the menu, never pushed

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: The small-company paper books H1 and H2, their view, and the README

**Files:**
- Modify: `paper.py` (constants, `EXITS`, `Book`, `BOOKS`, `create_books`, `stock_signals`, `stock_step`, `run`)
- Modify: `paper_report.py` (`_BENCH_NAME`, `stats`, `format_summary`)
- Modify: `README.md` (the «Бумажный портфель» section, and the crypto/signal description of «Высокий риск»)
- Test: `tests/test_paper.py`, `tests/test_paper_report.py`

**Interfaces:**
- Consumes: `Selection.high_risk` (Task 1).
- Produces:
  - the books `H1` and `H2` (sleeve `small`, benchmark `IWM`);
  - `paper.SMALL_START_EUR`, `SMALL_SLICE`, `SMALL_MAX_POSITIONS`, `SMALL_BENCHMARK`;
  - `EXITS["H1"]`, `EXITS["H2"]`.

- [ ] **Step 1: Write the failing tests.**

In `tests/test_paper.py`:
- in `test_books_are_created_once_with_their_money`, change `assert len(rows) == 11` to `assert len(rows) == 13` and add:
  ```python
      assert ("H1", "small", TODAY.isoformat(), 20_000.0, 20_000.0, "IWM") in rows
  ```
- in the run test (the one asserting `_run_day(...) == 11`), change 11 to 13;
- in `test_one_failing_book_does_not_stop_the_others`, change 10 to 12.

Then append:

```python
# ------------------------------------------------------- small-company books
def _hr_sel(*sigs):
    return strategy.Selection(strong=[], candidates=[], t212_checked=True,
                              high_risk=[strategy.Tiered(s, "high_risk") for s in sigs])


def test_small_books_buy_only_high_risk_signals(conn):
    sel = strategy.Selection(strong=[strategy.Tiered(_sig("AAA"), "strong")], candidates=[],
                             t212_checked=True,
                             high_risk=[strategy.Tiered(_sig("SML", tier="high_risk"), "high_risk")])
    assert [s.ticker for s in paper.stock_signals(sel, paper.BOOK_BY_CODE["H1"])] == ["SML"]
    assert [s.ticker for s in paper.stock_signals(sel, paper.BOOK_BY_CODE["R1-E1"])] == ["AAA"]


def test_small_books_buy_a_fifth_and_hold_five(conn):
    _book(conn)
    sigs = [_sig(f"S{i}", tier="high_risk") for i in range(6)]
    paper.stock_step(conn, paper.BOOK_BY_CODE["H1"], _hr_sel(*sigs), paper.Prices(Fetch({})), TODAY)
    orders = paper.orders(conn, "H1")
    assert [o["amount_eur"] for o in orders if o["status"] == "pending"] == [4_000.0] * 5
    assert [o["note"] for o in orders if o["status"] == "skipped"] == ["мест нет"]


@pytest.mark.parametrize("code,days,value,expected", [
    ("H1", 181, 1_000.0, None), ("H1", 182, 4_000.0, "182 дн. в позиции"),
    ("H2", 91, 4_000.0, "91 дн. в позиции"), ("H2", 10, 2_799.0, "стоп -30%"),
    ("H2", 10, 6_001.0, "цель +50%"), ("H2", 10, 3_000.0, None),
])
def test_small_book_exits(conn, code, days, value, expected):
    _book(conn)
    pos = _position(conn, code, fill_days_ago=days, net=4_000.0, value=value)
    assert _exit(conn, code, pos) == expected


def test_run_trades_the_small_books_against_iwm(conn):
    day1 = TODAY - dt.timedelta(days=1)
    before = day1 - dt.timedelta(days=1)       # run() only uses bars dated before its day
    _run_day(conn, day1, {"IWM": _bars([200, 210], before), "SML": _bars([10, 10], before)},
             _hr_sel(_sig("SML", tier="high_risk")))
    assert [o["ticker"] for o in paper.orders(conn, "H1")] == ["SML"]
    assert conn.execute("SELECT bench FROM paper_equity WHERE book = 'H1'").fetchone()[0] == \
        pytest.approx(20_000.0)
```

(`_run_day`, `_position`, `_exit`, `_book`, `_bars`, `Fetch` and `_sig` are the existing helpers in this file. `_run_day(conn, day, series, selection)` runs `paper.run` with a stub fetch. Its `Prices` keeps only bars dated before `day`, so the series end the day before `day1`. The benchmark is indexed from the last close on or before the start date, so H1's first value equals its €20,000.)

Append to `tests/test_paper_report.py`:

```python
def test_small_books_wait_for_their_20_trades(conn):
    _start(conn, 190)
    _equity(conn, "H1", [(190, 20_000, 20_000), (0, 24_000, 21_000)])
    _closed_trades(conn, "H1", 5)
    assert paper_report.stats(conn, paper.BOOK_BY_CODE["H1"], TODAY)["status"] == "идёт (сделок 5 из 20)"


def test_summary_has_a_high_risk_group_with_its_own_start(conn):
    _start(conn, 40)
    conn.execute("UPDATE paper_books SET start_date = ? WHERE sleeve = 'small'",
                 ((TODAY - dt.timedelta(days=10)).isoformat(),))
    conn.commit()
    text = paper_report.format_summary(conn, TODAY)
    started = (TODAY - dt.timedelta(days=10)).strftime("%d.%m.%Y")
    assert "ВЫСОКИЙ РИСК (Russell 2000:" in text and f"с {started})" in text
    assert "H1" in text and "H2" in text


def test_summary_skips_books_the_database_does_not_have_yet(conn):
    _start(conn, 40)
    conn.execute("DELETE FROM paper_books WHERE sleeve = 'small'")
    conn.commit()
    text = paper_report.format_summary(conn, TODAY)
    assert "ВЫСОКИЙ РИСК" not in text and "R1·E1" in text
```

(`_start`, `_equity` and `_closed_trades` are the existing helpers in `tests/test_paper_report.py`.)

- [ ] **Step 2: Run them to verify they fail**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_paper.py tests/test_paper_report.py`
Expected: FAIL with `KeyError: 'H1'` (no such book yet).

- [ ] **Step 3: Implement `paper.py`.**

- after `CRYPTO_BENCHMARK = "BTC-USD"`, add:

```python
SMALL_START_EUR = 20_000.0      # the high-risk sleeve: small-company insider buying
SMALL_SLICE = 0.20              # of the book's value, per buy -- few signals a month
SMALL_MAX_POSITIONS = 5
SMALL_BENCHMARK = "IWM"         # the Russell 2000
_START_EUR = {"stock": STOCK_START_EUR, "crypto": CRYPTO_START_EUR, "small": SMALL_START_EUR}
_BENCHMARK = {"stock": STOCK_BENCHMARK, "crypto": CRYPTO_BENCHMARK, "small": SMALL_BENCHMARK}
```

- add two entries to `EXITS`:

```python
    "H1": (182, None, None, False),
    "H2": (91, -0.30, 0.50, False),
```

- in `Book`, change the `sleeve` comment to `# stock | crypto | small` and the `label` property to:

```python
    @property
    def label(self) -> str:
        if self.sleeve in ("crypto", "small"):
            return self.code
        return f"{self.buy}·{self.exit}" + ("+аналитики" if self.analyst else "")
```

- append the two books to `BOOKS`:

```python
BOOKS = tuple(
    [Book(f"{r}-{e}", "stock", r, e) for r in ("R1", "R2") for e in ("E1", "E2", "E3", "E4")]
    + [Book("R1-E1-AN", "stock", "R1", "E1", analyst=True),
       Book("C-A", "crypto", rule="A"),
       Book("C-B", "crypto", rule="B"),
       Book("H1", "small", "H", "H1"),
       Book("H2", "small", "H", "H2")])
```

- in `create_books`, replace the two lines choosing `start` and `bench` with:

```python
        start, bench = _START_EUR[b.sleeve], _BENCHMARK[b.sleeve]
```

  Also change its docstring to: `"""Every book, once -- the first run is a book's start date (a book added to the code later starts on the next run)."""`

- in `stock_signals`, add at the top of the body:

```python
    if book.sleeve == "small":
        return [t.signal for t in getattr(selection, "high_risk", [])]
```

  and add to its docstring: `The small-company books take only the day's high-risk signals.`

- in `stock_step`, replace `SLICE * value` and `max_positions=MAX_POSITIONS` with sleeve-dependent values. Add before the buy loop:

```python
    slice_, max_positions = ((SMALL_SLICE, SMALL_MAX_POSITIONS) if book.sleeve == "small"
                             else (SLICE, MAX_POSITIONS))
```

  and use `slice_ * value` and `max_positions=max_positions` in the `place_buy` call.

- in `run`, change `if book.sleeve == "stock":` to `if book.sleeve in ("stock", "small"):`.

- [ ] **Step 4: Implement `paper_report.py`.**

- change `_BENCH_NAME` to:

```python
_BENCH_NAME = {"stock": "S&P 500", "crypto": "BTC", "small": "Russell 2000"}
_GROUPS = (("stock", "АКЦИИ"), ("crypto", "КРИПТО"), ("small", "ВЫСОКИЙ РИСК"))
```

- in `stats`, change `elif book.sleeve == "stock" and trades < paper.MIN_STOCK_TRADES:` to:

```python
    elif book.sleeve in ("stock", "small") and trades < paper.MIN_STOCK_TRADES:
```

- in `format_summary`, replace

```python
    for sleeve, title in (("stock", "АКЦИИ"), ("crypto", "КРИПТО")):
        books = [b for b in paper.BOOKS if b.sleeve == sleeve]
        head = stats(conn, books[0], today)
```

  with

```python
    for sleeve, title in _GROUPS:
        # A book added to the code after the database was created exists only from
        # the next run on -- leave it out until then.
        books = [b for b in paper.BOOKS if b.sleeve == sleeve and _book_row(conn, b.code)]
        if not books:
            continue
        head = stats(conn, books[0], today)
        since = ("" if head["start"] == first["start"]
                 else f", с {dt.date.fromisoformat(head['start']).strftime('%d.%m.%Y')}")
```

  and in that group's header line, append `{since}` just before the closing parenthesis:

```python
            f"{title} ({_BENCH_NAME[sleeve]}: {_pct(head['bench_ret'])}, "
            f"худшая просадка {_pct(head['bench_dd'])}{month}{since})", html))
```

- [ ] **Step 5: Update the README.** In `README.md`'s «Бумажный портфель» section, after the «Крипто (2 книги…)» bullet, add:

```markdown
- **Высокий риск (2 книги, по €20 000):** инсайдерские покупки в компаниях на €50–300
  млн (раньше отбрасывались порогом €300 млн). Сигнал — 2+ инсайдера из руководства
  купили вместе от 0,1% стоимости компании, или CEO/CFO один — от 0,1%. Каждая
  покупка — 20% книги, не больше 5 позиций. H1 держит 6 месяцев без стопа, H2 — 3
  месяца, стоп −30%, фиксация +50%. Сравнение — с Russell 2000 (IWM). Такие сигналы
  видны в меню «Сигналы» в разделе «🎲 Высокий риск» и в Telegram не приходят, пока
  книги не пройдут проверку.
```

In the same section, change «11 виртуальных книг» to «13 виртуальных книг».

- [ ] **Step 6: Run the tests to verify they pass**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_paper.py tests/test_paper_report.py`
Expected: all pass.

- [ ] **Step 7: Run the full suite, then commit**

Run: `/Users/sevastians/Desktop/disclosure-bot/.venv/bin/python -m pytest -q -p no:cacheprovider`
Expected: all pass.

```bash
git add paper.py paper_report.py README.md tests/test_paper.py tests/test_paper_report.py
git commit -m "feat(paper): small-company books H1 and H2 against the Russell 2000

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## After the plan (manual, needs the user's go-ahead)

1. Merge and push.
2. Restart the Telegram bot.
3. H1 and H2 start on the next daily run after the merge.
