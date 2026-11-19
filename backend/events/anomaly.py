"""이상 징후 판정 (로깅 Agent)

판정은 규칙으로만 함 (LLM X)
  repeat_call       같은 Tool 을 같은 입력으로 2번 이상 호출 (캐시 적중은 안 셈)
  excessive_replan  재계획 3회 상한 도달
  fail_streak       같은 Tool 이 연속 실패
  unplanned_call    최근 계획에 없던 Tool 호출 (Worker 재시도는 retry_of 로 따로 셈)
plan, session.end 이벤트가 없으면 '이상 없음'이 아니라 '판정 불가'
이상일 때만 LLM 으로 원인 진단, 진단이 실패해도 판정은 저장
"""
from __future__ import annotations

import json
from collections import Counter

from app import db

REPLAN_CAP = 3


def evaluate(events: list[dict], final: bool = True) -> dict:
    names = [e["event"] for e in events]
    plans = [e for e in events if e["event"] == "plan"]
    if "session.start" not in names or not plans:
        return {"verdict": "undecidable", "findings": [],
                "reason": "판정에 필요한 이벤트(session.start · plan)가 없습니다."}
    calls = [e for e in events if e["event"] == "tool.call"]
    findings = []

    keys = Counter((c["payload"]["tool"], json.dumps(c["payload"].get("args", {}), sort_keys=True, ensure_ascii=False))
                   for c in calls)
    for (tool, args), n in keys.items():
        if n > 1:
            findings.append({"type": "repeat_call", "tool": tool, "args": json.loads(args), "count": n})

    # 같은 Tool 이 바로 앞에서도 실패했는데 또 실패
    prev = None
    for e in events:
        if e["event"] == "tool.result":
            p = e["payload"]
            if not p.get("ok") and p.get("tool") == prev:
                findings.append({"type": "fail_streak", "tool": p["tool"], "seq": e["seq"], "code": p.get("code")})
                break
            prev = None if p.get("ok") else p.get("tool")

    replans = [e for e in events if e["event"] == "replan"]
    if len(replans) >= REPLAN_CAP:
        findings.append({"type": "excessive_replan", "count": len(replans), "cap": REPLAN_CAP})

    # 호출마다 그 시점의 계획이랑 비교
    planned_by_seq = []
    for p in plans:
        tools = {t["tool"] for t in p["payload"].get("tasks", [])}
        planned_by_seq.append((p["seq"], tools))
    fixes = 0
    for c in calls:
        valid = [tools for seq, tools in planned_by_seq if seq < c["seq"]]
        allowed = valid[-1] if valid else set()
        if c["payload"].get("retry_of"):
            fixes += 1
            continue
        if c["payload"]["tool"] not in allowed:
            findings.append({"type": "unplanned_call", "tool": c["payload"]["tool"], "seq": c["seq"]})

    ended = "session.end" in names or not final  # 실행 중 검증은 끝날 때까지 안 기다림
    verdict = "anomaly" if findings else ("normal" if ended else "undecidable")
    return {"verdict": verdict, "findings": findings,
            "stats": {"tool_calls": len(calls), "replans": len(replans), "fixed_calls": fixes,
                      "llm_calls": names.count("llm.call")},
            "reason": None if ended or findings else "실행이 끝나지 않아(session.end 없음) 판정할 수 없습니다."}


def run_and_store(session_id: str, run_id: str, llm=None) -> dict:
    with db.connect() as con:
        rows = db.rows(con.execute(
            "SELECT seq, event, payload FROM run_logs WHERE session_id=? AND run_id=? ORDER BY log_id",
            (session_id, run_id)))
    events = [{"seq": r["seq"], "event": r["event"], "payload": db.loads(r["payload"], {})} for r in rows]
    result = evaluate(events)
    diagnosis = None
    if result["verdict"] == "anomaly" and llm is not None:
        try:
            lines = [f"{e['seq']:03d} {e['event']} {json.dumps(e['payload'], ensure_ascii=False)[:160]}" for e in events]
            diagnosis = llm.diagnose(result["findings"], lines)
        except Exception:
            diagnosis = None  # 진단이 실패해도 판정은 남김
    with db.connect() as con:
        con.execute("INSERT INTO anomaly_reports(session_id, run_id, verdict, findings, diagnosis) VALUES(?,?,?,?,?)",
                    (session_id, run_id, result["verdict"],
                     json.dumps({"findings": result["findings"], "stats": result.get("stats"),
                                 "reason": result.get("reason")}, ensure_ascii=False), diagnosis))
    result["diagnosis"] = diagnosis
    return result


def latest_report(session_id: str) -> dict | None:
    """메인 Agent 가 쓰는 형태: {verdict, findings, diagnosis, run_id}"""
    with db.connect() as con:
        r = con.execute("SELECT run_id, verdict, findings, diagnosis, created_at FROM anomaly_reports "
                        "WHERE session_id=? ORDER BY report_id DESC LIMIT 1", (session_id,)).fetchone()
    if not r:
        return None
    f = db.loads(r["findings"], {})
    return {"run_id": r["run_id"], "verdict": r["verdict"], "findings": f.get("findings", []),
            "stats": f.get("stats"), "diagnosis": r["diagnosis"], "created_at": r["created_at"]}


def list_reports(session_id: str) -> list[dict]:
    with db.connect() as con:
        rows = db.rows(con.execute("SELECT * FROM anomaly_reports WHERE session_id=? ORDER BY report_id",
                                   (session_id,)))
    for r in rows:
        r["findings"] = db.loads(r["findings"], {})
    return rows
