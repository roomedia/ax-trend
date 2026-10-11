#!/usr/bin/env bash
# fingerprint.sh — 최근 open 이슈의 핑거프린트를 스캔한다.
# 사용법: ./fingerprint.sh <owner/repo> [limit]
# 출력: "#번호 | 제목 | 핑거프린트" (핑거프린트 없는 이슈는 제외)
# 발행 직전 후보 토큰과 대조하는 용도. GitHub 검색 인덱스 지연을 피하기 위해
# 검색 API가 아닌 이슈 목록 직접 스캔을 사용한다.
set -euo pipefail

REPO="${1:?usage: fingerprint.sh <owner/repo> [limit]}"
LIMIT="${2:-100}"

gh issue list --repo "$REPO" --state open --limit "$LIMIT" --json number,title,body \
  --jq '.[] | [.number, .title, ((.body // "" | capture("<!--[ \\t]*ax-events:[ \\t]*(?<fp>[^>]+)-->") | .fp // empty) // "-")] | @tsv' \
| while IFS=$'\t' read -r num title fp; do
    [ "$fp" = "-" ] && continue
    printf '#%s | %s | %s\n' "$num" "$title" "$fp"
  done
