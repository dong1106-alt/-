#!/usr/bin/env python3
"""Build and validate a historical point-in-time A-share universe."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import socket
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SNAPSHOT = ROOT / "data" / "universe" / "point_in_time.json.gz"
DEFAULT_METADATA = ROOT / "data" / "universe" / "metadata.json"
DEFAULT_HISTORY_MANIFEST = ROOT / "data" / "universe" / "stock_history_manifest.json"
DEFAULT_STOCK_DIR = ROOT / "data" / "stocks"


def _main_board(code: str) -> bool:
    return code.startswith(("sh.600", "sh.601", "sh.603", "sh.605",
                            "sz.000", "sz.001", "sz.002", "sz.003"))


def _listed_on(row: dict, day: str) -> bool:
    ipo = row.get("ipo_date", "")
    out = row.get("out_date", "")
    return bool(ipo and ipo <= day and (not out or day <= out))


def universe_from_listings(listings: list[dict], trading_days: list[str]) -> dict[str, set[str]]:
    """Rebuild daily membership from immutable listing/delisting intervals."""
    return {
        day: {row["code"].replace(".", "") for row in listings if _listed_on(row, day)}
        for day in trading_days
    }


def load_universe(snapshot_path=DEFAULT_SNAPSHOT, metadata_path=DEFAULT_METADATA):
    snapshot_path = Path(snapshot_path)
    metadata_path = Path(metadata_path)
    if not snapshot_path.exists() or not metadata_path.exists():
        return {}, {"complete": False, "reason": "point-in-time universe missing"}
    raw = snapshot_path.read_bytes()
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if hashlib.sha256(raw).hexdigest() != metadata.get("sha256"):
        return {}, {**metadata, "complete": False, "reason": "universe checksum mismatch"}
    with gzip.open(snapshot_path, "rt", encoding="utf-8") as fh:
        payload = json.load(fh)
    if "listings" in payload:
        trading_days = payload.get("trading_days", [])
        universe = universe_from_listings(payload["listings"], trading_days)
    else:  # v1 fixture/backward compatibility
        universe = {
            day: {row["code"].replace(".", "") for row in rows}
            for day, rows in payload.get("dates", {}).items()
        }
        trading_days = sorted(universe)
    expected = int(metadata.get("expected_trading_days", len(trading_days)))
    valid_range = bool(
        trading_days
        and metadata.get("start", trading_days[0]) <= trading_days[0]
        and metadata.get("end", trading_days[-1]) >= trading_days[-1]
    )
    metadata["complete"] = bool(
        metadata.get("complete", True)
        and metadata.get("coverage") == 1.0
        and len(trading_days) == expected
        and len(universe) == expected
        and valid_range
        and all(universe.values())
    )
    return universe, metadata


def load_history_manifest(required_start: str, required_end: str, universe_sha256: str,
                          manifest_path=DEFAULT_HISTORY_MANIFEST,
                          stock_dir=DEFAULT_STOCK_DIR, verify_files: bool = True,
                          universe_by_date: dict | None = None) -> dict:
    manifest_path = Path(manifest_path)
    stock_dir = Path(stock_dir)
    if not manifest_path.exists():
        return {"complete": False, "reason": "stock history manifest missing"}
    raw = manifest_path.read_bytes()
    try:
        manifest = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return {"complete": False, "reason": "stock history manifest invalid"}
    result = {**manifest, "manifest_sha256": hashlib.sha256(raw).hexdigest()}
    stocks = manifest.get("stocks") or {}
    complete = bool(
        manifest.get("complete") and manifest.get("coverage") == 1.0 and stocks
        and manifest.get("universe_sha256") == universe_sha256
        and manifest.get("start", "9999-99-99") <= required_start
        and manifest.get("end", "") >= required_end
        and manifest.get("expected_codes") == manifest.get("complete_codes") == len(stocks)
        and (universe_by_date is None or set(stocks) == set().union(*universe_by_date.values()))
    )
    if complete and verify_files:
        for code, row in stocks.items():
            path = stock_dir / f"{code}.parquet"
            windows = row.get("queried_windows") or []
            covered = bool(windows and windows[0][0] <= required_start
                           and windows[-1][1] >= required_end)
            for previous, current in zip(windows, windows[1:]):
                if date.fromisoformat(previous[1]) + timedelta(days=1) != date.fromisoformat(current[0]):
                    covered = False
                    break
            if row.get("status") != "complete" or not path.exists() or not covered:
                complete = False
                result["reason"] = f"stock history missing: {code}"
                break
            if hashlib.sha256(path.read_bytes()).hexdigest() != row.get("sha256"):
                complete = False
                result["reason"] = f"stock history checksum mismatch: {code}"
                break
    result["complete"] = complete
    if not complete and "reason" not in result:
        result["reason"] = "stock history coverage or range incomplete"
    return result


def _save_snapshot(snapshot_path: Path, metadata_path: Path, payload: dict,
                   expected: list[str], complete: bool) -> dict:
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(snapshot_path, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    raw = snapshot_path.read_bytes()
    captured = payload.get("trading_days", list(payload.get("dates", {})))
    coverage = len(captured) / len(expected) if expected else 0.0
    metadata = {
        "source": payload.get("source", "BaoStock"),
        "start": payload["start"], "end": payload["end"],
        "expected_trading_days": len(expected),
        "captured_trading_days": len(captured),
        "historical_security_count": len(payload.get("listings", [])),
        "coverage": round(coverage, 6),
        "complete": bool(complete and coverage == 1.0),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "generated_on": date.today().isoformat(),
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return metadata


def build_universe(start: str, end: str, snapshot_path=DEFAULT_SNAPSHOT,
                   metadata_path=DEFAULT_METADATA) -> dict:
    try:
        import baostock as bs
    except ImportError as exc:
        raise RuntimeError("baostock is required: pip install baostock") from exc

    socket.setdefaulttimeout(20)
    snapshot_path = Path(snapshot_path)
    metadata_path = Path(metadata_path)
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"BaoStock login failed: {login.error_msg}")
    expected: list[str] = []
    listings: list[dict] = []
    try:
        calendar = bs.query_trade_dates(start_date=start, end_date=end)
        while calendar.error_code == "0" and calendar.next():
            row = calendar.get_row_data()
            if len(row) >= 2 and row[1] == "1":
                expected.append(row[0])
        if calendar.error_code != "0" or len(calendar.data) == int(calendar.per_page_count):
            raise RuntimeError("BaoStock trading calendar pagination incomplete")
        result = bs.query_stock_basic()
        page = result.cur_page_num
        while result.error_code == "0" and result.next():
            if result.cur_page_num != page:
                page = result.cur_page_num
                print(f"[universe] security master page {page}", flush=True)
            item = dict(zip(result.fields, result.get_row_data()))
            code = item.get("code", "")
            if _main_board(code) and item.get("type") == "1":
                listings.append({
                    "code": code,
                    "name": item.get("code_name", ""),
                    "ipo_date": item.get("ipoDate", ""),
                    "out_date": item.get("outDate", ""),
                })
        if result.error_code != "0" or len(result.data) == int(result.per_page_count):
            raise RuntimeError(f"BaoStock stock basic query failed: {result.error_msg}")
    finally:
        bs.logout()

    valid = bool(expected and listings and all(row["ipo_date"] for row in listings))
    payload = {
        "source": "BaoStock.query_stock_basic+query_trade_dates",
        "start": start,
        "end": end,
        "trading_days": expected,
        "listings": listings,
    }
    return _save_snapshot(snapshot_path, metadata_path, payload, expected, valid)


def update_index_history(start: str, end: str,
                         path=ROOT / "data" / "index" / "sh000001.parquet") -> int:
    try:
        import baostock as bs
    except ImportError as exc:
        raise RuntimeError("baostock is required: pip install baostock") from exc
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"BaoStock login failed: {login.error_msg}")
    try:
        result = bs.query_history_k_data_plus(
            "sh.000001", "date,open,high,low,close,volume",
            start_date=start, end_date=end, frequency="d", adjustflag="3",
        )
        rows = []
        while result.error_code == "0" and result.next():
            rows.append(result.get_row_data())
        if result.error_code != "0":
            raise RuntimeError(f"BaoStock index query failed: {result.error_msg}")
    finally:
        bs.logout()
    frame = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
    if frame.empty:
        raise RuntimeError("BaoStock returned no index bars")
    frame["date"] = pd.to_datetime(frame["date"])
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["date", "open", "high", "low", "close"])
    path = Path(path)
    if path.exists():
        old = pd.read_parquet(path)
        frame = pd.concat([old, frame], ignore_index=True)
    frame = frame.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False)
    return len(frame)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--index-start")
    args = parser.parse_args()
    metadata = build_universe(args.start, args.end)
    if args.index_start:
        metadata["index_rows"] = update_index_history(args.index_start, args.end)
    print(json.dumps(metadata, ensure_ascii=False, indent=2))
    return 0 if metadata["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
