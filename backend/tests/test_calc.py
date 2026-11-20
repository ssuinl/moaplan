"""계산식 테스트"""
from agent import calc


def test_simple_saving_interest_formula():
    # 월 100만, 연 3%, 12개월 단리: 1,000,000 * 0.03/12 * 12*13/2 = 195,000
    assert round(calc.saving_interest(1_000_000, 3.0, 12, "S")) == 195_000
    p = calc.project_saving(1_000_000, 3.0, 12, "S")
    assert p.principal == 12_000_000 and p.tax == round(195_000 * 0.154)
    assert p.maturity_amount == 12_000_000 + 195_000 - round(195_000 * 0.154)


def test_compound_beats_simple():
    assert calc.saving_interest(500_000, 3.5, 24, "M") > calc.saving_interest(500_000, 3.5, 24, "S")
    assert calc.deposit_interest(10_000_000, 3.0, 12, "M") > calc.deposit_interest(10_000_000, 3.0, 12, "S")


def test_required_monthly_roundtrip():
    need = calc.required_monthly(10_000_000, 12, 3.1, "S")
    assert need % 10_000 == 0
    assert calc.project_saving(need, 3.1, 12).maturity_amount >= 10_000_000
    assert calc.project_saving(need - 10_000, 3.1, 12).maturity_amount < 10_000_000


def test_real_rate_unknown_inflation_is_none():
    assert calc.real_rate(3.0, None) is None
    assert calc.real_rate(3.0, 2.0) == round(3.0 * 0.846 - 2.0, 2)


def test_judge_combo_budget_and_limit():
    sav = {"base_rate": 3.0, "max_rate": 3.5, "interest_type": "S", "max_limit": 500_000, "join_deny": "1"}
    r = calc.judge_combo(10_000_000, 12, 700_000, 0, sav, None)
    assert r["status"] == "fail" and any("상품 월 한도" in x for x in r["reasons"])
    sav["max_limit"] = None
    r = calc.judge_combo(10_000_000, 12, 900_000, 0, sav, None)
    assert r["status"] == "pass" and r["expected_total"] >= 10_000_000


def test_restricted_product_is_unjudged_not_pass():
    sav = {"base_rate": 4.0, "max_rate": 5.5, "interest_type": "S", "max_limit": None, "join_deny": "3",
           "join_member": "만 19~34세 청년"}
    r = calc.judge_combo(5_000_000, 12, 900_000, 0, sav, None)
    assert r["status"] == "unjudged" and r["unjudged"]


def test_existing_calendar_payments_reduce_budget():
    sav = {"base_rate": 3.0, "max_rate": 3.0, "interest_type": "S", "max_limit": None, "join_deny": "1"}
    assert calc.judge_combo(6_000_000, 12, 600_000, 0, sav, None)["status"] == "pass"
    r = calc.judge_combo(6_000_000, 12, 600_000, 0, sav, None, existing_monthly=300_000)
    assert r["status"] == "fail" and "이미 잡힌 납입" in r["reasons"][0]
