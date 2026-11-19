"""LLM 연결 부분. GMS(실제 모델)랑 FakeLLM(테스트용)이 같은 메서드를 가짐

LLM 이 하는 건 4개뿐이고 계산, 판정, 저장은 코드가 함
  analyze     요청 분석 (의도 + 조건)
  compose     최종 응답 요약 문장
  worker_fix  Tool 실패 시 재시도/건너뛰기 판단 (pw 모드)
  diagnose    이상 징후 원인 진단 (로깅 Agent)
호출할 때마다 llm.call 이벤트를 남겨서 요청당 호출 수를 셀 수 있음
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from . import bonus as bonus_mod

INTENTS = ["plan", "fact", "knowledge", "save_plan", "calendar", "chitchat"]


class Analysis(BaseModel):
    intent: Literal["plan", "fact", "knowledge", "save_plan", "calendar", "chitchat"] = Field(
        description="plan=저축 계획 수립/수정, fact=금리·물가 같은 사실 조회, knowledge=제도·용어 질문, "
                    "save_plan=추천안을 계획으로 저장, calendar=저장한 계획을 캘린더 등록, chitchat=인사·사용법")
    reason: str = Field(description="그 의도로 분류한 이유 한 문장")
    goal_amount: int | None = Field(None, description="모으려는 목표 금액(원). 말하지 않았으면 null")
    months: int | None = Field(None, description="모으는 기간(개월). '1년'=12. 말하지 않았으면 null")
    monthly_budget: int | None = Field(None, description="매달 넣을 수 있는 최대 금액(원). 말하지 않았으면 null")
    lump_sum: int | None = Field(None, description="지금 가진 목돈(원). 말하지 않았으면 null")
    product_pref: Literal["any", "saving", "deposit"] | None = Field(None, description="예금/적금 선호. 말하지 않았으면 null")
    allow_savings_bank: bool | None = Field(None, description="저축은행 허용 여부를 말했으면 true/false, 아니면 null")
    region: str | None = Field(None, description="영업점을 찾을 지역. 말하지 않았으면 null")
    pick: int | None = Field(None, description="저장하려는 추천안 번호(1부터). save_plan 일 때만")
    start_date: str | None = Field(None, description="시작일 YYYY-MM-DD. 말하지 않았으면 null")
    payment_day: int | None = Field(None, description="매월 납입일(1~31). 말하지 않았으면 null")
    topic: str | None = Field(None, description="fact/knowledge 일 때 물어본 주제를 짧게")
    bonus_conds: list[Literal["급여이체", "자동이체", "카드실적", "첫거래", "마케팅동의", "비대면가입", "공과금이체", "청약보유"]] | None = Field(
        None, description="우대금리를 받으려고 사용자가 '할 수 있다'고 말한 조건. 말 안 했으면 null")
    compare_months: list[int] | None = Field(
        None, description="기간 두 개 이상을 비교해 달라고 했을 때 그 개월 수들 (예: '1년이랑 2년 비교' → [12, 24]). 아니면 null")


class ArgChange(BaseModel):
    name: str = Field(description="바꿀 인자 이름")
    value: str = Field(description="새 값")


class Diagnosis(BaseModel):
    stage: Literal["분석", "계획", "Tool 호출", "판단", "응답", "사용자 조건"] = Field(
        description="문제가 생긴 단계. Agent 잘못이 아니라 사용자 조건으로는 달성할 수 없어서 생긴 징후면 '사용자 조건'")
    evidence: str = Field(description="근거가 된 이벤트 — seq 번호와 Tool 이름 그대로, 한 문장")
    fix: str = Field(description="무엇을 고칠지 한 문장. 사용자 조건이면 사용자에게 어떤 조정을 안내했는지 확인할 점")


class WorkerDecision(BaseModel):
    action: Literal["retry", "skip", "replan"]
    changes: list[ArgChange] = Field(default_factory=list, description="retry 일 때 바꾼 인자만 (고정 인자 제외)")
    reason: str


SYSTEM_ANALYZE = """너는 저축 계획 서비스의 요청 분석기다. 오늘은 {today} 이다.
규칙:
- 사용자가 '이번 메시지에서' 말한 조건만 채운다. 말하지 않은 값은 반드시 null 로 둔다. 추측하지 않는다.
- 금액은 원 단위 정수 (예: 1000만원 → 10000000, 월 70만 → monthly_budget 700000).
- 기간은 개월 (1년 → 12, 반년 → 6). '이직까지 6개월' 같은 표현도 months 다.
- 이전 대화의 조건(아래 memory)은 참고만 하고 다시 채우지 않는다. 바뀐 값만 채운다.
- 인사·사용법 질문은 chitchat, 예금자보호·세금·단리복리 같은 제도 질문은 knowledge,
  '요즘 금리/물가 어때' 는 fact, '캘린더에 넣어줘' 는 calendar.
- save_plan 은 '저장', '확정', '이걸로 할게' 처럼 추천안을 고르는 말이 있을 때만이다.
  '월 85만원으로 바꿔줘', '기간을 2년으로', '저축은행도 넣어줘' 처럼 조건을 바꾸는 말은 save_plan 이 아니라 plan 이다.
- '급여이체 가능해', '카드 실적 채울 수 있어' 처럼 우대 조건을 말하면 bonus_conds 에 넣고 intent 는 plan 이다
  (우대금리가 뭐냐고 묻는 건 knowledge).
- '1년이랑 2년 비교해줘', '1년 vs 2년' 은 compare_months 에 넣고 intent 는 plan. 비교할 때 months 는 첫 번째 기간.
memory: {memory}"""


_KO = {"goal": "목표 금액", "months": "기간(개월)", "budget": "월 예산", "lump_sum": "목돈",
       "recommendations": "추천안", "label": "상품", "expected_total": "세후 예상 수령액",
       "monthly_payment": "매달 넣을 돈", "real_rate": "물가 뺀 실질 수익률(%p)", "excluded_count": "제외한 후보 수",
       "replans": "재계획", "inflation": "물가상승률(%)", "adjustments": "조정 방안", "required_monthly": "필요 월 납입",
       "reachable_total": "도달 가능 금액", "longer_term": "필요 기간", "basis": "기준 상품", "hits": "안내 문서",
       "inflation_yoy": "물가상승률(%)"}


def _korean_keys(x):
    if isinstance(x, dict):
        return {_KO.get(k, k): _korean_keys(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_korean_keys(v) for v in x]
    return x


def _gms_client():
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(model=os.getenv("GMS_MODEL", "gpt-5.4-nano"), api_key=os.environ["GMS_API_KEY"],
                      base_url="https://gms.ssafy.io/gmsapi/api.openai.com/v1", timeout=40)


class BaseLLM:
    name = "base"

    def __init__(self):
        self.emit = lambda *a, **k: None
        self.calls = 0

    def _track(self, purpose: str, fn):
        t = time.perf_counter()
        try:
            return fn()
        finally:
            self.calls += 1
            self.emit("llm.call", purpose=purpose, model=self.name,
                      ms=round((time.perf_counter() - t) * 1000))


class GMSLLM(BaseLLM):
    name = os.getenv("GMS_MODEL", "gpt-5.4-nano")

    def __init__(self):
        super().__init__()
        self.chat = _gms_client()

    def analyze(self, user: str, memory: dict) -> dict:
        prompt = SYSTEM_ANALYZE.format(today=date.today().isoformat(),
                                       memory=json.dumps(memory.get("slots", {}), ensure_ascii=False))
        model = self.chat.with_structured_output(Analysis)
        out = self._track("analyze", lambda: model.invoke([("system", prompt), ("user", user)]))
        return out.model_dump()

    def compose(self, kind: str, facts: dict) -> str:
        prompt = ("너는 저축 계획 도우미다. 아래 자료만 근거로 사용자에게 짧은 한국어 요약을 쓴다.\n"
                  "- 2문장, 한 문장은 50자 안팎으로 짧게. 쉬운 말로 결론부터.\n"
                  "- 계획 추천이면: 첫 문장은 1순위 상품 이름과 매달 넣을 돈, 둘째 문장은 왜 골랐는지(판단 기준) 또는 재계획 이유. "
                  "세후 수령액 · 실질 수익률 · 2 · 3순위 숫자는 아래 카드에 이미 보이므로 반복하지 않는다.\n"
                  "- 자료에 없는 숫자 · 상품 · 금리를 만들지 않는다.\n"
                  "- 우대금리는 추천안의 '적용 금리'에 적힌 조건만 말한다. 사용자가 말했어도 적용 안 된 조건은 적용됐다고 하지 않는다.\n"
                  "- '기간 비교' 가 있으면 둘째 문장에서 어느 기간이 예산 안에 되는지 한 번 말한다.\n"
                  "- 목록 · 표 · 마크다운(**, #)을 쓰지 않는다. 화면이 따로 보여 준다.\n"
                  "- '자료', 'facts', 영어 항목 이름(real_rate 등)을 말하지 않는다. "
                  "실질 수익률은 '물가를 뺀 실질 수익률 0.49%p'처럼 쓴다.\n"
                  "- 금액은 자료의 값을 그대로 쓴다. '매달 넣을 돈'과 '월 예산 상한'을 혼동하지 않는다.\n"
                  "- 재계획을 했으면 몇 번, 왜 했는지 말한다. 재계획이 '없음'이면 재계획 얘기를 꺼내지 않는다.\n"
                  f"요청 종류: {kind}\n자료: {json.dumps(_korean_keys(facts), ensure_ascii=False, default=str)[:6000]}")
        text = self._track("compose", lambda: self.chat.invoke(prompt).content.strip())
        return re.sub(r"[*#`]+", "", text)

    def worker_fix(self, tool: str, args: dict, fixed: list[str], error: dict) -> dict:
        model = self.chat.with_structured_output(WorkerDecision)
        prompt = (f"Tool {tool} 이(가) 실패했다. 오류: {json.dumps(error, ensure_ascii=False)}\n"
                  f"보낸 인자: {json.dumps(args, ensure_ascii=False)}\n"
                  f"사용자 조건으로 고정된 인자(절대 바꾸지 말 것): {fixed}\n"
                  "인자를 고치면 결과가 나올 것 같으면 retry 와 바꾼 인자를, 아니면 skip, "
                  "다른 작업이 필요하면 replan 을 고르고 이유를 한 문장으로 쓴다.")
        out = self._track("worker_fix", lambda: model.invoke(prompt))
        args = {c.name: c.value for c in out.changes}
        return {"action": out.action, "args": args, "reason": out.reason}

    def diagnose(self, findings: list[dict], log_lines: list[str]) -> str:
        prompt = ("다음은 Agent 실행 로그에서 규칙으로 찾은 이상 징후와 로그(앞 숫자가 seq)다. "
                  "어느 단계 문제인지, 근거 이벤트, 고칠 점을 각각 한 문장으로 답해. 기록에 없는 것은 추측하지 마. "
                  "재계획 상한 도달처럼 목표가 사용자 조건(예산 · 기간)으로는 애초에 불가능해서 생긴 징후면 "
                  "단계를 '사용자 조건'으로 하고 Agent 코드를 고치라고 하지 마.\n"
                  f"이상 징후: {json.dumps(findings, ensure_ascii=False)}\n로그:\n" + "\n".join(log_lines[-60:]))

        def run():
            d = self.chat.with_structured_output(Diagnosis).invoke(prompt)
            clean = lambda t: re.sub(r"[*#`]+", "", t).strip()
            return f"단계: {d.stage}\n근거: {clean(d.evidence)}\n고칠 점: {clean(d.fix)}"
        return self._track("diagnose", run)


# FakeLLM: 키 없이 테스트할 때 쓰는 규칙 기반 대역
_UNIT = {"억": 100_000_000, "천만": 10_000_000, "백만": 1_000_000, "만": 10_000, "천": 1_000}
_AMT = r"(\d+(?:[.,]\d+)?)\s*(억|천만|백만|만|천)?\s*원?"


def parse_amount(s: str) -> int | None:
    s = s.replace(",", "")
    total, found = 0, False
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(억|천만|백만|만|천)", s):
        total += float(num) * _UNIT[unit]
        found = True
    if found:
        return int(total)
    m = re.search(r"(\d{5,})\s*원", s)
    return int(m.group(1)) if m else None


class FakeLLM(BaseLLM):
    name = "fake-rule-llm"

    def analyze(self, user: str, memory: dict) -> dict:
        return self._track("analyze", lambda: self._analyze(user, memory))

    def _analyze(self, user: str, memory: dict) -> dict:
        u = user.strip()
        out = {k: None for k in Analysis.model_fields}
        slots = memory.get("slots", {})
        if re.search(r"(안녕|고마워|감사|뭐 할 수|사용법|도움말)", u) and not re.search(r"\d", u):
            out.update(intent="chitchat", reason="인사·사용법 문의로 숫자 조건이 없다")
            return out
        if re.search(r"(캘린더|달력|일정에? ?(등록|넣))", u):
            out.update(intent="calendar", reason="저장한 계획을 캘린더에 등록해 달라는 요청")
            m = re.search(r"(\d+)\s*번", u)
            out["pick"] = int(m.group(1)) if m else None
            return out
        if re.search(r"(저장|확정|이걸로|이대로)", u):
            m = re.search(r"(\d+)\s*번", u)
            out.update(intent="save_plan", reason="추천안을 계획으로 저장해 달라는 요청",
                       pick=int(m.group(1)) if m else None)
            m = re.search(r"매달\s*(\d{1,2})\s*일|(\d{1,2})\s*일(?:에|마다)?\s*납입", u)
            if m:
                out["payment_day"] = int(m.group(1) or m.group(2))
            return out
        bonus_said = bonus_mod.from_text(u)
        if re.search(r"(예금자\s*보호|세금|비과세|이자소득|단리|복리|우대금리|중도\s*해지|만기\s*후)", u) and not bonus_said:
            out.update(intent="knowledge", reason="외부 API 로 확인할 수 없는 제도·용어 질문", topic=u)
            return out
        if re.search(r"(금리|물가|기준금리).*(어때|얼마|요즘|수준)", u) and not re.search(r"모으|목표", u):
            out.update(intent="fact", reason="현재 금리·물가 수준을 묻는 사실 조회", topic=u)
            return out

        # plan: 조건 뽑기
        m = re.search(r"(\d+(?:\.\d+)?\s*(?:억|천만|백만|만)\s*원?)\s*(?:을|를|은|는)?\s*(?:모으|모을|모아|만들|목표)", u) \
            or re.search(r"목표\s*(?:금액)?\s*(?:은|는|를|을)?\s*(\d+(?:\.\d+)?\s*(?:억|천만|백만|만)\s*원?)", u)
        if m:
            out["goal_amount"] = parse_amount(m.group(1))
        m = re.search(r"(?:월|매달|한\s*달에?|매월)\s*(?:최대\s*)?(\d+(?:\.\d+)?\s*(?:만|천)\s*원?)", u)
        if m:
            out["monthly_budget"] = parse_amount(m.group(1))
        m = re.search(r"(?:목돈|지금|현재|모아\s*둔|모아둔)\s*(?:이|은|는)?\s*(?:돈\s*)?(\d+(?:\.\d+)?\s*(?:억|천만|백만|만)\s*원?)", u) \
            or re.search(r"(\d+(?:\.\d+)?\s*(?:억|천만|백만|만)\s*원?)\s*(?:이|은|는)?\s*(?:지금|목돈|있)", u)
        if m:
            out["lump_sum"] = parse_amount(m.group(1))
        m = re.search(r"(\d+)\s*년\s*(?:반)?", u)
        if m:
            out["months"] = int(m.group(1)) * 12 + (6 if "반" in m.group(0) else 0)
        m = re.search(r"(\d+)\s*개월", u)
        if m:
            out["months"] = int(m.group(1))
        if "반년" in u:
            out["months"] = 6
        if re.search(r"저축은행\s*(빼|말고|싫|제외|안\s*돼)", u):
            out["allow_savings_bank"] = False
        elif re.search(r"저축은행\s*(도|포함|괜찮|가능)", u):
            out["allow_savings_bank"] = True
        if re.search(r"적금만", u):
            out["product_pref"] = "saving"
        elif re.search(r"예금만", u):
            out["product_pref"] = "deposit"
        m = re.search(r"([가-힣]{2,6}(?:역|동|구|시)?)\s*(?:근처|주변|쪽)", u)
        if m:
            out["region"] = m.group(1)
        if bonus_said:
            out["bonus_conds"] = bonus_said
        terms = [int(a) * 12 for a in re.findall(r"(\d+)\s*년", u)] + [int(a) for a in re.findall(r"(\d+)\s*개월", u)]
        if len(terms) >= 2 and re.search(r"(비교|vs|랑|이랑|하고|중에)", u):
            out["compare_months"] = terms
            out["months"] = terms[0]
        has_cond = any(out[k] is not None for k in ("goal_amount", "months", "monthly_budget", "lump_sum",
                                                     "allow_savings_bank", "product_pref", "region", "bonus_conds"))
        wants_saving = re.search(r"(모으|모아|저축|적금|예금|목돈|만들고 싶)", u)
        if not has_cond and not slots and not wants_saving:
            out.update(intent="chitchat", reason="저축 조건이 없는 일반 대화")
            return out
        out.update(intent="plan", reason="목표 금액·기간·납입 조건으로 저축 계획을 요청" if not slots
                   else "이전 계획 조건 중 일부를 바꿔 다시 계획해 달라는 후속 요청")
        return out

    def compose(self, kind: str, facts: dict) -> str:
        def make():
            if kind == "plan" and facts.get("추천안(순위순)"):
                top = facts["추천안(순위순)"][0]
                return (f"{facts['결과']}를 찾았습니다. 1순위는 {top['상품']}으로, 매달 {top['매달 넣을 돈']}을 넣으면 "
                        f"만기에 약 {top['세후 예상 수령액']}(세후)을 받을 것으로 계산됩니다.")
            if kind == "plan":
                return "조건을 모두 만족하는 상품 조합을 찾지 못했습니다. 아래에 무엇을 조정하면 되는지 계산해 두었습니다."
            if kind == "knowledge" and facts.get("hits"):
                return facts["hits"][0]["text"].split("\n", 1)[-1]
            if kind == "fact":
                return (f"최근 소비자물가 상승률은 {facts.get('소비자물가 상승률(전년 같은 달 대비)')}, "
                        f"한국은행 기준금리는 {facts.get('한국은행 기준금리')}입니다.")
            return facts.get("text") or "저축 목표 금액과 기간을 말씀해 주시면 예·적금 조합을 계산해 드릴게요."
        return self._track("compose", make)

    def worker_fix(self, tool: str, args: dict, fixed: list[str], error: dict) -> dict:
        def decide():
            if tool == "find_bank_branches" and "bank_name" not in fixed:
                name = args.get("bank_name", "")
                clean = re.sub(r"^(주식회사|\(주\))\s*", "", name).strip()
                if clean != name:
                    return {"action": "retry", "args": {"bank_name": clean},
                            "reason": "공시상 법인명 '주식회사'를 빼면 지점 검색어와 맞을 수 있다"}
            if error.get("code") == "NO_RATE":
                return {"action": "skip", "args": {}, "reason": "해당 기간 금리가 없는 상품은 고칠 인자가 없다"}
            return {"action": "skip", "args": {}, "reason": "인자를 바꿔도 결과가 달라질 근거가 없다"}
        return self._track("worker_fix", decide)

    def diagnose(self, findings: list[dict], log_lines: list[str]) -> str:
        def make():
            kinds = {f["type"] for f in findings}
            parts = []
            if "repeat_call" in kinds:
                parts.append("같은 Tool 을 같은 입력으로 다시 불렀습니다. 실행 단계의 결과 캐시를 확인해야 합니다(메인 Agent/Worker).")
            if "excessive_replan" in kinds:
                parts.append("재계획이 상한까지 반복됐습니다. 사용자 조건이 공시 상품으로 채울 수 없는 수준일 가능성이 큽니다(Planner).")
            if "fail_streak" in kinds:
                parts.append("같은 Tool 이 연달아 실패했습니다. 재시도 전에 인자를 바꿀 근거가 있었는지 Worker 판단을 확인해야 합니다.")
            if "unplanned_call" in kinds:
                parts.append("계획에 없던 Tool 호출이 있었습니다. Worker 가 작업 목록 밖에서 호출했는지 확인해야 합니다.")
            stage = ("사용자 조건" if kinds == {"excessive_replan"} else
                     "Tool 호출" if kinds & {"repeat_call", "unplanned_call", "fail_streak"} else "판단")
            ev = ", ".join(f"seq {f.get('seq', '?')} {f['type']}" + (f" ({f['tool']})" if f.get("tool") else "") for f in findings)
            return f"단계: {stage}\n근거: {ev}\n고칠 점: {' '.join(parts[:2]) or '규칙 판정 결과를 확인하세요.'}"
        return self._track("diagnose", make)


def make_llm() -> BaseLLM:
    if os.getenv("MOAPLAN_LLM", "auto") == "fake" or not os.getenv("GMS_API_KEY"):
        return FakeLLM()
    return GMSLLM()
