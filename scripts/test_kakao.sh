#!/usr/bin/env bash
set -e
URL=${1:-http://localhost:8000/kakao/skill}
read -r -d '' PAYLOAD << 'JSON'
{
  "userRequest": {
    "utterance": "가족 2박3일, 리조트, 바다·해변 위주, 해산물 좋아해. 반나절 코스도 가능할까?"
  }
}
JSON
curl -sS -X POST "$URL" -H "Content-Type: application/json" -d "$PAYLOAD" | jq . || curl -sS -X POST "$URL" -H "Content-Type: application/json" -d "$PAYLOAD"
