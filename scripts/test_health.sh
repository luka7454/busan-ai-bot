#!/usr/bin/env bash
set -e
URL=${1:-http://localhost:8000}
echo "GET $URL/health"
curl -sS "$URL/health" | jq . || curl -sS "$URL/health"
