"""계획 저장소

납입 기간이 겹치는 계획이 있어도 저장은 하고, 겹치는 계획이랑 그 기간 합산 월 납입액을 같이 알려줌.
납입일이 같으면 그것도 알려줌 (같은 날 출금이 몰리니까)
"""
from __future__ import annotations

import json
from datetime import date

from . import db

FIELDS = ["title", "goal_amount", "start_date", "maturity_date", "payment_day", "monthly_amount",
          "lump_sum", "term_months", "products", "expected_amount", "memo", "session_id"]


def add_months(d: date, months: int) -> date:
    y, m = d.year + (d.month - 1 + months) // 12, (d.month - 1 + months) % 12 + 1
    last = [31, 29 if y % 4 == 0 and (y % 100 or y % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return date(y, m, min(d.day, last))


def _row(r: dict | None) -> dict | None:
    if r is None:
        return None
    r = dict(r)
    r["products"] = db.loads(r.get("products"), [])
    return r


def list_plans() -> list[dict]:
    with db.connect() as con:
        return [_row(r) for r in db.rows(con.execute("SELECT * FROM plans ORDER BY start_date, plan_id"))]


def get_plan(plan_id: int) -> dict | None:
    with db.connect() as con:
        r = con.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
    return _row(dict(r)) if r else None


def find_overlaps(start: str, end: str, payment_day: int, exclude_id: int | None = None) -> list[dict]:
    with db.connect() as con:
        cand = db.rows(con.execute(
            "SELECT plan_id, title, start_date, maturity_date, payment_day, monthly_amount FROM plans "
            "WHERE start_date <= ? AND maturity_date >= ? AND plan_id IS NOT ?", (end, start, exclude_id)))
    for c in cand:
        c["same_payment_day"] = c["payment_day"] == payment_day
        c["overlap_from"] = max(c["start_date"], start)
        c["overlap_to"] = min(c["maturity_date"], end)
    return cand


def _validate(p: dict) -> dict:
    p = dict(p)
    start = date.fromisoformat(str(p["start_date"]))
    if not p.get("maturity_date"):
        p["maturity_date"] = add_months(start, int(p["term_months"])).isoformat()
    if not p.get("payment_day"):
        p["payment_day"] = start.day
    p["start_date"] = start.isoformat()
    if isinstance(p.get("products"), (list, dict)):
        p["products"] = json.dumps(p["products"], ensure_ascii=False)
    return p


def create_plan(data: dict) -> tuple[dict, list[dict]]:
    p = _validate(data)
    overlaps = find_overlaps(p["start_date"], p["maturity_date"], int(p["payment_day"]))
    with db.connect() as con:
        cur = con.execute(
            f"INSERT INTO plans({', '.join(FIELDS)}) VALUES({', '.join('?' * len(FIELDS))})",
            [p.get(f) for f in FIELDS])
        pid = cur.lastrowid
    return get_plan(pid), overlaps


def update_plan(plan_id: int, changes: dict) -> tuple[dict | None, list[dict]]:
    cur_plan = get_plan(plan_id)
    if cur_plan is None:
        return None, []
    merged = {**cur_plan, **{k: v for k, v in changes.items() if v is not None}}
    if "term_months" in changes or "start_date" in changes:
        if "maturity_date" not in changes:
            merged["maturity_date"] = None
    p = _validate(merged)
    sets = [f for f in FIELDS if f in p]
    with db.connect() as con:
        con.execute(f"UPDATE plans SET {', '.join(f'{f}=?' for f in sets)}, updated_at=datetime('now','localtime') "
                    f"WHERE plan_id=?", [p[f] for f in sets] + [plan_id])
    overlaps = find_overlaps(p["start_date"], p["maturity_date"], int(p["payment_day"]), exclude_id=plan_id)
    return get_plan(plan_id), overlaps


def delete_plan(plan_id: int) -> bool:
    with db.connect() as con:
        n = con.execute("DELETE FROM plans WHERE plan_id=?", (plan_id,)).rowcount
        con.execute("DELETE FROM calendar_events WHERE plan_id=?", (plan_id,))
    return n > 0


def overlap_message(new_monthly: int, overlaps: list[dict]) -> str | None:
    if not overlaps:
        return None
    total = new_monthly + sum(o["monthly_amount"] for o in overlaps)
    names = ", ".join(f"#{o['plan_id']} {o['title']}" for o in overlaps)
    same = [o for o in overlaps if o["same_payment_day"]]
    msg = f"납입 기간이 겹치는 계획이 {len(overlaps)}개 있습니다({names}). 겹치는 기간에는 매달 합계 {total:,}원이 나갑니다."
    if same:
        msg += f" 이 중 {len(same)}개는 납입일도 같아 같은 날 출금이 몰립니다."
    return msg
