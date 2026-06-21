#!/usr/bin/env python3
"""LLM-based quality check for roomedia/ax-trend issues.

Classifies each open issue as ok / duplicate / stale via OpenRouter and applies:
- confidence >= 0.9 + duplicate_of set  → close with reason
- confidence >= 0.9 + stale              → close + label
- confidence <  0.9                       → label only (human review)

Designed for GitHub Actions. Reads OPENROUTER_API_KEY, GH_TOKEN from env.
Idempotent:
  - Issues already labeled with low-quality:* are skipped.
  - Issues whose (title+body) hash matches a recent ledger entry are skipped
    (configurable TTL via LEDGER_SKIP_DAYS, default 14).
"""
import os
import sys
import json
import time
import hashlib
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

REPO = os.environ.get('REPO', 'roomedia/ax-trend')
GH_TOKEN = os.environ['GH_TOKEN']
OPENROUTER_API_KEY = os.environ['OPENROUTER_API_KEY']
LLM_MODEL = os.environ.get('LLM_MODEL') or 'nvidia/nemotron-3-super-120b-a12b:free'
DRY_RUN = os.environ.get('DRY_RUN', '').lower() == 'true'
MAX_BODY = 1500
SLEEP = 3.0  # OpenRouter free tier ~20 RPM
THRESHOLD = 0.9

LEDGER_PATH = os.environ.get('LEDGER_PATH', '.github/data/issue-quality-ledger.json')
LEDGER_SKIP_DAYS = int(os.environ.get('LEDGER_SKIP_DAYS', '14'))

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


# ---------------- Ledger ----------------

def load_ledger():
    """Load ledger JSON. Missing file → empty ledger. Corrupt file → fail loud."""
    if not os.path.exists(LEDGER_PATH):
        return {'schema_version': 1, 'last_run': None, 'decisions': {}}
    with open(LEDGER_PATH, 'r', encoding='utf-8') as f:
        data = json.load(f)
    if not isinstance(data, dict) or 'decisions' not in data:
        raise RuntimeError(f'Ledger at {LEDGER_PATH} is malformed (missing decisions key)')
    return data


def save_ledger(ledger):
    """Write ledger JSON atomically. Sort keys for stable diffs."""
    ledger['last_run'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    tmp = LEDGER_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(ledger, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write('\n')
    os.replace(tmp, LEDGER_PATH)


def body_hash(title, body):
    """Stable hash of classification-relevant content. Truncate body to MAX_BODY."""
    payload = (title or '') + '\n--\n' + (body or '')[:MAX_BODY]
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]


def should_skip(ledger, num, content_hash):
    """Return entry if ledger says skip, else None.

    Skip when:
      - hash matches (body unchanged)
      - decided_at within LEDGER_SKIP_DAYS
    """
    entry = ledger['decisions'].get(str(num))
    if not entry:
        return None
    if entry.get('hash') != content_hash:
        return None  # body changed → re-evaluate
    try:
        decided = datetime.fromisoformat(entry['decided_at'].replace('Z', '+00:00'))
    except Exception:
        return None
    if datetime.now(timezone.utc) - decided > timedelta(days=LEDGER_SKIP_DAYS):
        return None  # stale ledger → re-evaluate
    return entry


def record_decision(ledger, num, content_hash, result, action_taken):
    """Update ledger entry. Action_taken is None on dry-run or label-only."""
    ledger['decisions'][str(num)] = {
        'hash': content_hash,
        'classification': result['category'],
        'confidence': round(result['confidence'], 3),
        'reason': (result.get('reason') or '')[:200],
        'duplicate_of': result.get('duplicate_of'),
        'model': LLM_MODEL,
        'decided_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'action_taken': action_taken,
    }


# ---------------- GitHub API ----------------

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


# ---------------- OpenRouter ----------------

def classify_with_retry(title, body, max_attempts=3):
    """Call OpenRouter with retry + temperature=0 + JSON mode.

    Returns parsed dict {category, confidence, reason, duplicate_of}.
    Raises last exception if all attempts fail.
    """
    user = f"TITLE:\n{title}\n\nBODY (first {MAX_BODY} chars):\n{(body or '')[:MAX_BODY]}"
    payload = {
        'model': LLM_MODEL,
        'messages': [
            {'role': 'system', 'content': SYSTEM.format(today=datetime.now(timezone.utc).strftime('%Y-%m-%d'))},
            {'role': 'user', 'content': user},
        ],
        'temperature': 0.0,
        'response_format': {'type': 'json_object'},
        'max_tokens': 200,
    }
    body_bytes = json.dumps(payload).encode('utf-8')
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            req = urllib.request.Request(
                'https://openrouter.ai/api/v1/chat/completions',
                data=body_bytes,
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
        except Exception as e:
            last_err = e
            if attempt < max_attempts:
                backoff = 2 ** attempt  # 2, 4 seconds
                print(f'  [retry {attempt}/{max_attempts}] {type(e).__name__}: {str(e)[:80]} (sleep {backoff}s)')
                time.sleep(backoff)
    raise last_err


# ---------------- Issue actions ----------------

def add_label(num, label):
    if DRY_RUN:
        print(f'  [dry] label {label} -> #{num}')
        return
    try:
        gh_api(f'issues/{num}/labels', method='POST', body={'labels': [label]})
    except RuntimeError as e:
        print(f'  [label!] #{num} {label}: {e}')


def close_issue(num, comment, auto_close=True):
    """Close issue. Respects DRY_RUN and auto_close flag."""
    if DRY_RUN:
        print(f'  [dry] close #{num}: {comment[:120].strip()}')
        return False
    if not auto_close:
        print(f'  [label-only] #{num} would close (issues:opened mode, deferred to next run)')
        return False
    try:
        gh_api(f'issues/{num}/comments', method='POST', body={'body': comment})
        gh_api(f'issues/{num}', method='PATCH', body={'state': 'closed', 'state_reason': 'not_planned'})
        print(f'  [closed] #{num}')
        return True
    except RuntimeError as e:
        print(f'  [close!] #{num}: {e}')
        return False


# ---------------- Main ----------------

def main():
    if not OPENROUTER_API_KEY:
        print('ERROR: OPENROUTER_API_KEY not set', file=sys.stderr)
        sys.exit(1)
    ensure_labels()

    ledger = load_ledger()
    print(f'[ledger] loaded {len(ledger["decisions"])} prior decisions, skip_ttl={LEDGER_SKIP_DAYS}d')

    single = os.environ.get('ISSUE_NUMBER')
    event = os.environ.get('EVENT_NAME', '')
    # close-on-open: close immediately when not in dry-run.
    # - issues:opened  → real LLM eval, auto_close if conf high
    # - schedule/manual → same
    # - DRY_RUN=true   → label only, never close
    auto_close = not DRY_RUN

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

    stats = {'ok': 0, 'duplicate': 0, 'stale': 0, 'closed': 0, 'errors': 0, 'skipped': 0}
    ledger_dirty = False
    for i, issue in enumerate(targets, 1):
        num = issue['number']
        title = issue.get('title', '')
        body = issue.get('body') or ''
        chash = body_hash(title, body)

        cached = should_skip(ledger, num, chash)
        if cached:
            stats['skipped'] += 1
            print(f'[{i}/{len(targets)}] #{num}: SKIP (ledger {cached["classification"]}@{cached["decided_at"][:10]} conf={cached["confidence"]:.2f})')
            continue

        print(f'\n[{i}/{len(targets)}] #{num}: {title[:80]}')
        try:
            r = classify_with_retry(title, body)
            cat, conf = r['category'], r['confidence']
            print(f'  -> {cat} conf={conf:.2f} | {r["reason"][:100]}')
            stats[cat] = stats.get(cat, 0) + 1
            action = None
            if cat == 'ok':
                record_decision(ledger, num, chash, r, action)
                ledger_dirty = True
                continue
            if conf >= THRESHOLD:
                if cat == 'duplicate' and r['duplicate_of']:
                    closed = close_issue(num, f"🤖 LLM 자동 분류: 중복 이슈 (신뢰도 {conf:.2f}).\n\n추정 원본: #{r['duplicate_of']}\n\n사유: {r['reason']}\n\n_모델: `{LLM_MODEL}` (OpenRouter). 오분류 시 이 코멘트에 답글로 알려주세요._", auto_close=auto_close)
                    if closed:
                        stats['closed'] += 1
                        action = 'closed'
                    else:
                        action = 'dry-run' if DRY_RUN else 'label-only'
                elif cat == 'stale':
                    closed = close_issue(num, f"🤖 LLM 자동 분류: 오래된 내용을 최신 기술로 보고 (신뢰도 {conf:.2f}).\n\n사유: {r['reason']}\n\n_모델: `{LLM_MODEL}` (OpenRouter). 오분류 시 이 코멘트에 답글로 알려주세요._", auto_close=auto_close)
                    if closed:
                        stats['closed'] += 1
                        action = 'closed'
                    else:
                        action = 'dry-run' if DRY_RUN else 'label-only'
                    if not DRY_RUN and not closed:
                        # still apply label for visibility even when not auto-closing
                        add_label(num, 'low-quality:stale')
                else:
                    add_label(num, 'low-quality:needs-review')
                    action = 'labeled'
            else:
                add_label(num, f'low-quality:{cat}')
                add_label(num, 'low-quality:needs-review')
                action = 'labeled-low-conf'
            record_decision(ledger, num, chash, r, action)
            ledger_dirty = True
        except Exception as e:
            print(f'  [ERROR] {e}')
            stats['errors'] += 1
            # Do not record failed classification in ledger — leave stale or absent
            # so a future run will retry.
        if i < len(targets):
            time.sleep(SLEEP)

    if ledger_dirty:
        save_ledger(ledger)
        print(f'[ledger] saved {len(ledger["decisions"])} decisions')

    print(f'\n[done] {json.dumps(stats)}')


if __name__ == '__main__':
    main()
