#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回测与模拟盘共用的交易口径（费用、滑点、止损）。"""
from pathlib import Path

DEFAULT_TRADE_COST = {
    "commission_rate": 0.0003,
    "min_commission": 5.0,
    "stamp_tax": 0.001,
    "market_cap_split": 100.0,
    "slip_small": 0.001,
    "slip_big": 0.002,
}


def load_trade_cost(config_path):
    cfg = dict(DEFAULT_TRADE_COST)
    try:
        import yaml
        raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
        cfg.update({k: float(v) for k, v in (raw.get("trade_cost") or {}).items() if k in cfg})
    except Exception:
        pass
    return cfg


def estimate_market_cap(price):
    if price < 5:
        return 30.0
    if price < 10:
        return 80.0
    if price < 20:
        return 150.0
    if price < 50:
        return 300.0
    if price < 100:
        return 500.0
    return 800.0


def calc_trade_cost(price, shares, side, trade_cfg=None, market_cap=None):
    """返回 (成交价, 现金总额, 佣金, 印花税)。买入总额为支出，卖出总额为净收入。"""
    cfg = {**DEFAULT_TRADE_COST, **(trade_cfg or {})}
    value = float(price) * int(shares)
    commission = max(value * cfg["commission_rate"], cfg["min_commission"])
    tax = value * cfg["stamp_tax"] if side == "sell" else 0.0
    mc = estimate_market_cap(price) if market_cap is None else market_cap
    slip = cfg["slip_small"] if mc < cfg["market_cap_split"] else cfg["slip_big"]
    if side == "buy":
        execution_price = price * (1 + slip)
        cash_total = execution_price * shares + commission
    else:
        execution_price = price * (1 - slip)
        cash_total = execution_price * shares - commission - tax
    return round(execution_price, 6), round(cash_total, 2), round(commission, 2), round(tax, 2)


def bounded_stop_loss(price, raw_stop, max_stop_pct=0.0):
    price = float(price)
    stop = float(raw_stop)
    if max_stop_pct > 0 and price > 0:
        stop = max(stop, price * (1.0 - float(max_stop_pct)))
    return round(stop, 2)
