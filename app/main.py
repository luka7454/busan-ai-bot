import os
import json
import logging
import re
import urllib.parse
from typing import List, Dict

from fastapi import FastAPI, Request
from fastapi.responses import Response, JSONResponse
from openai import OpenAI

# ──────────────────────────────────────────────────────────────────────────────
# ENV
# ──────────────────────────────────────────────────────────────────────────────
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL   = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
MAX_TOKENS     = int(os.getenv("MAX_TOKENS", "512"))
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "15"))

SEARCH_ENABLED       = os.getenv("SEARCH_ENABLED", "true").lower() == "true"
SEARCH_TIMEOUT       = float(os.getenv("SEARCH_TIMEOUT", "4.0"))
NAVER_CLIENT_ID      = os.getenv("NAVER_CLIENT_ID", "").strip()
NAVER_CLIENT_SECRET  = os.getenv("NAVER_CLIENT_SECRET", "").strip()

logger = logging.getLogger("uvicorn.error")
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
app = FastAPI()

# ──────────────────────────────────────────────────────────────────────────────
# Kakao 템플릿 유틸
# ──────────────────────────────────────────────────────────────────────────────
def kakao_text(text: str) -> Dict:
    return {"version": "2.0", "template": {"outputs": [{"simpleText": {"text": text}}]}}

def kakao_basic_card(title: str, description: str, buttons: list, image_url: str | None = None) -> Dict:
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

def kakao_text_plus_card(text: str, card_obj: Dict) -> Dict:
    outputs = [{"simpleText": {"text": text}}]
    if "template" in card_obj and "outputs" in card_obj["template"]:
        outputs.extend(card_obj["template"]["outputs"])
    return {"version": "2.0", "template": {"outputs": outputs}}

def kakao_carousel(cards: List[Dict]) -> Dict:
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

# ──────────────────────────────────────────────────────────────────────────────
# 주소 → 주소 파싱 & 지도 카드
# ──────────────────────────────────────────────────────────────────────────────
def parse_addr_to_addr(utter: str):
    if not utter:
        return None, None
    t = utter.strip()

    m = re.search(r"(.+?)\s*(?:to|->|→|⇒)\s*(.+)", t, flags=re.IGNORECASE)
    if m:
        a, b = m.group(1).strip(), m.group(2).strip()
        if len(a) > 2 and len(b) > 2:
            return a, b

    m = re.search(r"(.+?)\s*에서\s*(.+?)\s*까지", t)
    if m:
        a, b = m.group(1).strip(), m.group(2).strip()
        if len(a) > 2 and len(b) > 2:
            return a, b

    return None, None

def build_directions_card(start_addr: str, end_addr: str, lang: str = "en") -> Dict:
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

    return kakao_basic_card(
        title=title,
        description=desc,
        buttons=btns,
        image_url="https://t1.daumcdn.net/localimg/localimages/07/mapapidoc/marker_red.png"
    )

def guess_lang(text: str) -> str:
    return "ko" if any("\uac00" <= ch <= "\ud7a3" for ch in (text or "")) else "en"

# ──────────────────────────────────────────────────────────────────────────────
# 네이버 검색 (로그 강화)
# ──────────────────────────────────────────────────────────────────────────────
def _naver_search(query: str, size: int) -> List[Dict]:
    # 지연 임포트: requests 미설치여도 서버 부팅은 되게
    try:
        import requests  # type: ignore
    except Exception:
        logger.warning("[naver_search] requests not installed")
        return []

    if not (NAVER_CLIENT_ID and NAVER_CLIENT_SECRET):
        logger.warning("[naver_search] missing NAVER_CLIENT_ID/SECRET")
        return []

    url = "https://openapi.naver.com/v1/search/webkr.json"
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
        "User-Agent": "JejuCityAI/1.0 (+cloudtype)",
        "Accept": "application/json",
    }
    params = {"query": query, "display": max(1, min(size, 5)), "start": 1}

    try:
        r = requests.get(url, headers=headers, params=params, timeout=SEARCH_TIMEOUT)
        if r.status_code != 200:
            logger.error(f"[naver_search] HTTP {r.status_code} - {r.text[:500]}")
            return []
        data = r.json()
        items = data.get("items", [])[:size]
        out = [{"title": it.get("title", ""),
                "snippet": it.get("description", ""),
                "link": it.get("link", "")} for it in items]
        if not out:
            logger.info(f"[naver_search] empty items for query='{query}'")
        return out
    except Exception as e:
        logger.exception(f"[naver_search] exception: {e}")
        return []

def web_search(query: str, size: int = 3) -> List[Dict]:
    if not SEARCH_ENABLED:
        return []
    return _naver_search(query, size)

def format_web_context(results: List[Dict]) -> str:
    if not results:
        return ""
    return "\n\n".join(
        f"[{i+1}] {r.get('title','')}\n{r.get('snippet','')}\n{r.get('link','')}"
        for i, r in enumerate(results)
    )

# ──────────────────────────────────────────────────────────────────────────────
# 날씨 전용 카드 유틸
# ──────────────────────────────────────────────────────────────────────────────
def pick_weather_links(results: List[Dict]) -> Dict[str, str]:
    """검색 결과에서 기상청/네이버 날씨 링크를 추출"""
    out = {"kma": "", "naver": ""}
    for r in results:
        link = r.get("link", "")
        if not out["kma"] and ("weather.go.kr" in link or "kma.go.kr" in link):
            out["kma"] = link
        if not out["naver"] and "search.naver.com" in link:
            out["naver"] = link
    return out

def kakao_link_card(title: str, desc: str, links: Dict[str, str]) -> Dict:
    buttons = []
    if links.get("kma"):
        buttons.append({"action": "webLink", "label": "기상청 날씨", "webLinkUrl": links["kma"]})
    if links.get("naver"):
        buttons.append({"action": "webLink", "label": "네이버 날씨", "webLinkUrl": links["naver"]})
    if not buttons:
        buttons.append({"action": "webLink", "label": "네이버 검색", "webLinkUrl": "https://search.naver.com/search.naver?query=제주+날씨"})
    return kakao_basic_card(title, desc, buttons)

# ──────────────────────────────────────────────────────────────────────────────
# SYSTEM PROMPT (Jeju)
# ──────────────────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """
You are the Jeju City AI Assistant.

Knowledge scope (not exhaustive):
- Geography & districts: Aewol, Hallim, Jocheon, Gujwa, Samyang, Ido, Seogwipo (for comparison), Jeju International Airport, Hallasan National Park.
- Transportation: airport limousine buses, local buses (e.g., 181, 182), rental cars, taxi fare ranges, ferry & cruise port info.
- Tourism: Hallasan trails (Seongpanak, Gwaneumsa), beaches (Hamdeok, Iho Tewoo, Hyeopjae, Gwakji), landmarks (Seongsan Ilchulbong), parks & museums (EcoLand, OSULLOC, Stone Park), markets (Dongmun, 5-day markets).
- Food: black pork, abalone dishes, sea urchin bibimbap, tangerines & hallabong, seafood stew, sea-view cafes.
- Weather & seasons: wind patterns, typhoon season, best visiting months.
- Safety/etiquette: driving rules, parking around attractions, beach flags, environment protection.
Behavior:
- Always reply in the same language as the user's message.
- Be concise, friendly, and practical. Offer mini-itineraries, nearest stops, expected times when useful.
- If live info is needed (festival, weather, notices), use provided Web context. Cite URLs inline as plain text.
"""

# ──────────────────────────────────────────────────────────────────────────────
# 명소 카드(기본 3곳)
# ──────────────────────────────────────────────────────────────────────────────
JEJU_SPOTS = [
    {
        "title": "성산일출봉",
        "desc": "제주의 상징적 일출 명소이자 유네스코 세계자연유산.",
        "img": "https://api.cdn.visitjeju.net/photomng/imgpath/202009/10/2020091009043672295d3c-9b69-4a9b-a0ec-2d24f8e2df4c.jpg",
        "link": "https://map.kakao.com/?q=성산일출봉"
    },
    {
        "title": "협재해변",
        "desc": "에메랄드빛 바다와 하얀 모래로 유명한 서제주 대표 해변.",
        "img": "https://api.cdn.visitjeju.net/photomng/imgpath/202103/19/20210319024335214f0668-5a4f-4d1e-b31a-7a773e9482b0.jpg",
        "link": "https://map.kakao.com/?q=협재해변"
    },
    {
        "title": "한라산 국립공원",
        "desc": "대한민국 최고봉. 계절마다 다른 풍경과 다양한 탐방로.",
        "img": "https://api.cdn.visitjeju.net/photomng/imgpath/201910/14/2019101409570831a2c4ff-fc02-48fa-b4c9-25ad33d93a69.jpg",
        "link": "https://map.kakao.com/?q=한라산"
    }
]

# ──────────────────────────────────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"ok": True, "service": "Jeju City AI Chatbot", "model": OPENAI_MODEL}

@app.get("/debug/env")
def debug_env():
    return {
        "has_openai_key": bool(OPENAI_API_KEY),
        "model": OPENAI_MODEL,
        "max_tokens": MAX_TOKENS,
        "timeout": OPENAI_TIMEOUT,
        "search_enabled": SEARCH_ENABLED,
        "has_naver_keys": bool(NAVER_CLIENT_ID and NAVER_CLIENT_SECRET),
    }

@app.get("/debug/search")
def debug_search(q: str):
    try:
        results = web_search(q, size=3)
        return {
            "ok": True,
            "has_keys": bool(NAVER_CLIENT_ID and NAVER_CLIENT_SECRET),
            "count": len(results),
            "items": results
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/kakao/skill")
async def kakao_skill(request: Request):
    # 요청 파싱
    try:
        body = await request.json()
        utter = (body.get("userRequest") or {}).get("utterance") or ""
    except Exception:
        utter = ""

    # 주소 → 주소 : 지도 카드 즉시 응답
    start_addr, end_addr = parse_addr_to_addr(utter)
    if start_addr and end_addr:
        lang = guess_lang(utter)
        card = build_directions_card(start_addr, end_addr, lang)
        explain = "아래 버튼으로 지도에서 길찾기를 확인하세요." if lang == "ko" else "Tap a button below to open directions."
        payload = kakao_text_plus_card(explain, card)
        return Response(content=json.dumps(payload, ensure_ascii=False), media_type="application/json")

    # 명소/추천 키워드 : 이미지 캐러셀
    if any(k in utter for k in ["명소", "추천", "관광지", "여행지", "볼만한 곳", "어디가 좋아"]):
        cards = []
        for s in JEJU_SPOTS:
            cards.append({
                "title": s["title"],
                "description": s["desc"],
                "thumbnail": {"imageUrl": s["img"]},
                "buttons": [{"action": "webLink", "label": "지도 보기", "webLinkUrl": s["link"]}]
            })
        carousel = kakao_carousel(cards)
        text = "제주 인기 명소 TOP 3를 추천드려요 🌴"
        outputs = [{"simpleText": {"text": text}}]
        outputs.extend(carousel["template"]["outputs"])
        payload = {"version": "2.0", "template": {"outputs": outputs}}
        return Response(content=json.dumps(payload, ensure_ascii=False), media_type="application/json")

    # OpenAI 키 없을 때
    if not client:
        return Response(
            content=json.dumps(kakao_text("서버 오류: OPENAI_API_KEY가 설정되지 않았습니다."), ensure_ascii=False),
            media_type="application/json"
        )

    # 실시간 키워드 감지
    live_keywords = ["축제", "행사", "공연", "날씨", "운항", "운행", "실시간", "시간표",
                     "공지", "폐장", "휴무", "입장료", "요금", "예약", "전시", "대회", "오늘", "이번주", "오늘밤",
                     "festival", "event", "weather", "today", "tonight", "hours", "open", "close"]
    lower = utter.lower()
    need_search = any(k in utter for k in live_keywords) or any(k in lower for k in ["weather"])

    # 검색 컨텍스트
    results = web_search(utter, size=3) if (SEARCH_ENABLED and need_search) else []
    web_ctx = format_web_context(results) if results else ""

    # ✅ 날씨 전용 즉시 카드 (LLM 호출 전 우선 응답)
    if need_search and any(k in utter for k in ["날씨"]) or ("weather" in lower):
        links = pick_weather_links(results)
        lang = guess_lang(utter)
        if lang == "ko":
            title = "제주시 실시간 날씨"
            desc  = "공식 페이지에서 현재 기온·강수·바람 정보를 확인하세요."
            guide = "아래 버튼을 눌러 확인하세요."
        else:
            title = "Jeju City Weather (Live)"
            desc  = "Open the official page for real-time temperature, precipitation and wind."
            guide = "Tap a button to check live weather."
        card = kakao_link_card(title, desc, links)
        payload = kakao_text_plus_card(guide, card)
        return Response(content=json.dumps(payload, ensure_ascii=False), media_type="application/json")

    # LLM 호출
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": utter or "안녕하세요"}
    ]
    if web_ctx:
        messages.append({
            "role": "system",
            "content": (
                "If web context is provided, you MUST ground your answer in it. "
                "Do not say you cannot provide real-time info; summarize what the links indicate "
                "and include the most relevant URL inline as plain text.\n"
                "Web context (non-authoritative):\n" + web_ctx
            )
        })

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
        logger.exception(f"[kakao/skill] OpenAI error: {e}")
        return Response(content=json.dumps(kakao_text("죄송합니다. 잠시 후 다시 시도해 주세요."), ensure_ascii=False),
                        media_type="application/json")
