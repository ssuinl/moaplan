"""우대금리 조건 반영

사용자가 '할 수 있다'고 말한 조건(급여이체, 카드 실적 등)을 공시 우대조건 문구에서 찾아서
그 줄에 적힌 우대금리만 더한다. 최고금리(intr_rate2)는 넘지 않게 자름.
문구에서 금리 숫자를 못 찾으면 더하지 않음 (지어내지 않기)
"""
from __future__ import annotations

import re

CONDS = {
    "급여이체": ["급여", "월급", "연금"],
    "자동이체": ["자동이체"],
    "카드실적": ["카드"],
    "첫거래": ["첫거래", "첫 거래", "신규고객", "신규 고객", "최초"],
    "마케팅동의": ["마케팅"],
    "비대면가입": ["비대면", "인터넷", "스마트폰", "모바일", "앱"],
    "공과금이체": ["공과금", "관리비", "아파트"],
    "청약보유": ["청약"],
}

# 사용자 말에서 조건 찾기 (FakeLLM, 규칙 보정용)
SAY = {
    "급여이체": r"(급여|월급)\s*(이체|통장|들어|받)",
    "자동이체": r"자동\s*이체",
    "카드실적": r"카드\S{0,2}\s*(\S+\s*)?(실적|사용|쓰|써)",
    "첫거래": r"(첫\s*거래|처음\s*(거래|가입)|신규\s*고객)",
    "마케팅동의": r"마케팅",
    "비대면가입": r"(비대면|앱으로|인터넷으로|모바일로)",
    "공과금이체": r"(공과금|관리비)",
    "청약보유": r"청약",
}

_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*p?", re.I)


def from_text(text: str) -> list[str]:
    return [k for k, pat in SAY.items() if re.search(pat, text or "")]


def _segments(special: str) -> list[str]:
    s = (special or "").replace("\r", "\n")
    s = re.sub(r"\s*(①|②|③|④|⑤|⑥|⑦|⑧|⑨)", r"\n\1", s)
    s = re.sub(r"\s+(\d\)|\(\d\)|\d\.\s|-\s)", r"\n\1", s)
    return [x.strip() for x in s.split("\n") if x.strip()]


def items(special: str) -> list[dict]:
    """우대조건 문구 -> [{conds, rate, text}] (금리 숫자가 있는 줄만)"""
    out = []
    for seg in _segments(special):
        if re.search(r"(최고|최대)\s*(연\s*)?\d", seg):  # '최고 연 1.0%p' 같은 합계 줄은 뺌
            continue
        rates = [float(x) for x in _PCT.findall(seg) if 0 < float(x) <= 3.0]
        if not rates:
            continue
        conds = [k for k, kws in CONDS.items() if any(w in seg for w in kws)]
        if conds:
            out.append({"conds": conds, "rate": max(rates), "text": seg[:90]})
    return out


def apply(product: dict, user_conds: list[str] | None) -> tuple[float, list[dict]]:
    """사용자 조건에 맞는 우대만 더한 금리, 적용 내역"""
    base, top = product["base_rate"], product.get("max_rate") or product["base_rate"]
    if not user_conds:
        return base, []
    applied, extra = [], 0.0
    for it in items(product.get("special_condition", "")):
        hit = [c for c in it["conds"] if c in user_conds]
        if hit:
            applied.append({"cond": hit[0], "rate": it["rate"], "text": it["text"]})
            extra += it["rate"]
    rate = round(min(base + extra, top), 2)
    return rate, applied
