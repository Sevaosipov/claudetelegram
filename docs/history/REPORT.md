# The stock rules on 20 years of SEC data: the results

Run 2026-10-10T14:18:05. The rules of the test are in PREREGISTRATION.md, written before this run; they are the bot's own, nothing was fitted.

Candidate cluster days: 10949 on 3614 tickers; with prices: 4758 (43 %). Buy signals: 781.

**Reading, as fixed beforehand: not shown either way: both halves above zero, but the excess is not distinguishable from chance.**

Survivorship: companies later delisted have no prices at Yahoo and are missing, so every figure below is too good by an unknown amount.

## A. The event study: the return after a buy signal, and over the S&P 500

### 2006–2025

| Trading days | Signals | Mean | Median | Above zero | t | Mean over SPY | t of the excess |
|---|---|---|---|---|---|---|---|
| 20 | 781 | +3.7 % | +1.4 % | 58 % | +3.21 | +2.6 % | +2.30 |
| 60 | 781 | +7.6 % | +2.8 % | 56 % | +5.39 | +4.4 % | +3.23 |
| 126 | 780 | +16.5 % | +3.7 % | 56 % | +2.55 | +9.8 % | +1.51 |
| 252 | 751 | +23.1 % | +5.2 % | 56 % | +5.15 | +9.2 % | +2.09 |

### 2006–2015

| Trading days | Signals | Mean | Median | Above zero | t | Mean over SPY | t of the excess |
|---|---|---|---|---|---|---|---|
| 20 | 283 | +2.3 % | +1.1 % | 55 % | +2.21 | +2.2 % | +2.30 |
| 60 | 283 | +2.2 % | +1.6 % | 55 % | +1.70 | +1.4 % | +1.12 |
| 126 | 283 | +3.6 % | +3.5 % | 57 % | +1.78 | +1.0 % | +0.55 |
| 252 | 283 | +11.1 % | +7.2 % | 61 % | +3.88 | +2.6 % | +1.01 |

### 2016–2025

| Trading days | Signals | Mean | Median | Above zero | t | Mean over SPY | t of the excess |
|---|---|---|---|---|---|---|---|
| 20 | 498 | +4.5 % | +2.0 % | 59 % | +2.63 | +2.8 % | +1.66 |
| 60 | 498 | +10.7 % | +3.4 % | 56 % | +5.14 | +6.1 % | +3.04 |
| 126 | 497 | +23.9 % | +4.0 % | 55 % | +2.36 | +14.8 % | +1.47 |
| 252 | 468 | +30.3 % | +3.8 % | 53 % | +4.36 | +13.1 % | +1.91 |

## B. The bot's exits: trailing stop, dead money, one year; 0.30 % a round trip

| Years | Trades | Mean | Median | In profit | t | Profit factor | Mean days held | Mean over SPY | t of the excess | Exits |
|---|---|---|---|---|---|---|---|---|---|---|
| 2006–2025 | 781 | +7.0 % | -1.9 % | 45 % | +2.24 | 2.31 | 67 | +5.0 % | +1.62 | dead 116, stop 650, time 15 |
| 2006–2015 | 283 | +3.4 % | -0.9 % | 46 % | +2.38 | 1.65 | 77 | +1.9 % | +1.54 | dead 61, stop 214, time 8 |
| 2016–2025 | 498 | +9.1 % | -2.2 % | 44 % | +1.88 | 2.66 | 62 | +6.8 % | +1.41 | dead 55, stop 436, time 7 |

## By year

| Year | Candidate days | With prices | Signals with a 126-day result | Mean over SPY at 126 days | Trades | Mean per trade | Mean over SPY per trade |
|---|---|---|---|---|---|---|---|
| 2006 | 449 | 97 | 19 | -11.6 % | 19 | -2.5 % | -4.4 % |
| 2007 | 725 | 179 | 33 | -3.3 % | 33 | +0.4 % | -0.7 % |
| 2008 | 891 | 268 | 33 | +18.8 % | 33 | +2.6 % | +9.3 % |
| 2009 | 366 | 123 | 20 | +15.5 % | 20 | +8.9 % | +6.2 % |
| 2010 | 286 | 78 | 28 | -7.2 % | 28 | +1.2 % | -2.2 % |
| 2011 | 517 | 158 | 23 | +3.3 % | 23 | +1.4 % | +0.6 % |
| 2012 | 347 | 106 | 27 | +0.6 % | 27 | +9.2 % | +2.1 % |
| 2013 | 345 | 101 | 38 | +3.0 % | 38 | +7.5 % | +1.8 % |
| 2014 | 514 | 156 | 24 | -6.9 % | 24 | +0.7 % | -1.5 % |
| 2015 | 639 | 257 | 38 | -4.0 % | 38 | +2.7 % | +4.9 % |
| 2016 | 563 | 208 | 34 | +6.2 % | 35 | +13.0 % | +6.4 % |
| 2017 | 441 | 140 | 33 | +140.7 % | 33 | +68.0 % | +63.7 % |
| 2018 | 587 | 255 | 42 | +27.8 % | 42 | +20.9 % | +21.9 % |
| 2019 | 651 | 329 | 39 | -2.5 % | 39 | +3.1 % | -0.1 % |
| 2020 | 890 | 522 | 66 | +9.8 % | 66 | +5.9 % | +3.2 % |
| 2021 | 534 | 293 | 58 | -4.7 % | 58 | -1.4 % | -3.8 % |
| 2022 | 593 | 348 | 51 | +4.9 % | 51 | -3.3 % | -1.1 % |
| 2023 | 508 | 332 | 47 | +6.0 % | 47 | +6.5 % | +2.4 % |
| 2024 | 451 | 305 | 52 | +9.4 % | 52 | +1.5 % | -1.1 % |
| 2025 | 652 | 503 | 75 | +0.4 % | 75 | +4.4 % | +2.1 % |

## For information: what the averages are made of (looked at after the run, not part of the reading)

The mean excess over SPY at 126 days is +9.8 %, but the median signal is 2.7 % **behind** SPY. The mean is carried
by a handful of signals:

| Left out | Signals | Mean over SPY at 126 days | t | Median |
|---|---|---|---|---|
| none | 780 | +9.8 % | +1.51 | −2.7 % |
| the best 1 (HIVE, May 2017: +4 724 %, a $0.30 stock in the crypto run) | 779 | +3.7 % | +1.63 | −2.8 % |
| the best 3 | 777 | +1.3 % | +0.91 | −2.8 % |
| the best 8 | 772 | −0.1 % | −0.10 | −3.0 % |

So one signal in a hundred decides whether the rules beat the market, and the typical signal does slightly worse
than the index. The same shows in the trades (B): the mean is +7.0 % and the median −1.9 %, with 45 % in profit --
the trailing stop cuts most trades at a small loss and a few run far.

Two things make even this too good. Half of the tickers (1 814 of 3 614) have no prices at Yahoo -- mostly companies
later delisted -- and the candidate days with prices are 43 % of all, fewer in the early years (22 % in 2006). And
the live score has a liquidity floor (a company under €20 million or trading under €100 000 a day is not bought)
that the test could not apply: a $0.30 stock like the best signal here would likely not have passed it.
