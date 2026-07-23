#!/usr/bin/env bash
# Fetch a Say Technologies Q&A event's ranked question list (public JSON API,
# discovered 2026-07-22 via browser network capture — docs/SAY-QA-PRIOR.md).
#   usage: scripts/fetch-say-qa.sh <event-slug>          # e.g. tesla-2026-q2
# Writes state/say/<slug>-<date>.json (all questions, vote/share counts,
# categories, and — post-call — the company_leadership_response field, which
# marks the questions management actually answered: the acceptance dataset).
set -euo pipefail
SLUG="${1:?usage: fetch-say-qa.sh <event-slug>}"
mkdir -p state/say
python3 - "$SLUG" <<'EOF'
import json, sys, time, urllib.request
from datetime import date

slug = sys.argv[1]
base = f"https://issuers.saytechnologies.com/v3/qa-events/{slug}"
def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

details = get(f"{base}/details/")
HOST = "https://issuers.saytechnologies.com"
questions, url = [], f"{base}/questions/?page_size=100&filter=all&ordering=-count_shares_upvoted"
while url:
    d = get(url)
    questions.extend(d.get("results", []))
    url = d.get("next")
    if url and url.startswith("/"):
        url = HOST + url
    time.sleep(0.2)

out = {"slug": slug, "fetched": str(date.today()), "details": details, "questions": questions}
path = f"state/say/{slug}-{date.today()}.json"
json.dump(out, open(path, "w"))
answered = sum(1 for q in questions if q.get("company_leadership_response") or q.get("event_host_response"))
print(f"{slug}: {len(questions)} questions -> {path} (answered-marked: {answered})")
for q in questions[:10]:
    t = (q.get("question_text") or "").replace("\n", " ")[:78]
    print(f"  votes={q.get('count_users_upvoted'):>5} shares={q.get('count_shares_upvoted'):>12} cat={q.get('specific_category') or q.get('general_category')} :: {t}")
EOF
