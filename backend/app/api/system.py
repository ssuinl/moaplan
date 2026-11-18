"""상태, 캘린더, Tool 목록 API"""
from __future__ import annotations

import os

from fastapi import APIRouter

from tools import REGISTRY
from tools.base import fake_mode
from tools.calendar_tool import get_schedule

router = APIRouter(prefix="/api", tags=["system"])

KEYS = {"GMS_API_KEY": "LLM (SSAFY GMS)", "FINLIFE_API_KEY": "금감원 금융상품 한눈에",
        "ECOS_API_KEY": "한국은행 ECOS", "KAKAO_REST_API_KEY": "카카오 로컬"}


@router.get("/status", summary="키 설정 여부(값은 보내지 않음) · 실행 모드")
def status():
    return {"fake_mode": fake_mode(),
            "llm": "fake" if (os.getenv("MOAPLAN_LLM") == "fake" or not os.getenv("GMS_API_KEY")) else "gms",
            "keys": [{"name": k, "label": v, "configured": bool(os.getenv(k))} for k, v in KEYS.items()],
            "calendar": os.getenv("MOAPLAN_CALENDAR", "local")}


@router.get("/tools", summary="등록된 Tool 명세 (이름 · 설명 · 입력 스키마 · 구분 · 출처)")
def tools():
    return [{**t.card(), "kind": t.kind, "source": t.source} for t in REGISTRY.all()]


@router.get("/calendar", summary="등록된 납입 · 만기 일정")
def calendar(start_date: str | None = None, end_date: str | None = None):
    r = get_schedule(start_date, end_date)
    return r["data"] if r["ok"] else {"events": [], "error": r["error"]["message"]}
