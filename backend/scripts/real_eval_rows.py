"""실제 데이터 측정값 (2026-11-20, 금감원 / ECOS / 카카오 / GMS gpt-5.4-nano)

브라우저에서 /api/chat/stream 으로 S1~S9 x 세 구조 실행하고
세션별 /api/logs/{id}/anomalies 판정까지 모은 것. real_report.py 에서 씀
"""
# scenario, name, mode, request, type, ms, llm, tool, cache, replans, recs, verdict, findings, worker, session
R = [
 ("S1","한 번에 충족","single","1년 안에 1000만원 모으고 싶어. 월 90만원까지 넣을 수 있어","plan",5663,2,7,0,0,3,"normal","","","c83bb2e922"),
 ("S1","한 번에 충족","pw","","plan",3742,2,7,0,0,3,"normal","","","4eae20716a"),
 ("S1","한 번에 충족","pw-off","","plan",3898,2,7,0,0,3,"normal","","","a2d1a2408d"),
 ("S2","재계획 후 충족","single","1년 안에 5000만원 모으고 싶어. 월 409만원까지 넣을 수 있어","plan",6444,2,19,1,2,1,"normal","","","d50e5b7ef4"),
 ("S2","재계획 후 충족","pw","","plan",5071,2,19,1,2,1,"normal","","","e6acc373a4"),
 ("S2","재계획 후 충족","pw-off","","plan",4810,2,19,1,2,1,"normal","","","907b9e8bae"),
 ("S3","재계획 상한 도달","single","1년 안에 5000만원 모으고 싶어. 월 100만원까지","plan",4680,2,27,1,3,0,"anomaly","excessive_replan","","46906c6e48"),
 ("S3","재계획 상한 도달","pw","","plan",6093,2,27,1,3,0,"anomaly","excessive_replan","","ca648f2fa8"),
 ("S3","재계획 상한 도달","pw-off","","plan",5166,2,27,1,3,0,"anomaly","excessive_replan","","4351022bc1"),
 ("S4","Tool 실패 (영업점 없음)","single","1년 안에 1000만원 모으고 싶어. 월 90만원까지. 울릉도 근처","plan",4889,2,8,0,0,3,"normal","","","83eb02f207"),
 ("S4","Tool 실패 (영업점 없음)","pw","","plan",6468,3,8,0,0,3,"normal","","skip (region 변경 차단)","0232b0b5f2"),
 ("S4","Tool 실패 (영업점 없음)","pw-off","","plan",4658,2,8,0,0,3,"normal","","","258f55c56c"),
 ("S5","Tool 불필요 (인사)","single","안녕, 뭐 할 수 있어?","chitchat",3764,2,0,0,0,0,"normal","","","14fe1ec910"),
 ("S5","Tool 불필요 (인사)","pw","","chitchat",3698,2,0,0,0,0,"normal","","","ff2c443580"),
 ("S5","Tool 불필요 (인사)","pw-off","","chitchat",3302,2,0,0,0,0,"normal","","","e29537aff0"),
 ("S6","조건 부족 → 되묻기","single","돈 좀 모으고 싶어. 월 50만원 가능해","ask",1818,1,0,0,0,0,"normal","","","482a8faacb"),
 ("S6","조건 부족 → 되묻기","pw","","ask",1975,1,0,0,0,0,"normal","","","a5ced1a15d"),
 ("S6","조건 부족 → 되묻기","pw-off","","ask",1728,1,0,0,0,0,"normal","","","ea7ae27d65"),
 ("S7","제도 질문 (RAG)","single","예금자보호 한도가 얼마야?","knowledge",3752,2,1,0,0,0,"normal","","","b8b7520dd6"),
 ("S7","제도 질문 (RAG)","pw","","knowledge",3875,2,1,0,0,0,"normal","","","ce1595464d"),
 ("S7","제도 질문 (RAG)","pw-off","","knowledge",3999,2,1,0,0,0,"normal","","","ac6ff449cc"),
 ("S8","후속 요청 (조건 하나만 변경)","single","(S1 다음 턴) 월 85만원으로 바꿔줘","plan",3854,2,1,6,0,3,"normal","","","36b4802e96"),
 ("S8","후속 요청 (조건 하나만 변경)","pw","","plan",4909,2,1,6,0,3,"normal","","","29fdf8d0d6"),
 ("S8","후속 요청 (조건 하나만 변경)","pw-off","","plan",4418,2,1,6,0,3,"normal","","","7528ac3e4d"),
 ("S9","사실 조회","single","요즘 금리랑 물가 어때?","fact",5028,2,2,0,0,0,"normal","","","35c79b8f80"),
 ("S9","사실 조회","pw","","fact",3928,2,2,0,0,0,"normal","","","c3580249f0"),
 ("S9","사실 조회","pw-off","","fact",4341,2,2,0,0,0,"normal","","","7b8c7b5831"),
]
KEYS = ["scenario","name","mode","request","type","ms","llm_calls","tool_calls","cache_hits","replans","recs","verdict","findings","worker","session_id"]
ROWS = [dict(zip(KEYS, r)) for r in R]
