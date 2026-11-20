"""메인 Agent 흐름 테스트"""
from agent.executor import RunContext
from agent.graph import build_graph
from agent.llm import FakeLLM
from agent.service import run_chat
from events.bus import Emitter
from tests.conftest import events_of


def analyze_exit(res):
    return next(e for e in res["events"] if e["event"] == "step.exit" and e["payload"]["step"] == "analyze")["payload"]["output"]


def test_extracted_conditions_are_not_invented():
    res = run_chat("1년 안에 1000만원 모으고 싶어")
    cond = analyze_exit(res)["conditions"]
    assert cond["기준 조건"] == {"목표 금액": 10_000_000, "기간(개월)": 12}
    assert all(v is None for v in cond["제약 조건"].values())
    assert all(v is None for v in cond["선호 조건"].values())


def test_graph_has_required_nodes():
    g = build_graph(RunContext(emit=Emitter(None, "s", "r"), llm=FakeLLM())).get_graph()
    assert {"analyze", "plan", "execute", "judge", "respond"} <= set(g.nodes)


def test_chitchat_skips_tools_and_plan_differs():
    hi = run_chat("안녕")
    plan = run_chat("1년 안에 1000만원 모으고 싶어. 월 90만원까지")
    assert events_of(hi, "plan")[0]["payload"]["tasks"] == [] and not events_of(hi, "tool.call")
    assert len(events_of(plan, "plan")[0]["payload"]["tasks"]) >= 3


def test_missing_condition_asks_before_tools():
    res = run_chat("월 50만원 가능해")
    assert res["view"]["type"] == "ask" and not events_of(res, "tool.call")


def test_tool_failure_does_not_stop_flow(monkeypatch):
    from tools import REGISTRY
    tool = REGISTRY.get("get_economic_indicators")
    monkeypatch.setattr(tool, "func", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    res = run_chat("1년 안에 1000만원 모으고 싶어. 월 90만원까지")
    assert any(e["payload"]["code"] == "UPSTREAM_ERROR" for e in events_of(res, "tool.result") if not e["payload"]["ok"])
    assert res["view"]["recommendations"]  # 나머지 단계는 진행
    assert res["view"]["recommendations"][0]["real_rate"] is None  # 물가 모르면 지어내지 않음


def test_judgement_matches_answer_and_excluded_reasons():
    res = run_chat("1년 안에 1000만원 모으고 싶어. 월 90만원까지")
    top = res["view"]["recommendations"][0]
    judge = events_of(res, "judge")[0]["payload"]
    assert judge["best_total"] == top["expected_total"] and top["expected_total"] >= 10_000_000
    assert res["view"]["criteria"] and res["view"]["excluded"] and all(x["reason"] for x in res["view"]["excluded"])
    assert "전문가" in res["view"]["disclaimer"]


def test_replan_reuses_results_and_stops_at_cap():
    res = run_chat("1년 안에 5000만원 모으고 싶어. 월 100만원까지")
    replans = events_of(res, "replan")
    assert len(replans) == 3 and res["stats"]["replans"] == 3
    keys = [(e["payload"]["tool"], str(sorted(e["payload"]["args"].items()))) for e in events_of(res, "tool.call")]
    assert len(keys) == len(set(keys))  # 같은 호출 다시 안 함
    assert res["view"]["adjustments"]["required_monthly"] > 1_000_000
    assert "조정 방안" in res["answer"]


def test_follow_up_changes_only_one_slot():
    a = run_chat("1년 안에 1000만원 모으고 싶어. 월 90만원까지")
    b = run_chat("월 85만원으로 바꿔줘", a["session_id"])
    assert b["memory_slots"] == {"goal_amount": 10_000_000, "months": 12, "monthly_budget": 850_000}
    assert analyze_exit(b)["changed"] == ["monthly_budget"]
    assert b["stats"]["cache_hits"] > 0  # 받은 상품 결과 재사용


def test_missing_api_key_gives_setup_guide(monkeypatch):
    monkeypatch.setenv("MOAPLAN_FAKE", "0")
    res = run_chat("1년 안에 1000만원 모으고 싶어")
    assert res["view"]["type"] == "error" and res["view"]["error_code"] == "INVALID_KEY"
    assert ".env" in res["answer"] and not events_of(res, "replan")


def test_knowledge_question_uses_rag_with_sources():
    from tools.rag_tool import build_index
    build_index()
    res = run_chat("예금자보호 한도가 얼마야?")
    assert events_of(res, "tool.call")[0]["payload"]["tool"] == "rag_search"
    assert res["view"]["hits"][0]["url"].startswith("https://") and "1억" in res["answer"]


def test_save_and_calendar_flow():
    a = run_chat("1년 안에 1000만원 모으고 싶어. 월 90만원까지")
    s = run_chat("1번으로 저장해줘 매달 25일 납입", a["session_id"])
    assert s["view"]["type"] == "saved" and s["view"]["plan"]["payment_day"] == 25
    c = run_chat("캘린더에 등록해줘", a["session_id"])
    call = events_of(c, "tool.call")[0]["payload"]
    assert call["tool"] == "register_plan_schedule" and call["args"] == {"plan_id": s["view"]["plan"]["plan_id"]}


def test_rule_corrects_misclassified_condition_change(monkeypatch):
    from agent import llm as llm_mod
    a = run_chat("1년 안에 1000만원 모으고 싶어. 월 90만원까지")
    orig = llm_mod.FakeLLM._analyze

    def wrong(self, user, memory):  # 실제 GMS 에서 나왔던 오분류
        out = orig(self, user, memory)
        out["intent"] = "save_plan"
        return out
    monkeypatch.setattr(llm_mod.FakeLLM, "_analyze", wrong)
    b = run_chat("월 85만원으로 바꿔줘", a["session_id"])
    assert b["view"]["type"] == "plan"
    fix = [e for e in events_of(b, "decision") if e["payload"]["kind"] == "intent_correction"]
    assert fix and fix[0]["payload"]["by"] == "rule"


def test_bonus_conditions_raise_rate_only_for_said_conditions():
    from agent import bonus
    t = "최고 연 0.8%p\n① 급여이체 실적 6개월 이상: 연 0.3%p\n② 카드 결제실적 월 30만원 이상: 연 0.3%p\n③ 마케팅 동의: 연 0.2%p"
    rate, applied = bonus.apply({"base_rate": 3.2, "max_rate": 3.7, "special_condition": t}, ["급여이체", "카드실적"])
    assert rate == 3.7 and [a["cond"] for a in applied] == ["급여이체", "카드실적"]  # 최고금리에서 자름
    assert bonus.apply({"base_rate": 3.2, "max_rate": 4.0, "special_condition": "우대 있음"}, ["급여이체"])[0] == 3.2  # 숫자 없으면 안 더함
    a = run_chat("1년 안에 1000만원 모으고 싶어. 월 90만원까지")
    b = run_chat("급여이체 가능하고 카드도 많이 써", a["session_id"])
    assert b["view"]["conditions"]["선호 조건"]["우대 조건"] == ["급여이체", "카드실적"]
    top = b["view"]["recommendations"][0]
    assert top["rate_used"] >= top["base_rate_raw"] and all(x["cond"] in ("급여이체", "카드실적") for x in top["bonus_applied"])


def test_term_comparison_table():
    r = run_chat("1년이랑 2년 비교해줘. 2000만원 모으고 싶어. 월 100만원까지")
    comp = r["view"]["comparison"]
    assert [c["months"] for c in comp] == [12, 24]
    assert comp[1]["required_monthly"] < comp[0]["required_monthly"]
    assert r["stats"]["replans"] <= 1  # 비교 중에는 재계획 1번까지
