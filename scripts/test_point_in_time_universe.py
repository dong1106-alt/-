#!/usr/bin/env python3
import gzip
import hashlib
import json
import tempfile
from pathlib import Path

import pandas as pd

from point_in_time_universe import (
    build_universe_from_snapshot,
    load_history_manifest,
    load_universe,
    universe_from_listings,
)


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

with tempfile.TemporaryDirectory() as tmp:
    base = Path(tmp)
    source = base / "source.json.gz"
    snapshot = base / "universe.json.gz"
    metadata = base / "metadata.json"
    calendar = base / "index.parquet"
    with gzip.open(source, "wt", encoding="utf-8") as fh:
        json.dump({"listings": [
            {"code": "sh.600001", "ipo_date": "2024-01-01", "out_date": ""},
        ]}, fh)
    pd.DataFrame({"date": pd.to_datetime(["2024-01-02", "2024-01-03"])}).to_parquet(calendar)
    built = build_universe_from_snapshot(
        "2024-01-01", "2024-01-04", source, calendar, snapshot, metadata,
    )
    assert built["complete"] and built["expected_trading_days"] == 2
    universe, loaded = load_universe(snapshot, metadata)
    assert loaded["complete"] and universe["2024-01-02"] == {"sh600001"}

print("point_in_time_universe_ok")

with tempfile.TemporaryDirectory() as tmp:
    base = Path(tmp)
    stocks = base / "stocks"
    stocks.mkdir()
    stock = stocks / "sh600001.parquet"
    pd.DataFrame({"date": pd.to_datetime(["2024-01-02"]), "open": [10.0],
                  "high": [11.0], "low": [9.0], "close": [10.5], "volume": [100]}).to_parquet(stock)
    digest = hashlib.sha256(stock.read_bytes()).hexdigest()
    manifest = base / "manifest.json"
    payload = {
        "complete": True, "coverage": 1.0, "start": "2023-01-01", "end": "2024-12-31",
        "universe_sha256": "universe", "expected_codes": 1, "complete_codes": 1,
        "stocks": {"sh600001": {"status": "complete", "sha256": digest,
                                "queried_windows": [["2023-01-01", "2024-12-31"]]}},
    }
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    assert load_history_manifest("2024-01-01", "2024-12-01", "universe", manifest, stocks)["complete"]
    assert not load_history_manifest("2024-01-01", "2024-12-01", "universe", manifest, stocks,
                                     universe_by_date={"2024-01-02": {"sh600001", "sz000001"}})["complete"]
    assert not load_history_manifest("2022-01-01", "2024-12-01", "universe", manifest, stocks)["complete"]
    assert not load_history_manifest("2024-01-01", "2024-12-01", "changed", manifest, stocks)["complete"]
    stock.write_bytes(stock.read_bytes() + b"tampered")
    assert not load_history_manifest("2024-01-01", "2024-12-01", "universe", manifest, stocks)["complete"]

print("stock_history_manifest_ok")
