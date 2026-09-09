#!/usr/bin/env python3
"""
烟雾测试：验证 sim_trade_tracker.py 新增的两项风控逻辑
  (A) 行业集中度 <=40% 检查 —— 买入第3只同行业股票应被拦截
  (B) 账户级回撤熔断 —— 回撤16%触发持仓减半

使用临时 PORTFOLIO_DIR，不污染 data/sim_trades/portfolio.json。
不调用网络/SDK，只直接调用风控函数。
"""

import os
import sys
import json
import tempfile
import shutil

# 1) 在 import sim_trade_tracker 之前，把 PORTFOLIO_DIR 重定向到临时目录
TMP_DIR = tempfile.mkdtemp(prefix="sim_trade_smoke_")
os.makedirs(os.path.join(TMP_DIR, "trades"), exist_ok=True)
os.makedirs(os.path.join(TMP_DIR, "snapshots"), exist_ok=True)
os.makedirs(os.path.join(TMP_DIR, "signals_archive"), exist_ok=True)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

# 屏蔽 codeact_sdk（顶层 import 会失败）
import types
fake_sdk = types.ModuleType("codeact_sdk")
class _FakeSDK:
    async def submit_result(self, **kw): pass
fake_sdk.CodeActSDK = _FakeSDK
sys.modules["codeact_sdk"] = fake_sdk

import sim_trade_tracker as stt  # noqa: E402

# 把组合持久化目录改到临时目录，保护真实数据
stt.PORTFOLIO_DIR = TMP_DIR
stt.PORTFOLIO_FILE = os.path.join(TMP_DIR, "portfolio.json")
stt.TRADES_DIR = os.path.join(TMP_DIR, "trades")
stt.SNAPSHOTS_DIR = os.path.join(TMP_DIR, "snapshots")

# 把 append_trade_log 写到临时目录（它内部用的是 PORTFOLIO_DIR，已重定向）

PASS = 0
FAIL = 0

def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ PASS: {msg}")
    else:
        FAIL += 1
        print(f"  ❌ FAIL: {msg}")


def make_position(code, shares, price, cost=None):
    cost = cost if cost is not None else price * shares
    return {
        "code": code,
        "entry_date": "2026-08-01",
        "entry_price": price,
        "shares": shares,
        "stop_loss": round(price * 0.95, 2),
        "take_profit": round(price * 1.4, 2),
        "cost": round(cost, 2),
        "current_price": price,
        "current_value": round(price * shares, 2),
        "pnl": 0.0,
        "pnl_pct": 0.0,
        "hold_days": 5,
        "suggested_position_pct": 10,
        "take_profit_pct": 40,
    }


def make_portfolio(positions, cash=None, initial=1_000_000, peak_equity=None):
    pos_value = sum(p["current_value"] for p in positions)
    if cash is None:
        cash = initial - pos_value
    total_value = cash + pos_value
    return {
        "initial_capital": initial,
        "cash": round(cash, 2),
        "positions": positions,
        "trade_history": [],
        "start_date": "2026-08-01",
        "last_processed_date": None,
        "current_date": "2026-08-16",
        "total_value": round(total_value, 2),
        "total_pnl": round(total_value - initial, 2),
        "total_pnl_pct": round((total_value - initial) / initial * 100, 2),
        "max_value": peak_equity if peak_equity else max(initial, total_value),
        "max_drawdown": 0.0,
        "peak_equity": peak_equity if peak_equity else max(initial, total_value),
        "halved_peak": None,
        "circuit_stop_until": None,
    }


# ============================================================
# Test A: 行业集中度检查
# ============================================================
print("\n" + "=" * 60)
print("Test A: 行业集中度 <=40% 拦截")
print("=" * 60)

# 组合：5只股票，其中2只食品饮料各占25% = 50%；总仓位50%
# 总市值 1,000,000；现金 500,000；持仓市值 500,000
# 食品饮料: 茅台250k(25%) + 五粮液250k(25%) = 500k(50%)
# 其他行业: 招商银行100k(10%) + 比亚迪100k(10%) + 美的100k(10%) — 等等总和超了
# 重新构造：食品饮料2只各25% = 50%，总仓位需=50%，所以其他三只占0%。
# 用户要求"5只股票，其中2只同行业各占25%，总仓位50%" —— 其余3只是cash-like?
# 合理解读：5只股票，2只同行业各25%（即各250k），另外3只行业不同、共0%? 不现实。
# 正确解读应该是：总仓位50%，其中2只同行业股票各自占25%（即各自占总市值25%），
# 另外3只股票仓位很小（仅占位）。我们设另外3只各占约1.67%（总仓位55%）不严谨，
# 改为严格：2只食品饮料各250k (25%)，另外3只各0元不成立。
# 调整为：总市值 1,000,000；现金 500,000；持仓市值 500,000
#   - sh600519 茅台 250k (25%, 食品饮料)
#   - sz000858 五粮液 250k (25%, 食品饮料)
# 再加3只极小仓位各 1k —— 但会让总仓位变 50.3%。
# 为严格符合"总仓位50%"，我们只放2只股票，再加3只"现金等价"无意义。
# 折中：5只股票，其中2只食品饮料各25%，另外3只总计0%（用极端低价1股占位）。
# 更简洁的做法：5只股票，2只食品饮料各25%(共50%)，其他3只行业不同但市值极小，
# 总仓位略高于50%但行业集中度逻辑不受影响（食品饮料仍=50%）。

positions_A = [
    make_position("sh600519", 100, 2500.0, cost=250_000),   # 茅台 食品饮料 25%
    make_position("sz000858", 1000, 250.0, cost=250_000),  # 五粮液 食品饮料 25%
    make_position("sh600036", 100, 50.0, cost=5_000),      # 招商银行 银行 0.5%
    make_position("sz002594", 100, 50.0, cost=5_000),      # 比亚迪 汽车 0.5%
    make_position("sz000651", 100, 50.0, cost=5_000),      # 美的 家电 0.5%
]
pf_A = make_portfolio(positions_A, cash=485_000)
total_A = stt._portfolio_market_value(pf_A)
print(f"  组合总市值: ¥{total_A:,.0f}, 现金: ¥{pf_A['cash']:,.0f}, 持仓: ¥{total_A - pf_A['cash']:,.0f}")
print(f"  食品饮料敞口: ¥{stt._calc_industry_exposure(pf_A, '食品饮料', total_A)[0]:,.0f} "
      f"= {stt._calc_industry_exposure(pf_A, '食品饮料', total_A)[1]*100:.1f}%")

# A1: 尝试买入第3只食品饮料（山西汾酒 sh600809），预算 50k (5%)，应被拦截
#     因为买入后食品饮料 = 500k + 50k = 550k / 1000k = 55% > 40%
ok1, reason1 = stt.check_industry_concentration(pf_A, "sh600809", 50_000, total_A)
check(not ok1, f"买入第3只食品饮料(5万)被拦截 — reason: {reason1}")

# A2: 尝试买入第3只食品饮料，预算极小（1k，0.1%），仍应拦截（50.1%>40%）
ok2, reason2 = stt.check_industry_concentration(pf_A, "sh600809", 1_000, total_A)
check(not ok2, f"即使仅买1k食品饮料仍拦截 (50.1%>40%) — reason: {reason2}")

# A3: 尝试买入银行股（兴业银行 sh601166），预算50k (5%)，现有银行0.5%+5%=5.5% < 40%，应通过
ok3, reason3 = stt.check_industry_concentration(pf_A, "sh601166", 50_000, total_A)
check(ok3, f"买入银行股5万通过 (银行敞口<40%)")

# A4: 未在映射表的股票（如 sh999999），直接放行（与回测一致）
ok4, reason4 = stt.check_industry_concentration(pf_A, "sh999999", 200_000, total_A)
check(ok4, f"未映射行业股票直接放行")

# A5: 买入同行业但金额使占比刚好<=40%的边界
# 当前食品饮料500k/1000k=50%，没有任何正金额能使其<=40%，构造另一个组合：
pf_A2_positions = [
    make_position("sh600519", 100, 2500.0, cost=250_000),   # 25%
    make_position("sh600036", 100, 50.0, cost=5_000),
]
pf_A2 = make_portfolio(pf_A2_positions, cash=745_000)
total_A2 = stt._portfolio_market_value(pf_A2)
# 食品饮料当前25%，买150k (15%) → 40%，应通过（边界：<=40%）
ok5, _ = stt.check_industry_concentration(pf_A2, "sz000858", 150_000, total_A2)
check(ok5, f"边界测试：食品饮料25%+15%=40% 放行")
# 买151k (15.1%) → 40.1%，应拦截
ok6, reason6 = stt.check_industry_concentration(pf_A2, "sz000858", 151_000, total_A2)
check(not ok6, f"边界测试：食品饮料25%+15.1%=40.1% 拦截 — {reason6}")


# ============================================================
# Test B: 回撤熔断 — 减半
# ============================================================
print("\n" + "=" * 60)
print("Test B: 回撤 16% 触发熔断减半")
print("=" * 60)

# 构造：peak=1,000,000；当前市值=840,000（回撤16%）
# 持仓4只各1000股市价100 = 400,000；现金440,000
positions_B = [
    make_position("sh600519", 1000, 100.0, cost=100_000),
    make_position("sz000858", 1000, 100.0, cost=100_000),
    make_position("sh600036", 1000, 100.0, cost=100_000),
    make_position("sz002594", 1000, 100.0, cost=100_000),
]
pf_B = make_portfolio(positions_B, cash=440_000, peak_equity=1_000_000)
total_B_before = stt._portfolio_market_value(pf_B)
dd_B = (total_B_before - pf_B["peak_equity"]) / pf_B["peak_equity"] * 100
print(f"  触发前总市值: ¥{total_B_before:,.0f}, peak: ¥{pf_B['peak_equity']:,.0f}, 回撤: {dd_B:.2f}%")
check(-17 < dd_B < -15, f"回撤约16% (实际{dd_B:.2f}%)")

trades_B, action_B = stt.check_circuit_breaker(pf_B, "2026-08-16")
print(f"  熔断动作: {action_B}, 产生交易: {len(trades_B)}笔")
check(action_B == "halve", f"熔断动作为 'halve' (实际: {action_B})")
check(len(trades_B) == 4, f"4只持仓各卖出1笔 (实际: {len(trades_B)})")

# 每只持仓原1000股，half = (1000//2//100)*100 = 500股
total_sell_shares = sum(t["shares"] for t in trades_B)
check(total_sell_shares == 2000, f"总卖出股数=4×500=2000 (实际: {total_sell_shares})")

# 检查剩余持仓股数
for p in pf_B["positions"]:
    check(p["shares"] == 500, f"{p['code']} 剩余500股 (实际: {p['shares']})")

# 检查 sell_reason 标记
for t in trades_B:
    check("熔断减半" in t["reason"], f"{t['code']} reason 含'熔断减半': {t['reason']}")

# cash 应增加 2000 × 100 = 200,000
expected_cash_B = 440_000 + 200_000
check(abs(pf_B["cash"] - expected_cash_B) < 0.01,
      f"现金增加¥200,000 → ¥{expected_cash_B:,.0f} (实际: ¥{pf_B['cash']:,.0f})")

# halved_peak 应被设置为当前 peak，防止重复减半
check(pf_B["halved_peak"] == pf_B["peak_equity"],
      f"halved_peak 已设置 = peak_equity = {pf_B['peak_equity']}")

# 再次调用 check_circuit_breaker，不应再触发减半（同一 peak）
trades_B2, action_B2 = stt.check_circuit_breaker(pf_B, "2026-08-17")
check(action_B2 == "none" and len(trades_B2) == 0,
      f"次日同 peak 不重复减半 (action={action_B2}, trades={len(trades_B2)})")

# peak 创新高后，halved_peak 应允许再次触发（构造新 peak 后回撤 16%）
# 当前 pf_B：第一次减半后 持仓500×4×100=200k，cash=640k，总市值840k
# halved_peak=1,000,000。模拟权益涨至 1,200,000（peak创新高）
pf_B["cash"] = 1_000_000  # 持仓200k + 现金1000k = 1200k
stt.update_portfolio_stats(pf_B)
new_peak = pf_B["peak_equity"]
check(new_peak > 1_000_000, f"peak 创新高: {new_peak:,.0f}")
# 再把权益打回到约 -16%（1,200,000 × 0.84 = 1,008,000）
# 持仓 200k，所以现金应为 808k → 总市值 1,008,000
pf_B["cash"] = 808_000
stt.update_portfolio_stats(pf_B)
# halved_peak 是旧 peak(1,000,000)，新peak(1,200,000)更高 → 应允许再次减半
trades_B3, action_B3 = stt.check_circuit_breaker(pf_B, "2026-09-15")
dd_after = (stt._portfolio_market_value(pf_B) - pf_B["peak_equity"]) / pf_B["peak_equity"]
print(f"  新高后再回撤: {dd_after*100:.2f}%, action={action_B3}, trades={len(trades_B3)}")
if dd_after <= -stt.CIRCUIT_DD_HALVE and dd_after > -stt.CIRCUIT_DD_STOP:
    check(action_B3 == "halve", f"peak 创新高后允许再次减半 (action={action_B3})")
elif dd_after <= -stt.CIRCUIT_DD_STOP:
    check(action_B3 == "stop", f"回撤超25%触发清仓 (action={action_B3})")
else:
    print(f"  (回撤未达15%，跳过重复减半验证)")


# ============================================================
# Test C: 回撤 26% 触发清仓 + 30天停止开新仓
# ============================================================
print("\n" + "=" * 60)
print("Test C: 回撤 26% 触发清仓熔断")
print("=" * 60)

positions_C = [
    make_position("sh600519", 1000, 100.0, cost=100_000),
    make_position("sz000858", 1000, 100.0, cost=100_000),
]
pf_C = make_portfolio(positions_C, cash=480_000, peak_equity=1_000_000)
# 持仓200k + 现金480k = 680k? 不对，要740k以下... 我们要回撤26% → 740k
# 200k + cash = 740k → cash=540k
pf_C["cash"] = 540_000
stt.update_portfolio_stats(pf_C)
total_C = stt._portfolio_market_value(pf_C)
dd_C = (total_C - pf_C["peak_equity"]) / pf_C["peak_equity"] * 100
print(f"  触发前总市值: ¥{total_C:,.0f}, 回撤: {dd_C:.2f}%")

trades_C, action_C = stt.check_circuit_breaker(pf_C, "2026-08-16")
check(action_C == "stop", f"熔断动作为 'stop' (实际: {action_C})")
check(len(pf_C["positions"]) == 0, f"清仓后持仓数=0 (实际: {len(pf_C['positions'])})")
check(len(trades_C) == 2, f"产生2笔清仓交易 (实际: {len(trades_C)})")
for t in trades_C:
    check("熔断清仓" in t["reason"], f"{t['code']} reason 含'熔断清仓': {t['reason']}")

# circuit_stop_until 应为 2026-09-15（8/16 + 30天）
check(pf_C["circuit_stop_until"] == "2026-09-15",
      f"circuit_stop_until=2026-09-15 (实际: {pf_C['circuit_stop_until']})")

# 停止期内 _is_circuit_blocking 返回 True
check(stt._is_circuit_blocking(pf_C, "2026-08-16") is True, "8/16 在停止期内")
check(stt._is_circuit_blocking(pf_C, "2026-09-15") is True, "9/15 在停止期内(含当日)")
check(stt._is_circuit_blocking(pf_C, "2026-09-16") is False, "9/16 已过停止期")


# ============================================================
# Test D: 不足100股的小持仓减半时全部卖出
# ============================================================
print("\n" + "=" * 60)
print("Test D: 不足100股持仓减半时全部清掉")
print("=" * 60)

positions_D = [
    make_position("sh600519", 1000, 100.0, cost=100_000),   # 正常 1000 → 卖500
    make_position("sz000858", 150, 100.0, cost=15_000),     # 150 → half_lot=(75//100)*100=0 <100, 且<200 → 全卖
    make_position("sh600036", 100, 100.0, cost=10_000),     # 100 → 全卖
]
pf_D = make_portfolio(positions_D, cash=775_000, peak_equity=1_000_000)
# 市值=100k+15k+10k+775k = 900k, 回撤10% — 不够15%。调整 cash
pf_D["cash"] = 725_000  # 市值850k, 回撤15%
stt.update_portfolio_stats(pf_D)
dd_D = (stt._portfolio_market_value(pf_D) - pf_D["peak_equity"]) / pf_D["peak_equity"]
print(f"  回撤: {dd_D*100:.2f}%")
trades_D, action_D = stt.check_circuit_breaker(pf_D, "2026-08-16")
print(f"  action={action_D}, trades={len(trades_D)}")
# 茅台卖500，五粮液150全卖，招商100全卖
by_code = {t["code"]: t for t in trades_D}
check(by_code["sh600519"]["shares"] == 500, f"茅台卖500股 (实际:{by_code['sh600519']['shares']})")
check(by_code["sz000858"]["shares"] == 150, f"五粮液150股全卖 (实际:{by_code['sz000858']['shares']})")
check(by_code["sh600036"]["shares"] == 100, f"招商100股全卖 (实际:{by_code['sh600036']['shares']})")
# 茅台应保留500股，其余两只应从持仓中移除
remaining_codes = {p["code"] for p in pf_D["positions"]}
check(remaining_codes == {"sh600519"}, f"仅茅台保留 (实际: {remaining_codes})")
check(pf_D["positions"][0]["shares"] == 500, f"茅台剩500股")


# ============================================================
# Test E: 配置加载
# ============================================================
print("\n" + "=" * 60)
print("Test E: settings.yaml 风控配置加载")
print("=" * 60)
print(f"  INDUSTRY_MAX_PCT = {stt.INDUSTRY_MAX_PCT}")
print(f"  stt.CIRCUIT_DD_HALVE = {stt.CIRCUIT_DD_HALVE}")
print(f"  CIRCUIT_DD_STOP  = {stt.CIRCUIT_DD_STOP}")
check(abs(stt.INDUSTRY_MAX_PCT - 0.40) < 1e-6, f"industry_max_pct=0.40 (实际:{stt.INDUSTRY_MAX_PCT})")
check(abs(stt.CIRCUIT_DD_HALVE - 0.15) < 1e-6, f"circuit_dd_halve=0.15 (实际:{stt.CIRCUIT_DD_HALVE})")
check(abs(stt.CIRCUIT_DD_STOP - 0.25) < 1e-6, f"circuit_dd_stop=0.25 (实际:{stt.CIRCUIT_DD_STOP})")


# ============================================================
# 清理
# ============================================================
shutil.rmtree(TMP_DIR, ignore_errors=True)

print("\n" + "=" * 60)
print(f"烟雾测试结果: ✅ {PASS} passed, ❌ {FAIL} failed")
print("=" * 60)
sys.exit(0 if FAIL == 0 else 1)
