#!/usr/bin/env python3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAMES = {
    "candidate_engine.py", "candidate_optimize.py", "daily_optimize.py",
    "daily_pipeline_codeact.py", "daily_signal_scan.py", "daily_wechat_summary.py",
    "ff_detector.py", "monthly_reoptimize.py", "regression_test.py",
    "shadow_evaluate.py", "shadow_pipeline.py", "sim_trade_tracker.py",
    "test_candidate_engine.py", "test_candidate_optimizer_safety.py", "wechat_push.py",
}
for name in NAMES:
    wrapper = (ROOT / "codeact" / "scripts" / name).read_text(encoding="utf-8")
    assert 'TARGET = ROOT / "scripts" / Path(__file__).name' in wrapper, name
print("canonical_wrappers_ok")
