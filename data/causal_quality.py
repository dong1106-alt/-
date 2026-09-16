#!/usr/bin/env python3
"""因果预评分：只用截至当日的滚动特征，禁止样本内模拟盈亏和向后看突破。"""
import numpy as np
import pandas as pd


def _r2(closes):
    n_pts = len(closes)
    if n_pts < 10:
        return 0.0
    x = np.arange(n_pts)
    x_mean = x.mean()
    y_mean = closes.mean()
    ss_xy = np.sum((x - x_mean) * (closes - y_mean))
    ss_xx = np.sum((x - x_mean) ** 2)
    ss_yy = np.sum((closes - y_mean) ** 2)
    if ss_xx <= 0 or ss_yy <= 0:
        return 0.0
    return float((ss_xy ** 2) / (ss_xx * ss_yy))


def score_stock(df: pd.DataFrame, st_cfg=None):
    """返回 (score, detail)。df 必须已是截至决策日的历史，不得含未来行。"""
    st_cfg = st_cfg or {}
    if len(df) < 30:
        return 0.0, {}

    recent = df.copy()
    recent["trend_deviation"] = recent["close"] / recent["ma60"] - 1
    recent["trend_stability"] = (1 - recent["trend_deviation"].rolling(20).std() * 10).clip(0, 1)
    recent["trend_strength_score"] = (recent["close"] / recent["ma60"] - 1).clip(-1, 1) * 0.5 + 0.5
    recent["above_ma20_ratio"] = (recent["close"] > recent["ma20"]).rolling(20).mean()
    last = recent.iloc[-1]

    def _f(v):
        try:
            x = float(v)
            return x if np.isfinite(x) else 0.0
        except (TypeError, ValueError):
            return 0.0

    base_score = (
        _f(last["trend_stability"]) * 0.30
        + _f(last["trend_strength_score"]) * 0.30
        + _f(last["above_ma20_ratio"]) * 0.20
    ) * 100

    r_squared = _r2(recent["close"].iloc[-60:].values)
    r2_score = r_squared * 100
    score = base_score * 0.7 + r2_score * 0.30
    if r_squared < 0.3:
        score = min(score, 35.0)

    x1 = recent["low"].shift(1)
    abs_diff = (recent["low"] - x1).abs()
    pos_diff = (recent["low"] - x1).clip(lower=0)
    sma_abs = abs_diff.ewm(alpha=1 / 3, adjust=False).mean()
    sma_pos = pos_diff.ewm(alpha=1 / 3, adjust=False).mean()
    x2 = pd.Series(np.where(sma_pos > 1e-10, sma_abs / sma_pos * 100, 9999.0), index=recent.index)
    x3 = (x2 * 10).ewm(alpha=2 / 4, adjust=False).mean()
    x4 = recent["low"].rolling(38).min()
    x5 = x3.rolling(38).max()
    accum_raw = pd.Series(0.0, index=recent.index)
    new_low_mask = recent["low"] <= x4
    accum_raw[new_low_mask] = (x3[new_low_mask] + x5[new_low_mask] * 2) / 2
    x7 = accum_raw.ewm(alpha=2 / 4, adjust=False).mean() / 618
    x7_last = float(x7.iloc[-1]) if len(x7) and np.isfinite(x7.iloc[-1]) else 0.0
    dip_on = x7_last >= 1
    dip_score = min(x7_last * 5.0, 15.0) if dip_on else 0.0
    score += dip_score

    if "valuation_percentile" in recent.columns:
        val_pct = recent["valuation_percentile"].iloc[-1]
        is_extreme = (val_pct is not None) and (not pd.isna(val_pct)) and (val_pct < 0.10)
        price_percentile = float(val_pct) if val_pct is not None and not pd.isna(val_pct) else 0.5
    else:
        lookback = min(250, len(recent))
        window = recent["close"].iloc[-lookback:]
        lo, hi = float(window.min()), float(window.max())
        current = float(recent["close"].iloc[-1])
        price_percentile = 0.5 if hi == lo else (current - lo) / (hi - lo)
        is_extreme = price_percentile < 0.10
    exemption_bonus = 0.0
    if st_cfg.get("extreme_value_exemption", True) and is_extreme:
        exemption_bonus = 15.0
        score += exemption_bonus

    if "ma5" not in recent.columns:
        recent["ma5"] = recent["close"].rolling(5).mean()
    tail = recent.tail(20)
    deviation = abs(tail["close"] / tail["ma60"] - 1).mean()
    chip_bonus = 0.0
    if deviation < 0.05 and recent["ma5"].iloc[-1] > recent["ma20"].iloc[-1] > recent["ma60"].iloc[-1]:
        chip_bonus = 5.0
        score += chip_bonus

    detail = {
        "base_score": round(float(base_score), 1),
        "r_squared": round(float(r_squared), 3),
        "sim_trades": 0,
        "sim_win_rate": 0.0,
        "dip_count": int(dip_on),
        "dip_score": round(float(dip_score), 1),
        "valuation_percentile": round(float(price_percentile), 3),
        "is_extreme_value": bool(is_extreme),
        "exemption_bonus": float(exemption_bonus),
        "chip_bonus": float(chip_bonus),
        "final_score": round(float(score), 1),
    }
    return round(float(score), 1), detail
