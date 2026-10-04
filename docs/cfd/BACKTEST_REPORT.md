# CFD backtest report

Run at 2026-10-04T22:42:04+00:00. Source: Yahoo (yfinance), daily bars through 2026-10-03; hourly gold 2024-05-17 to 2026-10-02. Pre-registration: docs/superpowers/specs/2026-10-05-cfd-signals.md, Part 1.

## Outcome

**Nothing passed the gate: no CFD signal goes live.**

| Setup | Exit | Gate | Enabled classes |
| --- | --- | --- | --- |
| PB-D | E1 | FAIL | none |
| BO-D | E1 | FAIL | none |
| PB-H1-GOLD | E1 | FAIL | none |

## Gate

Every line is pass or fail; a setup or class that fails is not traded.

### PB-D (exit E1)

| Scope | Level | Rule | Value | Threshold | Result |
| --- | --- | --- | --- | --- | --- |
| PB-D | setup | in-sample mean R | -0.1867 | > 0 | FAIL |
| PB-D | setup | out-of-sample trades | 1030 | >= 150 | PASS |
| PB-D | setup | out-of-sample profit factor | 0.6252 | >= 1.15 | FAIL |
| PB-D | setup | out-of-sample mean R | -0.2133 | > 0 | FAIL |
| PB-D | setup | out-of-sample t-statistic | -6.4051 | >= 1.96 | FAIL |

Class level not evaluated: the setup failed level 1.

### BO-D (exit E1)

| Scope | Level | Rule | Value | Threshold | Result |
| --- | --- | --- | --- | --- | --- |
| BO-D | setup | in-sample mean R | -0.1073 | > 0 | FAIL |
| BO-D | setup | out-of-sample trades | 887 | >= 150 | PASS |
| BO-D | setup | out-of-sample profit factor | 0.6465 | >= 1.15 | FAIL |
| BO-D | setup | out-of-sample mean R | -0.2110 | > 0 | FAIL |
| BO-D | setup | out-of-sample t-statistic | -5.5832 | >= 1.96 | FAIL |

Class level not evaluated: the setup failed level 1.

### PB-H1-GOLD (exit E1)

| Scope | Level | Rule | Value | Threshold | Result |
| --- | --- | --- | --- | --- | --- |
| PB-H1-GOLD | setup | profit factor, chosen exit | 0.7002 | >= 1.10 | FAIL |
| PB-H1-GOLD | setup | mean R, chosen exit | -0.0989 | > 0 | FAIL |
| PB-H1-GOLD | setup | E0 profit factor, lower bound | 0.9122 | >= 1.05 | FAIL |
| PB-H1-GOLD | setup | E0 profit factor, upper bound | 0.9122 | <= 1.35 | PASS |

## PB-D

### Pooled, by exit and period (* = the chosen exit, E1)

| Exit | Period | Trades | Win | PF | Mean R | t | Total R | Max DD (R) | Hold (days) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| E0 | IS | 1745 | 31.8% | 0.90 | -0.055 | -1.09 | -95.2 | 182.2 | 24.7 |
| E0 | OOS | 1526 | 27.3% | 0.66 | -0.186 | -5.57 | -284.5 | 284.5 | 24.8 |
| E1 * | IS | 1124 | 42.9% | 0.68 | -0.187 | -5.47 | -209.8 | 218.0 | 59.2 |
| E1 * | OOS | 1030 | 42.9% | 0.63 | -0.213 | -6.41 | -219.7 | 223.0 | 43.5 |
| E2 | IS | 1152 | 42.4% | 0.66 | -0.201 | -5.97 | -231.1 | 243.5 | 56.7 |
| E2 | OOS | 1044 | 42.6% | 0.62 | -0.215 | -6.50 | -224.5 | 226.0 | 42.8 |

### By class

| Class | Exit | Period | Trades | Win | PF | Mean R | t | Total R | Max DD (R) | Hold (days) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| FX | E0 | IS | 732 | 28.7% | 0.88 | -0.070 | -0.69 | -51.5 | 113.3 | 26.9 |
| FX | E0 | OOS | 537 | 22.9% | 0.40 | -0.381 | -8.12 | -204.6 | 204.6 | 29.4 |
| FX | E1 * | IS | 492 | 42.7% | 0.64 | -0.217 | -4.24 | -106.5 | 106.7 | 64.7 |
| FX | E1 * | OOS | 426 | 40.1% | 0.47 | -0.311 | -6.56 | -132.3 | 132.9 | 41.5 |
| FX | E2 | IS | 500 | 42.6% | 0.62 | -0.231 | -4.61 | -115.5 | 115.7 | 62.8 |
| FX | E2 | OOS | 429 | 39.9% | 0.47 | -0.314 | -6.65 | -134.8 | 135.4 | 40.8 |
| Metals | E0 | IS | 227 | 33.5% | 0.91 | -0.048 | -0.46 | -10.8 | 23.3 | 18.9 |
| Metals | E0 | OOS | 180 | 28.3% | 0.75 | -0.140 | -1.39 | -25.2 | 31.9 | 20.6 |
| Metals | E1 * | IS | 91 | 44.0% | 0.78 | -0.138 | -1.00 | -12.6 | 24.2 | 89.5 |
| Metals | E1 * | OOS | 104 | 45.2% | 0.73 | -0.151 | -1.34 | -15.7 | 22.3 | 37.5 |
| Metals | E2 | IS | 103 | 44.7% | 0.77 | -0.144 | -1.12 | -14.8 | 25.3 | 77.8 |
| Metals | E2 | OOS | 103 | 44.7% | 0.72 | -0.160 | -1.36 | -16.4 | 22.7 | 37.1 |
| Energy | E0 | IS | 138 | 32.6% | 1.22 | +0.099 | 0.60 | +13.6 | 21.2 | 31.8 |
| Energy | E0 | OOS | 154 | 24.7% | 0.84 | -0.083 | -0.68 | -12.7 | 33.1 | 27.0 |
| Energy | E1 * | IS | 99 | 45.5% | 0.96 | -0.024 | -0.20 | -2.4 | 23.5 | 58.0 |
| Energy | E1 * | OOS | 107 | 46.7% | 0.72 | -0.149 | -1.39 | -16.0 | 20.2 | 49.9 |
| Energy | E2 | IS | 109 | 41.3% | 0.85 | -0.088 | -0.74 | -9.6 | 24.2 | 50.9 |
| Energy | E2 | OOS | 106 | 46.2% | 0.75 | -0.135 | -1.22 | -14.3 | 19.2 | 50.4 |
| Indices | E0 | IS | 564 | 33.9% | 0.69 | -0.150 | -3.25 | -84.4 | 92.0 | 23.5 |
| Indices | E0 | OOS | 485 | 30.1% | 0.73 | -0.139 | -2.47 | -67.2 | 95.8 | 22.1 |
| Indices | E1 * | IS | 384 | 41.4% | 0.56 | -0.258 | -4.95 | -98.9 | 104.9 | 49.1 |
| Indices | E1 * | OOS | 287 | 42.5% | 0.70 | -0.168 | -2.66 | -48.2 | 64.9 | 48.0 |
| Indices | E2 | IS | 384 | 40.6% | 0.55 | -0.269 | -5.14 | -103.1 | 110.5 | 48.1 |
| Indices | E2 | OOS | 292 | 42.1% | 0.69 | -0.173 | -2.81 | -50.6 | 62.7 | 47.6 |
| Crypto | E0 | IS | 84 | 39.3% | 2.08 | +0.451 | 1.97 | +37.9 | 7.9 | 18.1 |
| Crypto | E0 | OOS | 170 | 34.1% | 1.31 | +0.148 | 1.11 | +25.2 | 21.2 | 20.4 |
| Crypto | E1 * | IS | 58 | 48.3% | 1.35 | +0.183 | 0.94 | +10.6 | 11.3 | 33.1 |
| Crypto | E1 * | OOS | 106 | 49.1% | 0.88 | -0.071 | -0.58 | -7.6 | 25.7 | 38.7 |
| Crypto | E2 | IS | 56 | 51.8% | 1.44 | +0.213 | 1.10 | +11.9 | 8.6 | 34.1 |
| Crypto | E2 | OOS | 114 | 49.1% | 0.87 | -0.074 | -0.65 | -8.5 | 27.1 | 36.0 |

### By instrument (exit E1)

| Instrument | Class | Period | Trades | Win | PF | Mean R | t | Total R | Max DD (R) | Hold (days) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| EURUSD | FX major | IS | 47 | 34.0% | 0.40 | -0.395 | -2.68 | -18.6 | 18.7 | 59.3 |
| EURUSD | FX major | OOS | 43 | 44.2% | 0.64 | -0.172 | -1.17 | -7.4 | 12.1 | 43.3 |
| GBPUSD | FX major | IS | 54 | 37.0% | 0.62 | -0.244 | -1.59 | -13.2 | 15.8 | 48.5 |
| GBPUSD | FX major | OOS | 38 | 31.6% | 0.27 | -0.504 | -3.49 | -19.2 | 20.3 | 40.1 |
| USDJPY | FX major | IS | 27 | 29.6% | 0.25 | -0.574 | -3.17 | -15.5 | 16.5 | 133.3 |
| USDJPY | FX major | OOS | 44 | 50.0% | 1.05 | +0.023 | 0.13 | +1.0 | 6.4 | 40.5 |
| AUDUSD | FX major | IS | 46 | 56.5% | 1.02 | +0.009 | 0.06 | +0.4 | 5.6 | 40.0 |
| AUDUSD | FX major | OOS | 33 | 36.4% | 0.41 | -0.379 | -2.13 | -12.5 | 15.8 | 43.6 |
| USDCAD | FX major | IS | 49 | 42.9% | 0.63 | -0.217 | -1.37 | -10.6 | 13.5 | 53.2 |
| USDCAD | FX major | OOS | 44 | 38.6% | 0.50 | -0.311 | -1.97 | -13.7 | 16.9 | 37.4 |
| USDCHF | FX major | IS | 44 | 43.2% | 0.63 | -0.228 | -1.33 | -10.0 | 11.5 | 68.9 |
| USDCHF | FX major | OOS | 22 | 50.0% | 0.36 | -0.314 | -1.91 | -6.9 | 7.1 | 33.7 |
| NZDUSD | FX major | IS | 54 | 55.6% | 1.06 | +0.027 | 0.17 | +1.4 | 9.4 | 45.2 |
| NZDUSD | FX major | OOS | 55 | 40.0% | 0.47 | -0.324 | -2.35 | -17.8 | 19.6 | 33.3 |
| EURGBP | FX cross | IS | 55 | 38.2% | 0.49 | -0.310 | -2.21 | -17.1 | 21.2 | 42.9 |
| EURGBP | FX cross | OOS | 42 | 26.2% | 0.21 | -0.577 | -4.44 | -24.2 | 24.2 | 35.5 |
| EURJPY | FX cross | IS | 34 | 47.1% | 0.62 | -0.232 | -1.15 | -7.9 | 9.5 | 79.9 |
| EURJPY | FX cross | OOS | 48 | 47.9% | 0.59 | -0.213 | -1.54 | -10.2 | 11.9 | 34.8 |
| GBPJPY | FX cross | IS | 45 | 48.9% | 1.01 | +0.007 | 0.03 | +0.3 | 8.4 | 99.1 |
| GBPJPY | FX cross | OOS | 23 | 47.8% | 0.47 | -0.287 | -1.61 | -6.6 | 6.9 | 46.1 |
| EURCHF | FX cross | IS | 37 | 29.7% | 0.48 | -0.428 | -2.01 | -15.8 | 15.9 | 91.4 |
| EURCHF | FX cross | OOS | 34 | 32.4% | 0.35 | -0.435 | -2.63 | -14.8 | 16.2 | 77.2 |
| XAUUSD | Metals | IS | 48 | 39.6% | 0.63 | -0.253 | -1.33 | -12.1 | 16.3 | 67.4 |
| XAUUSD | Metals | OOS | 56 | 53.6% | 1.06 | +0.027 | 0.18 | +1.5 | 7.4 | 44.6 |
| XAGUSD | Metals | IS | 43 | 48.8% | 0.98 | -0.011 | -0.05 | -0.5 | 8.3 | 114.1 |
| XAGUSD | Metals | OOS | 48 | 35.4% | 0.48 | -0.358 | -2.13 | -17.2 | 19.9 | 29.2 |
| WTI | Energy | IS | 62 | 45.2% | 0.98 | -0.009 | -0.06 | -0.5 | 13.1 | 56.0 |
| WTI | Energy | OOS | 55 | 45.5% | 0.58 | -0.241 | -1.64 | -13.3 | 13.3 | 46.1 |
| BRENT | Energy | IS | 37 | 45.9% | 0.91 | -0.050 | -0.25 | -1.9 | 12.7 | 61.3 |
| BRENT | Energy | OOS | 52 | 48.1% | 0.89 | -0.053 | -0.33 | -2.7 | 9.2 | 53.8 |
| US500 | Indices | IS | 68 | 41.2% | 0.55 | -0.241 | -2.08 | -16.4 | 19.4 | 43.5 |
| US500 | Indices | OOS | 44 | 47.7% | 0.97 | -0.016 | -0.10 | -0.7 | 9.3 | 50.0 |
| US100 | Indices | IS | 57 | 42.1% | 0.52 | -0.295 | -2.16 | -16.8 | 19.6 | 56.1 |
| US100 | Indices | OOS | 62 | 50.0% | 0.98 | -0.008 | -0.06 | -0.5 | 10.0 | 42.7 |
| US30 | Indices | IS | 62 | 35.5% | 0.49 | -0.325 | -2.53 | -20.2 | 21.6 | 51.9 |
| US30 | Indices | OOS | 59 | 35.6% | 0.53 | -0.306 | -2.17 | -18.0 | 24.4 | 39.0 |
| DE40 | Indices | IS | 65 | 52.3% | 0.83 | -0.081 | -0.63 | -5.3 | 11.1 | 48.2 |
| DE40 | Indices | OOS | 46 | 39.1% | 0.45 | -0.295 | -2.22 | -13.6 | 15.6 | 43.2 |
| UK100 | Indices | IS | 62 | 27.4% | 0.24 | -0.551 | -5.02 | -34.2 | 34.8 | 53.6 |
| UK100 | Indices | OOS | 37 | 32.4% | 0.52 | -0.305 | -1.73 | -11.3 | 16.1 | 59.3 |
| JP225 | Indices | IS | 70 | 48.6% | 0.83 | -0.087 | -0.64 | -6.1 | 13.2 | 43.1 |
| JP225 | Indices | OOS | 39 | 48.7% | 0.81 | -0.104 | -0.59 | -4.1 | 6.1 | 62.8 |
| BTCUSD | Crypto | IS | 44 | 52.3% | 1.58 | +0.279 | 1.24 | +12.3 | 4.6 | 34.1 |
| BTCUSD | Crypto | OOS | 40 | 52.5% | 0.90 | -0.052 | -0.27 | -2.1 | 9.1 | 53.7 |
| ETHUSD | Crypto | IS | 14 | 35.7% | 0.82 | -0.119 | -0.31 | -1.7 | 6.7 | 29.9 |
| ETHUSD | Crypto | OOS | 66 | 47.0% | 0.86 | -0.083 | -0.52 | -5.5 | 18.4 | 29.6 |

## BO-D

### Pooled, by exit and period (* = the chosen exit, E1)

| Exit | Period | Trades | Win | PF | Mean R | t | Total R | Max DD (R) | Hold (days) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| E0 | IS | 1048 | 35.4% | 0.98 | -0.009 | -0.21 | -9.9 | 75.4 | 26.4 |
| E0 | OOS | 934 | 30.5% | 0.73 | -0.140 | -3.57 | -130.3 | 130.3 | 26.2 |
| E1 * | IS | 1003 | 46.8% | 0.81 | -0.107 | -2.88 | -107.6 | 129.0 | 38.4 |
| E1 * | OOS | 887 | 43.0% | 0.65 | -0.211 | -5.58 | -187.1 | 187.3 | 37.8 |
| E2 | IS | 985 | 46.8% | 0.80 | -0.114 | -3.06 | -112.5 | 131.6 | 38.4 |
| E2 | OOS | 871 | 42.0% | 0.63 | -0.221 | -5.77 | -192.5 | 192.6 | 37.8 |

### By class

| Class | Exit | Period | Trades | Win | PF | Mean R | t | Total R | Max DD (R) | Hold (days) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| FX | E0 | IS | 459 | 33.6% | 0.83 | -0.091 | -1.41 | -41.6 | 53.6 | 28.6 |
| FX | E0 | OOS | 364 | 21.7% | 0.32 | -0.421 | -9.27 | -153.3 | 153.3 | 29.6 |
| FX | E1 * | IS | 424 | 46.2% | 0.76 | -0.143 | -2.46 | -60.7 | 70.6 | 46.3 |
| FX | E1 * | OOS | 329 | 32.2% | 0.35 | -0.472 | -8.57 | -155.3 | 155.3 | 50.1 |
| FX | E2 | IS | 414 | 47.1% | 0.77 | -0.132 | -2.25 | -54.6 | 67.9 | 46.8 |
| FX | E2 | OOS | 324 | 32.1% | 0.34 | -0.479 | -8.77 | -155.1 | 155.1 | 49.6 |
| Metals | E0 | IS | 129 | 34.1% | 1.10 | +0.053 | 0.41 | +6.8 | 10.6 | 17.8 |
| Metals | E0 | OOS | 96 | 36.5% | 1.29 | +0.130 | 0.85 | +12.4 | 14.7 | 21.6 |
| Metals | E1 * | IS | 138 | 47.1% | 0.86 | -0.078 | -0.73 | -10.7 | 23.1 | 19.9 |
| Metals | E1 * | OOS | 96 | 49.0% | 1.06 | +0.031 | 0.24 | +3.0 | 9.8 | 26.2 |
| Metals | E2 | IS | 134 | 46.3% | 0.78 | -0.126 | -1.19 | -16.9 | 23.3 | 20.7 |
| Metals | E2 | OOS | 93 | 46.2% | 0.99 | -0.008 | -0.06 | -0.7 | 9.4 | 26.1 |
| Energy | E0 | IS | 81 | 42.0% | 1.60 | +0.257 | 1.13 | +20.8 | 14.0 | 33.3 |
| Energy | E0 | OOS | 85 | 32.9% | 1.14 | +0.063 | 0.41 | +5.4 | 16.9 | 26.9 |
| Energy | E1 * | IS | 83 | 51.8% | 1.08 | +0.042 | 0.31 | +3.5 | 14.1 | 40.5 |
| Energy | E1 * | OOS | 86 | 46.5% | 0.75 | -0.138 | -1.09 | -11.8 | 18.9 | 36.5 |
| Energy | E2 | IS | 82 | 48.8% | 1.03 | +0.014 | 0.10 | +1.1 | 13.7 | 38.9 |
| Energy | E2 | OOS | 84 | 45.2% | 0.72 | -0.154 | -1.18 | -12.9 | 18.9 | 37.8 |
| Indices | E0 | IS | 344 | 35.8% | 0.73 | -0.127 | -2.44 | -43.8 | 52.1 | 26.3 |
| Indices | E0 | OOS | 311 | 34.1% | 0.80 | -0.093 | -1.49 | -28.9 | 42.3 | 25.0 |
| Indices | E1 * | IS | 304 | 42.8% | 0.67 | -0.187 | -3.05 | -56.9 | 63.2 | 40.5 |
| Indices | E1 * | OOS | 279 | 45.5% | 0.76 | -0.136 | -2.04 | -37.8 | 51.0 | 34.1 |
| Indices | E2 | IS | 305 | 42.6% | 0.66 | -0.194 | -3.17 | -59.2 | 65.6 | 39.2 |
| Indices | E2 | OOS | 276 | 44.2% | 0.74 | -0.149 | -2.23 | -41.1 | 51.4 | 33.9 |
| Crypto | E0 | IS | 35 | 45.7% | 5.63 | +1.368 | 2.68 | +47.9 | 2.7 | 14.9 |
| Crypto | E0 | OOS | 78 | 47.4% | 2.12 | +0.438 | 2.25 | +34.1 | 5.4 | 20.7 |
| Crypto | E1 * | IS | 54 | 64.8% | 1.94 | +0.319 | 1.85 | +17.2 | 4.1 | 9.9 |
| Crypto | E1 * | OOS | 97 | 62.9% | 1.38 | +0.153 | 1.28 | +14.9 | 10.5 | 19.4 |
| Crypto | E2 | IS | 50 | 68.0% | 2.12 | +0.342 | 1.98 | +17.1 | 4.1 | 10.8 |
| Crypto | E2 | OOS | 94 | 62.8% | 1.46 | +0.186 | 1.49 | +17.4 | 10.5 | 19.9 |

### By instrument (exit E1)

| Instrument | Class | Period | Trades | Win | PF | Mean R | t | Total R | Max DD (R) | Hold (days) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| EURUSD | FX major | IS | 40 | 55.0% | 1.14 | +0.057 | 0.32 | +2.3 | 6.1 | 36.4 |
| EURUSD | FX major | OOS | 31 | 32.3% | 0.42 | -0.382 | -2.17 | -11.8 | 15.8 | 52.9 |
| GBPUSD | FX major | IS | 38 | 42.1% | 0.89 | -0.064 | -0.31 | -2.4 | 10.6 | 49.0 |
| GBPUSD | FX major | OOS | 30 | 30.0% | 0.27 | -0.505 | -2.99 | -15.1 | 15.1 | 36.9 |
| USDJPY | FX major | IS | 40 | 47.5% | 0.94 | -0.035 | -0.18 | -1.4 | 13.1 | 38.5 |
| USDJPY | FX major | OOS | 32 | 50.0% | 0.78 | -0.109 | -0.55 | -3.5 | 5.8 | 36.9 |
| AUDUSD | FX major | IS | 30 | 56.7% | 1.11 | +0.053 | 0.24 | +1.6 | 5.5 | 67.4 |
| AUDUSD | FX major | OOS | 35 | 25.7% | 0.28 | -0.589 | -3.43 | -20.6 | 21.5 | 47.4 |
| USDCAD | FX major | IS | 38 | 44.7% | 0.83 | -0.098 | -0.49 | -3.7 | 12.8 | 44.3 |
| USDCAD | FX major | OOS | 26 | 38.5% | 0.44 | -0.414 | -1.95 | -10.8 | 12.1 | 43.2 |
| USDCHF | FX major | IS | 37 | 43.2% | 0.58 | -0.294 | -1.32 | -10.9 | 10.9 | 58.3 |
| USDCHF | FX major | OOS | 26 | 26.9% | 0.47 | -0.350 | -1.66 | -9.1 | 10.1 | 76.7 |
| NZDUSD | FX major | IS | 45 | 42.2% | 0.70 | -0.172 | -1.00 | -7.7 | 11.8 | 41.2 |
| NZDUSD | FX major | OOS | 31 | 38.7% | 0.38 | -0.410 | -2.40 | -12.7 | 12.7 | 54.5 |
| EURGBP | FX cross | IS | 37 | 54.1% | 0.77 | -0.111 | -0.64 | -4.1 | 7.9 | 50.5 |
| EURGBP | FX cross | OOS | 31 | 16.1% | 0.14 | -0.853 | -5.06 | -26.4 | 26.4 | 37.4 |
| EURJPY | FX cross | IS | 42 | 42.9% | 0.67 | -0.196 | -1.16 | -8.2 | 11.3 | 40.6 |
| EURJPY | FX cross | OOS | 28 | 32.1% | 0.24 | -0.578 | -3.51 | -16.2 | 16.2 | 72.2 |
| GBPJPY | FX cross | IS | 36 | 50.0% | 0.90 | -0.054 | -0.27 | -2.0 | 9.9 | 50.6 |
| GBPJPY | FX cross | OOS | 32 | 31.2% | 0.35 | -0.471 | -2.69 | -15.1 | 15.3 | 37.1 |
| EURCHF | FX cross | IS | 41 | 34.1% | 0.32 | -0.587 | -2.98 | -24.1 | 24.1 | 40.0 |
| EURCHF | FX cross | OOS | 27 | 33.3% | 0.31 | -0.517 | -2.66 | -13.9 | 14.5 | 63.5 |
| XAUUSD | Metals | IS | 66 | 48.5% | 0.77 | -0.133 | -0.90 | -8.8 | 14.6 | 20.2 |
| XAUUSD | Metals | OOS | 40 | 50.0% | 1.36 | +0.173 | 0.84 | +6.9 | 6.1 | 34.4 |
| XAGUSD | Metals | IS | 72 | 45.8% | 0.95 | -0.027 | -0.18 | -2.0 | 13.0 | 19.6 |
| XAGUSD | Metals | OOS | 56 | 48.2% | 0.87 | -0.070 | -0.41 | -3.9 | 12.7 | 20.3 |
| WTI | Energy | IS | 48 | 47.9% | 0.86 | -0.078 | -0.47 | -3.7 | 11.9 | 42.0 |
| WTI | Energy | OOS | 43 | 46.5% | 0.70 | -0.161 | -0.90 | -6.9 | 10.4 | 37.9 |
| BRENT | Energy | IS | 35 | 57.1% | 1.47 | +0.206 | 0.93 | +7.2 | 5.5 | 38.5 |
| BRENT | Energy | OOS | 43 | 46.5% | 0.79 | -0.114 | -0.63 | -4.9 | 9.0 | 35.1 |
| US500 | Indices | IS | 53 | 39.6% | 0.46 | -0.356 | -2.60 | -18.9 | 19.9 | 41.8 |
| US500 | Indices | OOS | 45 | 51.1% | 0.96 | -0.021 | -0.13 | -0.9 | 6.2 | 40.7 |
| US100 | Indices | IS | 49 | 49.0% | 0.84 | -0.079 | -0.50 | -3.9 | 9.1 | 46.6 |
| US100 | Indices | OOS | 47 | 55.3% | 1.29 | +0.136 | 0.77 | +6.4 | 5.6 | 36.4 |
| US30 | Indices | IS | 57 | 36.8% | 0.58 | -0.219 | -1.73 | -12.5 | 14.1 | 32.7 |
| US30 | Indices | OOS | 51 | 39.2% | 0.57 | -0.249 | -1.69 | -12.7 | 20.0 | 34.9 |
| DE40 | Indices | IS | 41 | 48.8% | 0.94 | -0.034 | -0.18 | -1.4 | 5.2 | 49.5 |
| DE40 | Indices | OOS | 48 | 39.6% | 0.51 | -0.329 | -2.07 | -15.8 | 17.5 | 38.5 |
| UK100 | Indices | IS | 51 | 35.3% | 0.45 | -0.345 | -2.46 | -17.6 | 19.4 | 39.8 |
| UK100 | Indices | OOS | 42 | 40.5% | 0.53 | -0.267 | -1.79 | -11.2 | 11.9 | 26.5 |
| JP225 | Indices | IS | 53 | 49.1% | 0.90 | -0.051 | -0.31 | -2.7 | 11.4 | 35.6 |
| JP225 | Indices | OOS | 46 | 47.8% | 0.86 | -0.078 | -0.43 | -3.6 | 9.0 | 26.4 |
| BTCUSD | Crypto | IS | 46 | 67.4% | 1.93 | +0.305 | 1.68 | +14.0 | 4.1 | 9.0 |
| BTCUSD | Crypto | OOS | 51 | 62.7% | 1.13 | +0.057 | 0.36 | +2.9 | 7.1 | 16.8 |
| ETHUSD | Crypto | IS | 8 | 50.0% | 1.99 | +0.399 | 0.73 | +3.2 | 1.9 | 15.1 |
| ETHUSD | Crypto | OOS | 46 | 63.0% | 1.70 | +0.261 | 1.42 | +12.0 | 4.7 | 22.3 |

## PB-H1-GOLD

A port check, not a new test: the hourly history is the user's own fitting data.

### Pooled, by exit and period (* = the chosen exit, E1)

| Exit | Period | Trades | Win | PF | Mean R | t | Total R | Max DD (R) | Hold (days) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| E0 | ALL | 434 | 42.9% | 0.91 | -0.032 | -0.68 | -14.1 | 32.3 | 0.2 |
| E1 * | ALL | 461 | 47.3% | 0.70 | -0.099 | -3.06 | -45.6 | 46.6 | 0.2 |
| E2 | ALL | 461 | 47.3% | 0.70 | -0.099 | -3.06 | -45.6 | 46.6 | 0.2 |

## Trade accounting

| Setup | Exit | Signals | Closed | Skipped (R rules) | Open at end of data | Blocked (a trade was open) |
| --- | --- | --- | --- | --- | --- | --- |
| PB-D | E0 | 14166 | 3271 | 63 | 11 | 10821 |
| PB-D | E1 | 14166 | 2154 | 53 | 16 | 11943 |
| PB-D | E2 | 14166 | 2196 | 51 | 16 | 11903 |
| BO-D | E0 | 7607 | 1982 | 10 | 10 | 5605 |
| BO-D | E1 | 7607 | 1890 | 16 | 12 | 5689 |
| BO-D | E2 | 7607 | 1856 | 14 | 12 | 5725 |
| PB-H1-GOLD | E0 | 950 | 434 | 3 | 0 | 513 |
| PB-H1-GOLD | E1 | 950 | 461 | 3 | 0 | 486 |
| PB-H1-GOLD | E2 | 950 | 461 | 3 | 0 | 486 |

## Data

| Instrument | Symbol | Interval | Bars | First | Last | Dropped | Fetched |
| --- | --- | --- | --- | --- | --- | --- | --- |
| EURUSD | EURUSD=X | 1d | 5925 | 2003-12-01 | 2026-10-02 | 2 (missing 0, inverted 0, wick 1, duplicate 0, unparsable 0, forming 1) | 2026-10-04 |
| GBPUSD | GBPUSD=X | 1d | 5937 | 2003-12-01 | 2026-10-02 | 2 (missing 0, inverted 0, wick 1, duplicate 0, unparsable 0, forming 1) | 2026-10-04 |
| USDJPY | USDJPY=X | 1d | 6946 | 2000-01-03 | 2026-10-02 | 1 (missing 0, inverted 0, wick 0, duplicate 0, unparsable 0, forming 1) | 2026-10-04 |
| AUDUSD | AUDUSD=X | 1d | 5301 | 2006-05-16 | 2026-10-02 | 2 (missing 0, inverted 0, wick 1, duplicate 0, unparsable 0, forming 1) | 2026-10-04 |
| USDCAD | USDCAD=X | 1d | 5994 | 2003-09-17 | 2026-10-02 | 1 (missing 0, inverted 0, wick 0, duplicate 0, unparsable 0, forming 1) | 2026-10-04 |
| USDCHF | USDCHF=X | 1d | 5991 | 2003-09-17 | 2026-10-02 | 2 (missing 0, inverted 0, wick 1, duplicate 0, unparsable 0, forming 1) | 2026-10-04 |
| NZDUSD | NZDUSD=X | 1d | 5924 | 2003-12-01 | 2026-10-02 | 4 (missing 0, inverted 0, wick 3, duplicate 0, unparsable 0, forming 1) | 2026-10-04 |
| EURGBP | EURGBP=X | 1d | 6965 | 2000-01-03 | 2026-10-02 | 2 (missing 0, inverted 0, wick 1, duplicate 0, unparsable 0, forming 1) | 2026-10-04 |
| EURJPY | EURJPY=X | 1d | 6151 | 2003-01-23 | 2026-10-02 | 1 (missing 0, inverted 0, wick 0, duplicate 0, unparsable 0, forming 1) | 2026-10-04 |
| GBPJPY | GBPJPY=X | 1d | 5942 | 2003-12-01 | 2026-10-02 | 1 (missing 0, inverted 0, wick 0, duplicate 0, unparsable 0, forming 1) | 2026-10-04 |
| EURCHF | EURCHF=X | 1d | 6147 | 2003-01-23 | 2026-10-02 | 3 (missing 0, inverted 0, wick 2, duplicate 0, unparsable 0, forming 1) | 2026-10-04 |
| XAUUSD | GC=F | 1d | 6546 | 2000-08-30 | 2026-10-02 | 3 (missing 0, inverted 1, wick 1, duplicate 0, unparsable 0, forming 1) | 2026-10-04 |
| XAGUSD | SI=F | 1d | 6544 | 2000-08-30 | 2026-10-02 | 6 (missing 0, inverted 0, wick 5, duplicate 0, unparsable 0, forming 1) | 2026-10-04 |
| WTI | CL=F | 1d | 6532 | 2000-08-23 | 2026-10-02 | 25 (missing 2, inverted 0, wick 22, duplicate 0, unparsable 0, forming 1) | 2026-10-04 |
| BRENT | BZ=F | 1d | 4759 | 2007-07-30 | 2026-10-02 | 16 (missing 0, inverted 6, wick 9, duplicate 0, unparsable 0, forming 1) | 2026-10-04 |
| US500 | ^GSPC | 1d | 6728 | 2000-01-03 | 2026-10-02 | 0 | 2026-10-04 |
| US100 | ^NDX | 1d | 6727 | 2000-01-03 | 2026-10-02 | 1 (missing 0, inverted 0, wick 1, duplicate 0, unparsable 0, forming 0) | 2026-10-04 |
| US30 | ^DJI | 1d | 6728 | 2000-01-03 | 2026-10-02 | 0 | 2026-10-04 |
| DE40 | ^GDAXI | 1d | 6794 | 2000-01-03 | 2026-10-02 | 0 | 2026-10-04 |
| UK100 | ^FTSE | 1d | 6758 | 2000-01-04 | 2026-10-02 | 0 | 2026-10-04 |
| JP225 | ^N225 | 1d | 6551 | 2000-01-04 | 2026-10-02 | 0 | 2026-10-04 |
| BTCUSD | BTC-USD | 1d | 4363 | 2014-09-17 | 2026-10-03 | 38 (missing 0, inverted 0, wick 37, duplicate 0, unparsable 0, forming 1) | 2026-10-04 |
| ETHUSD | ETH-USD | 1d | 3186 | 2017-11-09 | 2026-10-03 | 66 (missing 0, inverted 0, wick 65, duplicate 0, unparsable 0, forming 1) | 2026-10-04 |
| XAUUSD | GC=F | 1h | 13645 | 2024-05-17 | 2026-10-02 | 1 (missing 0, inverted 0, wick 0, duplicate 0, unparsable 0, forming 1) | 2026-10-04 |

## Notes

- CARRY-D (the EUR/USD carry-gated trend) is not in this report: carry_strategy.py computes only the last bar's state and does not expose its historical entries and exits, and 15 trades could not pass a gate anyway; it is information-only.
- Results are in R after costs (§1.4): one round trip per trade plus financing for every calendar night held (indices charge longs and shorts differently).
- Daily setups enter at the next bar's open; PB-H1-GOLD at the signal bar's close and is flat at the close of the first bar outside 07:00-16:00 London.
- One open trade per instrument per setup and exit: a signal read while a trade is open is not taken, so the trade set differs by exit. A trade still open when the data ends is not counted.
- In-sample and out-of-sample are split by the signal date; signals before 2006-01-01 are warm-up only. Crypto is in-sample to 2019-12-31.
- The t-statistics pool trades across instruments and treat them as independent; trades that overlap in time in correlated markets make the true uncertainty larger.
- E2: the last quarter's trailing stop starts once the first three quarters have filled; before that E2 is E1.
- Yahoo's continuous futures (GC=F, SI=F, CL=F, BZ=F) are not back-adjusted: contract rolls are gaps in the data.
- Hourly gold is as much history as Yahoo serves for 1h bars; the 4-hour trend uses UTC-aligned 4-hour blocks built from them.
