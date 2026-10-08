# CFD backtest report, round 2

Run at 2026-10-08T15:54:54+00:00. Source: Yahoo (yfinance), daily bars through 2026-10-07; FRED, OECD 3-month interbank rates through 2026-08. Pre-registration: docs/cfd/PREREGISTRATION_R2.md.

## Outcome

**No hypothesis passed the gate: nothing from round 2 goes live.**

| Hypothesis | Judged exit | Gate | Out-of-sample trades | PF | Mean R | t |
| --- | --- | --- | --- | --- | --- | --- |
| H3 CR-BO | E2 | FAIL | 391 | 0.69 | -0.153 | -3.11 |
| H4 IDX-DIP | EL | FAIL | 438 | 1.00 | +0.001 | 0.03 |
| H5 CARRY-FX | EL | FAIL | 134 | 0.57 | -0.145 | -2.87 |

## Gate

Each hypothesis on its own; every line is pass or fail. A hypothesis that fails is not traded, and there is no round 3 on this data.

### CR-BO (judged exit E2)

| Rule | Value | Threshold | Result |
| --- | --- | --- | --- |
| out-of-sample trades | 391 | >= 100 | PASS |
| out-of-sample profit factor | 0.6853 | >= 1.10 | FAIL |
| out-of-sample mean R | -0.1526 | > 0 | FAIL |
| out-of-sample t-statistic | -3.1063 | >= 2.33 | FAIL |
| in-sample mean R | -0.1097 (82 trades) | > 0 (required from 30 trades) | FAIL |

### IDX-DIP (judged exit EL)

| Rule | Value | Threshold | Result |
| --- | --- | --- | --- |
| out-of-sample trades | 438 | >= 100 | PASS |
| out-of-sample profit factor | 1.0033 | >= 1.10 | FAIL |
| out-of-sample mean R | +0.0012 | > 0 | PASS |
| out-of-sample t-statistic | 0.0309 | >= 2.33 | FAIL |
| in-sample mean R | -0.0289 (420 trades) | > 0 (required from 30 trades) | FAIL |

### CARRY-FX (judged exit EL)

| Rule | Value | Threshold | Result |
| --- | --- | --- | --- |
| out-of-sample trades | 134 | >= 40 | PASS |
| out-of-sample profit factor | 0.5687 | >= 1.10 | FAIL |
| out-of-sample mean R | -0.1452 | > 0 | FAIL |
| out-of-sample t-statistic | -2.8747 | >= 2.33 | FAIL |
| in-sample mean R | -0.0551 (132 trades) | > 0 (required from 30 trades) | FAIL |

## CR-BO

H3: trend breakout (BO-D) on the eleven alt coins.

BO-D exactly as in round 1: the close above the highest high of the previous 55 bars and above SMA200 (mirrored for a short), entry at the next open, stop 2.5 ATR from the signal close. E0, E1 and E2 exactly as in round 1. Costs: round trip 0.40 %, financing 0.06 % a night. In-sample to 2019-12-31, out-of-sample from 2020-01-01. Judged on E1 or E2, whichever has the higher out-of-sample mean R.

### Pooled, by exit and period (* = the judged exit, E2)

| Exit | Period | Trades | Win | PF | Mean R | t | Total R | Max DD (R) | Hold (days) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| E0 | IS | 70 | 42.9% | 1.52 | +0.223 | 1.32 | +15.6 | 6.4 | 15.8 |
| E0 | OOS | 357 | 40.1% | 1.60 | +0.265 | 2.08 | +94.7 | 30.3 | 16.7 |
| E1 | IS | 81 | 49.4% | 0.74 | -0.138 | -1.16 | -11.2 | 13.3 | 20.4 |
| E1 | OOS | 395 | 51.9% | 0.67 | -0.162 | -3.27 | -63.8 | 74.7 | 22.3 |
| E2 * | IS | 82 | 50.0% | 0.79 | -0.110 | -0.91 | -9.0 | 13.3 | 19.3 |
| E2 * | OOS | 391 | 52.7% | 0.69 | -0.153 | -3.11 | -59.7 | 72.1 | 22.0 |

### By instrument (exit E2; for information, not a gate)

| Instrument | Class | Period | Trades | Win | PF | Mean R | t | Total R | Max DD (R) | Hold (days) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| SOLUSD | Crypto | IS | 0 | – | – | – | – | – | – | – |
| SOLUSD | Crypto | OOS | 46 | 60.9% | 1.16 | +0.069 | 0.43 | +3.2 | 6.9 | 16.7 |
| XRPUSD | Crypto | IS | 7 | 42.9% | 0.23 | -0.470 | -1.65 | -3.3 | 3.9 | 38.4 |
| XRPUSD | Crypto | OOS | 50 | 50.0% | 0.79 | -0.110 | -0.71 | -5.5 | 11.3 | 12.8 |
| BNBUSD | Crypto | IS | 11 | 72.7% | 2.28 | +0.389 | 1.10 | +4.3 | 1.3 | 18.7 |
| BNBUSD | Crypto | OOS | 47 | 55.3% | 1.00 | +0.002 | 0.01 | +0.1 | 9.2 | 20.2 |
| DOGEUSD | Crypto | IS | 9 | 22.2% | 0.27 | -0.639 | -1.80 | -5.8 | 6.6 | 21.0 |
| DOGEUSD | Crypto | OOS | 55 | 61.8% | 0.60 | -0.166 | -1.53 | -9.1 | 11.8 | 10.3 |
| AVAXUSD | Crypto | IS | 0 | – | – | – | – | – | – | – |
| AVAXUSD | Crypto | OOS | 21 | 71.4% | 1.34 | +0.078 | 0.45 | +1.6 | 1.6 | 46.3 |
| LTCUSD | Crypto | IS | 38 | 55.3% | 0.93 | -0.033 | -0.19 | -1.2 | 7.3 | 12.8 |
| LTCUSD | Crypto | OOS | 35 | 48.6% | 0.41 | -0.326 | -2.26 | -11.4 | 12.1 | 42.3 |
| LINKUSD | Crypto | IS | 12 | 41.7% | 0.64 | -0.221 | -0.68 | -2.7 | 4.3 | 13.2 |
| LINKUSD | Crypto | OOS | 51 | 47.1% | 0.60 | -0.219 | -1.50 | -11.2 | 16.3 | 24.1 |
| TRXUSD | Crypto | IS | 5 | 40.0% | 0.87 | -0.066 | -0.12 | -0.3 | 2.5 | 54.6 |
| TRXUSD | Crypto | OOS | 59 | 40.7% | 0.35 | -0.375 | -3.49 | -22.1 | 23.3 | 14.2 |
| ENAUSD | Crypto | IS | 0 | – | – | – | – | – | – | – |
| ENAUSD | Crypto | OOS | 3 | 33.3% | 0.30 | -0.541 | -0.86 | -1.6 | 2.3 | 192.7 |
| HYPEUSD | Crypto | IS | 0 | – | – | – | – | – | – | – |
| HYPEUSD | Crypto | OOS | 6 | 66.7% | 0.78 | -0.080 | -0.22 | -0.5 | 2.2 | 15.7 |
| SUIUSD | Crypto | IS | 0 | – | – | – | – | – | – | – |
| SUIUSD | Crypto | OOS | 18 | 44.4% | 0.67 | -0.176 | -0.69 | -3.2 | 5.2 | 28.2 |

## IDX-DIP

H4: buying a sharp dip in a rising equity index (long only).

Signal at the close of bar t: close above SMA200 and RSI(2) under 10; entry at the open of bar t+1; stop 2.5 ATR below the signal close. E0c: leave at the next open after the first close above SMA5, at the open of the 11th bar after entry, or at the stop. EL: a quarter at each of +0.5R, +1R, +1.5R, +2R, the stop at its initial level until TP2, at the entry after TP2 and at TP1 after TP3, what is left at the open of the 16th bar after entry. Costs: the round-1 Indices row. In-sample 2006-2016, out-of-sample from 2017. Judged on EL.

### Pooled, by exit and period (* = the judged exit, EL)

| Exit | Period | Trades | Win | PF | Mean R | t | Total R | Max DD (R) | Hold (days) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| E0c | IS | 522 | 66.9% | 1.29 | +0.059 | 2.28 | +30.9 | 10.0 | 4.3 |
| E0c | OOS | 531 | 65.0% | 1.16 | +0.037 | 1.29 | +19.4 | 28.0 | 4.0 |
| EL * | IS | 420 | 56.0% | 0.93 | -0.029 | -0.70 | -12.1 | 26.3 | 16.3 |
| EL * | OOS | 438 | 56.6% | 1.00 | +0.001 | 0.03 | +0.5 | 34.5 | 15.5 |

### By instrument (exit EL; for information, not a gate)

| Instrument | Class | Period | Trades | Win | PF | Mean R | t | Total R | Max DD (R) | Hold (days) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| US500 | Indices | IS | 72 | 55.6% | 0.92 | -0.032 | -0.33 | -2.3 | 6.5 | 17.2 |
| US500 | Indices | OOS | 77 | 58.4% | 1.22 | +0.075 | 0.79 | +5.8 | 5.1 | 16.4 |
| US100 | Indices | IS | 79 | 64.6% | 1.20 | +0.071 | 0.73 | +5.6 | 5.4 | 17.1 |
| US100 | Indices | OOS | 79 | 64.6% | 1.20 | +0.067 | 0.72 | +5.3 | 7.8 | 14.8 |
| US30 | Indices | IS | 77 | 61.0% | 1.08 | +0.029 | 0.31 | +2.2 | 7.9 | 16.2 |
| US30 | Indices | OOS | 82 | 57.3% | 1.06 | +0.024 | 0.26 | +2.0 | 9.8 | 15.2 |
| DE40 | Indices | IS | 71 | 49.3% | 0.85 | -0.066 | -0.62 | -4.7 | 6.9 | 15.5 |
| DE40 | Indices | OOS | 65 | 44.6% | 0.64 | -0.163 | -1.59 | -10.6 | 13.9 | 15.3 |
| UK100 | Indices | IS | 63 | 54.0% | 0.77 | -0.090 | -0.93 | -5.7 | 9.6 | 16.6 |
| UK100 | Indices | OOS | 70 | 47.1% | 0.56 | -0.205 | -2.14 | -14.4 | 17.2 | 15.2 |
| JP225 | Indices | IS | 58 | 48.3% | 0.74 | -0.125 | -1.07 | -7.3 | 11.1 | 14.7 |
| JP225 | Indices | OOS | 65 | 66.2% | 1.65 | +0.191 | 1.81 | +12.4 | 4.2 | 16.2 |

## CARRY-FX

H5: the carry-gated trend on the eleven FX pairs.

Entry state at each close: long when the close is above 1.025 x SMA200 and rate(base) is above rate(quote), short when it is below 0.975 x SMA200 and rate(base) is below rate(quote), otherwise flat. Entry at the next open on a change from flat (or from the opposite side) into long or short; stop 6 ATR(20) from the signal close. A trade is held on the original's hold condition: a long is held while the close is above SMA200 and rate(base) is above rate(quote), a short on the mirror -- the 2.5 % band is for entries only. E0c: leave at the next open after the hold drops, or at a 6 ATR(20) trailing stop that never loosens. EL: the ladder of H4, its remainder also leaving at the next open after the hold drops; no time stop. Costs: the round-1 FX round trip and 0.004 % a night. In-sample 2006-2016, out-of-sample from 2017. Judged on EL.

### Pooled, by exit and period (* = the judged exit, EL)

| Exit | Period | Trades | Win | PF | Mean R | t | Total R | Max DD (R) | Hold (days) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| E0c | IS | 144 | 31.2% | 0.80 | -0.065 | -0.95 | -9.4 | 13.6 | 98.0 |
| E0c | OOS | 142 | 24.6% | 0.53 | -0.166 | -2.81 | -23.5 | 24.3 | 88.4 |
| EL * | IS | 132 | 38.6% | 0.80 | -0.055 | -1.06 | -7.3 | 12.8 | 100.4 |
| EL * | OOS | 134 | 32.1% | 0.57 | -0.145 | -2.87 | -19.5 | 19.5 | 88.8 |

### By instrument (exit EL; for information, not a gate)

| Instrument | Class | Period | Trades | Win | PF | Mean R | t | Total R | Max DD (R) | Hold (days) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| EURUSD | FX major | IS | 9 | 66.7% | 1.51 | +0.101 | 0.49 | +0.9 | 1.4 | 144.4 |
| EURUSD | FX major | OOS | 9 | 22.2% | 0.29 | -0.362 | -1.66 | -3.3 | 3.3 | 105.1 |
| GBPUSD | FX major | IS | 12 | 41.7% | 0.39 | -0.191 | -1.37 | -2.3 | 3.0 | 125.4 |
| GBPUSD | FX major | OOS | 9 | 55.6% | 0.86 | -0.032 | -0.16 | -0.3 | 1.2 | 82.6 |
| USDJPY | FX major | IS | 14 | 35.7% | 0.99 | -0.003 | -0.02 | -0.0 | 2.2 | 67.6 |
| USDJPY | FX major | OOS | 10 | 40.0% | 0.56 | -0.159 | -0.80 | -1.6 | 2.2 | 129.6 |
| AUDUSD | FX major | IS | 13 | 38.5% | 0.99 | -0.003 | -0.02 | -0.0 | 3.4 | 101.8 |
| AUDUSD | FX major | OOS | 17 | 29.4% | 0.74 | -0.064 | -0.52 | -1.1 | 1.7 | 105.0 |
| USDCAD | FX major | IS | 6 | 50.0% | 1.24 | +0.053 | 0.20 | +0.3 | 1.0 | 113.3 |
| USDCAD | FX major | OOS | 16 | 43.8% | 0.67 | -0.101 | -0.69 | -1.6 | 1.8 | 57.8 |
| USDCHF | FX major | IS | 15 | 40.0% | 0.91 | -0.022 | -0.14 | -0.3 | 1.8 | 77.0 |
| USDCHF | FX major | OOS | 10 | 10.0% | 0.16 | -0.437 | -2.77 | -4.4 | 4.4 | 61.4 |
| NZDUSD | FX major | IS | 18 | 38.9% | 1.19 | +0.041 | 0.29 | +0.7 | 2.6 | 98.4 |
| NZDUSD | FX major | OOS | 22 | 22.7% | 0.49 | -0.160 | -1.42 | -3.5 | 4.5 | 61.6 |
| EURGBP | FX cross | IS | 12 | 41.7% | 0.52 | -0.191 | -0.99 | -2.3 | 3.4 | 104.6 |
| EURGBP | FX cross | OOS | 8 | 12.5% | 0.06 | -0.530 | -3.75 | -4.2 | 4.2 | 103.6 |
| EURJPY | FX cross | IS | 15 | 33.3% | 0.66 | -0.122 | -0.72 | -1.8 | 4.0 | 99.5 |
| EURJPY | FX cross | OOS | 11 | 45.5% | 1.28 | +0.074 | 0.37 | +0.8 | 1.6 | 128.1 |
| GBPJPY | FX cross | IS | 14 | 28.6% | 0.75 | -0.069 | -0.43 | -1.0 | 2.3 | 85.5 |
| GBPJPY | FX cross | OOS | 18 | 38.9% | 1.20 | +0.045 | 0.33 | +0.8 | 2.0 | 97.7 |
| EURCHF | FX cross | IS | 4 | 0.0% | 0.00 | -0.359 | -2.54 | -1.4 | 1.4 | 157.2 |
| EURCHF | FX cross | OOS | 4 | 25.0% | 0.53 | -0.270 | -0.55 | -1.1 | 2.3 | 60.0 |

## Trade accounting

| Hypothesis | Exit | Signals | Closed | Skipped (R rules) | Open at end of data | Blocked (a trade was open) |
| --- | --- | --- | --- | --- | --- | --- |
| CR-BO | E0 | 1247 | 427 | 1 | 5 | 814 |
| CR-BO | E1 | 1247 | 476 | 2 | 8 | 761 |
| CR-BO | E2 | 1247 | 473 | 2 | 8 | 764 |
| IDX-DIP | E0c | 1797 | 1053 | 1 | 2 | 741 |
| IDX-DIP | EL | 1797 | 858 | 1 | 1 | 937 |
| CARRY-FX | E0c | 949 | 286 | 3 | 6 | 654 |
| CARRY-FX | EL | 949 | 266 | 3 | 6 | 674 |

## Data

| Instrument | Symbol | Interval | Bars | First | Last | Dropped | Fetched |
| --- | --- | --- | --- | --- | --- | --- | --- |
| SOLUSD | SOL-USD | 1d | 2370 | 2020-04-10 | 2026-10-07 | 3 (missing 0, inverted 0, wick 2, duplicate 0, unparsable 0, forming 1) | 2026-10-08 |
| XRPUSD | XRP-USD | 1d | 3253 | 2017-11-09 | 2026-10-07 | 3 (missing 0, inverted 0, wick 2, duplicate 0, unparsable 0, forming 1) | 2026-10-08 |
| BNBUSD | BNB-USD | 1d | 3254 | 2017-11-09 | 2026-10-07 | 2 (missing 0, inverted 0, wick 1, duplicate 0, unparsable 0, forming 1) | 2026-10-08 |
| DOGEUSD | DOGE-USD | 1d | 3251 | 2017-11-09 | 2026-10-07 | 5 (missing 0, inverted 0, wick 4, duplicate 0, unparsable 0, forming 1) | 2026-10-08 |
| AVAXUSD | AVAX-USD | 1d | 2207 | 2020-07-13 | 2026-10-07 | 3 (missing 0, inverted 0, wick 2, duplicate 0, unparsable 0, forming 1) | 2026-10-08 |
| LTCUSD | LTC-USD | 1d | 4403 | 2014-09-17 | 2026-10-07 | 2 (missing 0, inverted 0, wick 1, duplicate 0, unparsable 0, forming 1) | 2026-10-08 |
| LINKUSD | LINK-USD | 1d | 3253 | 2017-11-09 | 2026-10-07 | 3 (missing 0, inverted 0, wick 2, duplicate 0, unparsable 0, forming 1) | 2026-10-08 |
| TRXUSD | TRX-USD | 1d | 3254 | 2017-11-09 | 2026-10-07 | 2 (missing 0, inverted 0, wick 1, duplicate 0, unparsable 0, forming 1) | 2026-10-08 |
| ENAUSD | ENA-USD | 1d | 919 | 2024-04-02 | 2026-10-07 | 1 (missing 0, inverted 0, wick 0, duplicate 0, unparsable 0, forming 1) | 2026-10-08 |
| HYPEUSD | HYPE32196-USD | 1d | 677 | 2024-11-29 | 2026-10-07 | 1 (missing 0, inverted 0, wick 0, duplicate 0, unparsable 0, forming 1) | 2026-10-08 |
| SUIUSD | SUI20947-USD | 1d | 1252 | 2023-05-04 | 2026-10-07 | 3 (missing 0, inverted 0, wick 2, duplicate 0, unparsable 0, forming 1) | 2026-10-08 |
| US500 | ^GSPC | 1d | 6728 | 2000-01-03 | 2026-10-02 | 0 | 2026-10-04 |
| US100 | ^NDX | 1d | 6727 | 2000-01-03 | 2026-10-02 | 1 (missing 0, inverted 0, wick 1, duplicate 0, unparsable 0, forming 0) | 2026-10-04 |
| US30 | ^DJI | 1d | 6728 | 2000-01-03 | 2026-10-02 | 0 | 2026-10-04 |
| DE40 | ^GDAXI | 1d | 6794 | 2000-01-03 | 2026-10-02 | 0 | 2026-10-04 |
| UK100 | ^FTSE | 1d | 6758 | 2000-01-04 | 2026-10-02 | 0 | 2026-10-04 |
| JP225 | ^N225 | 1d | 6551 | 2000-01-04 | 2026-10-02 | 0 | 2026-10-04 |
| EURUSD | EURUSD=X | 1d | 5926 | 2003-12-01 | 2026-10-04 | 1 (missing 0, inverted 0, wick 1, duplicate 0, unparsable 0, forming 0) | 2026-10-04 |
| GBPUSD | GBPUSD=X | 1d | 5938 | 2003-12-01 | 2026-10-04 | 1 (missing 0, inverted 0, wick 1, duplicate 0, unparsable 0, forming 0) | 2026-10-04 |
| USDJPY | USDJPY=X | 1d | 6947 | 2000-01-03 | 2026-10-04 | 0 | 2026-10-04 |
| AUDUSD | AUDUSD=X | 1d | 5302 | 2006-05-16 | 2026-10-04 | 1 (missing 0, inverted 0, wick 1, duplicate 0, unparsable 0, forming 0) | 2026-10-04 |
| USDCAD | USDCAD=X | 1d | 5995 | 2003-09-17 | 2026-10-04 | 0 | 2026-10-04 |
| USDCHF | USDCHF=X | 1d | 5992 | 2003-09-17 | 2026-10-04 | 1 (missing 0, inverted 0, wick 1, duplicate 0, unparsable 0, forming 0) | 2026-10-04 |
| NZDUSD | NZDUSD=X | 1d | 5925 | 2003-12-01 | 2026-10-04 | 3 (missing 0, inverted 0, wick 3, duplicate 0, unparsable 0, forming 0) | 2026-10-04 |
| EURGBP | EURGBP=X | 1d | 6966 | 2000-01-03 | 2026-10-04 | 1 (missing 0, inverted 0, wick 1, duplicate 0, unparsable 0, forming 0) | 2026-10-04 |
| EURJPY | EURJPY=X | 1d | 6152 | 2003-01-23 | 2026-10-04 | 0 | 2026-10-04 |
| GBPJPY | GBPJPY=X | 1d | 5943 | 2003-12-01 | 2026-10-04 | 0 | 2026-10-04 |
| EURCHF | EURCHF=X | 1d | 6148 | 2003-01-23 | 2026-10-04 | 2 (missing 0, inverted 0, wick 2, duplicate 0, unparsable 0, forming 0) | 2026-10-04 |

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

- Results are in R after costs: one round trip per trade plus financing for every calendar night held. H3 pays a round trip of 0.40 % and 0.06 % a night, H4 the round-1 Indices row (0.030 % and 0.020 % a night, long only), H5 the pair's round-1 FX round trip (0.015 % a major, 0.030 % a cross) and 0.004 % a night. H5 charges the broker's markup and credits none of the carry it earns, which is the conservative reading.
- Everything the pre-registration does not restate is round 1's: data hygiene, ATR(14) for H3 and H4, the bar order (stop before target within a bar), gaps filling at the open, the skip rule (R <= 0.25 ATR, or R > 6 ATR; H5: 8 ATR), one open trade per instrument per setup and exit, and 300 bars of history before a first signal.
- Three points were settled in writing before the first run (the pre-registration's amendment section). A1: H5 is held on the original's hold condition -- a long while the close is above SMA200 and the base rate above the quote rate, a short on the mirror; the 2.5 % band is for entries only. A2: H5 uses ATR(20), the original's, for its stop, its trailing stop and its R bounds. A3: the coins of H3 are cleaned with a wick threshold of 60 % in place of 15 %, which was written for bad FX ticks; every other instrument keeps 15 %.
- Every stop is fixed at the signal (H4: the close - 2.5 ATR; H5: the close -/+ 6 ATR(20)) and R is measured from the actual entry, the next open, as for round 1's setups; so R is about 6 ATR for H5 and the gap of the entry bar is what the 8 ATR bound allows for.
- A time stop of N bars leaves at the open of the Nth bar after the entry bar (the bar whose open is the entry). A condition exit (the close above SMA5; the state no longer the trade's side) is read at each close from the entry bar's own close on and leaves at the next bar's open; a condition read at the signal bar, before the entry, never counts. Both fill at that open, before the bar's own stop and targets, and the earlier of the two wins.
- H5's entry state includes the 2.5 % band (carry_strategy.py's frozen 2.5 % and 200 days); its exit does not: a trade leaves when the close crosses back through the 200-day average or the rates no longer agree with its side, as carry_strategy.py's own hold condition does.
- The rate of a currency on a bar is the value of the latest month already published (the 15th of the next month reached, that day included); a bar with no rate for either currency has no state.
- In-sample and out-of-sample are split by the signal date; signals before 2006-01-01 (crypto: no lower bound) are warm-up only. The in-sample line of the gate is required only with at least 30 in-sample trades; the coins that listed late have none.
- The t-statistics pool trades across instruments and treat them as independent; trades that overlap in time in correlated markets make the true uncertainty larger. The bar of 2.33 is Bonferroni for the five hypotheses of the two rounds.
- The gate judges each hypothesis on its pooled sample. A hypothesis that passes is enabled for its whole universe (H3: BTC and ETH too, on round 1's evidence); there is no per-instrument picking, and a hypothesis that fails is not traded.
