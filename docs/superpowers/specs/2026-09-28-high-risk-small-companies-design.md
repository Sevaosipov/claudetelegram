# High risk, high reward: small-company insider buying

Date: 2026-09-28. This is the first of two high-risk additions to the trading strategy:
1. **Small companies** (this document).
2. **Coins with real buying behind them**, built together with crypto piece 2 (more coins).

It builds on the paper portfolio (`docs/superpowers/specs/2026-09-28-paper-portfolio-design.md`). Everything runs on paper first. The bot never trades for real, and these signals are not pushed to Telegram until their books prove themselves.

## Goal

Insider buying in small companies is where research has found the largest gains after insider purchases. Today the bot throws these signals away: `strategy.py` drops anything under a €300M market value or €1M a day of trading.

This piece does three things:
- it keeps the small-company signals that clear rules scaled to the company's size;
- it shows them in the menu;
- it trades them in two new paper books, so their track record builds up alongside the main books.

In the past year of stored SEC data:
- 20 companies worth €50–300M had insider buying, 9 of them with two or more insiders;
- 18 of those 20 traded more than €100k a day.

That is roughly 2–5 candidate signals a month.

## Decisions (from the design conversation)

| | Decision |
|---|---|
| What "high risk" means here | Small companies (this piece) and coins with company/ETF buying (a later piece). No leverage |
| Qualifying rule | Scaled to company size: purchases of at least 0.1% of the company's value |
| Books | Two books side by side, H1 and H2, so the history test can choose between them |
| Sizing | 20% of the book per buy, at most 5 positions |
| Where the signals appear | In the menu's Сигналы only, not pushed until the books pass their test |

## 1. Small-company signals

**Which companies count**
- Market value at least **€50M** and under **€300M**.
- At least **€100k traded per day**.
- A company whose market value or trading volume is unknown doesn't qualify.
- The main signals' filters still apply:
  - buy side only;
  - disclosed within the last 3 days (`cluster/recency.py`);
  - buyable on Trading 212.

**Sources:** SEC and Oslo (`source` in `SEC`, `NORWAY`). These are the insider sources that carry buyer roles and a tradable ticker.
- Politicians (House/Senate) are out, since the rules depend on insider roles.
- 13D/G stakes are out.
- Coins are out.
- BaFin and FI are out, because they are keyed by ISIN.

**Qualifying rule.** "Management insiders" are buyers whose role is in `cluster.roles.INSIDER_ROLES`: ceo, cfo, chair, officer, director, insider. A signal qualifies when either holds:
1. **two or more management insiders** bought, and together their purchases are at least **0.1%** of the company's market value;
2. **a CEO or CFO** (role `ceo` or `cfo`) bought at least **0.1%** of the company's market value on their own.

**Tier and the rule line**
- A qualifying signal gets the tier `high_risk`.
- It carries one ✓ line with the size relative to the company, e.g. «CEO купил 0,40% компании» or «3 инсайдера купили вместе 0,18% компании».
- A signal in the band that doesn't qualify is dropped, as it is today.
- Signals at €300M and above go through the main tier rules, unchanged.

**Where the signals go**
- **Selection:** a new list, `Selection.high_risk`. The main `strong` and `candidates` lists are unchanged.
- **Journal:** new high-risk signals are recorded in `signal_journal` with `tier = 'high_risk'` on the run that finds them, and marked alerted. Like the caution signals, they are journaled without being sent.
- **Menu:** the menu's Сигналы shows a section «🎲 Высокий риск (N)» after Кандидаты, with the usual signal layout and the ✓ line.
- **Telegram:** the daily digest never includes them.

## 2. The small-company books

The paper portfolio gets two books in a new sleeve, `small`.

| Book | Money | Buy | Size | Sells on |
|---|---|---|---|---|
| **H1** | €20,000 | every signal in `Selection.high_risk` | 20% of the book's value, at most 5 positions | 182 days held (no stop-loss) |
| **H2** | €20,000 | the same | the same | 91 days held, −30%, or +50% |

**Mechanics.** Everything else is the paper portfolio's own machinery:
- fills at the first completed close after the decision;
- costs of 0.25% (in another currency) or 0.10% (in EUR) per side;
- values from Yahoo's adjusted closes, in EUR;
- a partial slice with at least half a slice of cash, otherwise «нет денег»;
- «мест нет» when full;
- no repeats of a held or pending ticker;
- a sale with no price for 5 business days closes at the last value;
- a book that fails is logged and skipped.

**Benchmark.** The Russell 2000 (**IWM**, adjusted, in EUR), indexed from the book's start date.

**Success line.** The same as the stock books:
- after 182 days, the return beats IWM's;
- the worst drop is smaller than IWM's;
- at least 20 completed trades;
- until it has 20 trades, the book shows «идёт (сделок N из 20)».

**Start date.** Existing databases get H1 and H2 on the next run (`create_books` inserts missing books). Their start date is that run, which may be later than the main books' start.

**Where you see them**
- In the paper portfolio view and the monthly report, as a third group: «ВЫСОКИЙ РИСК (Russell 2000: x%, худшая просадка y%)».
- When that group's start date differs from the header's, the group header also shows «с дд.мм.гггг».
- `python paper.py H1` shows one book's detail.

That makes 13 books in total. Part 3's history test judges H1 against H2 on the 20-year SEC history, which includes small companies.

## 3. Failures

- A market value or trading volume that can't be looked up means the signal is not high-risk. It is skipped silently, as today's floors do.
- A failing small-company book is logged and skipped, like any other book.

## 4. Testing (offline, with stubbed sizes and prices)

- **The band:**
  - €49M is out;
  - €50M and €299M are in;
  - €300M goes to the main rules;
  - under €100k a day is out;
  - an unknown size is out.
- **The rule:**
  - two management insiders together at 0.1% qualify, and at 0.09% they don't;
  - one CEO at 0.1% qualifies, and one CFO too;
  - a lone director doesn't;
  - a politician cluster doesn't;
  - a coin doesn't;
  - the 3-day window and the Trading 212 filter apply.
- **The rule line:** the ✓ text states the percentage.
- **Where they go:**
  - journaled with tier `high_risk` and not re-journaled on the next run;
  - shown in the menu's section;
  - never in the digest.
- **The books:**
  - H1 sells only at 182 days;
  - H2 sells at 91 days, −30% or +50%;
  - 20% slices and 5 positions;
  - IWM benchmark;
  - the «ВЫСОКИЙ РИСК» group and its own start date in the view;
  - `paper.py H1`.

## Out of scope

- The high-risk coin sleeve (built with crypto piece 2).
- Pushing high-risk signals to Telegram (only after the books pass).
- Leverage of any kind.
- Real trading.
