# The stock rules on history, part 2: exits, liquidity, the missing companies, activist stakes

Written 2026-10-10, after part 1 (REPORT.md) and before any code of part 2 ran. Part 1 found the insider-cluster
signals "not shown either way": the mean beat the S&P 500 only through a handful of signals, the median did not.
Part 2 asks four further questions. Each is fixed here first.

## 1. The exits (no new data)

Part 1 closed 650 of 781 trades at the trailing stop. Does another way of leaving keep more of the result?

The same 781 signals, each closed five ways:

| Name | Rule |
|---|---|
| E0 | the bot's: trailing stop of 3 typical daily moves (10–25 %) under the highest close, dead money (60 days under +5 %), 365 days |
| E1 | a wide trailing stop: 6 typical daily moves (20–40 %), 365 days, no dead-money exit |
| E2 | no stop: out after 126 trading days |
| E3 | no stop: out after 252 trading days |
| E4 | a fixed stop 25 % under the entry (not trailing), else out after 252 trading days |

Costs 0.30 % a round trip; the excess is over SPY for the same days.

**Choice and check.** Signals filed in 2006–2015 choose: the exit with the highest **median** excess per trade (the
mean is carried by a few signals and would choose by luck). Signals filed in 2016–2025 check it: the chosen exit
is "better than the bot's" only if there both its median and its mean excess per trade are above E0's. Otherwise
the answer is "no exit shown to be better", and nothing is to change.

Even when confirmed, this changes nothing in the bot by itself: when the bot tells the user to sell is the user's
decision.

## 2. Liquidity

The live score does not buy a stock that trades under €100 000 a day; part 1 could not apply that. Part 1 is
repeated with it: a signal counts only when the mean of close × volume over the 20 bars before the entry is
$100 000 or more (Yahoo volume, adjusted with the price). The company-size floor (€20 million) still cannot be
applied: no historical share counts. Reported: the same tables and the same reading as part 1.

## 3. The companies with no prices

Half of the tickers have no prices at Yahoo. No free source without a sign-up has them (Stooq now requires a
browser check that a script must not pass). Two things are done instead:

- **A sensitivity table.** The candidate days with no prices are assumed to turn into signals at the same rate as
  those with prices; the table gives the overall mean excess at 126 days if those missing signals had an excess of
  0 %, −10 %, −25 % and −50 %, and the excess at which the overall mean is zero.
- **A loader for Tiingo** (which keeps delisted tickers), used when a `TIINGO_API_KEY` is set: its free tier allows
  500 tickers a month, so it is the user's to switch on.

## 4. Activist stakes (13D)

The signals the user actually received came from Schedule 13D filings; part 1 could not test that path.

- **Data:** EDGAR's quarterly form index, the initial "SC 13D" and "SCHEDULE 13D" filings (no amendments) of 2006
  Q1–2025 Q4 whose subject company has a ticker in the SEC's current list. For each, the head of the filing gives
  the subject company and the cover page's "percent of class"; a filing whose percent cannot be read is left out
  and counted.
- **The signal:** the bot's `stake_part` for an activist — 30 points plus 2 for each point of stake above 5 %, up
  to 50; nothing for 50 % or more (control) — plus `momentum_part`; a buy at 60 or more, with 21 closes. As in
  part 1 the news, the triggers and the size floors are not reproduced. One signal per company in 30 days and
  while its trade is open.
- **Measured and read** exactly as part 1 (A, B, both halves, by year, the same three-way reading), with the same
  flaw: companies since delisted are missing, and a 13D is often the first step of a buyout, whose target is
  then delisted -- so the missing half is likely the more favourable one here, and the result too *low* rather
  than too high. Which way it cuts is not known; the report says so.
