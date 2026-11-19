"""만기 수령액, 필요 월 납입액 계산.

판정에 쓰는 숫자는 LLM 말고 여기서 직접 계산한다.
- 이자소득세는 일반과세 15.4%로 계산 (비과세/세금우대는 개인마다 달라서 뺌)
- 판정은 기본금리(intr_rate)로만 한다. 우대금리는 조건을 채웠는지 알 수 없어서
  '최대로 받으면 이 정도'로만 같이 보여준다.
- 적금은 매달 같은 날 같은 금액을 넣는다고 가정. k번째 납입분은 (n-k+1)개월 이자가 붙음
    단리: M * r/12 * n(n+1)/2
    월복리: M * sum((1 + r/12)^j - 1), j = 1..n
- 예금: 단리 P * r * n/12, 월복리 P * ((1 + r/12)^n - 1)
- 실제 은행 계산(일수, 복리 주기)이랑 몇 원~몇백 원 차이 날 수 있어서 '예상'이라고 표시함
"""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass

TAX_RATE = 0.154


def saving_interest(monthly: float, annual_rate_pct: float, months: int, interest_type: str = "S") -> float:
    r = annual_rate_pct / 100 / 12
    if interest_type == "M":
        return monthly * sum((1 + r) ** j - 1 for j in range(1, months + 1))
    return monthly * r * months * (months + 1) / 2


def deposit_interest(principal: float, annual_rate_pct: float, months: int, interest_type: str = "S") -> float:
    r = annual_rate_pct / 100
    if interest_type == "M":
        return principal * ((1 + r / 12) ** months - 1)
    return principal * r * months / 12


def after_tax(interest: float) -> float:
    return interest * (1 - TAX_RATE)


def saving_factor(annual_rate_pct: float, months: int, interest_type: str = "S") -> float:
    """월 1원 넣었을 때 세후 수령액. 납입액에 비례하니까 역산할 때 씀"""
    return months + after_tax(saving_interest(1.0, annual_rate_pct, months, interest_type))


@dataclass
class Projection:
    principal: int  # 원금 합계
    interest_pre_tax: int
    tax: int
    maturity_amount: int  # 세후 만기 수령액
    rate_used: float
    formula: str


def project_saving(monthly: int, rate: float, months: int, itype: str = "S") -> Projection:
    pre = saving_interest(monthly, rate, months, itype)
    tax = pre * TAX_RATE
    kind = "월복리" if itype == "M" else "단리"
    f = (f"{monthly:,}원 x {months}개월 + 세후이자({kind} {rate}%): "
         + (f"{monthly:,} x {rate}%/12 x {months}x{months + 1}/2 x (1-15.4%)" if itype != "M"
            else f"{monthly:,} x Σ((1+{rate}%/12)^j-1) x (1-15.4%)"))
    return Projection(monthly * months, round(pre), round(tax), round(monthly * months + pre - tax), rate, f)


def project_deposit(principal: int, rate: float, months: int, itype: str = "S") -> Projection:
    pre = deposit_interest(principal, rate, months, itype)
    tax = pre * TAX_RATE
    kind = "월복리" if itype == "M" else "단리"
    f = f"{principal:,}원 + 세후이자({kind} {rate}%, {months}개월)"
    return Projection(principal, round(pre), round(tax), round(principal + pre - tax), rate, f)


def required_monthly(goal: int, months: int, rate: float, itype: str = "S", lump_after: int = 0) -> int:
    """목표를 채우려면 매달 얼마 넣어야 하는지 (만원 단위 올림)"""
    need = max(0, goal - lump_after)
    if need == 0:
        return 0
    m = need / saving_factor(rate, months, itype)
    return int(math.ceil(m / 10000) * 10000)


def real_rate(nominal_pct: float, inflation_pct: float | None) -> float | None:
    """세후 금리 - 물가상승률 (근사값). 물가를 모르면 None"""
    if inflation_pct is None:
        return None
    return round(nominal_pct * (1 - TAX_RATE) - inflation_pct, 2)


# 판정
@dataclass
class Verdict:
    status: str  # pass / fail / unjudged
    reasons: list
    combo: dict | None = None  # 계산에 쓴 조합


_PERSONAL = re.compile(r"개인(?!\s*사업자)|실명|누구나|제한\s*없|내국인|거주자|국민|세\s*이상|만\s*\d+\s*세|청년|일반")
_BUSINESS = re.compile(r"사업자|법인|기업|조합|단체")


def personal_ok(product: dict | None) -> bool:
    """가입 대상에 개인이 없고 사업자/법인만 있으면 False

    공시 join_deny 가 '제한없음'인데 실제로는 법인 전용인 상품이 있음 (기업정기적금 같은 것)
    """
    text = (product or {}).get("join_member") or ""
    return not (_BUSINESS.search(text) and not _PERSONAL.search(text))


def judge_combo(goal: int, months: int, monthly_budget: int | None, lump_sum: int,
                saving: dict | None, deposit: dict | None, existing_monthly: int = 0) -> dict:
    """적금(월 납입) + 예금(목돈) 조합이 목표, 예산, 한도를 만족하는지 판정

    saving, deposit: get_product_detail 결과. 없으면 그쪽은 안 씀
    existing_monthly: 캘린더에 이미 잡혀 있는 다른 계획 월 납입액
    """
    reasons, unjudged = [], []
    dep_proj = None
    if deposit and lump_sum > 0:
        dep_proj = project_deposit(lump_sum, deposit["base_rate"], months, deposit.get("interest_type") or "S")
        if deposit.get("max_limit") and lump_sum > deposit["max_limit"]:
            reasons.append(f"예금 한도 {deposit['max_limit']:,}원 초과")
    lump_after = dep_proj.maturity_amount if dep_proj else lump_sum  # 예금이 없으면 목돈은 그냥 더함

    available = None if monthly_budget is None else monthly_budget - existing_monthly
    sav_proj = need_m = None
    if saving:
        itype = saving.get("interest_type") or "S"
        need_m = required_monthly(goal, months, saving["base_rate"], itype, lump_after)
        cap = saving.get("max_limit")
        pay = need_m
        if available is not None and need_m > available:
            reasons.append(f"필요 월 납입 {need_m:,}원 > 쓸 수 있는 월 예산 {available:,}원"
                           + (f" (이미 잡힌 납입 {existing_monthly:,}원 제외)" if existing_monthly else ""))
            pay = max(0, available)
        if cap and need_m > cap:
            reasons.append(f"필요 월 납입 {need_m:,}원 > 상품 월 한도 {cap:,}원")
            pay = min(pay, cap)
        sav_proj = project_saving(pay, saving["base_rate"], months, itype) if pay > 0 else None
        if not personal_ok(saving):
            reasons.append(f"개인 가입 불가 (가입 대상: {saving.get('join_member')})")
        if saving.get("join_deny") in ("2", "3"):
            unjudged.append(f"가입 대상 제한({saving.get('join_member') or saving.get('join_deny_name')}) — 자격 확인 필요")
    elif lump_after < goal:
        reasons.append("월 납입 상품 없이 목돈만으로는 목표에 못 미침")

    if deposit and not personal_ok(deposit):
        reasons.append(f"예금 개인 가입 불가 (가입 대상: {deposit.get('join_member')})")
    if deposit and deposit.get("join_deny") in ("2", "3"):
        unjudged.append(f"예금 가입 대상 제한({deposit.get('join_member')}) — 자격 확인 필요")

    total = (sav_proj.maturity_amount if sav_proj else 0) + lump_after
    shortfall = max(0, goal - total)
    if shortfall > 0 and not any("못 미침" in r for r in reasons):
        reasons.append(f"예상 수령액 {total:,}원으로 목표보다 {shortfall:,}원 부족")

    status = "pass" if not reasons else "fail"
    if unjudged and status == "pass":
        status = "unjudged"  # 계산은 통과인데 자격 조건을 확인 못 해서 보류
    best_case = None
    if saving and saving.get("max_rate") and sav_proj:
        best_case = project_saving(sav_proj.principal // months, saving["max_rate"], months,
                                   saving.get("interest_type") or "S").maturity_amount + lump_after
    return {
        "status": status, "reasons": reasons, "unjudged": unjudged,
        "goal": goal, "months": months,
        "required_monthly": need_m, "monthly_payment": (sav_proj.principal // months) if sav_proj else 0,
        "saving": asdict(sav_proj) if sav_proj else None,
        "deposit": asdict(dep_proj) if dep_proj else None,
        "expected_total": total, "shortfall": shortfall,
        "best_case_total": best_case,
    }
