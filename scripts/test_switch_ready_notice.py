#!/usr/bin/env python3
"""A release-ready strategy notification is emitted once per candidate signature."""
import json
import tempfile
from pathlib import Path

import daily_wechat_summary as summary

with tempfile.TemporaryDirectory(prefix="switch_notice_") as tmp:
    base = Path(tmp) / "candidates"
    base.mkdir(parents=True)
    signature = "abc123"
    (base / "promotion_recommendation.json").write_text(json.dumps({
        "candidate_id": "candidate-test",
        "candidate_signature": signature,
        "paired_baseline": {"passed": True},
    }), encoding="utf-8")
    (base / "release_gate_abc123.json").write_text(json.dumps({
        "decision": "release_approved",
        "candidate_signature": signature,
        "candidate_metrics": {"sharpe": 1.2, "drawdown": 3.0},
        "baseline_metrics": {"sharpe": 1.0},
    }), encoding="utf-8")
    original = summary.DATA
    summary.DATA = Path(tmp)
    try:
        notice = summary.switch_ready_notice()
        assert notice and notice[1] == signature
        notice[0].write_text(json.dumps({"candidate_signature": signature}), encoding="utf-8")
        assert summary.switch_ready_notice() is None
    finally:
        summary.DATA = original
print("switch_ready_notice_ok")
