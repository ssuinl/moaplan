"""로깅 Agent, 계획 관리, API 테스트"""
import inspect

from fastapi.testclient import TestClient

from agent.service import run_chat
from events import logging_agent
from tests.conftest import events_of


def test_events_cover_steps_tools_and_llm_decisions():
    res = run_chat("1년 안에 1000만원 모으고 싶어. 월 90만원까지")
    names = {e["event"] for e in res["events"]}
    assert {"step.enter", "step.exit", "tool.call", "tool.result", "decision", "llm.call", "plan"} <= names
    assert all("ms" in e["payload"] for e in events_of(res, "tool.result"))
    d = events_of(res, "decision")[0]["payload"]
    assert d["kind"] == "intent" and d["reason"]
    assert all(e["session_id"] == res["session_id"] for e in res["events"])


def test_main_agent_runs_without_logging_agent():
    res = run_chat("1년 안에 1000만원 모으고 싶어. 월 90만원까지")  # 구독자 없음
    assert res["view"]["recommendations"]
    assert logging_agent.list_sessions() == []


def test_logging_agent_does_not_use_llm():
    src = inspect.getsource(logging_agent.LoggingAgent.handle)  # 저장 경로
    assert "llm" not in src.lower() and "langchain" not in inspect.getsource(logging_agent)


def test_logs_are_per_session_and_ordered(logging_agent):
    a = run_chat("1년 안에 1000만원 모으고 싶어. 월 90만원까지")
    b = run_chat("안녕")
    la = logging_agent_rows(a["session_id"])
    lb = logging_agent_rows(b["session_id"])
    assert la and lb and {r["session_id"] for r in la} == {a["session_id"]}
    assert [r["seq"] for r in la] == sorted(r["seq"] for r in la)
    assert len(la) == len(a["events"])


def logging_agent_rows(sid):
    return logging_agent.get_session_log(sid)


def test_logging_failure_does_not_break_chat(logging_agent, monkeypatch):
    from app import db
    monkeypatch.setattr(db, "connect", lambda: (_ for _ in ()).throw(RuntimeError("disk")))
    from agent import memory
    res = run_chat("안녕")
    assert res["answer"] and logging_agent.failed > 0


def client():
    from app.main import app
    return TestClient(app)


def test_api_routes_and_no_log_update(monkeypatch):
    monkeypatch.setenv("MOAPLAN_LOGGING", "1")
    with client() as c:
        r = c.post("/api/chat", json={"message": "1년 안에 1000만원 모으고 싶어. 월 90만원까지"}).json()
        sid = r["session_id"]
        assert c.get("/api/logs").json()[0]["session_id"] == sid
        assert c.get(f"/api/logs/{sid}").status_code == 200
        assert c.put(f"/api/logs/{sid}", json={}).status_code == 405
        assert c.patch(f"/api/logs/{sid}", json={}).status_code == 405
        assert c.get(f"/api/logs/{sid}/anomalies").json()[0]["verdict"] == "normal"
        assert c.delete(f"/api/logs/{sid}").status_code == 204
        assert c.get(f"/api/logs/{sid}").status_code == 404
        st = c.get("/api/status").json()
        assert all("value" not in k for k in st["keys"])  # 키 값은 안 내보냄


def test_plan_crud_and_overlap_notice():
    with client() as c:
        p1 = c.post("/api/plans", json={"title": "여행", "goal_amount": 3_000_000, "start_date": "2026-12-01",
                                        "term_months": 6, "payment_day": 10, "monthly_amount": 500_000}).json()
        assert p1["overlaps"] == [] and p1["plan"]["maturity_date"] == "2027-06-01"
        p2 = c.post("/api/plans", json={"title": "비상금", "goal_amount": 2_000_000, "start_date": "2027-01-10",
                                        "term_months": 3, "payment_day": 10, "monthly_amount": 600_000}).json()
        assert p2["plan"]["plan_id"] and p2["overlaps"][0]["same_payment_day"]
        assert "1,100,000" in p2["overlap_message"]
        lst = c.get("/api/plans").json()
        assert len(lst) == 2 and all(x["created_at"] for x in lst)
        pid = p2["plan"]["plan_id"]
        up = c.put(f"/api/plans/{pid}", json={"payment_day": 20, "term_months": 6}).json()
        assert up["plan"]["payment_day"] == 20 and up["plan"]["maturity_date"] == "2027-07-10"
        assert c.delete(f"/api/plans/{pid}").status_code == 204
        assert len(c.get("/api/plans").json()) == 1
        assert c.post("/api/plans", json={"title": "x", "goal_amount": -1, "start_date": "2026-12-01",
                                          "term_months": 6}).status_code == 422


def test_edit_registered_plan_resyncs_calendar_and_delete_removes_it():
    from agent.service import run_chat
    from tools.base import REGISTRY
    a = run_chat("1년 안에 1000만원 모으고 싶어. 월 90만원까지")
    run_chat("1번으로 저장해줘", a["session_id"])
    run_chat("캘린더에 등록해줘", a["session_id"])
    pid = REGISTRY.run("get_schedule")["data"]["events"][0]["plan_id"]
    with client() as c:
        r = c.put(f"/api/plans/{pid}", json={"payment_day": 25, "monthly_amount": 850000}).json()
        assert r["calendar_synced"] is True
        ev = [e for e in c.get("/api/calendar").json()["events"] if e["kind"] == "payment"]
        assert len(ev) == 1 and ev[0]["amount"] == 850000 and "BYMONTHDAY=25" in ev[0]["rrule"]
        assert c.delete(f"/api/plans/{pid}").status_code == 204
        assert c.get("/api/calendar").json()["events"] == []
