#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQLite-backed task state and cross-process scheduler lock."""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import os
import sqlite3
import time
import uuid
from pathlib import Path


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _parse_ts(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.fromisoformat(value)


class TaskState:
    def __init__(self, path: Path, lease_seconds: int = 7200):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lease_seconds = lease_seconds
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self):
        with self._connect() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS task_runs (
                task_name TEXT NOT NULL,
                logical_date TEXT NOT NULL,
                period_key TEXT NOT NULL DEFAULT '',
                run_id TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                max_attempts INTEGER NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                lease_until TEXT,
                exit_code INTEGER,
                error_type TEXT,
                error TEXT,
                log_path TEXT,
                next_retry_at TEXT,
                PRIMARY KEY (task_name, logical_date, period_key)
            );
            CREATE TABLE IF NOT EXISTS alert_dedup (
                fingerprint TEXT PRIMARY KEY,
                sent_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_task_retry ON task_runs(status, next_retry_at);
            """)

    def get(self, task_name: str, logical_date: str, period_key: str = ""):
        with self._connect() as c:
            return c.execute("SELECT * FROM task_runs WHERE task_name=? AND logical_date=? AND period_key=?",
                             (task_name, logical_date, period_key)).fetchone()

    def claim(self, task_name: str, logical_date: str, period_key: str = "", max_attempts: int = 2,
              log_path: str = "", retry_delays=(60, 300, 900)):
        now = dt.datetime.now(dt.timezone.utc)
        now_s = now.isoformat(timespec="seconds")
        lease_s = (now + dt.timedelta(seconds=self.lease_seconds)).isoformat(timespec="seconds")
        run_id = uuid.uuid4().hex
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM task_runs WHERE task_name=? AND logical_date=? AND period_key=?",
                            (task_name, logical_date, period_key)).fetchone()
            if row:
                if row["status"] == "succeeded":
                    c.rollback(); return None
                if row["status"] == "running":
                    lease = _parse_ts(row["lease_until"])
                    if lease and lease > now:
                        c.rollback(); return None
                if row["status"] == "permanent_failed":
                    c.rollback(); return None
                retry_at = _parse_ts(row["next_retry_at"])
                if retry_at and retry_at > now:
                    c.rollback(); return None
                if row["status"] == "postponed":
                    attempt = int(row["attempt"])
                else:
                    attempt = int(row["attempt"]) + 1
                if attempt > max_attempts:
                    c.execute("UPDATE task_runs SET status='permanent_failed', finished_at=? WHERE task_name=? AND logical_date=? AND period_key=?",
                              (now_s, task_name, logical_date, period_key))
                    c.commit(); return None
                c.execute("""UPDATE task_runs SET run_id=?, status='running', attempt=?, max_attempts=?,
                           started_at=?, finished_at=NULL, lease_until=?, exit_code=NULL, error_type=NULL,
                           error=NULL, log_path=?, next_retry_at=NULL
                           WHERE task_name=? AND logical_date=? AND period_key=?""",
                          (run_id, attempt, max_attempts, now_s, lease_s, log_path,
                           task_name, logical_date, period_key))
            else:
                attempt = 1
                c.execute("""INSERT INTO task_runs(task_name,logical_date,period_key,run_id,status,attempt,max_attempts,
                           started_at,lease_until,log_path) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                          (task_name, logical_date, period_key, run_id, "running", attempt, max_attempts,
                           now_s, lease_s, log_path))
            c.commit()
            return {"run_id": run_id, "attempt": attempt, "max_attempts": max_attempts}

    def finish(self, task_name: str, logical_date: str, period_key: str = "", status: str = "succeeded",
               exit_code: int | None = 0, error_type: str = "", error: str = "", retry_delays=(60, 300, 900)):
        now = dt.datetime.now(dt.timezone.utc)
        next_retry = None
        row = self.get(task_name, logical_date, period_key)
        if status == "postponed":
            # 顺延任务下一自然日再允许 claim，避免常驻循环每20秒重复启动。
            next_retry = (now + dt.timedelta(days=1)).isoformat(timespec="seconds")
        elif status in {"failed", "timeout"} and row:
            attempt = int(row["attempt"])
            if attempt < int(row["max_attempts"]):
                delay = retry_delays[min(attempt - 1, len(retry_delays) - 1)]
                next_retry = (now + dt.timedelta(seconds=delay)).isoformat(timespec="seconds")
            else:
                status = "permanent_failed"
        with self._connect() as c:
            c.execute("""UPDATE task_runs SET status=?, finished_at=?, lease_until=NULL, exit_code=?,
                       error_type=?, error=?, next_retry_at=? WHERE task_name=? AND logical_date=? AND period_key=?""",
                      (status, now.isoformat(timespec="seconds"), exit_code, error_type, error[:2000], next_retry,
                       task_name, logical_date, period_key))

    def status(self, task_name: str, logical_date: str, period_key: str = "") -> str:
        row = self.get(task_name, logical_date, period_key)
        return str(row["status"]) if row else "missing"

    def alert_once(self, task_name: str, logical_date: str, signature: str) -> bool:
        fp = hashlib.sha256(f"{task_name}|{logical_date}|{signature}".encode()).hexdigest()
        with self._connect() as c:
            try:
                c.execute("INSERT INTO alert_dedup(fingerprint,sent_at) VALUES(?,?)", (fp, utc_now()))
                return True
            except sqlite3.IntegrityError:
                return False


@contextlib.contextmanager
def scheduler_lock(path: Path):
    """Portable lock: atomic create on Unix/Windows; stale lock is reclaimed by PID check."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = None
    try:
        for _ in range(2):
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                break
            except FileExistsError:
                try:
                    pid = int(path.read_text().strip())
                    os.kill(pid, 0)
                except (ValueError, OSError):
                    path.unlink(missing_ok=True)
                    continue
                raise RuntimeError(f"scheduler lock already held: {path}")
        if fd is None:
            raise RuntimeError(f"could not acquire scheduler lock: {path}")
        yield
    finally:
        if fd is not None:
            os.close(fd)
            path.unlink(missing_ok=True)
