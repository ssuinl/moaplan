"""캘린더 Tool (납입일, 만기일)

구현체를 바꿔 끼울 수 있게 함. 입출력 형식은 둘 다 같음
  LocalCalendar: SQLite (기본)
  GoogleCalendar: MOAPLAN_CALENDAR=google, credentials.json / token.json 필요

등록 Tool 은 plan_id 만 받는다. 제목, 날짜, 금액은 서버가 DB에서 읽어서 모델이 값을 지어낼 수 없음.
이미 잡힌 월 납입액은 판정할 때 월 예산에서 뺌
"""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from app import db
from .base import REGISTRY, Tool, fail, ok


class LocalCalendar:
    name = "local"

    def list_events(self, start: str, end: str) -> list[dict]:
        with db.connect() as con:
            return db.rows(con.execute(
                "SELECT event_id, plan_id, title, kind, start_date, rrule, until_date, amount FROM calendar_events "
                "WHERE (kind='payment' AND start_date<=? AND COALESCE(until_date, start_date)>=?) "
                "   OR (kind='maturity' AND start_date BETWEEN ? AND ?) ORDER BY start_date", (end, start, start, end)))

    def add_events(self, events: list[dict]) -> list[dict]:
        out = []
        with db.connect() as con:
            for e in events:
                cur = con.execute(
                    "INSERT INTO calendar_events(plan_id, title, kind, start_date, rrule, until_date, amount) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (e["plan_id"], e["title"], e["kind"], e["start_date"], e.get("rrule"), e.get("until_date"),
                     e.get("amount")))
                out.append({**e, "event_id": cur.lastrowid})
        return out

    def remove_plan(self, plan_id: int) -> list[dict]:
        """계획에 연결된 일정 삭제 (계획 수정/삭제할 때 서버가 부름, Tool 아님)"""
        with db.connect() as con:
            gone = db.rows(con.execute("SELECT event_id, external_id FROM calendar_events WHERE plan_id=?", (plan_id,)))
            con.execute("DELETE FROM calendar_events WHERE plan_id=?", (plan_id,))
        return gone

    def has_plan(self, plan_id: int) -> bool:
        with db.connect() as con:
            return con.execute("SELECT 1 FROM calendar_events WHERE plan_id=?", (plan_id,)).fetchone() is not None


class GoogleCalendar(LocalCalendar):
    """Google Calendar 구현체. 입출력은 LocalCalendar 와 같음
    로컬 표에도 external_id 로 사본을 남겨서 중복 등록 확인, 조회에 씀
    """
    name = "google"
    SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

    def _service(self):
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
        base = Path(os.getenv("GOOGLE_CRED_DIR", Path(__file__).resolve().parents[1] / "data"))
        token, cred = base / "token.json", base / "credentials.json"
        creds = Credentials.from_authorized_user_file(str(token), self.SCOPES) if token.exists() else None
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                creds = InstalledAppFlow.from_client_secrets_file(str(cred), self.SCOPES).run_local_server(port=0)
            token.write_text(creds.to_json())
        return build("calendar", "v3", credentials=creds)

    def add_events(self, events: list[dict]) -> list[dict]:
        svc = self._service()
        saved = super().add_events(events)
        for e in saved:
            body = {"summary": e["title"], "start": {"date": e["start_date"]}, "end": {"date": e["start_date"]},
                    "description": f"모아플랜 계획 #{e['plan_id']}"}
            if e.get("rrule"):
                body["recurrence"] = [e["rrule"]]
            g = svc.events().insert(calendarId="primary", body=body).execute()
            with db.connect() as con:
                con.execute("UPDATE calendar_events SET external_id=? WHERE event_id=?", (g.get("id"), e["event_id"]))
        return saved


    def remove_plan(self, plan_id: int) -> list[dict]:
        gone = super().remove_plan(plan_id)
        ids = [g["external_id"] for g in gone if g.get("external_id")]
        if ids:
            svc = self._service()
            for gid in ids:
                svc.events().delete(calendarId="primary", eventId=gid).execute()
        return gone


def backend():
    return GoogleCalendar() if os.getenv("MOAPLAN_CALENDAR") == "google" else LocalCalendar()


# 조회 Tool
def get_schedule(start_date: str | None = None, end_date: str | None = None) -> dict:
    s = start_date or date.today().isoformat()
    e = end_date or date(date.today().year + 3, 12, 31).isoformat()
    try:
        date.fromisoformat(s), date.fromisoformat(e)
    except ValueError:
        return fail("BAD_ARGUMENT", "날짜는 YYYY-MM-DD 형식입니다.")
    events = backend().list_events(s, e)
    monthly = sum(ev["amount"] or 0 for ev in events if ev["kind"] == "payment")
    return ok({"start": s, "end": e, "events": events, "monthly_committed": monthly,
               "calendar": backend().name})


def resync_plan_schedule(plan_id: int) -> dict | None:
    """캘린더에 올린 계획을 고쳤을 때 일정 다시 만들기. 올린 적 없으면 None"""
    cal = backend()
    if not cal.has_plan(plan_id):
        return None
    cal.remove_plan(plan_id)
    return register_plan_schedule(plan_id)


# 등록 Tool (plan_id 만 받음)
def register_plan_schedule(plan_id: int) -> dict:
    from app import plans_store
    plan = plans_store.get_plan(int(plan_id))
    if plan is None:
        return fail("NOT_FOUND", f"계획 #{plan_id} 이(가) 없습니다.")
    cal = backend()
    if cal.has_plan(plan["plan_id"]):
        return fail("BAD_ARGUMENT", f"계획 #{plan_id} 은(는) 이미 캘린더에 등록되어 있습니다.")
    events = []
    if plan["monthly_amount"] > 0:
        until = plan["maturity_date"]
        events.append({"plan_id": plan["plan_id"], "kind": "payment",
                       "title": f"[모아플랜] {plan['title']} 적금 납입 {plan['monthly_amount']:,}원",
                       "start_date": plan["start_date"], "until_date": until, "amount": plan["monthly_amount"],
                       "rrule": f"RRULE:FREQ=MONTHLY;BYMONTHDAY={plan['payment_day']};UNTIL={until.replace('-', '')}"})
    events.append({"plan_id": plan["plan_id"], "kind": "maturity",
                   "title": f"[모아플랜] {plan['title']} 만기 (예상 {plan.get('expected_amount') or 0:,}원)",
                   "start_date": plan["maturity_date"], "amount": None})
    saved = cal.add_events(events)
    return ok({"plan_id": plan["plan_id"], "calendar": cal.name, "events": saved})


REGISTRY.register(Tool(
    name="get_schedule",
    description=("이미 등록된 납입 · 만기 일정을 확인해야 할 때 부른다. 새 계획을 세우기 전에 이미 매달 나가는 "
                 "납입액을 확인해 월 예산에서 빼는 데 쓴다."),
    parameters={"type": "object", "properties": {
        "start_date": {"type": "string"}, "end_date": {"type": "string"}}},
    func=get_schedule, kind="local", source="자체 캘린더 (SQLite / Google Calendar)",
))

REGISTRY.register(Tool(
    name="register_plan_schedule",
    description=("사용자가 저장한 계획을 캘린더에 넣어 달라고 할 때 부른다. 계획 번호(plan_id)만 넘기면 "
                 "납입일(매월 반복)과 만기일이 등록된다."),
    parameters={"type": "object", "properties": {"plan_id": {"type": "integer"}}, "required": ["plan_id"]},
    func=register_plan_schedule, kind="local", source="자체 캘린더 (SQLite / Google Calendar)",
))
