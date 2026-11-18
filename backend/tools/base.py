"""Tool 공통 규칙

성공: {"ok": True, "data": {...}}
실패: {"ok": False, "error": {"code": "...", "message": "..."}}
예외는 밖으로 안 던짐 (Tool.run 에서 다 잡음).
LLM 한테는 이름, 설명, 입력 스키마만 넘김
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Callable, Literal

# 실패 코드
# fixable: 인자를 바꾸면 결과가 달라질 수 있는지. Worker 는 이게 True 일 때만 LLM 을 부름
FAILURE_CODES: dict[str, dict] = {
    "BAD_ARGUMENT":   {"fixable": True,  "desc": "입력이 규격과 다르다 (빈 값 · 형식 틀림 · 모르는 인자)"},
    "MISSING_ARGUMENT": {"fixable": False, "desc": "필수 인자가 없다 — 사용자에게 되물어야 한다"},
    "INVALID_KEY":    {"fixable": False, "desc": "API 키가 없거나 틀렸다 — 다시 시도해도 같다"},
    "NOT_FOUND":      {"fixable": True,  "desc": "찾는 대상(식별자)이 없다"},
    "NO_DATA":        {"fixable": True,  "desc": "조회는 됐지만 조건에 맞는 결과가 비었다"},
    "NO_RESULT":      {"fixable": True,  "desc": "검색 결과가 없다 (보조 정보 검색)"},
    "AMBIGUOUS":      {"fixable": False, "desc": "같은 이름이 여러 곳 — 사용자에게 되물어야 한다"},
    "NO_RATE":        {"fixable": False, "desc": "상품에 해당 기간 금리 정보가 없다 — 판단하지 않고 제외"},
    "NO_INDEX":       {"fixable": False, "desc": "RAG 색인이 아직 없다"},
    "RATE_LIMITED":   {"fixable": False, "desc": "호출 한도 초과"},
    "UPSTREAM_ERROR": {"fixable": False, "desc": "외부 API 가 응답하지 않았다 (시간 초과 · 서버 오류)"},
}


def ok(data) -> dict:
    return {"ok": True, "data": data}


def fail(code: str, message: str, **extra) -> dict:
    err = {"code": code, "message": message}
    err.update(extra)
    return {"ok": False, "error": err}


def is_fixable(result: dict) -> bool:
    if result.get("ok"):
        return False
    return FAILURE_CODES.get(result["error"]["code"], {}).get("fixable", False)


# 키 가리기
SECRET_ENV_NAMES = ["FINLIFE_API_KEY", "ECOS_API_KEY", "KAKAO_REST_API_KEY", "GMS_API_KEY"]
_PARAM = re.compile(r"(?i)\b(key|apikey|auth|token|serviceKey)=([^&\s'\"]+)")


def scrub(text) -> str:
    """오류 메시지나 로그에 키가 안 나가게 값이랑 파라미터 이름 둘 다 가림"""
    text = str(text)
    for name in SECRET_ENV_NAMES:
        secret = os.getenv(name, "")
        if secret and len(secret) > 4:
            text = text.replace(secret, "***")
    text = _PARAM.sub(lambda m: f"{m.group(1)}=***", text)
    text = re.sub(r"(KakaoAK\s+)\S+", r"\1***", text)  # 카카오 인증 헤더
    # ECOS 는 키가 URL 경로에 들어감
    text = re.sub(r"(StatisticSearch/)[^/\s]+", r"\1***", text)
    return text


# Tool 정의
@dataclass
class Tool:
    name: str
    description: str  # LLM 이 읽으니까 '언제 부르는지'를 적음
    parameters: dict  # JSON Schema
    func: Callable
    kind: Literal["external", "local"] = "external"
    source: str = ""  # 출처
    output_schema: dict = field(default_factory=dict)

    def run(self, **kwargs) -> dict:
        allowed = set(self.parameters.get("properties", {}))
        unknown = set(kwargs) - allowed
        if unknown:
            return fail("BAD_ARGUMENT", f"모르는 인자: {', '.join(sorted(unknown))}")
        for req in self.parameters.get("required", []):
            if kwargs.get(req) in (None, ""):
                return fail("MISSING_ARGUMENT", f"{req} 는 필수 입력입니다.")
        try:
            out = self.func(**kwargs)
        except TypeError as e:
            return fail("BAD_ARGUMENT", f"입력값이 규약과 다릅니다: {scrub(e)}")
        except Exception as e:  # 여기서 다 잡아서 서비스가 안 멈추게
            return fail("UPSTREAM_ERROR", f"Tool 실행 중 오류: {type(e).__name__}")
        if not isinstance(out, dict) or "ok" not in out:
            return fail("UPSTREAM_ERROR", "Tool 이 약속한 형식으로 답하지 않았습니다.")
        return out

    def card(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


class Registry:
    """Tool 등록부"""

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def run(self, name: str, **kwargs) -> dict:
        tool = self.get(name)
        if tool is None:
            return fail("NOT_FOUND", f"등록되지 않은 Tool: {name}")
        return tool.run(**kwargs)

    def catalog(self, kind: str | None = None) -> list[dict]:
        return [t.card() for t in self._tools.values() if kind is None or t.kind == kind]

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def __contains__(self, name):
        return name in self._tools


REGISTRY = Registry()


def fake_mode() -> bool:
    """MOAPLAN_FAKE=1 이면 키 없이 fixture 로 응답"""
    return os.getenv("MOAPLAN_FAKE", "0") == "1"
