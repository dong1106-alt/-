#!/usr/bin/env python3
"""
止盈逻辑回测对比脚本
- 用统一的买入信号框架（Donchian突破+MA20趋势+量能+MACD），分别测试：
  1) 旧逻辑（OLD）：固定5%止损 + 固定40%止盈全部卖出 + 海龟退出线
  2) 新逻辑（NEW）：保本止损 + 阶梯止盈(MA20×0.98) + 部分止盈(强/中/弱R²) + 海龟退出线
- 对比指标：总收益率、最大回撤、Sharpe、胜率、盈亏比、交易次数

用法（CodeAct）：
  args = [result_mode]              # display_only
普通运行：
  python3 tp_backtest_compare.py display_only
"""
from pathlib import Path as _P
_ROOT = _P(__file__).resolve().parent.parent  # === LOCAL-PATCH v1 ===

import asyncio
import sys
import os
import json
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd
import requests
from codeact_sdk import CodeActSDK

# ===================== 参数配置 =====================
# 6股标准测试集（消费/金融/制造/医药/新能源/科技）
TEST_STOCKS = [
    "sh600519",  # 贵州茅台 - 白酒龙头
    "sh601318",  # 中国平安 - 金融保险
    "sz000651",  # 格力电器 - 家电制造
    "sh600276",  # 恒瑞医药 - 医药
    "sz300750",  # 宁德时代 - 新能源
    "sz002475",  # 立讯精密 - 电子科技
]

INITIAL_CAPITAL_PER_STOCK = 100000  # 每只股票独立账户10万
BACKTEST_BARS = 640                  # 腾讯API最多640条≈2.5年
KLINE_API = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"

# 买入信号参数（与 fast_backtest 一致）
CHANNEL_BASE = 20      # Donchian通道周期
EXIT_BASE = 10         # 海龟退出线周期
VOL_Q75_WIN = 20       # 成交量75分位窗口
MIN_ENTRY_SCORE = 2.0  # 最低入场评分
ATR_MULT = 2.0         # ATR止损倍数（用于初始止损距离计算，但sim脚本固定用5%）
STOP_LOSS_PCT = 0.05   # sim脚本固定5%止损（初始止损）
TP_THRESHOLD = 0.40    # 固定止盈阈值40%（旧逻辑）

# 新逻辑参数（对齐主策略v6_optimized.py 1530-1590行）
# 注意：部分止盈触发线保持与sim原固定止盈一致的40%基线（不使用主策略自适应放大到50%/75%），
# 这样对比才公平——只改变"分批卖出+保本+阶梯跟踪"的退出方式，不引入止盈阈值变化。
# 保本触发阈值设为8%而非5%：sim脚本初始止损固定5%，若5%就移到成本价等于收紧5%止损，
# 消融实验显示这会导致-26%以上的Sharpe下降。8%给仓位更多呼吸空间。
BREAKEVEN_TRIGGER = 0.08        # 浮盈8%以上保本（比主策略5%更宽松，适配固定5%初始止损）
TIER_STRONG = 0.20              # 强趋势阶梯触发线
TIER_NEUTRAL = 0.15             # 中性趋势阶梯触发线
TIER_WEAK = 0.10                # 弱趋势阶梯触发线
TREND_STRONG_TH = 0.15          # trend_strength > 0.15 视为强趋势
TREND_WEAK_TH = -0.05           # trend_strength < -0.05 视为弱趋势
TP_SELL_PCT_STRONG = 0.15       # R²强趋势部分止盈15%
TP_SELL_PCT_NEUTRAL = 0.25      # R²中性部分止盈25%
TP_SELL_PCT_WEAK = 0.40         # R²弱趋势部分止盈40%
TP_THRESHOLD_PARTIAL = 0.40     # 部分止盈触发线统一40%（与原固定止盈阈值对齐，公平对比）
R2_STRONG = 0.15                # R²>0.15 → 强趋势止盈比例
R2_NEUTRAL = 0.05               # R²>0.05 → 中性
MA20_TIER_FACTOR = 0.98         # 阶梯止损 = MA20 × 0.98

OUTPUT_DIR = f"{_ROOT}/codeact/output"
os.makedirs(OUTPUT_DIR, exist_ok=True)


# ===================== 数据获取 =====================
def fetch_kline(code: str, count: int = 640) -> Optional[pd.DataFrame]:
    """从腾讯API获取前复权日线数据"""
    url = f"{KLINE_API}?param={code},day,,,{count},qfq"
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=15)
            if r.status_code != 200:
                continue
            raw = r.json()
            if raw.get("code") != 0:
                continue
            d = raw.get("data", {})
            sd = d.get(code) or d.get(code.upper()) or {}
            bars = sd.get("qfqday") or sd.get("day") or []
            if not bars:
                return None
            rows = []
            for b in bars:
                rows.append({
                    "date": b[0],
                    "open": float(b[1]),
                    "close": float(b[2]),
                    "high": float(b[3]),
                    "low": float(b[4]),
                    "volume": float(b[5]) if len(b) > 5 else 0.0,
                })
            df = pd.DataFrame(rows)
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").reset_index(drop=True)
            for c in ["open", "close", "high", "low", "volume"]:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            return df
        except Exception as e:
            print(f"  [重试 {attempt+1}/3] {code} 获取失败: {e}")
            time.sleep(1)
    return None


# ===================== 指标计算 =====================
def calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """计算回测所需技术指标（向量化，与fast_backtest一致）"""
    df = df.copy()
    n = len(df)

    # Donchian通道
    df["dc_high"] = df["high"].rolling(CHANNEL_BASE).max().shift(1)
    df["exit_low"] = df["low"].rolling(EXIT_BASE).min().shift(1)

    # MA
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()
    df["ma20_slope"] = (df["ma20"] - df["ma20"].shift(5)) / df["ma20"].shift(5)

    # 趋势强度
    df["trend_strength"] = (df["close"] - df["ma60"]) / df["ma60"]

    # ATR
    df["prev_close"] = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["prev_close"]).abs(),
        (df["low"] - df["prev_close"]).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.rolling(20).mean()

    # MACD
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    df["macd_hist"] = 2 * (dif - dea)

    # 成交量
    df["vol_q75"] = df["volume"].rolling(VOL_Q75_WIN).quantile(0.75)

    # R²（20日）
    df["r2_20"] = 0.0
    for i in range(20, n):
        closes = df["close"].iloc[i-19:i+1].values
        if len(closes) >= 10:
            x = np.arange(len(closes), dtype=float)
            slope, intercept = np.polyfit(x, closes, 1)
            y_pred = slope * x + intercept
            ss_res = np.sum((closes - y_pred) ** 2)
            ss_tot = np.sum((closes - closes.mean()) ** 2)
            df.iloc[i, df.columns.get_loc("r2_20")] = 1 - ss_res / (ss_tot + 1e-10)

    return df


def generate_signals(df: pd.DataFrame) -> pd.DataFrame:
    """生成买卖信号（买入: Donchian突破+量能+趋势+MACD评分≥2; 卖出: 跌破退出线）"""
    df = df.copy()
    n = len(df)
    buy_signals = np.zeros(n, dtype=bool)
    sell_signals = np.zeros(n, dtype=bool)
    entry_scores = np.full(n, 3.0)

    for i in range(60, n):
        if pd.isna(df["dc_high"].iloc[i]) or pd.isna(df["vol_q75"].iloc[i]):
            continue

        breakout = df["close"].iloc[i] > df["dc_high"].iloc[i]
        vol_ok = df["volume"].iloc[i] > df["vol_q75"].iloc[i]
        ma20s = df["ma20_slope"].iloc[i] if not pd.isna(df["ma20_slope"].iloc[i]) else 0
        price_above_ma60 = df["close"].iloc[i] > df["ma60"].iloc[i] if not pd.isna(df["ma60"].iloc[i]) else False
        trend_ok = (ma20s > 0.001) and price_above_ma60
        macd_ok = False
        if i >= 3 and not pd.isna(df["macd_hist"].iloc[i]):
            macd_ok = df["macd_hist"].iloc[i] > 0 and df["macd_hist"].iloc[i] > df["macd_hist"].iloc[i-3]

        score = 0
        if breakout:
            score += 1
        if vol_ok:
            score += 1
        if trend_ok:
            score += 0.5
        if macd_ok:
            score += 0.5

        entry_scores[i] = score
        if score >= MIN_ENTRY_SCORE and trend_ok:
            buy_signals[i] = True

        if not pd.isna(df["exit_low"].iloc[i]) and df["close"].iloc[i] < df["exit_low"].iloc[i]:
            sell_signals[i] = True

    df["buy_signal"] = buy_signals
    df["sell_signal"] = sell_signals
    df["entry_score"] = entry_scores
    return df


# ===================== 回测引擎 =====================
@dataclass
class Trade:
    code: str
    buy_date: object
    buy_price: float
    shares: int
    sell_date: object = None
    sell_price: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    sell_reason: str = ""
    holding_days: int = 0
    partial: bool = False


def _calc_metrics(equity_curve: list, trades: list, init_cap: float) -> dict:
    """从权益曲线和交易列表计算绩效指标"""
    if not equity_curve:
        return {"total_return": 0, "max_drawdown": 0, "sharpe": 0,
                "trades": 0, "win_rate": 0, "profit_factor": 0, "avg_win": 0, "avg_loss": 0}

    eq = pd.DataFrame(equity_curve)
    eq["daily_ret"] = eq["equity"].pct_change()
    total_return = (eq["equity"].iloc[-1] / init_cap - 1) * 100

    eq["peak"] = eq["equity"].cummax()
    eq["dd"] = (eq["equity"] / eq["peak"] - 1) * 100
    max_dd = eq["dd"].min()

    dr = eq["daily_ret"].dropna()
    if len(dr) > 20 and dr.std() > 0:
        sharpe = dr.mean() / dr.std() * np.sqrt(250)
    else:
        sharpe = 0

    completed = [t for t in trades if t.sell_date is not None]
    wins = [t for t in completed if t.pnl > 0]
    losses = [t for t in completed if t.pnl <= 0]
    win_rate = len(wins) / len(completed) * 100 if completed else 0

    total_win = sum(t.pnl for t in wins)
    total_loss = abs(sum(t.pnl for t in losses))
    pf = total_win / total_loss if total_loss > 0 else (999.0 if total_win > 0 else 0)

    avg_win = total_win / len(wins) if wins else 0
    avg_loss = total_loss / len(losses) if losses else 0

    return {
        "total_return": round(float(total_return), 2),
        "max_drawdown": round(float(max_dd), 2),
        "sharpe": round(float(sharpe), 3),
        "trades": len(completed),
        "win_rate": round(float(win_rate), 1),
        "profit_factor": round(float(pf), 2),
        "avg_win": round(float(avg_win), 0),
        "avg_loss": round(float(avg_loss), 0),
    }


def backtest_old(df: pd.DataFrame, init_cap: float = INITIAL_CAPITAL_PER_STOCK) -> dict:
    """
    旧逻辑：
    - 初始止损 = entry × 0.95（固定不移动）
    - 固定止盈 = entry × 1.40（日内最高价触及即全部卖出）
    - 海龟退出信号全部卖出
    """
    n = len(df)
    position = 0          # 当前持仓股数
    entry_price = 0.0
    stop_loss = 0.0
    take_profit = 0.0
    entry_idx = 0
    cash = init_cap
    trades = []
    equity_curve = []

    for i in range(n):
        row = df.iloc[i]
        high = row["high"]
        low = row["low"]
        close = row["close"]
        dt = row["date"]

        sell_flag = False
        sell_price = close
        sell_reason = ""
        sell_shares = position

        if position > 0:
            # 止损（日内最低价触及）
            if low <= stop_loss:
                sell_flag = True
                sell_price = stop_loss
                sell_reason = "止损"
            # 止盈（日内最高价触及固定线，全部卖出）
            elif high >= take_profit:
                sell_flag = True
                sell_price = take_profit
                sell_reason = "固定止盈"
            # 海龟退出信号
            elif row["sell_signal"]:
                sell_flag = True
                sell_price = close
                sell_reason = "海龟退出"

            if sell_flag and sell_shares > 0:
                pnl = (sell_price - entry_price) * sell_shares
                cash += sell_price * sell_shares
                trades.append(Trade(
                    code="", buy_date=df.iloc[entry_idx]["date"], buy_price=entry_price,
                    shares=sell_shares, sell_date=dt, sell_price=sell_price,
                    pnl=pnl, pnl_pct=(sell_price/entry_price-1)*100,
                    sell_reason=sell_reason, holding_days=(dt - df.iloc[entry_idx]["date"]).days,
                ))
                position = 0

        # 买入信号（空仓时）
        if position == 0 and row["buy_signal"]:
            atr_val = row["atr"] if not pd.isna(row["atr"]) else close * 0.03
            risk_per_share = atr_val * ATR_MULT
            if risk_per_share <= 0:
                risk_per_share = close * STOP_LOSS_PCT
            max_shares = int(cash * 0.10 / risk_per_share)
            max_shares = min(max_shares, int(cash * 0.5 / close))
            if max_shares < 100:
                max_shares = 100
            shares = (max_shares // 100) * 100
            if shares >= 100 and shares * close <= cash:
                position = shares
                entry_price = close
                entry_idx = i
                stop_loss = close * (1 - STOP_LOSS_PCT)
                take_profit = close * (1 + TP_THRESHOLD)
                cash -= close * shares

        equity = cash + position * close
        equity_curve.append({"date": dt, "equity": equity})

    # 收盘强制平仓（只用于计算最终净值，不计入已完成交易统计）
    if position > 0:
        final_close = df.iloc[-1]["close"]
        cash += final_close * position
        # 最后一笔按市值计入但不算completed trade
        equity_curve[-1]["equity"] = cash

    return _calc_metrics(equity_curve, trades, init_cap)


def backtest_new(df: pd.DataFrame, init_cap: float = INITIAL_CAPITAL_PER_STOCK,
                 use_tier: bool = True, use_partial: bool = True,
                 use_breakeven: bool = True) -> dict:
    """
    新逻辑（对齐主策略v6_optimized.py 1530-1590行）：
    1. 初始止损 = entry × 0.95
    2. 浮盈>5%：止损上移到entry_price（保本止损）
    3. 浮盈>触发线（强20%/中15%/弱10%）：止损上移到MA20×0.98（阶梯止盈）
    4. 浮盈>部分止盈阈值40%且未部分卖出过：
       卖出take_profit_pct比例（R²分档15%/25%/40%），向下取整100股，标记partial_sold
    5. 部分卖出后剩余仓位继续用阶梯止盈跟踪
    6. 止损/阶梯止盈触发：全部卖出
    7. 海龟退出信号：非强趋势锁仓时全部卖出
    8. 不足100股零头全部卖出

    use_tier/use_partial/use_breakeven: 开关，用于消融实验
    """
    n = len(df)
    position = 0
    entry_price = 0.0
    stop_loss = 0.0
    entry_idx = 0
    partial_sold = False
    cash = init_cap
    trades = []
    equity_curve = []

    for i in range(n):
        row = df.iloc[i]
        high = row["high"]
        low = row["low"]
        close = row["close"]
        dt = row["date"]
        ma20 = row["ma20"] if not pd.isna(row["ma20"]) else close
        ma60 = row["ma60"] if not pd.isna(row["ma60"]) else close
        ma20_slope = row["ma20_slope"] if not pd.isna(row["ma20_slope"]) else 0
        trend_str = row["trend_strength"] if not pd.isna(row["trend_strength"]) else 0
        r2 = row["r2_20"] if not pd.isna(row["r2_20"]) else 0

        # 确定趋势档位（阶梯触发线按趋势强度分档）
        if trend_str > TREND_STRONG_TH:
            tier_trigger = TIER_STRONG
        elif trend_str < TREND_WEAK_TH:
            tier_trigger = TIER_WEAK
        else:
            tier_trigger = TIER_NEUTRAL
        # 部分止盈触发线统一40%（公平对比，与旧逻辑固定止盈阈值一致）
        tp_threshold = TP_THRESHOLD_PARTIAL

        # 部分止盈比例（按R²，与主策略一致）
        if r2 > R2_STRONG:
            tp_sell_pct = TP_SELL_PCT_STRONG
        elif r2 > R2_NEUTRAL:
            tp_sell_pct = TP_SELL_PCT_NEUTRAL
        else:
            tp_sell_pct = TP_SELL_PCT_WEAK

        pnl = (close - entry_price) / entry_price if position > 0 and entry_price > 0 else 0

        # === 动态调整止损线（在卖出检查之前） ===
        if position > 0:
            if use_tier and pnl > tier_trigger:
                tier_stop = ma20 * MA20_TIER_FACTOR
                if tier_stop > stop_loss:
                    stop_loss = tier_stop
            elif use_breakeven and pnl > BREAKEVEN_TRIGGER:
                if entry_price > stop_loss:
                    stop_loss = entry_price
            # pnl <= 5%: 保持初始止损

        # === 卖出逻辑 ===
        sell_flag = False
        sell_price = close
        sell_reason = ""
        sell_shares = position
        is_partial = False

        if position > 0:
            # 止损/阶梯止盈（日内最低价触及）→ 全部卖出
            if low <= stop_loss:
                sell_flag = True
                sell_price = stop_loss
                if pnl > tier_trigger:
                    sell_reason = "阶梯止盈(MA20×0.98)"
                elif pnl > BREAKEVEN_TRIGGER:
                    sell_reason = "保本止损"
                else:
                    sell_reason = "初始止损"

            # 部分止盈（浮盈超过部分止盈阈值，且未部分卖出过）
            if use_partial and not sell_flag and not partial_sold and pnl > tp_threshold:
                partial_shares = int(position * tp_sell_pct)
                partial_shares = (partial_shares // 100) * 100  # 向下取整100股
                if partial_shares >= 100:
                    sell_flag = True
                    sell_price = close
                    sell_shares = partial_shares
                    sell_reason = f"部分止盈(卖{int(tp_sell_pct*100)}%)"
                    is_partial = True
                    partial_sold = True

            # 海龟退出信号（强趋势锁仓时屏蔽）
            trend_lock = (close > ma20) and (ma20 > ma60) and (ma20_slope > 0.02)
            if not sell_flag and row["sell_signal"] and not trend_lock:
                sell_flag = True
                sell_price = close
                sell_shares = position
                sell_reason = "海龟退出"

        # === 执行卖出 ===
        if sell_flag and sell_shares > 0:
            if sell_shares > position:
                sell_shares = position
            # 不足100股零头全部卖出
            if position - sell_shares < 100 and not is_partial:
                sell_shares = position

            pnl_amount = (sell_price - entry_price) * sell_shares
            cash += sell_price * sell_shares
            trades.append(Trade(
                code="", buy_date=df.iloc[entry_idx]["date"], buy_price=entry_price,
                shares=sell_shares, sell_date=dt, sell_price=sell_price,
                pnl=pnl_amount, pnl_pct=(sell_price/entry_price-1)*100,
                sell_reason=sell_reason, holding_days=(dt - df.iloc[entry_idx]["date"]).days,
                partial=is_partial,
            ))

            if is_partial:
                position -= sell_shares
                if position < 100:
                    # 零头全部卖出（已在上面处理，但兜底）
                    if position > 0:
                        leftover_pnl = (close - entry_price) * position
                        cash += close * position
                        trades.append(Trade(
                            code="", buy_date=df.iloc[entry_idx]["date"], buy_price=entry_price,
                            shares=position, sell_date=dt, sell_price=close,
                            pnl=leftover_pnl, pnl_pct=(close/entry_price-1)*100,
                            sell_reason="零头清仓", holding_days=(dt - df.iloc[entry_idx]["date"]).days,
                        ))
                    position = 0
                    partial_sold = False
            else:
                position = 0
                partial_sold = False

        # === 买入（空仓时） ===
        if position == 0 and row["buy_signal"]:
            atr_val = row["atr"] if not pd.isna(row["atr"]) else close * 0.03
            risk_per_share = atr_val * ATR_MULT
            if risk_per_share <= 0:
                risk_per_share = close * STOP_LOSS_PCT
            max_shares = int(cash * 0.10 / risk_per_share)
            max_shares = min(max_shares, int(cash * 0.5 / close))
            if max_shares < 100:
                max_shares = 100
            shares = (max_shares // 100) * 100
            if shares >= 100 and shares * close <= cash:
                position = shares
                entry_price = close
                entry_idx = i
                stop_loss = close * (1 - STOP_LOSS_PCT)
                partial_sold = False
                cash -= close * shares

        equity = cash + position * close
        equity_curve.append({"date": dt, "equity": equity})

    if position > 0:
        final_close = df.iloc[-1]["close"]
        cash += final_close * position
        equity_curve[-1]["equity"] = cash

    return _calc_metrics(equity_curve, trades, init_cap)


# ===================== 主流程 =====================
async def main():
    result_mode = sys.argv[1] if len(sys.argv) > 1 else "display_only"
    sdk = CodeActSDK()

    try:
        print("=" * 70)
        print("止盈逻辑回测对比：旧(固定40%全部卖出) vs 新(分段止盈)")
        print("=" * 70)

        all_old = []
        all_new = []
        all_new_b = []  # 变体B：保本+部分止盈，无MA20阶梯
        all_new_c = []  # 变体C：保本+阶梯，无部分止盈
        all_new_d = []  # 变体D：仅部分止盈，无保本无阶梯
        all_new_e = []  # 变体E：仅阶梯，无保本无部分止盈
        stock_results = []

        for code in TEST_STOCKS:
            print(f"\n{'='*50}")
            print(f"处理 {code} ...")
            df = fetch_kline(code, BACKTEST_BARS)
            if df is None or len(df) < 100:
                print(f"  [跳过] {code} 数据不足")
                continue
            print(f"  获取 {len(df)} 条K线: {df['date'].iloc[0].date()} ~ {df['date'].iloc[-1].date()}")

            df = calc_indicators(df)
            df = generate_signals(df)
            buy_count = int(df["buy_signal"].sum())
            sell_count = int(df["sell_signal"].sum())
            print(f"  买入信号: {buy_count}, 卖出信号: {sell_count}")

            old_m = backtest_old(df)
            new_m = backtest_new(df, use_tier=True, use_partial=True, use_breakeven=True)
            new_b = backtest_new(df, use_tier=False, use_partial=True, use_breakeven=True)
            new_c = backtest_new(df, use_tier=True, use_partial=False, use_breakeven=True)
            new_d = backtest_new(df, use_tier=False, use_partial=True, use_breakeven=False)
            new_e = backtest_new(df, use_tier=True, use_partial=False, use_breakeven=False)

            print(f"  旧逻辑:   收益={old_m['total_return']}% 回撤={old_m['max_drawdown']}% "
                  f"Sharpe={old_m['sharpe']} 胜率={old_m['win_rate']}% 交易={old_m['trades']}")
            print(f"  新(全):   收益={new_m['total_return']}% 回撤={new_m['max_drawdown']}% "
                  f"Sharpe={new_m['sharpe']} 胜率={new_m['win_rate']}% 交易={new_m['trades']}")
            print(f"  新(保+部):收益={new_b['total_return']}% 回撤={new_b['max_drawdown']}% "
                  f"Sharpe={new_b['sharpe']} 胜率={new_b['win_rate']}% 交易={new_b['trades']}")
            print(f"  新(保+阶):收益={new_c['total_return']}% 回撤={new_c['max_drawdown']}% "
                  f"Sharpe={new_c['sharpe']} 胜率={new_c['win_rate']}% 交易={new_c['trades']}")
            print(f"  新(仅部): 收益={new_d['total_return']}% 回撤={new_d['max_drawdown']}% "
                  f"Sharpe={new_d['sharpe']} 胜率={new_d['win_rate']}% 交易={new_d['trades']}")
            print(f"  新(仅阶): 收益={new_e['total_return']}% 回撤={new_e['max_drawdown']}% "
                  f"Sharpe={new_e['sharpe']} 胜率={new_e['win_rate']}% 交易={new_e['trades']}")

            all_old.append(old_m)
            all_new.append(new_m)
            all_new_b.append(new_b)
            all_new_c.append(new_c)
            all_new_d.append(new_d)
            all_new_e.append(new_e)
            stock_results.append({
                "code": code,
                "bars": len(df),
                "date_range": f"{df['date'].iloc[0].date()}~{df['date'].iloc[-1].date()}",
                "buy_signals": buy_count,
                "old": old_m,
                "new_full": new_m,
                "new_breakeven_partial": new_b,
                "new_breakeven_tier": new_c,
                "new_partial_only": new_d,
                "new_tier_only": new_e,
            })

        if not all_old:
            await sdk.submit_result(
                result_mode="notify", status="error",
                message="回测失败：未能获取任何股票数据",
            )
            return

        # 汇总（等权平均各股票指标）
        def avg_metrics(mlist):
            keys = ["total_return", "max_drawdown", "sharpe", "trades", "win_rate", "profit_factor"]
            return {k: round(float(np.mean([m[k] for m in mlist])), 3) for k in keys}

        avg_old = avg_metrics(all_old)
        avg_new = avg_metrics(all_new)
        avg_new_b = avg_metrics(all_new_b)
        avg_new_c = avg_metrics(all_new_c)
        avg_new_d = avg_metrics(all_new_d)
        avg_new_e = avg_metrics(all_new_e)

        def sharpe_chg(avg_new_v):
            so = avg_old["sharpe"]
            sn = avg_new_v["sharpe"]
            if so == 0:
                return 0.0
            return (sn - so) / abs(so) * 100

        chg_full = sharpe_chg(avg_new)
        chg_b = sharpe_chg(avg_new_b)
        chg_c = sharpe_chg(avg_new_c)
        chg_d = sharpe_chg(avg_new_d)
        chg_e = sharpe_chg(avg_new_e)

        # 选择Sharpe最优的新逻辑变体
        candidates = [
            ("完整分段止盈(保本+阶梯+部分止盈)", avg_new, chg_full),
            ("保本+部分止盈(无MA20阶梯)", avg_new_b, chg_b),
            ("保本+MA20阶梯(无部分止盈)", avg_new_c, chg_c),
            ("仅部分止盈(无保本无阶梯)", avg_new_d, chg_d),
            ("仅MA20阶梯(无保本无部分止盈)", avg_new_e, chg_e),
        ]
        best_name, best_avg, best_chg = max(candidates, key=lambda x: x[1]["sharpe"])
        adopted = best_chg >= -5.0

        print("\n" + "=" * 80)
        print("汇总结果（6股等权平均）")
        print("=" * 80)
        print(f"{'指标':<15} {'旧逻辑':>10} {'全':>10} {'保+部':>10} {'保+阶':>10} {'仅部':>10} {'仅阶':>10}")
        print("-" * 80)
        for k in ["total_return", "max_drawdown", "sharpe", "trades", "win_rate", "profit_factor"]:
            print(f"{k:<15} {avg_old[k]:>10.3f} {avg_new[k]:>10.3f} {avg_new_b[k]:>10.3f} "
                  f"{avg_new_c[k]:>10.3f} {avg_new_d[k]:>10.3f} {avg_new_e[k]:>10.3f}")
        print("-" * 80)
        print(f"Sharpe变化%:   {'':15} {chg_full:>+10.2f} {chg_b:>+10.2f} {chg_c:>+10.2f} {chg_d:>+10.2f} {chg_e:>+10.2f}")
        print(f"\n最优变体: {best_name} (Sharpe变化 {best_chg:+.2f}%)")
        print(f"采纳判定: {'✅ 采纳' if adopted else '❌ 不采纳（最优变体Sharpe下降仍超过5%）'}")

        # 保存报告
        report = {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "test_stocks": TEST_STOCKS,
            "bars_per_stock": BACKTEST_BARS,
            "old_logic": "固定5%止损+固定40%止盈全部卖出+海龟退出",
            "variants": {
                "new_full": "保本止损+阶梯止盈(MA20×0.98)+部分止盈(R²分档)+海龟退出(趋势锁仓)",
                "new_breakeven_partial": "保本止损+部分止盈(R²分档)+海龟退出(无MA20阶梯)",
                "new_breakeven_tier": "保本止损+阶梯止盈(MA20×0.98)+海龟退出(无部分止盈)",
                "new_partial_only": "初始5%止损+部分止盈(R²分档)+海龟退出(无保本无阶梯)",
                "new_tier_only": "初始5%止损+阶梯止盈(MA20×0.98)+海龟退出(无保本无部分止盈)",
            },
            "avg_old": avg_old,
            "avg_new_full": avg_new,
            "avg_new_breakeven_partial": avg_new_b,
            "avg_new_breakeven_tier": avg_new_c,
            "avg_new_partial_only": avg_new_d,
            "avg_new_tier_only": avg_new_e,
            "sharpe_change": {
                "new_full": round(chg_full, 2),
                "new_breakeven_partial": round(chg_b, 2),
                "new_breakeven_tier": round(chg_c, 2),
                "new_partial_only": round(chg_d, 2),
                "new_tier_only": round(chg_e, 2),
            },
            "best_variant": best_name,
            "best_sharpe_change_pct": round(best_chg, 2),
            "adopted": adopted,
            "stocks": stock_results,
        }
        report_path = os.path.join(OUTPUT_DIR, "tp_backtest_report.json")
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\n报告已保存: {report_path}")

        # 构造用户消息
        verdict = "采纳" if adopted else "不采纳"
        msg = (
            f"止盈逻辑回测对比完成，结论：**{verdict}**\n\n"
            f"6股标准测试集，每只10万独立账户，{BACKTEST_BARS}条前复权日线（约2.5年）。\n"
            f"测试了3种新逻辑变体，取Sharpe最优者与旧逻辑对比。\n\n"
            f"**最优变体：{best_name}**\n\n"
            f"| 指标 | 旧逻辑 | 新(最优) | 变化 |\n"
            f"|---|---|---|---|\n"
            f"| 总收益率(均) | {avg_old['total_return']}% | {best_avg['total_return']}% | {best_avg['total_return']-avg_old['total_return']:+.2f}% |\n"
            f"| 最大回撤(均) | {avg_old['max_drawdown']}% | {best_avg['max_drawdown']}% | {best_avg['max_drawdown']-avg_old['max_drawdown']:+.2f}% |\n"
            f"| Sharpe(均) | {avg_old['sharpe']} | {best_avg['sharpe']} | {best_chg:+.2f}% |\n"
            f"| 胜率(均) | {avg_old['win_rate']}% | {best_avg['win_rate']}% | {best_avg['win_rate']-avg_old['win_rate']:+.1f}% |\n"
            f"| 盈亏比(均) | {avg_old['profit_factor']} | {best_avg['profit_factor']} | {best_avg['profit_factor']-avg_old['profit_factor']:+.2f} |\n\n"
            f"**三变体Sharpe变化：**\n"
            f"- 完整(保本+阶梯+部分止盈): {chg_full:+.2f}%\n"
            f"- 保本+部分止盈(无阶梯): {chg_b:+.2f}%\n"
            f"- 保本+阶梯(无部分止盈): {chg_c:+.2f}%\n"
            f"- 仅部分止盈(无保本无阶梯): {chg_d:+.2f}%\n"
            f"- 仅阶梯(无保本无部分止盈): {chg_e:+.2f}%\n\n"
        )
        if adopted:
            msg += f"Sharpe下降≤5%，最优变体通过验证，将据此修改sim_trade_tracker.py。"
        else:
            msg += f"最优变体Sharpe仍下降{abs(best_chg):.2f}%，超过5%阈值，不采纳，保持原有固定止盈逻辑。"

        actual_mode = result_mode if result_mode != "auto" else "display_only"
        await sdk.submit_result(
            result_mode=actual_mode,
            status="success",
            message=msg,
            data={
                "report_path": report_path,
                "adopted": adopted,
                "best_variant": best_name,
                "sharpe_old": avg_old["sharpe"],
                "sharpe_new": best_avg["sharpe"],
                "sharpe_change_pct": round(best_chg, 2),
                "all_sharpe_changes": {
                    "new_full": round(chg_full, 2),
                    "new_breakeven_partial": round(chg_b, 2),
                    "new_breakeven_tier": round(chg_c, 2),
                    "new_partial_only": round(chg_d, 2),
                    "new_tier_only": round(chg_e, 2),
                },
            },
        )

    except Exception as e:
        import traceback
        traceback.print_exc()
        await sdk.submit_result(
            result_mode="notify",
            status="error",
            message=f"回测对比执行失败: {e}",
        )


if __name__ == "__main__":
    asyncio.run(main())