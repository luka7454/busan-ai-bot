import os
import json
import asyncio
import logging
import re
import urllib.parse
from fastapi import FastAPI, Request
from fastapi.responses import Response, JSONResponse
from openai import OpenAI

logger = logging.getLogger("uvicorn.error")

# ---- ENV (기존 명칭 유지) ----
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL   = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
MAX_TOKENS     = int(os.getenv("MAX_TOKENS", "512"))
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "15"))

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
    # card_obj는 전체 템플릿이므로 내부에서 outputs만 꺼내 합친다
    if "template" in card_obj and "outputs" in card_obj["template"]:
        outputs.extend(card_obj["template"]["outputs"])
    return {"version": "2.0", "template": {"outputs": outputs}}

# =========================
# 주소→주소 파싱 & 지도 카드
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
# 기존 라우트 유지 + 확장
# =========================
@app.get("/health")
async def health():
    return {"ok": True}

# 🔎 환경변수/상태 확인용 (개발 디버그용)
@app.get("/debug/env")
async def debug_env():
    return {
        "has_openai_key": bool(OPENAI_API_KEY),
        "model": OPENAI_MODEL,
        "max_tokens": MAX_TOKENS,
        "timeout": OPENAI_TIMEOUT
    }

# 🔎 OpenAI 호출 자체를 점검하는 간단 테스트 (개발 디버그용)
@app.get("/debug/chat")
async def debug_chat(q: str = "안녕! 부산시 AI야?"):
    if not client:
        return JSONResponse({"error": "OPENAI_API_KEY is not set on server"}, status_code=500)
    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system",
                 "content": "You are Busan City public service assistant. "
                            "Answer concisely in the same language as the question. "
                            "No markdown, plain text."},
                {"role": "user", "content": q}
            ],
            max_tokens=MAX_TOKENS,
            temperature=0.3,
            timeout=OPENAI_TIMEOUT,
        )
        return {"ok": True, "answer": resp.choices[0].message.content.strip()}
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
        # 텍스트 설명 + 카드 동시 제공
        explain = "아래 버튼으로 지도에서 길찾기를 확인하세요." if lang == "ko" \
                  else "Tap a button below to open directions."
        payload = kakao_text_plus_card(explain, card)
        return Response(content=json.dumps(payload, ensure_ascii=False),
                        media_type="application/json")

    # 3) OpenAI 키 확인
    if not client:
        # 개발 중 문제 파악 위해 임시로 원인 노출 (운영 전엔 일반 문구로 교체 가능)
        text = "서버 설정 오류: OPENAI_API_KEY가 설정되지 않았습니다."
        return Response(content=json.dumps(kakao_text(text), ensure_ascii=False),
                        media_type="application/json")

    # 4) OpenAI 호출 (기존 로직 유지)
    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system",
                 "content": "You are Busan City public service assistant. "
                            "Answer concisely in the same language as the user's message. "
                            "No markdown, plain text."},
                {"role": "user", "content": (utter or "안녕하세요")}
            ],
            max_tokens=MAX_TOKENS,
            temperature=0.3,
            timeout=OPENAI_TIMEOUT,
        )
        answer = resp.choices[0].message.content.strip()
        payload = kakao_text(answer)
        return Response(content=json.dumps(payload, ensure_ascii=False),
                        media_type="application/json")
    except Exception as e:
        # 5) 에러는 로그로 남기고, 사용자에겐 기본 문구
        logger.exception(f"[kakao/skill] OpenAI error: {e}")
        fallback = "죄송합니다. 잠시 후 다시 시도해 주세요."
        return Response(content=json.dumps(kakao_text(fallback), ensure_ascii=False),
                        media_type="application/json")
