"""계획 API (/api/plans)"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response

from tools import calendar_tool

from .. import plans_store
from ..schemas import PlanIn, PlanOut, PlanSaveResult, PlanUpdate

router = APIRouter(prefix="/api/plans", tags=["plans"])
NOT_FOUND = "그런 계획이 없습니다."


def _result(plan, overlaps, monthly, calendar_synced=None):
    return {"plan": plan, "overlaps": overlaps, "overlap_message": plans_store.overlap_message(monthly, overlaps),
            "calendar_synced": calendar_synced}


@router.get("", response_model=list[PlanOut], summary="계획 목록")
def list_plans():
    return plans_store.list_plans()


@router.post("", response_model=PlanSaveResult, status_code=201,
             summary="계획 저장 — 납입 기간이 겹치는 계획이 있으면 저장하되 함께 알린다")
def create_plan(body: PlanIn):
    data = body.model_dump()
    data["start_date"] = body.start_date.isoformat()
    data["products"] = [p.model_dump() for p in body.products]
    plan, overlaps = plans_store.create_plan(data)
    return _result(plan, overlaps, plan["monthly_amount"])


@router.get("/{plan_id}", response_model=PlanOut, summary="계획 상세")
def get_plan(plan_id: int):
    p = plans_store.get_plan(plan_id)
    if p is None:
        raise HTTPException(404, NOT_FOUND)
    return p


@router.put("/{plan_id}", response_model=PlanSaveResult, summary="계획 핵심 값 수정")
def update_plan(plan_id: int, body: PlanUpdate):
    changes = body.model_dump(exclude_unset=True)
    if "start_date" in changes and changes["start_date"]:
        changes["start_date"] = changes["start_date"].isoformat()
    plan, overlaps = plans_store.update_plan(plan_id, changes)
    if plan is None:
        raise HTTPException(404, NOT_FOUND)
    synced = calendar_tool.resync_plan_schedule(plan_id)  # 캘린더에 올린 계획이면 일정도 다시 만듦
    return _result(plan, overlaps, plan["monthly_amount"], bool(synced and synced["ok"]) if synced else None)


@router.delete("/{plan_id}", status_code=204, summary="계획 삭제 (연결된 캘린더 일정도 함께)")
def delete_plan(plan_id: int):
    if plans_store.get_plan(plan_id) is not None:
        calendar_tool.backend().remove_plan(plan_id)  # 구글 캘린더면 구글 일정도 삭제
    if not plans_store.delete_plan(plan_id):
        raise HTTPException(404, NOT_FOUND)
    return Response(status_code=204)
