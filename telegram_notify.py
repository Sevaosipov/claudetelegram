"""Send a digest of new purchases to Telegram via the Bot HTTP API.

Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars. To get them:
  1. Message @BotFather on Telegram, send /newbot, follow the prompts -> gives you
     a bot token like "123456:ABC-DEF...".
  2. Send any message to your new bot, then open in a browser:
     https://api.telegram.org/bot<TOKEN>/getUpdates
     Your numeric chat id is in the JSON response under result[0].message.chat.id.

If either env var is missing, send_digest() prints a warning and does nothing --
it never raises, so a misconfigured bot doesn't break the rest of the pipeline.
"""
from __future__ import annotations

import datetime as dt
import os
import re

import requests

import crypto
import datefmt

API_URL = "https://api.telegram.org/bot{token}/sendMessage"
MAX_LEN = 3500  # stay under Telegram's 4096-char limit with room to spare
_TOKEN_IN_URL = re.compile(r"bot\d+:[A-Za-z0-9_-]+")


def _redact(text: str, token: str | None) -> str:
    """`text` without the bot token. requests puts the whole URL, token included, into its error
    messages, and what send_text prints lands in logs and in a headless Claude's transcript."""
    if token:
        text = text.replace(token, "<token>")
    return _TOKEN_IN_URL.sub("bot<token>", text)


def _esc(text) -> str:
    """Escape the 3 characters Telegram's HTML parse_mode treats specially.
    Applied to any text that isn't a literal written in this file -- names,
    company names, tickers -- anything that ultimately comes from a filing."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _b(text: str, html: bool) -> str:
    """Bold a line for Telegram, escaping it in the same step -- the two must
    happen together so a stray '&'/'<'/'>' inside filer-supplied text (a
    ticker, a company name) can never land inside the tag unescaped."""
    return f"<b>{_esc(text)}</b>" if html else text


# ------------------------------------------------------------------ the signal line
# Every automatic signal is one short message in the same shape (spec 2026-10-04-signal-message-style.md):
# «🔴 <b>GME!</b>: сработал стоп — пора продавать: вход 23,10 → сейчас 20,70, итог <b>−$24,00</b> (−10,4%)».
DOT_GREEN, DOT_RED, DOT_NEUTRAL = "🟢", "🔴", "⚪"   # a buy or a gain / a loss or an unknown result / a neutral event


def signal_line(dot: str, name: str, event: str, details: str | None = None, *,
                result: str | None = None, extra: str | None = None, html: bool = True,
                label: str = "итог") -> str:
    """`{dot} <b>{name}!</b>: {event} — {details}`, and for a close `, итог <b>{result}</b>{extra}`.

    `extra` follows the bold result as it is (a leading space and its brackets are the caller's:
    « (−10,4%)»). `label` is what the result is called -- «итог», «итог ≈» (a result on the last
    price known), «сейчас» (a sale still waiting). A part that is None or empty is left out, and
    `extra` goes with the result alone. Every part is escaped; with html=False there are no tags
    and nothing is escaped (the log, the menu)."""
    text = f"{_e(dot, html)} {_b(f'{name}!', html)}: {_e(event, html)}"
    if details:
        text += f" — {_e(details, html)}"
    if result:
        text += f", {_e(label, html)} {_b(result, html)}{_e(extra or '', html)}"
    return text


def _chunk(text: str, size: int) -> list[str]:
    """Split a digest for Telegram's message limit, preferring signal boundaries.

    Signals are joined by a blank line, so packing whole blocks keeps a signal's
    header, members and links together instead of tearing one across two messages
    at whatever line happens to cross the limit. A single block bigger than the
    limit (a very large cluster) still falls back to splitting by line.
    """
    blocks = text.split("\n\n")
    chunks: list[str] = []
    current = ""
    for block in blocks:
        if len(block) + 2 > size:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_chunk_lines(block, size))
            continue
        if current and len(current) + len(block) + 2 > size:
            chunks.append(current)
            current = ""
        current = f"{current}\n\n{block}" if current else block
    if current:
        chunks.append(current)
    return chunks


def _chunk_lines(text: str, size: int) -> list[str]:
    lines = text.split("\n")
    chunks, current = [], ""
    for line in lines:
        if len(current) + len(line) + 1 > size:
            chunks.append(current)
            current = ""
        current += line + "\n"
    if current:
        chunks.append(current)
    return chunks


def send_text(text: str) -> bool:
    """Send `text` (in chunks of MAX_LEN); True only when every chunk went through."""
    accepted, total = send_text_parts(text)
    return bool(total) and accepted == total


def send_text_parts(text: str) -> tuple[int, int]:
    """send_text, telling how it went: (chunks Telegram accepted, chunks sent). (0, 0) without
    the credentials. A caller can tell "nothing went out" from "part of it went out"."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[telegram] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set, skipping notification")
        return 0, 0
    chunks = _chunk(text, MAX_LEN)
    accepted = 0
    for chunk in chunks:
        try:
            resp = requests.post(
                API_URL.format(token=token),
                data={
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=15,
            )
            resp.raise_for_status()
            accepted += 1
        except requests.RequestException as e:
            print(f"[telegram] send failed: {_redact(str(e), token)}")
    return accepted, len(chunks)


def format_sec_line(p, avg_return_pct: float | None) -> str:
    role = p.officer_title or ("Director" if p.is_director else "10%+ Owner")
    value = f"${p.value:,.0f}" if p.value else "?"
    perf = f", track record {avg_return_pct:+.0f}% vs S&P" if avg_return_pct is not None else ""
    return (
        f"🟢 SEC: {p.owner_name} ({role}) bought {p.issuer_name} ({p.ticker or '?'})\n"
        f"   {datefmt.fmt(p.transaction_date)} · {p.shares:g} sh @ ${p.price:,.2f} = {value}{perf}\n"
        f"   {p.source_url}"
    )


def format_house_line(t) -> str:
    return (
        f"🏛 House: {t.member_name} [{t.state_district}] bought {t.asset}\n"
        f"   {datefmt.fmt(t.txn_date)} · {t.amount_range}\n"
        f"   {t.source_url}"
    )


def format_bafin_line(f, detail, source_url: str) -> str:
    verb = "bought" if f.txn_type == "P" else "sold"
    vol = f"€{detail.volume_eur:,.0f}" if detail.volume_eur else "?"
    price = f" @ €{detail.price_eur:,.2f}" if detail.price_eur else ""
    role = f" ({f.position})" if f.position else ""
    return (
        f"🇩🇪 BaFin: {f.notifier_name}{role} {verb} {f.issuer_name} ({f.isin})\n"
        f"   {datefmt.fmt(f.txn_date)} · {vol}{price}\n"
        f"   {source_url}"
    )


def format_norway_line(t: dict) -> str:
    verb = "bought" if t["txn_type"] == "P" else "sold"
    value = t["shares"] * t["price"]
    vol = f"{value:,.0f} {t['currency']}" if value else "?"
    price = f" @ {t['price']:,.2f} {t['currency']}/sh" if t.get("price") else ""
    return (
        f"🇳🇴 Oslo Børs: {t['person']} {verb} {t['issuer_name']} ({t['ticker'] or '?'})\n"
        f"   {datefmt.fmt(t['txn_date'])} · {t['shares']:g} sh{price} = {vol}\n"
        f"   {t['source_url']}"
    )


def format_senate_line(t) -> str:
    return (
        f"🏛 Senate: {t.member_name} [{t.office}] "
        f"{'bought' if t.txn_type == 'P' else 'sold'} {t.asset}\n"
        f"   {datefmt.fmt(t.txn_date)} · {t.amount_range}\n"
        f"   {t.source_url}"
    )


def format_stake_line(f) -> str:
    """One reporting person from a 13D/G filing, for the terminal feed."""
    pct = f"{f.percent_of_class:.2f}%" if f.percent_of_class is not None else "?"
    shares = f", {f.amount_owned:,.0f} sh" if f.amount_owned else ""
    when = f" · {datefmt.fmt(f.event_date)}" if f.event_date else ""
    icon = "🐋" if f.is_activist else "📊"
    return (
        f"{icon} {f.form_type}: {f.person_name} holds {pct} of "
        f"{f.issuer_name} ({f.ticker or f.issuer_cik}){shares}{when}\n"
        f"   {f.source_url}"
    )


def format_144_line(s) -> str:
    """One Form 144 notice of intended sale, for the terminal feed."""
    value = f"${s.market_value:,.0f}" if s.market_value else "?"
    pct = f" ({s.percent_of_class:.3f}% of class)" if s.percent_of_class is not None else ""
    nature = f" · {s.acquisition_nature}" if s.acquisition_nature else ""
    return (
        f"📤 Form 144: {s.person_name} ({s.relationship or '?'}) intends to sell "
        f"{value} of {s.issuer_name} ({s.ticker or s.issuer_cik}){pct}\n"
        f"   ~{datefmt.fmt(s.approx_sale_date)}{nature}\n"
        f"   {s.source_url}"
    )


def format_sweden_line(t: dict) -> str:
    verb = "bought" if t["txn_type"] == "P" else "sold"
    vol = f"{t['value']:,.0f} {t['currency']}" if t["value"] else "?"
    price = f" @ {t['price']:,.2f} {t['currency']}/sh" if t.get("price") else ""
    role = f" ({t['position']})" if t.get("position") else ""
    # Worth saying on the line: this is compensation being disclosed, not a
    # decision to buy. No other source in this project distinguishes the two.
    plan = " [share programme]" if t.get("share_program") else ""
    return (
        f"🇸🇪 FI: {t['person']}{role} {verb} {t['issuer_name']} ({t['isin']}){plan}\n"
        f"   {datefmt.fmt(t['txn_date'])} · {t['shares']:g} sh{price} = {vol}\n"
        f"   {t['source_url']}"
    )


def format_treasury_line(t) -> str:
    """t is a crypto_treasury.TreasuryTxn."""
    verb = "bought" if t.side == "P" else "sold"
    who = f"{t.company} ({t.ticker})" if t.ticker else t.company
    value = f" = ${t.value_usd:,.0f}" if t.value_usd else ""
    price = f" @ ${t.avg_price_usd:,.0f}" if t.avg_price_usd else ""
    return (f"🪙 Treasury: {who} {verb} {t.units:,.4g} {t.coin}{price}{value}\n"
            f"   {t.form} filed {datefmt.fmt(t.filed_date)}\n"
            f"   {t.source_url}")


def format_etf_flow_line(fund: str, coin: str, prev_as_of: str, as_of: str, flow_usd: float) -> str:
    sign = "+" if flow_usd >= 0 else "−"
    return (f"🏦 ETF {fund} ({coin}): {sign}${abs(flow_usd) / 1e6:,.1f}m net flow "
            f"{datefmt.fmt(prev_as_of)} → {datefmt.fmt(as_of)}")


def format_condensed(rep: dict) -> str:
    """Telegram-safe rendering of a research.build() report for the inbound
    ticker-analysis reply in telegram_bot.py: the opinion section (when present)
    plus a few key supporting facts, not the entire dossier -- research.py's
    CLI is the place for the full deep-dive. opinion.format_opinion()'s own
    text is entirely bot-authored fixed strings and numbers, no filer-supplied
    free text, so it needs no escaping here. send_text() always sends with
    parse_mode HTML, so every field that ultimately comes from a filing
    (owner/person/member names, form types) goes through _esc() below -- the
    same rule the disclosure-signal formatters above follow, and the reason
    format_sec_line & co. (print-only, never sent to Telegram) don't need to.
    """
    if rep.get("kind") == "crypto":
        import crypto_research
        return crypto_research.format_condensed(rep)

    import opinion
    import research
    import tradingview
    t = rep["ticker"]
    L = [_b(t, True)]

    if rep.get("opinion"):
        L.append(opinion.format_opinion(rep["opinion"]))

    entry_target = research.format_entry_target(rep)
    if entry_target:
        L.append(entry_target)

    if rep.get("outlook"):
        import outlook
        L.append(_esc(outlook.format_outlook(rep["outlook"], t)))

    buys, sells = rep["insiders"]["buys"], rep["insiders"]["sells"]
    if buys or sells:
        L.append("\n" + _b("Инсайдеры (SEC Form 4)", True))
        for (date, owner, title, is_dir, is_off, is_ten, value, deriv, plan, url) in buys[:3]:
            role = title or ("Director" if is_dir else "Officer" if is_off
                              else "10%+ Owner" if is_ten else "Insider")
            L.append(f"🟢 {datefmt.fmt(date)} {_esc(owner)} ({_esc(role)}) ${value or 0:,.0f}")
        for (date, owner, title, value, url) in sells[:2]:
            L.append(f"🔴 {datefmt.fmt(date)} {_esc(owner)} ({_esc(title or 'Insider')}) "
                     f"${value or 0:,.0f}")
    else:
        L.append("\nИнсайдеров (SEC Form 4) в базе нет.")

    if rep["stakes"]:
        L.append("\n" + _b("Крупные доли (13D/G)", True))
        for (date, person, form, pct, amount, url) in rep["stakes"][:3]:
            L.append(f"{datefmt.fmt(date) if date else '?'} {_esc(person)} "
                     f"{pct:.2f}% ({_esc(form)})")

    if rep["political"]:
        L.append("\n" + _b("Политики (STOCK Act)", True))
        for (date, member, ttype, amount, url, chamber) in rep["political"][:3]:
            icon = "🟢" if ttype == "P" else "🔴"
            L.append(f"{icon} {datefmt.fmt(date)} {_esc(member)} [{chamber}] {_esc(amount)}")

    if rep.get("tradingview"):
        # tradingview.format_view()'s own text is all fixed internal labels, no
        # filer-supplied free text -- safe to embed unescaped.
        L.append(tradingview.format_view(rep["tradingview"]))

    notes = research._source_notes(rep)
    if notes:
        L.append("\nИсточники: " + _esc(" · ".join(notes)))

    # The resolved key, quoted: a bare "BTC" would open the coin's report, not the
    # $BTC stock's, and an unquoted "$BTC" would be expanded away by the shell.
    key = rep["asset"].key if "asset" in rep else t
    L.append(f"\nПолный отчёт: python research.py '{_esc(key)}'")
    return "\n".join(L)


_HORIZON_LABELS = {1: "1 день", 5: "1 неделя", 21: "1 месяц", 63: "1 квартал"}


def format_ticker_backtest(result: dict) -> str:
    """Telegram-safe rendering of backtest.backtest_ticker()'s output for the
    /backtest command in telegram_bot.py. All bot-authored numbers/labels, no
    filer-supplied free text -- no escaping needed, same reasoning as
    format_carry_signal above."""
    t, n = result["ticker"], result["n_purchases"]
    L = [f"📉 {_b(t, True)} — бэктест по собственным инсайдерским покупкам (окно 10 лет)"]
    L.append(f"Покупок в базе: {n}")
    if not result["by_horizon"]:
        L.append("Недостаточно ценовой истории для расчёта.")
        return "\n".join(L)
    for h in sorted(result["by_horizon"]):
        stats = result["by_horizon"][h]
        label = _HORIZON_LABELS.get(h, f"{h} дн.")
        p = f", p={stats['p']:.2f}" if stats["p"] is not None else ""
        L.append(f"• {label}: медиана {stats['median_return']:+.1f}% "
                 f"({stats['median_excess']:+.1f}pp к SPY), hit-rate {stats['hit_rate']:.0f}%{p}")
    return "\n".join(L)


_CARRY_DOT = {"LONG": DOT_GREEN, "SHORT": DOT_RED, "FLAT": DOT_NEUTRAL}
_CARRY_SIDE = {"LONG": "лонг", "SHORT": "шорт"}
# carry_strategy.step's reasons, in words
_CARRY_REASON = {"entry": "новый сигнал", "trailing stop": "сработал стоп", "signal off": "сигнал снят"}


def _rate(x: float) -> str:
    """An EURUSD rate to four places, a comma decimal: «1,1327»."""
    return f"{x:.4f}".replace(".", ",")


def _spread(x: float) -> str:
    """A yield spread in percentage points, signed: «+0,35 п.п.», «−1,68 п.п.»."""
    return f"{round(x, 2) or 0.0:+.2f}".replace(".", ",").replace("-", "−") + " п.п."


def format_carry_signal(from_state: str, to_state: str, s: dict, *, reason: str,
                         level: float | None) -> str:
    """An EURUSD carry-gated-strategy state change, from carry_strategy.py, as one signal line:

        🟢 EURUSD!: вход в лонг (новый сигнал) — цена 1,2000, 200-дн. средняя 1,1500, спред DE-US 2 г.
                    +0,35 п.п., стоп ~1,1520 (6×ATR)
        🔴 EURUSD!: вход в шорт (новый сигнал) — ...
        ⚪ EURUSD!: выход во флэт (сработал стоп) — цена 1,1600, выход ~1,1670

    A price/rate-driven trading rule rather than a disclosed transaction, so it states facts only --
    today's price, the average, the rate spread (DE 2y less US 2y), the stop level and where the
    exit is -- with no verdict and no advice; execution is the user's. `reason` is carry_strategy's
    own («entry», «trailing stop», «signal off»), put into words; one nobody translated is shown as
    it is. What is not known (the average, the spread, the level) is left out. `from_state` is
    only for the caller's own reading: the line is about where the strategy is now."""
    why = _CARRY_REASON.get(reason, reason)
    if to_state == "FLAT":
        details = f"цена {_rate(s['close'])}" + (f", выход ~{_rate(level)}" if level is not None else "")
        return signal_line(DOT_NEUTRAL, "EURUSD", f"выход во флэт ({why})", details)
    facts = [f"цена {_rate(s['close'])}"]
    if s.get("ma") is not None:
        facts.append(f"200-дн. средняя {_rate(s['ma'])}")
    if s.get("diff") is not None:
        facts.append(f"спред DE-US 2 г. {_spread(s['diff'])}")
    if level is not None:
        facts.append(f"стоп ~{_rate(level)} (6×ATR)")
    return signal_line(_CARRY_DOT[to_state], "EURUSD", f"вход в {_CARRY_SIDE[to_state]} ({why})",
                       ", ".join(facts))


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Russian noun pluralization: 1 -> one, 2-4 -> few, 0/5-20 -> many (with the
    usual 11-14 exception)."""
    if 11 <= n % 100 <= 14:
        return many
    if n % 10 == 1:
        return one
    if 2 <= n % 10 <= 4:
        return few
    return many


_SOURCE_ICON = {"SEC": "🟢", "HOUSE": "🏛", "SENATE": "🏛", "BAFIN": "🇩🇪",
                "NORWAY": "🇳🇴", "SWEDEN": "🇸🇪"}

# Signal amounts arrive already converted to EUR by cluster.py (see fx.py), so
# every source formats the same way here. The per-filing lines above stay in
# their native currency on purpose -- those mirror the linked source document.
SIGNAL_CURRENCY = "€"


def format_signal(sig, *, html: bool = False) -> str:
    icon = _SOURCE_ICON.get(sig.source, "🟢")
    if sig.source == "HOUSE":
        label = _plural(sig.buyer_count, "конгрессмен", "конгрессмена", "конгрессменов")
    elif sig.source == "SENATE":
        label = _plural(sig.buyer_count, "сенатор", "сенатора", "сенаторов")
    else:
        label = _plural(sig.buyer_count, "инсайдер", "инсайдера", "инсайдеров")
    # House PTRs only disclose a bracket, never an exact figure, so the total
    # there is a conservative lower-bound estimate, not an exact sum.
    if sig.total_value:
        # Both chambers disclose a bracket rather than an amount, so their totals
        # are lower bounds, not sums.
        prefix = "от " if sig.source in ("HOUSE", "SENATE") else ""
        value = f", {prefix}{SIGNAL_CURRENCY}{sig.total_value:,.0f}"
    else:
        value = ""
    reason = "КРУПНАЯ ПОКУПКА" if getattr(sig, "reason", "cluster") == "solo" else "СИГНАЛ"
    # Say so when nobody in the cluster is an officer or director. An institution
    # adding to a >10% stake and the people who run the company buying are different
    # events, and the euro amounts alone make them look identical -- the largest
    # signal in the database so far is a fund, not an insider.
    tag = " · только держатели >10%" if getattr(sig, "holder_only", False) else ""
    score = getattr(sig, "score", None)
    rank = f" · {score:.0f} баллов" if score else ""
    headline = f"{reason}: {sig.ticker} — {sig.buyer_count} {label}{value}{tag}{rank}"
    lines = [f"{icon} {_b(headline, html)}"]
    lines.append(f"   {_esc(sig.company) if html else sig.company}")
    lines.append(f"   {datefmt.fmt(sig.window_start)} – {datefmt.fmt(sig.window_end)}")
    context = _context_line(sig)
    if context:
        lines.append(f"   {_esc(context) if html else context}")
    for m in sig.members:
        lines.append(f"   • {_esc(m) if html else m}")
    corr = getattr(sig, "corroborated_by", None)
    if corr:
        text = f"Другие источники по этому тикеру: {', '.join(corr)}"
        lines.append(f"   🔗 {_esc(text) if html else text}")
    return "\n".join(lines)


def _context_line(sig) -> str:
    """Company size, how big the purchase is against it, how liquid the name is, and
    how stale the disclosure is.

    None of this was ever shown. The euro amount alone made a purchase in a shell
    company and one in a mega-cap read identically, and a two-day-old Form 4 look
    the same as a six-week-old PTR.
    """
    import marketcap
    parts = []
    cap = getattr(sig, "market_cap_eur", None)
    if cap:
        parts.append(f"компания €{_short_money(cap)} ({marketcap.size_bucket(cap)})")
    pct = getattr(sig, "value_pct_of_mcap", None)
    if pct and pct <= 100:
        parts.append(f"{pct:.2f}% капитализации")
    pos = getattr(sig, "position_increase_pct", None)
    if pos:
        parts.append(f"+{pos:.0f}% к своей позиции")
    if getattr(sig, "first_buy", False):
        parts.append("первая покупка в этой бумаге")
    adv = getattr(sig, "avg_daily_value", None)
    if adv is not None:
        note = " ⚠️ низкая ликвидность" if adv < 250_000 else ""
        parts.append(f"оборот €{_short_money(adv)}/день{note}")
    lag = getattr(sig, "lag_days", None)
    if lag is not None:
        parts.append(f"раскрыто через {lag:.0f} дн.")
    return " · ".join(parts)


def _short_money(value: float) -> str:
    for unit, suffix in ((1e9, "млрд"), (1e6, "млн"), (1e3, "тыс")):
        if abs(value) >= unit:
            return f"{value / unit:,.1f} {suffix}"
    return f"{value:,.0f}"


def format_exit_signal(sig, *, html: bool = False) -> str:
    label = {"HOUSE": "конгрессменов", "SENATE": "сенаторов"}.get(sig.source, "инсайдеров")
    icon = _SOURCE_ICON.get(sig.source, "🚨")
    headline = (f"ВЫХОД: {sig.ticker} — {sig.seller_count} из {sig.total_buyers} {label}, "
                f"кто покупал, теперь продали")
    lines = [f"{icon} {_b(headline, html)}"]
    lines.append(f"   {_esc(sig.company) if html else sig.company}")
    for l in sig.lines:
        lines.append(f"   • {_esc(l) if html else l}")
    corr = getattr(sig, "corroborated_by", None)
    if corr:
        text = f"Другие источники по этому тикеру: {', '.join(corr)}"
        lines.append(f"   🔗 {_esc(text) if html else text}")
    return "\n".join(lines)


def format_stake_signal(sig, *, html: bool = False) -> str:
    """Schedule 13D/G: one holder's share OF THE COMPANY, not an amount of money.
    13D and 13G carry the same 5% trigger but opposite intent -- 13D means the
    holder may seek to influence control -- so they're labelled differently."""
    kind = "🐋 АКТИВИСТ" if sig.is_activist else "📊 КРУПНЫЙ ДЕРЖАТЕЛЬ"
    delta = f" (было {sig.prev_percent:.2f}%)" if sig.prev_percent is not None else ""
    co_filers = getattr(sig, "co_filer_names", None) or []
    # Same underlying filing, other required reporting persons (GP/LP/individual
    # managers) -- shown as a count, not every name, so the headline stays one line.
    co_filer_tag = (f" (+{len(co_filers)} "
                     f"{_plural(len(co_filers), 'содокладчик', 'содокладчика', 'содокладчиков')})"
                     if co_filers else "")
    headline = f"{kind}: {sig.ticker} — {sig.person}{co_filer_tag} {sig.percent:.2f}% компании{delta}"
    lines = [_b(headline, html)]
    lines.append(f"   {_esc(sig.company) if html else sig.company}")
    detail = sig.form_type
    if sig.amount_owned:
        detail = f"{sig.amount_owned:,.0f} акций · {detail}"
    if sig.event_date:
        detail += f" · событие {datefmt.fmt(sig.event_date)}"
    lines.append(f"   {_esc(detail) if html else detail}")
    corr = getattr(sig, "corroborated_by", None)
    if corr:
        text = f"Другие источники по этому тикеру: {', '.join(corr)}"
        lines.append(f"   🔗 {_esc(text) if html else text}")
    if html:
        lines.append(f'   <a href="{_esc(sig.url)}">источник</a>')
    else:
        lines.append(f"   {sig.url}")
    return "\n".join(lines)


_CRYPTO_HEADINGS = {
    # (crypto_kind, bullish) -> heading
    ("treasury", True): "🪙 ПОКУПКИ КОМПАНИЙ ЗА НЕДЕЛЮ",
    ("treasury", False): "🪙 КОМПАНИЯ ПРОДАЛА",
    ("etf_flow", True): "🏦 ПРИТОК В СПОТ-ETF",
    ("etf_flow", False): "🏦 ОТТОК ИЗ СПОТ-ETF",
    ("exchange_flow", True): "⛓ ВЫВОД С БИРЖ",
    ("exchange_flow", False): "⛓ ЗАВОД НА БИРЖИ",
}


def format_crypto_signal(sig, *, html: bool = False) -> str:
    """Treasury purchase, spot-ETF flow, or exchange-wallet flow -- see cluster/crypto.py."""
    heading = _CRYPTO_HEADINGS[(sig.crypto_kind, sig.bullish)]
    size = []
    if sig.units:
        size.append(f"{sig.units:,.0f} {sig.coin}")
    if sig.total_value:
        size.append(f"€{sig.total_value:,.0f}")
    score = getattr(sig, "score", None)
    tail = f" · {score:.0f} баллов" if score is not None else ""
    headline = f"{heading}: {sig.ticker} — {' · '.join(size) or 'сумма неизвестна'}{tail}"
    lines = [_b(headline, html), f"   {_esc(sig.company) if html else sig.company}"]
    when = datefmt.fmt(sig.window_end[:10])
    if sig.window_start[:10] != sig.window_end[:10]:
        when = f"{datefmt.fmt(sig.window_start[:10])} – {when}"
    lines.append(f"   {when}")
    for d in sig.details:
        lines.append(f"   • {_esc(d) if html else d}")
    if sig.crypto_kind == "exchange_flow":
        caveat = "не раскрытие: балансы помеченных кошельков бирж, возможны внутренние переводы"
        lines.append(f"   ⚠️ {_esc(caveat) if html else caveat}")
    corr = getattr(sig, "corroborated_by", None)
    if corr:
        text = f"Другие источники по этой монете: {', '.join(corr)}"
        lines.append(f"   🔗 {_esc(text) if html else text}")
    if sig.url:
        lines.append(f'   <a href="{_esc(sig.url)}">источник</a>' if html else f"   {sig.url}")
    return "\n".join(lines)


def format_any_signal(s, *, html: bool = False) -> str:
    if hasattr(s, "crypto_kind"):
        return format_crypto_signal(s, html=html)
    if hasattr(s, "seller_count"):
        return format_exit_signal(s, html=html)
    if hasattr(s, "percent"):
        return format_stake_signal(s, html=html)
    return format_signal(s, html=html)


def format_signals_digest(signals: list) -> str:
    exit_count = sum(1 for s in signals if hasattr(s, "seller_count"))
    stake_count = sum(1 for s in signals if hasattr(s, "percent"))
    crypto_count = sum(1 for s in signals if hasattr(s, "crypto_kind"))
    buy_count = len(signals) - exit_count - stake_count - crypto_count
    parts = [f"{buy_count} кластерных покупок", f"{exit_count} выходов"]
    if stake_count:
        parts.append(f"{stake_count} крупных долей")
    if crypto_count:
        parts.append(f"{crypto_count} крипто")
    header = "🎯 <b>Новые сигналы: " + ", ".join(parts) + "</b>"
    return "\n\n".join([header] + [format_any_signal(s, html=True) for s in signals])


def format_digest(sec_lines: list[str], house_lines: list[str]) -> str:
    parts = [f"📈 Disclosure bot — {len(sec_lines)} insider buy(s), {len(house_lines)} Congress buy(s)\n"]
    parts.extend(sec_lines)
    if sec_lines and house_lines:
        parts.append("")
    parts.extend(house_lines)
    return "\n\n".join(parts) if len(parts) > 1 else parts[0]


# ------------------------------------------- the signals on the user's own positions (positions.py)
def money_cents(x: float, currency: str | None = "EUR") -> str:
    """A profit or loss to the cent, its sign before the currency: «+€8,30», «−$26,40», «+8,30 CHF»
    for a currency with no sign of its own. No currency told is the euro, as money() has it."""
    cents = round(abs(x), 2)
    sign = "−" if x < 0 and cents else "+"
    mark = _CURRENCY_SIGN.get(currency or "EUR")
    return f"{sign}{mark}{_price(cents)}" if mark else f"{sign}{_price(cents)} {currency}"


def position_result(entry: float, last: float, qty: float | None = None,
                    currency: str | None = "EUR") -> tuple[str, str | None, bool]:
    """How a real position stands, as signal_line shows it: (result, extra, gain).

    With a quantity (a Trading 212 holding) the result is the money, in the position's own
    currency -- no euro value is known here -- and `extra` the percent in brackets: «−$24,00»,
    « (−10,4%)». Without one it is the percent alone: «−10,4%», None. `gain` is whether the
    result, as shown, is zero or more (a green dot): it follows the rounded figure, so «+0,0%» is
    never a loss."""
    pct = last / entry - 1
    if qty:
        money = (last - entry) * qty
        return money_cents(money, currency), f" ({signed_pct(pct)})", round(money, 2) >= 0
    return signed_pct(pct), None, round(pct * 100, 1) >= 0


# What a close alert says happened, by its trigger (positions.py). A model sale (paper_report) words
# its own reasons with the same phrases.
CLOSE_EVENT = {"trailing_stop": "сработал стоп", "insider_sell": "продаёт инсайдер",
               "caution": "отток по монете", "trend_down": "тренд развернулся вниз",
               "dead_money": "стоит на месте", "time": "год в позиции", "news": "плохие новости",
               "activist_cut": "активист сократил долю"}


def _position_name(pos) -> str:
    """What the menu's list calls a position: its ticker -- but a Trading 212
    holding keyed by its ISIN by its Trading 212 symbol (SAP, not DE0007164600), the name its
    owner knows."""
    import positions        # light, and positions never imports this module
    if pos.source == positions.T212_SOURCE:
        return positions.name_of(pos.ticker, pos.source, pos.t212_ticker)
    return pos.ticker


def _signal_name(pos) -> str:
    """What a signal calls a position of the user's: a coin by its symbol (BTC), a Trading 212
    holding by the name its owner knows (positions.name_of), anything else by its ticker."""
    import positions
    return positions.display_name(pos)


# ---- the detail line of a sell alert
# positions.py words an alert's detail for the log and the menu -- an ISO date, an English decimal point,
# a comma between thousands. The message shows it the Russian way; the alert itself is not changed.
_NUMBER = re.compile(r"(?<![\w.,])([-+−])?(\d{1,3}(?:,\d{3})+(?!\d)|\d+)(?:\.(\d+))?(?!\.?\d)")
_ISO_DATE = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")


def _ru_number(m) -> str:
    sign, whole, fraction = m.groups()
    text = whole.replace(",", " ") + (f",{fraction}" if fraction else "")
    return ("−" if sign == "-" else sign or "") + text


def _short_date(m) -> str:
    try:
        return f"{dt.date.fromisoformat(m.group()):%d.%m}"
    except ValueError:                  # 2026-13-45 is no date: left as it is
        return m.group()


def ru_text(text: str) -> str:
    """`text` as the Russian message has its numbers and dates: a comma decimal, a space between
    thousands, the real minus («-6.2%» is «−6,2%», «€1,050 млн» is «€1 050 млн»), and an ISO date
    as DD.MM («2026-10-01» is «01.10»). A comma is a thousands separator only before exactly three
    digits (a number already written the Russian way, «9,0%», stays); a date that is not an ISO
    date is left alone. Numbers go first, so that the DD.MM it writes is not read as a decimal."""
    return _ISO_DATE.sub(_short_date, _NUMBER.sub(_ru_number, text))


# The triggers whose alert has a second line: who sold and when, what the outflow was, which headline.
# For every other trigger the main line says it all.
_DETAIL_TRIGGERS = ("insider_sell", "caution", "news")


def alert_detail(alert) -> str | None:
    """The second line of a sell alert, or None. Only an insider's sale, a coin's caution and bad
    news have one -- the alert's own detail (positions.CloseAlert.detail), with its dates and numbers in
    the Russian format (ru_text); a news headline is somebody else's text and is quoted as it is."""
    if alert.trigger not in _DETAIL_TRIGGERS or not alert.detail:
        return None
    return alert.detail if alert.trigger == "news" else ru_text(alert.detail)


def format_close_alert(alert, *, html: bool = True) -> str:
    """The sell alert on a position of the user's (/bought or the Trading 212 account), one message:

        🔴 <b>GME!</b>: сработал стоп — пора продавать: вход 23,10 → сейчас 20,70, итог <b>−$24,00</b> (−10,4%)
           −10% от максимума 25.80

    The dot is green when the result is zero or more and red when it is a loss or there is no
    price to tell it by (then «пора продавать: вход X» stands alone). A position with a quantity
    gets its money result first (position_result), any other the percent. A second line, plain,
    only for an insider's sale (who and when), a coin's caution (the outflow) and bad news (the
    headline): see alert_detail. Every other alert is the one line."""
    pos = alert.position
    event = CLOSE_EVENT.get(alert.trigger, alert.trigger)
    details = f"пора продавать: вход {_price(pos.entry_price)}"
    shown = None
    if alert.last_price and pos.entry_price:
        details += f" → сейчас {_price(alert.last_price)}"
        shown = position_result(pos.entry_price, alert.last_price, pos.quantity, pos.currency)
    dot = DOT_GREEN if shown and shown[2] else DOT_RED
    line = signal_line(dot, _signal_name(pos), event, details, html=html,
                       result=shown[0] if shown else None, extra=shown[1] if shown else None)
    detail = alert_detail(alert)
    return f"{line}\n   {_e(detail, html)}" if detail else line


def format_positions(positions: list, price_fn) -> str:
    if not positions:
        return "Открытых позиций нет. /bought TICKER [цена] — добавить."
    today = dt.date.today()
    lines = ["Открытые позиции:"]
    for p in positions:
        price = price_fn(p.ticker, p.source)
        change = f"{(price / p.entry_price - 1) * 100:+.1f}%" if price else "цена недоступна"
        days = (today - dt.date.fromisoformat(p.opened_at)).days
        lines.append(f"• {_position_name(p)}: вход {p.entry_price:,.2f}, сейчас "
                     f"{f'{price:,.2f}' if price else '—'} ({change}), {days} дн.")
    return "\n".join(lines)


# ------------------------------------------------------------------ the model portfolio
_MAX_OTHER_STOCKS = 15


_CURRENCY_SIGN = {"EUR": "€", "USD": "$", "GBP": "£"}


def money(x: float, currency: str | None = "EUR") -> str:
    """Whole units of `currency`, a space between thousands, a real minus sign: «€9 800», «$120»,
    and «9 800 CHF» for a currency with no sign of its own. No currency told is the euro."""
    n = round(abs(x))
    amount = f"{n:,}".replace(",", " ")
    minus = "−" if x < 0 and n else ""
    sign = _CURRENCY_SIGN.get(currency or "EUR")
    return f"{minus}{sign}{amount}" if sign else f"{minus}{amount} {currency}"


def money_eur(x: float) -> str:
    """«€9 800»: a space between thousands, a real minus sign."""
    return money(x, "EUR")


def signed_pct(x: float | None) -> str:
    """A fraction as «+1,2%» (comma decimal, real minus sign); «—» for None."""
    if x is None:
        return "—"
    v = round(x * 100, 1) or 0.0            # a -0.0 would read «−0,0%»
    return f"{v:+.1f}%".replace(".", ",").replace("-", "−")


def share_pct(x: float) -> str:
    """A fraction as «9,8%»."""
    return f"{x * 100:.1f}%".replace(".", ",")


def _e(text, html: bool) -> str:
    return _esc(text) if html else str(text)


_DECISION_ICON = {"buy": "🟢", "watch": "👀", "block": "⛔", "skip": "·"}


def _pts(x: float) -> str:
    return str(round(x)).replace("-", "−")


def _score_line(s, html: bool) -> str:
    icon = _DECISION_ICON.get(s.decision, "·")
    if s.kind == "crypto":
        parts = f"тренд {_pts(s.trend)} · потоки {_pts(s.flows)} · новости {_pts(s.news)}"
        why = s.block
    else:
        parts = (f"инсайдеры {_pts(s.insiders)} · поводы {_pts(s.triggers)} · "
                 f"импульс {_pts(s.momentum)} · новости {_pts(s.news)}")
        why = s.block or s.untradeable
    line = f"{icon} {_e(s.ticker, html)} {_pts(s.total)}: {parts}"
    if why:
        line += f" — {_e(why, html)}"
    if getattr(s, "t212", None) is False:
        line += " · нет на T212"
    return line


def format_scored(scored: list, *, html: bool = False) -> str:
    """The model's scores, one line each (the menu's «Сигналы»): the buys, the watched and the
    blocked first, the stocks that scored below the watch line after «Прочие». The bars
    named are model_score's own."""
    import model_score      # light (pure functions); imported here like model elsewhere in this file
    if not scored:
        return "Свежих сигналов за 14 дней нет."
    buy, watch = model_score.STOCK_BUY, model_score.STOCK_WATCH
    others = [s for s in scored if s.kind == "stock" and s.decision == "skip"]
    main = [s for s in scored if s.kind != "stock" or s.decision != "skip"]
    lines = [_b(f"СИГНАЛЫ — оценка модели (покупка от {buy:.0f}, наблюдение {watch:.0f}–{buy - 1:.0f})",
                html)]
    lines += [_score_line(s, html) for s in main]
    if others:
        lines.append(f"Прочие (балл ниже {watch:.0f}):")
        lines += [_score_line(s, html) for s in others[:_MAX_OTHER_STOCKS]]
    return "\n".join(lines)


# ------------------------------------------------------- your own positions (/portfolio)
_NO_POSITIONS = ("Ваших позиций нет. Купили? /bought TICKER [цена] — например /bought GME 23.10. "
                 "Модельный портфель: /model.")
_SELL_HINT = "Сигнал на продажу придёт сразу. /sold TICKER — закрыть, /model — модельный портфель."


def _price(x: float) -> str:
    """«1 234,50»: a space between thousands, a comma decimal."""
    return f"{x:,.2f}".replace(",", " ").replace(".", ",")


def quantity(x: float) -> str:
    """A number of shares, «10», «1 234,5» or «0,1235»: at most four decimals, none it doesn't need."""
    text = f"{x:,.4f}".rstrip("0").rstrip(".")
    return text.replace(",", " ").replace(".", ",")


def _status_lines(pos, st: dict, model_holds: bool) -> list[str]:
    """What stands under a position's own line: the stop (when there is a price), who it watches,
    and whether the model holds it too. `st` is positions.position_status; its to_stop is how far
    the price can still fall before the stop, and below the stop (negative) «ниже стопа на» shows
    the same figure without its sign."""
    lines = []
    if st["last"] is not None:
        gap = (f"до стопа {share_pct(st['to_stop'])}" if st["to_stop"] >= 0
               else f"ниже стопа на {share_pct(-st['to_stop'])}")
        lines.append(f"   стоп {_price(st['stop_level'])} (−{st['stop_pct'] * 100:.0f}% от максимума "
                     f"{_price(st['peak'])}), {gap}")
    if pos.insiders:
        lines.append(f"   слежу за продажами: {', '.join(pos.insiders)}")
    if model_holds:
        lines.append("   модель тоже держит")
    return lines


def _my_block(pos, st: dict, model_holds: bool, html: bool) -> str:
    """One position: how it stands now, then its status lines (_status_lines)."""
    now = (f"сейчас {_price(st['last'])} ({signed_pct(st['result'])})" if st["last"] is not None
           else "сейчас — цена недоступна")
    lines = [f"• {crypto.symbol_of(pos.ticker)}: вход {_price(pos.entry_price)} "
             f"({dt.date.fromisoformat(pos.opened_at):%d.%m}), {now}, {st['days']} дн."]
    return "\n".join(_e(line, html) for line in lines + _status_lines(pos, st, model_holds))


def my_position_blocks(rows: list, *, html: bool = True) -> list[str]:
    """The positions of `rows` -- (Position, positions.position_status, model_holds) -- oldest
    first, one block each (its lines joined by a newline)."""
    ordered = sorted(rows, key=lambda r: (r[0].opened_at, r[0].id))
    return [_my_block(pos, st, held, html) for pos, st, held in ordered]


# ---- the Trading 212 account in /portfolio (t212_account.portfolio_view builds what is shown)
_T212_HEADER = "💼 Trading 212"
_OUTSIDE_T212 = "✍️ Вне Trading 212"


def _signed_money(x: float, currency: str | None) -> str:
    """An account's profit or loss in whole units: «+€346», «−€120»."""
    text = money(x, currency)
    return text if text.startswith("−") else f"+{text}"


def _signed_cents(x: float, currency: str | None) -> str:
    """One holding's profit or loss to the cent, the sign after the currency's: «€+8,30»,
    «€−8,30», and «+8,30 CHF» for a currency with no sign of its own."""
    amount = ("−" if x < 0 and round(abs(x), 2) else "+") + _price(abs(x))
    sign = _CURRENCY_SIGN.get(currency or "EUR")
    return f"{sign}{amount}" if sign else f"{amount} {currency}"


def format_t212_account(total: float | None, invested: float | None, pnl: float | None,
                        cash: float | None, currency: str | None = None) -> str:
    """«Счёт: €X · вложено €Y · P/L ±€Z (±W%) · свободно €C»: the account's value, what its
    holdings cost, what they have made or lost (and as a share of that cost), the free cash. A
    figure that isn't known is left out; "" when none is."""
    parts = []
    if total is not None:
        parts.append(f"Счёт: {money(total, currency)}")
    if invested is not None:
        parts.append(f"вложено {money(invested, currency)}")
    if pnl is not None:
        share = f" ({signed_pct(pnl / invested)})" if invested else ""
        parts.append(f"P/L {_signed_money(pnl, currency)}{share}")
    if cash is not None:
        parts.append(f"свободно {money(cash, currency)}")
    return " · ".join(parts)


def _summary_line(summary) -> str:
    """format_t212_account of a t212_account.T212Summary; the profit or loss is the holdings'
    value less their cost when the account didn't tell it."""
    pnl = summary.unrealized_pnl
    if pnl is None and summary.invested_value is not None and summary.invested_cost is not None:
        pnl = summary.invested_value - summary.invested_cost
    return format_t212_account(summary.total_value, summary.invested_cost, pnl, summary.cash_free,
                               summary.currency)


def _t212_result(h, *, stale: bool = False) -> float | None:
    """A holding's result: the price against the average price paid. A stored price that is too
    old to be one (h.price_date) gives no result -- unless `stale`, for the line that shows that
    price with its date."""
    if h.price is None or not h.avg_price or (h.price_date and not stale):
        return None
    return h.price / h.avg_price - 1


def _t212_block(h, html: bool) -> str:
    """One Trading 212 holding (t212_account.Holding): «• GME — 10 шт., средняя 23,10, сейчас
    24,05 USD (+4,1%), €+8,30, 5 дн.», then -- for one the bot tracks -- the status lines of a
    /bought position. The result is on the average price paid and the days are since it was
    bought in Trading 212. What isn't known is left out. A stored price that is stale reads
    «цена на 28.09: 24,05 USD» and has no stop line under it."""
    parts = []
    if h.quantity is not None:
        parts.append(f"{quantity(h.quantity)} шт.")
    if h.avg_price is not None:
        parts.append(f"средняя {_price(h.avg_price)}")
    if h.price is None:
        parts.append("сейчас — цена недоступна")
    else:
        result = _t212_result(h, stale=True)
        # a stored price too old to be one is said to be the price of its day, not «сейчас»
        when = f"цена на {dt.date.fromisoformat(h.price_date):%d.%m}:" if h.price_date else "сейчас"
        parts.append(f"{when} {_price(h.price)}" + (f" {h.currency}" if h.currency else "")
                     + (f" ({signed_pct(result)})" if result is not None else ""))
    if h.pnl is not None:
        parts.append(_signed_cents(h.pnl, h.pnl_currency))
    if h.days is not None:          # since Trading 212's purchase date, not since the bot's tracking
        parts.append(f"{h.days} дн.")
    lines = [f"• {h.name} — " + ", ".join(parts)]
    if h.position is not None and h.status is not None:
        lines += _status_lines(h.position, h.status, h.model_holds)
    elif h.model_holds:
        lines.append("   модель тоже держит")
    return "\n".join(_e(line, html) for line in lines)


def t212_blocks(holdings: list, *, html: bool = True) -> list[str]:
    """The Trading 212 holdings, oldest first, one block each."""
    return [_t212_block(h, html) for h in sorted(holdings, key=lambda h: (h.opened, h.name))]


def _t212_head(view) -> list[str]:
    """What stands under «💼 Trading 212»: the account line of a live answer; or why there is no
    live answer -- with the time the stored holdings shown are from, and the hint when what is
    missing is a key."""
    if view.error is None:
        line = _summary_line(view.summary) if view.summary is not None else ""
        return [line] if line else []
    if view.hint:
        line = f"⚠️ {view.error[:1].upper()}{view.error[1:]}"
    else:
        line = f"⚠️ Trading 212 не ответил ({view.error})"
    if view.holdings and view.as_of:
        line += f" — данные на {view.as_of} последней синхронизации"
    return [line] + ([view.hint] if view.hint else [])


def format_my_portfolio(rows: list, *, html: bool = True, t212=None) -> str:
    """/portfolio: the positions the user recorded with /bought, with how each stands now (a
    block per position, a blank line between), their average result and the hint that a
    signal to sell comes by itself. `rows` as in my_position_blocks.

    With `t212` (t212_account.PortfolioView) the message opens with «💼 Trading 212» -- the
    account line and a block per holding, or why Trading 212 gave no answer -- and `rows`, the
    positions that are not in the account, follow under «✍️ Вне Trading 212». The average is
    then over both."""
    results = [st["result"] for _pos, st, _held in rows if st["result"] is not None]
    if t212 is None:
        if not rows:
            return _NO_POSITIONS
        n = len(rows)
        parts = [_b(f"💼 Ваш портфель — {n} {_plural(n, 'позиция', 'позиции', 'позиций')}", html)]
    else:
        parts = ["\n".join([_b(_T212_HEADER, html)] + [_e(line, html) for line in _t212_head(t212)])]
        parts += t212_blocks(t212.holdings, html=html)
        results += [r for r in map(_t212_result, t212.holdings) if r is not None]
        if not rows and not t212.holdings:
            return "\n\n".join(parts + [_NO_POSITIONS])
        if rows:
            parts.append(_b(_OUTSIDE_T212, html))
    footer = []
    if results:
        k = len(results)
        footer.append(f"Средний результат: {signed_pct(sum(results) / k)} по {k} "
                      f"{_plural(k, 'позиции', 'позициям', 'позициям')}")
    footer.append(_SELL_HINT)
    return "\n\n".join(parts + my_position_blocks(rows, html=html) + ["\n".join(footer)])
