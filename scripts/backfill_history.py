#!/usr/bin/env python3
"""Backfill point-in-time stock bars and write a verifiable data manifest."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

from point_in_time_universe import load_universe

ROOT = Path(__file__).resolve().parent.parent
STOCK_DIR = ROOT / "data" / "stocks"
MANIFEST_PATH = ROOT / "data" / "universe" / "stock_history_manifest.json"
API = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
FALLBACK_API = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
FIELDS = ("date", "open", "close", "high", "low", "volume")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_parquet(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.stem + ".", suffix=".tmp", dir=path.parent)
    os.close(fd)
    temp_path = Path(temporary)
    try:
        frame.to_parquet(temp_path, index=False)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _windows(start: str, end: str):
    cursor = datetime.strptime(start, "%Y-%m-%d").date()
    final = datetime.strptime(end, "%Y-%m-%d").date()
    while cursor <= final:
        stop = min(cursor + timedelta(days=729), final)
        yield cursor.isoformat(), stop.isoformat()
        cursor = stop + timedelta(days=1)


def fetch_history(code: str, start: str, end: str, retries: int = 4) -> dict:
    last_error = "unknown error"
    all_rows = []
    queried = []
    for window_start, window_end in _windows(start, end):
        params = {"param": f"{code},day,{window_start},{window_end},640,qfq"}
        fetched = False
        for endpoint in (API, FALLBACK_API):
            endpoint_params = {**params, **({"_var": "kline_dayqfq"} if endpoint == FALLBACK_API else {})}
            for attempt in range(retries):
                try:
                    response = requests.get(endpoint, params=endpoint_params, headers=HEADERS, timeout=20)
                    response.raise_for_status()
                    text = response.text
                    if endpoint == FALLBACK_API:
                        text = text.split("=", 1)[-1]
                    raw = json.loads(text)
                    if raw.get("code") != 0:
                        raise RuntimeError(raw.get("msg") or "Tencent API error")
                    stock = (raw.get("data") or {}).get(code) or {}
                    bars = stock.get("qfqday") or stock.get("day") or []
                    all_rows.extend(row[:6] for row in bars
                                    if len(row) >= 6 and window_start <= row[0] <= window_end)
                    queried.append([window_start, window_end])
                    fetched = True
                    break
                except requests.HTTPError as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    break
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    if attempt + 1 < retries:
                        time.sleep(0.5 * (attempt + 1))
            if fetched:
                break
        if not fetched:
            return {"code": code, "status": "failed", "error": last_error}
    if not all_rows:
        return {"code": code, "status": "no_bars", "error": "empty response"}
    frame = pd.DataFrame(all_rows, columns=FIELDS)
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    for column in FIELDS[1:]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["date", "open", "high", "low", "close"])
    frame = frame.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)
    frame["trade_status"] = (frame["volume"].fillna(0) > 0).astype(int)
    path = STOCK_DIR / f"{code}.parquet"
    _atomic_parquet(frame, path)
    return {
        "code": code, "status": "complete", "rows": len(frame),
        "first_date": frame["date"].iloc[0].strftime("%Y-%m-%d"),
        "last_date": frame["date"].iloc[-1].strftime("%Y-%m-%d"),
        "queried_windows": queried, "sha256": _sha256(path),
    }


def write_manifest(entries: dict[str, dict], start: str, end: str,
                   universe_sha256: str, path: Path = MANIFEST_PATH) -> dict:
    complete = sum(row.get("status") == "complete" for row in entries.values())
    payload = {
        "schema": 1, "source": "Tencent daily kline", "adjustment": "forward",
        "start": start, "end": end, "universe_sha256": universe_sha256,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "expected_codes": len(entries), "complete_codes": complete,
        "coverage": round(complete / len(entries), 6) if entries else 0.0,
        "complete": bool(entries and complete == len(entries)), "stocks": entries,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2022-01-01")
    parser.add_argument("--end", required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    universe, metadata = load_universe()
    if not metadata.get("complete"):
        raise SystemExit("point-in-time universe is incomplete")
    codes = sorted(set().union(*universe.values()))
    try:
        old = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        old = {}
    reusable = old.get("stocks", {}) if (
        not args.force and old.get("start") == args.start and old.get("end") == args.end
        and old.get("universe_sha256") == metadata.get("sha256")
    ) else {}
    entries = {}
    for code in codes:
        row = reusable.get(code, {})
        path = STOCK_DIR / f"{code}.parquet"
        if (row.get("status") == "complete" and path.exists()
                and row.get("sha256") == _sha256(path)):
            entries[code] = row
        else:
            entries[code] = {"code": code, "status": "pending"}
    pending = [code for code in codes if entries[code]["status"] != "complete"]
    print(f"[history] complete={len(codes) - len(pending)} pending={len(pending)} workers={args.workers}", flush=True)
    write_manifest(entries, args.start, args.end, metadata["sha256"])
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(fetch_history, code, args.start, args.end): code for code in pending}
        for index, future in enumerate(as_completed(futures), 1):
            row = future.result()
            entries[row["code"]] = row
            if index % 100 == 0 or index == len(pending):
                manifest = write_manifest(entries, args.start, args.end, metadata["sha256"])
                print(f"[history] {index}/{len(pending)} coverage={manifest['coverage']:.2%}", flush=True)
    manifest = write_manifest(entries, args.start, args.end, metadata["sha256"])
    print(json.dumps({key: manifest[key] for key in (
        "expected_codes", "complete_codes", "coverage", "complete")
    }, ensure_ascii=False, indent=2))
    return 0 if manifest["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
