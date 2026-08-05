#!/usr/bin/env bash
# The green gate: is this config actually cleared to arm?
#   assert-ready.sh <config.toml>
#
# Exit 0 (one PASS line) only when BOTH hold:
#   1. state/<EVENT_ID>/preflight-result.json exists, pass==true,
#      config_sha256 matches the CURRENT config bytes, generated_utc ≤ 24 h.
#   2. configs/.readiness/<basename>.json keywords_sha256 matches
#      scripts/keywords-extract.py --sha256 of the config, and no keyword has
#      confirmed==0 (unless configs/.acks/<basename>-dead-word.ack names it).
#      A config with zero [[keywords]] (capture-only) skips check 2.
# Exit 1 with one REFUSED line naming the missing prerequisite otherwise.
#
# Born 2026-08-04: ZETA and TOST were armed carrying confirmed:0 keywords
# because no launcher consulted the readiness artifacts. This script is pure
# read — call sites prepend `assert-ready.sh <cfg> &&`, and the launchers'
# marker-activated gate shares it.
#
# Exemption: a rehearsal config (basename starts with "rehearsal-") passes
# unconditionally — rehearsals validate mechanisms, not events.
set -uo pipefail
REPO="${EARSHOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO"

CFG="${1:?usage: assert-ready.sh <config.toml>}"
[[ -f "$CFG" ]] || { echo "REFUSED: config not found: $CFG"; exit 1; }
BASE="$(basename "$CFG" .toml)"

if [[ "$BASE" == rehearsal-* ]]; then
  echo "PASS: rehearsal config ($BASE) — green gate exempt by design"
  exit 0
fi

EVENT_ID=$(sed -n '/^\[event\]/,/^\[/{s/^[[:space:]]*id[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p;}' "$CFG" | head -1)
[[ -n "$EVENT_ID" ]] || { echo "REFUSED: cannot parse [event].id from $CFG"; exit 1; }

PF="state/${EVENT_ID}/preflight-result.json"
[[ -f "$PF" ]] || { echo "REFUSED: no preflight verdict at $PF — run scripts/preflight-event.sh --config $CFG --account <env> --event-env <env> tonight (CLAUDE.md: REQUIRED the night before)"; exit 1; }

CFG_SHA=$(shasum -a 256 "$CFG" | cut -d' ' -f1)
KW_SHA=$(python3 "$(dirname "${BASH_SOURCE[0]}")/keywords-extract.py" "$CFG" --sha256 2>/dev/null || true)
N_KW=$(python3 "$(dirname "${BASH_SOURCE[0]}")/keywords-extract.py" "$CFG" 2>/dev/null | grep -c . || true)

VERDICT=$(PF="$PF" CFG="$CFG" CFG_SHA="$CFG_SHA" KW_SHA="$KW_SHA" N_KW="${N_KW:-0}" BASE="$BASE" python3 - <<'PY'
import json, os, sys
from datetime import datetime, timezone

pf_path = os.environ["PF"]
try:
    pf = json.load(open(pf_path))
except Exception as e:
    print(f"REFUSED: preflight verdict unreadable ({e}): {pf_path}")
    sys.exit(0)

if not pf.get("pass"):
    print(f"REFUSED: preflight FAILED ({pf.get('fails','?')} fails, {pf.get('generated_utc','?')}) — fix the gates, re-run preflight; do not arm through a red verdict")
    sys.exit(0)
if pf.get("config_sha256") != os.environ["CFG_SHA"]:
    print("REFUSED: config CHANGED since its preflight (sha mismatch) — the verdict proves nothing about the current bytes; re-run preflight")
    sys.exit(0)
try:
    age_h = (datetime.now(timezone.utc)
             - datetime.strptime(pf.get("generated_utc", ""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
             ).total_seconds() / 3600
except ValueError:
    age_h = 9e9
if age_h > 24:
    print(f"REFUSED: preflight verdict STALE ({age_h:.0f} h > 24 h) — re-run preflight tonight")
    sys.exit(0)

n_kw = int(os.environ.get("N_KW") or 0)
if n_kw == 0:
    print(f"PASS: preflight green ({pf.get('generated_utc')}), capture-only config (no keywords) — cleared")
    sys.exit(0)

ready_path = f"configs/.readiness/{os.environ['BASE']}.json"
try:
    ready = json.load(open(ready_path))
except Exception as e:
    print(f"REFUSED: readiness artifact missing/unreadable ({e}): {ready_path} — run scripts/variant-study.sh {os.environ['CFG']}")
    sys.exit(0)
if str(ready.get("keywords_sha256", "")) != os.environ["KW_SHA"]:
    print(f"REFUSED: keywords changed since the study ({ready_path} sha mismatch) — re-run scripts/variant-study.sh {os.environ['CFG']}")
    sys.exit(0)
dead = [k.get("ticker", "?").split("-")[-1] for k in ready.get("keywords", []) if not k.get("confirmed")]
if dead:
    ack = f"configs/.acks/{os.environ['BASE']}-dead-word.ack"
    try:
        ack_text = open(ack).read()
    except OSError:
        ack_text = ""
    unacked = [d for d in dead if d not in ack_text]
    if unacked:
        print(f"REFUSED: {len(unacked)} keyword(s) scored ZERO Confirmed and are not acked: {' '.join(unacked)} — fix variants + restudy, or write {ack} naming each token with the reason (a deliberate decision, not a bypass)")
        sys.exit(0)
    print(f"PASS: preflight green ({pf.get('generated_utc')}), readiness green with ACKED dead words ({' '.join(dead)}) — cleared")
    sys.exit(0)
print(f"PASS: preflight green ({pf.get('generated_utc')}), readiness green ({len(ready.get('keywords', []))} keywords all confirmed) — cleared to arm")
PY
)
echo "$VERDICT"
[[ "$VERDICT" == PASS:* ]] && exit 0 || exit 1
