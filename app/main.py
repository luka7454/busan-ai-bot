import os
import json
import csv
import re
import logging
import asyncio
from typing import List, Dict, Optional, Tuple
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from openai import OpenAI, APIConnectionError, APITimeoutError

# -------------------------------
# Logging
# -------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:  %(message)s")
logger = logging.getLogger("uvicorn.error")

# -------------------------------
# ENV & Paths
# -------------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
MODEL = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")   # ✅ 빠른 모델
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "480"))
DEADLINE_MS = int(os.getenv("OPENAI_DEADLINE_MS", "2000"))  # ✅ 2초 제한
DISABLE_OPENAI = os.getenv("DISABLE_OPENAI", "0") == "1"

DEFAULT_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DEFAULT_DOCS_DIR = os.path.join(os.path.dirname(__file__), "docs")
DATA_DIR = os.getenv("DATA_DIR", DEFAULT_DATA_DIR).rstrip("/")
DOCS_DIR = os.getenv("DOCS_DIR", DEFAULT_DOCS_DIR).rstrip("/")

client: Optional[OpenAI] = None
if OPENAI_API_KEY and not DISABLE_OPENAI:
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        logger.warning(f"[OpenAI] client init fail: {e}")
        client = None

app = FastAPI(title="Jeju ChatPi Fast", version="1.0.0")

# -------------------------------
# File helpers
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
    for d in [DOCS_DIR, os.getcwd()]:
        p = os.path.join(d, filename)
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception:
                pass
    return ""

# -------------------------------
# Build Prompt
# -------------------------------
readme_text = read_md("README_jeju_planner_v1.md")
rule_spec_text = read_md("jeju_rule_engine_spec.md")
arrived_hook_text = read_md("jeju_arrived_mode_prompt_hook.md")

SYSTEM_PROMPT = f"""
너는 “제주도 여행플래너 챗피(Jeju Travel Planner ChatPi)”야.
제주관광공사·제주시청 등 공식 자료에 기반하여 정확하게 안내해.

[지침]
- CSV와 공식 자료를 우선 사용.
- 자연휴식년제, 혼잡 지역, 우천 등은 대체 코스 제안.
- 톤: 따뜻하지만 간결, 공식 데이터 기반.
- 출력 형식:
📌 여행 기본 팁
📍 추천 여행지 & 코스 아이디어
🍽️ 맛집 추천
마지막 줄: 최신 운영시간과 예약은 공식 안내 확인이 필요합니다.
"""

# -------------------------------
# Kakao helpers
# -------------------------------
def kakao_text(text: str) -> dict:
    return {"version": "2.0", "template": {"outputs": [{"simpleText": {"text": text}}]}}

def is_internal_probe(text: str) -> bool:
    if not text:
        return False
    keywords = ["지침", "룰엔진", "만들어졌", "csv", "데이터셋", "internal", "prompt"]
    return any(k in text for k in keywords)

def short_greeting_reply() -> str:
    return (
        "📌 여행 기본 팁\n"
        "먼저 여행 조건 몇 가지만 알려주시면 딱 맞게 추천해드릴게요.\n\n"
        "📍 추천 여행지 & 코스 아이디어\n"
        "1) 몇 박을 머무실 예정인가요?\n"
        "2) 숙소 유형은 무엇인가요? (호텔/리조트/펜션 등)\n"
        "3) 여행 분위기는 어디에 집중하시나요? (자연/바다/도시)\n"
        "4) 음식 취향은 어떤가요? (해산물/한식/카페 등)\n"
        "5) 동행 인원 구성을 알려주세요. (가족/커플/친구 등)\n\n"
        "🍽️ 맛집 추천\n"
        "조건을 알려주시면 동선 맞춰 2~3곳 추천드릴게요.\n\n"
        "최신 운영시간과 예약은 공식 안내 확인이 필요합니다."
    )

# -------------------------------
# Rule helpers
# -------------------------------
def build_draft(utter: str) -> str:
    pois = read_csv_dicts("jeju_sample_halfday_courses.csv")
    items = pois[:3]
    lines = [f"- {i.get('name') or '추천 코스'} ({i.get('area','')})" for i in items]
    return (
        "📌 여행 기본 팁\n"
        "이동 시간은 여유 있게 30~40분 단위로 잡아주세요.\n"
        "바람이 강할 수 있으니 바람막이를 챙기세요.\n\n"
        "📍 추천 여행지 & 코스 아이디어\n"
        + "\n".join(lines)
        + "\n\n🍽️ 맛집 추천\n"
        "- 인근 해산물/한식 위주로 동선 맞춰 추천\n"
        "- 카페·디저트 1곳 포함해 휴식 동선 구성\n\n"
        "최신 운영시간과 예약은 공식 안내 확인이 필요합니다."
    )

# -------------------------------
# Routes
# -------------------------------
@app.get("/")
def root():
    return {"ok": True, "model": MODEL, "deadline_ms": DEADLINE_MS}

@app.get("/health")
def health():
    return {
        "ok": True,
        "model": MODEL,
        "disable_openai": DISABLE_OPENAI,
        "deadline_ms": DEADLINE_MS,
    }

@app.post("/kakao/skill")
async def kakao_skill(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    utter = ((body.get("userRequest") or {}).get("utterance") or "").strip()

    if is_internal_probe(utter):
        return JSONResponse(kakao_text("비밀이에요 🤫 공식적으로 공개되지 않은 정보입니다."))
    if re.sub(r"\s+", "", utter) in {"안녕", "안녕하세요", "hi", "hello"}:
        return JSONResponse(kakao_text(short_greeting_reply()))

    draft = build_draft(utter)

    # OpenAI 비활성 모드면 즉시 드래프트
    if DISABLE_OPENAI or not client:
        logger.info("[Reply] DRAFT (DISABLE_OPENAI)")
        return JSONResponse(kakao_text(draft))

    async def call_openai():
        try:
            resp = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": utter},
                    {"role": "system", "content": "아래 초안을 다듬어 제주 여행 스타일로 출력:\n" + draft},
                ],
                temperature=0.3,
                max_tokens=MAX_TOKENS,
                timeout=DEADLINE_MS / 1000.0,
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:
            logger.warning(f"[OpenAI] error: {e}")
            return None

    try:
        answer = await asyncio.wait_for(call_openai(), timeout=(DEADLINE_MS / 1000.0 + 0.3))
        if answer:
            logger.info("[Reply] LLM OK")
            return JSONResponse(kakao_text(answer))
        else:
            logger.info("[Reply] DRAFT (no LLM)")
            return JSONResponse(kakao_text(draft))
    except asyncio.TimeoutError:
        logger.info("[Reply] DRAFT (timeout)")
        return JSONResponse(kakao_text(draft))
