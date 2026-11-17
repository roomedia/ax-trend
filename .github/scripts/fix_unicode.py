#!/usr/bin/env python3
"""Fix literal \\uXXXX escape sequences left in an issue title/body.

Usage: fix_unicode.py <issue_number>

Only literal backslash-u sequences are decoded. Existing non-ASCII
text (e.g. Korean) is never touched, unlike a unicode-escape decode
pass which would corrupt it.
"""
import json
import re
import subprocess
import sys
import tempfile

PAIR_RE = re.compile(r"\\u([dD][89abAB][0-9a-fA-F]{2})\\u([dD][c-fC-F][0-9a-fA-F]{2})")
ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")


def _pair_sub(m):
    hi = int(m.group(1), 16) - 0xD800
    lo = int(m.group(2), 16) - 0xDC00
    return chr(0x10000 + (hi << 10) + lo)


def decode_unicode(text):
    if not text:
        return text
    text = PAIR_RE.sub(_pair_sub, text)
    return ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), text)


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def main():
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        print(f"usage: fix_unicode.py <issue_number>, got {sys.argv[1:]!r}")
        return 1
    number = sys.argv[1]

    res = run(["gh", "issue", "view", number, "--json", "title,body"])
    if res.returncode != 0:
        print(f"gh issue view {number} failed: {res.stderr.strip()}")
        return 1

    data = json.loads(res.stdout)
    title = data.get("title") or ""
    body = data.get("body") or ""

    new_title = decode_unicode(title)
    new_body = decode_unicode(body)
    if new_title == title and new_body == body:
        print(f"issue #{number}: no literal \\uXXXX sequences, nothing to do")
        return 0

    with tempfile.NamedTemporaryFile(
        "w", suffix=".md", encoding="utf-8", delete=False
    ) as f:
        f.write(new_body)
        body_file = f.name

    res = run(
        ["gh", "issue", "edit", number, "--title", new_title,
         "--body-file", body_file]
    )
    if res.returncode != 0:
        print(f"gh issue edit {number} failed: {res.stderr.strip()}")
        return 1

    print(f"issue #{number}: decoded literal \\uXXXX sequences")
    return 0


if __name__ == "__main__":
    sys.exit(main())
