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

import requests

import datefmt

API_URL = "https://api.telegram.org/bot{token}/sendMessage"
MAX_LEN = 3500  # stay under Telegram's 4096-char limit with room to spare


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
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[telegram] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set, skipping notification")
        return False
    ok = True
    for chunk in _chunk(text, MAX_LEN):
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
        except requests.RequestException as e:
            print(f"[telegram] send failed: {e}")
            ok = False
    return ok


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

    L.append(f"\nПолный отчёт: python research.py {_esc(t)}")
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


def format_carry_signal(from_state: str, to_state: str, s: dict, *, reason: str,
                         level: float | None) -> str:
    """A EURUSD carry-gated-strategy state change, from carry_strategy.py.

    Deliberately not styled like the disclosure signals above (no score, no
    buyer list) -- this is a different kind of thing, a price/rate-driven
    trading rule rather than a disclosed transaction. States facts (today's
    price, MA, rate spread, and the stop level) and says explicitly that
    execution is manual, matching this project's stance everywhere else: no
    verdict, no advice, no order placed on the user's behalf.
    """
    icon = {"LONG": "📈", "SHORT": "📉", "FLAT": "⚪️"}[to_state]
    lines = [f"{icon} EURUSD carry-gated: {from_state} → {to_state} ({reason})"]
    lines.append(f"   price {s['close']:.4f} · 200d MA {s['ma']:.4f} · "
                 f"DE-US 2y spread {s['diff']:+.2f}pp")
    if to_state in ("LONG", "SHORT") and level is not None:
        lines.append(f"   initial stop ~{level:.4f} (6×ATR={s['atr']:.4f}) — set a "
                      f"broker-side ATR trailing stop at this distance if you take it")
    elif level is not None:
        lines.append(f"   exit ~{level:.4f}")
    lines.append("   Manual execution — backtested OOS PF 1.92 on 15 trades "
                 "(small sample, see ~/forex-daytrader)")
    return "\n".join(lines)


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
    note = getattr(sig, "market_note", None)
    if note:
        lines.append(f"   {'<i>' + _esc(note) + '</i>' if html else note}")
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
    note = getattr(sig, "market_note", None)
    if note:
        lines.append(f"   {'<i>' + _esc(note) + '</i>' if html else note}")
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
    note = getattr(sig, "market_note", None)
    if note:
        lines.append(f"   {'<i>' + _esc(note) + '</i>' if html else note}")
    if html:
        lines.append(f'   <a href="{_esc(sig.url)}">источник</a>')
    else:
        lines.append(f"   {sig.url}")
    return "\n".join(lines)


_CRYPTO_HEADINGS = {
    # (crypto_kind, bullish) -> heading
    ("treasury", True): "🪙 КОМПАНИЯ КУПИЛА",
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
    note = getattr(sig, "market_note", None)
    if note:
        lines.append(f"   {'<i>' + _esc(note) + '</i>' if html else note}")
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


_CLOSE_REASON = {"insider_sell": "инсайдеры продают", "time": "срок вышел",
                 "stop_loss": "стоп-лосс"}


def _rule_lines(t, html: bool) -> list[str]:
    lines = []
    if t.met:
        text = " · ".join(f"✓ {m}" for m in t.met)
        lines.append(f"   {_esc(text) if html else text}")
    if t.missed:
        text = " · ".join(f"✗ {m}" for m in t.missed)
        lines.append(f"   {_esc(text) if html else text}")
    return lines


def format_close_alert(alert, *, html: bool = True) -> str:
    pos = alert.position
    reason = _CLOSE_REASON.get(alert.trigger, alert.trigger)
    price = ""
    if alert.last_price:
        change = (alert.last_price / pos.entry_price - 1) * 100
        price = f" · вход {pos.entry_price:,.2f} → {alert.last_price:,.2f} ({change:+.1f}%)"
    head = f"🚪 {pos.ticker} — {reason}"
    detail = f"{alert.detail}{price} · открыта {datefmt.fmt(pos.opened_at)}"
    return f"{_b(head, html)}\n   {_esc(detail) if html else detail}"


def format_tiered_digest(selection, closes: list, *, html: bool = True) -> str:
    """One message: 🔥 Сильные, 👀 Кандидаты, 🚪 Закрыть -- the daily Telegram digest
    and the menu's "Сигналы" view (html=False) both render this."""
    parts = []
    if not selection.t212_checked:
        parts.append("⚠️ Trading 212 не проверялся (нет ключа в .env) — показаны все акции.")
    if selection.strong:
        parts.append(_b(f"🔥 Сильные ({len(selection.strong)})", html))
        parts += ["\n".join([format_any_signal(t.signal, html=html)] + _rule_lines(t, html))
                  for t in selection.strong]
    if selection.candidates:
        parts.append(_b(f"👀 Кандидаты ({len(selection.candidates)})", html))
        parts += ["\n".join([format_any_signal(t.signal, html=html)] + _rule_lines(t, html))
                  for t in selection.candidates]
    if closes:
        parts.append(_b(f"🚪 Закрыть ({len(closes)})", html))
        parts += [format_close_alert(a, html=html) for a in closes]
    if not (selection.strong or selection.candidates or closes):
        parts.append("За последние 3 дня сигналов нет.")
    return "\n\n".join(parts)


def format_positions(positions: list, price_fn) -> str:
    if not positions:
        return "Открытых позиций нет. /bought TICKER [цена] — добавить."
    today = dt.date.today()
    lines = ["Открытые позиции:"]
    for p in positions:
        price = price_fn(p.ticker, p.source)
        change = f"{(price / p.entry_price - 1) * 100:+.1f}%" if price else "цена недоступна"
        days = (today - dt.date.fromisoformat(p.opened_at)).days
        lines.append(f"• {p.ticker}: вход {p.entry_price:,.2f}, сейчас "
                     f"{f'{price:,.2f}' if price else '—'} ({change}), {days} дн.")
    return "\n".join(lines)
