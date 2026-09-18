import json
from pathlib import Path
from tempfile import TemporaryDirectory

from research_smallcap_70_audit import PARAMETERS, SOURCE_BLOB_SHA, audit_inputs, monthly_schedule

assert monthly_schedule(["2011-01-04", "2011-01-05"]) == [
    {"signal": "2011-01-04", "fill": "2011-01-05"}
]
with TemporaryDirectory() as temp:
    root = Path(temp) / "historical_validation" / "2010-2017" / "liquidity"
    root.mkdir(parents=True)
    (root / "manifest.json").write_text(json.dumps({"coverage": 0.998207, "complete": False}), encoding="utf-8")
    result = audit_inputs(Path(temp))
    assert any("99.8207%" in item for item in result["failures"])
    assert any("总市值" in item for item in result["failures"])
assert SOURCE_BLOB_SHA == "9237456662067f2d62281eb550bf0df06778f15f"
assert PARAMETERS["holding_days"] == 30
assert PARAMETERS["cooldown_days"] == 20
assert PARAMETERS["fill"] == "next open, T+1"
print("research_smallcap_70_audit_ok")
