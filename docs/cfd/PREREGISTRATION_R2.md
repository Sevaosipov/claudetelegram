# CFD research, round 2: pre-registration

Written 2026-10-05, after round 1 (BACKTEST_REPORT.md) failed every gate line and before any round-2 code or run
exists. The user's requirement: "we need strategy with positive profit factor above 1.1".

Searching until something clears 1.1 always finds something. So this round has **three hypotheses and no more**.
Each has a mechanism and prior evidence. The rules are fixed here, they are judged on data they weren't shaped on,
and the significance bar is raised for the number of tries so far (five hypotheses across both rounds).

Everything not restated here is as in docs/superpowers/specs/2026-10-05-cfd-signals.md, Part 1:
- data hygiene;
- indicators (ATR(14), Wilder);
- the bar-order rule (stop before target within a bar);
- gap handling;
- the skip rule (R ≤ 0.25 × ATR or R > 6 × ATR; for H5 the upper bound is 8 × ATR, since its stop is 6 × ATR by
  construction);
- one open trade per instrument per setup;
- the result in R after costs;
- the report columns.

## Hypotheses

### H3. CR-BO: trend breakout on coins it has never been run on

**Why.** Round 1's only positive class was BO-D on BTC and ETH, but that class was picked after seeing results. The
honest test of "trend breakouts work on crypto" is other coins. Crypto time-series momentum is documented in the
literature.

**Universe.** The eleven coins the bot follows besides BTC and ETH, daily bars from Yahoo:

| Coin | Yahoo symbol |
|---|---|
| SOL | `SOL-USD` |
| XRP | `XRP-USD` |
| BNB | `BNB-USD` |
| DOGE | `DOGE-USD` |
| AVAX | `AVAX-USD` |
| LTC | `LTC-USD` |
| LINK | `LINK-USD` |
| TRX | `TRX-USD` |
| ENA | `ENA-USD` |
| HYPE | `HYPE32196-USD` |
| SUI | `SUI20947-USD` |

A coin needs 300 bars before its first signal, as in round 1.

**Setup.** BO-D exactly as in round 1:
- close above the highest high of the previous 55 bars and above SMA200 (mirror for short);
- entry at the next open;
- stop 2.5 × ATR from the signal close.

**Exits.** E0, E1 and E2 exactly as in round 1.

**Costs.** Round trip 0.40 % (alts trade wider than bitcoin). Financing 0.06 % per night.

**Periods.** In-sample to 2019-12-31, out-of-sample from 2020-01-01.

### H4. IDX-DIP: buying a sharp dip in a rising equity index (long only)

**Why.** Short-term reversal in equity indices inside an uptrend is one of the most replicated effects in daily
data. It holds for days, not weeks, so overnight financing is small.

**Universe.** `^GSPC`, `^NDX`, `^DJI`, `^GDAXI`, `^FTSE`, `^N225`.

**Signal** at the close of bar t: close > SMA200 and RSI(2) < 10. RSI is Wilder's, on closes.

**Entry** at the open of bar t+1.

**Stop** = signal close − 2.5 × ATR. R is measured from the actual entry.

**Exits:**
- **E0c** (the classic one): leave at the next open after the first close above SMA5, or at the open of the 11th
  bar after entry (time stop), or at the stop.
- **EL** (four take-profits):
  - a quarter at each of +0.5R, +1.0R, +1.5R, +2.0R;
  - the stop stays at its initial level until TP2, moves to the entry after TP2, and to TP1 after TP3;
  - whatever is left closes at the open of the 16th bar after entry.

**Costs.** The Indices row of the round-1 table.

**Periods.** In-sample 2006–2016, out-of-sample 2017 onward.

### H5. CARRY-FX: the user's carry-gated trend, on every FX pair, not re-tuned

**Why.** It is the one mechanism the user's research validated (EUR/USD, profit factor 1.9 on 15 trades). One pair
gives too few trades to judge. Eleven pairs give a real sample of the same frozen rule.

**Universe.** The eleven FX pairs of round 1.

**Rates.** OECD 3-month interbank rates, monthly, from FRED (`fredgraph.csv`):

| Currency | FRED series |
|---|---|
| USD | `IR3TIB01USM156N` |
| EUR | `IR3TIB01EZM156N` |
| GBP | `IR3TIB01GBM156N` |
| JPY | `IR3TIB01JPM156N` |
| AUD | `IR3TIB01AUM156N` |
| CAD | `IR3TIB01CAM156N` |
| CHF | `IR3TIB01CHM156N` |
| NZD | `IR3TIB01NZM156N` |

- The value for month M is used from the 15th of month M+1 (publication lag: no look-ahead).
- A missing later month carries the last value forward.

**State** at the close of bar t:
- LONG when close > 1.025 × SMA200 and rate(base) > rate(quote);
- SHORT when close < 0.975 × SMA200 and rate(base) < rate(quote);
- otherwise FLAT.

These are the frozen numbers of `carry_strategy.py`: 2.5 % and 200 days.

**Entry** at the open of bar t+1 on a change from FLAT (or from the opposite side) into LONG or SHORT.

**Stop** = entry ∓ 6 × ATR, so R = 6 × ATR.

**Exits:**
- **E0c** (the original): leave at the next open after the state stops being the trade's side, or at a 6 × ATR
  trailing stop that never loosens.
- **EL:** as in H4 (quarters at +0.5R, +1.0R, +1.5R, +2.0R; stop to entry after TP2, to TP1 after TP3). The
  remainder also leaves at the next open after the state stops being the trade's side. No time stop.

**Costs.** The round-1 FX round trips. Financing is 0.004 % per night, about 1.5 % a year. The trade always holds
the higher-yielding currency, so in practice it earns the rate difference less the broker's markup. Charging the
markup with no credit for the carry is conservative.

**Periods.** In-sample 2006–2016, out-of-sample 2017 onward.

## The gate (each hypothesis on its own)

**Live exit.** The live signals need four take-profit stages. A hypothesis is therefore judged on its four-stage
exit: E1 or E2 for H3, whichever has the higher out-of-sample mean R; EL for H4 and H5. E0 and E0c are reported
beside it.

| Rule | Bar |
|---|---|
| Out-of-sample trades | H3 and H4: at least 100. H5: at least 40. |
| Out-of-sample profit factor, after costs | ≥ 1.10 |
| Out-of-sample mean R | > 0, with a one-sided t-statistic ≥ 2.33 (Bonferroni for five hypotheses at 5 %) |
| In-sample mean R | > 0, when the in-sample period has at least 30 trades (otherwise reported, not required) |

- **A hypothesis that passes** is enabled for its whole universe. For H3 that also covers BTC and ETH, on the
  round-1 evidence. There is no per-instrument picking.
- **A hypothesis that fails is not traded.** There is no round 3 on the same data: a further idea would need new
  data (a forward paper record) to be judged on.

## Outputs

- `docs/cfd/BACKTEST_REPORT_R2.md`, in the same shape as round 1: the pooled table per exit and period, a
  per-instrument table (for information), and the gate table.
- `docs/cfd/enabled.json` gains the round-2 outcome:
  `{"round2": {"enabled": ["CR-BO", …], "exit": {"CR-BO": "E1", …}, "run_at": …}}`. The round-1 keys stay as they
  are.

## Amendments before the first run (2026-10-05, no round-2 backtest has been executed)

The builder found three places where the text above was ambiguous or would distort the data. They are settled
here, before any result exists:

- **A1. H5's hold condition is the original's.** "The state stops being the trade's side" means the hold condition
  of `carry_strategy.py`:
  - a long is held while close > SMA200 and rate(base) > rate(quote);
  - a short is held while close < SMA200 and rate(base) < rate(quote).

  The 2.5 % band applies to entries only. H5 is defined as the user's rule, not re-tuned; leaving on a fall back
  inside the band would be a different rule.
- **A2. H5 uses ATR(20)** for its stop and its trailing stop: the original's `ATR_LEN`. H3 and H4 keep ATR(14).
- **A3. Crypto hygiene.** The rule that drops a bar whose high or low is more than 15 % from its close was written
  for bad FX ticks. Alt-coins move that far on real days, and those are the breakout days under test. For crypto
  symbols the threshold is 60 %. Every other hygiene rule is unchanged.
