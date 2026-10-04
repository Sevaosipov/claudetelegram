# More coins

Date: 2026-10-04. Requested by the user: "lets add more coins now". The scope was chosen earlier: SOL with ETF flows,
and company buying for SOL, XRP, BNB, DOGE, AVAX, HYPE, LTC, ENA, LINK, TRX and SUI, on top of BTC and ETH.

The bot holds no portfolio. A coin gets a weekly buy signal when its score says BUY, and a sell alert only for a
position the user recorded with `/bought`.

## What was checked on the live sources (2026-10-04)

- **Price history** (`sources.price_history`, 420 days):
  - SOL, XRP, BNB, DOGE, AVAX, LTC, ENA, LINK, TRX: full, from Yahoo.
  - SUI: full, from Binance. Yahoo has no `SUI-USD`.
  - HYPE:
    - Yahoo has no `HYPE-USD`;
    - Binance has only 11 days (listed 2026-09-24);
    - Bybit has the full 421 days.

    Today's "first source that answers" rule returns Binance's 11 bars, which is too few to score.
- **Farside ETF flows:**
  - `https://farside.co.uk/sol/` exists, with 7 funds and about 14 recent days. A typical day is $0–90M, and a
    notable 3-day run is about $100M.
  - There is no all-data page for SOL (404).
  - The other ten coins have no page.
- **Company purchases:** EDGAR full-text search works per coin. Today's parser and queries cover BTC and ETH only.

## Decisions

### 1. Coins

`model.COINS = ("BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "AVAX", "HYPE", "LTC", "ENA", "LINK", "TRX", "SUI")`.
`model.MAJOR_COINS = ("BTC", "ETH")`. The other eleven are "alts".

**`crypto.py` knows all thirteen:**
- `SYMBOLS` gains BNB, HYPE, ENA, TRX, SUI. The existing extras (ADA, DOT, BCH, ETC) stay.
- `COINGECKO_IDS` gains: `binancecoin`, `hyperliquid`, `ethena`, `tron`, `sui`.
- `ALIASES` gains: «hyperliquid» → HYPE, «ethena» → ENA, «binance coin» → BNB, «bnb» → BNB, «tron» → TRX, and «sui» →
  SUI. The existing ordering rule (longest names first) is kept.
- The result: `/bought HYPE 90`, `/sold SUI` and the lookups work.

### 2. Prices

- **`sources.price_history` for a coin** returns the first source, in today's order, that has at least
  `min(days, CRYPTO_MIN_BARS = 200)` bars. If none has that many, it returns the longest series any source gave. It
  must never return a short series while a longer one is available. Stocks are unchanged.
- **`positions.last_close` for a coin** (and anything else that prices a coin by `_yahoo_close`) falls back to the
  last completed bar of the multi-source daily series when Yahoo has no symbol for it (HYPE, SUI). A `/bought HYPE`
  position then has a price, a stop and alerts. The existing behaviour stays for coins Yahoo does price.

### 3. ETF flows for SOL

- `crypto_etf.FARSIDE_URLS` gains `"SOL": "https://farside.co.uk/sol/"`.
- `FARSIDE_ALL_URLS` has no SOL entry. `fetch_farside(coin, full_history=True)` for a coin with no all-data page
  falls back to the recent page. Nothing may raise KeyError for SOL. The first runs build its history day by day.
- **Thresholds are per coin** (`cluster/crypto.py`). SOL's funds are an order of magnitude smaller than bitcoin's,
  and BTC and ETH keep today's numbers:

| | BTC, ETH | SOL |
|---|---|---|
| Fixed day bar (under 30 days of history) | $400M | $50M |
| Fixed 3-day bar | $500M | $100M |
| Day floor (relative rule) | $100M | $25M |
| 3-day floor | $250M | $60M |

- Implement this as dicts keyed by coin with a default (`ETF_DAY_FLOW_USD` and the others become per-coin lookups).
  Every existing BTC/ETH test must pass unchanged.

### 4. Company purchases for the eleven alts (`crypto_treasury.py`)

**Queries.** `QUERIES` gains one query per coin:
- SOL `"solana"`
- XRP `"XRP"`
- BNB `"BNB"`
- DOGE `"dogecoin"`
- AVAX `"AVAX"`
- HYPE `"hyperliquid"`
- LTC `"litecoin"`
- ENA `"ethena"`
- LINK `"chainlink"`
- TRX `"TRX" OR "TRON"`
- SUI `"SUI"`

**Parsing.** `_COIN_WORDS`, `_PROSE_RE` and the table regex learn the new coins:
- **Full names** (solana, dogecoin, litecoin, avalanche, chainlink, hyperliquid, ethena) match in any case, as
  bitcoin does.
- **Tickers** (SOL, XRP, BNB, DOGE, AVAX, HYPE, LTC, ENA, LINK, TRX, TRON, SUI) match **only in upper case**, with
  an optional «tokens»/«coins» word after them. "link", "hype", "sol" and "sui" are ordinary words in filings.
- The existing safeguards stay: past-tense verb, a unit count, not mining/production, no "up to".

**Plausible prices.** `PLAUSIBLE_PRICE_USD` gains sanity bounds per coin:
- SOL (5, 5 000)
- XRP (0.05, 100)
- BNB (20, 20 000)
- DOGE (0.005, 20)
- AVAX (1, 2 000)
- HYPE (1, 5 000)
- LTC (5, 5 000)
- ENA (0.02, 100)
- LINK (1, 2 000)
- TRX (0.01, 20)
- SUI (0.1, 500)

**Signal floors** per coin (`cluster/crypto.py`):
- weekly demand floor: €50M for BTC and ETH (as today), €10M for the alts;
- a company sale counts as a caution from €10M for BTC and ETH (as today), €5M for the alts.

**Run cost.** The daily pass scans every coin's query. EDGAR's existing retry and pause rules apply. One coin's
query failing must not lose the others: collect what succeeded, and report the source as failed only when every
query failed. Document what the current behaviour is and keep it at least as robust.

### 5. Scoring

- **`score_coin` is unchanged in its parts.** Trend, flows and news work for any coin. Stops use the "crypto" clamp
  (15–35 %).
- **Bitcoin regime filter for alts.** An alt's decision can be BUY only when bitcoin's own trend is up:
  `coin_trend(BTC closes)["above_ma100"]` is true. Otherwise the alt's decision is WATCH with the reason «биткоин
  ниже 100-дн. средней — альты не покупаем». BTC and ETH are not gated. The gate is applied in `model._score_coins`,
  and `score_coin` gets a keyword `btc_up: bool | None = None` (None = not gated).
- **A coin with too little history** (under 121 completed closes) stays WATCH «мало истории», as today.
- **News:** the coin red flags are unchanged.

### 6. Weekly picks and the message

- **`signals_weekly.pick_buys`:** at most `WEEKLY_COIN_LIMIT = 2` coins among the week's (at most 5) picks: the two
  highest-scoring coins. Stocks fill the rest.
- **The buy line for an alt** ends with «, высокий риск»:
  `🟢 <b>SOL!</b>: покупка — выше 100-дн. средней; приток в ETF; балл 75, стоп −22%, высокий риск`. BTC and ETH have
  no tag.
  - Store the flag with the picked signal: add a `risk` field to the kept pick JSON. Don't add a column to
    `buy_signals` unless it is needed.
- **The menu's scored list and the analyst's context** show all thirteen coins. The analyst method's line about
  coins names the list.

### 7. Backfill

`python crypto_treasury.py --backfill N` covers the new coins. The existing command needs no interface change. The
controller runs it after the merge. It isn't part of the tests.

## Tests (offline)

- **`crypto.py`:** the new symbols and aliases resolve (`symbol_for_text`, `is_crypto`, `ticker`). "tron" inside
  "electronic" is not a coin, and "sui" inside "suite" is not either. Keep or extend the existing word-boundary
  behaviour.
- **`sources.price_history`** (stub sources):
  - Yahoo empty, Binance 11 bars, Bybit 421 → Bybit;
  - every source short → the longest;
  - a first source with ≥200 bars → it wins;
  - stocks unchanged.
- **`positions.last_close`** for a coin Yahoo doesn't know → the last completed bar of the multi-source series.
  `/bought HYPE` stores a stop. A trailing-stop alert fires for it.
- **Farside:** SOL parses from a fixture of the real page shape (7 funds). `full_history=True` for SOL uses the
  recent page. `run_farside_pass` includes SOL.
- **ETF thresholds:** a $60M SOL day with under 30 days of history → a signal; a $30M one → none. BTC's $400M bar
  is unchanged. The relative rule uses SOL's floors.
- **Treasury parsing**, one positive and one negative per new coin, e.g.:
  - "purchased 1,250,000 SOL at an average price of $182.40" → SOL;
  - "acquired 5,000,000 HYPE tokens" → HYPE;
  - "the link to the press release" → nothing;
  - "purchased 10,000 sol" (lower case) → nothing;
  - "generated hype" → nothing;
  - a price outside the plausible bounds → rejected.
- **Treasury floors:** an alt week of €12M → a signal (above the alt floor); €8M → none; BTC's €50M is unchanged.
  An alt sale of €6M → a caution; €4M → none.
- **Scoring:**
  - all thirteen coins are scored;
  - an alt with an up-trend and score ≥ 60 is BUY when BTC is above its 100-day average, and WATCH with the reason
    when BTC is below;
  - ETH is not gated;
  - short history → WATCH.
- **Picks:** five coin BUYs and three stock BUYs → 2 coins (the highest) plus 3 stocks. The alt line carries
  «высокий риск», and BTC's doesn't.
- **A queries-robustness test:** one coin's query raising doesn't lose the other coins' filings.

## README

The crypto section lists the thirteen coins and what feeds each:
- trend for all;
- ETF flows for BTC, ETH and SOL;
- company purchases for all;
- news for all.

It also covers the bitcoin regime filter, the two-coins-a-week limit, and the «высокий риск» tag.

## Amendment (2026-10-04, after the live check)

A live check on real prices scored 12 of 13 coins BUY (a broad uptrend), with AVAX and ENA ahead only because of
keyword "positive news" (+10): crypto headlines say "upgrade" for network upgrades. Two binding rulings follow. They
supersede what sections 5 ("News: the coin red flags are unchanged") and 6 ("the two highest-scoring coins") say; the
rest of the spec stands.

### R1. A coin's positive headlines add no points

- `news_part(headlines, coin=True)` gives at most 0. A positive headline adds nothing, and is not counted, so it is not
  listed among the reasons (it would explain no points). A negative headline still subtracts (-10 each, not below
  -30). A red flag still blocks; the coin red flags are unchanged.
- A mixed list scores as its negatives alone: "downgrade", "upgrade", "raises guidance" is -10 for a coin (for a stock
  it is 0).
- A coin's news is therefore -30..0, in `news_part` and in `score_coin`. Stocks are unchanged (-30..+10, `NEWS_MAX`
  stays 10).
- Tests: a coin's good headlines score 0 and leave no reason line; a network "upgrade" is not news for a coin and
  +5 for a stock; negatives still subtract and good ones do not offset them; a red flag still blocks; the stock tests
  are untouched; no alt gets an edge from keyword news. The README line and the analyst method say it.

### R2. Which two coins the week signals

- The week's coin places (at most `WEEKLY_COIN_LIMIT = 2`, among the at most 5 picks) go first to BTC and ETH
  (`model.MAJOR_COINS`, in that order) when they are a BUY and eligible -- whatever the alts score -- then to the alts by
  total score, equal totals broken by the coin's 60-day return, the highest first. A score with no 60-day return sorts
  last among equal totals (equals stay as they came).
- A coin that is not eligible (held, signalled in the last 30 days, not a BUY) does not use up a place: BTC held or
  signalled lately, and the places go to ETH and then the best alt; both majors out, to the two best alts.
- The 60-day return is `CoinScore.ret60`, a fraction taken from `coin_trend` (None under 121 closes), and a field of
  the cached-score JSON (`model_scored_<day>`); a kept score without it (written before it existed) has none.
  It is not part of the score.
- The chosen coins then compete with the stocks for the five places by score, as before: the picks are listed highest
  score first, equal scores as they came for stocks, stocks before coins and coins in their own order. Stocks' ordering
  is unchanged. A week with fewer stocks has fewer picks, not a third coin. The majors keep the coin places even when
  five stocks outscore them: an alt does not take their place.
- Tests: five alt BUYs at the same score give the two with the highest `ret60`; BTC and ETH BUY alongside higher-scoring
  alts are the two picked; BTC held, or signalled within 30 days, gives its place to ETH and then the best alt; a major
  that is not a BUY takes no place; a missing `ret60` sorts last; the return reaches the kept scores; the week's kept
  picks (`weekly_buys_<week>`) follow the same rule. The README and the analyst method say it.

### R3. The coin places are reserved (supersedes R2's last two bullets on competition and order)

Coin and stock scores are on different scales (a coin is 0-100 from trend, flows and news; a stock from insiders, triggers,
momentum and news), so they no longer compete for the places:

- `pick_buys` first takes up to `WEEKLY_COIN_LIMIT` (2) eligible coin BUYs in the R2 order (BTC, ETH, then alts by score and
  `ret60`), then fills the remaining places, `WEEKLY_BUY_LIMIT - coins picked`, with stock BUYs by score (equal scores as they
  came). With no eligible coin the stocks take all five; with few stocks there are fewer picks, never a third coin. A coin
  that is held, signalled in the last 30 days or not a BUY takes no place (BTC out: ETH, then the best alt).
- One order for sending: the stocks by score first, then the coins (BTC and ETH, then the alts). (R2 said the picks were
  listed by score; this replaces it.) The kept picks (`weekly_buys_<week>`) and the messages follow that order.
- Tests: five stock BUYs at 70+ and BTC and ETH BUY at 60 give three stocks, BTC and ETH; one eligible coin gives four stocks and
  it; none gives five stocks; an alt scoring 99 does not take a major's place; the alts take the places the majors leave.
  README, analyst method and `weekly.py` say it.
