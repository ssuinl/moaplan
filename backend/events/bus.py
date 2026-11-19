"""실행 이벤트 발행/구독

Agent 쪽은 emit() 만 부르고 누가 구독하는지 모름.
구독자가 없거나 에러가 나도 메인 흐름은 그대로 돌아감.
모든 이벤트에 session_id(대화), run_id(요청 한 번), seq(순서)가 붙음

이벤트 종류
  session.start / session.end   요청 시작, 끝
  step.enter / step.exit        그래프 단계 진입, 종료 (ms)
  decision                      LLM 판단과 이유
  plan                          작업 계획
  tool.call / tool.result       Tool 호출, 결과
  judge                         판정 결과
  replan                        재계획
  llm.call                      LLM 호출 1회
  worker.decision / planner.tasks   pw 모드 Agent 간 메시지
"""
from __future__ import annotations

import itertools
import threading
from datetime import datetime
from typing import Callable


class EventBus:
    def __init__(self):
        self._subs: list[Callable[[dict], None]] = []
        self._lock = threading.Lock()

    def subscribe(self, fn: Callable[[dict], None]) -> Callable[[], None]:
        with self._lock:
            self._subs.append(fn)

        def unsubscribe():
            with self._lock:
                if fn in self._subs:
                    self._subs.remove(fn)
        return unsubscribe

    def publish(self, msg: dict) -> dict:
        for fn in list(self._subs):
            try:
                fn(msg)
            except Exception:
                pass  # 구독자가 에러 나도 계속 진행
        return msg


class Emitter:
    """요청 한 번 동안 쓰는 발행기"""

    def __init__(self, bus: EventBus | None, session_id: str, run_id: str):
        self.bus, self.session_id, self.run_id = bus, session_id, run_id
        self._seq = itertools.count(1)
        self.local: list[dict] = []  # 이번 요청 이벤트 사본 (응답이랑 같이 화면에 보냄)

    def __call__(self, event: str, **payload) -> dict:
        msg = {"event": event, "session_id": self.session_id, "run_id": self.run_id,
               "seq": next(self._seq), "ts": datetime.now().isoformat(timespec="milliseconds"),
               "payload": payload}
        self.local.append(msg)
        if self.bus is not None:
            self.bus.publish(msg)
        return msg


BUS = EventBus()
