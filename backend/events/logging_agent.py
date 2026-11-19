"""로깅 Agent

버스를 구독해서 받은 이벤트를 세션별로 순서대로 저장한다.
LLM 안 쓰고, 가공 없이 그대로 저장. 저장에 실패해도 예외를 밖으로 안 던짐.
조회, 삭제만 있고 수정은 없음.
run 이 끝나면(session.end) 이상 징후 판정을 돌림
"""
from __future__ import annotations

import json

from app import db
from .bus import EventBus


class LoggingAgent:
    def __init__(self, bus: EventBus, on_run_end=None):
        self.bus = bus
        self.on_run_end = on_run_end  # run 끝나면 이상 판정
        self._unsub = None
        self.saved = 0
        self.failed = 0

    def start(self):
        self._unsub = self.bus.subscribe(self.handle)
        return self

    def stop(self):
        if self._unsub:
            self._unsub()
            self._unsub = None

    def handle(self, msg: dict):
        try:
            with db.connect() as con:
                con.execute(
                    "INSERT INTO sessions(session_id, title) VALUES(?, ?) ON CONFLICT(session_id) DO NOTHING",
                    (msg["session_id"], (msg["payload"].get("user") or "")[:80] if msg["event"] == "session.start" else None))
                if msg["event"] == "session.start" and msg["payload"].get("user"):
                    con.execute("UPDATE sessions SET title=COALESCE(title, ?) WHERE session_id=?",
                                (msg["payload"]["user"][:80], msg["session_id"]))
                con.execute(
                    "INSERT INTO run_logs(session_id, run_id, seq, event, ts, payload) VALUES(?,?,?,?,?,?)",
                    (msg["session_id"], msg.get("run_id"), msg["seq"], msg["event"], msg["ts"],
                     json.dumps(msg["payload"], ensure_ascii=False, default=str)))
                con.execute("UPDATE sessions SET last_at=?, event_count=event_count+1 WHERE session_id=?",
                            (msg["ts"], msg["session_id"]))
            self.saved += 1
        except Exception:
            self.failed += 1  # 저장 실패해도 대화는 계속
            return
        if msg["event"] == "session.end" and self.on_run_end:
            try:
                self.on_run_end(msg["session_id"], msg.get("run_id"))
            except Exception:
                pass


# 조회, 삭제
def list_sessions() -> list[dict]:
    with db.connect() as con:
        return db.rows(con.execute(
            "SELECT s.session_id, s.title, s.started_at, s.last_at, s.event_count, "
            " (SELECT COUNT(DISTINCT run_id) FROM run_logs r WHERE r.session_id=s.session_id) AS run_count, "
            " (SELECT verdict FROM anomaly_reports a WHERE a.session_id=s.session_id ORDER BY report_id DESC LIMIT 1) AS last_verdict "
            "FROM sessions s ORDER BY s.last_at DESC"))


def get_session_log(session_id: str, run_id: str | None = None) -> list[dict]:
    q = "SELECT session_id, run_id, seq, event, ts, payload FROM run_logs WHERE session_id=?"
    args = [session_id]
    if run_id:
        q += " AND run_id=?"
        args.append(run_id)
    q += " ORDER BY log_id"
    with db.connect() as con:
        out = db.rows(con.execute(q, args))
    for r in out:
        r["payload"] = db.loads(r["payload"], {})
    return out


def delete_session_log(session_id: str) -> bool:
    with db.connect() as con:
        n = con.execute("DELETE FROM run_logs WHERE session_id=?", (session_id,)).rowcount
        con.execute("DELETE FROM anomaly_reports WHERE session_id=?", (session_id,))
        m = con.execute("DELETE FROM sessions WHERE session_id=?", (session_id,)).rowcount
    return bool(n or m)
