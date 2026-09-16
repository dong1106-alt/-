#!/usr/bin/env python3
"""Shared causal execution and walk-forward rules for strategy evaluation."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable, Sequence

from scripts.trading_rules import calc_trade_cost

STRICT_EVALUATION_START = "2023-01-01"


@dataclass(frozen=True)
class WalkForwardFold:
    train_start: object
    train_end: object
    validation_start: object
    validation_end: object

    def to_dict(self) -> dict:
        return asdict(self)


def _ordered_unique(values: Iterable[object]) -> list[object]:
    return sorted(dict.fromkeys(values))


def build_walk_forward_folds(
    dates: Sequence[object],
    *,
    min_train: int = 252,
    purge: int = 20,
    validation: int = 63,
    embargo: int = 5,
    holdout: int = 126,
    min_folds: int = 3,
) -> tuple[list[WalkForwardFold], list[object]]:
    """Build expanding folds and reserve the newest dates as a sealed holdout."""
    ordered = _ordered_unique(dates)
    required = min_train + purge + min_folds * validation + (min_folds - 1) * embargo + holdout
    if len(ordered) < required:
        raise ValueError(f"insufficient dates: need {required}, got {len(ordered)}")

    development = ordered[:-holdout]
    sealed = ordered[-holdout:]
    folds: list[WalkForwardFold] = []
    val_start = min_train + purge
    while val_start + validation <= len(development):
        train_end = val_start - purge - 1
        folds.append(WalkForwardFold(
            train_start=development[0],
            train_end=development[train_end],
            validation_start=development[val_start],
            validation_end=development[val_start + validation - 1],
        ))
        val_start += validation + embargo

    if len(folds) < min_folds:
        raise ValueError(f"insufficient complete folds: need {min_folds}, got {len(folds)}")
    return folds, sealed


def stop_fill_price(open_price: float, low_price: float, stop_price: float) -> float | None:
    """Long stop: gap through the stop fills at open, otherwise at the stop."""
    if open_price <= stop_price:
        return float(open_price)
    if low_price <= stop_price:
        return float(stop_price)
    return None


def is_tradable_bar(open_price, volume) -> bool:
    try:
        return float(open_price) > 0 and float(volume) > 0
    except (TypeError, ValueError):
        return False


def first_tradable_after(rows: Sequence[dict], signal_date):
    """Return the first executable bar after a signal, or None at data end."""
    return next((row for row in rows
                 if row.get("date") > signal_date
                 and is_tradable_bar(row.get("open"), row.get("volume"))), None)


def execute_fill(*, side: str, signal_date, fill_date, fill_source: str,
                 open_price: float, shares: int, trade_cfg: dict,
                 market_cap: float, low_price: float | None = None,
                 stop_price: float | None = None):
    """Price and charge one causal order; return trade-cost tuple and audit record."""
    if fill_date <= signal_date or shares <= 0:
        raise ValueError("order must fill after its signal with positive shares")
    if fill_source == "next_open":
        raw_price = open_price
    elif fill_source in {"gap_stop", "intraday_stop"} and low_price is not None and stop_price is not None:
        raw_price = stop_fill_price(open_price, low_price, stop_price)
        expected = "gap_stop" if open_price <= stop_price else "intraday_stop"
        if raw_price is None or fill_source != expected:
            raise ValueError("stop order did not trigger at the claimed price")
    else:
        raise ValueError("invalid fill source or missing stop bar")
    cost = calc_trade_cost(raw_price, shares, side, trade_cfg, market_cap)
    record = {
        "side": side, "signal_date": signal_date, "fill_date": fill_date,
        "fill_price": cost[0], "bar_open": float(open_price),
        "fill_source": fill_source, "raw_price": float(raw_price),
    }
    return cost, record


def assert_causal_fills(records: Iterable[dict]) -> None:
    """Reject same-bar fills and prices not sourced from the execution bar."""
    for record in records:
        signal_date = record.get("signal_date")
        fill_date = record.get("fill_date")
        if signal_date is None or fill_date is None or fill_date <= signal_date:
            raise AssertionError(f"non-causal fill: {record}")
        if record.get("fill_source") not in {"next_open", "intraday_stop", "gap_stop"}:
            raise AssertionError(f"unknown fill source: {record}")
        if record.get("fill_source") == "next_open" and "raw_price" in record:
            if record["raw_price"] != record["bar_open"]:
                raise AssertionError(f"non-open execution: {record}")
