import os
import re
import csv
import json
import time
import logging
import asyncio
import urllib.request
from typing import List, Dict, Optional, Tuple

from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse

# -------------------------------
# Logging
# -------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:  %(message)s")
logger = logging.getLogger("uvicorn.error")

# -------------------------------
# ENV
# -------------------------------
OPENAI_API_KEY      = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL        = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")
OPENAI_DEADLINE_MS  = int(os.getenv("OPENAI_DEADLINE_MS", "12000"))   # LLM 예산(초과시 드래프트)
MAX_TOKENS          = int(os.getenv("MAX_TOKENS", "480"))

USE_KAKAO_CALLBACK  = os.getenv("USE_KAKAO_CALLBACK", "1") == "1"
CALLBACK_MAX_MS     = int(os.getenv("CALLBACK_MAX_MS", "45000"))      # 콜백 토큰 유효시간
CALLBACK_WAIT_TEXT  = os.getenv("CALLBACK_WAIT_TEXT", "생각을 정리하고 있어요 😊 최대 15초 정도 걸려요.")
FAST_ONLY           = os.getenv("FAST_ONLY", "0") == "1"

GUARD_ENABLED       = os.getenv("GUARD_ENABLED", "1") == "1"

DEFAULT_DATA_DIR    = os.path.join(os.path.dirname(__file__), "data")
DEFAULT_DOCS_DIR    = os.path.join(os.path.dirname(__file__), "docs")
DATA_DIR            = os.getenv("DATA_DIR", DEFAULT_DATA_DIR).rstrip("/")
DOCS_DIR            = os.getenv("DOCS_DIR", DEFAULT_DOCS_DIR).rstrip("/")

# -------------------------------
# Files
# -------------------------------
def read_csv_dicts(filename: str) -> List[Dict]:
    path = os.path.join(DATA_DIR, filename)
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))
    except Exception as e:
        logger.warning(f"[CSV] {filename} read fail: {e}")
        return []

def read_md(filename: str) -> str:
    path = os.path.join(DOCS_DIR, filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""

README_TXT  = read_md("README_jeju_planner_v1.md")
RULE_TXT    = read_md("jeju_rule_engine_spec.md")
ARRIVED_TXT = read_md("jeju_arrived_mode_prompt_hook.md")

# -------------------------------
# Kakao helpers
# -------------------------------
def kakao_text(text: str) -> dict:
    return {"version": "2.0", "template": {"outputs": [{"simpleText": {"text": text}}]}}

def kakao_bubble(text: str) -> dict:
    return {"version": "2.0", "template": {"outputs": [{"simpleText": {"text": text}}]}}

def is_short_greeting(text: str) -> bool:
    t = re.sub(r"\s+", "", text or "")
    return t in {"안녕", "안녕하세요", "hi", "hello", "ㅎㅇ", "하이"}

# 내부 공개요구만 차단 (오탐 최소화 + 토글)
def is_internal_probe(text: str) -> bool:
    if not GUARD_ENABLED:
        return False
    if not text:
        return False
    t = (text or "").lower()

    sens = r"(system\s*prompt|시스템\s*프롬프트|internal|내부|지침|룰엔진|rule\s*engine|설정|spec|스펙|prompt)"
    verb = r"(보여줘|공개|원문|원본|출력|덤프|누설|노출|설명|어떻게|코드|소스|source)"
    pat  = rf"({sens}.*{verb}|{verb}.*{sens})"

    hit = re.search(pat, t)
    if hit:
        logger.warning(f"[Guard HIT] text='{t[:160]}' match='{hit.group(0)}'")
        return True
    return False

# -------------------------------
# Jeju draft (RAT: Retrieval + Augmented Templating)
# -------------------------------
ASK_FLOW = [
    "몇 박을 머무실 예정인가요?",
    "숙소 유형은 무엇인가요? (호텔/리조트/일반호텔/펜션/민박/여관)",
    "여행 분위기는 어디에 집중하시나요? (도시·문화 / 산·자연 / 바다·해변)",
    "음식 취향은 어떤가요? (해산물 / 한식 / 카페·디저트 / 가성비 / 특별한 경험식당 등)",
    "(선택) 동행 인원·구성을 알려주세요. (커플 / 가족(아이 포함) / 친구 / 단체 등)"
]

def short_greeting_reply() -> str:
    return (
        "📌 여행 기본 팁\n"
        "먼저 여행 조건 몇 가지만 알려주시면 딱 맞게 추천해드릴게요.\n\n"
        "📍 추천 여행지 & 코스 아이디어\n"
        f"1) {ASK_FLOW[0]}\n2) {ASK_FLOW[1]}\n3) {ASK_FLOW[2]}\n4) {ASK_FLOW[3]}\n5) {ASK_FLOW[4]}\n\n"
        "🍽️ 맛집 추천\n"
        "조건을 알려주시면 동선 맞춰 2~3곳 추천드릴게요.\n\n"
        "최신 운영시간과 예약은 공식 안내 확인이 필요합니다."
    )

def filter_blacklist(pois: List[Dict], bl: List[Dict]) -> List[Dict]:
    blocked = {
        (r.get("poi_id") or r.get("name") or "").strip()
        for r in bl if (r.get("severity") or "").lower() == "high"
    }
    return [p for p in pois if (p.get("poi_id") or p.get("name") or "").strip() not in blocked]

def apply_congestion(pois: List[Dict], rules: List[Dict]) -> Tuple[List[Dict], bool]:
    high = {(r.get("area") or "").strip() for r in rules if (r.get("level") or "").lower() == "high"}
    filtered = [p for p in pois if (p.get("area") or "").strip() not in high]
    return (filtered or pois, len(filtered) < len(pois))

def pick_courses() -> List[Dict]:
    items = read_csv_dicts("jeju_hotel_halftime_courses.csv")
    if not items:
        items = read_csv_dicts("jeju_sample_halfday_courses.csv")
    return items[:3]

def build_draft(utter: str) -> str:
    bl  = read_csv_dicts("jeju_access_blacklist.csv")
    cg  = read_csv_dicts("jeju_congestion_rules.csv")
    raw = pick_courses()
    pois = filter_blacklist(raw, bl)
    pois, congested = apply_congestion(pois, cg)

    tips = [
        "이동 시간은 여유 있게 30~40분 단위로 잡아주세요.",
        "바람이 강할 수 있어 바람막이/우산을 준비하세요.",
        "주요 스팟은 주차 대기가 발생할 수 있어요.",
    ]
    if congested:
        tips.insert(0, "혼잡 구간이 있어 대체 시간대/인근 코스를 권장해요.")

    course_lines = [
        f"- {p.get('name') or p.get('title','추천 코스')} ({p.get('area','')}) — 공식 안내 확인 필요"
        for p in pois
    ] or ["- 반나절 2~3곳 위주로 이동 동선 최소화"]

    eat_lines = [
        "- 인근 해산물/한식 위주로 동선 맞춰 추천",
        "- 카페·디저트 1곳 포함해 휴식 동선 구성",
    ]

    return (
        "📌 여행 기본 팁\n" + "\n".join(tips[:5]) + "\n\n" +
        "📍 추천 여행지 & 코스 아이디어\n" + "\n".join(course_lines[:5]) + "\n\n" +
        "🍽️ 맛집 추천\n" + "\n".join(eat_lines[:5]) + "\n\n" +
        "최신 운영시간과 예약은 공식 안내 확인이 필요합니다."
    )

# -------------------------------
# OpenAI client
# -------------------------------
client: Optional[object] = None
if OPENAI_API_KEY and not FAST_ONLY:
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        logger.info("[OpenAI] client init ok")
    except Exception as e:
        logger.warning(f"[OpenAI] client init fail: {e}")
        client = None
else:
    if not OPENAI_API_KEY:
        logger.warning("[OpenAI] missing API key")
    if FAST_ONLY:
        logger.info("[OpenAI] FAST_ONLY=1 (LLM disabled)")

# -------------------------------
# FastAPI app (global)
# -------------------------------
app = FastAPI(title="Jeju ChatPi (Callback)", version="2.3.0")

# -------------------------------
# LLM Polish
# -------------------------------
async def polish_with_llm(utter: str, draft: str, timeout_s: float) -> Optional[str]:
    if FAST_ONLY or not client:
        return None
    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": (
                    "너는 “제주도 여행플래너 챗피(Jeju Travel Planner ChatPi)”. "
                    "제주관광공사·제주시청 등 공식 자료에 기반해 정확히 안내한다.\n\n"
                    "출력 형식(각 섹션 5줄 이내)\n"
                    "📌 여행 기본 팁\n📍 추천 여행지 & 코스 아이디어\n🍽️ 맛집 추천\n"
                    "항상 마지막 줄: 최신 운영시간과 예약은 공식 안내 확인이 필요합니다."
                )},
                {"role": "user", "content": utter},
                {"role": "system", "content": "아래 초안을 제주 여행 스타일로 간결하게 다듬어 출력:\n" + draft},
            ],
            temperature=0.2,
            max_tokens=MAX_TOKENS,
            timeout=timeout_s,
        )
        text = (resp.choices[0].message.content or "").strip()
        return text if text else None
    except Exception as e:
        logger.warning(f"[OpenAI] error: {e}")
        return None

# -------------------------------
# Callback sender (small retry)
# -------------------------------
def post_callback(callback_url: str, payload: dict) -> Tuple[bool, str]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        callback_url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    for attempt in range(1, 3):
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                body = r.read().decode("utf-8", "ignore")
            return True, body
        except Exception as e:
            if attempt == 2:
                return False, str(e)
            time.sleep(0.6)
    return False, "unknown"

# -------------------------------
# Routes
# -------------------------------
@app.get("/")
def root():
    return {
        "ok": True,
        "mode": "callback" if USE_KAKAO_CALLBACK else "direct",
        "model": OPENAI_MODEL
    }

@app.get("/health")
def health():
    return {
        "ok": True,
        "use_callback": USE_KAKAO_CALLBACK,
        "fast_only": FAST_ONLY,
        "guard_enabled": GUARD_ENABLED,
        "model": OPENAI_MODEL,
        "deadline_ms": OPENAI_DEADLINE_MS,
        "data_dir": DATA_DIR,
        "docs_dir": DOCS_DIR,
    }

@app.post("/kakao/skill")
async def kakao_skill(request: Request, background_tasks: BackgroundTasks):
    try:
        body = await request.json()
    except Exception:
        body = {}

    user_req    = (body.get("userRequest") or {})
    utter       = (user_req.get("utterance") or "").strip()
    callbackUrl = user_req.get("callbackUrl")

    # 내부 정보 차단 (정말 '내부 공개 요구'일 때만)
    if is_internal_probe(utter):
        logger.warning(f"[Guard] internal probe: {utter}")
        return JSONResponse(kakao_text("비밀이에요 🤫 공식적으로 공개되지 않은 정보입니다."))

    # 짧은 인사 즉시 처리 (LLM 없이)
    if is_short_greeting(utter):
        text = short_greeting_reply()
        logger.info(f"[ReplyText] {text[:200].replace(os.linesep,' ')}")
        return JSONResponse(kakao_text(text))

    # 초고속 드래프트 생성 (CSV + 규칙)
    draft = build_draft(utter)

    # ===== 콜백 모드 =====
    if USE_KAKAO_CALLBACK and callbackUrl:
        logger.info("[Callback] useCallback start")

        # 즉시 대기 응답 (템플릿 없이 data만 반환 → 콘솔에서 '스킬데이터 사용'으로 매핑 가능)
        immediate = {"version": "2.0", "useCallback": True, "data": {"text": CALLBACK_WAIT_TEXT}}

        async def job():
            # 콜백 유효시간 안에서 LLM 예산 설정 (최대 20초)
            llm_budget_s = min(max((CALLBACK_MAX_MS - 2000) / 1000.0, 1.0), 20.0)
            final_text = await polish_with_llm(utter, draft, llm_budget_s)
            if not final_text:
                final_text = draft

            logger.info(f"[CallbackText] {final_text[:200].replace(os.linesep,' ')}")
            payload = kakao_bubble(final_text)
            ok, msg = post_callback(callbackUrl, payload)
            logger.info(f"[Callback] sent={ok} msg={msg[:180]}")

        background_tasks.add_task(job)
        return JSONResponse(immediate)

    # ===== 일반(비 콜백) 모드 =====
    if FAST_ONLY or not client:
        logger.info("[Reply] DRAFT (FAST_ONLY or no client)")
        logger.info(f"[ReplyText] {draft[:200].replace(os.linesep,' ')}")
        return JSONResponse(kakao_text(draft))

    async def call_llm():
        return await polish_with_llm(utter, draft, OPENAI_DEADLINE_MS / 1000.0)

    try:
        answer = await asyncio.wait_for(call_llm(), timeout=(OPENAI_DEADLINE_MS / 1000.0 + 0.3))
        if answer:
            logger.info("[Reply] LLM OK")
            logger.info(f"[ReplyText] {answer[:200].replace(os.linesep,' ')}")
            return JSONResponse(kakao_text(answer))
        logger.info("[Reply] DRAFT (no LLM)")
        logger.info(f"[ReplyText] {draft[:200].replace(os.linesep,' ')}")
        return JSONResponse(kakao_text(draft))
    except asyncio.TimeoutError:
        logger.info("[Reply] DRAFT (timeout)")
        logger.info(f"[ReplyText] {draft[:200].replace(os.linesep,' ')}")
        return JSONResponse(kakao_text(draft))
