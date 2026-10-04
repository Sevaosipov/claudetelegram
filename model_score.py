"""model_score.py: the scoring strategy, as pure functions.

Every stock and coin gets a score from 0 to 100; the score decides whether it is worth a buy
signal, and the stock's or coin's own volatility decides the stop. Nothing here touches the
network or the database: signals, price closes (oldest first) and headlines come in as
arguments, so the whole strategy is testable on hand-built data and model.py can feed it
anything (spec 2026-09-30, sections 1-3; the position sizing it once had went with the virtual
portfolio, spec 2026-10-04-remove-model-portfolio.md).

The stock score is insiders + triggers + momentum + news, each part capped so that no
single ingredient can buy on its own -- an activist 13D alone (30-50) or politicians
alone (5) never reach the bar; three managers with a CEO buying a real stake do.
Each part returns its points together with the Russian lines that explain them, which
end up in the Telegram message and the menu.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from cluster.roles import INSIDER_ROLES

STOCK_BUY = 60.0
STOCK_WATCH = 45.0
STAKE_CONTROL_PERCENT = 50.0  # a stake this big controls the company: a takeover, not a signal
STAKE_MIN_GROWTH_PP = 1.0     # an amendment says something only when the stake grew this much
COIN_BUY = 60.0
MIN_MCAP_EUR = 20e6
MIN_ADV_EUR = 100_000
MIN_CLOSES = 21               # 20 daily changes: the least history a stop can be sized from
STOP_MULT = 3.0               # stop distance = this many typical daily moves ...
STOP_MIN = {"stock": 0.10, "crypto": 0.15}   # ... never tighter than the ordinary noise
STOP_MAX = {"stock": 0.25, "crypto": 0.35}   # ... never so wide the loss is unbounded
BUY, WATCH, SKIP, BLOCK = "buy", "watch", "skip", "block"

INSIDERS_CAP = 60
TRIGGERS_CAP = 20
MOMENTUM_CAP = 15
NEWS_MIN, NEWS_MAX = -30, 10
NEWS_NEGATIVE_POINTS, NEWS_POSITIVE_POINTS = -10, 5
COIN_TREND_STEP = 15          # above the 100-day average, and each positive 20/60/120-day return
COIN_BULLISH_FLOW, COIN_CAUTION = 15, -20
# An alt (any coin but BTC and ETH) is bought only while bitcoin itself is above its 100-day average.
ALT_GATE_REASON = "биткоин ниже 100-дн. средней — альты не покупаем"

# How many management buyers earn what; the last entry is "that many or more".
_COUNT_POINTS = {1: 22, 2: 34, 3: 42, 4: 46}
_POLITICIAN_SOURCES = ("HOUSE", "SENATE")
_STAKE_SOURCE = "SEC13DG"

# The phrase tuples are the source of truth; _phrase_regex turns them into patterns.
# A red flag blocks the buy outright (and closes a position); the others only move the
# score. Matching is against the lower-cased headline, from a word boundary on, so
# "asphalts" is not "halts" and "ETHGlobal hackathon" is not a hack.
RED_FLAGS = ("fraud", "sec investigation", "subpoena", "going concern", "bankruptcy",
             "chapter 11", "restatement", "delisting", "delisted",
             "public offering", "secondary offering", "private placement", "at-the-market")
COIN_RED_FLAGS = ("hack", "exploit", "stolen", "sec lawsuit")
NEGATIVE = ("downgrade", "cuts guidance", "cuts forecast", "misses estimates", "lawsuit",
            "probe", "recall", "resigns", "halts", "short seller")
POSITIVE = ("upgrade", "raises guidance", "raises forecast", "beats estimates", "buyback",
            "record revenue", "wins contract", "fda approval")

# Most phrases are open-ended on the right, so "fraudulent", "probes" and "downgraded"
# match. The short coin words are not: "hack" and "exploit" are also the start of
# "hackathon" and "exploitation", so they may only continue with these inflections.
# Known limitation: "miners exploit cheap power" still reads as an exploit.
_CLOSED_INFLECTIONS = {"hack": "s|ed|er|ers|ing", "exploit": "s|ed|ing"}
# "initial public offering" is a new listing, not this company diluting its holders; an
# "anti-fraud" product is not fraud.
_NOT_PRECEDED_BY = {"public offering": "initial ", "fraud": "anti-"}


def _phrase_regex(phrase: str) -> str:
    pattern = r"\b"
    if phrase in _NOT_PRECEDED_BY:
        pattern += f"(?<!{re.escape(_NOT_PRECEDED_BY[phrase])})"
    pattern += re.escape(phrase)
    if phrase in _CLOSED_INFLECTIONS:
        pattern += f"(?:{_CLOSED_INFLECTIONS[phrase]})?" + r"\b"
    return pattern


def _compile(phrases: tuple[str, ...]) -> re.Pattern:
    return re.compile("|".join(_phrase_regex(p) for p in phrases))


_RED_RE = _compile(RED_FLAGS)
_COIN_RED_RE = _compile(RED_FLAGS + COIN_RED_FLAGS)
_NEGATIVE_RE = _compile(NEGATIVE)
_POSITIVE_RE = _compile(POSITIVE)


@dataclass
class Part:
    points: float
    lines: list[str]


@dataclass
class StockScore:
    ticker: str
    source: str
    company: str
    signal: object
    insiders: float
    triggers: float
    momentum: float
    news: float
    total: float
    decision: str            # BUY / WATCH / SKIP / BLOCK
    reasons: list[str]       # the parts' lines, insiders first
    block: str | None        # the red-flag headline, when decision == BLOCK
    untradeable: str | None  # why a 60+ score is only WATCH, else None
    t212: bool | None        # False -> label «нет на T212»; None -> not checked
    stop_pct: float | None
    last_close: float | None
    kind: str = "stock"


@dataclass
class CoinScore:
    coin: str                # "BTC"
    ticker: str              # "CRYPTO:BTC"
    trend: float
    flows: float
    news: float
    total: float
    trend_up: bool
    trend_down: bool
    caution: str | None
    block: str | None        # the red-flag headline, else the caution, when decision == BLOCK
    decision: str
    reasons: list[str]
    stop_pct: float | None
    last_close: float | None
    kind: str = "crypto"


# ---------------------------------------------------------------- small helpers
def _num(x: float, digits: int) -> str:
    """Russian number: a comma decimal separator."""
    return f"{x:.{digits}f}".replace(".", ",")


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def _is_activist(sig) -> bool:
    flag = getattr(sig, "is_activist", None)
    if flag is not None:
        return bool(flag)
    return str(getattr(sig, "form_type", "")).startswith("SCHEDULE 13D")


# ---------------------------------------------------------------- stock parts
def insider_part(sig) -> Part:
    """Who bought and how convincingly (0-60): the number of management buyers, a CEO/CFO
    (or Chair) among them, the size against the company, how much the biggest buyer
    added to what they held, and a first-ever purchase."""
    if getattr(sig, "source", None) in _POLITICIAN_SOURCES:
        return Part(0, [])   # politicians count under triggers
    if getattr(sig, "holder_only", False):
        return Part(15, ["только крупные акционеры (>10%)"])

    buyers = getattr(sig, "buyers", None)
    if buyers:
        mgmt = [b for b in buyers if b.role in INSIDER_ROLES]
        count = len(mgmt)
        roles = {b.role for b in mgmt}
    else:   # no per-buyer detail: trust the count, know nothing about who they are
        count = getattr(sig, "buyer_count", 0) or 0
        roles = set()

    points, lines = 0.0, []
    if count:
        points += _COUNT_POINTS[min(count, 4)]
        word = _plural(count, "инсайдер", "инсайдера", "инсайдеров")
        lines.append(f"{count} {word} из руководства")
    if roles & {"ceo", "cfo"}:
        points += 10
        lines.append(f"{'CEO' if 'ceo' in roles else 'CFO'} среди покупателей")
    elif "chair" in roles:
        points += 5
        lines.append("председатель совета среди покупателей")

    pct = getattr(sig, "value_pct_of_mcap", None)      # a percent: 0.05 is 0.05%
    if pct is not None and pct >= 0.01:
        points += 10 if pct >= 0.2 else 6 if pct >= 0.05 else 3
        lines.append(f"{_num(pct, 2)}% компании")

    inc = getattr(sig, "position_increase_pct", None)
    if inc is not None and inc >= 10:
        points += 6 if inc >= 30 else 3
        lines.append(f"позиция +{inc:.0f}%")

    if getattr(sig, "first_buy", False):
        points += 4
        lines.append("первая покупка")
    return Part(min(points, INSIDERS_CAP), lines)


def is_amendment(sig) -> bool:
    """A 13D/A or 13G/A: an update of a stake already on file."""
    return "/A" in str(getattr(sig, "form_type", "") or "").upper()


def _grew(sig) -> bool:
    prev = getattr(sig, "prev_percent", None)
    return prev is not None and round(sig.percent - prev, 6) >= STAKE_MIN_GROWTH_PP


def stake_part(sig) -> Part:
    """A 13D/G stake replaces the insiders part: the bigger the stake the more it says,
    and an activist (13D) says far more than a passive fund (13G). Two kinds say nothing
    (spec section 1, as calibrated on the first live run): a controlling stake of
    STAKE_CONTROL_PERCENT or more -- a takeover or a parent company, not someone betting on
    the price -- and an amendment that doesn't show the stake up by at least a point (most
    are routine updates by holders who have owned their stake for years)."""
    if sig.percent >= STAKE_CONTROL_PERCENT:
        return Part(0, ["контрольный пакет — не сигнал"])
    if is_amendment(sig) and not _grew(sig):
        return Part(0, ["поправка без роста доли"])
    over = min(max(sig.percent - 5, 0), 10)
    if _is_activist(sig):
        points, lines = 30 + 2 * over, [f"активист 13D: {_num(sig.percent, 1)}%"]
    else:
        points, lines = 15 + over, [f"13G: {_num(sig.percent, 1)}%"]
    if _grew(sig):
        points += 5
        lines.append(f"доля +{_num(sig.percent - sig.prev_percent, 1)} п.п.")
    return Part(min(points, INSIDERS_CAP), lines)


def trigger_part(sig, *, activist: bool, passive_big: bool, politicians: bool) -> Part:
    """What else is going on around the same ticker (0-20). The caller says whether an
    activist 13D / a 10%+ 13G / politicians are buying it; a source that is already
    counted by one of those doesn't also corroborate."""
    points, lines = 0.0, []
    counted: set[str] = set()
    if activist:
        points += 15
        lines.append("рядом активист 13D")
        counted.add(_STAKE_SOURCE)
    if passive_big:
        points += 6
        lines.append("рядом 13G от 10%")
        counted.add(_STAKE_SOURCE)
    if politicians:
        points += 5
        lines.append("покупают политики")
        counted.update(_POLITICIAN_SOURCES)
    extra = [s for s in (getattr(sig, "corroborated_by", None) or []) if s not in counted]
    if extra:
        points += 5
        lines.append(f"подтверждает: {', '.join(extra)}")
    return Part(min(points, TRIGGERS_CAP), lines)


def momentum_part(closes: list[float]) -> Part:
    """Is the price already going the right way (0-15)? Closes are oldest first. A window
    the history doesn't cover scores nothing for that line; momentum never blocks a buy."""
    n = len(closes)
    points, lines = 0.0, []
    if n >= 127:
        ret6 = closes[-1] / closes[-127] - 1
        if ret6 > 0:
            points += 8 if ret6 > 0.25 else 5
            lines.append(f"6 мес. {ret6 * 100:+.0f}%")
    if n >= 200 and closes[-1] > _mean(closes[-200:]):
        points += 5
        lines.append("выше 200-дн. средней")
    if n >= 22:
        ret1 = closes[-1] / closes[-22] - 1
        if ret1 > 0:
            points += 2
            lines.append(f"1 мес. {ret1 * 100:+.0f}%")
    return Part(min(points, MOMENTUM_CAP), lines)


def news_part(headlines: list[dict] | None, *, coin: bool = False) -> tuple[Part, str | None]:
    """Score the headlines (-30..+10) and return the first red-flag title, if any.
    A title counts once per list and a red-flag title is not also a negative one."""
    if not headlines:
        return Part(0, []), None
    red_re = _COIN_RED_RE if coin else _RED_RE
    red: str | None = None
    bad = good = 0
    for item in headlines:
        title = item.get("title") if isinstance(item, dict) else None
        if not title:
            continue
        low = title.lower()
        if red_re.search(low):
            red = red or title
            continue
        if _NEGATIVE_RE.search(low):
            bad += 1
        if _POSITIVE_RE.search(low):
            good += 1
    points = max(NEWS_MIN, min(NEWS_MAX, bad * NEWS_NEGATIVE_POINTS + good * NEWS_POSITIVE_POINTS))
    lines = []
    if bad or good:
        counts = []
        if bad:
            counts.append(f"{bad} {_plural(bad, 'плохая', 'плохие', 'плохих')}")
        if good:
            counts.append(f"{good} {_plural(good, 'хорошая', 'хорошие', 'хороших')}")
        lines.append(f"новости: {', '.join(counts)}")
    return Part(points, lines), red


# ---------------------------------------------------------------- volatility
def typical_move(closes: list[float]) -> float | None:
    """The mean absolute daily change over the last 20 changes."""
    if len(closes) < MIN_CLOSES:
        return None
    tail = closes[-MIN_CLOSES:]
    return _mean([abs(tail[i] / tail[i - 1] - 1) for i in range(1, len(tail))])


def stop_distance(closes: list[float], kind: str) -> float | None:
    """How far below its high a position may fall before it is sold: three typical daily
    moves, kept between the floor and the cap of its kind."""
    move = typical_move(closes)
    if move is None:
        return None
    return min(max(STOP_MULT * move, STOP_MIN[kind]), STOP_MAX[kind])


def untradeable_reason(sig, closes: list[float]) -> str | None:
    """Why a stock can't be bought even if it scores well, or None when it can."""
    if len(closes) < MIN_CLOSES:
        return "нет цены"
    cap = getattr(sig, "market_cap_eur", None)
    adv = getattr(sig, "avg_daily_value", None)
    if cap is None and adv is None:
        return "размер неизвестен"
    if cap is not None and cap < MIN_MCAP_EUR:
        return "компания меньше €20 млн"
    if adv is not None and adv < MIN_ADV_EUR:
        return "торгуется меньше €100 тыс. в день"
    return None


# ---------------------------------------------------------------- stocks
def score_stock(sig, closes: list[float], headlines: list[dict] | None, *,
                activist: bool = False, passive_big: bool = False,
                politicians: bool = False, t212: bool | None = None) -> StockScore:
    """Score one stock signal. `activist`/`passive_big`/`politicians` say what else is
    buying the same ticker; a signal that is itself a stake or a politician's purchase
    can't be its own trigger, so they are adjusted here rather than by every caller."""
    is_stake = hasattr(sig, "percent")
    if is_stake:
        activist = passive_big = False
    if getattr(sig, "source", None) in _POLITICIAN_SOURCES:
        politicians = True

    insiders = stake_part(sig) if is_stake else insider_part(sig)
    triggers = trigger_part(sig, activist=activist, passive_big=passive_big,
                            politicians=politicians)
    momentum = momentum_part(closes)
    news, red = news_part(headlines)

    total = max(0.0, min(100.0, insiders.points + triggers.points + momentum.points + news.points))
    block = untradeable = None
    if red:
        decision, block = BLOCK, red
    elif total >= STOCK_BUY:
        untradeable = untradeable_reason(sig, closes)
        decision = WATCH if untradeable else BUY
    elif total >= STOCK_WATCH:
        decision = WATCH
    else:
        decision = SKIP

    return StockScore(
        ticker=sig.ticker, source=sig.source, company=sig.company, signal=sig,
        insiders=insiders.points, triggers=triggers.points, momentum=momentum.points,
        news=news.points, total=total, decision=decision,
        reasons=insiders.lines + triggers.lines + momentum.lines + news.lines,
        block=block, untradeable=untradeable, t212=t212,
        stop_pct=stop_distance(closes, "stock"),
        last_close=closes[-1] if closes else None)


# ---------------------------------------------------------------- coins
def coin_trend(closes: list[float]) -> dict | None:
    """The coin's trend from 121+ daily closes (oldest first), or None with less."""
    if len(closes) < 121:
        return None
    last = closes[-1]
    above = last > _mean(closes[-100:])
    rets = {f"ret{d}": last / closes[-d - 1] - 1 for d in (20, 60, 120)}
    return {
        "above_ma100": above,
        **rets,
        "up": above and sum(r > 0 for r in rets.values()) >= 2,
        "down": not above and rets["ret20"] < 0,
    }


def score_coin(coin: str, closes: list[float], *, bullish_flow: bool, caution: str | None,
               headlines: list[dict] | None, btc_up: bool | None = None) -> CoinScore:
    """Score a coin: trend (0-60), flows (-20..+15) and news (-30..+10). A coin buys
    only in an uptrend; a red-flag headline or a price-confirmed caution blocks it.

    `btc_up` is the bitcoin regime filter for an alt: False -- bitcoin's close is not above its
    100-day average (coin_trend(...)["above_ma100"]) -- turns a would-be BUY into WATCH, with
    ALT_GATE_REASON among the reasons; the score itself is not touched. None -- BTC and ETH, or
    any caller with no regime to apply -- is not gated, and neither is True. A BLOCK stays a
    block, and a coin that would not buy anyway (no uptrend, under the bar, too little history)
    has nothing for the gate to stop."""
    trend = coin_trend(closes)
    news, red = news_part(headlines, coin=True)

    reasons: list[str] = []
    trend_pts = 0
    if trend is None:
        reasons.append("мало истории")
    else:
        if trend["above_ma100"]:
            trend_pts += COIN_TREND_STEP
            reasons.append("выше 100-дн. средней")
        else:
            reasons.append("ниже 100-дн. средней")
        for days in (20, 60, 120):
            ret = trend[f"ret{days}"]
            if ret > 0:
                trend_pts += COIN_TREND_STEP
                reasons.append(f"{days} дн. {ret * 100:+.0f}%")

    flows = (COIN_BULLISH_FLOW if bullish_flow else 0) + (COIN_CAUTION if caution else 0)
    if bullish_flow:
        reasons.append("покупают крупные игроки")
    if caution:
        reasons.append(f"осторожно: {caution}")
    reasons += news.lines
    if red:
        reasons.append(f"новости: {red}")

    total = max(0.0, min(100.0, trend_pts + flows + news.points))
    trend_up = bool(trend and trend["up"])
    block = red or caution
    if block:
        decision = BLOCK
    elif trend_up and total >= COIN_BUY:
        decision = BUY
        if btc_up is False:
            decision = WATCH
            reasons.append(ALT_GATE_REASON)
    else:
        decision = WATCH

    return CoinScore(
        coin=coin, ticker=f"CRYPTO:{coin}", trend=trend_pts, flows=flows, news=news.points,
        total=total, trend_up=trend_up, trend_down=bool(trend and trend["down"]),
        caution=caution, block=block, decision=decision, reasons=reasons,
        stop_pct=stop_distance(closes, "crypto"),
        last_close=closes[-1] if closes else None)
