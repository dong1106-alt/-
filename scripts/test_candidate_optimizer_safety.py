#!/usr/bin/env python3
"""验证候选优化仅写临时候选目录，绝不触碰主 optimal_params.json。"""
import datetime as dt
import hashlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
import candidate_optimize as runner


def row(state):
    return {
        "state": state, "status": "adopted", "val_trades": 20,
        "params": {"marker": 1},
        "val_sharpe": 1.2, "baseline_val_sharpe": 1.0,
        "val_return": 5, "baseline_val_return": 4,
        "val_drawdown": -5, "baseline_val_drawdown": -6,
        "val_win_rate": 55, "baseline_val_win_rate": 50,
        "fold_metrics": [
            {"excess_sharpe_positive": True, "val_start": "2024-01-01", "val_end": "2024-03-31"},
            {"excess_sharpe_positive": True, "val_start": "2024-04-10", "val_end": "2024-06-30"},
            {"excess_sharpe_positive": True, "val_start": "2024-07-10", "val_end": "2024-09-30"},
        ],
    }


class FakeCore:
    def run_optimization(self, *, output_path, **_):
        output = {
            "validation_protocol": "wf-v2",
            "point_in_time_universe": {"complete": True},
            "data_snapshot_hash": "fixture",
            "results": [row("bull"), row("bear"), row("sideways")],
        }
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(output), encoding="utf-8")
        return output


class Monday(dt.date):
    @classmethod
    def today(cls):
        return cls(2026, 9, 7)


main_params = ROOT / "data" / "optimal_params.json"
before = hashlib.sha256(main_params.read_bytes()).hexdigest() if main_params.exists() else None
with tempfile.TemporaryDirectory(prefix="candidate_safety_") as tmp:
    runner.CANDIDATES = Path(tmp)
    runner._load_core = lambda: FakeCore()
    runner.dt.date = Monday
    assert runner.main(["--monthly"]) == 0
    assert (Path(tmp) / "active_shadow.json").exists()
    accepted = json.loads((Path(tmp) / "active_shadow.json").read_text(encoding="utf-8"))
    repeated, reused = runner._ledger_decision(accepted)
    assert reused and repeated["decision"] == "shadow_ready"
    changed = json.loads(json.dumps(accepted))
    changed["data_snapshot_hash"] = "changed-data"
    changed["results"][0]["params"] = {"marker": 2}
    rejected, reused = runner._ledger_decision(changed)
    assert reused and rejected["decision"] == "rejected"
after = hashlib.sha256(main_params.read_bytes()).hexdigest() if main_params.exists() else None
assert before == after
print("candidate_optimizer_main_params_unchanged_ok")
