#!/usr/bin/env python3
"""
模拟交易跟踪器
- 每日16:00运行（信号扫描15:30之后）
- 读取latest_signals.json中的买卖信号
- 模拟执行交易，跟踪持仓和盈亏
- 保存每日交易记录和持仓快照
- 100万本金，按龟缠量化v6_optimized策略规则执行
"""
from pathlib import Path as _P
_ROOT = _P(__file__).resolve().parent.parent  # === LOCAL-PATCH v1 ===

import json
import sys
import os
import time
from datetime import datetime
import requests
from codeact_sdk import CodeActSDK
from trading_rules import calc_trade_cost, load_trade_cost

# ===================== 路径配置 =====================
BASE_DIR = f"{_ROOT}"
SIGNALS_FILE = os.environ.get(
    "SIM_SIGNALS_FILE", os.path.join(BASE_DIR, "codeact/output/latest_signals.json")
)
PORTFOLIO_DIR = os.environ.get("SIM_PORTFOLIO_DIR", os.path.join(BASE_DIR, "data/sim_trades"))
PORTFOLIO_FILE = os.path.join(PORTFOLIO_DIR, "portfolio.json")
TRADES_DIR = os.path.join(PORTFOLIO_DIR, "trades")
SNAPSHOTS_DIR = os.path.join(PORTFOLIO_DIR, "snapshots")
SIGNALS_ARCHIVE_DIR = os.path.join(PORTFOLIO_DIR, "signals_archive")

# ===================== 策略参数 =====================
INITIAL_CAPITAL = 1000000
MAX_POSITIONS = int(os.environ.get("SIM_MAX_POSITIONS", "10"))
KLINE_API = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
# 备用端点（主端点被WAF拦截时切换）
KLINE_API_FALLBACKS = [
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
    "https://web.ifzq.gtimg.cn/appstock/app/kline/kline",
]
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def _tencent_get(code: str, count: int) -> dict | None:
    """请求腾讯K线，主端点被WAF拦截时自动切换备用端点。成功返回json，全部失败返回None。"""
    for ep in [KLINE_API] + KLINE_API_FALLBACKS:
        url = f"{ep}?param={code},day,,,{count},qfq"
        try:
            resp = requests.get(url, timeout=10, headers=HTTP_HEADERS)
            if resp.status_code == 200 and not resp.text.lstrip().startswith("<"):
                j = resp.json()
                if j.get("code") == 0:
                    return j
        except Exception:
            continue
    return None

# stock-cli 二进制：自动探测多个可能路径（bash/CodeAct 挂载点不同）
def _find_cli_bin():
    candidates = [
        f"{_ROOT}/.skills/skill_stock-data-skill/bin/stock-cli",
        f"{_ROOT}/.skills/skill_stock-data-skill/bin/stock-cli",
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                     ".skills/skill_stock-data-skill/bin/stock-cli"),
    ]
    for p in candidates:
        if os.path.exists(p) and os.access(p, os.X_OK):
            return p
    return candidates[0]


CLI_BIN = _find_cli_bin()


def _fetch_kline_via_cli(code, count=60):
    """通过 stock-cli 获取前复权日K线，返回 list[dict] 或 []。"""
    try:
        import subprocess
        result = subprocess.run(
            [CLI_BIN, "call", "kline",
             "--param", f"code={code}",
             "--param", "period=day",
             "--param", f"count={count}",
             "--param", "fq=qfq"],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode != 0:
            return []
        payload = json.loads(result.stdout)
        rows = payload.get("data") or []
        out = []
        for r in rows:
            out.append({
                "date": r["date"],
                "open": float(r["open"]),
                "close": float(r["close"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "volume": float(r.get("volume", 0)),
            })
        return out
    except Exception:
        return []

# 单票仓位上限（占总市值比例）
SINGLE_STOCK_MAX_PCT = float(os.environ.get("SIM_SINGLE_STOCK_MAX_PCT", "45.0"))
# 止损距离上限（小数，0.10=-10%以内必须止损）；影子组合经 env 覆盖为 0.12。
# 2026-09-11 起主模拟盘启用，收紧“宽止损导致单笔亏损过大”。
MAX_STOP_PCT = float(os.environ.get("SIM_MAX_STOP_PCT", "0.10"))
# 单笔风险敞口上限（小数）：买入金额 × 止损距离 ≤ 总资产 × 该比例（0.01=单笔最多亏总资产1%）
MAX_RISK_PER_TRADE_PCT = float(os.environ.get("SIM_MAX_RISK_PER_TRADE_PCT", "0.01"))
SHADOW_MODE = os.environ.get("SIM_SHADOW_MODE", "0") == "1"
MAX_HOLD_DAYS = int(os.environ.get("SIM_MAX_HOLD_DAYS", "0"))
TRADE_COST_CFG = load_trade_cost(os.path.join(BASE_DIR, "config", "settings.yaml"))

# 极小仓位阈值（占总市值比例，低于此值的持仓若没有买入信号续持，将被清仓腾位置）
TINY_POSITION_PCT = 3.0

# 极小仓位再平衡保护：持仓不足此天数不清理（避免刚买入就被卖）
TINY_MIN_HOLD_DAYS = 3


def bounded_stop_loss(price, raw_stop):
    """限制新开仓止损距离；默认关闭，避免改写主模拟盘口径。"""
    try:
        stop = float(raw_stop)
        if MAX_STOP_PCT > 0 and price > 0:
            stop = max(stop, price * (1.0 - MAX_STOP_PCT))
        return round(stop, 2)
    except (TypeError, ValueError):
        return round(price * (1.0 - MAX_STOP_PCT), 2) if MAX_STOP_PCT > 0 else round(price * 0.95, 2)


def execution(price, shares, side):
    """影子盘使用回测同一费用/滑点口径；主盘保持历史的原价成交。"""
    if SHADOW_MODE:
        return calc_trade_cost(price, shares, side, TRADE_COST_CFG)
    return round(price, 6), round(price * shares, 2), 0.0, 0.0


def _fill_metadata(quote_price, shares, side):
    """统一生成成交信息；amount 是现金实际流入/流出（含费用）。"""
    fill_price, amount, commission, stamp_tax = execution(quote_price, shares, side)
    return {
        "quote_price": round(float(quote_price), 6),
        "price": round(float(fill_price), 6),
        "amount": round(float(amount), 2),
        "commission": round(float(commission), 2),
        "stamp_tax": round(float(stamp_tax), 2),
        "slippage_pct": round((float(fill_price) / float(quote_price) - 1) * 100, 4)
        if quote_price else 0.0,
    }


def _allocated_cost(position, shares):
    """按股数从持仓成本中分摊成本（成本包含买入费用）。"""
    original_shares = int(position.get("shares", 0))
    if original_shares <= 0:
        return 0.0
    return round(float(position.get("cost", 0.0)) * int(shares) / original_shares, 2)

# ===================== 风控配置（从 settings.yaml 读取，失败用默认值） =====================
RISK_CONFIG_PATH = os.path.join(BASE_DIR, "config/settings.yaml")
_DEFAULT_RISK_CFG = {
    "industry_max_pct": 0.40,
    "circuit_dd_halve": 0.15,
    "circuit_dd_stop": 0.25,
}


def _load_risk_config():
    """从 settings.yaml 读取风控参数；任何失败回退默认值 0.40/0.15/0.25"""
    cfg = dict(_DEFAULT_RISK_CFG)
    try:
        import yaml  # 沙箱已预装 PyYAML
        with open(RISK_CONFIG_PATH, "r", encoding="utf-8") as f:
            full = yaml.safe_load(f) or {}
        risk = full.get("risk", {}) or {}
        for k in _DEFAULT_RISK_CFG:
            if k in risk:
                cfg[k] = float(risk[k])
        print(f"[风控配置] 加载成功: industry_max_pct={cfg['industry_max_pct']}, "
              f"circuit_dd_halve={cfg['circuit_dd_halve']}, circuit_dd_stop={cfg['circuit_dd_stop']}")
    except Exception as e:
        print(f"[风控配置] 读取 settings.yaml 失败({e})，使用默认值 0.40/0.15/0.25")
    return cfg


RISK_CFG = _load_risk_config()
INDUSTRY_MAX_PCT = RISK_CFG["industry_max_pct"]
CIRCUIT_DD_HALVE = RISK_CFG["circuit_dd_halve"]
CIRCUIT_DD_STOP = RISK_CFG["circuit_dd_stop"]

# ===================== 行业分类映射（与回测引擎 v6_optimized 一致） =====================
# 申万一级行业近似映射；未在表中的股票返回"其他"
_INDUSTRY_MAP = {
    # 食品饮料
    "sh600519": "食品饮料", "sz000858": "食品饮料", "sh600809": "食品饮料",
    "sz000568": "食品饮料", "sh603369": "食品饮料", "sh600779": "食品饮料",
    "sz000596": "食品饮料", "sh603589": "食品饮料", "sh603198": "食品饮料",
    "sh600132": "食品饮料", "sh600600": "食品饮料", "sz000729": "食品饮料",
    "sh603288": "食品饮料", "sz000895": "食品饮料", "sh603899": "食品饮料",
    "sz300999": "食品饮料", "sh605499": "食品饮料", "sh603027": "食品饮料",
    # 银行
    "sh600036": "银行", "sh601398": "银行", "sh601288": "银行", "sh601988": "银行",
    "sh601939": "银行", "sh601328": "银行", "sh600000": "银行", "sh601166": "银行",
    "sh600016": "银行", "sh601818": "银行", "sz000001": "银行", "sh600015": "银行",
    "sh601169": "银行", "sh601009": "银行", "sh601229": "银行", "sz002142": "银行",
    "sh601838": "银行", "sh601916": "银行", "sh600926": "银行", "sh601077": "银行",
    # 非银金融
    "sh601318": "非银金融", "sh601628": "非银金融", "sh601601": "非银金融",
    "sh601336": "非银金融", "sh601688": "非银金融", "sh600030": "非银金融",
    "sz000776": "非银金融", "sh601211": "非银金融", "sh600999": "非银金融",
    "sh601878": "非银金融", "sh600837": "非银金融", "sh601788": "非银金融",
    "sh601066": "非银金融",
    # 汽车
    "sz002594": "汽车", "sh600104": "汽车", "sz000625": "汽车", "sh601238": "汽车",
    "sh601633": "汽车", "sz000800": "汽车", "sh600006": "汽车", "sz000550": "汽车",
    "sh601127": "汽车", "sz002460": "汽车", "sz300750": "汽车",
    # 家用电器
    "sz000651": "家用电器", "sz000333": "家用电器", "sh600690": "家用电器",
    "sz002032": "家用电器", "sz002508": "家用电器", "sh603868": "家用电器",
    "sz002242": "家用电器", "sz002705": "家用电器", "sh603551": "家用电器",
    "sz002677": "家用电器", "sh603486": "家用电器", "sh600854": "家用电器",
}


def _get_industry(code):
    """返回股票所属行业，未在映射表中返回"其他"（与回测引擎一致）"""
    return _INDUSTRY_MAP.get(code, "其他")


def _calc_industry_exposure(portfolio, industry, total_value):
    """计算指定行业当前持仓市值占总市值的比例（0~1）"""
    if total_value <= 0:
        return 0.0, 0.0
    exposure_value = 0.0
    for p in portfolio["positions"]:
        if _get_industry(p["code"]) == industry:
            cur_val = p.get("current_value", p.get("entry_price", 0) * p.get("shares", 0))
            exposure_value += cur_val
    return exposure_value, exposure_value / total_value


def _portfolio_market_value(portfolio):
    """计算组合当前总市值（现金 + 持仓市值）"""
    positions_value = sum(
        p.get("current_value", p.get("entry_price", 0) * p.get("shares", 0))
        for p in portfolio["positions"]
    )
    return portfolio["cash"] + positions_value


# 不同市场状态下的目标总仓位区间（占总市值比例，%）
MARKET_TARGET_POSITION = {
    "bull": {"low": 70.0, "high": 80.0},
    "neutral": {"low": 50.0, "high": 65.0},
    "bear": {"low": 40.0, "high": 55.0},
    "unknown": {"low": 50.0, "high": 60.0},
}


# ===================== 工具函数 =====================
def _fetch_realtime_quote(code):
    """从腾讯实时行情API qt.gtimg.cn 获取当日OHLC（这个端点长期稳定，作为K线端点全挂时的最终兜底）。
    返回 dict(date/open/high/low/close) 或 None。"""
    try:
        url = f"https://qt.gtimg.cn/q={code}"
        resp = requests.get(url, timeout=8, headers=HTTP_HEADERS)
        if resp.status_code != 200:
            return None
        text = resp.content.decode("gbk", errors="ignore")
        # var v_sh600206="1~名称~代码~现价~昨收~今开~成交量~...";
        if '="' not in text:
            return None
        payload = text.split('="', 1)[1].rstrip('";\n')
        f = payload.split("~")
        if len(f) < 40:
            return None
        # f[3]=当前价 f[4]=昨收 f[5]=今开 f[33]=最高 f[34]=最低（部分格式是f[33]=涨跌%，需验证）
        # 标准腾讯字段：[1]名称 [3]现价 [4]昨收 [5]今开 [6]成交量(手) [33]最高 [34]最低
        # 但不同接口字段索引不同，稳妥用 f[3]现价 + f[5]今开 + f[33]最高 + f[34]最低
        price = float(f[3])
        prev_close = float(f[4]) if f[4] else price
        open_p = float(f[5]) if f[5] else price
        # 高/低索引兼容：尝试多个位置
        high = low = None
        for idx in [33, 41, 34]:
            try:
                v = float(f[idx])
                if v > 0 and idx == 33 and high is None:
                    high = v
                elif v > 0 and idx == 34 and low is None:
                    low = v
            except (ValueError, IndexError):
                pass
        if high is None or high <= 0:
            high = max(price, open_p)
        if low is None or low <= 0:
            low = min(price, open_p)
        # 日期：从字段 [30] 或 [31] 拿，格式 yyyymmddhhmmss
        date_str = time.strftime("%Y-%m-%d")
        for idx in [30, 31]:
            try:
                if len(f) > idx and f[idx].isdigit() and len(f[idx]) >= 8:
                    date_str = f"{f[idx][:4]}-{f[idx][4:6]}-{f[idx][6:8]}"
                    break
            except (ValueError, IndexError):
                pass
        return {
            "date": date_str,
            "open": open_p,
            "close": price,
            "high": high,
            "low": low,
        }
    except Exception:
        return None


def fetch_latest_ohlc(code, expected_date=None):
    """获取最新K线（OHLC），可选校验日期。
    优先级：腾讯K线HTTP(多端点) → 腾讯实时行情qt.gtimg.cn → CLI。"""
    # 1. 腾讯K线HTTP（多端点轮询）
    raw = _tencent_get(code, 5)
    if raw is not None:
        stock_data = raw.get("data", {}).get(code, {})
        bars = stock_data.get("qfqday") or stock_data.get("day")
        if bars:
            last = bars[-1]
            ohlc = {
                "date": last[0],
                "open": float(last[1]),
                "close": float(last[2]),
                "high": float(last[3]),
                "low": float(last[4]),
            }
            if expected_date:
                if ohlc["date"].replace("-", "") != expected_date.replace("-", ""):
                    print(f"  [WARN] {code} 日期不匹配: API={ohlc['date']} 期望={expected_date}")
                    return None
            return ohlc
    # 2. 腾讯实时行情（qt.gtimg.cn 长期稳定）
    rt = _fetch_realtime_quote(code)
    if rt is not None:
        if expected_date is None or rt["date"].replace("-", "") == expected_date.replace("-", ""):
            return rt
        # 日期不匹配时也返回（实时行情可能还没更新日期），但打警告
        print(f"  [WARN] {code} 实时行情日期{rt['date']} != 期望{expected_date}，仍使用实时价")
        return rt
    # 3. CLI 兜底
    cli_rows = _fetch_kline_via_cli(code, 5)
    if cli_rows:
        last = cli_rows[-1]
        if expected_date and last["date"].replace("-", "") != expected_date.replace("-", ""):
            print(f"  [WARN] {code} 日期不匹配: CLI={last['date']} 期望={expected_date}")
            return None
        return last
    print(f"  [ERROR] fetch {code}: all channels failed")
    return None


def fetch_kline_history(code, count=60):
    """获取前复权日K线历史数据（用于计算MA20/MA60/趋势强度/R²等指标）。
    返回 list[dict]，每个元素包含 date/open/close/high/low/volume，按日期升序。
    失败返回空列表。腾讯API被WAF时切备用端点，再失败CLI兜底。
    """
    raw = _tencent_get(code, count)
    if raw is not None:
        stock_data = raw.get("data", {}).get(code, {})
        bars = stock_data.get("qfqday") or stock_data.get("day")
        if bars:
            return [{
                "date": b[0],
                "open": float(b[1]),
                "close": float(b[2]),
                "high": float(b[3]),
                "low": float(b[4]),
                "volume": float(b[5]) if len(b) > 5 else 0.0,
            } for b in bars]
    # API 全部失败 → CLI 兜底
    try:
        rows = _fetch_kline_via_cli(code, count)
        if rows:
            return rows
    except Exception as e:
        print(f"  [ERROR] fetch_kline_history {code}: {e}")
    return []


# ===================== 分段止盈参数（对齐主策略v6_optimized.py，经回测验证） =====================
# 回测结论：保本触发线用8%（比主策略5%宽松，因sim初始止损固定5%，5%就移到成本价过紧），
# 完整分段止盈(保本+阶梯+部分止盈) Sharpe下降仅1.78%，收益+0.2%，胜率+4.3%，通过5%阈值。
BREAKEVEN_TRIGGER = 0.08        # 浮盈8%以上：止损上移到entry_price（保本止损）
TIER_STRONG_TRIGGER = 0.20      # 强趋势阶梯止盈触发线
TIER_NEUTRAL_TRIGGER = 0.15     # 中性趋势阶梯止盈触发线
TIER_WEAK_TRIGGER = 0.10        # 弱趋势阶梯止盈触发线
TREND_STRONG_TH = 0.15          # trend_strength > 0.15 强趋势
TREND_WEAK_TH = -0.05           # trend_strength < -0.05 弱趋势
TP_THRESHOLD = 0.40             # 部分止盈触发线（与原固定止盈40%对齐）
TP_SELL_PCT_STRONG = 0.15       # R²强趋势部分止盈15%
TP_SELL_PCT_NEUTRAL = 0.25      # R²中性部分止盈25%
TP_SELL_PCT_WEAK = 0.40         # R²弱趋势部分止盈40%
R2_STRONG_TH = 0.15
R2_NEUTRAL_TH = 0.05
MA20_TIER_FACTOR = 0.98         # 阶梯止损 = MA20 × 0.98
MA20_SLOPE_LOCK_TH = 0.02       # 趋势锁仓：MA20斜率 > 0.02


def _calc_ma(closes, period):
    """计算简单移动平均，数据不足返回None"""
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def _calc_r2(closes, lookback=20):
    """计算线性回归R²（决定系数），用于判断趋势强度"""
    n = min(len(closes), lookback)
    if n < 10:
        return 0.0
    y = closes[-n:]
    x = list(range(n))
    n_pts = len(x)
    x_mean = sum(x) / n_pts
    y_mean = sum(y) / n_pts
    ss_xy = sum((xi - x_mean) * (yi - y_mean) for xi, yi in zip(x, y))
    ss_xx = sum((xi - x_mean) ** 2 for xi in x)
    ss_yy = sum((yi - y_mean) ** 2 for yi in y)
    if ss_xx == 0 or ss_yy == 0:
        return 0.0
    return (ss_xy ** 2) / (ss_xx * ss_yy)


def calc_segmented_tp_state(code, close, high, low):
    """
    计算分段止盈所需的全部状态指标。
    返回 dict: ma20, ma60, ma20_slope, trend_strength, r2_20, tier_trigger, tp_sell_pct, trend_lock
    任一指标无法计算时用close等安全默认值。
    """
    bars = fetch_kline_history(code, count=80)
    closes = [b["close"] for b in bars] if bars else []

    # 确保最后一条是当日收盘价（fetch_kline_history返回的最后一条可能就是当日）
    if closes and abs(closes[-1] - close) > 0.01:
        # 如果历史数据最后一条不是当日，用传入的close补充
        closes.append(close)
    elif not closes:
        closes = [close]

    ma20 = _calc_ma(closes, 20) or close
    ma60 = _calc_ma(closes, 60) or close

    # MA20斜率（5日变化率）
    if len(closes) >= 25:
        ma20_5ago = sum(closes[-25:-20]) / 5
        ma20_slope = (ma20 - ma20_5ago) / ma20_5ago if ma20_5ago > 0 else 0
    else:
        ma20_slope = 0.0

    trend_strength = (close - ma60) / ma60 if ma60 > 0 else 0
    r2 = _calc_r2(closes, lookback=20)

    # 趋势分档 → 阶梯触发线
    if trend_strength > TREND_STRONG_TH:
        tier_trigger = TIER_STRONG_TRIGGER
    elif trend_strength < TREND_WEAK_TH:
        tier_trigger = TIER_WEAK_TRIGGER
    else:
        tier_trigger = TIER_NEUTRAL_TRIGGER

    # R²分档 → 部分止盈比例
    if r2 > R2_STRONG_TH:
        tp_sell_pct = TP_SELL_PCT_STRONG
    elif r2 > R2_NEUTRAL_TH:
        tp_sell_pct = TP_SELL_PCT_NEUTRAL
    else:
        tp_sell_pct = TP_SELL_PCT_WEAK

    # 强趋势锁仓：close > MA20 > MA60 且 MA20斜率 > 0.02
    trend_lock = (close > ma20) and (ma20 > ma60) and (ma20_slope > MA20_SLOPE_LOCK_TH)

    return {
        "ma20": round(ma20, 3),
        "ma60": round(ma60, 3),
        "ma20_slope": round(ma20_slope, 4),
        "trend_strength": round(trend_strength, 4),
        "r2_20": round(r2, 4),
        "tier_trigger": tier_trigger,
        "tp_sell_pct": tp_sell_pct,
        "trend_lock": trend_lock,
    }


def load_portfolio():
    """加载或初始化投资组合"""
    if os.path.exists(PORTFOLIO_FILE):
        with open(PORTFOLIO_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "initial_capital": INITIAL_CAPITAL,
        "cash": INITIAL_CAPITAL,
        "positions": [],
        "trade_history": [],
        "start_date": None,
        "last_processed_date": None,
        "current_date": None,
        "total_value": INITIAL_CAPITAL,
        "total_pnl": 0,
        "total_pnl_pct": 0,
        "max_value": INITIAL_CAPITAL,
        "max_drawdown": 0,
        # 【风控新增】峰值权益 & 熔断状态（与回测引擎字段语义一致）
        # peak_equity    : 历史最高账户总市值，用于计算当前回撤
        # halved_peak    : 上一次触发减半时的 peak_equity，防止同一 peak 重复减半
        # circuit_stop_until : "YYYY-MM-DD"，该日期之前（含）禁止开新仓
        "peak_equity": INITIAL_CAPITAL,
        "halved_peak": None,
        "circuit_stop_until": None,
    }


def save_portfolio(portfolio):
    """保存投资组合"""
    os.makedirs(PORTFOLIO_DIR, exist_ok=True)
    with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
        json.dump(portfolio, f, ensure_ascii=False, indent=2)


# ===================== 股票名称查询（腾讯实时行情） =====================
_STOCK_NAME_CACHE = {}


def get_stock_name(code):
    """通过腾讯实时行情API获取股票名称。

    code 格式如 sz001229 / sh603078。返回股票名称字符串；失败返回 ""。
    带内存缓存，同一 code 不重复请求。科创板（688xxx）会在 sz/sh 之间回退尝试。
    """
    if not code:
        return ""
    if code in _STOCK_NAME_CACHE:
        return _STOCK_NAME_CACHE[code]

    candidates = [code]
    # 科创板前缀回退：sz688xxx <-> sh688xxx
    if code.startswith("sz688"):
        candidates.append("sh" + code[2:])
    elif code.startswith("sh688"):
        candidates.append("sz" + code[2:])

    name = ""
    for cand in candidates:
        try:
            resp = requests.get(f"https://qt.gtimg.cn/q={cand}", timeout=8, headers=HTTP_HEADERS)
            resp.encoding = "gbk"
            text = resp.text.strip()
            # v_sz001229="51~魅视科技~001229~...";
            if "~" in text:
                parts = text.split("~")
                if len(parts) >= 2:
                    cand_name = parts[1].strip()
                    if cand_name:
                        name = cand_name
                        break
        except Exception as e:
            print(f"  [名称查询] {cand} 失败: {e}")
            continue

    _STOCK_NAME_CACHE[code] = name
    return name


def backfill_position_names(portfolio):
    """遍历持仓，为缺失 name 字段的持仓补全股票名称。返回补全数量。"""
    filled = 0
    for p in portfolio.get("positions", []):
        if not p.get("name"):
            nm = get_stock_name(p["code"])
            if nm:
                p["name"] = nm
                filled += 1
                print(f"  [名称补全] {p['code']} -> {nm}")
    return filled


def save_trade_record(date, trade):
    """保存单日交易记录"""
    os.makedirs(TRADES_DIR, exist_ok=True)
    path = os.path.join(TRADES_DIR, f"trades_{date}.json")
    trades = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            trades = json.load(f)
    trades.append(trade)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(trades, f, ensure_ascii=False, indent=2)


def append_trade_log(trade):
    """追加单笔交易记录到CSV日志"""
    os.makedirs(PORTFOLIO_DIR, exist_ok=True)
    csv_path = os.path.join(PORTFOLIO_DIR, "trade_log.csv")
    header = "日期,代码,操作,成交价,股数,金额,盈亏,盈亏%,原因,止损线,止盈线,持仓天数,买入价"
    if SHADOW_MODE:
        header += ",行情价,佣金,印花税,滑点%,成本基准"
    file_exists = os.path.exists(csv_path)
    with open(csv_path, "a", encoding="utf-8") as f:
        if not file_exists:
            f.write(header + "\n")
        entry_price = trade.get("entry_price", trade.get("price", ""))
        row = [
            str(trade.get("date", "")),
            str(trade.get("code", "")),
            str(trade.get("action", "")),
            str(trade.get("price", "")),
            str(trade.get("shares", "")),
            str(trade.get("amount", "")),
            str(trade.get("pnl", "")),
            str(trade.get("pnl_pct", "")),
            str(trade.get("reason", "")),
            str(trade.get("stop_loss", "")),
            str(trade.get("take_profit", "")),
            str(trade.get("hold_days", "")),
            str(entry_price),
        ]
        if SHADOW_MODE:
            row.extend([
                str(trade.get("quote_price", "")),
                str(trade.get("commission", "")),
                str(trade.get("stamp_tax", "")),
                str(trade.get("slippage_pct", "")),
                str(trade.get("cost_basis", "")),
            ])
        f.write(",".join(row) + "\n")


def save_snapshot(date, portfolio, market_state="", buy_count=0, sell_count=0):
    """保存每日持仓快照"""
    os.makedirs(SNAPSHOTS_DIR, exist_ok=True)
    path = os.path.join(SNAPSHOTS_DIR, f"snapshot_{date}.json")
    snapshot = {
        "date": date,
        "market_state": market_state,
        "signal_count": buy_count,
        "cash": portfolio["cash"],
        "total_value": portfolio["total_value"],
        "total_pnl": portfolio["total_pnl"],
        "total_pnl_pct": portfolio["total_pnl_pct"],
        "max_drawdown": portfolio.get("max_drawdown", 0),
        "positions": [
            {
                "code": p["code"],
                "name": p.get("name", ""),
                "entry_price": p["entry_price"],
                "current_price": p.get("current_price", p["entry_price"]),
                "shares": p["shares"],
                "pnl": p.get("pnl", 0),
                "pnl_pct": p.get("pnl_pct", 0),
                "hold_days": p.get("hold_days", 0),
            }
            for p in portfolio["positions"]
        ],
        "position_count": len(portfolio["positions"]),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)


def archive_signals(date):
    """归档当天信号数据"""
    if not os.path.exists(SIGNALS_FILE):
        return
    os.makedirs(SIGNALS_ARCHIVE_DIR, exist_ok=True)
    dst = os.path.join(SIGNALS_ARCHIVE_DIR, f"signals_{date}.json")
    with open(SIGNALS_FILE, "r", encoding="utf-8") as f:
        data = f.read()
    with open(dst, "w", encoding="utf-8") as f:
        f.write(data)


def get_target_total_position_pct(market_state_label, buy_signal_count):
    """根据市场状态和买入信号数量，计算目标总仓位百分比

    逻辑：
    - 不同市场状态有不同的目标仓位区间（low ~ high）
    - 信号数量越多，目标仓位越靠近区间上限
    - 信号数量越少，目标仓位越靠近区间下限
    - 以 5 个信号作为"满配"基准（超过5个按5个计）
    """
    cfg = MARKET_TARGET_POSITION.get(
        market_state_label.lower(),
        MARKET_TARGET_POSITION["unknown"],
    )
    low = cfg["low"]
    high = cfg["high"]

    # 信号数量映射到 0~1 的填充比例（5个及以上算满配）
    fill_ratio = min(buy_signal_count, 5) / 5.0
    target_pct = low + (high - low) * fill_ratio
    return round(target_pct, 2)


def allocate_buy_budget(total_value, current_position_value, cash,
                        market_state_label, buy_signals, current_positions,
                        max_signals=None):
    """为新买入信号分配仓位预算（按信号建议仓位加权）

    修复要点：
    1. 按 suggested_position_pct 从高到低排序，只取前 max_signals 个信号参与分配
       （避免预算被过多信号摊薄，导致每只分到的钱不够买1手）
    2. 按权重比例分配，受单票上限约束
    3. 返回的分配仅为"目标预算"，实际买入时因100股整数倍会有零头剩余，
       由调用方通过剩余预算再分配逻辑消化

    返回 dict: code -> target_value（目标市值金额）
    仅对"新买入"的股票分配，不包含已有持仓。
    """
    # 信号总数（用于目标仓位计算，反映市场整体热度）
    total_signal_count = len(buy_signals)
    target_total_pct = get_target_total_position_pct(market_state_label, total_signal_count)
    target_total_value = total_value * target_total_pct / 100.0

    # 已有持仓占用的仓位价值
    existing_value = current_position_value

    # 可用的新增仓位预算 = max(目标总仓位 - 已有仓位, 0)，但不能超过可用现金
    available_new_budget = max(target_total_value - existing_value, 0)
    available_new_budget = min(available_new_budget, cash)

    if available_new_budget <= 0 or not buy_signals:
        return {}

    # 按 quality_score（综合质量评分）降序，同评分再按 suggested_position_pct 降序
    sorted_signals = sorted(
        buy_signals,
        key=lambda b: (b.get("quality_score", 0), b.get("suggested_position_pct", 10)),
        reverse=True,
    )
    if max_signals is not None and max_signals > 0:
        selected_signals = sorted_signals[:max_signals]
    else:
        selected_signals = sorted_signals

    # 单票上限
    max_single_value = total_value * SINGLE_STOCK_MAX_PCT / 100.0

    # 计算选中信号的权重（基于 suggested_position_pct）
    total_weight = 0.0
    signal_weights = {}
    for buy in selected_signals:
        suggested = buy.get("suggested_position_pct", 10)
        weight = max(suggested, 5)
        signal_weights[buy["code"]] = weight
        total_weight += weight

    if total_weight <= 0:
        return {}

    # 第一轮分配：按权重比例分配
    allocations = {}
    remaining_budget = available_new_budget
    remaining_weight = total_weight
    capped_codes = set()

    # 迭代分配，处理单票上限溢出
    for _ in range(5):  # 最多迭代5次，足够收敛
        changed = False
        for code, weight in signal_weights.items():
            if code in capped_codes:
                continue
            # 该股票按权重应分得的金额
            share = remaining_budget * weight / remaining_weight
            # 受单票上限约束
            capped_share = min(share, max_single_value)
            if capped_share < share - 0.01:
                # 触发了上限，剩余资金需要重新分配
                allocations[code] = capped_share
                remaining_budget -= capped_share
                remaining_weight -= weight
                capped_codes.add(code)
                changed = True
            else:
                allocations[code] = share

        if not changed:
            break

    # 【优化·稳健】单笔风险敞口上限：买入金额 × 止损距离 ≤ 总资产 × MAX_RISK_PER_TRADE_PCT
    signal_by_code = {b.get("code"): b for b in selected_signals}
    for code in list(allocations.keys()):
        b = signal_by_code.get(code) or {}
        raw_stop_pct = float(b.get("stop_pct") or 0) / 100.0  # 信号端为百分数，转小数
        if raw_stop_pct <= 0:
            raw_stop_pct = MAX_STOP_PCT if MAX_STOP_PCT > 0 else 0.10
        eff_stop = min(raw_stop_pct, MAX_STOP_PCT) if MAX_STOP_PCT > 0 else raw_stop_pct
        if eff_stop <= 0:
            continue
        max_risk_value = total_value * MAX_RISK_PER_TRADE_PCT / eff_stop
        if allocations[code] > max_risk_value:
            allocations[code] = max_risk_value

    # 确保不超过可用现金总额
    total_allocated = sum(allocations.values())
    if total_allocated > available_new_budget + 0.01:
        # 按比例缩放
        scale = available_new_budget / total_allocated
        for code in allocations:
            allocations[code] *= scale

    return allocations


def redistribute_remaining_budget(portfolio, sorted_buys_codes, budget_map,
                                  price_map, purchased_codes, total_value,
                                  target_total_value):
    """剩余预算再分配：把已买入股票因100股整数倍剩下的零头，
    在总目标仓位范围内，按优先级补到已买入的股票上，提高资金利用率。

    核心约束：
    1. 总加仓金额 + 已买入市值 <= 目标总仓位市值（不能突破目标仓位）
    2. 单票加仓后不超过单票上限（SINGLE_STOCK_MAX_PCT）
    3. 只对已买入且分配了预算的股票补差额（不到目标预算的部分）
    4. 如果还有剩余且有空闲slot，可以尝试给下一个优先级信号开小仓

    返回 dict: code -> additional_budget（追加预算金额）
    """
    if not portfolio["cash"] or portfolio["cash"] < 100:
        return {}

    # 已买入市值（统计全部持仓，不限于本轮新买入）
    purchased_value = sum(
        p.get("current_value", p["entry_price"] * p["shares"])
        for p in portfolio["positions"]
    )
    # 还能加仓的总额度 = 目标总仓位 - 已买入市值（不能突破目标）
    total_add_room = max(target_total_value - purchased_value, 0)
    if total_add_room < 100:
        return {}

    # 实际可用于加仓的现金 = min(剩余现金, 目标缺口)
    available_add_cash = min(portfolio["cash"], total_add_room)
    if available_add_cash < 100:
        return {}

    max_single_value = total_value * SINGLE_STOCK_MAX_PCT / 100.0
    additional = {}
    remaining_cash = available_add_cash

    # 第一步：给已买入的股票补差额（补到分配的目标预算为止）
    # 按优先级从高到低
    for code in sorted_buys_codes:
        if remaining_cash < 100:
            break
        if code not in purchased_codes:
            continue
        target_budget = budget_map.get(code, 0)
        if target_budget <= 0:
            continue
        # 找到当前持仓
        pos = next((p for p in portfolio["positions"] if p["code"] == code), None)
        if not pos:
            continue
        current_val = pos.get("current_value", pos["entry_price"] * pos["shares"])
        # 差额 = 目标预算 - 当前市值（还有多少没买满的）
        gap = target_budget - current_val
        if gap < 100:
            continue
        # 同时受单票上限约束
        single_room = max_single_value - current_val
        add_amount = min(gap, single_room, remaining_cash)
        if add_amount >= 100:
            additional[code] = add_amount
            remaining_cash -= add_amount

    # 第二步：如果还有剩余预算 + 空闲slot，给尚未买入但在排序列表里的高优先级信号开小仓
    # 计算剩余 slot
    used_slots = len(purchased_codes)
    free_slots = MAX_POSITIONS - used_slots
    if remaining_cash >= 100 and free_slots > 0:
        for code in sorted_buys_codes:
            if remaining_cash < 100 or free_slots <= 0:
                break
            if code in purchased_codes or code in additional:
                continue
            price = price_map.get(code, 0)
            if price <= 0:
                continue
            # 至少买1手
            min_cost = price * 100
            if remaining_cash < min_cost:
                continue
            # 新开仓金额不超过单票上限、不超过剩余预算
            add_amount = min(remaining_cash, max_single_value)
            # 再向下取整到100股整数倍（这里只返回预算，调用方会再计算股数）
            additional[code] = add_amount
            remaining_cash -= add_amount
            free_slots -= 1

    return additional


def calc_position_size_from_budget(budget, price):
    """根据预算金额和价格，计算可买股数（100股整数倍，向下取整）"""
    if budget <= 0 or price <= 0:
        return 0
    shares = int(budget / price / 100) * 100
    return max(shares, 0)


def calc_position_size(total_value, suggested_pct, price, available_cash, max_positions=10):
    """兼容旧接口的单股仓位计算（保留用于回测或单股调用）"""
    equal_weight_pct = 100.0 / max_positions
    floor_pct = equal_weight_pct * 0.7
    effective_pct = max(suggested_pct, floor_pct)
    target_value = total_value * effective_pct / 100.0
    max_value = total_value * SINGLE_STOCK_MAX_PCT / 100.0
    target_value = min(target_value, max_value, available_cash)
    shares = int(target_value / price / 100) * 100
    return max(shares, 0)


def find_tiny_positions_to_trim(portfolio, total_value, buy_signal_codes, scan_date=None):
    """找出需要清仓腾位置的极小仓位

    条件：
    1. 当前市值占总市值比例 < TINY_POSITION_PCT
    2. 不在今日买入信号列表中（即没有续持理由）
    3. 持仓天数 >= TINY_MIN_HOLD_DAYS（避免刚买入就被卖）
    4. 当前不亏损（亏损的极小仓位继续持有等反弹，不割肉腾位）
    """
    tiny_codes = []
    threshold = total_value * TINY_POSITION_PCT / 100.0
    for pos in portfolio["positions"]:
        cur_val = pos.get("current_value", pos["entry_price"] * pos["shares"])
        if cur_val >= threshold:
            continue
        if pos["code"] in buy_signal_codes:
            continue
        # 持仓天数保护
        hold_days = pos.get("hold_days", 0)
        if hold_days < TINY_MIN_HOLD_DAYS:
            continue
        # 不割肉：当前价低于成本价时不卖（等反弹或触发止损）
        current_price = pos.get("current_price", pos["entry_price"])
        if current_price < pos["entry_price"]:
            continue
        tiny_codes.append(pos["code"])
    return tiny_codes


# ===================== 风控：账户级回撤熔断 =====================
def _is_circuit_blocking(portfolio, today_str):
    """熔断停止期内（circuit_stop_until 之前，含当日）禁止开新仓"""
    stop_until = portfolio.get("circuit_stop_until")
    if not stop_until:
        return False
    try:
        return today_str <= stop_until
    except Exception:
        return False


def check_circuit_breaker(portfolio, today_str):
    """账户级回撤熔断：在当日卖出执行完毕、买入之前调用。

    - 回撤 >= CIRCUIT_DD_STOP (25%)：清仓所有持仓，并设置 circuit_stop_until = 当日+30自然日
    - 回撤 >= CIRCUIT_DD_HALVE (15%)：每个持仓卖出一半（100股整数倍向下取整；
      不足100股的零头全部卖出）。同一 peak_equity 只触发一次减半。

    影子盘按统一交易成本/滑点口径成交；主盘保持历史的原价成交。
    返回 (trades, action) — action ∈ {"stop","halve","none"}
    """
    equity_today = _portfolio_market_value(portfolio)

    # 更新峰值（持久化到 portfolio.json）
    prev_peak = portfolio.get("peak_equity") or portfolio.get("initial_capital", INITIAL_CAPITAL)
    if equity_today > prev_peak:
        portfolio["peak_equity"] = round(equity_today, 2)
    else:
        portfolio["peak_equity"] = round(prev_peak, 2)
    peak = portfolio["peak_equity"]

    dd_now = (equity_today - peak) / peak if peak > 0 else 0.0
    trades = []

    def _mk_sell_trade(pos, sell_shares, price, reason):
        fill = _fill_metadata(price, sell_shares, "sell")
        alloc_cost = _allocated_cost(pos, sell_shares)
        pnl = fill["amount"] - alloc_cost
        pnl_pct = pnl / alloc_cost * 100 if alloc_cost > 0 else 0
        return {
            "date": today_str,
            "code": pos["code"],
            "name": pos.get("name", ""),
            "action": "SELL",
            **fill,
            "shares": int(sell_shares),
            "reason": reason,
            "cost_basis": round(alloc_cost, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "hold_days": pos.get("hold_days", 0),
            "entry_price": pos.get("entry_price", price),
        }

    # 触发清仓熔断
    if dd_now <= -CIRCUIT_DD_STOP and portfolio["positions"]:
        print(f"\n  [熔断清仓] 当前回撤 {dd_now*100:.2f}% <= -{CIRCUIT_DD_STOP*100:.0f}%，"
              f"清仓 {len(portfolio['positions'])} 只持仓，停止开新仓30天")
        to_remove = []
        for pos in list(portfolio["positions"]):
            price = pos.get("current_price", pos.get("entry_price", 0))
            if price <= 0:
                continue
            sell_shares = pos["shares"]
            t = _mk_sell_trade(pos, sell_shares, price,
                               f"熔断清仓(DD{dd_now*100:.1f}%)")
            trades.append(t)
            append_trade_log(t)
            portfolio["cash"] = round(portfolio["cash"] + t["amount"], 2)
            to_remove.append(pos["code"])
            print(f"    [清仓] {pos['code']} @ {price} × {sell_shares}股 "
                  f"回收¥{t['amount']:,.0f}")
        portfolio["positions"] = [p for p in portfolio["positions"] if p["code"] not in to_remove]

        # 停止至当日+30个自然日
        from datetime import datetime, timedelta
        try:
            d = datetime.strptime(today_str, "%Y-%m-%d").date()
        except Exception:
            d = datetime.now().date()
        stop_until = d + timedelta(days=30)
        portfolio["circuit_stop_until"] = stop_until.strftime("%Y-%m-%d")
        # 清仓后峰值保持不变，但允许未来重新触发减半
        return trades, "stop"

    # 触发减半熔断（同一 peak_equity 只减半一次）
    halved_peak = portfolio.get("halved_peak")
    if (dd_now <= -CIRCUIT_DD_HALVE and portfolio["positions"]
            and halved_peak != peak):
        print(f"\n  [熔断减半] 当前回撤 {dd_now*100:.2f}% <= -{CIRCUIT_DD_HALVE*100:.0f}%，"
              f"持仓减半（100股整数倍；不足100股全部卖出）")
        portfolio["halved_peak"] = round(peak, 2)
        to_remove = []
        for pos in list(portfolio["positions"]):
            price = pos.get("current_price", pos.get("entry_price", 0))
            if price <= 0:
                continue
            orig_shares = pos["shares"]
            half_lot = (orig_shares // 2 // 100) * 100
            if half_lot >= 100:
                sell_shares = half_lot
            else:
                # 不足100股的持仓全部卖出（与回测引擎行为不同：回测直接跳过，
                # 模拟交易按用户明确要求"不足100股全部卖出"）
                if orig_shares < 200:
                    sell_shares = orig_shares  # 全部清掉
                else:
                    continue  # 理论不会走到
            t = _mk_sell_trade(pos, sell_shares, price,
                               f"熔断减半(DD{dd_now*100:.1f}%)")
            trades.append(t)
            append_trade_log(t)
            portfolio["cash"] = round(portfolio["cash"] + t["amount"], 2)

            remaining = orig_shares - sell_shares
            if remaining <= 0:
                to_remove.append(pos["code"])
                print(f"    [减半→清仓] {pos['code']} @ {price} × {sell_shares}股 "
                      f"(剩余{remaining}股) 回收¥{t['amount']:,.0f}")
            else:
                # 按比例缩减成本、更新市值
                old_cost = pos.get("cost", price * orig_shares)
                pos["shares"] = remaining
                pos["cost"] = round(old_cost * (remaining / orig_shares), 2)
                pos["current_value"] = round(price * remaining, 2)
                pos["pnl"] = round(pos["current_value"] - pos.get("cost", 0), 2)
                pos["pnl_pct"] = round(pos["pnl"] / pos["cost"] * 100, 2) if pos.get("cost", 0) > 0 else 0
                print(f"    [减半] {pos['code']} @ {t['price']} 卖出{sell_shares}股 "
                      f"剩{remaining}股 回收¥{t['amount']:,.0f}")
        if to_remove:
            portfolio["positions"] = [p for p in portfolio["positions"] if p["code"] not in to_remove]
        return trades, "halve"

    return [], "none"


def check_industry_concentration(portfolio, code, est_buy_value, total_value):
    """新买入前行业集中度检查。

    若买入 code 后，其所属行业持仓市值/总市值 > INDUSTRY_MAX_PCT 则拦截。
    返回 (ok, reason)
    """
    industry = _get_industry(code)
    if industry == "其他":
        return True, ""  # 未在映射表中的股票不限制（与回测一致）
    cur_value, cur_pct = _calc_industry_exposure(portfolio, industry, total_value)
    if total_value <= 0:
        return True, ""
    new_pct = (cur_value + est_buy_value) / total_value
    if new_pct > INDUSTRY_MAX_PCT:
        reason = (f"行业集中度拦截: {industry} 现有占比{cur_pct*100:.1f}% + "
                  f"新买入占比{est_buy_value/total_value*100:.1f}% = "
                  f"{new_pct*100:.1f}% > 上限{INDUSTRY_MAX_PCT*100:.0f}%")
        return False, reason
    return True, ""


def update_portfolio_stats(portfolio):
    """更新投资组合统计数据"""
    positions_value = sum(
        p.get("current_value", p["entry_price"] * p["shares"])
        for p in portfolio["positions"]
    )
    total_value = portfolio["cash"] + positions_value
    initial = portfolio["initial_capital"]
    total_pnl = total_value - initial
    total_pnl_pct = total_pnl / initial * 100 if initial > 0 else 0

    portfolio["total_value"] = round(total_value, 2)
    portfolio["total_pnl"] = round(total_pnl, 2)
    portfolio["total_pnl_pct"] = round(total_pnl_pct, 2)

    # 最大回撤
    current_max = max(portfolio.get("max_value", initial), total_value)
    portfolio["max_value"] = current_max
    dd = (total_value - current_max) / current_max * 100 if current_max > 0 else 0
    portfolio["max_drawdown"] = round(min(portfolio.get("max_drawdown", 0), dd), 2)

    # 【风控新增】同步峰值权益（peak_equity），熔断逻辑以该值为基准；
    # 即使当日未触发熔断，也保持 peak_equity 与 max_value 一致
    prev_peak = portfolio.get("peak_equity") or initial
    portfolio["peak_equity"] = round(max(prev_peak, total_value), 2)

    # 影子候选与配对基准使用同一口径计算日收益和夏普。
    history = portfolio.setdefault("equity_history", [])
    day = str(portfolio.get("current_date") or portfolio.get("last_processed_date") or datetime.now().date())[:10]
    point = {"date": day, "total_value": round(total_value, 2)}
    if history and history[-1].get("date") == day:
        history[-1] = point
    else:
        history.append(point)
    portfolio["equity_history"] = history[-500:]


# ===================== 主流程 =====================
async def main():
    result_mode = sys.argv[1] if len(sys.argv) > 1 else "notify"
    print(f"[参数] result_mode={result_mode}")

    # 交易日判断：读标记文件
    _today_str = time.strftime('%Y-%m-%d')
    _flag_dir = os.environ.get("SIM_TRADING_DAY_FLAG_DIR", os.path.join(BASE_DIR, "data"))
    _flag_file = os.path.join(_flag_dir, f"not_trading_day_{_today_str}.txt")
    # 影子模式允许使用当天已生成的只读信号副本继续完成验证；主盘仍严格遵守非交易日标记。
    _shadow_has_today_signal = False
    if SHADOW_MODE and os.path.exists(SIGNALS_FILE):
        try:
            with open(SIGNALS_FILE, "r", encoding="utf-8") as _sf:
                _shadow_has_today_signal = json.load(_sf).get("scan_date") == _today_str
        except Exception:
            _shadow_has_today_signal = False
    if os.path.exists(_flag_file) and not _shadow_has_today_signal:
        print(f"[跳过] 今天 {_today_str} 非交易日，模拟交易跳过")
        return

    sdk = CodeActSDK()

    try:
        # 检查信号文件
        if not os.path.exists(SIGNALS_FILE):
            print("[模拟交易] 无信号文件，跳过")
            await sdk.submit_result(
                result_mode="no_reply",
                status="success",
                message="NO_REPLY",
                data={},
            )
            return

        with open(SIGNALS_FILE, "r", encoding="utf-8") as f:
            signals = json.load(f)

        scan_date = signals.get("scan_date", "")
        buy_signals = signals.get("buy_signals", [])
        sell_signals = signals.get("sell_signals", [])
        market_state_label = signals.get("market_state", "unknown")

        # 日期验证：确保信号文件是今天的，防止读到前一天的旧数据
        today = time.strftime('%Y-%m-%d')
        if scan_date != today:
            print(f"[警告] 信号文件日期不匹配：文件={scan_date}，今天={today}。可能信号扫描尚未完成或失败。")
            # 检查是否为非交易日（有标记文件则正常跳过）
            _flag_file_today = os.path.join(_flag_dir, f"not_trading_day_{today}.txt")
            if not os.path.exists(_flag_file_today):
                # 交易日但信号不是今天的，跳过执行避免用旧数据交易
                print(f"[跳过] 今天 {today} 是交易日，但信号文件不是今日数据，跳过模拟交易")
                await sdk.submit_result(
                    result_mode="no_reply",
                    status="success",
                    message="NO_REPLY",
                    data={"reason": "signal_date_mismatch", "signal_date": scan_date, "today": today},
                )
                return
            else:
                # 非交易日，正常跳过
                print(f"[跳过] 今天 {today} 非交易日，且信号文件日期不匹配，跳过")
                await sdk.submit_result(
                    result_mode="no_reply",
                    status="success",
                    message="NO_REPLY",
                    data={"reason": "non_trading_day_date_mismatch", "signal_date": scan_date, "today": today},
                )
                return

        print(f"[模拟交易] 扫描日期: {scan_date}")
        print(f"[模拟交易] 市场状态: {market_state_label}")
        print(f"[模拟交易] 买入信号: {len(buy_signals)}, 卖出信号: {len(sell_signals)}")

        # 加载投资组合
        portfolio = load_portfolio()

        # 启动时自动补全缺失的持仓名称（老数据无 name 字段）
        try:
            missing = [p for p in portfolio["positions"] if not p.get("name")]
            if missing:
                print(f"[名称补全] 发现 {len(missing)} 只持仓缺失名称，开始补全...")
                filled = backfill_position_names(portfolio)
                if filled > 0:
                    save_portfolio(portfolio)
                    print(f"[名称补全] 已补全 {filled} 只持仓名称并保存")
        except Exception as e:
            print(f"[名称补全] 补全过程异常(不影响主流程): {e}")

        # 防止重复处理同一天（设置 SIM_TRADE_RERUN=1 可强制重跑，用于回测/验证新逻辑）
        if portfolio.get("last_processed_date") == scan_date:
            if os.environ.get("SIM_TRADE_RERUN", "").strip() == "1":
                print(f"[模拟交易] {scan_date} 已处理过，但 SIM_TRADE_RERUN=1 强制重跑模式")
                # 重跑模式：根据当日交易记录精确回滚到处理前状态
                today_trades = [
                    t for t in portfolio["trade_history"]
                    if t.get("date") == scan_date
                ]
                if today_trades:
                    # 回滚每一笔交易
                    # 收集需要恢复的持仓（被卖出的）
                    sold_positions = {}
                    for t in today_trades:
                        if t["action"] == "SELL":
                            # 卖出回滚：需要重新加回持仓，扣除收回的现金
                            sold_positions.setdefault(t["code"], []).append(t)
                        elif t["action"] in ("BUY", "BUY_ADD"):
                            # 买入回滚：从持仓中减去，加回现金
                            pos = next((p for p in portfolio["positions"] if p["code"] == t["code"]), None)
                            if pos:
                                if t["action"] == "BUY":
                                    # 完全移除
                                    portfolio["positions"] = [
                                        p for p in portfolio["positions"]
                                        if p["code"] != t["code"]
                                    ]
                                else:
                                    # 加仓回滚：减股数、减成本
                                    pos["shares"] -= t["shares"]
                                    if pos["shares"] <= 0:
                                        portfolio["positions"] = [
                                            p for p in portfolio["positions"]
                                            if p["code"] != t["code"]
                                        ]
                                    else:
                                        pos["cost"] = round(pos["cost"] - t["amount"], 2)
                                        pos["entry_price"] = round(pos["cost"] / pos["shares"], 2)
                                        pos["current_value"] = round(pos.get("current_price", pos["entry_price"]) * pos["shares"], 2)
                                portfolio["cash"] = round(portfolio["cash"] + t["amount"], 2)

                    # 回滚卖出：把持仓加回来，现金减去卖出收入
                    # 对于被卖出的股票，需要从卖出交易记录中重建持仓信息
                    # 由于信息不完整，我们基于交易记录做近似恢复
                    for code, sells in sold_positions.items():
                        # 累计卖出的股数和金额
                        total_shares = sum(s["shares"] for s in sells)
                        total_amount = sum(s["amount"] for s in sells)
                        entry_price = sells[0].get("entry_price", sells[0]["price"])
                        # 估算原持仓成本（用 entry_price × 股数）
                        original_cost = round(sum(
                            s.get("cost_basis", entry_price * s["shares"])
                            for s in sells
                        ), 2)
                        # 从现金中扣除卖出收入
                        portfolio["cash"] = round(portfolio["cash"] - total_amount, 2)
                        # 重新加入持仓
                        portfolio["positions"].append({
                            "code": code,
                            "name": get_stock_name(code),
                            "entry_date": f"before_{scan_date}",
                            "entry_price": entry_price,
                            "shares": total_shares,
                            "stop_loss": sells[0].get("stop_loss", round(entry_price * 0.95, 2)),
                            "take_profit": sells[0].get("take_profit", round(entry_price * 1.4, 2)),
                            "cost": round(original_cost, 2),
                            "current_price": sells[0].get("quote_price", sells[0]["price"]),
                            "current_value": round(sells[0].get("quote_price", sells[0]["price"]) * total_shares, 2),
                            "pnl": round(
                                sells[0].get("quote_price", sells[0]["price"]) * total_shares - original_cost, 2
                            ),
                            "pnl_pct": round(
                                (sells[0].get("quote_price", sells[0]["price"]) * total_shares - original_cost)
                                / original_cost * 100, 2
                            ) if original_cost > 0 else 0,
                            "hold_days": sells[0].get("hold_days", 0),
                            "suggested_position_pct": sells[0].get("suggested_position_pct", 10),
                            "take_profit_pct": 40,
                        })

                    # 清除当日交易记录
                    portfolio["trade_history"] = [
                        t for t in portfolio["trade_history"]
                        if t.get("date") != scan_date
                    ]

                    # 回滚 hold_days 计数（所有持仓的 hold_days -1，因为今天还没处理）
                    for pos in portfolio["positions"]:
                        if pos.get("hold_days", 0) > 0:
                            pos["hold_days"] -= 1

                    print(f"  已回滚 {scan_date} 的 {len(today_trades)} 笔交易，准备重跑")
                    print(f"  回滚后现金: ¥{portfolio['cash']:,.0f}, 持仓数: {len(portfolio['positions'])}")
                else:
                    print(f"  当日无交易记录，直接重跑")

                # 重置 last_processed_date
                portfolio["last_processed_date"] = None
            else:
                print(f"[模拟交易] {scan_date} 已处理过，跳过")
                await sdk.submit_result(
                    result_mode="no_reply",
                    status="success",
                    message="NO_REPLY",
                    data={},
                )
                return

        if portfolio["start_date"] is None:
            portfolio["start_date"] = scan_date
        portfolio["current_date"] = scan_date
        portfolio["last_processed_date"] = scan_date

        trades_today = []

        # ========== Step 1: 检查持仓止损/分段止盈 ==========
        # 分段止盈逻辑（对齐主策略v6_optimized.py 1530-1590行，经回测验证）：
        # 1) 动态止损上移：浮盈>8%移到成本价（保本），浮盈>触发线(强20%/中15%/弱10%)移到MA20×0.98
        # 2) 部分止盈：浮盈>40%且未部分卖出过，卖出R²分档比例(15%/25%/40%)，100股整数倍向下取整
        # 3) 止损/阶梯止盈触及：全部卖出；海龟退出信号：非强趋势锁仓时全部卖出
        # 影子盘扣除统一交易成本；主盘保持历史原价成交；不足100股零头全部卖出。
        print("\n[Step1] 检查持仓止损/分段止盈...")
        for pos in portfolio["positions"]:
            ohlc = fetch_latest_ohlc(pos["code"], expected_date=scan_date)
            if ohlc is None:
                print(f"  [跳过] {pos['code']} 无法获取当日价格或日期不匹配")
                pos["hold_days"] = pos.get("hold_days", 0) + 1
                continue

            current_price = ohlc["close"]
            day_low = ohlc["low"]
            day_high = ohlc["high"]
            entry_price = pos["entry_price"]
            shares = pos["shares"]

            # 获取分段止盈状态指标（MA20/MA60/趋势强度/R²等）
            tp_state = calc_segmented_tp_state(pos["code"], current_price, day_high, day_low)
            ma20 = tp_state["ma20"]
            ma60 = tp_state["ma60"]
            ma20_slope = tp_state["ma20_slope"]
            tier_trigger = tp_state["tier_trigger"]
            tp_sell_pct = tp_state["tp_sell_pct"]
            trend_lock = tp_state["trend_lock"]

            # v6 影子候选：对长期横盘/趋势失效仓位提供退出保护。
            # 主模拟盘 SHADOW_MODE=False 时完全不启用，历史持仓不被改写。
            candidate_exit_reason = ""
            if SHADOW_MODE and MAX_HOLD_DAYS > 0:
                if pos.get("hold_days", 0) >= MAX_HOLD_DAYS and current_price <= entry_price * 1.05:
                    candidate_exit_reason = f"影子候选-持仓超时({MAX_HOLD_DAYS}天)"
                elif (current_price < ma20 and tp_state["trend_strength"] < TREND_WEAK_TH):
                    candidate_exit_reason = "影子候选-趋势失效"

            pnl_pct_ratio = (current_price - entry_price) / entry_price if entry_price > 0 else 0

            # === 动态调整止损线（只上移不下移） ===
            # 注意：当日上移的止损线在当天不触发（避免日内close恰好使pnl越过阈值、
            # 止损线移到close附近而当天low就触及的"自触发"问题）。次日开始生效。
            old_stop = pos["stop_loss"]
            stop_moved_today = False
            if pnl_pct_ratio > tier_trigger:
                tier_stop = round(ma20 * MA20_TIER_FACTOR, 2)
                tier_stop = max(tier_stop, round(entry_price, 2))
                if tier_stop > pos["stop_loss"]:
                    pos["stop_loss"] = tier_stop
                    stop_moved_today = True
            elif pnl_pct_ratio > BREAKEVEN_TRIGGER:
                if entry_price > pos["stop_loss"]:
                    pos["stop_loss"] = round(entry_price, 2)
                    stop_moved_today = True
            # pnl_pct_ratio <= BREAKEVEN_TRIGGER: 保持原止损

            if pos["stop_loss"] != old_stop:
                print(f"  [止损上移] {pos['code']} {old_stop:.2f} → {pos['stop_loss']:.2f} "
                      f"(浮盈{pnl_pct_ratio*100:.1f}%, MA20={ma20:.2f}, 次日生效)")

            # === 卖出逻辑判断 ===
            sell_action = None  # "full" | "partial"
            sell_price = current_price
            sell_reason = ""
            sell_shares = shares

            if candidate_exit_reason:
                sell_action = "full"
                sell_reason = candidate_exit_reason

            # 1) 止损/阶梯止盈（日内最低价触及）→ 全部卖出
            #    当日上移的止损线当天不触发，次日生效
            if not stop_moved_today and day_low <= pos["stop_loss"]:
                sell_action = "full"
                sell_price = pos["stop_loss"]
                if pnl_pct_ratio > tier_trigger:
                    sell_reason = f"阶梯止盈(MA20×0.98={pos['stop_loss']:.2f})"
                elif pnl_pct_ratio > BREAKEVEN_TRIGGER:
                    sell_reason = f"保本止损({pos['stop_loss']:.2f})"
                else:
                    sell_reason = f"止损触发(止损线{pos['stop_loss']:.2f})"

            # 1.5) 固定止盈（日内最高价触及take_profit）→ 全部卖出
            #      仅在止损线尚未上移到止盈线之上时生效；若阶梯止损已超过止盈线，
            #      说明系统在让利润奔跑，固定止盈已被阶梯止盈取代。
            #      优先级：止损 > 固定止盈 > 部分止盈(+40%)
            if sell_action is None:
                take_profit = pos.get("take_profit", 0)
                if (take_profit and take_profit > entry_price
                        and pos["stop_loss"] < take_profit
                        and day_high >= take_profit):
                    sell_action = "full"
                    sell_price = take_profit
                    sell_reason = f"固定止盈(止盈线{take_profit:.2f})"

            # 2) 部分止盈（浮盈>40%且未部分卖出过）
            partial_sold = pos.get("partial_sold", False)
            if sell_action is None and not partial_sold and pnl_pct_ratio > TP_THRESHOLD:
                partial_shares = int(shares * tp_sell_pct)
                partial_shares = (partial_shares // 100) * 100  # 100股整数倍向下取整
                if partial_shares >= 100:
                    # 检查卖出后剩余是否不足100股，若是则全部卖出
                    remaining = shares - partial_shares
                    if remaining < 100:
                        sell_action = "full"
                        sell_shares = shares
                        sell_price = current_price
                        sell_reason = (f"部分止盈+零头清仓(卖{int(tp_sell_pct*100)}%后余{remaining}股<100)")
                    else:
                        sell_action = "partial"
                        sell_shares = partial_shares
                        sell_price = current_price
                        sell_reason = f"部分止盈(卖{int(tp_sell_pct*100)}%)"

            # === 执行卖出 ===
            if sell_action is not None and sell_shares > 0:
                if sell_shares > shares:
                    sell_shares = shares
                fill = _fill_metadata(sell_price, sell_shares, "sell")
                alloc_cost = _allocated_cost(pos, sell_shares)
                pnl = fill["amount"] - alloc_cost
                pnl_pct = pnl / alloc_cost * 100 if alloc_cost > 0 else 0
                trade = {
                    "date": scan_date,
                    "code": pos["code"],
                    "name": pos.get("name", ""),
                    "action": "SELL",
                    **fill,
                    "shares": sell_shares,
                    "reason": sell_reason,
                    "cost_basis": round(alloc_cost, 2),
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 2),
                    "hold_days": pos.get("hold_days", 0) + 1,
                    "entry_price": entry_price,
                }
                trades_today.append(trade)
                append_trade_log(trade)
                portfolio["cash"] = round(portfolio["cash"] + fill["amount"], 2)

                if sell_action == "full":
                    pos["_to_remove"] = True
                    print(f"  [全部卖出] {pos['code']} @ {sell_price:.2f} × {sell_shares}股 "
                          f"盈亏:{pnl:.2f} ({pnl_pct:.1f}%) 原因:{sell_reason}")
                else:
                    # 部分卖出：更新持仓
                    old_shares = shares
                    pos["shares"] = shares - sell_shares
                    # 按比例缩减成本
                    cost_ratio = sell_shares / old_shares
                    pos["cost"] = round(pos["cost"] - alloc_cost, 2)
                    pos["partial_sold"] = True
                    pos["current_value"] = round(current_price * pos["shares"], 2)
                    pos["pnl"] = round(pos["current_value"] - pos["cost"], 2)
                    pos["pnl_pct"] = round(pos["pnl"] / pos["cost"] * 100, 2) if pos["cost"] > 0 else 0
                    print(f"  [部分卖出] {pos['code']} @ {sell_price:.2f} × {sell_shares}股 "
                          f"(余{pos['shares']}股) 盈亏:{pnl:.2f} ({pnl_pct:.1f}%) 原因:{sell_reason}")
                continue

            # 更新当前价格和浮盈
            pos["current_price"] = current_price
            pos["current_value"] = round(current_price * pos["shares"], 2)
            pos["pnl"] = round(pos["current_value"] - pos.get("cost", 0), 2)
            pos["pnl_pct"] = round(pos["pnl"] / pos["cost"] * 100, 2) if pos.get("cost", 0) > 0 else 0
            pos["hold_days"] = pos.get("hold_days", 0) + 1

        # ========== Step 2: 处理卖出信号（强趋势锁仓时屏蔽） ==========
        print("\n[Step2] 处理卖出信号...")
        sell_codes = {s["code"] for s in sell_signals}
        for pos in portfolio["positions"]:
            if pos.get("_to_remove"):
                continue
            if pos["code"] in sell_codes:
                sell_info = next(s for s in sell_signals if s["code"] == pos["code"])
                # 直接获取实盘收盘价作为卖出价
                fresh_ohlc = fetch_latest_ohlc(pos["code"], expected_date=scan_date)
                if fresh_ohlc is None:
                    print(f"  [跳过卖出] {pos['code']} 无法获取当日实盘价格")
                    continue
                sell_price = fresh_ohlc["close"]

                # 强趋势锁仓：close>MA20>MA60 且 MA20斜率>0.02 时屏蔽海龟退出信号
                tp_state = calc_segmented_tp_state(pos["code"], sell_price, fresh_ohlc["high"], fresh_ohlc["low"])
                if tp_state["trend_lock"]:
                    print(f"  [锁仓跳过] {pos['code']} 强趋势锁仓中 "
                          f"(close={sell_price:.2f}>MA20={tp_state['ma20']:.2f}>MA60={tp_state['ma60']:.2f}, "
                          f"slope={tp_state['ma20_slope']:.4f})，屏蔽卖出信号")
                    continue

                sell_shares = pos["shares"]
                fill = _fill_metadata(sell_price, sell_shares, "sell")
                alloc_cost = _allocated_cost(pos, sell_shares)
                pnl = fill["amount"] - alloc_cost
                pnl_pct = pnl / alloc_cost * 100 if alloc_cost > 0 else 0
                trade = {
                    "date": scan_date,
                    "code": pos["code"],
                    "name": pos.get("name", ""),
                    "action": "SELL",
                    **fill,
                    "shares": sell_shares,
                    "reason": f"卖出信号 ({sell_info.get('reasons', '')})",
                    "cost_basis": round(alloc_cost, 2),
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 2),
                    "hold_days": pos.get("hold_days", 0),
                    "entry_price": pos["entry_price"],
                }
                trades_today.append(trade)
                append_trade_log(trade)
                portfolio["cash"] = round(portfolio["cash"] + fill["amount"], 2)
                pos["_to_remove"] = True
                print(f"  [信号卖出] {pos['code']} @ {sell_price} 盈亏:{pnl:.2f} ({pnl_pct:.1f}%)")

        # 移除已卖出的持仓
        portfolio["positions"] = [p for p in portfolio["positions"] if not p.get("_to_remove")]

        # 重新计算当前持仓价值和总市值（止损/止盈/卖出信号处理后）
        positions_value = sum(
            p.get("current_value", p["entry_price"] * p["shares"])
            for p in portfolio["positions"]
        )
        total_value = portfolio["cash"] + positions_value
        current_position_pct = positions_value / total_value * 100 if total_value > 0 else 0

        buy_signal_codes = {b["code"] for b in buy_signals}

        # ========== Step 2.5: 极小仓位再平衡（清掉占比太小的持仓腾位置） ==========
        print("\n[Step2.5] 极小仓位再平衡...")
        target_total_pct = get_target_total_position_pct(market_state_label, len(buy_signals))
        print(f"  市场状态:{market_state_label} 目标总仓位:{target_total_pct}% "
              f"(当前:{current_position_pct:.1f}%)")

        tiny_codes = find_tiny_positions_to_trim(portfolio, total_value, buy_signal_codes)
        if tiny_codes and buy_signals:
            # 只有当有新的买入信号可以替换时才清极小仓位，避免无端减仓
            for pos in list(portfolio["positions"]):
                if pos["code"] in tiny_codes:
                    # 使用当前价卖出
                    current_price = pos.get("current_price", pos["entry_price"])
                    sell_shares = pos["shares"]
                    fill = _fill_metadata(current_price, sell_shares, "sell")
                    alloc_cost = _allocated_cost(pos, sell_shares)
                    pnl = fill["amount"] - alloc_cost
                    pnl_pct = pnl / alloc_cost * 100 if alloc_cost > 0 else 0
                    trade = {
                        "date": scan_date,
                        "code": pos["code"],
                        "name": pos.get("name", ""),
                        "action": "SELL",
                        **fill,
                        "shares": sell_shares,
                        "reason": f"再平衡-极小仓位腾位 (占比<{TINY_POSITION_PCT}%)",
                        "cost_basis": round(alloc_cost, 2),
                        "pnl": round(pnl, 2),
                        "pnl_pct": round(pnl_pct, 2),
                        "hold_days": pos.get("hold_days", 0),
                        "entry_price": pos["entry_price"],
                    }
                    trades_today.append(trade)
                    append_trade_log(trade)
                    portfolio["cash"] = round(portfolio["cash"] + fill["amount"], 2)
                    portfolio["positions"].remove(pos)
                    print(f"  [再平衡卖出] {pos['code']} @ {current_price:.2f} "
                          f"盈亏:{pnl:.2f} ({pnl_pct:.1f}%) — 腾位腾资金")

            # 重新计算
            positions_value = sum(
                p.get("current_value", p["entry_price"] * p["shares"])
                for p in portfolio["positions"]
            )
            total_value = portfolio["cash"] + positions_value
        elif not tiny_codes:
            print("  无需要清理的极小仓位")
        else:
            print("  有极小仓位但无新买入信号，暂不清理")

        # ========== Step 2.6: 账户级回撤熔断（卖出完成后、买入之前） ==========
        print("\n[Step2.6] 账户级回撤熔断检查...")
        # 确保旧组合（无 peak_equity 字段）平滑迁移
        if "peak_equity" not in portfolio or portfolio["peak_equity"] is None:
            portfolio["peak_equity"] = max(
                portfolio.get("max_value", portfolio["initial_capital"]),
                total_value,
            )
        circuit_trades, circuit_action = check_circuit_breaker(portfolio, scan_date)
        if circuit_trades:
            trades_today.extend(circuit_trades)
            # 熔断减半/清仓后，重算持仓价值和总市值
            positions_value = sum(
                p.get("current_value", p["entry_price"] * p["shares"])
                for p in portfolio["positions"]
            )
            total_value = portfolio["cash"] + positions_value
            current_position_pct = positions_value / total_value * 100 if total_value > 0 else 0
        if circuit_action == "stop":
            print(f"  [熔断] 已清仓，停止开新仓至 {portfolio.get('circuit_stop_until')}")
        elif circuit_action == "halve":
            print(f"  [熔断] 已减半持仓（halved_peak={portfolio.get('halved_peak')}）")
        else:
            print(f"  [熔断] 未触发（peak={portfolio.get('peak_equity'):.0f}, "
                  f"当前权益={total_value:.0f}）")

        # 检查熔断停止期
        circuit_block_buy = _is_circuit_blocking(portfolio, scan_date)
        if circuit_block_buy:
            print(f"  [熔断] 停止开新仓期内（至 {portfolio.get('circuit_stop_until')}），"
                  f"跳过所有买入信号")

        # ========== Step 3: 处理买入信号（动态预算分配） ==========
        print("\n[Step3] 处理买入信号（动态仓位分配）...")
        current_held = {p["code"] for p in portfolio["positions"]}
        available_slots = MAX_POSITIONS - len(portfolio["positions"])

        # 熔断停止期内禁止任何买入（含新买入、剩余预算加仓/新开仓）
        if circuit_block_buy:
            print("  [熔断停止期] 跳过 Step3 买入流程")
            new_buy_signals = []
        else:
            # 筛选出真正"新买入"的信号（排除已有持仓）
            new_buy_signals = [b for b in buy_signals if b["code"] not in current_held]

        if not new_buy_signals:
            if not circuit_block_buy:
                print("  无新增买入信号")
        else:
            # 按信号建议仓位加权分配买入预算
            # 关键修复：只取前 available_slots 个高优先级信号参与预算分配，
            # 避免预算被过多信号摊薄导致每只钱太少
            budget_map = allocate_buy_budget(
                total_value, positions_value, portfolio["cash"],
                market_state_label, new_buy_signals, portfolio["positions"],
                max_signals=available_slots,
            )
            target_pct = get_target_total_position_pct(market_state_label, len(buy_signals))
            print(f"  目标总仓位:{target_pct}% 可分配预算:¥{sum(budget_map.values()):,.0f} "
                  f"(可用slot:{available_slots}/{MAX_POSITIONS}, 参与分配信号:{len(budget_map)}/{len(new_buy_signals)})")

            # 按 quality_score 降序排序（与推送排序一致），同评分再按预算从高到低
            sorted_buys = sorted(
                new_buy_signals,
                key=lambda b: (b.get("quality_score", 0), budget_map.get(b["code"], 0)),
                reverse=True,
            )
            sorted_buy_codes = [b["code"] for b in sorted_buys]

            # 预先获取所有候选买入标的的价格（用于后续剩余预算再分配）
            price_map = {}
            for buy in sorted_buys:
                fresh_ohlc = fetch_latest_ohlc(buy["code"], expected_date=scan_date)
                if fresh_ohlc:
                    price_map[buy["code"]] = fresh_ohlc["close"]

            purchased_codes = set()

            # ---- 第一轮：按分配预算买入 ----
            print("\n  --- 第一轮买入（按分配预算）---")
            for buy in sorted_buys:
                if available_slots <= 0:
                    print(f"  [跳过] 持仓已满（{MAX_POSITIONS}/{MAX_POSITIONS}）")
                    break

                code = buy["code"]
                budget = budget_map.get(code, 0)
                if budget <= 0:
                    print(f"  [跳过] {code} 分配预算为0")
                    continue

                price = price_map.get(code, 0)
                if price <= 0:
                    print(f"  [跳过] {code} 无法获取当日实盘价格")
                    continue

                shares = calc_position_size_from_budget(budget, price)

                if shares < 100:
                    print(f"  [跳过] {code} 预算¥{budget:,.0f}不足买1手（股价{price}）")
                    continue

                # 实际花费不能超过可用现金（影子盘含滑点、佣金）
                fill = _fill_metadata(price, shares, "buy")
                cost = fill["amount"]
                if cost > portfolio["cash"]:
                    print(f"  [跳过] {code} 成本¥{cost:,.0f} > 可用现金¥{portfolio['cash']:,.0f}")
                    continue

                # 【风控】行业集中度检查：买入后该行业总仓位占比不得超过 INDUSTRY_MAX_PCT
                _total_now = _portfolio_market_value(portfolio)
                _ok_ind, _ind_reason = check_industry_concentration(
                    portfolio, code, cost, _total_now
                )
                if not _ok_ind:
                    print(f"  [跳过] {code} {_ind_reason}")
                    continue

                suggested_pct = buy.get("suggested_position_pct", 10)
                entry_price = fill["price"]
                stop_loss = bounded_stop_loss(entry_price, buy.get("stop_loss", price * 0.95))
                take_profit_pct = buy.get("take_profit_pct", 40)
                take_profit = round(entry_price * (1 + take_profit_pct / 100.0), 2)

                position = {
                    "code": code,
                    "name": get_stock_name(code),
                    "entry_date": scan_date,
                    "entry_price": entry_price,
                    "shares": shares,
                    "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "cost": cost,
                    "current_price": price,
                    "current_value": round(price * shares, 2),
                    "pnl": 0,
                    "pnl_pct": 0,
                    "hold_days": 0,
                    "chan_type": buy.get("chan_type", ""),
                    "atr": buy.get("atr", 0),
                    "stop_pct": buy.get("stop_pct", 0),
                    "take_profit_pct": take_profit_pct,
                    "suggested_position_pct": suggested_pct,
                }

                trade = {
                    "date": scan_date,
                    "code": code,
                    "name": get_stock_name(code),
                    "action": "BUY",
                    **fill,
                    "shares": shares,
                    "reason": f"买入信号 ({buy.get('chan_type', '')})",
                    "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "suggested_position_pct": suggested_pct,
                    "r2_20": buy.get("r2_20", 0),
                }

                portfolio["positions"].append(position)
                portfolio["cash"] = round(portfolio["cash"] - cost, 2)
                trades_today.append(trade)
                append_trade_log(trade)
                available_slots -= 1
                purchased_codes.add(code)
                current_held.add(code)
                actual_pct = cost / total_value * 100
                print(f"  [买入] {code} @ {fill['price']}（行情价{price}）× {shares}股 "
                      f"成本:{cost:.2f} (占总市值{actual_pct:.1f}%) "
                      f"止损:{stop_loss} 止盈:{take_profit}")

            # ---- 第二轮：剩余预算再分配 ----
            # 把因100股整数倍剩下的零头，按优先级尝试加仓或新开仓（不突破目标总仓位）
            print(f"\n  --- 第二轮：剩余预算再分配（剩余现金:¥{portfolio['cash']:,.0f}）---")
            # 重新计算当前市值（第一轮买入后）
            positions_value_after = sum(
                p.get("current_value", p["entry_price"] * p["shares"])
                for p in portfolio["positions"]
            )
            total_value_after = portfolio["cash"] + positions_value_after

            # 目标总仓位市值（基于第一轮买入后的总市值重算，更准确）
            target_total_pct_after = get_target_total_position_pct(
                market_state_label, len(buy_signals)
            )
            target_total_value_after = total_value_after * target_total_pct_after / 100.0

            add_budget_map = redistribute_remaining_budget(
                portfolio, sorted_buy_codes, budget_map,
                price_map, purchased_codes, total_value_after,
                target_total_value=target_total_value_after,
            )

            if add_budget_map:
                # 先处理加仓（已有持仓），再处理新开仓
                for code, add_budget in sorted(
                    add_budget_map.items(),
                    key=lambda x: (x[0] not in purchased_codes, -x[1]),
                ):
                    if available_slots <= 0 and code not in purchased_codes:
                        print(f"  [跳过加仓] {code} 无可用slot")
                        continue
                    if code not in price_map:
                        continue
                    price = price_map[code]
                    add_shares = calc_position_size_from_budget(add_budget, price)
                    if add_shares < 100:
                        continue
                    add_fill = _fill_metadata(price, add_shares, "buy")
                    add_cost = add_fill["amount"]
                    if add_cost > portfolio["cash"]:
                        continue

                    # 【风控】行业集中度检查（第二轮新开仓场景；加仓场景不重复检查）
                    if code not in purchased_codes:
                        _total_now2 = _portfolio_market_value(portfolio)
                        _ok_ind2, _ind_reason2 = check_industry_concentration(
                            portfolio, code, add_cost, _total_now2
                        )
                        if not _ok_ind2:
                            print(f"  [跳过新开仓] {code} {_ind_reason2}")
                            continue

                    if code in purchased_codes:
                        # 加仓已有持仓
                        pos = next(p for p in portfolio["positions"] if p["code"] == code)
                        old_shares = pos["shares"]
                        old_cost = pos["cost"]
                        new_shares = old_shares + add_shares
                        new_cost = old_cost + add_cost
                        avg_price = new_cost / new_shares
                        pos["shares"] = new_shares
                        pos["cost"] = round(new_cost, 2)
                        pos["entry_price"] = round(avg_price, 2)
                        pos["current_value"] = round(price * new_shares, 2)
                        pos["pnl"] = round(pos["current_value"] - new_cost, 2)
                        pos["pnl_pct"] = round(pos["pnl"] / new_cost * 100, 2) if new_cost > 0 else 0
                        # 止损止盈按新均价重新计算
                        pos["stop_loss"] = bounded_stop_loss(avg_price, avg_price * 0.95)
                        pos["take_profit"] = round(avg_price * (1 + pos["take_profit_pct"] / 100.0), 2)
                        # 加仓后重置部分止盈标记（新均价、新仓位，重新允许部分止盈）
                        pos["partial_sold"] = False

                        trade = {
                            "date": scan_date,
                            "code": code,
                            "name": pos.get("name", get_stock_name(code)),
                            "action": "BUY_ADD",
                            **add_fill,
                            "shares": add_shares,
                            "reason": "剩余预算加仓",
                            "stop_loss": pos["stop_loss"],
                            "take_profit": pos["take_profit"],
                            "suggested_position_pct": pos["suggested_position_pct"],
                        }
                        portfolio["cash"] = round(portfolio["cash"] - add_cost, 2)
                        trades_today.append(trade)
                        append_trade_log(trade)
                        actual_pct = pos["current_value"] / total_value_after * 100
                        print(f"  [加仓] {code} @ {add_fill['price']}（行情价{price}） +{add_shares}股 "
                              f"加仓成本:{add_cost:.2f} (现持仓占比{actual_pct:.1f}%)")
                    else:
                        # 新开仓
                        buy_info = next((b for b in sorted_buys if b["code"] == code), None)
                        if not buy_info:
                            continue
                        suggested_pct = buy_info.get("suggested_position_pct", 10)
                        entry_price = add_fill["price"]
                        stop_loss = bounded_stop_loss(entry_price, buy_info.get("stop_loss", price * 0.95))
                        take_profit_pct = buy_info.get("take_profit_pct", 40)
                        take_profit = round(entry_price * (1 + take_profit_pct / 100.0), 2)

                        position = {
                            "code": code,
                            "name": get_stock_name(code),
                            "entry_date": scan_date,
                            "entry_price": entry_price,
                            "shares": add_shares,
                            "stop_loss": stop_loss,
                            "take_profit": take_profit,
                            "cost": add_cost,
                            "current_price": price,
                            "current_value": round(price * add_shares, 2),
                            "pnl": 0,
                            "pnl_pct": 0,
                            "hold_days": 0,
                            "chan_type": buy_info.get("chan_type", ""),
                            "atr": buy_info.get("atr", 0),
                            "stop_pct": buy_info.get("stop_pct", 0),
                            "take_profit_pct": take_profit_pct,
                            "suggested_position_pct": suggested_pct,
                        }
                        trade = {
                            "date": scan_date,
                            "code": code,
                            "name": get_stock_name(code),
                            "action": "BUY",
                            **add_fill,
                            "shares": add_shares,
                            "reason": f"剩余预算新开仓 ({buy_info.get('chan_type', '')})",
                            "stop_loss": stop_loss,
                            "take_profit": take_profit,
                            "suggested_position_pct": suggested_pct,
                            "r2_20": buy_info.get("r2_20", 0),
                        }
                        portfolio["positions"].append(position)
                        portfolio["cash"] = round(portfolio["cash"] - add_cost, 2)
                        trades_today.append(trade)
                        append_trade_log(trade)
                        available_slots -= 1
                        purchased_codes.add(code)
                        current_held.add(code)
                        actual_pct = add_cost / total_value_after * 100
                        print(f"  [新开仓] {code} @ {price} × {add_shares}股 "
                              f"成本:{add_cost:.2f} (占总市值{actual_pct:.1f}%)")
            else:
                print("  无可再分配的剩余预算")

        # ========== Step 4: 更新统计 ==========
        update_portfolio_stats(portfolio)

        # ========== Step 5: 保存 ==========
        # 归档信号
        archive_signals(scan_date)

        # 追加交易记录到历史
        portfolio["trade_history"].extend(trades_today)

        # 保存
        save_portfolio(portfolio)
        save_snapshot(scan_date, portfolio, market_state=market_state_label, buy_count=len(buy_signals), sell_count=len(sell_signals))
        for trade in trades_today:
            save_trade_record(scan_date, trade)
        # 始终保存当日交易文件（即使为空也写入空数组）
        os.makedirs(TRADES_DIR, exist_ok=True)
        trades_file = os.path.join(TRADES_DIR, f"trades_{scan_date}.json")
        if not os.path.exists(trades_file):
            with open(trades_file, "w", encoding="utf-8") as f:
                json.dump([], f, ensure_ascii=False, indent=2)

        # ========== 输出 ==========
        print(f"\n[模拟交易] 处理完成 — {scan_date}")
        print(f"  总市值: ¥{portfolio['total_value']:,.2f} (现金:{portfolio['cash']:,.2f} + 持仓:{portfolio['total_value'] - portfolio['cash']:,.2f})")
        print(f"  总盈亏: ¥{portfolio['total_pnl']:,.2f} ({portfolio['total_pnl_pct']:.2f}%)")
        print(f"  最大回撤: {portfolio['max_drawdown']:.2f}%")
        print(f"  持仓数: {len(portfolio['positions'])}/{MAX_POSITIONS}")
        print(f"  今日交易: {len(trades_today)}笔")
        if portfolio["positions"]:
            print(f"  当前持仓:")
            for p in portfolio["positions"]:
                label = f"{p.get('name', p['code'])}({p['code']})" if p.get("name") else p["code"]
                print(f"    {label} @ {p['entry_price']} 现:{p['current_price']} "
                      f"盈亏:{p['pnl']:,.2f}({p['pnl_pct']:.1f}%) 持{p['hold_days']}天 "
                      f"止损:{p['stop_loss']} 止盈:{p['take_profit']}")

        # 构建每日交易摘要
        buy_count = sum(1 for t in trades_today if t["action"] == "BUY")
        sell_count = sum(1 for t in trades_today if t["action"] == "SELL")
        pnl_val = portfolio["total_pnl"]
        pnl_pct_val = portfolio["total_pnl_pct"]
        sign = "+" if pnl_val >= 0 else "-"
        pnl_str = f"{sign}¥{abs(pnl_val):,.0f}"
        pnl_pct_str = f"{sign}{abs(pnl_pct_val):.2f}%"
        new_buys_str = ", ".join(
            f"{t.get('name', t['code'])}({t['code']})@{t['price']}" if t.get("name") else f"{t['code']}@{t['price']}"
            for t in trades_today if t["action"] == "BUY"
        )

        summary_lines = [
            f"📊 模拟交易日报 {scan_date}",
            f"买入: {buy_count}笔 | 卖出: {sell_count}笔 | 持仓: {len(portfolio['positions'])}/{MAX_POSITIONS}",
            f"总市值: ¥{portfolio['total_value']:,.0f} | 盈亏: {pnl_str} ({pnl_pct_str})",
        ]
        if new_buys_str:
            summary_lines.append(f"新买入: {new_buys_str}")

        # 构建消息
        has_trades = len(trades_today) > 0
        if has_trades:
            lines = list(summary_lines)
            lines.append("")
            lines.append(f"市场状态: {market_state_label} | 最大回撤: {portfolio['max_drawdown']}%")
            for t in trades_today:
                t_label = f"{t.get('name', t['code'])}({t['code']})" if t.get("name") else t["code"]
                if t["action"] == "BUY":
                    lines.append(f"  📗 买入 {t_label} @{t['price']}×{t['shares']}股 成本¥{t['amount']:,.0f}")
                else:
                    emoji = "🔴" if t["pnl"] < 0 else "🟢"
                    lines.append(f"  {emoji} 卖出 {t_label} @{t['price']}×{t['shares']}股 盈亏¥{t['pnl']:,.0f}({t['pnl_pct']:.1f}%)")
            if portfolio["positions"]:
                lines.append("当前持仓:")
                for p in portfolio["positions"]:
                    pnl_emoji = "🟢" if p["pnl"] >= 0 else "🔴"
                    p_label = f"{p.get('name', p['code'])}({p['code']})" if p.get("name") else p["code"]
                    lines.append(f"  {pnl_emoji} {p_label} @{p['current_price']} 浮盈¥{p['pnl']:,.0f}({p['pnl_pct']:.1f}%) 持{p['hold_days']}天")
            message = "\n".join(lines)
        else:
            lines = list(summary_lines)
            lines.append(f"市场状态: {market_state_label} | 最大回撤: {portfolio['max_drawdown']}%")
            message = "\n".join(lines)

        # 根据result_mode决定输出
        actual_mode = result_mode
        if result_mode == "auto":
            actual_mode = "notify" if has_trades else "no_reply"

        if actual_mode == "no_reply":
            final_message = "NO_REPLY"
        else:
            final_message = message

        await sdk.submit_result(
            result_mode=actual_mode,
            status="success",
            message=final_message,
            data={
                "scan_date": scan_date,
                "market_state": market_state_label,
                "total_value": portfolio["total_value"],
                "total_pnl": portfolio["total_pnl"],
                "total_pnl_pct": portfolio["total_pnl_pct"],
                "max_drawdown": portfolio["max_drawdown"],
                "position_count": len(portfolio["positions"]),
                "trades_today": len(trades_today),
                "has_trades": has_trades,
            },
        )

    except Exception as e:
        import traceback
        traceback.print_exc()
        await sdk.submit_result(
            result_mode="notify",
            status="error",
            message=f"模拟交易跟踪执行失败: {str(e)[:200]}",
            data={"error_type": type(e).__name__},
        )


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
