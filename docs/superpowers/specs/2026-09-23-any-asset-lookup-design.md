# Any-asset lookup with a one-month outlook

Date: 2026-09-23. This is piece A of two. Piece B, crypto news and social monitoring,
is designed separately after this one ships; it can reuse this piece's crypto lookup
and news sources.

## Goal

Type any ticker in Telegram or the terminal and get what the bot knows about it,
plus a clearly labelled **outlook**: how often the price was higher one month later
in similar past situations. This works for:

- US stocks and ETFs;
- European stocks written with an exchange suffix;
- any sizeable crypto coin.

Every kind of data has fallback sources, so a lookup still arrives when one site is
down.

Today `BTC` is looked up as a US stock ticker, i.e. the Grayscale Bitcoin Mini
Trust ETF. That wrong answer is fixed as part of this work.

## Decisions (from the design conversation)

| | Decision |
|---|---|
| Outlook content | A direction call: the probability that the price is higher in a month, always next to its base rate, sample size and walk-forward check |
| Horizon | 1 month: 21 trading days for stocks, 30 days for crypto |
| "Up" | The price itself is higher, not "beats the market" |
| Asset scope | US stocks/ETFs, any crypto coin, and European stocks with an exchange suffix (`EQNR.OL`, `VOLV-B.ST`, `SAP.DE`). ISINs are out of scope |
| Method | Historical analogues: 18 coarse "situations" counted across many assets and years. Not a fitted model |
| Availability | Each kind of data has a chain of independent free sources, tried in order |

## 1. Resolving what the user typed (`assets.py`)

`resolve(text) -> Asset | None`, one function used by Telegram, the menu and the CLI.
Rules, in order:

1. `$XYZ` is always a stock.
2. `CRYPTO:XYZ` or `XYZ-USD` is a crypto coin.
3. A symbol with an exchange suffix (`.OL .ST .DE .CO .HE .PA .AS .L .SW .MI .MC`) is
   a stock on that exchange.
4. A symbol in the coin list is a crypto coin. The coin list is the ~250 largest coins
   by market cap, cached daily (source chain in §4). Crypto wins a clash, so `BTC` is
   bitcoin and `SOL` is Solana.
5. Anything else is a US stock or ETF.
6. A symbol no source recognises returns None. The reply is
   "не нашёл такой тикер" plus a hint about `$`, suffixes and `-USD`.

`Asset` holds these fields:

- `kind` (`stock` | `crypto`)
- `symbol` (display symbol, e.g. `BTC`, `EQNR.OL`)
- `yahoo` (e.g. `BTC-USD`, `EQNR.OL`)
- `tradingview` (e.g. `CRYPTO:BTCUSD`, or None to let tradingview.py resolve it)
- `exchange` (suffix or None)
- `name`, when known

The bot's other data keys stay as they are: a crypto asset maps to the `CRYPTO:BTC`
ticker used by the signal tables (crypto.ticker).

## 2. The outlook (`outlook.py`)

### Situation

A situation is three coarse facts from daily closes, giving 3 × 3 × 2 = 18 situations.

- **Trend:**
  - `up` when close > SMA50 > SMA200;
  - `down` when close < SMA50 < SMA200;
  - `mixed` otherwise.
- **Last month's move:** the return r over the last 21 trading days (30 days for
  crypto), against the asset's normal monthly swing σ_m. Here σ_m is the standard
  deviation of daily returns over the past 252 observations × √21 (√30 for crypto).
  - `strong_down` when r < −σ_m;
  - `strong_up` when r > +σ_m;
  - `flat` otherwise.
- **Volatility:** `high` when the current 21-day daily-return standard deviation is
  above the 75th percentile of its rolling values over the past 252 observations;
  `normal` otherwise.

At least 252 + 21 observations of history are required; with fewer the result is
"мало истории для прогноза".

**Fallback when no daily history is available:** TradingView's `close`, `SMA50`,
`SMA200`, `Perf.1M` and `Volatility.M`.

- Trend comes from close vs SMA50/SMA200.
- The move uses `Perf.1M` against σ_m ≈ `Volatility.M`/100 × √21.
- Volatility can't be classified this way, so the lookup uses that trend × move pair
  pooled over both volatility states, and says "(без учёта волатильности)".

### Tables

There are two tables, built from observations sampled **once per asset per month**
(every 21st trading day, or every 30th day for crypto), so overlapping days don't
inflate the counts.

- **stocks:** the S&P 100 ∪ Nasdaq-100 list (universe.py), with full available
  daily history (~15 years). European stocks use this table, and the message notes
  "(таблица по акциям США)".
- **crypto:** the 50 largest coins by market cap, minus stablecoins, keeping those
  with ≥ 2 years of history (~30 coins).

An observation at day t has label `close[t+h] > close[t]`, where h = 21 (stocks) or
30 (crypto).

Per situation the table stores:

- `n` and `up` (all history);
- the base rate of the table;
- the walk-forward result.

### Walk-forward check

For each calendar year Y, from the table's 5th year to the last complete year:

1. Compute the situation rates p_s and base rate b from observations before Y.
2. Score year Y's observations with p_s and with b.

A situation **has an edge** when both hold:

- across all its out-of-sample observations, the Brier score with p_s is lower than
  with b;
- it has n_oos ≥ 50.

Situations without an edge show "нет преимущества над базовой частотой (обычно X%)"
instead of a situation number.

### Output line (Telegram and terminal)

```
📈 Прогноз на месяц: рост в 61% похожих ситуаций (n = 4 210) · обычно 56% · проверено на истории
   ситуация: тренд вверх · месяц сильный рост · волатильность обычная
   у самой NVDA в такой ситуации: 64% (n = 36)
```

- The third line appears only when the asset itself has ≥ 30 past observations in
  that situation.
- The block always ends with a one-line note: "частота в прошлом, не гарантия".

### Refresh

- **Price storage:** a new `price_bars(symbol, date, close)` table holds daily closes
  for the universes and is topped up with only the new days.
- **Tables:** `outlook_table(table_name, situation, n, up, oos_n, oos_brier_s,
  oos_brier_base, base_rate, built_at)` holds the counts and check results.
- **When it rebuilds:** `outlook.refresh_if_stale(conn)` runs in the daily run and
  rebuilds a table older than 7 days. `python outlook.py --rebuild` forces a rebuild.
- **Lookups never train;** they read the stored tables. For the asset being looked
  up, recent bars (~2 years) are fetched through the price chain to place it in a
  situation.

## 3. The lookup (`research.py` + `crypto_research.py`)

`research.build(conn, text_or_ticker)` resolves the asset first, then dispatches on
its kind.

**Stocks:** today's dossier, unchanged apart from these:

- the **outlook** section is added;
- news, analyst targets, current price and technical indicators go through the
  source chains in §4;
- European stocks work as far as their data allows: price, technicals, news and
  outlook. The SEC and insider sections are empty for them, as today.

**Crypto** (new `crypto_research.build(conn, asset)`) contains:

- **price:** now, 7-day / 30-day / 1-year change, trend vs SMA50/200, volatility;
- **TradingView's** technical rating;
- **the bot's own data** for the coin's `CRYPTO:` ticker: company treasury buys and
  sales, spot-ETF flows, Congress purchases, and on-chain exchange flows when enabled;
- **latest headlines;**
- **the outlook.**

There is no Buy/Hold/Avoid score for crypto (opinion.py's factors don't exist for
coins); the outlook is its main number.

**Telegram** (`telegram_bot.py`):

- The existing flow stays: the message resolves to an asset → queue →
  run_claude_analysis.sh → one merged reply.
- `_extract_ticker` is replaced by `assets.resolve`.
- `claude_analysis_prompt.txt` gets:
  - a crypto branch (print the crypto dossier's key lines);
  - the outlook block, included verbatim;
  - an instruction to present the outlook as a historical frequency, not a promise.
- The fallback reply (Claude run fails) includes the outlook too.

**Terminal:**

- menu option 2 is renamed "Досье по тикеру или монете";
- `python research.py BTC` works;
- both accept every form in §1.

## 4. Source chains (`sources.py`)

Each function tries its sources in order and returns `(data, source_name)`, or
`(None, None)` when all fail. A source failure (network error, HTTP error, empty or
unparseable reply) moves on to the next source. All of the sources below were
verified keyless and answering on 2026-09-23.

| Function | Chain |
|---|---|
| `price_history(asset, days)` | stocks: Yahoo (yfinance) → Nasdaq API `api/quote/{T}/historical` (US only); crypto: Yahoo (`SYM-USD`) → Binance `api/v3/klines` (SYMUSDT) → Bybit `v5/market/kline` → Kraken `0/public/OHLC` |
| `current_price(asset)` | Yahoo → TradingView scanner `close` → stocks: Nasdaq quote / crypto: CoinGecko `simple/price` → Binance ticker |
| `indicators(asset)` | TradingView scanner → computed locally from `price_history` (RSI 14, SMA50/200) |
| `analyst_targets(asset)` | stocks only: Yahoo → Nasdaq API `api/analyst/{T}/targetprice` (US only) |
| `news(asset)` | Yahoo → Google News RSS (query: name + ticker) → crypto: CoinDesk RSS + Cointelegraph RSS filtered by coin name/symbol |
| `coin_list()` | CoinGecko `coins/markets` (top 250) → CoinPaprika `v1/tickers` → the built-in crypto.SYMBOLS |

Verified as unusable, so not included: Stooq (bot check), Euronext chart data
(encrypted), CoinGecko history beyond 365 days (needs a key).

**Messages:** a section filled from a non-first source says so briefly, e.g.
"цены: Binance (Yahoo недоступен)". A section whose whole chain failed says
"недоступно сейчас", and the rest of the lookup still arrives.

**Caching:** `price_history` results for the universes live in `price_bars`, and the
coin list is cached for a day. Per-lookup fetches aren't persisted beyond the
existing caches.

## 5. Error handling

- An unknown symbol gives the hint reply (§1); nothing is queued.
- The outlook tables not built yet → "прогноз ещё не готов (строится раз в неделю)".
- Under 273 observations of history and no TradingView fallback → "мало истории для
  прогноза".
- Every chain fails for a section → "недоступно сейчас" for that section only.
- A Claude analysis failure → the existing deterministic fallback reply, now
  including the outlook.

## 6. Testing (offline, like the rest of the suite)

- **Situations:** synthetic price paths map to each trend / move / volatility value,
  plus the TradingView fallback (pooled over volatility).
- **Tables:** counts from a small fixed set of bars; monthly sampling means no
  overlapping observations.
- **Walk-forward:** a situation that is random noise gets no edge; a planted real
  effect gets an edge; n_oos < 50 gets no edge.
- **Resolver:** `BTC`, `$BTC`, `btc-usd`, `CRYPTO:SOL`, `SOL`, `EQNR.OL`, `NVDA`,
  unknown, and the coin list falling back to the built-in list.
- **Source chains:** each chain with stubbed providers covers first-success,
  fallback-on-error, all-fail, and the "source" note.
- **Crypto dossier:** built from stubbed chains and seeded signal tables.
- **Telegram:** `BTC` routes to the crypto dossier, the unknown-symbol reply, and the
  fallback reply including the outlook.
- **Menu option 2 and `research.py` CLI:** both accept the new forms.

## Out of scope

- Crypto news and social monitoring (piece B).
- X/Twitter (paid API; decided in piece B).
- ISIN lookups.
- A fitted prediction model.
- Changing the signal strategy.
