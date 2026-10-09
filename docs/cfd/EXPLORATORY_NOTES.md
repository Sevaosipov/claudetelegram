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

---

# Round 2 (run 2026-10-08): notes beside BACKTEST_REPORT_R2.md

The gate judged each hypothesis on its four-take-profit exit, as pre-registered. All three fail; nothing is enabled.

| Hypothesis | Four take-profits (judged) | Its natural exit (reported) |
|---|---|---|
| H3 crypto breakout, 11 coins never tested before | OOS PF 0.69, mean R −0.15 (391 trades) | E0 trailing: OOS PF 1.60, mean R +0.27, t 2.08 (357 trades); IS PF 1.52 (70 trades) |
| H4 index dip-buying | OOS PF 1.00; IS 0.93 | E0c close above SMA5: OOS PF 1.16, mean R +0.04, t 1.29; IS PF 1.29 |
| H5 carry-gated trend, 11 FX pairs | OOS PF 0.57; IS 0.80 | E0c original exit: OOS PF 0.53; IS 0.80 |

## The pattern across both rounds

Wherever an edge shows, it shows only with the setup's natural exit, and the four-stage take-profit removes it:
crypto breakout 1.60 → 0.69 (BTC/ETH in round 1: 2.12 → 1.38), index dips 1.16 → 1.00, the 2006–2016 breakout 1.31 →
1.15. The requirement of four take-profit stages and the requirement of a profit factor above 1.1 were not met
together by anything tested.

## What the crypto result is made of (E0, out of sample, the 11 coins)

- By coin: 8 of 11 positive (AVAX 4.02, SOL 3.10, DOGE 2.83, BNB 2.08, ENA 1.86, XRP 1.58, HYPE 1.34, LINK 1.23);
  LTC 0.51, SUI 0.84, TRX 0.84 negative.
- By year (total R): 2020 +30.8, 2021 +66.9, 2022 −3.0, 2023 +3.5, 2024 −7.8, 2025 −10.2, 2026 +14.4.
- The five largest trades (BNB Feb 2021 +26.7R, DOGE Apr 2021 +20.0R, SOL Aug 2021 +14.2R, XRP Nov 2020 +13.8R,
  DOGE Feb 2024 +5.1R) are +79.7R of the +94.7R total; the other 352 trades sum to +15.0R.

So it is a real but lottery-shaped edge: a few runaway moves pay for years of small losses, most of them came in the
2020–21 boom, and 2022–2025 was slightly negative. That is also exactly why capping a trade at +4R destroys it. It is
below the pre-registered significance bar (t 2.08 against 2.33), and trades in different coins at the same time are
not independent, so the true uncertainty is larger than the t-statistic says.

## H5

The user's EUR/USD rule does not generalise to the other pairs with OECD 3-month rates as the carry gate (the original
uses 2-year yields, which are not freely available for most of these currencies). This says nothing new about the
EUR/USD original itself.

## Round 4 (2026-10-09): what the failed results suggest — not tested, not tradeable

- **H8, against the speculators, lost in both periods** (in-sample mean −0.357R, t −3.00; out-of-sample −0.237R,
  t −1.74). A loss that consistent hints that the opposite side — trading *with* the speculators when their position
  reaches a three-year extreme — may have made money. This is read off a result already seen, including the
  out-of-sample years, so it is a hypothesis for data not yet seen, not a finding: the mirror trade has other stops
  and exits, and its out-of-sample period is spent.
- **H9, the London breakout, made a little in 2012–2016 and lost in 2017–2024** (profit factor 1.04, then 0.94).
  GBPUSD alone was positive out of sample (+76.5R over 1 676 trades, mean +0.046R); one pair of seven being positive
  is what chance gives, and picking it afterwards is the per-pair picking the pre-registration rules out.
