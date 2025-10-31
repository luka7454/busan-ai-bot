import os
import json
import asyncio
import logging
import re
import urllib.parse
from typing import List, Dict
import requests
from fastapi import FastAPI, Request
from fastapi.responses import Response, JSONResponse
from openai import OpenAI

logger = logging.getLogger("uvicorn.error")

# ==== ENV ====
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL   = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
MAX_TOKENS     = int(os.getenv("MAX_TOKENS", "512"))
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "15"))

# ==== Web Search ====
SEARCH_ENABLED       = os.getenv("SEARCH_ENABLED", "true").lower() == "true"
SEARCH_PROVIDER      = os.getenv("SEARCH_PROVIDER", "auto")
SEARCH_MAX_RESULTS   = int(os.getenv("SEARCH_MAX_RESULTS", "3"))
SEARCH_TIMEOUT       = float(os.getenv("SEARCH_TIMEOUT", "4.0"))

NAVER_CLIENT_ID      = os.getenv("NAVER_CLIENT_ID", "").strip()
NAVER_CLIENT_SECRET  = os.getenv("NAVER_CLIENT_SECRET", "").strip()
GOOGLE_CSE_ID        = os.getenv("GOOGLE_CSE_ID", "").strip()
GOOGLE_API_KEY       = os.getenv("GOOGLE_API_KEY", "").strip()

client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
app = FastAPI()

# ======================
# Kakao 템플릿 빌더
# ======================
def kakao_text(text: str):
    return {
        "version": "2.0",
        "template": {"outputs": [{"simpleText": {"text": text}}]}
    }

def kakao_basic_card(title, description, buttons, image_url=None):
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

def kakao_text_plus_card(text, card_obj):
    outputs = [{"simpleText": {"text": text}}]
    if "template" in card_obj and "outputs" in card_obj["template"]:
        outputs.extend(card_obj["template"]["outputs"])
    return {"version": "2.0", "template": {"outputs": outputs}}

# ======================
# 주소→주소 파싱 & 지도 카드
# ======================
def parse_addr_to_addr(utter):
    if not utter:
        return None, None
    t = utter.strip()
    m = re.search(r"(.+?)\s*(?:to|->|→|⇒)\s*(.+)", t, flags=re.IGNORECASE)
    if m:
        a, b = m.group(1).strip(), m.group(2).strip()
        if len(a) > 2 and len(b) > 2:
            return a, b
    m = re.search(r"(.+?)\s*(?:에서)\s*(.+?)\s*(?:까지)", t)
    if m:
        a, b = m.group(1).strip(), m.group(2).strip()
        if len(a) > 2 and len(b) > 2:
            return a, b
    return None, None

def build_directions_card(start_addr, end_addr, lang="en"):
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

def guess_lang(text):
    if any("\uac00" <= ch <= "\ud7a3" for ch in (text or "")):
        return "ko"
    return "en"

# ======================
# Web Search Provider
# ======================
def _naver_search(query, size):
    if not (NAVER_CLIENT_ID and NAVER_CLIENT_SECRET):
        return []
    url = "https://openapi.naver.com/v1/search/webkr.json"
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET
    }
    params = {"query": query, "display": size}
    try:
        r = requests.get(url, headers=headers, params=params, timeout=SEARCH_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        items = data.get("items", [])[:size]
        return [{"title": it.get("title", ""), "snippet": it.get("description", ""), "link": it.get("link", "")} for it in items]
    except Exception as e:
        logger.warning(f"[naver_search] {e}")
        return []

def _google_cse(query, size):
    if not (GOOGLE_CSE_ID and GOOGLE_API_KEY):
        return []
    url = "https://www.googleapis.com/customsearch/v1"
    params = {"q": query, "cx": GOOGLE_CSE_ID, "key": GOOGLE_API_KEY, "num": size}
    try:
        r = requests.get(url, params=params, timeout=SEARCH_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        items = data.get("items", [])[:size]
        return [{"title": it.get("title", ""), "snippet": it.get("snippet", ""), "link": it.get("link", "")} for it in items]
    except Exception as e:
        logger.warning(f"[google_cse] {e}")
        return []

def web_search(query, size=3):
    if not SEARCH_ENABLED:
        return []
    providers = []
    if SEARCH_PROVIDER in ("auto", "naver"):
        providers.append(_naver_search)
    if SEARCH_PROVIDER in ("auto", "google"):
        providers.append(_google_cse)

    for fn in providers:
        try:
            results = fn(query, size)
            if results:
                return results[:size]
        except Exception as e:
            logger.warning(f"[web_search] {e}")
    return []

def format_web_context(results):
    if not results:
        return ""
    return "\n\n".join([f"[{i+1}] {r['title']}\n{r['snippet']}\n{r['link']}" for i, r in enumerate(results)])

# ======================
# 제주시 SYSTEM PROMPT
# ======================
SYSTEM_PROMPT = """
You are the Jeju City AI Assistant.

Knowledge scope (not exhaustive):
- Geography & districts: Aewol, Hallim, Jocheon, Gujwa, Samyang, Ido, Seogwipo (for comparison), Jeju International Airport, Hallasan National Park.
- Transportation: Jeju Airport limousine buses, rental cars, local buses (e.g., 181, 182), taxi fare ranges, airport parking, ferry & cruise port info.
- Tourism: Hallasan trails (Seongpanak, Gwaneumsa), beaches (Hamdeok, Iho Tewoo, Hyeopjae, Gwakji), theme parks (EcoLand, Jeju Stone Park), museums (OSULLOC, Art Jeju), markets (Dongmun, 5-day markets).
- Food: black pork (heuk-dwaeji), abalone dishes, sea urchin bibimbap, Jeju tangerines, hallabong, seafood stew, famous cafes with sea view.
- Accommodation: ocean-view resorts, pensions, boutique hotels in Aewol and Hamdeok, check-in/out conventions.
- Culture & nature: tangerine farms, Oreum volcanic cones, Olle walking trails, UNESCO heritage (Geopark, Seongsan Ilchulbong, Manjanggul Cave).
- Weather: four-season wind patterns, typhoon season, best visiting months.
- Safety/etiquette: driving rules, parking on tourist routes, beach flags, environmental protection.
- If a question needs live info (festival schedules, flights, weather, service notices), you may rely on provided "web context" below.
Behavior:
- Always reply in the same language as the user's message.
- Be concise, friendly, and practical. Provide mini itineraries, nearest attractions, expected travel times, and cultural notes when relevant.
- When using "web context," cite URLs inline in natural language (e.g., 'according to ... (URL)') rather than markdown.
"""

# ======================
# FastAPI 엔드포인트
# ======================
@app.get("/health")
async def health():
    return {"ok": True, "service": "Jeju City AI Chatbot", "model": OPENAI_MODEL}

@app.post("/kakao/skill")
async def kakao_skill(request: Request):
    try:
        body = await request.json()
        utter = (body.get("userRequest") or {}).get("utterance") or ""
    except Exception:
        utter = ""

    # 주소→주소 자동 카드
    start_addr, end_addr = parse_addr_to_addr(utter)
    if start_addr and end_addr:
        lang = guess_lang(utter)
        card = build_directions_card(start_addr, end_addr, lang)
        explain = "아래 버튼으로 지도에서 길찾기를 확인하세요." if lang == "ko" else "Tap below to open maps."
        payload = kakao_text_plus_card(explain, card)
        return Response(content=json.dumps(payload, ensure_ascii=False), media_type="application/json")

    if not client:
        return Response(content=json.dumps(kakao_text("서버 오류: OPENAI_API_KEY가 없습니다."), ensure_ascii=False),
                        media_type="application/json")

    # Web Search fallback
    keywords = ["축제", "날씨", "행사", "공연", "운항", "오늘", "이번주", "예약", "공지", "입장료", "휴무"]
    need_search = any(k in utter for k in keywords)
    web_ctx = ""
    if need_search:
        try:
            results = web_search(utter, size=SEARCH_MAX_RESULTS)
            web_ctx = format_web_context(results)
        except Exception as e:
            logger.warning(f"[websearch] {e}")

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": utter}
    ]
    if web_ctx:
        messages.append({"role": "system", "content": f"Web context:\n{web_ctx}"})

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
        return Response(content=json.dumps(payload, ensure_ascii=False), media_type="application/json")
    except Exception as e:
        logger.exception(f"[kakao/skill] {e}")
        return Response(content=json.dumps(kakao_text("죄송합니다. 제주시 챗봇 오류가 발생했습니다."), ensure_ascii=False),
                        media_type="application/json")
