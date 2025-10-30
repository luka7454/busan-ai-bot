import os
import json
import asyncio
import logging
import re
import urllib.parse
import time
from typing import List, Dict, Optional

import requests
from fastapi import FastAPI, Request
from fastapi.responses import Response, JSONResponse
from openai import OpenAI

logger = logging.getLogger("uvicorn.error")

# ---- ENV (기존 명칭 유지) ----
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL   = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
MAX_TOKENS     = int(os.getenv("MAX_TOKENS", "512"))
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "15"))

# ---- Web Search 설정(선택) ----
SEARCH_ENABLED       = os.getenv("SEARCH_ENABLED", "true").lower() == "true"
SEARCH_PROVIDER      = os.getenv("SEARCH_PROVIDER", "auto")  # auto|naver|google
SEARCH_MAX_RESULTS   = int(os.getenv("SEARCH_MAX_RESULTS", "3"))
SEARCH_TIMEOUT       = float(os.getenv("SEARCH_TIMEOUT", "4.0"))

# Naver
NAVER_CLIENT_ID      = os.getenv("NAVER_CLIENT_ID", "").strip()
NAVER_CLIENT_SECRET  = os.getenv("NAVER_CLIENT_SECRET", "").strip()

# Google Custom Search
GOOGLE_CSE_ID        = os.getenv("GOOGLE_CSE_ID", "").strip()
GOOGLE_API_KEY       = os.getenv("GOOGLE_API_KEY", "").strip()

client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
app = FastAPI()

# =========================
# Kakao 템플릿 빌더
# =========================
def kakao_text(text: str) -> dict:
    return {
        "version": "2.0",
        "template": {
            "outputs": [{"simpleText": {"text": text}}]
        }
    }

def kakao_basic_card(title: str, description: str, buttons: list, image_url: str = None) -> dict:
    card = {
        "version": "2.0",
        "template": {
            "outputs": [{
                "basicCard": {
                    "title": title,
                    "description": description,
                    "buttons": buttons
                }
            }]
        }
    }
    if image_url:
        card["template"]["outputs"][0]["basicCard"]["thumbnail"] = {"imageUrl": image_url}
    return card

def kakao_text_plus_card(text: str, card_obj: dict) -> dict:
    """simpleText + basicCard를 함께 반환 (시각 + 설명)"""
    outputs = [{"simpleText": {"text": text}}]
    if "template" in card_obj and "outputs" in card_obj["template"]:
        outputs.extend(card_obj["template"]["outputs"])
    return {"version": "2.0", "template": {"outputs": outputs}}

# =========================
# 주소→주소 파싱 & 지도 카드 (앱 설치不要, 웹 전용)
# =========================
def parse_addr_to_addr(utter: str):
    """
    'A to B', 'A -> B', 'A —> B', 'A에서 B까지' 등에서 (A, B) 추출
    매칭 실패 시 (None, None) 반환
    """
    if not utter:
        return None, None
    t = utter.strip()

    # 1) 영문 스타일
    m = re.search(r"(.+?)\s*(?:to|->|→|⇒)\s*(.+)", t, flags=re.IGNORECASE)
    if m:
        a, b = m.group(1).strip(), m.group(2).strip()
        if len(a) > 2 and len(b) > 2:
            return a, b

    # 2) 한글 스타일: "~에서 ~까지"
    m = re.search(r"(.+?)\s*(?:에서)\s*(.+?)\s*(?:까지)", t)
    if m:
        a, b = m.group(1).strip(), m.group(2).strip()
        if len(a) > 2 and len(b) > 2:
            return a, b

    return None, None

def build_directions_card(start_addr: str, end_addr: str, lang: str = "en") -> dict:
    """
    앱 설치 없이 웹으로 바로 열리는 길찾기 카드
    - Google Maps(웹), Kakao Map(웹), Apple Maps 버튼 제공
    """
    o = urllib.parse.quote_plus(start_addr.strip())
    d = urllib.parse.quote_plus(end_addr.strip())

    gmaps = f"https://www.google.com/maps/dir/?api=1&origin={o}&destination={d}&travelmode=transit"
    kmapw = f"https://map.kakao.com/?sName={o}&eName={d}"
    amap  = f"https://maps.apple.com/?saddr={o}&daddr={d}&dirflg=r"

    if lang == "ko":
        title = "길찾기"
        desc  = f"{start_addr} → {end_addr}\n원하는 지도에서 열어보세요."
        btns  = [
            {"action": "webLink", "label": "Google 지도", "webLinkUrl": gmaps},
            {"action": "webLink", "label": "카카오맵(웹)", "webLinkUrl": kmapw},
            {"action": "webLink", "label": "Apple 지도", "webLinkUrl": amap},
        ]
    else:
        title = "Directions"
        desc  = f"{start_addr} → {end_addr}\nOpen in your preferred map."
        btns  = [
            {"action": "webLink", "label": "Google Maps", "webLinkUrl": gmaps},
            {"action": "webLink", "label": "Kakao Map (Web)", "webLinkUrl": kmapw},
            {"action": "webLink", "label": "Apple Maps", "webLinkUrl": amap},
        ]

    return kakao_basic_card(
        title=title,
        description=desc,
        buttons=btns,
        image_url="https://t1.daumcdn.net/localimg/localimages/07/mapapidoc/marker_red.png"
    )

def guess_lang(text: str) -> str:
    # 아주 가벼운 휴리스틱: 한글 포함 여부
    if any("\uac00" <= ch <= "\ud7a3" for ch in (text or "")):
        return "ko"
    return "en"

# =========================
# Web Search Provider
# =========================
def _naver_search(query: str, size: int) -> List[Dict]:
    if not (NAVER_CLIENT_ID and NAVER_CLIENT_SECRET):
        return []
    url = "https://openapi.naver.com/v1/search/webkr.json"
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET
    }
    params = {"query": query, "display": size, "start": 1}
    try:
        r = requests.get(url, headers=headers, params=params, timeout=SEARCH_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        items = data.get("items", [])[:size]
        out = []
        for it in items:
            out.append({
                "title": it.get("title", ""),
                "snippet": it.get("description", ""),
                "link": it.get("link", "")
            })
        return out
    except Exception as e:
        logger.warning(f"[naver_search] {e}")
        return []

def _google_cse(query: str, size: int) -> List[Dict]:
    if not (GOOGLE_CSE_ID and GOOGLE_API_KEY):
        return []
    url = "https://www.googleapis.com/customsearch/v1"
    params = {"q": query, "cx": GOOGLE_CSE_ID, "key": GOOGLE_API_KEY, "num": size}
    try:
        r = requests.get(url, params=params, timeout=SEARCH_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        items = data.get("items", [])[:size]
        out = []
        for it in items:
            out.append({
                "title": it.get("title", ""),
                "snippet": it.get("snippet", ""),
                "link": it.get("link", "")
            })
        return out
    except Exception as e:
        logger.warning(f"[google_cse] {e}")
        return []

def web_search(query: str, size: int = 3) -> List[Dict]:
    if not SEARCH_ENABLED:
        return []
    size = max(1, min(size, SEARCH_MAX_RESULTS))
    providers = []

    if SEARCH_PROVIDER in ("auto", "naver"):
        providers.append(_naver_search)
    if SEARCH_PROVIDER in ("auto", "google"):
        providers.append(_google_cse)

    results: List[Dict] = []
    for fn in providers:
        try:
            results = fn(query, size)
            if results:
                break
        except Exception as e:
            logger.warning(f"[web_search] provider error: {e}")
            continue
    return results[:size]

def format_web_context(results: List[Dict]) -> str:
    if not results:
        return ""
    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        snippet = r.get("snippet", "")
        link = r.get("link", "")
        lines.append(f"[{i}] {title}\n{snippet}\n{link}")
    return "\n\n".join(lines)

# =========================
# Domain System Prompt (지식 주입)
# =========================
SYSTEM_PROMPT = """
You are the Busan City AI Assistant.

Knowledge scope (not exhaustive):
- Districts/areas: Haeundae, Suyeong, Nampo, Seomyeon, Dongnae, Yeongdo, Centum City, Gijang, Songdo, Taejongdae, Gamcheon Culture Village.
- Transit: Busan Metro Lines 1–4, BEXCO/Centum, KTX/SRT to Busan station, Gimhae International Airport, airport limousine bus, late-night bus basics, taxi fare ballpark.
- Tourism: beaches (Haeundae, Gwangalli, Songjeong), observatories (Hwangnyeongsan), night views (Gwangan Bridge), markets (Jagalchi, Gukje), museums, temples (Beomeosa).
- Food: dwaeji-gukbap, milmyeon, eomuk, hoe (sashimi), coffee street; basic ordering etiquette.
- Hotels: ocean-view areas, check-in/out conventions; Marysol by Haeundae as a local example property.
- Safety/etiquette: beach flags, swimming season, general tips for foreign visitors.
- If a question needs live info (dates, schedules, events, weather, service notices), you may rely on provided "web context" below.
Behavior:
- Always reply in the same language as the user's message.
- Be concise, friendly, and practical. Provide mini itineraries, nearest stations, opening hour norms when relevant.
- When using "web context," cite URLs inline in natural language (e.g., 'according to ... (URL)') rather than markdown.
"""

# =========================
# 헬스/디버그
# =========================
@app.get("/health")
async def health():
    return {"ok": True}

@app.get("/debug/env")
async def debug_env():
    return {
        "has_openai_key": bool(OPENAI_API_KEY),
        "model": OPENAI_MODEL,
        "max_tokens": MAX_TOKENS,
        "timeout": OPENAI_TIMEOUT,
        "search_enabled": SEARCH_ENABLED,
        "search_provider": SEARCH_PROVIDER,
        "has_naver_keys": bool(NAVER_CLIENT_ID and NAVER_CLIENT_SECRET),
        "has_google_cse": bool(GOOGLE_CSE_ID and GOOGLE_API_KEY),
    }

@app.get("/debug/chat")
async def debug_chat(q: str = "안녕! 부산시 AI야?"):
    if not client:
        return JSONResponse({"error": "OPENAI_API_KEY is not set on server"}, status_code=500)
    try:
        results = web_search(q, size=2) if SEARCH_ENABLED else []
        web_ctx = format_web_context(results)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": q}
        ]
        if web_ctx:
            messages.append({"role": "system", "content": f"Web context (non-authoritative):\n{web_ctx}"})
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            max_tokens=MAX_TOKENS,
            temperature=0.3,
            timeout=OPENAI_TIMEOUT,
        )
        return {"ok": True, "answer": resp.choices[0].message.content.strip(), "used_web": bool(web_ctx)}
    except Exception as e:
        logger.exception(f"[debug_chat] OpenAI error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

# =========================
# Kakao Skill
# =========================
@app.post("/kakao/skill")
async def kakao_skill(request: Request):
    # 1) 요청 파싱
    try:
        body = await request.json()
    except Exception:
        body = {}

    try:
        utter = (body.get("userRequest") or {}).get("utterance") or ""
    except Exception:
        utter = ""

    # 2) 주소→주소 패턴이면, LLM 호출 없이 즉시 '지도 카드'로 응답
    start_addr, end_addr = parse_addr_to_addr(utter)
    if start_addr and end_addr:
        lang = guess_lang(utter)
        card = build_directions_card(start_addr, end_addr, lang)
        explain = "아래 버튼으로 지도에서 길찾기를 확인하세요." if lang == "ko" \
                  else "Tap a button below to open directions."
        payload = kakao_text_plus_card(explain, card)
        return Response(content=json.dumps(payload, ensure_ascii=False),
                        media_type="application/json")

    # 3) OpenAI 키 확인
    if not client:
        text = "서버 설정 오류: OPENAI_API_KEY가 설정되지 않았습니다."
        return Response(content=json.dumps(kakao_text(text), ensure_ascii=False),
                        media_type="application/json")

    # 4) 웹 검색 (fallback) + LLM 호출
    # - 부산 관련 '실시간성'이 필요한 키워드 감지 시 우선 검색
    # - 그 외에는 LLM만으로 답하되, 모델이 확실치 않아 보이면 검색 보조
    must_search_keywords = ["축제", "행사", "공연", "날씨", "운항", "운행", "실시간", "시간표", "공지", "폐장", "휴무", "입장료", "요금", "예약", "전시", "대회", "오늘", "이번 주", "이번주", "오늘밤", "막차", "첫차"]
    lower = utter.lower()
    need_live = any(k in utter for k in must_search_keywords) or any(k in lower for k in ["festival", "event", "weather", "today", "tonight", "hours", "open", "close"])

    web_ctx = ""
    if SEARCH_ENABLED and (need_live or len(utter) > 80):
        try:
            results = web_search(utter, size=SEARCH_MAX_RESULTS)
            web_ctx = format_web_context(results)
        except Exception as e:
            logger.warning(f"[kakao websearch] {e}")

    # 메시지 구성
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": (utter or "안녕하세요")}
    ]
    if web_ctx:
        messages.append({"role": "system", "content": f"Web context (non-authoritative):\n{web_ctx}"})

    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            max_tokens=MAX_TOKENS,
            temperature=0.3,
            timeout=OPENAI_TIMEOUT,
        )
        answer = resp.choices[0].message.content.strip()
        payload = kakao_text(answer)
        return Response(content=json.dumps(payload, ensure_ascii=False),
                        media_type="application/json")
    except Exception as e:
        logger.exception(f"[kakao/skill] OpenAI error: {e}")
        fallback = "죄송합니다. 잠시 후 다시 시도해 주세요."
        return Response(content=json.dumps(kakao_text(fallback), ensure_ascii=False),
                        media_type="application/json")
