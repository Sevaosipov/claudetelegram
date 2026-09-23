"""Normalised buyer roles, so the strategy can tell a CEO from a board member from a
fund without re-parsing display strings.

Every source writes roles its own way -- SEC free-text officer titles ("President &
CEO", "See Remarks"), BaFin's German categories, Finansinspektionen's Swedish ones,
Oslo none at all -- and the tier rules in strategy.py need one vocabulary:

    ceo, cfo, chair            the top executives rule (b)/(c) look for
    officer, director          other people who run the company
    insider                    an insider of unstated rank (Oslo)
    holder                     a >10% holder with no other role (SEC)
    associate                  a closely associated person / related party
    other                      not a company insider at all (Congress)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

INSIDER_ROLES = {"ceo", "cfo", "chair", "officer", "director", "insider"}
TOP_EXEC_ROLES = {"ceo", "cfo", "chair"}

_CEO_RE = re.compile(r"chief executive|\bceo\b", re.IGNORECASE)
_CFO_RE = re.compile(r"chief financial|\bcfo\b", re.IGNORECASE)
_CHAIR_RE = re.compile(r"\bchair(man|woman|person)?\b", re.IGNORECASE)
_VD_RE = re.compile(r"\bvd\b|verkställande direktör", re.IGNORECASE)


@dataclass
class Buyer:
    name: str
    role: str
    total_eur: float
    increase_pct: float | None = None   # this buyer's buy vs. what they held (SEC only)


def sec_role(title: str | None, is_officer, is_director, is_ten_pct) -> str:
    """CEO outranks chair ("Chairman and CEO" is a CEO); the relationship flags
    decide when the title says nothing specific ("See Remarks", blank)."""
    t = title or ""
    if _CEO_RE.search(t):
        return "ceo"
    if _CFO_RE.search(t):
        return "cfo"
    if _CHAIR_RE.search(t):
        return "chair"
    if is_officer:
        return "officer"
    if is_director:
        return "director"
    if is_ten_pct:
        return "holder"
    return "other"


def bafin_role(position: str | None) -> str:
    p = (position or "").lower()
    if "enger beziehung" in p:
        return "associate"
    if "vorstand" in p:
        return "ceo" if "vorsitz" in p else "officer"
    if "aufsichtsrat" in p:
        return "chair" if "vorsitz" in p else "director"
    return "officer"   # "Sonstige Führungsperson" and anything unlabelled


def sweden_role(position: str | None, related_party) -> str:
    if related_party:
        return "associate"
    p = position or ""
    low = p.lower()
    if "vice vd" not in low and _VD_RE.search(p):
        return "ceo"
    if any(w in low for w in ("finanschef", "finansdirektör", "ekonomichef", "cfo")):
        return "cfo"
    if "styrelseordförande" in low:
        return "chair"
    if "styrelseledamot" in low:
        return "director"
    return "officer"
