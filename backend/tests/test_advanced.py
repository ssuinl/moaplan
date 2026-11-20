"""심화 기능 테스트"""
from agent.llm import FakeLLM
from agent.service import run_chat
from events import anomaly
from events.anomaly import evaluate
from tests.conftest import events_of
from tools import REGISTRY

Q_OK = "1년 안에 1000만원 모으고 싶어. 월 90만원까지"


# 이상 징후
def test_detects_repeat_unplanned_and_excessive(logging_agent):
    rep = run_chat(Q_OK, faults={"dup_call"})
    rog = run_chat(Q_OK, faults={"rogue_call"})
    cap = run_chat("1년 안에 5000만원 모으고 싶어. 월 100만원까지")
    kinds = lambda r: {f["type"] for f in anomaly.latest_report(r["session_id"])["findings"]}
    assert "repeat_call" in kinds(rep)
    assert "unplanned_call" in kinds(rog)
    assert "excessive_replan" in kinds(cap)


def test_undecidable_without_evidence():
    assert evaluate([{"seq": 1, "event": "session.start", "payload": {}}])["verdict"] == "undecidable"


def test_diagnosis_only_for_anomaly(logging_agent):
    before = logging_agent.diag_llm.calls
    ok = run_chat(Q_OK)
    assert anomaly.latest_report(ok["session_id"])["diagnosis"] is None
    assert logging_agent.diag_llm.calls == before  # 정상이면 LLM 0회
    bad = run_chat(Q_OK, faults={"rogue_call"})
    r = anomaly.latest_report(bad["session_id"])
    assert r["verdict"] == "anomaly" and r["diagnosis"] and logging_agent.diag_llm.calls == before + 1


# Planner / Worker
Q_FIX = "2년 안에 1200만원 모으고 싶어. 월 50만원까지. 강남 근처"


def test_worker_no_llm_on_normal_path():
    res = run_chat(Q_OK, mode="pw")
    assert not [e for e in events_of(res, "llm.call") if e["payload"]["purpose"] == "worker_fix"]


def test_worker_fixes_args_but_keeps_fixed():
    res = run_chat(Q_FIX, mode="pw")
    d = events_of(res, "worker.decision")[0]["payload"]
    assert d["action"] == "retry" and "region" not in d["changed_args"] and "region" in d["fixed_args"]
    retry = [e for e in events_of(res, "tool.call") if e["payload"].get("retry_of")][0]["payload"]
    assert retry["args"]["region"] == "강남" and retry["args"]["bank_name"] == "바다은행"
    assert list(res["view"]["branches"].values())[0]["count"] >= 1
    single = run_chat(Q_FIX, mode="single")
    assert "error" in list(single["view"]["branches"].values())[0]  # 분리 전에는 못 고침


def test_worker_llm_off_for_fatal_and_missing(monkeypatch):
    monkeypatch.setenv("MOAPLAN_FAKE", "0")
    res = run_chat(Q_OK, mode="pw")
    assert not [e for e in events_of(res, "llm.call") if e["payload"]["purpose"] == "worker_fix"]


def test_logging_verify_triggers_planner_rebuild():
    res = run_chat(Q_OK, mode="pw", faults={"rogue_call"})
    rp = events_of(res, "replan")
    assert rp and rp[0]["payload"]["strategy"] == "rebuild_verified"
    assert res["stats"]["cache_hits"] > 0  # 다시 만든 목록은 받은 결과 재사용
    assert [e["payload"]["verdict"] for e in events_of(res, "logging.verify")] == ["anomaly", "normal"]


def test_worker_llm_switch_comparison():
    on = run_chat(Q_FIX, mode="pw", worker_llm=True)
    off = run_chat(Q_FIX, mode="pw", worker_llm=False)
    assert on["stats"]["llm_calls"] == off["stats"]["llm_calls"] + 1
    assert not events_of(off, "worker.decision")


# RAG
def test_rag_no_index_then_hits():
    assert REGISTRY.run("rag_search", query="예금자보호")["error"]["code"] == "NO_INDEX"
    from tools.rag_tool import build_index
    assert build_index() > 10
    hit = REGISTRY.run("rag_search", query="적금 이자가 왜 적어 보여")["data"]["hits"][0]
    assert "적금" in hit["text"] and hit["source"] and hit["url"]


# 캘린더
def test_calendar_registration_only_takes_plan_id_and_feeds_judgement():
    a = run_chat(Q_OK)
    run_chat("1번으로 저장해줘", a["session_id"])
    card = REGISTRY.get("register_plan_schedule").parameters
    assert card["required"] == ["plan_id"] and list(card["properties"]) == ["plan_id"]
    run_chat("캘린더에 등록해줘", a["session_id"])
    sched = REGISTRY.run("get_schedule")["data"]
    assert sched["monthly_committed"] > 0 and {e["kind"] for e in sched["events"]} == {"payment", "maturity"}
    b = run_chat("1년 안에 150만원 모으고 싶어. 월 100만원까지")
    assert b["view"]["recommendations"] and any("이미 매달" in n for n in b["view"]["notes"])
    for r in b["view"]["recommendations"]:
        assert r["monthly_payment"] <= 1_000_000 - sched["monthly_committed"]


def test_calendar_backend_swappable_same_shape(monkeypatch):
    from tools import calendar_tool
    assert calendar_tool.backend().name == "local"
    monkeypatch.setenv("MOAPLAN_CALENDAR", "google")
    b = calendar_tool.backend()
    assert b.name == "google" and isinstance(b, calendar_tool.LocalCalendar)  # 메서드랑 반환 모양 같음


def test_worker_survives_llm_failure(monkeypatch):
    from agent.llm import FakeLLM
    monkeypatch.setattr(FakeLLM, "worker_fix", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("schema")))
    res = run_chat(Q_FIX, mode="pw")
    assert res["view"]["type"] == "plan" and res["answer"]
    d = events_of(res, "worker.decision")[0]["payload"]
    assert d["action"] == "skip" and "판단 실패" in d["reason"]


def test_worker_fixed_arg_change_is_blocked(monkeypatch):
    from agent.llm import FakeLLM
    monkeypatch.setattr(FakeLLM, "worker_fix", lambda self, tool, args, fixed, err: {
        "action": "retry", "args": {"region": "경상북도"}, "reason": "지역을 넓히면 나올 수 있다"})
    res = run_chat(Q_FIX, mode="pw")
    d = events_of(res, "worker.decision")[0]["payload"]
    assert d["blocked_args"] == {"region": "경상북도"} and d["changed_args"] == {}
    assert d["action"] == "skip" and d["llm_action"] == "retry"
    assert not [e for e in events_of(res, "tool.call") if e["payload"].get("retry_of")]


def test_fail_streak_rule():
    ev = [{"seq": 1, "event": "session.start", "payload": {}},
          {"seq": 2, "event": "plan", "payload": {"tasks": [{"tool": "find_bank_branches"}]}},
          {"seq": 3, "event": "tool.call", "payload": {"tool": "find_bank_branches", "args": {"bank_name": "A"}}},
          {"seq": 4, "event": "tool.result", "payload": {"tool": "find_bank_branches", "ok": False, "code": "NO_RESULT"}},
          {"seq": 5, "event": "tool.call", "payload": {"tool": "find_bank_branches", "args": {"bank_name": "B"}, "retry_of": "x"}},
          {"seq": 6, "event": "tool.result", "payload": {"tool": "find_bank_branches", "ok": False, "code": "NO_RESULT"}},
          {"seq": 7, "event": "session.end", "payload": {}}]
    r = evaluate(ev)
    assert r["verdict"] == "anomaly" and r["findings"][0]["type"] == "fail_streak"


def test_business_only_product_is_excluded():
    from agent import calc
    biz = {"base_rate": 4.0, "interest_type": "S", "join_member": "개인사업자, 조합(비영리법인 포함), 법인", "join_deny": "1"}
    r = calc.judge_combo(10_000_000, 12, 2_000_000, 0, biz, None)
    assert r["status"] == "fail" and any("개인 가입 불가" in x for x in r["reasons"])
    ok = {**biz, "join_member": "개인, 개인사업자, 임의단체"}
    assert calc.judge_combo(10_000_000, 12, 2_000_000, 0, ok, None)["status"] == "pass"


def test_no_replan_when_committed_payments_use_up_budget():
    a = run_chat(Q_OK)
    run_chat("1번으로 저장해줘", a["session_id"])
    run_chat("캘린더에 등록해줘", a["session_id"])
    committed = REGISTRY.run("get_schedule")["data"]["monthly_committed"]
    b = run_chat(f"1년 안에 1000만원 모으고 싶어. 월 {committed // 10000}만원까지")
    assert not b["view"]["recommendations"] and b["stats"]["replans"] == 0
    assert any(e["event"] == "decision" and e["payload"].get("kind") == "skip_replan" for e in b["events"])


def test_worker_replan_request_triggers_planner_replan(monkeypatch):
    from agent.llm import FakeLLM
    from tools.base import fail
    tool = REGISTRY.get("get_product_detail")
    orig, first = tool.func, {}

    def flaky(**kw):  # 처음 본 상품 하나만 NOT_FOUND
        code = kw.get("fin_prdt_cd")
        if not first:
            first["code"] = code
        return fail("NOT_FOUND", "상품 없음") if code == first["code"] else orig(**kw)
    monkeypatch.setattr(tool, "func", flaky)
    monkeypatch.setattr(FakeLLM, "worker_fix", lambda self, tool, args, fixed, err: {
        "action": "replan", "args": {}, "reason": "다른 후보를 봐야 함"})
    res = run_chat(Q_OK, mode="pw")
    rp = events_of(res, "replan")
    assert rp and rp[0]["payload"]["worker_requests"] and "Worker" in rp[0]["payload"]["reason"]
    assert res["view"]["recommendations"]
