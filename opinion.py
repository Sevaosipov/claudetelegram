"""A deterministic Buy/Hold/Avoid opinion, synthesized from everything
research.py already assembles for one ticker: recent insider and political
trading, annual-report red flags, the SEC XBRL financials trend, analyst
consensus, 13D/G stakes, ownership trend, price momentum vs a benchmark,
TradingView's aggregate technical gauge plus its individual RSI/MACD/moving-
average/ADX readings, and recent news headlines.

NEWS SCORING IS A NAIVE KEYWORD MATCH, NOT SENTIMENT ANALYSIS. It counts
headlines containing a bullish or bearish term from a short fixed list and
nets the counts -- no negation handling ("shares don't disappoint" scores as
bearish), no context, no source weighting. Kept deliberately small (CAP_NEWS)
for exactly that reason -- same treatment as TradingView's gauge, which this
module already scores lightly for the same "mechanical, not deeply
validated" reason. A genuine sentiment read needs an LLM reading the actual
text, which this deterministic, no-API pipeline doesn't have; ask Claude
directly for that (see format_opinion()'s own note).

This is the one place in the project that crosses the line every other module
deliberately stays behind -- see research.py's own module docstring and
README's "what's deliberately absent" section for why that line existed
(turning facts into "buy this" is investment advice, which needs a licensed
adviser). Added at the user's explicit request after being told directly that
this reverses that design choice, across three separate confirmations. Kept to
the same standard as cluster.score_signal: named weights chosen by reasoning
(not fitted), every factor's contribution shown so the number is auditable
rather than a black box, and the same "starting point, not a finding" caveat.

WHAT THIS DELIBERATELY DOESN'T SCORE:
  - Valuation (P/E). A "high" or "low" P/E only means something against a
    sector peer group this project has no data for; scoring it naively would
    penalize every high-growth name and reward every value trap. Shown as
    data (research.py's own financials section), not folded into the score.
  - Short interest. High-and-rising short interest is not cleanly bullish or
    bearish on its own -- it's as consistent with "the market has a real bear
    case" as with "short-squeeze setup." Shown, not scored.

Returns None when there isn't enough to responsibly weigh in on -- an ISIN
report (skips financials/annual-report/analyst entirely, see research.build)
or a ticker with no insider/political/stake/analyst/ownership/price data at
all gets no opinion rather than a number built on almost nothing.
"""
from __future__ import annotations

import datetime as dt

RECENCY_DAYS = 365  # only trades within the trailing year count toward the score
MIN_FACTORS = 2      # need at least this many non-trivial inputs to say anything

W_INSIDER_PER_NET_BUYER = 12.0
CAP_INSIDER = 40.0
W_POLITICAL_PER_NET_BUYER = 4.0
CAP_POLITICAL = 12.0
W_STAKE_ACTIVIST = 15.0
W_STAKE_PASSIVE = 5.0
CAP_STAKES = 25.0
W_RED_FLAG = -25.0
W_FIN_TREND_PER_METRIC = 6.0
CAP_FIN_TREND = 18.0
_ANALYST_CONSENSUS_POINTS = {"Strong Buy": 20.0, "Buy": 10.0, "Hold": 0.0,
                              "Sell": -10.0, "Strong Sell": -20.0}
W_ANALYST_UPSIDE_PER_10PCT = 3.0
CAP_ANALYST_UPSIDE = 15.0
W_OWNERSHIP_TREND = 6.0
W_MOMENTUM_PER_WINDOW = 3.0
CAP_MOMENTUM = 9.0
W_TV_GAUGE = 8.0
W_NEWS_PER_NET_HEADLINE = 2.5
CAP_NEWS = 10.0
W_MACD = 5.0
W_MA_TREND = 5.0
W_RSI_MOMENTUM = 3.0
CAP_TECHNICALS = 15.0
# ADX measures trend STRENGTH, not direction, so it can't be its own +/- factor
# the way everything else here is -- it scales how much the other technical
# signals above are trusted instead (a MACD cross in a directionless market is
# noisier than the same cross in a strongly trending one). Three bands, not a
# continuous curve, for the same reason every other threshold in this project
# is a round number: legible over precisely fitted.
_ADX_BANDS = ((25.0, 1.0, "сильный тренд"), (15.0, 0.7, "умеренный тренд"),
              (0.0, 0.4, "слабый тренд — вес урезан"))

LABELS = ((35.0, "Покупать"), (12.0, "Скорее покупать"), (-12.0, "Держать"),
          (-35.0, "Скорее избегать"), (None, "Избегать"))

# Short and fixed on purpose -- longer lists just mean more false positives on
# words used in unrelated senses ("guidance" in a governance headline, "beat"
# in a music-streaming story). Lowercase, checked as plain substrings.
_BULLISH_TERMS = ("beats estimates", "tops estimates", "beat expectations", "surge", "soars",
                   "record high", "upgrade", "outperform", "raises guidance", "raises forecast",
                   "all-time high", "rally", "jumps", "bullish", "strong demand", "buyback")
_BEARISH_TERMS = ("misses estimates", "miss expectations", "plunges", "tumbles", "downgrade",
                   "underperform", "cuts guidance", "cuts forecast", "lawsuit", "investigation",
                   "recall", "layoffs", "bearish", "weak demand", "sinks", "slumps", "warns",
                   "disappoint", "lukewarm", "probe")


def _cutoff() -> str:
    return (dt.date.today() - dt.timedelta(days=RECENCY_DAYS)).isoformat()


def _insider_component(insiders: dict) -> tuple[float, str | None]:
    cutoff = _cutoff()
    buyers = {b[1] for b in insiders["buys"] if (b[0] or "") >= cutoff}
    sellers = {s[1] for s in insiders["sells"] if (s[0] or "") >= cutoff}
    if not buyers and not sellers:
        return 0.0, None
    net = len(buyers) - len(sellers)
    pts = max(-CAP_INSIDER, min(CAP_INSIDER, net * W_INSIDER_PER_NET_BUYER))
    return pts, f"Инсайдеры: {len(buyers)} покупок, {len(sellers)} продаж (год)"


def _political_component(political: list) -> tuple[float, str | None]:
    cutoff = _cutoff()
    recent = [p for p in political if (p[0] or "") >= cutoff]
    if not recent:
        return 0.0, None
    buyers = {p[1] for p in recent if p[2] == "P"}
    sellers = {p[1] for p in recent if p[2] == "S"}
    net = len(buyers) - len(sellers)
    pts = max(-CAP_POLITICAL, min(CAP_POLITICAL, net * W_POLITICAL_PER_NET_BUYER))
    return pts, f"Политики: {len(buyers)} покупок, {len(sellers)} продаж (год)"


def _stakes_component(stakes: list) -> tuple[float, str | None]:
    cutoff = _cutoff()
    recent = [s for s in stakes if (s[0] or "") >= cutoff]
    if not recent:
        return 0.0, None
    pts = 0.0
    activist = sum(1 for s in recent if s[2].startswith("SCHEDULE 13D"))
    passive = sum(1 for s in recent if s[2].startswith("SCHEDULE 13G"))
    pts = min(CAP_STAKES, activist * W_STAKE_ACTIVIST + passive * W_STAKE_PASSIVE)
    return pts, f"Доли 13D/G: {activist} активист., {passive} пассивн. (год)"


def _red_flags_component(annual: dict | None) -> tuple[float, str | None]:
    if not annual or not annual.get("red_flags"):
        return 0.0, None
    n = len(annual["red_flags"])
    labels = ", ".join(f["label"] for f in annual["red_flags"])
    return n * W_RED_FLAG, f"Годовой отчёт: {n} тревожных флаг(ов) — {labels}"


def _financials_component(annual: dict | None) -> tuple[float, str | None]:
    if not annual or not annual.get("financials"):
        return 0.0, None
    fin = annual["financials"]
    metrics = [fin[k]["yoy_pct"] for k in ("revenue", "net_income", "operating_cf")
               if k in fin and fin[k]["yoy_pct"] is not None]
    if not metrics:
        return 0.0, None
    up = sum(1 for v in metrics if v > 0)
    down = sum(1 for v in metrics if v < 0)
    pts = max(-CAP_FIN_TREND, min(CAP_FIN_TREND, (up - down) * W_FIN_TREND_PER_METRIC))
    return pts, f"Финансы SEC: {up}↑/{down}↓ г/г из {len(metrics)}"


def _analyst_component(analyst: dict | None) -> tuple[float, str | None]:
    if not analyst or not analyst.get("consensus"):
        return 0.0, None
    pts = _ANALYST_CONSENSUS_POINTS.get(analyst["consensus"], 0.0)
    note = f"Аналитики: {analyst['consensus']}"
    if analyst.get("implied_upside_pct") is not None and not analyst.get("target_stale"):
        up = analyst["implied_upside_pct"]
        pts += max(-CAP_ANALYST_UPSIDE, min(CAP_ANALYST_UPSIDE, up / 10.0 * W_ANALYST_UPSIDE_PER_10PCT))
        note += f", потенциал {up:+.0f}%"
    return pts, note


def _ownership_component(ownership: dict | None) -> tuple[float, str | None]:
    if not ownership or not ownership.get("top_holders"):
        return 0.0, None
    changes = [h["pct_change"] for h in ownership["top_holders"] if h.get("pct_change") is not None]
    if not changes:
        return 0.0, None
    avg = sum(changes) / len(changes)
    pts = W_OWNERSHIP_TREND if avg > 0 else -W_OWNERSHIP_TREND if avg < 0 else 0.0
    direction = "наращивают" if avg > 0 else "сокращают" if avg < 0 else "без изменений"
    return pts, f"Институционалы: {direction}"


def _momentum_component(prices: dict | None) -> tuple[float, str | None]:
    windows = (prices or {}).get("windows") or []
    scored = [w for w in windows if w.get("excess") is not None]
    if not scored:
        return 0.0, None
    positive = sum(1 for w in scored if w["excess"] > 0)
    negative = sum(1 for w in scored if w["excess"] < 0)
    pts = max(-CAP_MOMENTUM, min(CAP_MOMENTUM, (positive - negative) * W_MOMENTUM_PER_WINDOW))
    return pts, f"Моментум vs SPY: {positive}/{len(scored)} окон"


def _tradingview_component(tv: dict | None) -> tuple[float, str | None]:
    if not tv or tv.get("gauge") is None:
        return 0.0, None
    pts = tv["gauge"] * W_TV_GAUGE
    return pts, f"TradingView: {tv['gauge']:+.2f} ({tv['gauge_label']})"


def _technicals_detail_component(tv: dict | None) -> tuple[float, str | None]:
    """RSI momentum, MACD cross, and price-vs-moving-average trend -- scored
    separately from the aggregate gauge above, since that gauge is a 26-way
    mechanical blend the reader can't decompose. These three are standard,
    named TA signals, individually attributable. ADX scales the total rather
    than scoring its own direction (it has none) -- see _ADX_BANDS."""
    if not tv:
        return 0.0, None
    raw = 0.0
    bits = []
    if tv.get("macd_state") == "MACD выше сигнальной":
        raw += W_MACD
        bits.append("MACD бычий")
    elif tv.get("macd_state") == "MACD ниже сигнальной":
        raw -= W_MACD
        bits.append("MACD медвежий")
    ma_state = tv.get("ma_state")
    if ma_state == "цена выше и 50-, и 200-дневной средней":
        raw += W_MA_TREND
        bits.append("цена выше обеих MA")
    elif ma_state == "цена ниже и 50-, и 200-дневной средней":
        raw -= W_MA_TREND
        bits.append("цена ниже обеих MA")
    rsi = tv.get("rsi")
    if rsi is not None and rsi != 50:
        raw += W_RSI_MOMENTUM if rsi > 50 else -W_RSI_MOMENTUM
        bits.append(f"RSI {rsi:.0f}")
    if not bits:
        return 0.0, None
    adx = tv.get("adx")
    scale, adx_label = next(((s, label) for cut, s, label in _ADX_BANDS
                              if adx is not None and adx >= cut), (1.0, "ADX н/д"))
    pts = max(-CAP_TECHNICALS, min(CAP_TECHNICALS, raw * scale))
    return pts, f"ТА-детали: {', '.join(bits)} · {adx_label}"


def _news_component(news: list) -> tuple[float, str | None]:
    if not news:
        return 0.0, None
    bull = bear = 0
    for item in news:
        title = (item.get("title") or "").lower()
        if any(term in title for term in _BULLISH_TERMS):
            bull += 1
        if any(term in title for term in _BEARISH_TERMS):
            bear += 1
    if bull == 0 and bear == 0:
        return 0.0, None
    net = bull - bear
    pts = max(-CAP_NEWS, min(CAP_NEWS, net * W_NEWS_PER_NET_HEADLINE))
    return pts, f"Новости (keyword): {bull} бычьих, {bear} медвежьих из {len(news)} — грубо"


_COMPONENTS = (
    ("insiders", _insider_component),
    ("political", _political_component),
    ("stakes", _stakes_component),
    ("annual_report", _red_flags_component),
    ("annual_report", _financials_component),
    ("analyst", _analyst_component),
    ("ownership", _ownership_component),
    ("prices", _momentum_component),
    ("tradingview", _tradingview_component),
    ("tradingview", _technicals_detail_component),
    ("news", _news_component),
)


def score(rep: dict) -> dict | None:
    """{'score': float, 'label': str, 'factors': [(points, note), ...]} or None."""
    if rep.get("is_isin"):
        return None
    factors = []
    total = 0.0
    for key, fn in _COMPONENTS:
        pts, note = fn(rep.get(key))
        if note is not None:
            factors.append((round(pts, 1), note))
            total += pts
    if len(factors) < MIN_FACTORS:
        return None
    label = next(l for cut, l in LABELS if cut is None or total >= cut)
    return {"score": round(total, 1), "label": label,
            "factors": sorted(factors, key=lambda f: -abs(f[0]))}


def format_opinion(op: dict | None) -> str:
    if not op:
        return ""
    L = [f"\n💡 ОПИНИОН: {op['label']}  (score {op['score']:+.0f})"]
    for pts, note in op["factors"]:
        L.append(f"  {pts:+5.1f}  {note}")
    L.append("  • Веса — рассуждение, не бэктест")
    L.append("  • Новости выше — грубый keyword-счёт; разбор от Claude придёт отдельным сообщением")
    return "\n".join(L)
