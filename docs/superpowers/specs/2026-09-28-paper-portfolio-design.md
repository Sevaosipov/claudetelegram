# Paper portfolio

Date: 2026-09-28. This is part 1 of a three-part trading strategy project:

1. **The paper portfolio** (this document).
2. **History data**: 20 years of SEC insider trades, turned into the bot's signals.
3. **History test**: it replays every rule combination and names the official stock book and crypto book.

The bot never places real trades. It runs virtual books on strict rules, and the user decides what to do with the results.

## Goal

Start the 6-month paper-trading clock now for every candidate rule combination at once. Whichever combination the history test (part 3) later picks will already have a paper record from day one. The choice is fixed by the history test, not by whichever paper book looks best, so running many books doesn't let luck pick the strategy.

## Decisions (from the design conversation)

| | Decision |
|---|---|
| How the strategy proves itself | A history test picks the rules; a live paper portfolio confirms them; real money is considered only after the success line |
| Structure | Two sleeves: stocks against the S&P 500, crypto against Bitcoin. Each book has its own money: €80,000 for stocks, €20,000 for crypto |
| Stock buy rules | Two candidates, R1 and R2. The history test chooses on 2006–2018 and confirms on 2019–2026 |
| Stock sizing | Equal slices: 10% of the book's current value per buy, at most 10 positions, the rest in cash |
| Stock exits | Four candidates, E1–E4. The history test chooses |
| Profit target | A fixed +25% target (E4) is tested on history. The analyst-target version runs only as a paper shadow |
| Crypto | Two variants, C-A (signals) and C-B (200-day trend plus signals), both run in paper |
| Success line | After 6 months, per book: return above the benchmark, a smaller worst drop than the benchmark, and at least 20 completed trades for a stock book |
| Telegram | A monthly report only. There are no per-trade messages until part 3 names the official books |

## 1. The books

The books are created on the first run, and that date is every book's start date.

### Stock books (8 = 2 buy rules × 4 exits)

**Buy rules:**
- **R1:** every stock signal in the day's Сильный list.
- **R2:** R1 plus every stock signal in the day's Кандидат list with a score of 70 or more.

**Exits:** each is checked on daily closes, and the first rule that holds sells.

| | Sells on |
|---|---|
| **E1** | (a) an insider named in the signal sells after the buy date (the same sources as `positions._insider_sale`); (b) 90 days held; (c) the return reaches −15% |
| **E2** | 182 days held; no stop-loss |
| **E3** | 91 days held, or the return reaches −15% |
| **E4** | E1, or the return reaches +25% |

- **"Stock signal"** means a signal whose ticker is not a `CRYPTO:` ticker. Congressional crypto clusters never enter a stock book.
- **Which signals count:** the ones in the day's `strategy.Selection` built by the daily run. It already applies the 3-day recency window, Trading 212 availability, the size floors and the tiers. Politician, Oslo, Sweden and Germany signals take part like US insider signals.

### Analyst-target shadow (1 book): "R1·E1+аналитики"

- R1 with E1, plus a sale when the price reaches the analysts' consensus target recorded at the buy.
- If there was no target at the buy, it behaves exactly like R1·E1.
- It is marked "(тень)" in every view, and part 3 never picks it as official.

### Crypto books (2)

**Coins:** BTC and ETH. SOL joins when crypto piece 2 lands. Each coin's target share is the book value divided by the number of coins.

**C-A (signals):**
- **Buy** a coin on a Сильный crypto signal (a `CryptoSignal` with tier `strong`) when the book doesn't already hold it.
- **Sell** on the first of these:
  - a caution on the coin, journaled within the last 7 days and on or after the buy date, confirmed by the price (the rule in `positions._crypto_caution`);
  - 90 days held;
  - a return of −25% (the coin stop, until the range-based stop exists).

**C-B (trend plus signals):**
- **Hold** a coin while its close is above its 200-day average, or for 30 days after a Сильный signal on it.
- **Sell** when neither holds.
- **Caution:** a price-confirmed caution sells the coin at once, and it can't be bought again for 7 days.

## 2. Mechanics

### Orders and fills

- Anything the bot decides on day D, whether a buy or a sale, becomes an order that fills at the **first daily close after D**.
- An order with no price for 5 trading days is cancelled and recorded as «не исполнено».

### Sizing

- **Stock books:**
  - a buy uses 10% of the book's value at the last close;
  - if cash is less than a full slice, it uses the cash available, provided that's at least half a slice; otherwise it skips («нет денег»);
  - a book with 10 open positions skips a new signal («мест нет»);
  - every skip is recorded.
- **Crypto books:** a buy uses the coin's target share, or the cash available if that's less.
- **Repeats:** a ticker or coin that is already held, or has an order pending, is not ordered again.

### Prices and currency

- **Which listing:**
  - each stock is priced on the listing of its signal's source, through the same symbol mapping as `/bought` positions (`positions.yahoo_symbol`);
  - coins are priced in USD;
  - the series come through `sources.price_history`.
- **Returns use adjusted closes**, so splits and dividends are handled. A position's value in EUR is its cost after fees × (adjusted close now ÷ adjusted close on the fill day) × (FX now ÷ FX on the fill day).
  - Both closes are taken from one series fetched the same day, so the ratio is consistent.
  - The FX rate at the fill is stored with the position.
- **Missing prices:** a position with no price on a day keeps its last value.

### Costs

These are charged on every buy and every sale:
- a stock in another currency: 0.25%;
- a stock in EUR: 0.10%;
- a coin: 0.50%.

Each is a settings constant.

### Benchmarks and scores

- **Stock books** are compared with SPY (adjusted, so dividends are included) in EUR.
- **Crypto books** are compared with BTC in EUR.
- Both benchmarks are indexed from the start date.
- Each book stores its value, cash and benchmark value every day.
- **Worst drop:** the largest fall from a peak to a later low in the daily values, for the book and for its benchmark.

### Success line

For each book, from 182 days after the start:

- **«пройдено»:**
  - the return is above the benchmark's return;
  - the worst drop is smaller than the benchmark's;
  - for a stock book, at least 20 completed trades.
- **«не пройдено»:** otherwise.
- **«идёт»:** before day 182, shown as "день N из 182".

## 3. What the user sees

**Menu option «3) Бумажный портфель»:**
- one line per book: return, difference to the benchmark, worst drop, completed trades, open positions and status;
- the lines are grouped under АКЦИИ and КРИПТО, each with its benchmark's return and worst drop;
- the day counter is at the top.

**`python paper.py BOOK`** (e.g. `R1-E2`) shows that book's open positions and every trade: date, ticker, reason for the buy or sale, price and result, plus the recorded skips.

**Telegram:**
- one report on the first daily run of each month, with the same lines as the menu plus each sleeve's result for the month;
- no messages per trade.

## 4. Storage, daily run, failures

**New tables:**
- **`paper_books`:** code, sleeve, rules, starting money, start date.
- **`paper_orders`:**
  - fields: book, ticker/coin, side, reason, created date, status (pending / filled / cancelled);
  - the source and the signal's insiders, for a buy.
- **`paper_positions`:**
  - fields: book, ticker, source, fill date, cost after fees, adjusted close and FX at the fill, the insiders behind the signal, and the analyst target at the buy;
  - on close: close date, reason and result.
- **`paper_equity`:** book, date, value, cash, benchmark value.

**Daily run:**
- `paper.run(conn, selection)` runs in `bot.main` after the digest and close alerts. It runs whether or not Telegram is on, and it gets the same `Selection` the digest used.
- For each book, in order:
  1. fill pending orders at the new close;
  2. check exits;
  3. place new orders from today's signals;
  4. store the day's values.
- Then it sends the monthly report if it's due, marked once per month in the key–value cache.

**Failures:**
- One book failing is logged and skipped, and the others and the daily run continue.
- The whole pass runs through `bot._run_source("PAPER", …)`, so a crash is reported by the existing failed-source warning.

## 5. Testing (offline, with stubbed prices)

- **Fills:** the next close, never the signal day; costs are charged on both sides.
- **Sizing:**
  - 10% of the current value;
  - a partial slice when at least half a slice of cash is left;
  - skips for «нет денег» and «мест нет»;
  - no repeat of a held or pending ticker.
- **Exits:**
  - each of E1–E4, including an insider sale after the buy (one before the buy doesn't count), the 90/91/182-day limits, −15% and +25%;
  - the analyst shadow sells at its target, or behaves like R1·E1 without one.
- **Crypto:**
  - C-A buys on a Сильный crypto signal and sells on a price-confirmed caution, at 90 days or at −25%;
  - C-B follows the 200-day average, holds 30 days after a signal, sells on a caution, and blocks a re-buy for 7 days.
- **Values:** the adjusted-close ratio with FX; the daily values; the worst drop; the benchmark index.
- **Success line:** «идёт» before day 182, «пройдено» and «не пройдено», and a stock book stuck under 20 trades.
- **Output:** the monthly report goes once per month and not on other days; the menu view; `paper.py BOOK`.
- **Robustness:** a failing book doesn't stop the others; an order with no price for 5 days is cancelled.

## Out of scope

- The 20-year history data (part 2) and the history test (part 3).
- Trade messages for the official books; part 3 switches them on.
- SOL in the crypto books (crypto piece 2).
- The range-based coin stop (the parked range design).
- Real trading of any kind.
