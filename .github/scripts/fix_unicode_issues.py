#!/usr/bin/env python3
r"""unicode-fixer — 이슈 제목·본문에 남은 리터럴 \uXXXX 이스케이프를 복원한다.

호출 경로: .github/workflows/unicode-fixer-ci.yml
  issues(opened, edited) · issue_comment(created) 이벤트에서 해당 이슈 1건을 처리한다.

환경변수
  GH_TOKEN      gh CLI 인증 (워크플로가 주입)
  ISSUE_NUMBER  이벤트가 가리키는 이슈 번호 (github.event.issue.number)
  REPO          owner/name (기본: GITHUB_REPOSITORY)
  DRY_RUN       '1'/'true'면 수정 없이 변경 예정 내용만 출력

왜 스크립트로 분리했나 — 이전 인라인 버전은 세 가지 이유로 매 실행 실패했다 (#3967, #3968 발행 시 CI failure).
  1. 셸 큰따옴표 안 인라인 파이썬의 `'\u'` 리터럴 → SyntaxError: truncated \uXXXX escape
  2. text.encode('utf-8').decode('unicode-escape') → 한글이 모지바케로 깨짐
  3. shell=True + f-string 조립 → 제목의 `$0`·`$2`가 셸 변수로 확장돼 제목이 훼손됨

이 스크립트는 셸을 거치지 않는다(subprocess argv + JSON 입력). 제목·본문의 `$`·백틱·따옴표는
그대로 보존된다.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile

ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})|\\U([0-9a-fA-F]{8})")


def _fold_surrogates(text: str) -> str:
    """\\ud83d\\ude00 처럼 따로 표기된 서로게이트 쌍을 한 문자로 합친다."""
    if not any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
        return text
    try:
        return text.encode("utf-16", "surrogatepass").decode("utf-16")
    except UnicodeError:
        return text


def decode_unicode_escapes(text: str | None) -> str | None:
    """리터럴 \\uXXXX / \\UXXXXXXXX 시퀀스만 문자로 바꾼다.

    나머지 텍스트(한글·이모지·`$`·역슬래시가 아닌 문자)는 건드리지 않는다.
    """
    if not text:
        return text
    if "\\u" not in text and "\\U" not in text:
        return text

    def repl(match: re.Match[str]) -> str:
        code = match.group(1) or match.group(2)
        return chr(int(code, 16))

    return _fold_surrogates(ESCAPE_RE.sub(repl, text))


def gh(*args: str) -> subprocess.CompletedProcess[str]:
    """gh를 셸 없이 실행한다. 인자는 그대로 argv로 전달된다."""
    return subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        check=False,
        env=os.environ,
    )


def fetch_issue(repo: str, number: str) -> dict:
    proc = gh("api", f"repos/{repo}/issues/{number}")
    if proc.returncode != 0:
        raise RuntimeError(f"gh api repos/{repo}/issues/{number} 실패: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def patch_issue(repo: str, number: str, title: str, body: str) -> None:
    """제목·본문을 JSON 파일로 넘겨 셸 확장·인자 길이 문제를 모두 피한다."""
    payload = json.dumps({"title": title, "body": body}, ensure_ascii=False)
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        proc = gh("api", "-X", "PATCH", f"repos/{repo}/issues/{number}", "--input", path)
        if proc.returncode != 0:
            raise RuntimeError(f"gh api PATCH 실패: {proc.stderr.strip()}")
    finally:
        os.unlink(path)


def _preview(text: str, limit: int = 160) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def run_self_test() -> int:
    cases = [
        # (입력, 기대)
        (r"\ud55c\uae00 \uc774\uc288", "한글 이슈"),
        (r"\ub9e4\uc6d4 $20\ubd80\ud130", "매월 $20부터"),
        ("건당 $0.005", "건당 $0.005"),  # 이스케이프 없음 → 그대로
        ("plain 제목", "plain 제목"),
        (r"\ud83d\ude00", "😀"),  # 서로게이트 쌍
        (r"\U0001F600", "😀"),
        ("", ""),
        (None, None),
    ]
    failures = []
    for src, want in cases:
        got = decode_unicode_escapes(src)
        if got != want:
            failures.append(f"{src!r} → {got!r} (기대 {want!r})")
    if failures:
        print("[self-test] FAILED", file=sys.stderr)
        for line in failures:
            print("  " + line, file=sys.stderr)
        return 1
    print(f"[self-test] ok — {len(cases)} cases")
    return 0


def main() -> int:
    if "--self-test" in sys.argv[1:]:
        return run_self_test()

    number = (os.environ.get("ISSUE_NUMBER") or "").strip()
    repo = (os.environ.get("REPO") or os.environ.get("GITHUB_REPOSITORY") or "").strip()
    dry_run = (os.environ.get("DRY_RUN") or "").strip().lower() in {"1", "true", "yes"}

    if not number.isdigit():
        print(f"[unicode-fixer] ISSUE_NUMBER가 없습니다: {number!r}", file=sys.stderr)
        return 1
    if "/" not in repo:
        print(f"[unicode-fixer] REPO가 owner/name 형식이 아닙니다: {repo!r}", file=sys.stderr)
        return 1

    issue = fetch_issue(repo, number)

    if "pull_request" in issue:
        # issue_comment 이벤트는 PR에도 걸린다. PR 제목·본문은 건드리지 않는다.
        print(f"[unicode-fixer] #{number}는 PR이므로 건너뜁니다")
        return 0

    title = issue.get("title") or ""
    body = issue.get("body") or ""
    new_title = decode_unicode_escapes(title) or ""
    new_body = decode_unicode_escapes(body) or ""

    if new_title == title and new_body == body:
        print(f"[unicode-fixer] #{number} 변경 없음 (리터럴 \\u 이스케이프 없음)")
        return 0

    for value in (new_title, new_body):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            print(
                f"[unicode-fixer] #{number} 복원 결과에 짝이 맞지 않는 서로게이트가 있어 건너뜁니다",
                file=sys.stderr,
            )
            return 1

    print(f"[unicode-fixer] #{number} 복원 예정")
    if new_title != title:
        print(f"  제목  -: {_preview(title)}")
        print(f"  제목  +: {_preview(new_title)}")
    if new_body != body:
        print(f"  본문  -: {_preview(body)}")
        print(f"  본문  +: {_preview(new_body)}")

    if dry_run:
        print("[unicode-fixer] DRY_RUN=1 — 수정하지 않았습니다")
        return 0

    patch_issue(repo, number, new_title, new_body)

    # 반영 확인 — 서버 상태를 다시 읽는다.
    after = fetch_issue(repo, number)
    if (after.get("title") or "") == new_title and (after.get("body") or "") == new_body:
        print(f"[unicode-fixer] #{number} 수정 완료")
        return 0

    print(f"[unicode-fixer] #{number} 수정 후 재확인에서 내용이 다릅니다", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
