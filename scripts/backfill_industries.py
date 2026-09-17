#!/usr/bin/env python3
"""Backfill weekly point-in-time CSRC industry snapshots for research."""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import socket
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from point_in_time_universe import load_universe

ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("SUPER_AGENT_DATA_ROOT", str(ROOT / "data"))).resolve()
OUTPUT_DIR = DATA_ROOT / "industries"
MANIFEST_PATH = OUTPUT_DIR / "manifest.json"
FIELDS = ("updateDate", "code", "code_name", "industry", "industryClassification")
_WORKER_BS = None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _weekly_snapshot_dates(days, start: str, end: str) -> list[str]:
    grouped = {}
    for raw in sorted(day for day in days if start <= day <= end):
        day = pd.Timestamp(raw)
        grouped[(day.isocalendar().year, day.isocalendar().week)] = str(day)[:10]
    return list(grouped.values())


def _normalize(rows: list[list[str]], allowed: set[str], snapshot_date: str) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=FIELDS)
    if frame.empty:
        return frame
    frame["code"] = frame["code"].str.replace(".", "", regex=False)
    frame["updateDate"] = pd.to_datetime(frame["updateDate"], errors="coerce")
    frame = frame[frame["code"].isin(allowed)].copy()
    if frame["updateDate"].isna().any() or (frame["updateDate"] > pd.Timestamp(snapshot_date)).any():
        raise RuntimeError(f"{snapshot_date}: non-causal industry update date")
    if frame["code"].duplicated().any():
        raise RuntimeError(f"{snapshot_date}: duplicate industry code")
    frame.insert(0, "snapshot_date", pd.Timestamp(snapshot_date))
    return frame.sort_values("code").reset_index(drop=True)


def _save(frame: pd.DataFrame, path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.stem + ".", suffix=".tmp", dir=path.parent)
    os.close(fd)
    temp_path = Path(temporary)
    try:
        frame.to_parquet(temp_path, index=False)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
    return _sha256(path)


def _write_manifest(payload: dict) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    temporary = MANIFEST_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, MANIFEST_PATH)


def _login(bs) -> None:
    result = bs.login()
    if result.error_code != "0":
        raise RuntimeError(f"BaoStock login failed: {result.error_msg}")


def _pagination_complete(result, rows: list) -> bool:
    page_size = int(result.per_page_count or 0)
    return result.error_code == "0" and bool(rows) and len(result.data) < page_size


def _fetch(bs, snapshot_date: str, retries: int = 4) -> list[list[str]]:
    error = "unknown error"
    for attempt in range(retries):
        result = bs.query_stock_industry(date=snapshot_date)
        rows = []
        while result.error_code == "0" and result.next():
            rows.append(result.get_row_data())
        if _pagination_complete(result, rows):
            return rows
        error = (
            f"{result.error_code}: {result.error_msg}; rows={len(rows)}; "
            f"final_page_rows={len(result.data)}"
        )
        bs.logout()
        time.sleep(min(8, 2 ** attempt))
        _login(bs)
    raise RuntimeError(f"{snapshot_date}: {error}")


def _worker_init() -> None:
    global _WORKER_BS
    import baostock as bs
    socket.setdefaulttimeout(60)
    _WORKER_BS = bs
    _login(bs)


def _worker_fetch(snapshot_date: str) -> list[list[str]]:
    return _fetch(_WORKER_BS, snapshot_date)


def _store_snapshot(manifest: dict, universe: dict, snapshot_date: str,
                    rows: list[list[str]]) -> float:
    path = OUTPUT_DIR / f"{snapshot_date}.parquet"
    expected = universe[snapshot_date]
    frame = _normalize(rows, expected, snapshot_date)
    classified = set(frame.loc[frame["industry"].str.len() > 0, "code"])
    coverage = len(classified) / len(expected)
    manifest["snapshots"][snapshot_date] = {
        "path": str(path), "sha256": _save(frame, path),
        "api_rows": len(rows), "expected_codes": len(expected),
        "classified_codes": len(classified), "coverage": round(coverage, 6),
        "pagination_complete": True,
    }
    _write_manifest(manifest)
    return coverage


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2010-01-01")
    parser.add_argument("--end", default="2017-12-31")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--workers", type=int, default=1, choices=range(1, 9))
    args = parser.parse_args(argv)

    try:
        import baostock as bs
    except ImportError as exc:
        raise RuntimeError("baostock is required") from exc

    universe, metadata = load_universe()
    if not metadata.get("complete"):
        raise SystemExit("point-in-time universe is incomplete")
    dates = _weekly_snapshot_dates(universe, args.start, args.end)
    if not dates:
        raise SystemExit("no weekly snapshots in requested range")
    try:
        old = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        old = {}
    reusable = old.get("snapshots", {}) if (
        not args.force and old.get("start") == dates[0] and old.get("end") == dates[-1]
        and old.get("universe_sha256") == metadata.get("sha256")
    ) else {}
    manifest = {
        "schema": 1,
        "source": "BaoStock.query_stock_industry CSRC classification",
        "start": dates[0], "end": dates[-1],
        "universe_sha256": metadata["sha256"],
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "snapshots": {},
    }

    todo = []
    for number, snapshot_date in enumerate(dates, 1):
        path = OUTPUT_DIR / f"{snapshot_date}.parquet"
        cached = reusable.get(snapshot_date, {})
        pagination_complete = cached.get(
            "pagination_complete", cached.get("api_rows") != 2000,
        )
        if path.exists() and cached.get("sha256") == _sha256(path) and pagination_complete:
            manifest["snapshots"][snapshot_date] = cached
            print(f"[industries] {number}/{len(dates)} reuse {snapshot_date}", flush=True)
        else:
            todo.append((number, snapshot_date))

    if args.workers == 1:
        socket.setdefaulttimeout(60)
        _login(bs)
        try:
            results = ((_fetch(bs, snapshot_date), number, snapshot_date)
                       for number, snapshot_date in todo)
            for rows, number, snapshot_date in results:
                coverage = _store_snapshot(manifest, universe, snapshot_date, rows)
                print(
                    f"[industries] {number}/{len(dates)} {snapshot_date} "
                    f"coverage={coverage:.2%}", flush=True,
                )
        finally:
            bs.logout()
    else:
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=args.workers, initializer=_worker_init,
        ) as pool:
            dates_to_fetch = [snapshot_date for _, snapshot_date in todo]
            for (number, snapshot_date), rows in zip(
                todo, pool.map(_worker_fetch, dates_to_fetch), strict=True,
            ):
                coverage = _store_snapshot(manifest, universe, snapshot_date, rows)
                print(
                    f"[industries] {number}/{len(dates)} {snapshot_date} "
                    f"coverage={coverage:.2%}", flush=True,
                )

    snapshots = manifest["snapshots"].values()
    expected_total = sum(row["expected_codes"] for row in snapshots)
    classified_total = sum(row["classified_codes"] for row in snapshots)
    manifest["expected_snapshot_code_rows"] = expected_total
    manifest["classified_snapshot_code_rows"] = classified_total
    manifest["coverage"] = round(classified_total / expected_total, 6) if expected_total else 0.0
    manifest["complete"] = bool(
        len(manifest["snapshots"]) == len(dates) and manifest["coverage"] == 1.0
    )
    _write_manifest(manifest)
    print(json.dumps({key: manifest[key] for key in (
        "expected_snapshot_code_rows", "classified_snapshot_code_rows", "coverage", "complete",
    )}, ensure_ascii=False, indent=2))
    return 0 if manifest["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
