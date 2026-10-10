# The stock rules on history, part 2: the results

Run 2026-10-10T17:57:13. The rules are in PREREGISTRATION_2.md, written before this ran.

## What part 2 says, in short

1. **Exits:** no other exit is shown to be better than the bot's. Every one of the five leaves the median trade
   behind the S&P 500 in both halves.
2. **Liquidity:** with the floor the live bot already applies ($100 000 traded a day), 694 of the 781 signals
   remain and the pre-set reading is "the rules hold up": +5.2 % over the S&P 500 at 126 days (t 2.08), above zero
   in both halves. That is only just past the bar, and part 1 said a result that only just holds up is to be read
   as not shown; what it does show is that the illiquid signals were the worse ones, so the floor earns its place.
3. **The missing companies:** if the signals of the companies with no prices averaged 7.5 % behind the S&P 500,
   the whole edge of part 1 is gone. That is a small number for companies that were later delisted.
4. **Activist stakes (13D):** the rules do **not** hold up. 711 signals: the median is behind the index at every
   horizon, 37 % of trades end in profit, and with the bot's exits the mean trade is 1.2 % behind the S&P 500 in
   both halves. The one large mean (+19.6 % at 126 days in 2016–2025) is 2021's handful of runaway stocks. This is
   the path that produced the user's RXO and SSTI signals.

Two limits of section 4: the stake could not be read in 2 213 of 11 101 filings (20 %), and 2025 is all but missing
(3 candidates) -- since late 2024 a 13D is filed as structured data and its cover page no longer has the words the
reader looks for.

## 1. The exits

| Exit | Rule | Half | Trades | Mean | Median | In profit | Mean over SPY | Median over SPY |
|---|---|---|---|---|---|---|---|---|
| E0 | the bot's: trailing 10–25 %, dead money, 365 days | 2006–2015 | 283 | +3.4 % | -0.9 % | 46 % | +1.9 % | -2.3 % |
| E0 | the bot's: trailing 10–25 %, dead money, 365 days | 2016–2025 | 498 | +9.1 % | -2.2 % | 44 % | +6.8 % | -3.6 % |
| E1 | wide trailing 20–40 %, 365 days | 2006–2015 | 283 | +4.3 % | -1.7 % | 47 % | -0.5 % | -6.1 % |
| E1 | wide trailing 20–40 %, 365 days | 2016–2025 | 498 | +24.2 % | -6.1 % | 41 % | +17.5 % | -8.6 % |
| E2 | no stop, 126 trading days | 2006–2015 | 283 | +3.3 % | +3.2 % | 56 % | +0.7 % | -1.5 % |
| E2 | no stop, 126 trading days | 2016–2025 | 498 | +23.6 % | +3.7 % | 55 % | +14.5 % | -3.5 % |
| E3 | no stop, 252 trading days | 2006–2015 | 283 | +10.8 % | +6.9 % | 61 % | +2.3 % | -1.1 % |
| E3 | no stop, 252 trading days | 2016–2025 | 498 | +29.0 % | +3.6 % | 54 % | +11.8 % | -9.5 % |
| E4 | fixed stop −25 % from the entry, 252 trading days | 2006–2015 | 283 | +4.9 % | +1.1 % | 51 % | -1.1 % | -8.2 % |
| E4 | fixed stop −25 % from the entry, 252 trading days | 2016–2025 | 498 | +23.6 % | -8.5 % | 45 % | +13.4 % | -13.9 % |

**Reading, as fixed beforehand: the first half chooses E3, the second half does not confirm it: no exit shown to be better.**

## 2. Liquidity: only signals trading $100 000 a day or more

694 of 781 signals pass the floor.

Candidate cluster days: 10949 on 3614 tickers; with prices: 4758 (43 %). Buy signals: 694.

**Reading, as fixed beforehand: the rules hold up: both halves above zero, t of the 126-day excess 2 or more.**

Survivorship: companies later delisted have no prices at Yahoo and are missing, so every figure below is too good by an unknown amount.

## A. The event study: the return after a buy signal, and over the S&P 500

### 2006–2025

| Trading days | Signals | Mean | Median | Above zero | t | Mean over SPY | t of the excess |
|---|---|---|---|---|---|---|---|
| 20 | 694 | +2.4 % | +1.8 % | 58 % | +3.64 | +1.4 % | +2.26 |
| 60 | 694 | +7.7 % | +3.6 % | 58 % | +5.83 | +4.4 % | +3.51 |
| 126 | 694 | +11.9 % | +5.0 % | 57 % | +4.68 | +5.2 % | +2.08 |
| 252 | 666 | +23.6 % | +5.8 % | 58 % | +5.38 | +9.5 % | +2.22 |

### 2006–2015

| Trading days | Signals | Mean | Median | Above zero | t | Mean over SPY | t of the excess |
|---|---|---|---|---|---|---|---|
| 20 | 235 | +1.3 % | +1.2 % | 55 % | +1.66 | +1.4 % | +1.99 |
| 60 | 235 | +3.4 % | +2.9 % | 58 % | +2.34 | +2.7 % | +2.01 |
| 126 | 235 | +5.8 % | +5.0 % | 61 % | +2.87 | +3.8 % | +2.02 |
| 252 | 235 | +15.0 % | +8.2 % | 63 % | +4.79 | +6.7 % | +2.34 |

### 2016–2025

| Trading days | Signals | Mean | Median | Above zero | t | Mean over SPY | t of the excess |
|---|---|---|---|---|---|---|---|
| 20 | 459 | +3.0 % | +2.2 % | 60 % | +3.25 | +1.4 % | +1.61 |
| 60 | 459 | +9.9 % | +4.0 % | 57 % | +5.37 | +5.2 % | +2.98 |
| 126 | 459 | +15.0 % | +4.3 % | 56 % | +4.06 | +5.9 % | +1.62 |
| 252 | 431 | +28.3 % | +4.1 % | 54 % | +4.32 | +11.1 % | +1.71 |

## B. The bot's exits: trailing stop, dead money, one year; 0.30 % a round trip

| Years | Trades | Mean | Median | In profit | t | Profit factor | Mean days held | Mean over SPY | t of the excess | Exits |
|---|---|---|---|---|---|---|---|---|---|---|
| 2006–2025 | 694 | +4.8 % | -1.7 % | 46 % | +3.09 | 1.92 | 69 | +2.7 % | +1.84 | dead 98, stop 581, time 15 |
| 2006–2015 | 235 | +4.9 % | -0.1 % | 49 % | +3.06 | 2.06 | 83 | +3.4 % | +2.46 | dead 52, stop 175, time 8 |
| 2016–2025 | 459 | +4.7 % | -2.2 % | 44 % | +2.15 | 1.86 | 63 | +2.4 % | +1.11 | dead 46, stop 406, time 7 |

## By year

| Year | Candidate days | With prices | Signals with a 126-day result | Mean over SPY at 126 days | Trades | Mean per trade | Mean over SPY per trade |
|---|---|---|---|---|---|---|---|
| 2006 | 449 | 97 | 13 | +3.2 % | 13 | +4.8 % | +1.4 % |
| 2007 | 725 | 179 | 28 | -0.8 % | 28 | +2.1 % | +0.9 % |
| 2008 | 891 | 268 | 32 | +19.6 % | 32 | +2.9 % | +9.4 % |
| 2009 | 366 | 123 | 16 | +12.6 % | 16 | +13.5 % | +10.3 % |
| 2010 | 286 | 78 | 23 | -5.6 % | 23 | +2.0 % | -1.3 % |
| 2011 | 517 | 158 | 21 | +4.5 % | 21 | +1.6 % | +1.0 % |
| 2012 | 347 | 106 | 22 | +1.5 % | 22 | +11.6 % | +3.6 % |
| 2013 | 345 | 101 | 32 | +4.3 % | 32 | +8.8 % | +2.6 % |
| 2014 | 514 | 156 | 17 | -4.7 % | 17 | +3.1 % | +0.5 % |
| 2015 | 639 | 257 | 31 | -0.7 % | 31 | +1.6 % | +4.4 % |
| 2016 | 563 | 208 | 30 | +10.5 % | 30 | +15.9 % | +8.7 % |
| 2017 | 441 | 140 | 25 | +1.6 % | 25 | +3.5 % | -1.2 % |
| 2018 | 587 | 255 | 39 | +29.4 % | 39 | +22.4 % | +23.5 % |
| 2019 | 651 | 329 | 36 | -1.5 % | 36 | +3.6 % | +0.3 % |
| 2020 | 890 | 522 | 62 | +11.4 % | 62 | +3.1 % | +0.7 % |
| 2021 | 534 | 293 | 55 | -2.4 % | 55 | -0.7 % | -3.1 % |
| 2022 | 593 | 348 | 49 | +3.9 % | 49 | -2.9 % | -0.5 % |
| 2023 | 508 | 332 | 43 | +1.6 % | 43 | +5.9 % | +1.3 % |
| 2024 | 451 | 305 | 46 | +8.6 % | 46 | +0.7 % | -2.1 % |
| 2025 | 652 | 503 | 74 | +0.4 % | 74 | +3.9 % | +1.6 % |

## 3. The companies with no prices

Signals with a 126-day result: 780, mean over SPY +9.8 %. Candidate days with prices: 4758; without: 6191 -- at the same rate about 1015 more signals.

| If the missing signals ended this far from SPY | The overall mean over SPY would be |
|---|---|
| +0.0 % | +4.3 % |
| -10.0 % | -1.4 % |
| -25.0 % | -9.9 % |
| -50.0 % | -24.0 % |

The overall mean is nil when the missing signals average -7.5 % against SPY.

## 4. Activist stakes (13D)

Initial 13D filings on a company with a ticker today: 11101; percent not readable: 2213; candidates (a stake of 12.5 % to under 50 %): 2945.

Candidate cluster days: 2945 on 1883 tickers; with prices: 2597 (88 %). Buy signals: 711.

**Reading, as fixed beforehand: the rules do NOT hold up: a half is below zero.**

Survivorship: companies later delisted have no prices at Yahoo and are missing, so every figure below is too good by an unknown amount.

## A. The event study: the return after a buy signal, and over the S&P 500

### 2006–2025

| Trading days | Signals | Mean | Median | Above zero | t | Mean over SPY | t of the excess |
|---|---|---|---|---|---|---|---|
| 20 | 711 | +0.3 % | -1.0 % | 46 % | +0.30 | -0.8 % | -0.77 |
| 60 | 711 | +2.6 % | -2.5 % | 45 % | +1.23 | -0.4 % | -0.22 |
| 126 | 711 | +18.0 % | -3.1 % | 46 % | +1.16 | +11.7 % | +0.75 |
| 252 | 711 | +11.1 % | -7.3 % | 43 % | +1.52 | -1.7 % | -0.23 |

### 2006–2015

| Trading days | Signals | Mean | Median | Above zero | t | Mean over SPY | t of the excess |
|---|---|---|---|---|---|---|---|
| 20 | 293 | +0.8 % | -0.8 % | 46 % | +0.49 | -0.1 % | -0.08 |
| 60 | 293 | +2.2 % | -1.6 % | 45 % | +0.59 | +0.1 % | +0.02 |
| 126 | 293 | +4.5 % | -2.6 % | 48 % | +1.18 | +0.4 % | +0.10 |
| 252 | 293 | +3.1 % | -7.3 % | 43 % | +0.59 | -5.8 % | -1.15 |

### 2016–2025

| Trading days | Signals | Mean | Median | Above zero | t | Mean over SPY | t of the excess |
|---|---|---|---|---|---|---|---|
| 20 | 418 | -0.0 % | -1.5 % | 46 % | -0.01 | -1.2 % | -0.94 |
| 60 | 418 | +2.8 % | -2.7 % | 45 % | +1.18 | -0.8 % | -0.35 |
| 126 | 418 | +27.5 % | -3.6 % | 45 % | +1.05 | +19.6 % | +0.74 |
| 252 | 418 | +16.7 % | -7.4 % | 43 % | +1.40 | +1.3 % | +0.11 |

## B. The bot's exits: trailing stop, dead money, one year; 0.30 % a round trip

| Years | Trades | Mean | Median | In profit | t | Profit factor | Mean days held | Mean over SPY | t of the excess | Exits |
|---|---|---|---|---|---|---|---|---|---|---|
| 2006–2025 | 711 | +0.2 % | -6.0 % | 37 % | +0.18 | 1.03 | 48 | -1.2 % | -0.96 | dead 62, stop 642, time 7 |
| 2006–2015 | 293 | -0.0 % | -5.3 % | 37 % | -0.01 | 1.00 | 49 | -1.1 % | -0.65 | dead 30, stop 261, time 2 |
| 2016–2025 | 418 | +0.4 % | -6.4 % | 36 % | +0.22 | 1.04 | 46 | -1.3 % | -0.72 | dead 32, stop 381, time 5 |

## By year

| Year | Candidate days | With prices | Signals with a 126-day result | Mean over SPY at 126 days | Trades | Mean per trade | Mean over SPY per trade |
|---|---|---|---|---|---|---|---|
| 2006 | 83 | 54 | 23 | -7.1 % | 23 | +3.8 % | +3.3 % |
| 2007 | 92 | 73 | 26 | +30.0 % | 26 | -1.3 % | -1.9 % |
| 2008 | 108 | 84 | 11 | -6.8 % | 11 | -3.5 % | -0.1 % |
| 2009 | 119 | 103 | 34 | +9.9 % | 34 | +4.1 % | +1.3 % |
| 2010 | 79 | 62 | 26 | -2.3 % | 26 | -6.5 % | -6.9 % |
| 2011 | 135 | 109 | 36 | -0.3 % | 36 | -3.2 % | -3.6 % |
| 2012 | 104 | 87 | 27 | +4.4 % | 27 | +11.5 % | +9.5 % |
| 2013 | 101 | 89 | 33 | +7.1 % | 33 | +4.4 % | +0.1 % |
| 2014 | 142 | 125 | 37 | -12.7 % | 37 | -1.5 % | -2.3 % |
| 2015 | 157 | 149 | 40 | -14.6 % | 40 | -7.0 % | -6.3 % |
| 2016 | 148 | 132 | 39 | +0.7 % | 39 | +4.6 % | +1.0 % |
| 2017 | 181 | 161 | 44 | -4.3 % | 44 | +2.5 % | -1.3 % |
| 2018 | 196 | 184 | 64 | -0.5 % | 64 | +4.1 % | +4.4 % |
| 2019 | 171 | 154 | 35 | -7.3 % | 35 | +0.8 % | -0.6 % |
| 2020 | 201 | 179 | 52 | +6.6 % | 52 | +3.9 % | +1.3 % |
| 2021 | 302 | 271 | 71 | +131.4 % | 71 | -4.4 % | -6.1 % |
| 2022 | 227 | 207 | 23 | -10.4 % | 23 | -7.3 % | -4.9 % |
| 2023 | 191 | 181 | 49 | -16.2 % | 49 | -2.5 % | -4.6 % |
| 2024 | 205 | 190 | 41 | -0.1 % | 41 | -0.1 % | -1.8 % |
| 2025 | 3 | 3 | 0 | — | 0 | — | — |

