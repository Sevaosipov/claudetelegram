"""Predictive-skill evaluation for disclosure-bot's signal features.

Fits a classifier to predict "does this disclosed purchase's stock beat SPY over
the next month" and evaluates it with walk-forward cross-validation. Reports
out-of-sample skill with the sample size on every line.

WHAT THIS IS NOT (binding -- see the spec):
  - No live scoring. Never attaches a probability to a current or future signal,
    never writes signal_journal, never touches bot.py or Telegram.
  - No persisted model. Refit every run, evaluation only. No predict(ticker).
  - Nothing in the project imports this module (there is a test for that).
  - No verdict, no forecast, no target price in the output.

The expected result on this project's data is "no detectable edge" -- a few
hundred rows from ~20 members is not enough to find one. Knowing that is the point.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import math
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

import numpy as np
import pandas as pd

import backtest
import cluster
import db
import marketcap

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "data" / "disclosures.db"

# corpus -> (numeric columns, categorical columns, the one-feature baseline column)
_COLUMNS = {
    "politicians": (
        ["log_amount", "lag_days", "cluster_size", "member_prior_hitrate", "price_vs_spy_63d"],
        ["chamber", "mcap_bucket"],
        "log_amount",
    ),
    "insiders": (
        ["log_value", "is_first_buy", "position_increase_pct", "lag_days",
         "cluster_size", "price_vs_spy_63d"],
        ["role", "mcap_bucket"],
        "log_value",
    ),
}

_CLUSTER_WINDOW = {"politicians": 30, "insiders": 14}
_ID_KEY = {"politicians": "member", "insiders": "owner"}


def _days_between(a: str, b: str) -> int:
    return (dt.date.fromisoformat(a) - dt.date.fromisoformat(b)).days


def _cluster_counts(rows: list[dict], window_days: int, id_key: str) -> list[int]:
    """For each row: distinct *other* id_key values buying the same ticker within
    [D - window_days, D]. Backward-looking -- never counts a later disclosure."""
    by_ticker: dict[str, list[tuple[str, str]]] = {}
    for r in rows:
        by_ticker.setdefault(r["ticker"], []).append((r["disclosure_date"], r[id_key]))
    out = []
    for r in rows:
        d, me = r["disclosure_date"], r[id_key]
        others = {
            who for (when, who) in by_ticker[r["ticker"]]
            if who != me and 0 <= _days_between(d, when) <= window_days
        }
        out.append(len(others))
    return out


def _prior_hitrate(rows: list[dict], horizon: int, id_key: str) -> list[float]:
    """For each row: mean label of earlier same-id rows whose own outcome window
    had settled by D. 'Settled' is approximated as disclosed at least 2*horizon
    calendar days before D -- strictly leak-free. nan when there are none."""
    settle = horizon * 2
    by_id: dict[str, list[tuple[str, int]]] = {}
    for r in rows:
        by_id.setdefault(r[id_key], []).append((r["disclosure_date"], r["label"]))
    out = []
    for r in rows:
        d = r["disclosure_date"]
        prior = [lab for (when, lab) in by_id[r[id_key]] if _days_between(d, when) >= settle]
        out.append(sum(prior) / len(prior) if prior else math.nan)
    return out


def build_feature_frame(conn, rows: list[dict], corpus: str, horizon: int):
    """rows -> (X: DataFrame, y: ndarray, dates: list[str]). Column order is
    exactly _COLUMNS[corpus] numeric then categorical."""
    numeric, categorical, _ = _COLUMNS[corpus]
    window = _CLUSTER_WINDOW[corpus]
    id_key = _ID_KEY[corpus]

    cluster_size = _cluster_counts(rows, window, id_key)
    prior_hr = _prior_hitrate(rows, horizon, id_key) if "member_prior_hitrate" in numeric else None

    records = []
    for i, r in enumerate(rows):
        d = r["disclosure_date"]
        feat = {
            "lag_days": _days_between(d, r["trade_date"]),   # both corpora carry trade_date
            "cluster_size": cluster_size[i],
            "price_vs_spy_63d": backtest._return_before(r["ticker"], d, 63),
            "mcap_bucket": marketcap.size_bucket(marketcap.market_cap_eur(conn, r["ticker"])),
        }
        if corpus == "politicians":
            feat["log_amount"] = math.log10(cluster.parse_amount_low(r["amount_range"]) + 1)
            feat["chamber"] = r["chamber"]
            feat["member_prior_hitrate"] = prior_hr[i]
        else:
            feat["log_value"] = math.log10((r.get("value") or 0) + 1)
            feat["role"] = r["role"]
            feat["is_first_buy"] = float(
                cluster._is_first_buy(conn, r["ticker"], [r["owner"]], d))
            feat["position_increase_pct"] = cluster._position_increase(
                {"shares": r.get("shares"), "owned_after": r.get("owned_after")})
        records.append(feat)

    X = pd.DataFrame(records, columns=numeric + categorical)
    y = np.array([r["label"] for r in rows], dtype=int)
    dates = [r["disclosure_date"] for r in rows]
    return X, y, dates
