#!/usr/bin/env bash
# create-issue.sh — 이슈 발행에서 제목을 셸 확장으로부터 분리한다.
#
# 사용법: scripts/create-issue.sh <owner/repo> <title-file> <body-file>
#
# 왜 필요한가 — `gh issue create --title "$TITLE"` 형태로 제목을 명령줄에 직접 쓰면
# `$0`·`$2` 같은 문자열이 셸 위치 인자로 확장돼 제목이 깨진다.
#   #3967: "건당 $0.005" → "건당 /bin/bash.005"   ($0 → /bin/bash)
#   #3968: "월 $20부터"  → "월 0부터"             ($2 → 빈 값 + 0)
# 본문은 --body-file이라 멀쩡했다. 제목도 파일로 넘기면 확장되지 않는다.
set -euo pipefail

REPO="${1:?usage: create-issue.sh <owner/repo> <title-file> <body-file>}"
TITLE_FILE="${2:?usage: create-issue.sh <owner/repo> <title-file> <body-file>}"
BODY_FILE="${3:?usage: create-issue.sh <owner/repo> <title-file> <body-file>}"

[[ -s "$TITLE_FILE" ]] || { echo "[create-issue] 제목 파일이 비어 있습니다: $TITLE_FILE" >&2; exit 1; }
[[ -s "$BODY_FILE" ]] || { echo "[create-issue] 본문 파일이 비어 있습니다: $BODY_FILE" >&2; exit 1; }

# 파일 내용은 이중 인용된 명령 치환 결과로만 받는다 — 결과에 대한 재확장은 없다.
TITLE="$(cat "$TITLE_FILE")"

case "$TITLE" in
  *"/bin/bash"*|*"/bin/sh"*)
    echo "[create-issue] 제목에 셸 확장 흔적이 있습니다 (인라인 \"\$변수\" 의심): $TITLE" >&2
    exit 2
    ;;
esac

if [[ "$TITLE" == *'\u'* ]]; then
  echo "[create-issue] 제목에 리터럴 \\uXXXX 이스케이프가 남아 있습니다: $TITLE" >&2
  exit 3
fi

gh issue create --repo "$REPO" --title "$TITLE" --body-file "$BODY_FILE"
