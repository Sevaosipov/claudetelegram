# CFD trading signals

Date: 2026-10-05.

**Requested by the user:**
- "next i want to add separate cfd trading signals. those must have 4 stages of take profit and a stop loss, as
  well as how much risk should this trade have according to my balance";
- then, on the design: "build it and add the analysis for other pairs and tickers the way you would use it
  yourself".

The bot never places orders. A CFD signal is a message. The risk figures are arithmetic on numbers the user sets
(balance, risk percent). Nothing here says what risk is right for the user.

## Background (the user's own research, ~/forex-daytrader)

About 245 backtests across 14 archetypes × 13 instruments found **no intraday forex edge**. Two setups survived:

- **The gold config:** `OANDA:XAUUSD` 1h, Model B (scale-free pullback trigger) with a 3×ATR trailing stop and
  ADX ≥ 20, London session 07:00–16:00, flat at session end. Out-of-sample profit factor 1.17 at realistic cost,
  Sharpe 0.21, about 14 trades a month.
- **The daily EUR/USD carry-gated trend** (`carry_strategy.py`): profit factor 1.9 on 15 trades, about one trade a
  year.

Both exit on a trailing stop. Neither was tested with staged take-profits.

That project's method is followed here: hypotheses, costs and the gate are written down before any run
(pre-registration), every result is reported, and what fails isn't traded.

## Part 1. Pre-registration (frozen before the first backtest run)

### 1.1 Universe: 23 instruments, daily OHLC from Yahoo

| Class | Instruments (Yahoo symbol → name shown) |
|---|---|
| FX major | `EURUSD=X`→EURUSD, `GBPUSD=X`→GBPUSD, `USDJPY=X`→USDJPY, `AUDUSD=X`→AUDUSD, `USDCAD=X`→USDCAD, `USDCHF=X`→USDCHF, `NZDUSD=X`→NZDUSD |
| FX cross | `EURGBP=X`→EURGBP, `EURJPY=X`→EURJPY, `GBPJPY=X`→GBPJPY, `EURCHF=X`→EURCHF |
| Metals | `GC=F`→XAUUSD, `SI=F`→XAGUSD |
| Energy | `CL=F`→WTI, `BZ=F`→BRENT |
| Indices | `^GSPC`→US500, `^NDX`→US100, `^DJI`→US30, `^GDAXI`→DE40, `^FTSE`→UK100, `^N225`→JP225 |
| Crypto | `BTC-USD`→BTCUSD, `ETH-USD`→ETHUSD |

- **Single-stock CFDs are out of scope:** gap and financing risk, and the bot already covers stocks through
  insider signals.
- **Data hygiene:** a bar is dropped when any of O/H/L/C is missing or ≤ 0, when high < low, or when its high or
  low is more than 15 % away from its close (Yahoo FX history has bad ticks). An instrument needs at least 300 bars
  before its first signal.

### 1.2 Setups (fixed parameters, both directions, one open trade per instrument per setup)

Indicators:
- ATR(14), Wilder smoothing;
- EMA and SMA of closes;
- ADX(14), Wilder;
- all computed on completed bars only.

**PB-D: trend pullback on daily bars** (the user's Model B, moved to the timeframe where costs are small against
the move).
- **Trend up:** close > EMA50 and EMA50 > EMA50 five bars earlier. Trend down is the mirror.
- **Pullback (long):** lowest low of the last 5 bars ≤ (highest high of the last 10 bars) − 1.0 × ATR.
- **Trigger (long):** close > previous bar's high, and close ≥ open.
- **Filter:** ADX ≥ 20.
- **Signal at the close of bar t.** Entry at the open of bar t+1.
- **Stop (long):** (lowest low of the last 5 bars as of t) − 0.5 × ATR_t. Short is the mirror.

**BO-D: Donchian breakout on daily bars.**
- **Long:** close_t > highest high of the previous 55 bars (excluding t), and close_t > SMA200_t. Short is the
  mirror.
- **Signal at the close of bar t.** Entry at the open of bar t+1.
- **Stop (long):** entry − 2.5 × ATR_t, known at the signal as close_t − 2.5 × ATR_t. The stop level is fixed at
  the signal; R is measured from the actual entry.

**PB-H1-GOLD: the gold config, ported 1:1 from `eurusd_daytrader.pine`** (Model B, not legacy), on `GC=F` 1h bars.
- **Session:** 07:00–16:00 Europe/London (bars whose start time is inside it).
- **Higher-timeframe trend:** EMA50 of 4-hour closes, using the previous completed 4h bar. Up means close > it
  and it is rising against the 4h bar before.
- **Swing high** = highest high of 10 bars. **Pullback low** = lowest low of 5 bars. **Dip:** pullback low ≤ swing
  high − 1.0 × ATR.
- **Trigger:** close > high[1] and close ≥ open.
- **Filter:** ADX ≥ 20.
- **Entry at the signal bar's close.**
- **Stop** = pullback low − 0.5 × ATR.
- **Flat at session end:** the trade is closed at the close of the last in-session bar.

**CARRY-D: the existing EUR/USD carry-gated trend.** It is reported with the exits below for information only: 15
trades can't pass a gate.

**Skipped entries.** A trade is skipped when R = |entry − stop| is ≤ 0.25 × ATR (a gap through the stop level) or
when R > 6 × ATR.

### 1.3 Exits compared

R is the distance from entry to the initial stop.

| Exit | What it does |
|---|---|
| **E0, baseline** | The setup's own exit: a trailing stop of 3 × ATR from the best extreme since entry (each bar: long stop = max(previous stop, high − 3 × ATR)), starting from the initial stop. For PB-H1-GOLD there is also the hard 5R target and the session-end close. |
| **E1, ladder, fixed** | A quarter is closed at each of TP1 = +1R, TP2 = +2R, TP3 = +3R, TP4 = +4R. The stop moves to the entry after TP1, to TP1 after TP2, and to TP2 after TP3. The rest closes at the stop. |
| **E2, ladder, runner** | Quarters at TP1–TP3 as in E1, with the same stop steps. The last quarter rides the baseline trailing stop (never below the stepped stop), with TP4 = +5R as its hard target. |

**Bar order.** Within one bar the stop is checked before the next target (conservative), unless the bar opens
beyond the target, which then fills at the open. A stop gapped through fills at the open. PB-H1-GOLD's
session-end close applies to every exit.

**The user requires four take-profit stages.** The live signals therefore use E1 or E2, whichever has the higher
pooled out-of-sample mean R for that setup. E0 is reported for comparison only.

### 1.4 Costs (charged on every trade, in % of price)

| Class | Round trip | Financing per night held |
|---|---|---|
| FX major | 0.015 | 0.010 |
| FX cross | 0.030 | 0.010 |
| Metals: XAUUSD / XAGUSD | 0.030 / 0.060 | 0.015 |
| Energy | 0.060 | 0.020 |
| Indices | 0.030 | long 0.020, short 0.005 |
| Crypto | 0.250 | 0.060 |

- A trade's result in R = Σ over its parts (fraction × signed move ÷ R) − (round trip + nights × financing) ×
  entry ÷ R.
- PB-H1-GOLD pays the XAUUSD round trip and no financing (it is flat the same day).

### 1.5 Periods

- **In-sample:** 2006-01-01 … 2016-12-31. **Out-of-sample:** 2017-01-01 … the last completed bar.
- **Crypto:** in-sample to 2019-12-31, out-of-sample from 2020-01-01.
- **PB-H1-GOLD** has only about 2.4 years of hourly data, all of which the user's own fit has already seen. It is a
  port check, not a new test.

### 1.6 The gate

Per daily setup (PB-D, BO-D), with its chosen ladder exit:

1. **Setup level, pooled over all instruments:**
   - in-sample mean R > 0;
   - out-of-sample at least 150 trades;
   - out-of-sample profit factor ≥ 1.15;
   - out-of-sample mean R > 0 with a one-sided t-statistic ≥ 1.96 (Bonferroni for two setups).
2. **Class level** (only for a setup that passed level 1): a class's signals are enabled when its out-of-sample
   mean R > 0, its profit factor is ≥ 1.10, and it has at least 30 out-of-sample trades. The classes are FX (majors
   and crosses together), Metals, Energy, Indices and Crypto.

**PB-H1-GOLD** is enabled when its port, with the chosen ladder exit, has profit factor ≥ 1.10 and mean R > 0 on
the available hourly history, AND its E0 result is within the range the user's own tests gave (profit factor
1.05–1.35). Otherwise it is reported and not signalled.

**A setup or class that fails is not traded.** If nothing passes, no CFD signal goes live, and the report says so.

### 1.7 Report

`docs/cfd/BACKTEST_REPORT.md`, written by the research command.

- **Per setup × exit × period** (IS / OOS) and per class:
  - trades;
  - win rate;
  - profit factor;
  - mean R;
  - t-statistic;
  - total R;
  - worst peak-to-trough in R (trades in exit-date order, 1R risked each);
  - mean holding days.
- **The gate table** with pass or fail for every line.
- **`docs/cfd/enabled.json`**, the machine-readable outcome:
  `{"exit": {"PB-D": "E2", …}, "enabled": [["PB-D","FX"], …], "run_at": …}`. The live module reads only this.

## Part 2. Research code (`cfd/`)

- **`cfd/instruments.py`:** the universe table. For each instrument: Yahoo symbol, name, class, cost keys, quote
  currency, and unit label («ед.», «унц.», «барр.», «контр.», «монет»).
- **`cfd/data.py`:** `daily_bars(symbol, *, fetch=None) -> list[Bar]` and `hourly_bars(symbol, *, fetch=None)`.
  `Bar` is (ts, open, high, low, close). It applies the hygiene rules, and the network sits behind `fetch` (the
  default is yfinance). Bars can be cached to `data/cfd_cache/` (git-ignored) for the research run.
- **`cfd/indicators.py`:** `atr`, `ema`, `sma`, `adx`, `highest`, `lowest`. Pure functions, with Wilder's method
  where it applies.
- **`cfd/setups.py`:** `pb_d(bars)`, `bo_d(bars)`, `pb_h1_gold(bars_1h)`. Each returns
  `list[Signal(index, side, stop, atr)]`. Pure.
- **`cfd/exits.py`:** `simulate(bars, signal, exit_kind, *, costs, entry="next_open"|"close", session=None) ->
  Trade`. `Trade` holds parts, R result, entry and exit timestamps, and nights. Pure.
- **`cfd/research.py`:** runs everything in Part 1, applies the gate, and writes the report and `enabled.json`.
  CLI: `python -m cfd.research --run [--refresh]`.
- **Tests** (offline, synthetic bars) cover:
  - every indicator against hand-computed values;
  - each setup's trigger and stop on crafted series, both directions;
  - each exit's ladder steps, stop moves, bar order, gaps, session end, and costs in R;
  - the gate arithmetic on synthetic trade lists;
  - the report writer.

## Part 3. Live signals (built only for what the gate enabled)

### 3.1 Storage

`cfd_signals` table:
- `id`
- `setup`, `instrument`, `side` (long/short)
- `created` (ISO datetime)
- `entry`, `stop0` (initial stop)
- `tp1`, `tp2`, `tp3`, `tp4`
- `r` (distance)
- `stage` (0–4, the last TP reached)
- `stop` (current stop)
- `status` (open/closed)
- `closed_at`
- `result_r`
- `risk_pct`, `risk_eur`, `qty` (at creation; NULL without a balance)
- `note`

kv settings: `cfd_balance_eur`, `cfd_risk_pct` (default 1.0), `cfd_max_open_risk_pct` (default 3.0).

### 3.2 Scanning and tracking

- **Daily setups:** scanned in the daily run, after the positions step. They use completed daily bars. A signal is
  created on the first run after the signal bar closed, with entry = that run's latest price (the next session's
  open isn't known yet; the message says «вход по рынку»). R is measured from the reference entry: the last
  completed close.
- **PB-H1-GOLD** (if enabled) and **the tracker for every open signal:** run in the Telegram agent's loop every 60
  minutes, alongside the Trading 212 sync. The tracker reads completed 1h bars since the last check and applies the
  chosen ladder exit bar by bar: TP hits, stop moves, stop hit, session end for gold. It sends one message per
  event.
- **Limits.** At most one open signal per instrument. A new signal is skipped (logged, not sent) when the open
  risk would exceed `cfd_max_open_risk_pct`, or when two signals of the same class and side are already open.
- **Failures.** A scan or tracker failure is logged and never stops the agent or the daily run.

### 3.3 Sizing

- **Quantity.** With a balance set: `risk_eur = balance × risk_pct / 100` and
  `qty = risk_eur ÷ (r × quote→EUR rate)` (via `fx.to_eur`). It is shown with the instrument's unit, and for FX
  also in lots (qty ÷ 100 000). Quantities are rounded down to a sensible step:

  | Instrument | Step |
  |---|---|
  | FX | 100 units |
  | Gold | 0.01 oz |
  | Indices | 0.01 |
  | Oil | 0.1 |
  | Silver | 1 oz |
  | Crypto | 0.0001 |

- **Without a balance** the signal shows the risk percent and «на €1 000 баланса: объём …».
- **The result of every event** is in R, and in euros when a balance was set at creation
  (`result_r × risk_eur`).

### 3.4 Messages (the user's style, one message per event)

```
🟢 <b>XAUUSD!</b>: покупка по 4 461,80 — стоп 4 449,10; TP1 4 474,50 · TP2 4 487,20 · TP3 4 499,90 · TP4 4 512,60; риск 1% = €5,00, объём 0,39 унц.
🟢 <b>XAUUSD!</b>: взят TP1 по 4 474,50 — закрыта четверть, стоп в безубыток, итог <b>+€1,25</b>
🟢 <b>XAUUSD!</b>: взят TP2 по 4 487,20 — закрыта четверть, стоп на TP1, итог <b>+€3,75</b>
🔴 <b>XAUUSD!</b>: сработал исходный стоп — закрыта вся позиция по 4 449,10, итог <b>−€5,00</b>
🟢 <b>XAUUSD!</b>: сработал стоп — закрыты части TP3, TP4 по 4 474,50, итог <b>+€6,25</b>
🟢 <b>XAUUSD!</b>: взят TP4 по 4 512,60 — позиция закрыта, итог <b>+€12,50</b>
⚪ <b>XAUUSD!</b>: конец сессии — остаток закрыт по 4 470,00, итог <b>+€2,10</b>
```

- A sell reads «продажа по …».
- «итог» is the cumulative result of the whole signal so far. The dot follows its sign.
- Without a balance the result is in R («итог <b>+1,25R</b>»).
- Prices use the instrument's precision: FX 5 decimals (JPY pairs 3), others 2.

### 3.5 Commands

- **`/cfd`:** open signals (stage, current stop, result so far) and the record of closed ones: count, win rate,
  mean R, total R (and € when sized), marked «гипотетически: если брать каждый сигнал».
- **`/cfd balance 500`:** set the CFD balance in EUR.
- **`/cfd risk 1`:** set the percent risked per signal (0.1–5).
- **`/cfd maxrisk 3`:** set the cap on open risk.
- **`/cfd off` / `/cfd on`:** pause or resume new signals. Open ones are still tracked.
- **HELP_TEXT** gets one line. The menu gets «5) CFD-сигналы», the same text as `/cfd`.

### 3.6 Carry strategy

`carry_strategy.py` keeps working as is. Its EUR/USD message is unchanged in this change.

## Tests (live part, offline)

- **Scanner:** creates a signal only for an enabled (setup, class), with one per instrument, and applies the
  risk-cap and same-class-side skips.
- **Sizing:** for an FX pair quoted in USD, a JPY pair, gold, an index quoted in EUR/GBP/JPY, and crypto. Also
  rounding down, and the no-balance text.
- **Tracker:** every event of both ladders on crafted hourly bars, including a gap through the stop, TP and stop
  in one bar (stop first), session end, and several events in one check (one message each, in order). A restart
  resumes from the stored stage.
- **Messages:** exact strings for every event, buy and sell, with and without a balance, HTML-escaped.
- **Commands:** `/cfd` views and setters with validation.
- **Robustness:** the agent loop runs the tracker hourly and survives an exception. The daily run scans after the
  positions step and survives an exception.
- **Disabled state:** with `enabled.json` missing or empty, there are no signals, and `/cfd` says «CFD-сигналы не
  включены: ни одна стратегия не прошла проверку».
