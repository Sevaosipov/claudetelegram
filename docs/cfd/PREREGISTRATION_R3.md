# CFD research, round 3 (forex): pre-registration

Written 2026-10-08, before any round-3 code or run. The user asked for forex signals ("test both"). Rounds 1–2 found
no forex edge in trend, pullback or carry-gated trend. This round tests the two remaining ideas with a mechanism, and
nothing else. They are the sixth and seventh hypotheses on this data, so the significance bar is Bonferroni for seven
at 5 %: one-sided t ≥ 2.45.

Each idea is judged on its natural exit. Rounds 1–2 showed that staged take-profits lower every result; live signals
would show TP checkpoints only.

Everything not restated is as in rounds 1–2: data hygiene (15 % wick rule), ATR(14) Wilder, entry at the next open, a
stop gapped through fills at the open, stop before any other exit within a bar, the skip rule (R ≤ 0.25 × ATR or
R > 6 × ATR), one open trade per instrument, the result in R after costs, in-sample 2006-01-01…2016-12-31,
out-of-sample from 2017-01-01.

## H6. FX-REV: range reversion in pairs of linked economies

**Universe** (Yahoo daily): `EURCHF=X`, `EURGBP=X`, `AUDNZD=X`, `AUDCAD=X`, `NZDCAD=X`, `USDCAD=X`, `EURNOK=X`,
`EURSEK=X`.

**Signal** at the close of bar t, when ADX(14) < 20 (no trend):
- long when close < SMA20 − 2.0 × SD20;
- short when close > SMA20 + 2.0 × SD20.

SD20 is the population standard deviation of the last 20 closes.

**Entry** at the open of bar t+1.

**Stop** = signal close ∓ 2.0 × ATR. R is measured from the actual entry.

**Exit**, the earliest of:
- the stop;
- the next open after the first close at or beyond SMA20 (long: close ≥ SMA20; short: close ≤ SMA20);
- the open of the 16th bar after entry.

**Costs:** round trip 0.030 % (USDCAD 0.015 %; EURNOK and EURSEK 0.060 %), financing 0.010 % per night.

**Gate:**
- out-of-sample at least 100 trades;
- out-of-sample profit factor ≥ 1.10;
- out-of-sample mean R > 0 with t ≥ 2.45;
- in-sample mean R > 0.

Pooled over the universe, no per-pair picking.

## H7. CARRY-BASKET: the three highest-yielding majors against the three lowest

**Currencies:** USD, EUR, GBP, JPY, AUD, CAD, CHF, NZD.

**Rates:** the OECD 3-month series of round 2 (`cfd/rates.py`), with the same publication rule (month M usable from
the 15th of M+1).

**Rebalance** on the first daily bar of each calendar month:
- rank the eight by the rate in force that day (a currency with no rate is left out; at least six are needed);
- long the top three, short the bottom three, equal weights;
- held to the first bar of the next month.

**A month's return** = mean(long legs) − mean(short legs). A leg's return, against USD, = its spot change over the
month (from the `XXXUSD=X` close, or the inverse of `USDXXX=X`; USD itself is 0) + (its rate − the USD rate) ÷ 12 ÷
100.

**Costs** per month:
- 0.015 % × 2 for every leg that enters or leaves the basket, weighted 1/3;
- a financing markup of 0.004 % per night on each of the six legs, weighted 1/3 (= 0.008 % × the nights in the
  month).

**Statistics** are on monthly returns:
- profit factor = sum of positive months ÷ |sum of negative months|;
- t on the mean month;
- the worst peak-to-trough in %.

**Gate:**
- out-of-sample at least 100 months;
- out-of-sample profit factor ≥ 1.10;
- out-of-sample mean month > 0 with t ≥ 2.45;
- in-sample mean month > 0.

## Outputs

- `docs/cfd/BACKTEST_REPORT_R3.md`: per hypothesis, the in-sample and out-of-sample table and the gate table. For
  information only: per-pair results for H6, and per-year results for H7.
- A `round3` block merged into `docs/cfd/enabled.json`.

A hypothesis that fails is not traded. There is no further round on this data.
