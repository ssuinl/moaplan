"""실행 로그 API (/api/logs) + 이상 징후 조회

목록, 상세, 삭제만 있음. 기록을 고칠 수 있으면 로그 의미가 없어서 수정 API는 안 만듦
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response

from events import anomaly
from events.logging_agent import delete_session_log, get_session_log, list_sessions
from ..schemas import AnomalyReport, LogEvent, LogSession

router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("", response_model=list[LogSession], summary="세션 단위 실행 로그 목록")
def sessions():
    return list_sessions()


@router.get("/{session_id}", response_model=list[LogEvent], summary="세션 실행 로그 상세 (발생 순서)")
def session_detail(session_id: str, run_id: str | None = None):
    rows = get_session_log(session_id, run_id)
    if not rows:
        raise HTTPException(404, "그런 세션 로그가 없습니다.")
    return rows


@router.delete("/{session_id}", status_code=204, summary="세션 실행 로그 삭제")
def session_delete(session_id: str):
    if not delete_session_log(session_id):
        raise HTTPException(404, "그런 세션 로그가 없습니다.")
    return Response(status_code=204)


@router.get("/{session_id}/anomalies", response_model=list[AnomalyReport], summary="이상 징후 판정 이력 (심화 F3020)")
def anomalies(session_id: str):
    out = []
    for r in anomaly.list_reports(session_id):
        f = r["findings"] or {}
        out.append({"run_id": r["run_id"], "verdict": r["verdict"], "findings": f.get("findings", []),
                    "stats": f.get("stats"), "diagnosis": r["diagnosis"], "created_at": r["created_at"]})
    return out
