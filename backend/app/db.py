"""SQLite 저장소 (계획, 실행 로그, 이상 판정, 로컬 캘린더, 대화 맥락)

DB 경로는 MOAPLAN_DB 환경변수, 없으면 backend/data/moaplan.db. 테스트는 임시 파일 씀
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

_LOCK = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    goal_amount INTEGER NOT NULL,
    start_date TEXT NOT NULL,          -- 계획의 날짜 (첫 납입일)
    maturity_date TEXT NOT NULL,
    payment_day INTEGER NOT NULL,      -- 매월 납입일
    monthly_amount INTEGER NOT NULL DEFAULT 0,
    lump_sum INTEGER NOT NULL DEFAULT 0,
    term_months INTEGER NOT NULL,
    products TEXT NOT NULL,            -- JSON: [{product_type, bank, product, fin_prdt_cd, base_rate, max_rate}]
    expected_amount INTEGER,
    memo TEXT,
    session_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    title TEXT,
    started_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    last_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    event_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS run_logs (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    run_id TEXT,
    seq INTEGER NOT NULL,
    event TEXT NOT NULL,
    ts TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_session ON run_logs(session_id, seq);
CREATE TABLE IF NOT EXISTS anomaly_reports (
    report_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    verdict TEXT NOT NULL,             -- normal | anomaly | undecidable
    findings TEXT NOT NULL,            -- JSON (규칙 판정)
    diagnosis TEXT,                    -- LLM 진단 (판정과 따로 보관)
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS calendar_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER,
    title TEXT NOT NULL,
    kind TEXT NOT NULL,                -- payment | maturity
    start_date TEXT NOT NULL,
    rrule TEXT,                        -- 매월 납입 반복
    until_date TEXT,
    amount INTEGER,
    external_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS memories (
    session_id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""


def db_path() -> str:
    return os.getenv("MOAPLAN_DB", str(Path(__file__).resolve().parents[1] / "data" / "moaplan.db"))


@contextmanager
def connect():
    path = db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()


def init_db():
    with connect() as con:
        con.executescript(SCHEMA)


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def loads(s, default=None):
    try:
        return json.loads(s) if s else default
    except (TypeError, ValueError):
        return default
