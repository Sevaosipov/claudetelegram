# Trading 212 account tracking

Date: 2026-10-01. Requested by the user: "can it track my real time portfolio in trading 212?" → "yes, build it".
It builds on docs/superpowers/specs/2026-10-01-my-portfolio-command.md: `/portfolio` shows the user's own
positions, and `/model` shows the model.

The bot only READS the account. It must never be able to place, change or cancel an order.

## The API (Trading 212 Public API v0, live)

- **Base:** `https://live.trading212.com/api/v0`
- **Auth:** reuse `trading212._auth_headers()`. That is HTTP Basic `base64(KEY:SECRET)`, or a bare key for old keys.
  Never log or print headers.
- **Endpoints used (GET only, this whitelist and nothing else):**
  - `GET /equity/positions` (rate limit 1 per 1 s). It returns a list of position objects:
    - `instrument {ticker, name, isin, currency}`
    - `quantity`, `quantityAvailableForTrading`, `quantityInPies`
    - `currentPrice`, `averagePricePaid` (both in the instrument's currency)
    - `createdAt` (ISO datetime)
    - `walletImpact {currency, totalCost, currentValue, unrealizedProfitLoss, fxImpact}` (in the account
      currency)
  - `GET /equity/account/summary` (rate limit 1 per 5 s). It returns:
    - `id`, `currency`, `totalValue`
    - `cash {availableToTrade, reservedForOrders, inPies}`
    - `investments {currentValue, totalCost, realizedProfitLoss, unrealizedProfitLoss}`
- **Parsing is tolerant:** a missing field becomes None, never a crash. A non-list positions payload or a
  non-dict summary raises ValueError, which is caught by the caller.
- **Errors:**
  - 401 gives `T212Error("ключ Trading 212 не подходит")`.
  - 403 gives `T212Error("ключу Trading 212 не хватает прав: нужны чтение портфеля и счёта")`.
  - 429 is retried once after 5 s, then becomes T212Error.
  - Network errors and other statuses give T212Error with the type name only.
  - No key configured gives T212Error("ключ Trading 212 не задан").

## Decisions

### 1. `t212_account.py` (new module): the client and the sync

- **`fetch_positions(session=None) -> list[T212Position]`** and **`fetch_summary(session=None) -> T212Summary`**.
- **Dataclasses:**
  - `T212Position(t212_ticker, name, isin, currency, quantity, avg_price, current_price, created_at,
    value_eur, cost_eur, pnl_eur, account_currency)`
  - `T212Summary(currency, total_value, cash_free, invested_value, invested_cost, unrealized_pnl, realized_pnl)`
- **Mapping a T212 instrument to the bot's position key:**
  - A `…_US_EQ` instrument becomes the US symbol (reuse `trading212._symbol`: AAPL_US_EQ→AAPL, BRK_B_US_EQ→BRK.B),
    with source None. Prices come from Yahoo daily closes, as for `/bought`; `averagePricePaid` is USD, the same
    currency.
  - Anything else (Frankfurt, London, Amsterdam…): the ticker is the instrument's ISIN and the source is "T212".
    - **Prices:** these positions have no Yahoo symbol. The exits use the T212 price snapshots below, in the same
      currency as `averagePricePaid`.
    - **Insiders:** looked up in the journal by the ISIN (BaFin and FI rows are ISIN-keyed). For a Norwegian ISIN,
      also by the Oslo ticker, via the reverse of norway's ISIN cache, when it is known.

### 2. Sync: `sync(conn, *, fetch=None, notify=None, now=None) -> SyncResult`

Runs every 15 minutes inside the always-on Telegram bot. A failure there is logged and the poll loop goes on.
`telegram_bot.T212_SYNC_SECONDS = 900`; the first sync runs at start. It also runs once in the daily run
(`bot.main`, before `positions.check_exits`, not on a filtered run).

With no key configured, the sync is a quiet no-op: it logs once per process and sends nothing.

**What a sync does:**
1. Fetches positions and the summary.
2. **Stores one price per held instrument per day:** `t212_prices(ticker, date, price)`, with the last
   `currentPrice` of the day replacing earlier ones.
3. **Stores one account snapshot per day:** `t212_equity(date, total_value, invested_value, invested_cost,
   cash_free, currency)`, the last of the day.
4. **New holding:** a T212 holding with no open bot position of `origin = 't212'` for its key.
   - If an open manual (`/bought`) position exists for the same key, it is converted: `origin` becomes 't212',
     and quantity and entry come from T212. No notification.
   - Otherwise a position is opened with:
     - `origin='t212'`, `quantity`, `t212_ticker`, `currency`;
     - `entry_price = averagePricePaid` and `opened_at = createdAt` date (today when missing);
     - insiders from the journal;
     - `stop_pct` computed as `/bought` does, from the price history.
     - Notify «📥 Вижу в Trading 212: GME — 10 шт. по 23,10 USD. Слежу: стоп, продажи инсайдеров, новости.»
5. **Held position, quantity or average price changed:** update `quantity` and `entry_price` (adding or trimming
   shares). No notification.
6. **Sold:** an open `origin='t212'` position missing from T212 is closed with `close_reason='продано в Trading
   212'`. Notify «📤 GME больше нет в Trading 212 — слежение закрыто.»
7. **First sync ever** (kv `t212_synced_once` unset): one combined message «📥 Слежу за вашими позициями в Trading
   212 (N): GME, INBX, …» instead of one message per holding. Then the flag is set.
8. **Notifications** go through `notify` (default `telegram_notify.send_text`) and are best-effort: a failed send
   doesn't undo the sync.
9. **A sync is atomic:** the DB changes are committed only when the whole fetch succeeded. A failed fetch changes
   nothing (no false "sold" on an API error).

**`SyncResult`** has `opened`, `updated`, `closed`, `error: str | None`, and `at` (the timestamp).

### 3. DB (`_ADDED_COLUMNS` and SCHEMA)

- `positions` gets: `origin TEXT DEFAULT 'manual'`, `quantity REAL`, `t212_ticker TEXT`, `currency TEXT`.
- New tables:
  - `t212_prices (ticker TEXT, date TEXT, price REAL, PRIMARY KEY (ticker, date))`
  - `t212_equity (date TEXT PRIMARY KEY, total_value REAL, invested_value REAL, invested_cost REAL,
    cash_free REAL, currency TEXT)`
- `positions.Position` gets the new fields (defaults keep old constructions working).

### 4. Prices for exits

`positions.daily_closes(ticker, source)` falls back to the `t212_prices` series when there's no Yahoo symbol and the
source is "T212". The function needs `conn` for that; add it, or a `t212_prices` reader passed via `closes_fn`.
Keep `check_exits`' seams working. The exit rules are unchanged: they apply to every open position, manual or
T212.

### 5. `/bought` and `/sold` with T212 positions

- `/bought X` where X is held in T212 replies «X уже отслеживается из Trading 212.»
- `/sold X` for a T212 position replies «X отслеживается из Trading 212: продайте там — бот увидит продажу сам.»
  The position is not closed.

### 6. `/portfolio` (extends the my-portfolio view)

**Live call.** `/portfolio` does one live `fetch_positions` and `fetch_summary` (no full sync, but it stores the
day's prices and equity like a sync does).

**«💼 Trading 212»** comes first:
- An account line: «Счёт: €X · вложено €Y · P/L ±€Z (±W%) · свободно €C».
- Per T212 position: «• GME — 10 шт., средняя 23,10, сейчас 24,05 USD (+4,1%), €+8,30», then the existing
  status lines from the my-portfolio view:
  - stop level and distance;
  - the insiders line;
  - «модель тоже держит».

**«✍️ Вне Trading 212»:** the manual positions, as the my-portfolio view shows them.

**If the live call fails,** show the stored T212 positions with «⚠️ Trading 212 не ответил (причина) — данные на
HH:MM последней синхронизации». A 403 or no key shows the reason and a one-line hint: «Создайте в Trading 212 →
Настройки → API ключ только для чтения (Portfolio, Account data) и положите в .env».

The footer and the empty text are as in the my-portfolio view.

### 7. The weekly message

`paper_report.format_week` gets one line before the portfolio line, when `t212_equity` has a value today or on/before
today: «Ваш счёт Trading 212: €X (за неделю ±Y%)». The week change is today vs the value on/before today-7, left out
when unknown.

### 8. The analyst

`analyst.py portfolio`'s «ВАШИ ПОЗИЦИИ» section includes the T212 holdings with quantity and P/L from the stored
data (no live call needed). It also includes the latest `t212_equity` line.

### 9. CLI: `python t212_account.py --check`

It prints «Trading 212: доступ есть, позиций N, валюта EUR» or the error reason, and never values that identify
the account or any secret. `python t212_account.py --sync` runs one sync with notifications off.

## Safety tests (mandatory)

- **Source inspection** of t212_account.py and trading212.py:
  - no `.post(`, `.put(`, `.patch(`, `.delete(` or `requests.request(`;
  - every URL string is one of the whitelisted read paths (instruments, positions, account/summary);
  - no path containing `/orders` other than history (which isn't used here: so none at all in t212_account.py).
- **The client** calls only `session.get` (a stub session records the calls).

## Tests (offline, stub sessions and fetchers)

- **Parsing:** the full and the minimal payload, missing fields, and a bad payload type.
- **Error mapping:** 401, 403, 429 (retry once, then error), timeout, no key.
- **Instrument mapping:** a US instrument, a class share, a non-US instrument by ISIN, and a Norwegian ISIN mapped to
  its Oslo ticker for insiders.
- **Sync:**
  - first sync (one combined message);
  - a new holding (message, position fields);
  - quantity/avg change (update, no message);
  - sold (closed, message);
  - API failure (nothing changes, no messages);
  - a manual position converted;
  - the day's price and equity rows replaced within a day;
  - no key (no-op).
- **Exits:** a T212 non-US position priced from `t212_prices` triggers the trailing stop. A US one uses Yahoo closes
  (stubbed).
- **`/bought` and `/sold`** replies for a T212-held ticker.
- **`/portfolio`:** live success (sections, the account line), live failure (the stored data with the warning), and
  403/no key (the hint).
- **The weekly line:** shown, the week change, and left out without data.
- **The analyst portfolio section** with T212 rows.
- **The telegram_bot loop:** the sync runs at start and then every 900 s (fake clock), and a sync exception doesn't
  stop polling.
- **bot.main:** the sync runs before check_exits on a full run, and not on a filtered run.

## README

A «Trading 212» section covers:
- what is read (positions and account summary, read-only), how often (every 15 minutes plus the daily run), and the
  notifications;
- that `/portfolio` is live;
- the read-only key recommendation (create a key without order permissions);
- `--check`;
- that the bot never places orders.
