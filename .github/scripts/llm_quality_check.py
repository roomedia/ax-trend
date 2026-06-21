#!/usr/bin/env python3
"""LLM-based quality check for roomedia/ax-trend issues.

Classifies each open issue as ok / duplicate / stale via OpenRouter and applies:
- confidence >= 0.9 + duplicate_of set  → close with reason
- confidence >= 0.9 + stale              → close + label
- confidence <  0.9                       → label only (human review)

Designed for GitHub Actions. Reads OPENROUTER_API_KEY, GH_TOKEN from env.
Idempotent: issues already labeled with low-quality:* are skipped on re-run.
"""
import os
import sys
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

REPO = os.environ.get('REPO', 'roomedia/ax-trend')
GH_TOKEN = os.environ['GH_TOKEN']
OPENROUTER_API_KEY = os.environ['OPENROUTER_API_KEY']
LLM_MODEL = os.environ.get('LLM_MODEL') or 'nvidia/nemotron-3-super-120b-a12b:free'
DRY_RUN = os.environ.get('DRY_RUN', '').lower() == 'true'
MAX_BODY = 1500
SLEEP = 3.0  # OpenRouter free tier ~20 RPM
THRESHOLD = 0.9

LABELS = [
    ('low-quality:duplicate', 'd93f0b', 'LLM-detected duplicate of another open issue'),
    ('low-quality:stale', 'fbca04', 'LLM-detected: body frames outdated info as current'),
    ('low-quality:needs-review', 'cccccc', 'LLM low-confidence classification, needs human review'),
]

SYSTEM = """You classify GitHub issues for roomedia/ax-trend (daily AI/agent technology trend reports, since 2024).

Today: {today}

Output STRICT JSON only (no markdown fences, no commentary):
{{"category": "ok"|"duplicate"|"stale", "confidence": 0.0-1.0, "reason": "<one sentence English>", "duplicate_of": <int|null>}}

Calibration:
- 0.95+: clear-cut, no ambiguity
- 0.80-0.94: likely but interpretive
- <0.80: uncertain — lean "ok"

Rules:
- "duplicate": same specific news/event/announcement as another open issue. Set duplicate_of to the inferred canonical issue number, or null if unsure.
- "stale": body explicitly frames OLD-generation info as current/latest (e.g., presents GPT-3/3.5 as state-of-the-art LLM, references deprecated APIs, uses 2022-era benchmarks as current). Do NOT flag historical/retrospective coverage.
- "ok": genuine, current, non-duplicate.
"""


def gh_api(path, method='GET', body=None):
    """Thin GitHub REST wrapper. Returns parsed JSON or raises."""
    url = f'https://api.github.com/repos/{REPO}/{path}'
    data = json.dumps(body).encode('utf-8') if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('Authorization', f'token {GH_TOKEN}')
    req.add_header('Accept', 'application/vnd.github+json')
    req.add_header('X-GitHub-Api-Version', '2022-11-28')
    if data:
        req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        msg = e.read().decode('utf-8', errors='replace')[:300]
        raise RuntimeError(f'GH {method} {path} {e.code}: {msg}')


def ensure_labels():
    for name, color, desc in LABELS:
        try:
            gh_api(f'labels/{name}')
        except RuntimeError as e:
            if '404' in str(e):
                try:
                    gh_api('labels', method='POST', body={'name': name, 'color': color, 'description': desc})
                    print(f'[label+] {name}')
                except RuntimeError as e2:
                    print(f'[label!] {name}: {e2}')


def list_open_issues():
    out = []
    page = 1
    while True:
        batch = gh_api(f'issues?state=open&per_page=100&page={page}')
        if not batch:
            break
        out.extend(i for i in batch if 'pull_request' not in i)
        page += 1
    return out


def classify(title, body):
    user = f"TITLE:\n{title}\n\nBODY (first {MAX_BODY} chars):\n{(body or '')[:MAX_BODY]}"
    payload = {
        'model': LLM_MODEL,
        'messages': [
            {'role': 'system', 'content': SYSTEM.format(today=datetime.now(timezone.utc).strftime('%Y-%m-%d'))},
            {'role': 'user', 'content': user},
        ],
        'temperature': 0.1,
        'max_tokens': 200,
    }
    req = urllib.request.Request(
        'https://openrouter.ai/api/v1/chat/completions',
        data=json.dumps(payload).encode('utf-8'),
        method='POST',
    )
    req.add_header('Authorization', f'Bearer {OPENROUTER_API_KEY}')
    req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    content = data['choices'][0]['message']['content'].strip()
    if content.startswith('```'):
        content = content.split('\n', 1)[1].rsplit('\n', 1)[0]
    parsed = json.loads(content)
    cat = parsed.get('category', 'ok')
    if cat not in ('ok', 'duplicate', 'stale'):
        cat = 'ok'
    conf = float(parsed.get('confidence', 0.5))
    conf = max(0.0, min(1.0, conf))
    return {
        'category': cat,
        'confidence': conf,
        'reason': (parsed.get('reason') or '')[:300],
        'duplicate_of': parsed['duplicate_of'] if isinstance(parsed.get('duplicate_of'), int) else None,
    }


def add_label(num, label):
    if DRY_RUN:
        print(f'  [dry] label {label} -> #{num}')
        return
    try:
        gh_api(f'issues/{num}/labels', method='POST', body={'labels': [label]})
    except RuntimeError as e:
        print(f'  [label!] #{num} {label}: {e}')


def close_issue(num, comment, auto_close=True):
    """Close issue. Respects DRY_RUN and auto_close flag.

    - DRY_RUN=true: print only
    - auto_close=False (issues:opened mode): print "label-only" + skip close
    """
    if DRY_RUN:
        print(f'  [dry] close #{num}: {comment[:120].strip()}')
        return
    if not auto_close:
        print(f'  [label-only] #{num} would close (issues:opened mode, deferred to next run)')
        return
    try:
        gh_api(f'issues/{num}/comments', method='POST', body={'body': comment})
        gh_api(f'issues/{num}', method='PATCH', body={'state': 'closed', 'state_reason': 'not_planned'})
        print(f'  [closed] #{num}')
    except RuntimeError as e:
        print(f'  [close!] #{num}: {e}')


def main():
    if not OPENROUTER_API_KEY:
        print('ERROR: OPENROUTER_API_KEY not set', file=sys.stderr)
        sys.exit(1)
    ensure_labels()

    single = os.environ.get('ISSUE_NUMBER')
    event = os.environ.get('EVENT_NAME', '')
    # Freshly opened issues: never auto-close immediately. Label + comment,
    # let the next scheduled run actually close if still high-confidence.
    auto_close = not (event == 'issues')

    if single:
        try:
            issue = gh_api(f'issues/{int(single)}')
        except RuntimeError as e:
            print(f'ERROR fetching issue #{single}: {e}', file=sys.stderr)
            sys.exit(1)
        targets = [issue]
        print(f'[scan] single issue #{single}, event={event}, auto_close={auto_close}, dry_run={DRY_RUN}')
    else:
        issues = list_open_issues()
        targets = [i for i in issues if not any(l['name'].startswith('low-quality:') for l in i.get('labels', []))]
        print(f'[scan] {len(issues)} open, {len(targets)} unlabeled, model={LLM_MODEL}, event={event}, auto_close={auto_close}, dry_run={DRY_RUN}')

    stats = {'ok': 0, 'duplicate': 0, 'stale': 0, 'closed': 0, 'errors': 0}
    for i, issue in enumerate(targets, 1):
        num = issue['number']
        title = issue.get('title', '')
        body = issue.get('body') or ''
        print(f'\n[{i}/{len(targets)}] #{num}: {title[:80]}')
        try:
            r = classify(title, body)
            cat, conf = r['category'], r['confidence']
            print(f'  -> {cat} conf={conf:.2f} | {r["reason"][:100]}')
            stats[cat] = stats.get(cat, 0) + 1
            if cat == 'ok':
                continue
            if conf >= THRESHOLD:
                if cat == 'duplicate' and r['duplicate_of']:
                    close_issue(num, f"🤖 LLM 자동 분류: 중복 이슈 (신뢰도 {conf:.2f}).\n\n추정 원본: #{r['duplicate_of']}\n\n사유: {r['reason']}\n\n_모델: `{LLM_MODEL}` (OpenRouter). 오분류 시 이 코멘트에 답글로 알려주세요._", auto_close=auto_close)
                    stats['closed'] += 1
                elif cat == 'stale':
                    close_issue(num, f"🤖 LLM 자동 분류: 오래된 내용을 최신 기술로 보고 (신뢰도 {conf:.2f}).\n\n사유: {r['reason']}\n\n_모델: `{LLM_MODEL}` (OpenRouter). 오분류 시 이 코멘트에 답글로 알려주세요._", auto_close=auto_close)
                    add_label(num, 'low-quality:stale')
                    stats['closed'] += 1
                else:
                    add_label(num, 'low-quality:needs-review')
            else:
                add_label(num, f'low-quality:{cat}')
                add_label(num, 'low-quality:needs-review')
        except Exception as e:
            print(f'  [ERROR] {e}')
            stats['errors'] += 1
        if i < len(targets):
            time.sleep(SLEEP)

    print(f'\n[done] {json.dumps(stats)}')


if __name__ == '__main__':
    main()
