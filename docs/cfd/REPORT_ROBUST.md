# Two checks (2026-10-12): the live crypto breakout, and stock indices

Code: cfd/research_robust.py. Neither is a new pre-registered test. The crypto breakout was found on all of its
history, so no unseen data exists for it; what follows are questions about that one result. The index part is a
comparison, not a search.

## A. The crypto breakout (55-day high above the 200-day average, trailing exit, 13 coins)

| 2020 on | trades | sum | per trade | t | PF |
|---|---|---|---|---|---|
| all | 445 | +125.2R | +0.28R | 2.6 | 1.65 |
| without the best 5 trades | 440 | +43.9R | +0.10R | 1.6 | 1.23 |
| without the best 10 | 435 | +18.2R | +0.04R | 0.7 | 1.09 |
| longs | 284 | +136.2R | +0.48R | 3.0 | 2.09 |
| shorts | 161 | -11.0R | -0.07R | -0.9 | 0.84 |

By year: 2020 +50.7R, 2021 +66.6R, 2022 -1.1R, 2023 +7.9R, 2024 -5.0R, 2025 -11.0R, 2026 +17.1R (to October).
By month of exit (the coins move together, so months are the fairer count): 80 months, t 2.2, 28 of them up.
Neighbouring settings (40/55/70-day high x 2.5/3/3.5 ATR trail): all nine profitable, PF 1.44-1.84.

Reading:
- **It is not a fluke of one setting** -- every neighbour is profitable.
- **It is a few very large trades:** ten trades of 445 are 85 % of the profit. Without them it is break-even.
- **It is the longs of two bull years:** 2020-2021 made 117R of the 125R; the four years 2022-2025 together lost
  9R on 271 trades. The shorts lose.
- So it is trend-following on crypto: it pays when a strong rally comes and bleeds slowly while waiting. That is a
  real, known behaviour, not an edge that pays steadily -- and nothing here can say when the next rally comes.
- One coin in three is down over the period (LTC -12.3R, TRX -3.9R, SUI, HYPE); eight are up.

## B. Stock indices as CFDs: long only above the 200-day average, against holding

Costs: 0.03 % a round trip, 0.02 % of the position every night (about 7 % a year). Price indices, so dividends
(1-2 % a year, which a long CFD is credited) are not in either column.

| | holding, a year | worst fall | above the average only, a year | worst fall | switches |
|---|---|---|---|---|---|
| US500 2000-2015 | -4.3 % | 74 % | -1.3 % | 32 % | 109 |
| US100 2000-2015 | -4.5 % | 84 % | -0.2 % | 29 % | 123 |
| DE40 2000-2015 | -3.9 % | 74 % | +2.7 % | 22 % | 64 |
| US500 2016-2026 | +5.3 % | 34 % | +2.8 % | 27 % | 61 |
| US100 2016-2026 | +10.9 % | 41 % | +10.0 % | 24 % | 53 |
| DE40 2016-2026 | +0.6 % | 47 % | +0.9 % | 34 % | 80 |

Reading:
- **The nightly financing is the story.** Held as a CFD, the US500 made about 5 % a year in a decade in which the
  index itself made about 12 %. An index CFD is a poor way to hold an index.
- **The 200-day rule halves the worst falls** in every row, and beat holding in 2000-2015 (two crashes). In
  2016-2026 it made less than holding on the US indices: it is insurance, paid for in rising markets.
- With rounds 1 and 2 (the 55-day breakout and dip-buying both failed on indices), there is no index CFD rule here
  worth signalling.
