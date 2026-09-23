"""Everything this bot knows about one ticker, plus public context, in one place.

The rest of the project is a firehose pointed at whatever is happening today. This
is the other direction: you name a company, and it assembles what has been recorded
about it -- every insider purchase and sale, every 5%+ stake, every notice of
intended sale, every signal it ever fired -- alongside company size, liquidity,
price behaviour, what sell-side analysts currently say, the multi-year revenue /
income / cash-flow trend, the share count over time (dilution or buyback),
institutional ownership, short interest, the earnings calendar, TradingView's
technical readings, the company's own recent SEC filings, and recent news headlines.

WHAT THIS DELIBERATELY DOES NOT DO

It does not tell you whether to buy, and it does not forecast where the price is
going. There is no verdict, no rating of its own, no predicted direction, and no
"worth it / not worth it" line -- a deliberate design decision, not an unfinished
feature. This is an aggregator of public disclosures, the same thing the README has
said since the first version, and turning a pile of facts into "buy this" or "this
is heading up" is investment advice, which needs a licensed adviser and a view of
your circumstances that a script does not have.

What it does instead is put the evidence in one place, with dates and links, so the
judgement is yours and you can see what it rests on.

A note on the analyst section: the consensus rating, target price and rating
changes are what SELL-SIDE ANALYSTS published -- reported and attributed, not a view
the bot holds. Their targets run systematically optimistic and are often wrong. The
current price they are measured against comes from this project's own price history,
not from yfinance's analyst payload, which is stale for small and renamed names.

A note on the TradingView section: it carries a harder caveat than the rest, in
the output itself. TradingView's technical-analysis gauge is a mechanical tally of
~26 momentum and trend indicators; it moves intraday, looks only backward, and has
no established predictive value. It is shown -- with TradingView's own "Strong
Buy" / "Sell" label -- the way a P/E ratio is shown: a number the market looks at,
computed by someone else, identical for every TradingView user, not a
recommendation and not the bot's view. Everything else in that section (RSI, MACD
state, price vs moving averages) is a plain statement of indicator state.

A note on the news section: headlines come from Yahoo Finance's aggregator, which
mixes wire services with message-board-adjacent sites. The publisher is printed next
to every headline for exactly that reason. Treat them as leads to read, not as
findings -- and note that a stock moving on a headline is not evidence the headline
was true.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

import annual_report
import assets
import cluster
import db
import datefmt
import fx
import marketcap
import opinion
import outlook
import sources
import termstyle
import tradingview

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "data" / "disclosures.db"

BENCHMARK = "SPY"
PRICE_WINDOWS = ((21, "1 month"), (63, "3 months"), (126, "6 months"), (252, "1 year"))
FILINGS_LIMIT = 8

# Form types worth surfacing in a company's own filing history. Everything else
# (ownership forms, prospectus supplements, fee tables) is either already covered by
# the insider sections or is noise at this level.
NOTABLE_FORMS = ("8-K", "10-K", "10-Q", "S-1", "S-3", "424B4", "DEF 14A", "SC 13D",
                 "SC 13D/A", "SC 13G", "SC 13G/A", "6-K", "20-F", "40-F")


def _fmt_money(value: float | None, currency: str = "€") -> str:
    if value is None:
        return "?"
    for unit, suffix in ((1e9, "bn"), (1e6, "m"), (1e3, "k")):
        if abs(value) >= unit:
            return f"{currency}{value / unit:,.1f}{suffix}"
    return f"{currency}{value:,.0f}"


def format_entry_target(rep: dict) -> str:
    """Current price as the entry reference, and the real sell-side analyst
    consensus target as the hold-until reference -- both genuine sourced
    numbers (current price from our own price history, target from actual
    analysts, see analyst_view()), never a level this project invents itself.
    "" when neither is available."""
    prices = rep.get("prices")
    current = prices.get("current") if isinstance(prices, dict) else None
    analyst = rep.get("analyst")
    bits = []
    if current is not None:
        bits.append(f"вход ~{current:,.2f}")
    if analyst and analyst.get("target_mean") is not None and not analyst.get("target_stale"):
        up = analyst.get("implied_upside_pct")
        up_str = f", {up:+.0f}%" if up is not None else ""
        bits.append(f"цель аналитиков ~{analyst['target_mean']:,.2f}{up_str}")
    return "💵 " + " · ".join(bits) if bits else ""


# --------------------------------------------------------------- our own data
def insider_activity(conn, ticker: str) -> dict:
    """SEC Form 4 purchases and sales recorded for this ticker."""
    buys = conn.execute(
        """SELECT transaction_date, owner_name, officer_title, is_director, is_officer,
                  is_ten_pct_owner, value, derivative, COALESCE(is_10b5_1, 0), source_url
           FROM sec_purchases WHERE ticker = ? ORDER BY transaction_date DESC""",
        (ticker,),
    ).fetchall()
    sells = conn.execute(
        """SELECT transaction_date, owner_name, officer_title, value, source_url
           FROM sec_sales WHERE ticker = ? ORDER BY transaction_date DESC""",
        (ticker,),
    ).fetchall()
    return {"buys": buys, "sells": sells}


def stakes(conn, ticker: str) -> list:
    return conn.execute(
        """SELECT event_date, person_name, form_type, percent_of_class, amount_owned, source_url
           FROM sec_stakes WHERE ticker = ?
           ORDER BY event_date DESC, percent_of_class DESC""",
        (ticker,),
    ).fetchall()


def proposed_sales(conn, ticker: str) -> list:
    return conn.execute(
        """SELECT approx_sale_date, person_name, relationship, market_value,
                  units_to_sell, units_outstanding, acquisition_nature, source_url
           FROM sec_proposed_sales WHERE ticker = ? ORDER BY approx_sale_date DESC""",
        (ticker,),
    ).fetchall()


def political_trades(conn, ticker: str) -> list:
    """House and Senate PTRs naming this ticker. Both chambers disclose an amount
    bracket rather than a figure, so the numbers here are ranges by nature."""
    house = conn.execute(
        """SELECT txn_date, member_name, txn_type, amount_range, source_url, 'House'
           FROM house_purchases WHERE ticker = ? ORDER BY txn_date DESC""", (ticker,)
    ).fetchall()
    senate = conn.execute(
        """SELECT txn_date, member_name, txn_type, amount_range, source_url, 'Senate'
           FROM senate_purchases WHERE ticker = ? ORDER BY txn_date DESC""", (ticker,)
    ).fetchall()
    return house + senate


def european_activity(conn, key: str) -> list:
    """BaFin, Oslo Børs and Finansinspektionen rows for this identifier.

    Accepts an ISIN as readily as a ticker, because that is how two of these three
    sources identify an issuer -- ask about DE0007190001 and you should get the
    German filings rather than an empty report.
    """
    rows = []
    for sql, source in (
        ("""SELECT txn_date, notifier_name, position, txn_type, volume_eur, 'EUR', source_url
            FROM bafin_purchases WHERE isin = ?""", "BaFin"),
        ("""SELECT txn_date, person, '', txn_type, value, currency, source_url
            FROM norway_purchases WHERE ticker = ?""", "Oslo Børs"),
        ("""SELECT txn_date, person, position, txn_type, value, currency, source_url
            FROM sweden_purchases WHERE isin = ? AND status = 'Aktuell'""", "FI (Sweden)"),
    ):
        for r in conn.execute(sql, (key,)).fetchall():
            rows.append((*r, source))
    # BaFin writes DD.MM.YYYY while the others write ISO, so sort on a normalised key.
    def sort_key(row):
        d = row[0] or ""
        return f"{d[6:10]}-{d[3:5]}-{d[0:2]}" if "." in d else d
    return sorted(rows, key=sort_key, reverse=True)


def corroboration_summary(conn, ticker: str,
                           window_days: int = cluster.CORROBORATION_WINDOW_DAYS) -> dict | None:
    """Distinct disclosure-source regimes that have recorded a signal on this
    ticker, all-time and within the trailing `window_days`. None when fewer
    than 2 distinct sources exist all-time -- nothing to report."""
    all_sources = sorted({r[0] for r in conn.execute(
        "SELECT DISTINCT source FROM signal_journal WHERE ticker = ?", (ticker,),
    ).fetchall()})
    if len(all_sources) < 2:
        return None
    since = (dt.date.today() - dt.timedelta(days=window_days)).isoformat()
    recent_sources = sorted({r[0] for r in conn.execute(
        "SELECT DISTINCT source FROM signal_journal WHERE ticker = ? AND emitted_at >= ?",
        (ticker, since),
    ).fetchall()})
    return {"all_sources": all_sources, "recent_sources": recent_sources}


def past_signals(conn, ticker: str) -> list:
    return conn.execute(
        """SELECT emitted_at, source, kind, buyer_count, total_value_eur, score
           FROM signal_journal WHERE ticker = ? ORDER BY emitted_at DESC""",
        (ticker,),
    ).fetchall()


# ------------------------------------------------------------ external context
def price_context(ticker: str) -> dict:
    """Performance over several windows against the benchmark, plus the latest close.

    Shown because insider activity is much easier to read against what the stock has
    already done -- buying after a 60% fall and buying into strength are different
    situations, and the raw feed never showed which one you were looking at.

    The latest close is returned alongside because the analyst section needs a
    current price it can trust, and yfinance's own analyst blob carries a stale one
    for small and renamed names. One fetch, used for both.
    """
    import yfinance as yf
    try:
        hist = yf.Ticker(ticker).history(period="2y")["Close"].dropna()
        bench = yf.Ticker(BENCHMARK).history(period="2y")["Close"].dropna()
    except Exception:
        return {"windows": [], "current": None}
    if len(hist) < 2:
        return {"windows": [], "current": None}
    windows = []
    for days, label in PRICE_WINDOWS:
        if len(hist) <= days:
            continue
        ret = (float(hist.iloc[-1]) / float(hist.iloc[-1 - days]) - 1) * 100
        rel = None
        if len(bench) > days:
            b = (float(bench.iloc[-1]) / float(bench.iloc[-1 - days]) - 1) * 100
            rel = ret - b
        windows.append({"label": label, "return": ret, "excess": rel})
    return {"windows": windows, "current": float(hist.iloc[-1])}


def recent_filings(ticker: str, cik: str | None) -> list:
    """The company's own recent SEC filings -- what it has told the market lately."""
    if not cik:
        return []
    import requests
    import universe
    try:
        resp = requests.get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json",
                             headers=universe.sec_headers(), timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return []
    recent = data.get("filings", {}).get("recent", {})
    out = []
    for form, filed, doc, acc in zip(recent.get("form", []), recent.get("filingDate", []),
                                      recent.get("primaryDocDescription", []),
                                      recent.get("accessionNumber", [])):
        if form not in NOTABLE_FORMS:
            continue
        out.append({
            "form": form, "filed": filed, "description": doc or "",
            "url": (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                     f"{acc.replace('-', '')}/"),
        })
        if len(out) >= FILINGS_LIMIT:
            break
    return out, data.get("name"), data.get("sicDescription")


# 1.0 = Strong Buy ... 5.0 = Strong Sell, the Yahoo/refinitiv convention. Cut points
# sit at the half-integers.
_CONSENSUS_BANDS = ((1.5, "Strong Buy"), (2.5, "Buy"), (3.5, "Hold"),
                     (4.5, "Sell"), (float("inf"), "Strong Sell"))
# How many rating-scale points the weighted mean must move over the covered period
# before it is called a shift rather than drift.
_TREND_THRESHOLD = 0.30
# Fewer analysts than this: shown, but labelled -- three price targets are not a
# consensus.
_THIN_COVERAGE = 3
# A target this many times off the real price (either direction) is stale data, not
# a view. yfinance carries exactly this for at least one currently-held ticker.
_STALE_TARGET_RATIO = 3.0
# An implied move past this, but not far enough to be called stale, gets a note
# rather than a number presented bare: it means either the target is out of date or
# the stock is a speculative one priced for a binary outcome (common in
# clinical-stage biotech), and "+142%" with no context reads as a promise.
_WIDE_UPSIDE_PCT = 50.0


def _weighted_mean(counts: dict) -> tuple[float | None, int]:
    """(rating-scale mean, total analysts) from a period's rec counts."""
    weights = {"strongBuy": 1, "buy": 2, "hold": 3, "sell": 4, "strongSell": 5}
    total = sum(counts.get(k, 0) for k in weights)
    if total == 0:
        return None, 0
    return sum(weights[k] * counts.get(k, 0) for k in weights) / total, total


def _fetch_analyst_data(ticker: str) -> dict:
    """Raw analyst data from yfinance, normalised to plain dicts/lists so the view
    logic below carries no pandas or yfinance dependency and stays unit-testable.

    Uses the three lightweight properties, never .info -- that call is heavy and its
    analyst fields are the stalest of the lot.
    """
    import yfinance as yf
    t = yf.Ticker(ticker)
    out = {"price_targets": {}, "recommendations": [], "upgrades_downgrades": []}
    try:
        pt = t.analyst_price_targets or {}
        out["price_targets"] = {k: float(v) for k, v in pt.items()
                                 if v is not None and str(v) != "nan"}
    except Exception:
        pass
    try:
        rec = t.recommendations
        if rec is not None and not rec.empty:
            out["recommendations"] = rec.to_dict("records")
    except Exception:
        pass
    try:
        ud = t.upgrades_downgrades
        if ud is not None and not ud.empty:
            for row in ud.reset_index().head(8).to_dict("records"):
                out["upgrades_downgrades"].append({
                    "date": str(row.get("GradeDate", ""))[:10],
                    "firm": str(row.get("Firm", "") or ""),
                    "to_grade": str(row.get("ToGrade", "") or ""),
                    "action": str(row.get("Action", "") or ""),
                })
    except Exception:
        pass
    return out


def analyst_view(raw: dict, current_price: float | None) -> dict | None:
    """What sell-side analysts currently say about this stock, or None if nobody
    meaningfully covers it.

    Everything here is third-party opinion, reported and attributed -- the tool
    forms no view of its own. `current_price` must come from our own price history,
    not from `raw`: yfinance's analyst payload carries a stale price for small and
    renamed names, and the implied upside is only as good as the price it is
    measured against.
    """
    recs = raw.get("recommendations") or []
    latest = recs[0] if recs else {}
    mean_now, total = _weighted_mean(latest)

    targets = raw.get("price_targets") or {}
    target_mean = targets.get("mean")

    if mean_now is None and target_mean is None:
        return None

    consensus = None
    if mean_now is not None:
        consensus = next(label for cut, label in _CONSENSUS_BANDS if mean_now < cut)

    # Trend: compare the newest period against the oldest one on file.
    trend = None
    if len(recs) > 1:
        mean_then, _ = _weighted_mean(recs[-1])
        if mean_then is not None and mean_now is not None:
            delta = mean_now - mean_then          # lower mean = more bullish
            if delta <= -_TREND_THRESHOLD:
                trend = "позитивнее"
            elif delta >= _TREND_THRESHOLD:
                trend = "осторожнее"

    # Target sanity check against the real price.
    target_stale = False
    implied_upside = None
    if target_mean is not None and current_price:
        ratio = target_mean / current_price
        if ratio > _STALE_TARGET_RATIO or ratio < 1 / _STALE_TARGET_RATIO:
            target_stale = True
        else:
            implied_upside = (ratio - 1) * 100
    if target_stale:
        target_mean = None

    return {
        "consensus": consensus,
        "analyst_count": total or None,
        "thin": 0 < total < _THIN_COVERAGE,
        "counts": {"sb": latest.get("strongBuy", 0), "b": latest.get("buy", 0),
                    "h": latest.get("hold", 0), "s": latest.get("sell", 0),
                    "ss": latest.get("strongSell", 0)},
        "trend": trend,
        "target_mean": target_mean,
        "target_high": None if target_stale else targets.get("high"),
        "target_low": None if target_stale else targets.get("low"),
        "target_stale": target_stale,
        "implied_upside_pct": implied_upside,
        "implied_upside_wide": (implied_upside is not None
                                 and abs(implied_upside) > _WIDE_UPSIDE_PCT),
        "recent_actions": (raw.get("upgrades_downgrades") or [])[:4],
    }


def _format_analyst(view: dict) -> str:
    L = ["\n" + termstyle.section("Аналитики (мнения третьих сторон, не прогноз бота)")]
    if view["consensus"]:
        n = view["analyst_count"]
        thin = "  ⚠️ тонкое покрытие" if view["thin"] else ""
        c = view["counts"]
        L.append(f"  Консенсус: {view['consensus']} · {n} аналитик(ов){thin}")
        L.append(f"    [strongBuy {c['sb']} · buy {c['b']} · hold {c['h']} · "
                 f"sell {c['s']} · strongSell {c['ss']}]")
    if view["trend"]:
        L.append(f"  За последние месяцы рейтинги стали {view['trend']}")
    if view["target_mean"] is not None:
        rng = ""
        if view["target_high"] and view["target_low"]:
            rng = f"  (диапазон {view['target_low']:,.0f}–{view['target_high']:,.0f})"
        up = ""
        if view["implied_upside_pct"] is not None:
            up = f"  ≈ {view['implied_upside_pct']:+.0f}% к текущей цене"
        L.append(f"  Средняя целевая цена: {view['target_mean']:,.0f}{rng}{up}")
        if view.get("implied_upside_wide"):
            L.append("    (большой разрыв с ценой — либо цель устарела, либо это "
                     "спекулятивная бумага; проверьте даты справа)")
    elif view["target_stale"]:
        L.append("  Целевые цены у источника выглядят устаревшими — пропущены.")
    for a in view["recent_actions"]:
        grade = f" — {a['to_grade']}" if a["to_grade"] else ""
        L.append(f"    {a['date']}  {a['firm']}{grade}")
    L.append("  Это то, что публикует sell-side, а не прогноз этого бота.")
    return "\n".join(L)


# ------------------------------------------------------- financial snapshot
_FIN_ROWS = {
    "revenue": ("Total Revenue",),
    "net_income": ("Net Income", "Net Income Common Stockholders",
                    "Net Income From Continuing Operation Net Minority Interest"),
    "operating_income": ("Operating Income", "Total Operating Income As Reported"),
    "fcf": ("Free Cash Flow",),
    "cash": ("Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"),
    "debt": ("Total Debt",),
}


def _pick_row(df, names):
    for n in names:
        if n in df.index:
            return df.loc[n]
    return None


def financials(ticker: str) -> dict | None:
    """Revenue / income / cash flow / balance-sheet trend, last four fiscal years.

    From Yahoo Finance's statement data. It is a scraped third-party representation
    of the filings, not the filings themselves -- fine for a trend at a glance, and
    the authoritative numbers are one click away in the SEC-filings section above.
    """
    import yfinance as yf
    try:
        t = yf.Ticker(ticker.replace(".", "-"))
        inc, bal, cf = t.income_stmt, t.balance_sheet, t.cashflow
    except Exception:
        return None
    if inc is None or inc.empty:
        return None
    years = [c.year for c in list(inc.columns)[:4]]
    out = {"years": years}
    for key, names in _FIN_ROWS.items():
        src = inc if key in ("revenue", "net_income", "operating_income") else (
            cf if key == "fcf" else bal)
        row = _pick_row(src, names) if src is not None and not src.empty else None
        out[key] = ([None if _isnan(v) else float(v) for v in list(row)[:4]]
                    if row is not None else [None] * len(years))
    return out if any(any(v is not None for v in out[k]) for k in _FIN_ROWS) else None


def _isnan(v) -> bool:
    try:
        return v != v
    except Exception:
        return v is None


def _trend_arrow(values: list) -> str:
    """↑ / ↓ / → from the first and last non-null value in a series (oldest first
    once reversed by the caller)."""
    nums = [v for v in values if v is not None]
    if len(nums) < 2 or nums[0] == 0:
        return ""
    change = (nums[-1] - nums[0]) / abs(nums[0])
    return " ↑" if change > 0.05 else " ↓" if change < -0.05 else " →"


def _format_financials(data: dict) -> str:
    years = list(reversed(data["years"]))
    L = ["\n" + termstyle.section("Финансы (Yahoo Finance, последние годы)")]
    L.append("  " + " " * 14 + "".join(f"{y:>12}" for y in years))
    labels = (("revenue", "Выручка"), ("operating_income", "Оп. прибыль"),
              ("net_income", "Чистая приб."), ("fcf", "Своб. ден.поток"),
              ("cash", "Кэш"), ("debt", "Долг"))
    for key, label in labels:
        series = list(reversed(data[key]))
        if not any(v is not None for v in series):
            continue
        cells = "".join(f"{_fmt_money(v, '$'):>12}" if v is not None else f"{'—':>12}"
                        for v in series)
        L.append(f"  {label:<14}{cells}{_trend_arrow(series)}")
    return "\n".join(L)


# ---------------------------------------------------- dilution / buyback (SEC)
def share_count_history(cik: str | None) -> list | None:
    """Common shares outstanding over time, straight from the company's XBRL facts.

    This one is authoritative (SEC, not a scrape) and single-concept, so it is
    cheap to get right -- and it sits directly under the insider-buying section on
    purpose: an insider buying while the company dilutes, or alongside a buyback,
    are different pictures.
    """
    if not cik:
        return None
    import requests
    import universe
    try:
        resp = requests.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik):010d}.json",
                            headers=universe.sec_headers(), timeout=30)
        resp.raise_for_status()
        facts = resp.json().get("facts", {})
    except Exception:
        return None
    points = (facts.get("us-gaap", {}).get("CommonStockSharesOutstanding", {})
              .get("units", {}).get("shares", []))
    if not points:
        points = (facts.get("dei", {}).get("EntityCommonStockSharesOutstanding", {})
                  .get("units", {}).get("shares", []))
    if not points:
        return None
    # One value per period end, keeping the most recently filed (restatements).
    by_end: dict = {}
    for pt in points:
        end = pt.get("end")
        if end and (end not in by_end or pt.get("filed", "") > by_end[end].get("filed", "")):
            by_end[end] = pt
    ordered = sorted(by_end.values(), key=lambda x: x["end"])
    return [{"end": pt["end"], "shares": pt["val"]} for pt in ordered[-6:]]


def _format_dilution(points: list) -> str:
    L = ["\n" + termstyle.section("Акции в обращении / размытие (SEC XBRL)")]
    for pt in points:
        L.append(f"  {pt['end']}   {pt['shares'] / 1e6:>12,.1f} млн")
    first, last = points[0]["shares"], points[-1]["shares"]
    if first:
        change = (last - first) / first * 100
        if change > 1:
            verdict = f"за период размытие на {change:+.1f}%"
        elif change < -1:
            verdict = f"за период выкуп {change:+.1f}% (обратный выкуп акций)"
        else:
            verdict = "число акций практически не менялось"
        L.append(f"  → {verdict}")
    return "\n".join(L)


# ---------------------------------------------------- ownership & short interest
def _yf_info(ticker: str) -> dict:
    """One `.info` fetch, shared by the ownership and short-interest sections.

    `.info` is the heaviest yfinance call and the rest of this module avoids it --
    but the dossier is a manual CLI, not the daily loop, and one call feeds two
    sections."""
    import yfinance as yf
    try:
        return yf.Ticker(ticker.replace(".", "-")).info or {}
    except Exception:
        return {}


def ownership(ticker: str, info: dict) -> dict | None:
    import yfinance as yf
    tk = yf.Ticker(ticker.replace(".", "-"))
    holders, count = [], None
    try:
        ih = tk.institutional_holders
        if ih is not None and not ih.empty:
            for row in ih.head(5).to_dict("records"):
                holders.append({
                    "name": str(row.get("Holder", "")),
                    "pct": row.get("pctHeld"),
                    "pct_change": row.get("pctChange"),
                    "date": str(row.get("Date Reported", ""))[:10],
                })
    except Exception:
        pass
    try:
        mh = tk.major_holders
        if mh is not None and "institutionsCount" in mh.index:
            count = int(mh.loc["institutionsCount"].iloc[0])
    except Exception:
        pass
    insiders_pct = info.get("heldPercentInsiders")
    inst_pct = info.get("heldPercentInstitutions")
    if not holders and insiders_pct is None and inst_pct is None:
        return None
    return {"insiders_pct": insiders_pct, "institutions_pct": inst_pct,
            "institutions_count": count, "top_holders": holders}


def _format_ownership(data: dict) -> str:
    L = ["\n" + termstyle.section("Структура владения (Yahoo Finance)")]
    bits = []
    if data["insiders_pct"] is not None:
        bits.append(f"инсайдеры {data['insiders_pct'] * 100:.1f}%")
    if data["institutions_pct"] is not None:
        bits.append(f"институционалы {data['institutions_pct'] * 100:.1f}%")
    if data.get("institutions_count"):
        bits.append(f"{int(data['institutions_count'])} фондов")
    if bits:
        L.append("  " + " · ".join(bits))
    for h in data["top_holders"]:
        pct = f"{h['pct'] * 100:.1f}%" if h["pct"] is not None else "?"
        chg = ""
        if h["pct_change"] is not None:
            chg = f"  ({h['pct_change'] * 100:+.1f}% за квартал)"
        L.append(f"    {h['name'][:34]:<34} {pct:>6}{chg}")
    return "\n".join(L)


def short_interest(info: dict) -> dict | None:
    pct = info.get("shortPercentOfFloat")
    shares = info.get("sharesShort")
    prior = info.get("sharesShortPriorMonth")
    ratio = info.get("shortRatio")
    if pct is None and shares is None:
        return None
    return {"pct_of_float": pct, "shares": shares, "prior": prior, "days_to_cover": ratio}


def _format_short(data: dict) -> str:
    L = ["\n" + termstyle.section("Короткие позиции (Yahoo Finance / FINRA)")]
    if data["pct_of_float"] is not None:
        L.append(f"  {data['pct_of_float'] * 100:.1f}% фри-флоата в шорте")
    if data["shares"] is not None and data["prior"]:
        direction = ("выросли" if data["shares"] > data["prior"]
                     else "снизились" if data["shares"] < data["prior"] else "без изменений")
        L.append(f"  {data['shares'] / 1e6:,.1f} млн акций в шорте — {direction} "
                 f"против {data['prior'] / 1e6:,.1f} млн месяцем ранее")
    if data["days_to_cover"] is not None:
        L.append(f"  дней на покрытие: {data['days_to_cover']:.1f}")
    return "\n".join(L)


# --------------------------------------------------------------- earnings dates
def earnings_calendar(ticker: str) -> dict | None:
    import datetime as _dt
    import yfinance as yf
    try:
        ed = yf.Ticker(ticker.replace(".", "-")).earnings_dates
    except Exception:
        return None
    if ed is None or ed.empty:
        return None
    today = _dt.date.today()
    upcoming, past = [], []
    for ts, row in ed.iterrows():
        d = ts.date() if hasattr(ts, "date") else None
        entry = {"date": str(ts)[:10],
                 "estimate": None if _isnan(row.get("EPS Estimate")) else float(row.get("EPS Estimate")),
                 "reported": None if _isnan(row.get("Reported EPS")) else float(row.get("Reported EPS")),
                 "surprise": None if _isnan(row.get("Surprise(%)")) else float(row.get("Surprise(%)"))}
        if d and d >= today:
            upcoming.append(entry)
        else:
            past.append(entry)
    return {"next": upcoming[-1] if upcoming else None, "recent": past[:4]}


def _format_earnings(data: dict) -> str:
    L = ["\n" + termstyle.section("Отчётность (Yahoo Finance)")]
    if data["next"]:
        est = f", прогноз EPS {data['next']['estimate']:.2f}" if data["next"]["estimate"] is not None else ""
        L.append(f"  Следующий отчёт: {data['next']['date']}{est}")
    for e in data["recent"]:
        if e["reported"] is None:
            continue
        surp = f"  ({e['surprise']:+.1f}% к прогнозу)" if e["surprise"] is not None else ""
        L.append(f"    {e['date']}  EPS {e['reported']:.2f}{surp}")
    return "\n".join(L)


# ------------------------------------------------------------------ assembly
def _build_stock(conn, asset) -> dict:
    ticker = asset.symbol
    import cik_map

    # An ISIN is how BaFin and Finansinspektionen name an issuer, and it is a
    # perfectly reasonable thing to type here. But yfinance will happily return a
    # price series for one anyway, and there is no way to confirm the series belongs
    # to the issuer being asked about -- so price, news and SEC filings are skipped
    # rather than shown with a silent chance of describing a different company.
    is_isin = asset.is_isin

    try:
        cik = None if is_isin else cik_map.CikMap().cik(ticker)
    except Exception:
        cik = None

    european = european_activity(conn, ticker)
    prices = {"windows": [], "current": None} if is_isin else price_context(asset.yahoo)
    source_notes = {}
    if prices.get("current") is None and not is_isin:
        prices["current"], source_notes["prices"] = sources.current_price(asset)
    analyst = None
    if not is_isin:
        raw, source_notes["analyst"] = _analyst_raw(asset)
        analyst = analyst_view(raw, prices.get("current")) if raw else None
    filings_result = recent_filings(ticker, cik) if not is_isin else []

    # TradingView resolves ISINs, so it runs either way. The rest are US-ticker only.
    tv, source_notes["indicators"] = sources.indicators(asset)
    if is_isin:
        fin = dilution = own = short = earnings = annual = None
    else:
        info = _yf_info(ticker)
        fin = financials(ticker)
        dilution = share_count_history(cik)
        own = ownership(ticker, info)
        short = short_interest(info)
        earnings = earnings_calendar(ticker)
        annual = annual_report.build(cik)
    filings, sec_name, industry = (filings_result if isinstance(filings_result, tuple)
                                    else (filings_result, None, None))
    facts = marketcap.facts(conn, ticker) or {}
    cap = marketcap.market_cap_eur(conn, ticker)
    adv = (fx.to_eur(facts["avg_daily_value"], facts.get("currency") or "USD", conn)
           if facts.get("avg_daily_value") else None)

    # For a non-US issuer the only name available is the one the local regulator
    # filed it under.
    if not sec_name and european:
        sec_name = conn.execute(
            "SELECT issuer_name FROM bafin_purchases WHERE isin = ? "
            "UNION SELECT issuer_name FROM sweden_purchases WHERE isin = ? LIMIT 1",
            (ticker, ticker),
        ).fetchone()
        sec_name = sec_name[0] if sec_name else None

    headlines, source_notes["news"] = sources.news(asset, sec_name)

    rep = {
        "ticker": ticker,
        "cik": cik,
        "is_isin": is_isin,
        "name": sec_name,
        "industry": industry,
        "market_cap_eur": cap,
        "size": marketcap.size_bucket(cap),
        "avg_daily_value": adv,
        "exchange": facts.get("exchange"),
        "insiders": insider_activity(conn, ticker),
        "european": european,
        "stakes": stakes(conn, ticker),
        "proposed_sales": proposed_sales(conn, ticker),
        "political": political_trades(conn, ticker),
        "signals": past_signals(conn, ticker),
        "corroboration": corroboration_summary(conn, ticker),
        "prices": prices,
        "analyst": analyst,
        "tradingview": tv,
        "financials": fin,
        "annual_report": annual,
        "dilution": dilution,
        "ownership": own,
        "short": short,
        "earnings": earnings,
        "filings": filings,
        "news": headlines or [],
        "kind": "stock",
        "asset": asset,
        "outlook": outlook.lookup(conn, asset),
        "sources": source_notes,
    }
    rep["opinion"] = opinion.score(rep)
    if rep["opinion"]:
        db.journal_opinion(conn, ticker, rep["opinion"])
    # News is deliberately excluded: Google News returns results for almost any
    # query, including a made-up ticker, so it is no evidence the symbol is real.
    rep["found"] = bool(prices.get("current") is not None or rep["insiders"]["buys"]
                        or rep["insiders"]["sells"] or european or rep["stakes"]
                        or rep["political"] or rep["tradingview"]
                        or rep["analyst"] or rep["financials"])
    return rep


def _analyst_raw(asset):
    """Yahoo's analyst data, falling back to Nasdaq's consensus (US listings only)."""
    def yahoo():
        raw = _fetch_analyst_data(asset.yahoo)
        return raw if (raw["price_targets"] or raw["recommendations"]) else None
    attempts = [("Yahoo", yahoo)]
    if not asset.exchange:
        attempts.append(("Nasdaq", lambda: sources.nasdaq_analyst(asset.symbol)))
    return sources.first_available(attempts)


def build(conn, text: str) -> dict:
    """Dossier for any stock, ETF, coin or ISIN (see assets.resolve). Raises
    ValueError when `text` isn't shaped like a ticker at all."""
    asset = assets.resolve(text, coins=lambda: sources.cached_coin_symbols(conn),
                           stocks=sources.stock_universe_symbols)
    if asset is None:
        raise ValueError(f"not a ticker: {text!r}")
    if asset.kind == "crypto":
        import crypto_research
        return crypto_research.build(conn, asset)
    return _build_stock(conn, asset)


_SOURCE_FIRST = {"prices": ("цены", "Yahoo"), "analyst": ("аналитики", "Yahoo"),
                 "indicators": ("индикаторы", "TradingView"), "news": ("новости", "Yahoo")}


def _source_notes(rep: dict) -> list[str]:
    return [f"{label}: {rep['sources'][k]}" for k, (label, first) in _SOURCE_FIRST.items()
            if rep.get("sources", {}).get(k) and rep["sources"][k] != first]


_NOT_FOUND_HINT = "Примеры: NVDA, BTC, EQNR.OL, $BTC (акция), BTC-USD (монета)."


def format_brief(rep: dict) -> str:
    """What run_claude_analysis.sh hands Claude: the asset, the deterministic parts
    and the headlines."""
    if rep.get("kind") == "crypto":
        import crypto_research
        return crypto_research.format_brief(rep)
    L = [f"АКТИВ: {rep['ticker']} (акция" + (f", {rep['name']}" if rep.get("name") else "") + ")"]
    if rep.get("opinion"):
        L.append(opinion.format_opinion(rep["opinion"]).strip())
    entry_target = format_entry_target(rep)
    if entry_target:
        L.append(entry_target)
    L.append(outlook.format_outlook(rep["outlook"], rep["ticker"]))
    L.append("---NEWS---")
    L += [f"{n['published']} [{n['publisher']}] {n['title']}" for n in rep["news"]]
    return "\n".join(L)


def format_report(rep: dict) -> str:
    if rep.get("kind") == "crypto":
        import crypto_research
        return crypto_research.format_report(rep)
    if not rep.get("found", True):
        return f"Не нашёл такой тикер: {rep['ticker']}\n{_NOT_FOUND_HINT}"
    t = rep["ticker"]
    title = t + (f" — {rep['name']}" if rep["name"] else "")
    L = ["\n" + termstyle.header(title)]

    bits = []
    if rep["industry"]:
        bits.append(rep["industry"])
    if rep["market_cap_eur"]:
        bits.append(f"{_fmt_money(rep['market_cap_eur'])} ({rep['size']})")
    if rep["avg_daily_value"] is not None:
        thin = "  ⚠️ низкая ликвидность" if rep["avg_daily_value"] < 250_000 else ""
        bits.append(f"оборот {_fmt_money(rep['avg_daily_value'])}/день{thin}")
    if rep["exchange"]:
        bits.append(rep["exchange"])
    if bits:
        L.append("  " + " · ".join(bits))

    if rep.get("opinion"):
        L.append(opinion.format_opinion(rep["opinion"]))

    entry_target = format_entry_target(rep)
    if entry_target:
        L.append("  " + entry_target)

    if rep.get("outlook"):
        L.append("\n" + outlook.format_outlook(rep["outlook"], t))

    if rep.get("is_isin"):
        L.append("\n  ISIN, а не тикер: цена, новости, отчётность SEC, финансы, владение,"
                 "\n  шорт и даты отчётов пропущены — сопоставить ISIN с торговым символом"
                 "\n  надёжно нельзя, а показать чужую бумагу хуже, чем не показать ничего."
                 "\n  Раздел TradingView ниже всё же работает (он резолвит ISIN сам)."
                 "\n  Для остального введите биржевой тикер.")

    windows = rep["prices"]["windows"] if isinstance(rep["prices"], dict) else rep["prices"]
    if windows:
        L.append("\n" + termstyle.section("Цена"))
        for p in windows:
            rel = f"  ({p['excess']:+.1f} п.п. к {BENCHMARK})" if p["excess"] is not None else ""
            L.append(f"  {p['label']:10} {p['return']:+7.1f}%{rel}")

    if rep.get("analyst"):
        L.append(_format_analyst(rep["analyst"]))

    if rep.get("tradingview"):
        L.append(tradingview.format_view(rep["tradingview"]))
    if rep.get("financials"):
        L.append(_format_financials(rep["financials"]))
    if rep.get("annual_report"):
        L.append(annual_report.format_report(rep["annual_report"]))
    if rep.get("dilution"):
        L.append(_format_dilution(rep["dilution"]))
    if rep.get("ownership"):
        L.append(_format_ownership(rep["ownership"]))
    if rep.get("short"):
        L.append(_format_short(rep["short"]))
    if rep.get("earnings"):
        L.append(_format_earnings(rep["earnings"]))

    buys, sells = rep["insiders"]["buys"], rep["insiders"]["sells"]
    L.append("\n" + termstyle.section("Инсайдеры (SEC Form 4)"))
    if not buys and not sells:
        L.append("  В базе ничего нет по этому тикеру."
                 if not rep["european"] else "  Ничего (это не US-эмитент — см. ниже).")
    for (date, owner, title, is_dir, is_off, is_ten, value, deriv, plan, url) in buys[:12]:
        role = title or ("Director" if is_dir else "Officer" if is_off
                          else "10%+ Owner" if is_ten else "Insider")
        tags = ("  [дериватив]" if deriv else "") + ("  [план 10b5-1]" if plan else "")
        L.append(f"  🟢 {datefmt.fmt(date)}  {owner} ({role})  ${value or 0:,.0f}{tags}")
    for (date, owner, title, value, url) in sells[:8]:
        L.append(f"  🔴 {datefmt.fmt(date)}  {owner} ({title or 'Insider'})  ${value or 0:,.0f}")

    if rep["european"]:
        L.append("\n" + termstyle.section("Инсайдеры (BaFin / Осло / Швеция)"))
        for (date, person, position, ttype, value, currency, url, source) in rep["european"][:12]:
            icon = "🟢" if ttype == "P" else "🔴" if ttype == "S" else "⚪"
            role = f" ({position})" if position else ""
            L.append(f"  {icon} {datefmt.fmt(date)}  {person}{role}  "
                     f"{value or 0:,.0f} {currency}  [{source}]")

    if rep["stakes"]:
        L.append("\n" + termstyle.section("Крупные доли (13D/G)"))
        for (date, person, form, pct, amount, url) in rep["stakes"][:8]:
            kind = "активист" if form.startswith("SCHEDULE 13D") else "пассивный"
            L.append(f"  {datefmt.fmt(date) if date else '?':12} {person}  "
                     f"{pct:.2f}% ({kind}, {form})")

    if rep["proposed_sales"]:
        L.append("\n" + termstyle.section("Заявленные намерения продать (Form 144)"))
        for (date, person, rel, value, units, outstanding, nature, url) in rep["proposed_sales"][:8]:
            pct = f", {units / outstanding * 100:.3f}% класса" if units and outstanding else ""
            L.append(f"  {datefmt.fmt(date) if date else '?':12} {person} ({rel or '?'})  "
                     f"${value or 0:,.0f}{pct}  {nature or ''}")

    if rep["political"]:
        L.append("\n" + termstyle.section("Политики (STOCK Act)"))
        for (date, member, ttype, amount, url, chamber) in rep["political"][:8]:
            icon = "🟢" if ttype == "P" else "🔴"
            L.append(f"  {icon} {datefmt.fmt(date)}  {member} [{chamber}]  {amount}")

    if rep["signals"]:
        L.append("\n" + termstyle.section("Сигналы, которые бот уже присылал"))
        corr = rep.get("corroboration")
        if corr:
            line = "Независимые источники по этому тикеру: " + ", ".join(corr["all_sources"])
            if corr["recent_sources"] and corr["recent_sources"] != corr["all_sources"]:
                line += (f" (за последние {cluster.CORROBORATION_WINDOW_DAYS} дней: "
                         + ", ".join(corr["recent_sources"]) + ")")
            L.append(f"  {line}")
        for (when, source, kind, buyers, value, score) in rep["signals"][:8]:
            L.append(f"  {when[:10]}  {source} {kind}  {buyers} чел.  "
                     f"€{value or 0:,.0f}  {score or 0:.0f} баллов")

    if rep["filings"]:
        L.append("\n" + termstyle.section("Что компания сама подавала в SEC"))
        for f in rep["filings"]:
            L.append(f"  {f['filed']}  {f['form']:10} {f['description'][:38]}")
            L.append(f"      {f['url']}")

    if rep["news"]:
        L.append("\n" + termstyle.section("Новости"))
        for n in rep["news"]:
            L.append(f"  {n['published']}  [{n['publisher']}]  {n['title'][:70]}")
            if n["url"]:
                L.append(f"      {n['url']}")

    notes = _source_notes(rep)
    if notes:
        L.append("\n  Источники: " + " · ".join(notes))

    L.append("\n" + "-" * 72)
    if rep.get("opinion"):
        L.append("  • ОПИНИОН вверху — детерминированная свёртка фактов ниже (opinion.py)")
        L.append("  • Источники ниже кликабельны — проверьте, на чём стоит каждый балл")
    else:
        L.append("  • Сводка публичных раскрытий, опиниона нет — недостаточно данных (ISIN")
        L.append("    либо меньше двух значимых факторов, см. opinion.py)")
    L.append("-" * 72)
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ticker", help="ticker, coin or ISIN, e.g. NVDA, BTC, EQNR.OL, $BTC")
    args = ap.parse_args()
    conn = db.connect(DB_PATH)
    try:
        print(format_report(build(conn, args.ticker)))
    except ValueError:
        print("Не похоже на тикер. Примеры: NVDA, BTC, EQNR.OL, $BTC (акция), BTC-USD (монета).")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
