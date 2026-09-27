# Crypto signals: quality and caution

Date: 2026-09-27. Piece 1 of three crypto-signal improvements:

- piece 1 (this document): quality and caution;
- piece 2: more coins than BTC and ETH;
- piece 3: new kinds of signal (futures positioning, stablecoin inflows, the Coinbase premium, exchange listings).

Pieces 2 and 3 build on this one.

## Goal

Today a "Сильный" crypto signal mostly means that Strategy bought bitcoin again. Company buys fire one by one, and the routine weekly buyers fire almost every week. ETF flows cover only BlackRock's two funds. Every sell-side move (ETF outflows, companies selling, coins moving onto exchanges) is thrown away, so a coin position gets no crypto-specific close alert.

This piece does four things:

- measures every flow against its own recent normal instead of fixed amounts;
- covers all US spot ETFs;
- turns sell-side moves into caution signals, shown in the menu;
- sends a close alert for a held coin when a caution signal is confirmed by the price.

## Decisions (from the design conversation)

| | Decision |
|---|---|
| Routine company buys | Summed into one weekly company-demand total per coin, which signals only when the week is unusually large. First-time buyers are marked |
| ETF coverage | All US spot BTC and ETH funds, from Farside |
| Where caution signals go | The menu's Сигналы only; never pushed to Telegram on their own |
| Close alert for a held coin | A caution signal confirmed by the price: down over 7 days and below the 20-day average |
| Coin stop-loss | Scaled to the coin's volatility (the bottom of its usual monthly range) once the range calculation ships; 25% until then. Stocks keep 15% |

## 1. Buy-side signals

### ETF flows (`CRYPTO_ETF`)

**Source.**

- Farside publishes daily net flows per fund in $ millions for all US spot funds: 12 BTC funds at `https://farside.co.uk/btc/` and 11 ETH funds at `https://farside.co.uk/eth/`.
- The full history is at `https://farside.co.uk/bitcoin-etf-flow-all-data/` (since 11 Jan 2024) and `https://farside.co.uk/ethereum-etf-flow-all-data/` (since 23 Jul 2024).
- All four pages were verified keyless and answering on 2026-09-27.

**Parsing.**

- Negative flows are written in parentheses, e.g. `(11.8)`.
- A `-` or an empty cell means no data yet, and is skipped.
- The summary rows `Total`, `Average`, `Maximum` and `Minimum` are skipped.
- Dates look like `25 Sep 2026`.

**What counts as unusual.** Each coin's flows are summed across its funds per day. Only days before today count as history.

- **Unusual day:** the day's |net flow| is in the top 10% of |daily net flow| over the previous 126 trading days, **and** is at least $100M.
- **Unusual streak:** 3 or more consecutive days with the same sign, whose total |flow| is in the top 10% of |3-day rolling totals| over the previous 126 trading days, **and** is at least $250M.
- **Too little history:** with fewer than 30 stored trading days, the current fixed thresholds apply ($400M for a day, $500M for a streak). The signal then says "(порог по умолчанию: мало истории)".

**Staleness.** The newest day is judged only if it is at most 7 days old, the same as today's `ETF_MAX_AGE_DAYS`. Each day can alert only once; alert keys carry the date, as today.

### Company demand (`CRYPTO_TREASURY`)

**What is summed.** Per coin, the treasury purchases filed in the current calendar week, Monday to today, by filing date, in EUR.

**When it signals.**

- It signals when the week-to-date total is above the 90th percentile of the previous 52 complete weeks' totals, **and** is at least €50M.
- Weeks with no purchases count as zero in that percentile.
- With fewer than 8 weeks of history, the fixed €50M threshold applies alone, and the signal says so.
- Alert key: `coin|ISO week`. A week alerts once, the first time it crosses the threshold.

**First-time buyer.** A company (by CIK) with no purchase of that coin anywhere in stored history is marked "(впервые)". This marking is on only once the backfill below has run, so that stored history covers at least 365 days.

**Message.** The buyers and their amounts, largest first, plus how unusual the week is:

```
🏦 Покупки компаний BTC за неделю: $2.4 млрд — больше, чем в 95% недель за год
   Strategy $1.9 млрд · Metaplanet $310 млн · Acme Corp $190 млн (впервые)
```

**Routine weeks** give no signal. They remain visible in the coin's dossier (`crypto_research.py`), as today.

**Backfill.** `python crypto_treasury.py --backfill DAYS` fetches older 8-K/6-K filings from the SEC full-text search, one date slice at a time, and runs them through the existing parser.

- It keeps the SEC's pace limit (`REQUEST_PAUSE_SECONDS`).
- It skips filings already stored (`crypto_treasury_seen`), so an interrupted run resumes.
- It is run once with 365 days.

### Exchange wallets (`CRYPTO_ONCHAIN`)

Unchanged for coins leaving exchanges.

### Tier rule

Unchanged (`strategy._crypto_tier`): a buy-side crypto signal is **Сильный** when the price confirms it, meaning up over 7 days and above the 20-day average. Otherwise it is **Кандидат**.

## 2. Caution signals

These are sell-side moves, each the mirror image of a buy-side rule:

| Source | Caution when |
|---|---|
| ETF flows | An unusual outflow day or outflow streak (the same rules as §1) |
| Companies | Any single treasury sale filing of €10M or more |
| Exchange wallets | A net inflow onto the tracked exchange wallets above the current thresholds (2,000 BTC or 40,000 ETH in about 24h) |

**Tier.** Caution signals carry the tier `caution`.

**Selection.** `strategy.select` keeps them in a new `Selection.cautions` list. There is no Trading 212 or size filter. There is the same 3-day recency window as other signals.

**Journal.** They are written to `signal_journal` with `tier = 'caution'` and `kind` = the crypto kind, so a track record can be measured later.

**Display.**

- The menu's Сигналы shows them in the fourth section, next to the stock exits, as `⚠️ Осторожно: BTC — отток из спот-ETF $1.1 млрд за 3 дня`, with the price state (confirmed / not confirmed / no price).
- `telegram_notify.format_tiered_digest` renders cautions only when asked (`include_cautions=True`, from the menu).
- The daily Telegram digest never includes them.

## 3. Close alerts for held coins

`positions.check_exits` gains a coin-specific reason. For an open position in a `CRYPTO:` ticker, one alert fires on the first of the following, checked in this order:

1. **caution:**
   - a caution signal for that coin was journaled within the last 7 days, **and** on or after the position's open date;
   - **and** the price confirms it today: `crypto.price_trend` shows a 7-day return below 0 and a price below the 20-day average;
   - the check runs on every run, so a caution the price confirms a few days later still fires;
   - without a price, it waits; the Сигналы section shows "цена не проверена".
2. **time:** `EXIT_MAX_DAYS` (90) held, as today.
3. **stop_loss:**
   - coins use `EXIT_STOP_LOSS_PCT_CRYPTO = 25.0` below entry; stocks keep `EXIT_STOP_LOSS_PCT = 15.0`;
   - when the range calculation ships (the parked range design), the coin stop becomes the bottom of the coin's usual monthly range;
   - until then it is the fixed 25%.

**Message.** The close alert is pushed to Telegram, like today's close alerts:

```
🔔 Пора закрыть BTC: отток из спот-ETF $1.1 млрд за 3 дня, цена подтверждает
   (−6.2% за 7 дн., ниже 20-дн. средней) · вход $84 500 → сейчас $79 900 (−5.4%)
```

**What stays the same:**

- one alert per position (`close_alerted_at`);
- `/sold` closes the position;
- the bot never trades.

## 4. Storage

- **New table `crypto_etf_flows`:**
  - columns: `coin`, `date`, `fund`, `flow_usd`, `source` ('farside'), with `PRIMARY KEY (coin, date, fund)`;
  - loaded once from the all-data pages, then from the main pages every run;
  - the newest day is rewritten on every run, since late-reporting funds fill in over the day.
- **`crypto_etf_snapshots`** (the issuer share counts for IBIT/ETHA) stays and keeps being collected. It is the fallback.
- **`crypto_treasury_txns`** is unchanged. The backfill adds rows to it.
- **`signal_journal`** is unchanged in schema. Cautions use `tier = 'caution'`.

## 5. Error handling

- **Farside unavailable, or its newest stored day older than 3 days:** ETF signals are computed from the issuer snapshots as today and say "только IBIT/ETHA".
- **Too little history** (fewer than 30 ETF days or 8 treasury weeks): the fixed thresholds apply, and the signal says so.
- **No price for the confirmation:**
  - a buy-side signal stays Кандидат with "цена не проверена", as today;
  - a caution is shown, and the close alert waits.
- **A backfill interrupted part-way** resumes on the next run of the command. Filings already stored are skipped.

## 6. Testing (offline, like the rest of the suite)

- **Farside parsing:** from a saved page sample, covering parentheses as negative, `-` skipped, summary rows skipped, and the date format.
- **ETF thresholds:**
  - an unusual day or streak fires, and a normal one doesn't;
  - the $100M/$250M floors;
  - the fixed-threshold fallback under 30 days;
  - the fallback to issuer snapshots when Farside is stale.
- **Company demand:**
  - a routine week gives no signal, and a week above the 90th percentile gives one;
  - the €50M floor;
  - first-time buyer marking, including off before the backfill;
  - one alert per ISO week.
- **Cautions:**
  - an ETF outflow, a company sale of €10M or more (and none under it), and an exchange inflow;
  - they land in `Selection.cautions` and in the journal;
  - they are in the menu's Сигналы and absent from the Telegram digest.
- **Close alerts:**
  - a confirmed caution fires, and an unconfirmed one doesn't;
  - a caution from before the open date doesn't count;
  - a caution confirmed 3 days later fires;
  - one older than 7 days doesn't;
  - the coin stop-loss is 25% and the stock stop-loss stays 15%.
- **Backfill:** already-seen filings are skipped, and the SEC pace is kept (stubbed clock).

## Out of scope

- More coins than BTC and ETH (piece 2); Farside's SOL page is noted for it.
- New signal kinds (piece 3).
- Pushing caution signals to Telegram.
- The range calculation itself (the parked range and track-record design). This piece only leaves the 25% placeholder where the range-based stop will go.
