# The stock rules on 20 years of SEC data: what is tested, fixed before the run

Written 2026-10-10, before any code ran on the data. The Friday buy signals come from the stock score
(`model_score.py`), which was calibrated by hand on a few weeks of live signals and never tested on history.
This test asks one question: **did US insider-cluster buys that the score would have called a buy beat the
market afterwards?** Nothing is fitted: the rules are the bot's own, as they stand today.

## Data

- **Insider purchases:** the SEC's quarterly "Insider Transactions Data Sets" (Forms 3/4/5 as tables),
  2006 Q1 to 2025 Q4. A purchase is a Form 4 non-derivative transaction with code `P`, acquired (`A`), a positive
  price and share count. One buyer's purchases in one filing are added up; a buyer's filing under $50 000 is
  ignored (the bot's `--min-value`).
- **Prices:** Yahoo adjusted daily closes for the ticker the filing names; the S&P 500 is SPY, adjusted.

## The signal

For each company, on each day a purchase is filed, the bot's own cluster rule and score are applied to what was
known that day:

- the window is the last 14 days of filings (`SEC_WINDOW_DAYS`); a cluster needs 2 or more buyers and $100 000
  in total, or a single buyer with $500 000 (`SEC_MIN_BUYERS`, `MIN_CLUSTER_VALUE`, `SEC_SOLO_THRESHOLD`);
- **insiders points** as `model_score.insider_part`: the number of management buyers (1: 22, 2: 34, 3: 42, 4+: 46;
  roles by `cluster.roles.sec_role`), +10 for a CEO or CFO among them (else +5 for a chair), +3 or +6 when the
  biggest buyer's purchase added 10 % or 30 % to what they held; a cluster of 10 %-holders only scores 15;
- **momentum points** as `model_score.momentum_part` on the closes up to the day before the filing;
- **buy** when the points reach 60 (`STOCK_BUY`) and the stock has at least 21 closes.

**What cannot be reproduced, and is left at zero:** the purchase's size against the company's market value (up to
+10; no historical market values), a buyer's first-ever purchase (+4), the triggers (an activist 13D, politicians:
no structured history), the news (−30…+10 and the red-flag block), and the liquidity floor. So this tests the
insider-cluster path of the score alone, and the historical signals are a **subset** of what the live score would
flag: every one of them would also be a buy today, but some live buys (13D stakes above all) are not in the test.

A company is signalled once: no new signal while its trade is open, nor within 30 days of the last one
(`RESIGNAL_DAYS`).

## What is measured

**A. The event study.** Entry at the first close after the filing day. The return to the close 20, 60, 126 and 252
trading days later, and the same for SPY over the same days. Reported: the number of signals, the mean and median
return, the share above zero, the mean excess over SPY and its t statistic.

**B. The bot's exits.** The same entries, closed by the bot's own rules that prices alone decide: the trailing stop
(`stop_distance`: three typical daily moves, 10–25 %, below the highest close since entry, fixed at entry), "dead
money" (60 business days held and a return under 5 %), and 365 days. Costs: 0.30 % a round trip (Trading 212's
currency fee both ways). Reported per trade: the mean and median return, the share in profit, the profit factor,
the mean holding time and the mean excess over SPY over the same days.

Both are reported for all years, for 2006–2015 and 2016–2025, and by year.

## How to read it — fixed now

- The rules **hold up** if, in both halves, the mean excess over SPY at 126 days (A) and the mean excess per trade
  (B) are above zero, and over all years the t statistic of the 126-day excess is 2 or more.
- They **do not** if either half is below zero.
- Anything between is "not shown either way".

## The known flaw

Yahoo has no prices for most companies that were delisted — bankrupt, bought, merged. Those signals drop out, and
the failures among them drop out with them, so **the results are too good by an unknown amount** (survivorship
bias). The report gives the share of signals with prices, by year, so the size of the hole is visible. A result
that only just holds up should be read as not shown.

## Note on the first run (2026-10-10)

The first run of the report was made on prices for only 849 of the 3 614 tickers: Yahoo began refusing requests
part-way through the download, and the loader recorded every refused ticker as "no prices". That report was
discarded and is not a result. The loader now keeps "no prices" for a ticker only from a batch in which Yahoo
answered for another one, and stops when a whole batch comes back empty. The test is to be run once the remaining
prices are in (`python history_test.py prices`, repeated until nothing is missing, then `report`); the rules and
the reading above are unchanged.
