#!/usr/bin/env python3
import gzip
import hashlib
import json
import tempfile
from pathlib import Path

from point_in_time_universe import load_universe, universe_from_listings


rebuilt = universe_from_listings([
    {"code": "sh.600001", "ipo_date": "2024-01-02", "out_date": "2024-01-03"},
    {"code": "sz.000001", "ipo_date": "2024-01-03", "out_date": ""},
], ["2024-01-02", "2024-01-03", "2024-01-04"])
assert rebuilt["2024-01-02"] == {"sh600001"}
assert rebuilt["2024-01-03"] == {"sh600001", "sz000001"}
assert rebuilt["2024-01-04"] == {"sz000001"}


with tempfile.TemporaryDirectory() as tmp:
    snapshot = Path(tmp) / "universe.json.gz"
    metadata = Path(tmp) / "metadata.json"
    with gzip.open(snapshot, "wt", encoding="utf-8") as fh:
        json.dump({"dates": {"2024-01-02": [
            {"code": "sh.600000", "trade_status": "1", "name": "fixture"},
            {"code": "sz.000001", "trade_status": "0", "name": "suspended"},
        ]}}, fh)
    metadata.write_text(json.dumps({
        "coverage": 1.0,
        "sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
    }), encoding="utf-8")
    universe, meta = load_universe(snapshot, metadata)
    assert meta["complete"]
    assert universe["2024-01-02"] == {"sh600000", "sz000001"}
    snapshot.write_bytes(snapshot.read_bytes() + b"tampered")
    _, bad = load_universe(snapshot, metadata)
    assert not bad["complete"]

with tempfile.TemporaryDirectory() as tmp:
    snapshot = Path(tmp) / "universe.json.gz"
    metadata = Path(tmp) / "metadata.json"
    with gzip.open(snapshot, "wt", encoding="utf-8") as fh:
        json.dump({"start": "2024-01-02", "end": "2024-01-04",
                   "trading_days": ["2024-01-02", "2024-01-03", "2024-01-04"],
                   "listings": [{"code": "sh.600001", "ipo_date": "2024-01-02", "out_date": "2024-01-03"},
                                {"code": "sz.000001", "ipo_date": "2024-01-03", "out_date": ""}]}, fh)
    metadata.write_text(json.dumps({"complete": True, "coverage": 1.0,
                                    "expected_trading_days": 3,
                                    "sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest()}), encoding="utf-8")
    universe, meta = load_universe(snapshot, metadata)
    assert meta["complete"] and universe == rebuilt
    metadata.write_text(json.dumps({**meta, "complete": False}), encoding="utf-8")
    assert not load_universe(snapshot, metadata)[1]["complete"]

print("point_in_time_universe_ok")
