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

def kakao_carousel(cards: List[Dict]) -> dict:
    """여러 장소를 카드 슬라이드로 보여주는 Carousel"""
    return {
        "version": "2.0",
        "template": {
            "outputs": [{
                "carousel": {
                    "type": "basicCard",
                    "items": cards
                }
            }]
        }
    }

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
    gmaps = f"https://www.google.com/maps/dir/?api=1&origin={o}&destination={d}"
    kmapw = f"https://map.kakao.com/?sName={o}&eName={d}"
    amap  = f"https://maps.apple.com/?saddr={o}&daddr={d}"
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
    return kakao_basic_card(title, desc, btns,
        "https://t1.daumcdn.net/localimg/localimages/07/mapapidoc/marker_red.png"
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

def web_search(query, size=3):
    if not SEARCH_ENABLED:
        return []
    return _naver_search(query, size)

def format_web_context(results):
    if not results:
        return ""
    return "\n\n".join([f"[{i+1}] {r['title']}\n{r['snippet']}\n{r['link']}" for i, r in enumerate(results)])

# ======================
# Jeju SYSTEM PROMPT
# ======================
SYSTEM_PROMPT = """
You are the Jeju City AI Assistant.

Knowledge scope:
- Jeju districts: Aewol, Hallim, Jocheon, Gujwa, Samyang, Ido, Seogwipo (for comparison)
- Tourism: Hallasan trails, Hamdeok, Iho Tewoo, Hyeopjae, Gwakji beaches, EcoLand, OSULLOC, Dongmun market
- Food: black pork, abalone, sea urchin bibimbap, tangerines, hallabong, seafood stew, cafes with sea view
- Weather & transportation: airport buses, rental cars, routes, typhoon season
Behavior:
- Always reply in the same language as the user's message.
- Provide friendly, concise, practical answers for Jeju visitors.
"""

# ======================
# 명소 카드 데이터 (기본 3곳)
# ======================
JEJU_SPOTS = [
    {
        "title": "성산일출봉",
        "desc": "제주를 대표하는 일출 명소로, UNESCO 세계자연유산에 등재된 곳이에요.",
        "img": "https://api.cdn.visitjeju.net/photomng/imgpath/202009/10/2020091009043672295d3c-9b69-4a9b-a0ec-2d24f8e2df4c.jpg",
        "link": "https://map.kakao.com/?q=성산일출봉"
    },
    {
        "title": "협재해변",
        "desc": "에메랄드빛 바다와 하얀 모래로 유명한 서쪽 대표 해변입니다.",
        "img": "https://api.cdn.visitjeju.net/photomng/imgpath/202103/19/20210319024335214f0668-5a4f-4d1e-b31a-7a773e9482b0.jpg",
        "link": "https://map.kakao.com/?q=협재해변"
    },
    {
        "title": "한라산 국립공원",
        "desc": "대한민국에서 가장 높은 산으로, 사계절 내내 다른 풍경을 보여줍니다.",
        "img": "https://api.cdn.visitjeju.net/photomng/imgpath/201910/14/2019101409570831a2c4ff-fc02-48fa-b4c9-25ad33d93a69.jpg",
        "link": "https://map.kakao.com/?q=한라산"
    }
]

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

    # 명소 추천 트리거 (이미지 카드 자동 표시)
    if any(k in utter for k in ["명소", "추천", "관광지", "여행지", "어디가 좋아"]):
        cards = []
        for s in JEJU_SPOTS:
            cards.append({
                "title": s["title"],
                "description": s["desc"],
                "thumbnail": {"imageUrl": s["img"]},
                "buttons": [{"action": "webLink", "label": "지도 보기", "webLinkUrl": s["link"]}]
            })
        carousel = kakao_carousel(cards)
        text = "제주의 인기 명소 TOP 3를 추천드릴게요 🌴"
        outputs = [{"simpleText": {"text": text}}]
        outputs.extend(carousel["template"]["outputs"])
        payload = {"version": "2.0", "template": {"outputs": outputs}}
        return Response(content=json.dumps(payload, ensure_ascii=False), media_type="application/json")

    # 기본 LLM 응답 (Web Search fallback)
    if not client:
        return Response(content=json.dumps(kakao_text("서버 오류: OPENAI_API_KEY가 없습니다."), ensure_ascii=False),
                        media_type="application/json")

    results = []
    if any(k in utter for k in ["날씨", "행사", "축제", "오늘", "이번주", "공지"]):
        results = web_search(utter)
    web_ctx = format_web_context(results) if results else ""

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
