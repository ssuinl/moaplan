"""API 요청/응답 모델"""
from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field


# 대화
class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=1000, examples=["1년 안에 1000만원 모으고 싶어. 월 70만원까지"])
    session_id: str | None = Field(None, description="없으면 새 대화를 만든다")
    mode: Literal["single", "pw"] = Field("single", description="single=메인 Agent, pw=Planner·Worker 분리(심화)")
    worker_llm: bool = Field(True, description="pw 모드에서 Worker 의 LLM 판단 사용 여부")


class ChatOut(BaseModel):
    session_id: str
    run_id: str
    mode: str
    answer: str
    view: dict[str, Any]
    events: list[dict[str, Any]]
    stats: dict[str, Any]
    memory_slots: dict[str, Any]


# 계획
class ProductRef(BaseModel):
    product_type: Literal["saving", "deposit"]
    bank: str
    product: str
    fin_prdt_cd: str | None = None
    base_rate: float | None = None
    max_rate: float | None = None


class PlanIn(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    goal_amount: int = Field(gt=0)
    start_date: date
    term_months: int = Field(gt=0, le=60)
    payment_day: int | None = Field(None, ge=1, le=31)
    monthly_amount: int = Field(0, ge=0)
    lump_sum: int = Field(0, ge=0)
    products: list[ProductRef] = []
    expected_amount: int | None = None
    memo: str | None = None


class PlanUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=80)
    goal_amount: int | None = Field(None, gt=0)
    start_date: date | None = None
    term_months: int | None = Field(None, gt=0, le=60)
    payment_day: int | None = Field(None, ge=1, le=31)
    monthly_amount: int | None = Field(None, ge=0)
    lump_sum: int | None = Field(None, ge=0)
    expected_amount: int | None = None
    memo: str | None = None


class PlanOut(BaseModel):
    plan_id: int
    title: str
    goal_amount: int
    start_date: str
    maturity_date: str
    payment_day: int
    monthly_amount: int
    lump_sum: int
    term_months: int
    products: list[dict[str, Any]]
    expected_amount: int | None
    memo: str | None
    session_id: str | None
    created_at: str
    updated_at: str


class Overlap(BaseModel):
    plan_id: int
    title: str
    start_date: str
    maturity_date: str
    payment_day: int
    monthly_amount: int
    same_payment_day: bool
    overlap_from: str
    overlap_to: str


class PlanSaveResult(BaseModel):
    plan: PlanOut
    overlaps: list[Overlap]
    overlap_message: str | None
    calendar_synced: bool | None = Field(None, description="캘린더에 올린 계획이면 일정을 새 값으로 다시 만들었는지 (올린 적 없으면 null)")


# 실행 로그
class LogSession(BaseModel):
    session_id: str
    title: str | None
    started_at: str
    last_at: str
    event_count: int
    run_count: int
    last_verdict: str | None


class LogEvent(BaseModel):
    session_id: str
    run_id: str | None
    seq: int
    event: str
    ts: str
    payload: dict[str, Any]


class AnomalyReport(BaseModel):
    run_id: str
    verdict: Literal["normal", "anomaly", "undecidable"]
    findings: list[dict[str, Any]]
    stats: dict[str, Any] | None
    diagnosis: str | None
    created_at: str
