"""대표 시나리오 평가

python -m scripts.evaluate          실제 키
python -m scripts.evaluate --fake   연습 데이터

같은 시나리오를 세 구조로 돌려서 비교
  single  메인 Agent 하나
  pw      Planner / Worker / 로깅 Agent, Worker LLM 켬
  pw-off  위와 같은데 Worker LLM 끔
결과는 docs/evaluation/ (--fake 면 fake/)
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT.parent / "docs"
sys.path.insert(0, str(ROOT))

SCENARIOS = [
    {"id": "S1", "name": "한 번에 충족", "turns": ["1년 안에 1000만원 모으고 싶어. 월 90만원까지 넣을 수 있어"],
     "expect": {"type": "plan", "recs": True, "replans": 0}},
    {"id": "S2", "name": "재계획 후 충족", "turns": ["1년 안에 1000만원 모으고 싶어. 월 82만원까지 넣을 수 있어"],
     "expect": {"type": "plan", "recs": True, "replans_min": 1}},
    {"id": "S3", "name": "재계획 상한 도달", "turns": ["1년 안에 5000만원 모으고 싶어. 월 100만원까지"],
     "expect": {"type": "plan", "recs": False, "replans": 3, "adjust": True}},
    {"id": "S4", "name": "Tool 하나 실패 (영업점 검색)", "turns": ["2년 안에 1200만원 모으고 싶어. 월 50만원까지. 강남 근처"],
     "expect": {"type": "plan", "recs": True, "branch_fail_ok": True}},
    {"id": "S5", "name": "Tool 불필요 (인사)", "turns": ["안녕, 뭐 할 수 있어?"],
     "expect": {"type": "chitchat", "tools": 0}},
    {"id": "S6", "name": "조건 부족 → 되묻기", "turns": ["돈 좀 모으고 싶어. 월 50만원 가능해"],
     "expect": {"type": "ask", "tools": 0}},
    {"id": "S7", "name": "제도 질문 (RAG)", "turns": ["예금자보호 한도가 얼마야?"],
     "expect": {"type": "knowledge", "tools": 1}},
    {"id": "S8", "name": "후속 요청 (조건 하나만 변경)",
     "turns": ["1년 안에 1000만원 모으고 싶어. 월 90만원까지 넣을 수 있어", "월 85만원으로 바꿔줘"],
     "expect": {"type": "plan", "recs": True, "cache_hits_min": 1}},
]
MODES = [("single", "single", True), ("pw", "pw", True), ("pw-off", "pw", False)]


def check(sc, res, mode_key):
    e, v, st = sc["expect"], res["view"], res["stats"]
    diffs = []
    if v.get("type") != e["type"]:
        diffs.append(f"응답 유형 {v.get('type')} (기대 {e['type']})")
    if "recs" in e and bool(v.get("recommendations")) != e["recs"]:
        diffs.append("추천 유무가 기대와 다름")
    if "replans" in e and st["replans"] != e["replans"]:
        diffs.append(f"재계획 {st['replans']}회 (기대 {e['replans']})")
    if "replans_min" in e and st["replans"] < e["replans_min"]:
        diffs.append(f"재계획 {st['replans']}회 (기대 ≥{e['replans_min']})")
    if "tools" in e and st["tool_calls"] != e["tools"]:
        diffs.append(f"Tool {st['tool_calls']}회 (기대 {e['tools']})")
    if e.get("adjust") and not v.get("adjustments"):
        diffs.append("조정 방안 없음")
    if "cache_hits_min" in e and st["cache_hits"] < e["cache_hits_min"]:
        diffs.append("후속 요청에서 캐시 재사용 없음")
    if e.get("branch_fail_ok"):
        b = list((v.get("branches") or {}).values())
        found = bool(b and "branches" in b[0])
        if mode_key == "pw" and not found:
            diffs.append("Worker 가 영업점 검색 실패를 고치지 못함")
    return diffs


def run(fake: bool):
    if fake:
        os.environ["MOAPLAN_FAKE"] = "1"
        os.environ["MOAPLAN_LLM"] = "fake"
    else:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    tmp = tempfile.mkdtemp()
    os.environ["MOAPLAN_DB"] = str(Path(tmp) / "eval.db")
    os.environ["MOAPLAN_CHROMA"] = str(Path(tmp) / "chroma")
    from app import db
    db.init_db()
    from agent import service
    from agent.llm import make_llm
    from events import anomaly
    from events.bus import BUS
    from events.logging_agent import LoggingAgent, get_session_log
    from tools.rag_tool import build_index
    build_index()
    LoggingAgent(BUS, on_run_end=lambda s, r: anomaly.run_and_store(s, r, make_llm())).start()

    rows, samples = [], {}
    for sc in SCENARIOS:
        for key, mode, wl in MODES:
            service._SESSION_CACHE.clear()
            sid = None
            for turn in sc["turns"]:
                res = service.run_chat(turn, sid, mode=mode, worker_llm=wl)
                sid = res["session_id"]
            rep = anomaly.latest_report(sid) or {}
            diffs = check(sc, res, key)
            rows.append({"scenario": sc["id"], "name": sc["name"], "mode": key, "request": sc["turns"][-1],
                         **res["stats"], "type": res["view"].get("type"),
                         "recommendations": len(res["view"].get("recommendations") or []),
                         "top": (res["view"].get("recommendations") or [{}])[0].get("label"),
                         "answer": res["answer"][:400], "verdict": rep.get("verdict"),
                         "worker_decisions": sum(1 for e in res["events"] if e["event"] == "worker.decision"),
                         "diffs": diffs, "session_id": sid})
            if key == "single" and sc["id"] in ("S1", "S3"):
                samples[sc["id"]] = get_session_log(sid)
            if key == "pw" and sc["id"] == "S4":
                samples["S4_pw"] = get_session_log(sid)
    # 이상 징후 테스트 (고장 주입)
    inj = []
    for fault, mode in (("dup_call", "single"), ("rogue_call", "single"), ("rogue_call", "pw")):
        res = service.run_chat(SCENARIOS[0]["turns"][0], mode=mode, faults={fault})
        rep = anomaly.latest_report(res["session_id"])
        inj.append({"fault": fault, "mode": mode, "verdict": rep["verdict"],
                    "findings": [f["type"] for f in rep["findings"]], "diagnosis": rep["diagnosis"],
                    "replans": res["stats"]["replans"], "cache_hits": res["stats"]["cache_hits"]})
    return rows, samples, inj


def write(rows, samples, inj, fake):
    out = DOCS / "evaluation" / ("fake" if fake else "")
    out.mkdir(parents=True, exist_ok=True)
    sdir = DOCS / "samples" / ("fake" if fake else "")
    sdir.mkdir(parents=True, exist_ok=True)
    data = {"fake": fake, "rows": rows, "fault_injection": inj}
    (out / "results.json").write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    for k, v in samples.items():
        (sdir / f"log_{k}.json").write_text(json.dumps(v, ensure_ascii=False, indent=1), encoding="utf-8")

    charts(rows, out)
    md = [f"# 대표 시나리오 평가 결과 ({'연습 데이터 · 규칙 LLM' if fake else '실제 API · GMS'})", "",
          "`python -m scripts.evaluate" + (" --fake" if fake else "") + "` 로 다시 만들 수 있다.", "",
          "## 시나리오별 결과", "",
          "| ID | 시나리오 | 구조 | 응답 | 추천 수 | 재계획 | Tool 호출 | 캐시 재사용 | LLM 호출 | 시간(ms) | 판정 | 기대와 차이 |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        md.append(f"| {r['scenario']} | {r['name']} | {r['mode']} | {r['type']} | {r['recommendations']} | {r['replans']} | "
                  f"{r['tool_calls']} | {r['cache_hits']} | {r['llm_calls']} | {r['ms']} | {r['verdict']} | "
                  f"{'; '.join(r['diffs']) or '없음'} |")
    md += ["", "## 구조별 평균 (요청당)", "", "| 구조 | LLM 호출 | Tool 호출 | 재계획 | 응답 시간(ms) | 기대와 다른 시나리오 |", "|---|---|---|---|---|---|"]
    for key, _, _ in MODES:
        rs = [r for r in rows if r["mode"] == key]
        md.append(f"| {key} | {statistics.mean(r['llm_calls'] for r in rs):.2f} | {statistics.mean(r['tool_calls'] for r in rs):.2f} | "
                  f"{statistics.mean(r['replans'] for r in rs):.2f} | {statistics.mean(r['ms'] for r in rs):.0f} | "
                  f"{sum(1 for r in rs if r['diffs'])} |")
    md += ["", "![요청당 LLM 호출 수](llm_calls.png)", "", "![요청당 Tool 호출 수](tool_calls.png)", "",
           "![응답 시간](latency.png)", "",
           "## 이상 징후 판정 시험 (고장 주입, F3020)", "", "| 주입 | 구조 | 판정 | 찾은 징후 | 재계획 | 캐시 재사용 | LLM 진단 |", "|---|---|---|---|---|---|---|"]
    for i in inj:
        md.append(f"| {i['fault']} | {i['mode']} | {i['verdict']} | {', '.join(i['findings'])} | {i['replans']} | "
                  f"{i['cache_hits']} | {i['diagnosis'] or ''} |")
    md += ["", "## 시나리오별 최종 응답 (single)", ""]
    for r in rows:
        if r["mode"] == "single":
            md += [f"**{r['scenario']} {r['name']}** — `{r['request']}`", "", "> " + r["answer"].replace("\n", "\n> "), ""]
    (out / "results.md").write_text("\n".join(md), encoding="utf-8")
    print(f"→ {out / 'results.md'}")


def charts(rows, out):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    for f in ["NanumGothic", "Noto Sans CJK KR", "Malgun Gothic", "AppleGothic", "IBM Plex Sans KR"]:
        if any(f in x.name for x in font_manager.fontManager.ttflist):
            plt.rcParams["font.family"] = f
            break
    plt.rcParams["axes.unicode_minus"] = False
    colors = {"single": "#2a78d6", "pw": "#eb6834", "pw-off": "#1baf7a"}  # 색 1~3번
    labels = {"single": "단일 Agent", "pw": "역할 분리 (Worker LLM 켬)", "pw-off": "역할 분리 (Worker LLM 끔)"}
    scen = list(dict.fromkeys(r["scenario"] for r in rows))
    for metric, title, fname in (("llm_calls", "요청당 LLM 호출 수", "llm_calls.png"),
                                 ("tool_calls", "요청당 Tool 호출 수", "tool_calls.png"),
                                 ("ms", "응답 시간 (ms)", "latency.png")):
        fig, ax = plt.subplots(figsize=(9, 3.6), dpi=140)
        w = 0.26
        for i, (key, _, _) in enumerate(MODES):
            vals = [next(r[metric] for r in rows if r["scenario"] == s and r["mode"] == key) for s in scen]
            xs = [x + (i - 1) * (w + 0.02) for x in range(len(scen))]
            ax.bar(xs, vals, width=w, color=colors[key], label=labels[key])
        short = {"S1": "한 번에 충족", "S2": "재계획 후 충족", "S3": "상한 도달", "S4": "Tool 실패",
                 "S5": "인사", "S6": "되묻기", "S7": "제도 질문", "S8": "후속 요청", "S9": "사실 조회"}
        ax.set_xticks(range(len(scen)), [f"{s}\n{short.get(s, s)}" for s in scen], fontsize=8)
        if metric != "ms":
            ax.yaxis.get_major_locator().set_params(integer=True)
        ax.set_title(title, loc="left", fontsize=11)
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
        ax.spines["left"].set_color("#c9c8c2"); ax.spines["bottom"].set_color("#c9c8c2")
        ax.grid(axis="y", color="#e6e5e0", linewidth=0.8); ax.set_axisbelow(True)
        ax.tick_params(colors="#52514e", labelsize=8)
        ax.legend(frameon=False, fontsize=8, ncol=3, loc="upper left", bbox_to_anchor=(0, -0.28))
        fig.tight_layout()
        fig.savefig(out / fname)
        plt.close(fig)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fake", action="store_true")
    a = ap.parse_args()
    rows, samples, inj = run(a.fake)
    write(rows, samples, inj, a.fake)
