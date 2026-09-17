#!/usr/bin/env python3
"""Backfill point-in-time turnover and inferred float market cap for research."""
from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from point_in_time_universe import load_universe

ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("SUPER_AGENT_DATA_ROOT", str(ROOT / "data"))).resolve()
OUTPUT_DIR = DATA_ROOT / "liquidity"
MANIFEST_PATH = OUTPUT_DIR / "manifest.json"
FIELDS = ("date", "close", "volume", "amount", "turn", "tradestatus", "isST")
WEEKLY_FIELDS = FIELDS[:5]
_BS = None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize(rows: list[list[str]], fields=FIELDS) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=fields)
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    for column in ("close", "volume", "amount", "turn"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["trade_status"] = (
        pd.to_numeric(frame.pop("tradestatus"), errors="coerce").astype("Int64")
        if "tradestatus" in frame else pd.Series(pd.NA, index=frame.index, dtype="Int64")
    )
    frame["is_st"] = (
        pd.to_numeric(frame.pop("isST"), errors="coerce").astype("Int64")
        if "isST" in frame else pd.Series(pd.NA, index=frame.index, dtype="Int64")
    )
    valid = (frame["close"] > 0) & (frame["volume"] > 0) & (frame["turn"] > 0)
    frame["float_shares"] = float("nan")
    frame.loc[valid, "float_shares"] = frame.loc[valid, "volume"] / (frame.loc[valid, "turn"] / 100.0)
    frame["float_market_cap"] = frame["close"] * frame["float_shares"]
    return frame.dropna(subset=["date"]).drop_duplicates("date", keep="last").sort_values("date")


def _save(code: str, frame: pd.DataFrame) -> dict:
    if frame["float_market_cap"].notna().sum() == 0:
        return {"code": code, "status": "no_valid_turnover"}
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"{code}.parquet"
    fd, temporary = tempfile.mkstemp(prefix=f"{code}.", suffix=".tmp", dir=OUTPUT_DIR)
    os.close(fd)
    temp_path = Path(temporary)
    try:
        frame.to_parquet(temp_path, index=False)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
    valid = frame.dropna(subset=["float_market_cap"])
    return {
        "code": code,
        "status": "complete",
        "rows": len(frame),
        "valid_market_cap_rows": len(valid),
        "first_date": frame["date"].iloc[0].strftime("%Y-%m-%d"),
        "last_date": frame["date"].iloc[-1].strftime("%Y-%m-%d"),
        "sha256": _sha256(path),
    }


def _write_manifest(entries: dict[str, dict], start: str, end: str,
                    universe_sha256: str, frequency: str) -> dict:
    complete = sum(row.get("status") == "complete" for row in entries.values())
    payload = {
        "schema": 1,
        "source": "BaoStock unadjusted turn",
        "start": start,
        "end": end,
        "frequency": frequency,
        "universe_sha256": universe_sha256,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "expected_codes": len(entries),
        "complete_codes": complete,
        "coverage": round(complete / len(entries), 6) if entries else 0.0,
        "complete": bool(entries and complete == len(entries)),
        "stocks": entries,
    }
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    temporary = MANIFEST_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, MANIFEST_PATH)
    return payload


def _worker_init() -> None:
    global _BS
    import baostock as bs
    login = None
    for attempt in range(5):
        login = bs.login()
        if login.error_code == "0":
            break
        time.sleep(attempt + 1)
    if login is None or login.error_code != "0":
        raise RuntimeError(f"BaoStock login failed: {login.error_msg if login else 'unknown'}")
    _BS = bs
    atexit.register(bs.logout)


def _fetch_one(code: str, start: str, end: str, frequency: str) -> dict:
    if _BS is None:
        raise RuntimeError("BaoStock worker is not initialized")
    dotted = f"{code[:2]}.{code[2:]}"
    fields = FIELDS if frequency == "d" else WEEKLY_FIELDS
    result = _BS.query_history_k_data_plus(
        dotted, ",".join(fields), start_date=start, end_date=end,
        frequency=frequency, adjustflag="3",
    )
    rows = []
    while result.error_code == "0" and result.next():
        rows.append(result.get_row_data())
    if result.error_code != "0":
        return {"code": code, "status": "failed", "error": result.error_msg}
    if not rows:
        return {"code": code, "status": "no_rows"}
    return _save(code, normalize(rows, fields))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--frequency", choices=("d", "w", "m"), default="w")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    try:
        import baostock as bs
    except ImportError as exc:
        raise SystemExit("baostock is required in the Codex research environment") from exc

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
        and old.get("frequency") == args.frequency
        and old.get("universe_sha256") == metadata.get("sha256")
    ) else {}
    entries = {}
    for code in codes:
        row = reusable.get(code, {})
        path = OUTPUT_DIR / f"{code}.parquet"
        if (row.get("status") == "complete" and path.exists()
                and row.get("sha256") == _sha256(path)):
            entries[code] = row
        else:
            entries[code] = {"code": code, "status": "pending"}

    pending = [code for code in codes if entries[code]["status"] != "complete"]
    print(
        f"[liquidity] complete={len(codes) - len(pending)} "
        f"pending={len(pending)} workers={args.workers}",
        flush=True,
    )
    _write_manifest(entries, args.start, args.end, metadata["sha256"], args.frequency)
    with ProcessPoolExecutor(max_workers=args.workers, initializer=_worker_init) as executor:
        futures = {
            executor.submit(_fetch_one, code, args.start, args.end, args.frequency): code
            for code in pending
        }
        for number, future in enumerate(as_completed(futures), 1):
            code = futures[future]
            try:
                entries[code] = future.result()
            except Exception as exc:
                entries[code] = {"code": code, "status": "failed", "error": str(exc)}
            if number % 50 == 0 or number == len(pending):
                manifest = _write_manifest(
                    entries, args.start, args.end, metadata["sha256"], args.frequency,
                )
                print(f"[liquidity] {number}/{len(pending)} coverage={manifest['coverage']:.2%}", flush=True)

    manifest = _write_manifest(entries, args.start, args.end, metadata["sha256"], args.frequency)
    print(json.dumps({key: manifest[key] for key in (
        "expected_codes", "complete_codes", "coverage", "complete",
    )}, ensure_ascii=False, indent=2))
    return 0 if manifest["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
