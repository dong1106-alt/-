#!/usr/bin/env python3
"""Backfill conservative point-in-time fundamentals for research only."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

from point_in_time_universe import load_universe

ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("SUPER_AGENT_DATA_ROOT", str(ROOT / "data"))).resolve()
OUTPUT_DIR = DATA_ROOT / "fundamentals"
MANIFEST_PATH = OUTPUT_DIR / "manifest.json"
API = "https://datacenter-web.eastmoney.com/api/data/v1/get"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
PAGE_SIZE = 500
REPORTS = {
    "performance": {
        "name": "RPT_LICO_FN_CPD",
        "date_field": "REPORTDATE",
        "sort": "UPDATE_DATE,SECURITY_CODE",
    },
    "balance": {
        "name": "RPT_DMSK_FN_BALANCE",
        "date_field": "REPORT_DATE",
        "sort": "NOTICE_DATE,SECURITY_CODE",
    },
}
REQUIRED_FACTORS = (
    "eps", "bps", "roe", "profit_yoy", "ocf_per_share", "debt_ratio",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _report_dates(start: str, end: str) -> list[str]:
    dates = pd.date_range(start=start, end=end, freq="QE")
    return [day.strftime("%Y-%m-%d") for day in dates]


def _code(row: dict) -> str:
    number = str(row.get("SECURITY_CODE") or "").zfill(6)
    secucode = str(row.get("SECUCODE") or "").upper()
    if secucode.endswith(".SH") or number.startswith("6"):
        return f"sh{number}"
    if secucode.endswith(".SZ") or number.startswith(("0", "3")):
        return f"sz{number}"
    return ""


def _date(value) -> pd.Timestamp:
    parsed = pd.to_datetime(value, errors="coerce")
    return pd.NaT if pd.isna(parsed) else parsed.normalize()


def _number(value):
    return pd.to_numeric(value, errors="coerce")


def _normalize_performance(rows: list[dict], allowed: set[str]) -> pd.DataFrame:
    output = []
    for row in rows:
        code = _code(row)
        if code not in allowed:
            continue
        notice = _date(row.get("NOTICE_DATE"))
        update = _date(row.get("UPDATE_DATE"))
        output.append({
            "code": code,
            "report_date": _date(row.get("REPORTDATE")),
            "performance_notice_date": notice,
            "performance_update_date": update,
            # Current API rows contain the latest revision, so original notice alone is unsafe.
            "performance_available_date": max(notice, update),
            "eps": _number(row.get("BASIC_EPS")),
            "net_profit": _number(row.get("PARENT_NETPROFIT")),
            "roe": _number(row.get("WEIGHTAVG_ROE")) / 100.0,
            "profit_yoy": _number(row.get("SJLTZ")) / 100.0,
            "bps": _number(row.get("BPS")),
            "ocf_per_share": _number(row.get("MGJYXJJE")),
            "industry": str(row.get("PUBLISHNAME") or ""),
        })
    frame = pd.DataFrame(output)
    if not frame.empty and frame.duplicated(["code", "report_date"]).any():
        raise RuntimeError("duplicate performance code/report_date")
    return frame


def _normalize_balance(rows: list[dict], allowed: set[str]) -> pd.DataFrame:
    output = []
    for row in rows:
        code = _code(row)
        if code not in allowed:
            continue
        output.append({
            "code": code,
            "report_date": _date(row.get("REPORT_DATE")),
            # This endpoint exposes the latest comparative disclosure date.
            "balance_available_date": _date(row.get("NOTICE_DATE")),
            "debt_ratio": _number(row.get("DEBT_ASSET_RATIO")) / 100.0,
            "total_assets": _number(row.get("TOTAL_ASSETS")),
            "total_liabilities": _number(row.get("TOTAL_LIABILITIES")),
        })
    frame = pd.DataFrame(output)
    if not frame.empty and frame.duplicated(["code", "report_date"]).any():
        raise RuntimeError("duplicate balance code/report_date")
    return frame


def _merge(performance: pd.DataFrame, balance: pd.DataFrame) -> pd.DataFrame:
    if performance.empty or balance.empty:
        return pd.DataFrame()
    frame = performance.merge(balance, on=["code", "report_date"], validate="one_to_one")
    frame["available_date"] = frame[
        ["performance_available_date", "balance_available_date"]
    ].max(axis=1)
    dates_ok = frame[[
        "performance_notice_date", "performance_update_date",
        "performance_available_date", "balance_available_date", "available_date",
    ]].notna().all(axis=1)
    frame["complete_factors"] = dates_ok & frame[list(REQUIRED_FACTORS)].notna().all(axis=1)
    return frame.sort_values(["available_date", "code"]).reset_index(drop=True)


def _request(session: requests.Session, params: dict, retries: int = 5) -> dict:
    error = "unknown error"
    for attempt in range(retries):
        try:
            response = session.get(API, params=params, headers=HEADERS, timeout=30)
            response.raise_for_status()
            payload = response.json()
            if payload.get("success") and payload.get("result") is not None:
                return payload
            error = f"API {payload.get('code')}: {payload.get('message')}"
        except (requests.RequestException, ValueError) as exc:
            error = f"{type(exc).__name__}: {exc}"
        time.sleep(min(8, 2 ** attempt))
    raise RuntimeError(error)


def _fetch(session: requests.Session, kind: str, report_date: str) -> tuple[list[dict], int]:
    spec = REPORTS[kind]
    filter_ = (
        '(SECURITY_TYPE_CODE in ("058001001","058001008"))'
        '(TRADE_MARKET_CODE!="069001017")'
        f"({spec['date_field']}='{report_date}')"
    )
    params = {
        "reportName": spec["name"], "columns": "ALL", "filter": filter_,
        "pageNumber": 1, "pageSize": PAGE_SIZE,
        "sortColumns": spec["sort"], "sortTypes": "-1,-1",
    }
    first = _request(session, params)
    result = first["result"]
    pages, expected = int(result.get("pages") or 0), int(result.get("count") or 0)
    if pages < 1 or expected < 1:
        raise RuntimeError(f"{kind} {report_date}: empty result")
    rows = list(result.get("data") or [])
    for page in range(2, pages + 1):
        params["pageNumber"] = page
        payload = _request(session, params)
        if int(payload["result"].get("count") or 0) != expected:
            raise RuntimeError(f"{kind} {report_date}: count changed during pagination")
        batch = payload["result"].get("data") or []
        if not batch:
            raise RuntimeError(f"{kind} {report_date}: empty page {page}/{pages}")
        rows.extend(batch)
    if len(rows) != expected:
        raise RuntimeError(f"{kind} {report_date}: fetched {len(rows)} of {expected}")
    field = spec["date_field"]
    actual = {str(row.get(field) or "")[:10] for row in rows}
    if actual != {report_date}:
        raise RuntimeError(f"{kind} {report_date}: unexpected report dates {sorted(actual)}")
    return rows, expected


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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-report", default="2010-03-31")
    parser.add_argument("--end-report", default="2017-12-31")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    universe, metadata = load_universe()
    if not metadata.get("complete"):
        raise SystemExit("point-in-time universe is incomplete")
    allowed = set().union(*universe.values())
    report_dates = _report_dates(args.start_report, args.end_report)
    if not report_dates:
        raise SystemExit("no quarter-end report dates in requested range")
    try:
        old = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        old = {}
    reusable = old.get("periods", {}) if (
        not args.force and old.get("start_report") == report_dates[0]
        and old.get("end_report") == report_dates[-1]
        and old.get("universe_sha256") == metadata.get("sha256")
    ) else {}
    manifest = {
        "schema": 1,
        "source": "Eastmoney RPT_LICO_FN_CPD + RPT_DMSK_FN_BALANCE",
        "revision_policy": "available_date=max(performance NOTICE_DATE, performance UPDATE_DATE, balance NOTICE_DATE)",
        "start_report": report_dates[0], "end_report": report_dates[-1],
        "universe_sha256": metadata["sha256"],
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "periods": {},
    }
    session = requests.Session()
    for number, report_date in enumerate(report_dates, 1):
        path = OUTPUT_DIR / f"{report_date}.parquet"
        cached = reusable.get(report_date, {})
        if path.exists() and cached.get("sha256") == _sha256(path):
            manifest["periods"][report_date] = cached
            print(f"[fundamentals] {number}/{len(report_dates)} reuse {report_date}", flush=True)
            continue
        performance_rows, performance_api_count = _fetch(session, "performance", report_date)
        balance_rows, balance_api_count = _fetch(session, "balance", report_date)
        performance = _normalize_performance(performance_rows, allowed)
        balance = _normalize_balance(balance_rows, allowed)
        frame = _merge(performance, balance)
        if frame.empty:
            raise RuntimeError(f"{report_date}: no joined universe rows")
        expected_day = max(day for day in universe if day <= report_date)
        expected_codes = universe[expected_day]
        complete_codes = set(frame.loc[frame["complete_factors"], "code"]) & expected_codes
        manifest["periods"][report_date] = {
            "path": str(path), "sha256": _save(frame, path),
            "performance_api_rows": performance_api_count,
            "balance_api_rows": balance_api_count,
            "universe_rows": len(frame), "expected_codes": len(expected_codes),
            "complete_codes": len(complete_codes),
            "coverage": round(len(complete_codes) / len(expected_codes), 6),
        }
        _write_manifest(manifest)
        print(
            f"[fundamentals] {number}/{len(report_dates)} {report_date} "
            f"coverage={len(complete_codes) / len(expected_codes):.2%}", flush=True,
        )

    periods = manifest["periods"].values()
    expected_total = sum(row["expected_codes"] for row in periods)
    complete_total = sum(row["complete_codes"] for row in periods)
    manifest["expected_period_code_rows"] = expected_total
    manifest["complete_period_code_rows"] = complete_total
    manifest["coverage"] = round(complete_total / expected_total, 6) if expected_total else 0.0
    manifest["complete"] = bool(
        len(manifest["periods"]) == len(report_dates) and manifest["coverage"] == 1.0
    )
    _write_manifest(manifest)
    print(json.dumps({key: manifest[key] for key in (
        "expected_period_code_rows", "complete_period_code_rows", "coverage", "complete",
    )}, ensure_ascii=False, indent=2))
    return 0 if manifest["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
