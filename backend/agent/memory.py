"""대화 맥락 저장

slots: 지금까지 정해진 조건 (목표, 기간, 월 예산, 목돈, 선호, 지역)
last_recommendations: 직전 추천 요약 ('1번 저장해줘' 처리용)
last_plan_id: 마지막 저장 계획 번호 (캘린더 등록용)
turns: 최근 6턴

Tool 원본 응답, API 키, 오류 상세는 저장 안 함.
조건이 바뀌면 바뀐 것만 갱신.
"""
from __future__ import annotations

import json
import re

from app import db

SLOT_KEYS = ["goal_amount", "months", "monthly_budget", "lump_sum", "product_pref",
             "allow_savings_bank", "region", "bonus_conds"]
SLOT_GROUPS = {  # 기준 / 선호 / 제약
    "기준 조건": ["goal_amount", "months"],
    "선호 조건": ["product_pref", "allow_savings_bank", "region", "bonus_conds"],
    "제약 조건": ["monthly_budget", "lump_sum"],
}
SLOT_LABEL = {"goal_amount": "목표 금액", "months": "기간(개월)", "monthly_budget": "월 납입 상한",
              "lump_sum": "지금 가진 목돈", "product_pref": "상품 선호", "allow_savings_bank": "저축은행 허용",
              "region": "영업점 지역", "bonus_conds": "우대 조건"}


def empty() -> dict:
    return {"slots": {}, "last_recommendations": [], "last_plan_id": None, "turns": []}


def load(session_id: str) -> dict:
    try:
        with db.connect() as con:
            r = con.execute("SELECT data FROM memories WHERE session_id=?", (session_id,)).fetchone()
        return db.loads(r["data"], empty()) if r else empty()
    except Exception:
        return empty()


def save(session_id: str, mem: dict):
    try:
        with db.connect() as con:
            con.execute("INSERT INTO memories(session_id, data) VALUES(?, ?) "
                        "ON CONFLICT(session_id) DO UPDATE SET data=excluded.data, updated_at=datetime('now','localtime')",
                        (session_id, json.dumps(mem, ensure_ascii=False, default=str)))
    except Exception:
        pass


def merge_slots(old: dict, analysis: dict) -> tuple[dict, list[str]]:
    """새로 말한 값만 덮어쓰고 바뀐 키 목록도 같이 반환"""
    new, changed = dict(old), []
    for k in SLOT_KEYS:
        v = analysis.get(k)
        if k == "region" and isinstance(v, str):  # '강남 근처' -> '강남'
            v = re.sub(r"\s*(근처|주변|쪽|인근|부근)$", "", v.strip()) or None
        if k == "bonus_conds" and v:  # 우대 조건은 말한 걸 계속 쌓음
            v = sorted(set(old.get(k) or []) | set(v))
        if v is not None and old.get(k) != v:
            new[k] = v
            changed.append(k)
    return new, changed


def grouped(slots: dict) -> dict:
    return {g: {SLOT_LABEL[k]: slots.get(k) for k in keys} for g, keys in SLOT_GROUPS.items()}


def add_turn(mem: dict, user: str, answer: str):
    mem.setdefault("turns", []).append({"user": user[:200], "answer": (answer or "").split("\n")[0][:200]})
    mem["turns"] = mem["turns"][-6:]
