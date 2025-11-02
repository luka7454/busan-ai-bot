# Jeju Rule Engine — 결합 사양

우선순위: Blacklist(high) ▶ Weather Alert ▶ Congestion Reorder ▶ Hotel Templates ▶ Generic POI

입력: {pos(lat,lon), time, mobility, party, weather, month,dow,tod}
출력: [steps...] + safety_notes + sources

검증체크:
- blacklist.active(now,weather) 교차(지오펜스) → 제외
- drive_time_total ≤ 80min, stop_count=3
- accessibility(party) 충족
- 각 경고/통제 문구에 (source_org, source_url, last_verified) 포함
