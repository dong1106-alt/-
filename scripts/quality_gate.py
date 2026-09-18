#!/usr/bin/env python3
"""Single fail-closed quality gate used locally and in CI."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FAST = [
    ["-m", "compileall", "-q", "龟缠量化v6_optimized.py", "data", "scripts"],
    ["scripts/ff_detector.py"],
    ["scripts/test_ff_detector.py"],
    ["scripts/test_causal_backtest.py"],
    ["scripts/test_walk_forward_optimization.py"],
    ["scripts/test_strategy_causality.py"],
    ["scripts/test_causal_quality.py"],
    ["scripts/test_candidate_engine.py"],
    ["scripts/test_candidate_optimizer_safety.py"],
    ["scripts/test_historical_validation.py"],
    ["scripts/test_shadow_package.py"],
    ["scripts/test_company_invest_gate.py"],
    ["scripts/test_canonical_wrappers.py"],
    ["scripts/test_runtime_guard.py"],
    ["scripts/test_performance_metrics.py"],
    ["scripts/test_switch_ready_notice.py"],
]

FULL = FAST + [
    ["scripts/test_point_in_time_universe.py"],
    ["scripts/test_backfill_liquidity.py"],
    ["scripts/test_backfill_fundamentals.py"],
    ["scripts/test_backfill_industries.py"],
    ["scripts/test_research_factor_ic.py"],
    ["scripts/test_research_quality_value.py"],
    ["scripts/test_research_linear_multifactor.py"],
    ["scripts/test_research_trend_forever.py"],
    ["scripts/test_research_trend_v5.py"],
    ["scripts/test_research_trend_risk_v5.py"],
    ["scripts/test_research_industry_reversal.py"],
    ["scripts/test_research_four_industry_breadth.py"],
    ["scripts/test_research_v27_price_volume.py"],
    ["scripts/test_research_cloud_factor_overlay.py"],
    ["scripts/test_research_topk_dropout.py"],
    ["scripts/test_research_v61c_low_turnover.py"],
    ["scripts/test_shadow_pairing.py"],
    ["scripts/test_sim_risk_smoke.py"],
    ["scripts/regression_test.py"],
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    env = dict(os.environ, PYTHONUTF8="1")
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT), str(ROOT / "scripts"), env.get("PYTHONPATH", "")])
    commands = FULL if args.full else FAST
    for command in commands:
        shown = " ".join(command)
        print(f"[gate] {shown}", flush=True)
        result = subprocess.run([sys.executable, *command], cwd=ROOT, env=env, check=False)
        if result.returncode:
            print(f"quality_gate_failed: {shown}")
            return result.returncode
    print("quality_gate_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
