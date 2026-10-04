# Exploratory notes (not part of the pre-registered gate)

Run 2026-10-05, after the pre-registered run in BACKTEST_REPORT.md failed every gate line. These numbers were
produced to understand *why* it failed. They change no decision: nothing is enabled.

## 1. Is it the costs? No.

The same backtests with **no costs at all** (pooled, 23 instruments):

| Setup | Exit | In-sample 2006–2016 | Out-of-sample 2017–2026 |
|---|---|---|---|
| BO-D (55-day breakout) | E0 trailing | PF 1.31, mean R +0.136, t 2.87 | PF 1.04, mean R +0.017, t 0.41 |
| BO-D | E1 four fixed take-profits | PF 1.15, mean R +0.075 | PF 0.97, mean R −0.016 |
| BO-D | E2 three take-profits + runner | PF 1.14, mean R +0.070 | PF 0.95, mean R −0.026 |
| PB-D (daily pullback) | E0 trailing | PF 1.16, mean R +0.076, t 1.45 | PF 0.93, mean R −0.033 |
| PB-D | E1 | PF 1.00, mean R −0.003 | PF 0.95, mean R −0.024 |
| PB-D | E2 | PF 0.96, mean R −0.022 | PF 0.95, mean R −0.024 |

With the round trip but no overnight financing the picture is the same (BO-D E0: IS PF 1.28, OOS 1.01).

Reading: the breakout had a real gross edge in 2006–2016 and none in 2017–2026. The pullback never had one out of
sample. The pre-registered costs (overnight financing above all, on holds of 25–60 days) turn "nothing" into a clear
loss; they are not what hides an edge.

## 2. Staged take-profits lower the result everywhere

In every setup and period the four-stage exits (E1, E2) score below the trailing exit (E0) they replace: the
breakout's in-sample profit factor drops from 1.31 to 1.15, the pullback's from 1.16 to 1.00. Closing quarters at
1R–3R cuts the large winners a trend exit lives on, and the longer hold (38–59 days against 25) pays more financing.

## 3. The gold 1-hour port does not reproduce the TradingView result

On Yahoo's hourly COMEX futures bars (2024-05 … 2026-10, about 2 years): E0 profit factor 0.91 (434 trades), against
1.17 out of sample on OANDA spot in ~/forex-daytrader. Different data (futures against spot, other session hours,
only two years), so this neither confirms nor refutes that test; it means the setup cannot be signalled from this
data source with any confidence. With four take-profits: profit factor 0.70.

## 4. One class stood out, and it is not enabled

Crypto (BTC, ETH), with the pre-registered costs:

| Setup | Exit | In-sample (to 2019) | Out-of-sample (2020–2026) |
|---|---|---|---|
| BO-D | E0 trailing | PF 5.63, mean R +1.37, 35 trades | PF 2.12, mean R +0.44, t 2.25, 78 trades |
| BO-D | E1 four take-profits | PF 1.94, mean R +0.32, 54 trades | PF 1.38, mean R +0.15, t 1.28, 97 trades |
| PB-D | E0 trailing | PF 2.08, mean R +0.45, 84 trades | PF 1.31, mean R +0.15, t 1.11, 170 trades |

The gate evaluates a class only for a setup that passed pooled, and none did, so this is a class picked after
seeing the results: two highly correlated instruments, under a hundred out-of-sample trades. It is a lead, not a
finding. The bot's investing signals already follow the same idea without leverage (a coin is a buy while its trend
is up).

## What was not done

No further setups were tried after seeing these results. Searching on until something passes is how a false edge is
found; ~/forex-daytrader stopped its own search for the same reason.
