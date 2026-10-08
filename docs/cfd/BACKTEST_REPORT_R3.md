# CFD backtest report, round 3 (forex)

Run at 2026-10-08T16:47:04+00:00. Source: Yahoo (yfinance), daily bars through 2026-10-07; FRED, OECD 3-month interbank rates through 2026-08. Pre-registration: docs/cfd/PREREGISTRATION_R3.md.

## Outcome

**No hypothesis passed the gate: nothing from round 3 goes live.**

| Hypothesis | Gate | Out-of-sample sample | PF | Mean | t |
| --- | --- | --- | --- | --- | --- |
| H6 FX-REV | FAIL | 447 trades | 0.57 | -0.199 R | -4.97 |
| H7 CARRY-BASKET | FAIL | 117 months | 0.87 | -0.083% | -0.59 |

## Gate

Each hypothesis on its own; every line is pass or fail. A hypothesis that fails is not traded, and there is no further round on this data.

### FX-REV (judged on E0c)

| Rule | Value | Threshold | Result |
| --- | --- | --- | --- |
| out-of-sample trades | 447 | >= 100 | PASS |
| out-of-sample profit factor | 0.5695 | >= 1.10 | FAIL |
| out-of-sample mean R | -0.1987 | > 0 | FAIL |
| out-of-sample t-statistic | -4.9698 | >= 2.45 | FAIL |
| in-sample mean R | -0.0650 (512 trades) | > 0 | FAIL |

### CARRY-BASKET (judged on monthly)

| Rule | Value | Threshold | Result |
| --- | --- | --- | --- |
| out-of-sample months | 117 | >= 100 | PASS |
| out-of-sample profit factor | 0.8698 | >= 1.10 | FAIL |
| out-of-sample mean month | -0.0829% | > 0 | FAIL |
| out-of-sample t-statistic | -0.5893 | >= 2.45 | FAIL |
| in-sample mean month | -0.2572% (132 months) | > 0 | FAIL |

## FX-REV

H6: range reversion in pairs of linked economies.

Signal at the close of bar t, when ADX(14) is under 20: long when the close is under SMA20 - 2.0 x SD20, short when it is over SMA20 + 2.0 x SD20 (SD20 the population standard deviation of the last 20 closes). Entry at the open of bar t+1; stop 2.0 ATR(14) from the signal close, R measured from the actual entry. Exit, the earliest of: the stop; the next open after the first close at or beyond SMA20 (long: close >= SMA20; short: close <= SMA20); the open of the 16th bar after entry. Costs: round trip 0.030 % (USDCAD 0.015 %; EURNOK and EURSEK 0.060 %) and 0.010 % a night. Pooled over the eight pairs. In-sample 2006-2016, out-of-sample from 2017.

### Pooled, by period

| Period | Trades | Win | PF | Mean R | t | Total R | Max DD (R) | Hold (days) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| IS | 512 | 51.2% | 0.84 | -0.065 | -1.65 | -33.3 | 35.8 | 11.1 |
| OOS | 447 | 47.4% | 0.57 | -0.199 | -4.97 | -88.8 | 88.8 | 11.0 |

### By pair (for information, not a gate)

| Pair | Period | Trades | Win | PF | Mean R | t | Total R | Max DD (R) | Hold (days) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| EURCHF | IS | 55 | 47.3% | 0.71 | -0.125 | -1.03 | -6.9 | 8.8 | 11.4 |
| EURCHF | OOS | 46 | 43.5% | 0.40 | -0.318 | -2.49 | -14.6 | 14.6 | 11.7 |
| EURGBP | IS | 71 | 52.1% | 0.97 | -0.010 | -0.09 | -0.7 | 7.4 | 10.1 |
| EURGBP | OOS | 61 | 60.7% | 1.05 | +0.018 | 0.15 | +1.1 | 3.4 | 10.9 |
| AUDNZD | IS | 52 | 44.2% | 0.64 | -0.195 | -1.44 | -10.1 | 12.4 | 12.4 |
| AUDNZD | OOS | 50 | 46.0% | 0.58 | -0.197 | -1.59 | -9.8 | 11.5 | 11.8 |
| AUDCAD | IS | 71 | 43.7% | 0.61 | -0.181 | -1.83 | -12.8 | 12.9 | 11.8 |
| AUDCAD | OOS | 68 | 57.4% | 0.93 | -0.022 | -0.25 | -1.5 | 5.4 | 9.7 |
| NZDCAD | IS | 65 | 47.7% | 0.73 | -0.122 | -1.14 | -8.0 | 11.2 | 10.4 |
| NZDCAD | OOS | 49 | 42.9% | 0.58 | -0.214 | -1.60 | -10.5 | 13.5 | 11.9 |
| USDCAD | IS | 63 | 63.5% | 1.62 | +0.182 | 1.65 | +11.4 | 3.4 | 11.9 |
| USDCAD | OOS | 68 | 36.8% | 0.32 | -0.353 | -3.90 | -24.0 | 24.0 | 10.8 |
| EURNOK | IS | 70 | 50.0% | 0.72 | -0.126 | -1.20 | -8.8 | 10.3 | 9.3 |
| EURNOK | OOS | 55 | 43.6% | 0.44 | -0.297 | -2.64 | -16.4 | 17.2 | 10.1 |
| EURSEK | IS | 65 | 60.0% | 1.12 | +0.041 | 0.36 | +2.6 | 7.0 | 12.3 |
| EURSEK | OOS | 50 | 46.0% | 0.49 | -0.262 | -2.15 | -13.1 | 15.3 | 11.7 |

## CARRY-BASKET

H7: the three highest-yielding majors against the three lowest, monthly.

Currencies: USD, EUR, GBP, JPY, AUD, CAD, CHF, NZD, ranked on the first daily bar of each calendar month by the OECD 3-month rate in force that day (month M usable from the 15th of M+1); a currency with no rate is left out and at least six are needed. Long the top three, short the bottom three, equal weights, held to the first bar of the next month. A leg's return against USD = its spot change over the month + (its rate - the USD rate) / 12 / 100; a month = mean(long legs) - mean(short legs). Costs per month: 0.015 % x 2 for every leg that enters or leaves the basket, weighted 1/3, and a financing markup of 0.004 % a night on each of the six legs, weighted 1/3 (0.008 % x the nights in the month). In-sample 2006-2016, out-of-sample from 2017, by the month's first day.

### Pooled, by period

| Period | Months | Win | PF | Mean month | t | Total | Worst peak-to-trough |
| --- | --- | --- | --- | --- | --- | --- | --- |
| IS | 132 | 41.7% | 0.77 | -0.257% | -1.08 | -33.9% | 40.7% |
| OOS | 117 | 58.1% | 0.87 | -0.083% | -0.59 | -9.7% | 17.3% |

### By year (for information, not a gate)

| Year | Months | Win | PF | Mean month | t | Total | Worst peak-to-trough |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2006 | 12 | 50.0% | 1.17 | +0.121% | 0.22 | +1.5% | 6.4% |
| 2007 | 12 | 50.0% | 1.18 | +0.157% | 0.23 | +1.9% | 6.4% |
| 2008 | 12 | 16.7% | 0.14 | -2.085% | -2.00 | -25.0% | 23.9% |
| 2009 | 12 | 66.7% | 2.73 | +1.214% | 1.11 | +14.6% | 6.3% |
| 2010 | 12 | 41.7% | 0.93 | -0.088% | -0.10 | -1.1% | 7.0% |
| 2011 | 12 | 33.3% | 0.84 | -0.172% | -0.23 | -2.1% | 9.1% |
| 2012 | 12 | 41.7% | 1.29 | +0.220% | 0.32 | +2.6% | 6.6% |
| 2013 | 12 | 16.7% | 0.36 | -0.755% | -1.25 | -9.1% | 10.3% |
| 2014 | 12 | 58.3% | 0.74 | -0.187% | -0.36 | -2.2% | 4.4% |
| 2015 | 12 | 41.7% | 0.48 | -0.916% | -0.98 | -11.0% | 14.7% |
| 2016 | 12 | 41.7% | 0.67 | -0.338% | -0.54 | -4.1% | 8.3% |
| 2017 | 12 | 50.0% | 0.60 | -0.367% | -0.77 | -4.4% | 7.3% |
| 2018 | 12 | 50.0% | 0.58 | -0.415% | -0.70 | -5.0% | 5.1% |
| 2019 | 12 | 58.3% | 0.96 | -0.017% | -0.04 | -0.2% | 4.9% |
| 2020 | 12 | 50.0% | 0.60 | -0.284% | -0.66 | -3.4% | 7.0% |
| 2021 | 12 | 58.3% | 1.37 | +0.190% | 0.43 | +2.3% | 2.6% |
| 2022 | 12 | 50.0% | 0.81 | -0.184% | -0.31 | -2.2% | 6.4% |
| 2023 | 12 | 58.3% | 0.88 | -0.072% | -0.19 | -0.9% | 2.9% |
| 2024 | 12 | 83.3% | 2.10 | +0.360% | 0.87 | +4.3% | 3.9% |
| 2025 | 12 | 50.0% | 0.50 | -0.344% | -0.89 | -4.1% | 6.8% |
| 2026 | 9 | 77.8% | 7.58 | +0.434% | 2.58 | +3.9% | 0.3% |

Mean cost per month 0.246%, mean legs entering or leaving per month 0.28, 249 months from 2006-01-02 to 2026-09-01.

## Trade accounting

| Hypothesis | Exit | Signals | Closed | Skipped (R rules) | Open at end of data | Blocked (a trade was open) |
| --- | --- | --- | --- | --- | --- | --- |
| FX-REV | E0c | 1837 | 959 | 5 | 0 | 873 |

## Data

| Instrument | Symbol | Interval | Bars | First | Last | Dropped | Fetched |
| --- | --- | --- | --- | --- | --- | --- | --- |
| EURCHF | EURCHF=X | 1d | 6150 | 2003-01-23 | 2026-10-07 | 3 (missing 0, inverted 0, wick 2, duplicate 0, unparsable 0, forming 1) | 2026-10-08 |
| EURGBP | EURGBP=X | 1d | 6968 | 2000-01-03 | 2026-10-07 | 2 (missing 0, inverted 0, wick 1, duplicate 0, unparsable 0, forming 1) | 2026-10-08 |
| AUDNZD | AUDNZD=X | 1d | 5948 | 2003-12-01 | 2026-10-07 | 1 (missing 0, inverted 0, wick 0, duplicate 0, unparsable 0, forming 1) | 2026-10-08 |
| AUDCAD | AUDCAD=X | 1d | 5948 | 2003-12-01 | 2026-10-07 | 2 (missing 0, inverted 0, wick 1, duplicate 0, unparsable 0, forming 1) | 2026-10-08 |
| NZDCAD | NZDCAD=X | 1d | 5943 | 2003-12-01 | 2026-10-07 | 3 (missing 0, inverted 0, wick 2, duplicate 0, unparsable 0, forming 1) | 2026-10-08 |
| USDCAD | USDCAD=X | 1d | 5997 | 2003-09-17 | 2026-10-07 | 1 (missing 0, inverted 0, wick 0, duplicate 0, unparsable 0, forming 1) | 2026-10-08 |
| EURNOK | EURNOK=X | 1d | 6888 | 2000-03-15 | 2026-10-07 | 8 (missing 0, inverted 0, wick 7, duplicate 0, unparsable 0, forming 1) | 2026-10-08 |
| EURSEK | EURSEK=X | 1d | 6944 | 2000-01-03 | 2026-10-07 | 3 (missing 0, inverted 0, wick 2, duplicate 0, unparsable 0, forming 1) | 2026-10-08 |
| EURUSD | EURUSD=X | 1d | 5928 | 2003-12-01 | 2026-10-07 | 2 (missing 0, inverted 0, wick 1, duplicate 0, unparsable 0, forming 1) | 2026-10-08 |
| GBPUSD | GBPUSD=X | 1d | 5940 | 2003-12-01 | 2026-10-07 | 2 (missing 0, inverted 0, wick 1, duplicate 0, unparsable 0, forming 1) | 2026-10-08 |
| AUDUSD | AUDUSD=X | 1d | 5304 | 2006-05-16 | 2026-10-07 | 2 (missing 0, inverted 0, wick 1, duplicate 0, unparsable 0, forming 1) | 2026-10-08 |
| NZDUSD | NZDUSD=X | 1d | 5927 | 2003-12-01 | 2026-10-07 | 4 (missing 0, inverted 0, wick 3, duplicate 0, unparsable 0, forming 1) | 2026-10-08 |
| USDJPY | USDJPY=X | 1d | 6949 | 2000-01-03 | 2026-10-07 | 1 (missing 0, inverted 0, wick 0, duplicate 0, unparsable 0, forming 1) | 2026-10-08 |
| USDCHF | USDCHF=X | 1d | 5994 | 2003-09-17 | 2026-10-07 | 2 (missing 0, inverted 0, wick 1, duplicate 0, unparsable 0, forming 1) | 2026-10-08 |

## Rates

OECD 3-month interbank rates, monthly, from FRED. The value for month M is used from the 15th of month M+1; a month with no value carries the last value forward.

| Currency | Series | Observations | First month | Last month | Fetched | Status |
| --- | --- | --- | --- | --- | --- | --- |
| USD | IR3TIB01USM156N | 746 | 1964-06 | 2026-08 | 2026-10-08 | ok |
| EUR | IR3TIB01EZM156N | 385 | 1994-01 | 2026-01 | 2026-10-08 | ok |
| GBP | IR3TIB01GBM156N | 829 | 1957-01 | 2026-01 | 2026-10-08 | ok |
| JPY | IR3TIB01JPM156N | 292 | 2002-04 | 2026-07 | 2026-10-08 | ok |
| AUD | IR3TIB01AUM156N | 704 | 1968-01 | 2026-08 | 2026-10-08 | ok |
| CAD | IR3TIB01CAM156N | 848 | 1956-01 | 2026-08 | 2026-10-08 | ok |
| CHF | IR3TIB01CHM156N | 326 | 1999-07 | 2026-08 | 2026-10-08 | ok |
| NZD | IR3TIB01NZM156N | 633 | 1973-12 | 2026-08 | 2026-10-08 | ok |

## Notes

- H6 results are in R after costs: one round trip per trade plus financing for every calendar night held; each pair pays its own round trip (0.030 %, USDCAD 0.015 %, EURNOK and EURSEK 0.060 %) and 0.010 % a night. H7 results are in % of the basket's notional per month, after the costs listed above.
- Everything the pre-registration does not restate is rounds 1-2's: data hygiene (15 % wick rule), ATR(14), the bar order (stop before any other exit within a bar), gaps filling at the open, the skip rule (R <= 0.25 ATR, or R > 6 ATR), one open trade per pair, and 300 bars of history before a first signal.
- H6's time stop of 16 bars leaves at the open of the 16th bar after the entry bar (the bar whose open is the entry); the SMA20 condition is read at each close from the entry bar's own close on and leaves at the next bar's open. Both fill at that open, before the bar's own stop; the earlier of the two wins. The stop is fixed at the signal (the close -/+ 2.0 ATR) and does not trail.
- H7: the rebalance day is the first day of each calendar month on which any of the seven USD pairs has a bar; a pair that lacks that day is priced at its latest bar before it. A currency with no rate, or whose pair has no price covering the month, is left out of that month's ranking; fewer than six left means no basket and no month. Ties in the rate go to the earlier of USD, EUR, GBP, JPY, AUD, CAD, CHF, NZD. A leg that changes side counts as one leaving and one entering. The month still running when the data ends is not a month. The worst peak-to-trough is taken on the compounded curve of the months.
- In-sample and out-of-sample are split by the signal date (H6) or the month's first day (H7); earlier ones are warm-up only. The in-sample line of the gate is always required.
- The t-statistics pool trades across pairs and treat them as independent (trades that overlap in time in correlated pairs make the true uncertainty larger); H7's months overlap in nothing but share the dollar. The bar of 2.45 is Bonferroni for the seven hypotheses of the three rounds.
- The gate judges each hypothesis on its pooled sample. A hypothesis that passes is enabled for its whole universe; there is no per-pair picking, a hypothesis that fails is not traded, and there is no further round on this data.
