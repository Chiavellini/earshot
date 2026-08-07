#!/usr/bin/env bash
# Nightly keyword-readiness studies for a slate date — the scheduled
# generalization of the ad-hoc study-afternoon-26aug04.sh (which was re-written
# by hand per event day; this one takes the date).
#
#   scripts/study-nightly.sh --date 2026-08-05
#
# For every staged lane config of that date (webcast-<lc>-<dp>-arm-wc.toml AND
# twilio-<lc>-<dp>-arm.toml — readiness artifacts are keyed to the config
# BASENAME, so each lane needs its own), in CALL ORDER (earliest [event]
# .starts_at first, so a slow study can never leave the nearest event ungated):
#   * if the twilio twin's keywords_sha256 equals a fresh sibling artifact,
#     CLONE the artifact to this basename (recorded via "cloned_from" — honest:
#     the gate re-hashes the config itself);
#   * else run scripts/variant-study.sh (Darwin `say` — laptop only).
# Pages on: any study failure, any confirmed==0 keyword, missing binary/ORT.
# Journals per event. Exit 1 if anything needs attention.
set -uo pipefail
REPO="${EARSHOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO"

DATE="" DRY_RUN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --date) DATE="${2:?}"; shift 2 ;;
    # --dry-run: print the decision tree, send no pages, write no configs, run
    # no re-study and no preflight. For exercising the dead-word resolution
    # below without burning 30 min of `say` on every config.
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$DATE" ]] || { echo "usage: study-nightly.sh --date YYYY-MM-DD [--dry-run]" >&2; exit 2; }
DP=$(python3 -c "
from datetime import date
d = date.fromisoformat('$DATE')
print(f\"{d.strftime('%y')}{d.strftime('%b').lower()}{d.strftime('%d')}\")")
if [[ "$DRY_RUN" == 1 ]]; then
  page() { echo "── [dry-run] WOULD PAGE: $1"; echo "$2" | sed 's/^/      /'; }
else
  page() { scripts/notify.sh "$1" "$2" --quiet || true; }
fi

[[ "$(uname -s)" == Darwin ]] || { echo "variant-study needs macOS 'say' — laptop only" >&2; exit 2; }
[[ -x target/release/earshot ]] || { page "study-nightly BLOCKED" "no target/release/earshot — build first"; exit 1; }
export EARSHOT_BIN=target/release/earshot

# macOS ships no GNU timeout, and a detector replay has been observed to hang
# on an individual item (variant-study.sh:70-84 carries the same helper for the
# same reason). Everything below that runs the detector or touches the network
# goes through this, so one stuck keyword cannot swallow the whole night.
run_bounded() {  # run_bounded <seconds> <command> [args...]
  local seconds="$1"; shift
  if command -v timeout >/dev/null; then timeout "$seconds" "$@"
  elif command -v gtimeout >/dev/null; then gtimeout "$seconds" "$@"
  else perl -e 'alarm shift @ARGV; exec @ARGV or die "exec: $!\n"' "$seconds" "$@"
  fi
}

# ── Dead-word resolution helpers (2026-08-07, TTWO) ────────────────────────
# THE INCIDENT. TTWO's board traded 69,658 contracts and we placed nothing.
# Three of twelve keywords scored confirmed:0 (gta, playstation, tailwind);
# this script paged and CONTINUED, so scripts/preflight-event.sh never ran,
# scripts/assert-ready.sh refused at T-75 with "no preflight verdict", the
# launchd `&&` short-circuited, and scripts/arm-on-url.sh was never exec'd.
# Twelve markets forfeited to protect three words that could not have fired.
#
# docs/runbooks/RUNBOOK-2026-08-05.md:86 already states the rule — "fix, ack
# deliberately, or descope. NO THIRD STATE." Paging and continuing is exactly a
# third state, entered silently because nobody was awake. Everything below
# exists to make this script always land in one of the three.
#
# WHY AUTO-ACKING RISKS NO MONEY. scripts/posture-from-study.py pins any
# keyword whose ratio = confirmed/clips falls below the floor to a 1c
# non-crossing cap. A dead word has ratio 0, so it PROVABLY CANNOT FILL.
# Acking it buys nothing for that market and costs nothing — it only stops one
# unhearable word vetoing eleven hearable ones.

dead_suffixes() {  # dead_suffixes <readiness.json> -> space-separated SUFFIXes
  python3 -c "
import json, sys
a = json.load(open(sys.argv[1]))
print(' '.join(k.get('ticker','?').split('-')[-1]
                for k in a.get('keywords', []) if not k.get('confirmed')))
" "$1" 2>/dev/null || true
}

# Which dead words is WIDENING the right fix for? The autopsy's whole point is
# that the two buckets need OPPOSITE fixes: NEAR-MISS means the model HEARD the
# word and the operating point rejected it (a confirmed_threshold question — no
# variant helps), while NO-EVIDENCE/WEAK means some BPE piece is never emitted
# (a spelling question — no threshold helps). Widening a NEAR-MISS word burns
# minutes of `say` and fixes nothing.
autopsy_widenable() {  # autopsy_widenable <study-dir> <all-dead…> -> SUFFIXes
  local sd="$1"; shift
  local all="$*" out rc
  if [[ ! -f "$sd/report.tsv" ]]; then
    echo "$all"; return 0   # no study dir (fresh/cloned artifact): widen all
  fi
  out=$(mktemp)
  run_bounded 600 scripts/dead-word-autopsy.sh --auto --study-dir "$sd" > "$out" 2>&1; rc=$?
  if (( rc != 0 )); then
    # Controls did not fire ⇒ the instrument is not trustworthy and EVERY
    # bucket verdict below it is void (dead-word-autopsy.sh:129). Widen
    # everything rather than trust a void diagnosis.
    echo "⚠ autopsy controls did not fire — bucket verdicts VOID, widening all dead words" >&2
    rm -f "$out"; echo "$all"; return 0
  fi
  local nearmiss
  nearmiss=$(sed -n 's/^  NEAR-MISS *([0-9]*): *//p' "$out" \
             | tr ' ' '\n' | sed 's/^://' | grep -v '^none$' | grep . || true)
  echo "── autopsy: NEAR-MISS (threshold fix, NOT widening): ${nearmiss:-none}" >&2
  rm -f "$out"
  local keep=""
  for d in $all; do
    grep -qx "$d" <<<"$nearmiss" || keep="$keep $d"
  done
  echo "${keep# }"
}

# Which residual dead tokens must NOT be auto-acked because they are a DESCOPE
# question, not a detectability one? The AAPL "iPhone 18" class: a
# perfectly-heard WRONG word scores 16/16 and still cannot settle the contract.
# Acking that is acking the wrong thing — the fix is to drop the keyword, which
# is a trading decision (scripts/descope-event.sh), not a nightly one.
phrase_screen_flagged() {  # phrase_screen_flagged <cfg> -> SUFFIXes (may be empty)
  local cfg="$1" out rc
  out=$(mktemp)
  run_bounded 120 python3 scripts/market-phrase-screen.py "$cfg" > "$out" 2>&1; rc=$?
  if (( rc == 2 || rc == 124 )); then
    # Board unreachable. Do NOT let a network blip block the ack — that would
    # re-create the third state this whole change removes. Proceed unscreened
    # and say so loudly in the page.
    echo "⚠ market-phrase-screen could not run (board unreachable) — ack written UNSCREENED" >&2
    rm -f "$out"; echo ""; return 0
  fi
  # "  PHRASE-MISMATCH: KXEARNINGSMENTIONTTWO-26AUG07-GTA" -> "GTA"
  sed -n 's/.*PHRASE-MISMATCH: *//p' "$out" | awk '{print $1}' \
    | sed 's/.*-//' | sort -u | tr '\n' ' '
  rm -f "$out"
}

# The account env for a config, so preflight can run for real. slate-stage.sh
# writes configs/<stem>-autorun.env with CONFIG_PATH= pointing back here; that
# mapping is the only authority (the stem is NOT the config basename).
account_env_for() {  # account_env_for <cfg> -> path, or empty
  local cfg="$1" f rec
  for f in configs/*-autorun.env; do
    [[ -f "$f" ]] || continue
    rec=$( set +e +u; . "$f" >/dev/null 2>&1
           printf '%s\x1f%s' "${CONFIG_PATH:-}" "${ACCOUNT_ENV:-}" ) || true
    local cp="" ae=""
    IFS=$'\x1f' read -r cp ae <<<"$rec" || true
    if [[ "$cp" == "$cfg" ]]; then printf '%s\x1f%s' "$ae" "$f"; return 0; fi
  done
  printf '\x1f'
}

CFGS=$(ls configs/webcast-*-"$DP"-arm-wc.toml configs/twilio-*-"$DP"-arm.toml 2>/dev/null || true)
[[ -n "$CFGS" ]] || { page "study-nightly: NOTHING STAGED for $DATE" "no lane configs matching $DP — did slate-stage run?"; exit 1; }

# Call order: sort by [event].starts_at.
ORDERED=$(for c in $CFGS; do
  st=$(sed -n '/^\[event\]/,/^\[/s/^[[:space:]]*starts_at[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$c" | head -1)
  echo "${st:-9999}|$c"
done | sort | cut -d'|' -f2)

PROBLEMS=0
for cfg in $ORDERED; do
  base=$(basename "$cfg" .toml)
  ready="configs/.readiness/${base}.json"
  ev=$(sed -n '/^\[event\]/,/^\[/s/^[[:space:]]*id[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$cfg" | head -1)
  kw_sha=$(python3 scripts/keywords-extract.py "$cfg" --sha256 2>/dev/null || true)
  n_kw=$(python3 scripts/keywords-extract.py "$cfg" 2>/dev/null | grep -c . || true)
  if [[ "${n_kw:-0}" -eq 0 ]]; then
    echo "── $base: no keywords (capture-only) — skipping"
    continue
  fi
  # Fresh + matching already? Don't re-burn 10-30 min of `say`.
  if [[ -f "$ready" ]] && python3 - "$ready" "$kw_sha" <<'PY'
import json, sys
from datetime import date, datetime
a = json.load(open(sys.argv[1]))
ok = (a.get("keywords_sha256") == sys.argv[2]
      and (date.today() - datetime.strptime(a.get("date",""), "%Y-%m-%d").date()).days <= 1)
sys.exit(0 if ok else 1)
PY
  then
    echo "── $base: readiness artifact already fresh + hash-matched — keeping"
  else
    # Clone from a same-event sibling with identical keyword extraction?
    CLONED=0
    for sib in configs/.readiness/*-"$DP"-*.json; do
      [[ -f "$sib" && "$sib" != "$ready" ]] || continue
      if python3 - "$sib" "$kw_sha" "$base" "$ready" <<'PY'
import json, sys
from datetime import date, datetime
a = json.load(open(sys.argv[1]))
if a.get("keywords_sha256") != sys.argv[2]:
    sys.exit(1)
if (date.today() - datetime.strptime(a.get("date",""), "%Y-%m-%d").date()).days > 1:
    sys.exit(1)
a["config"] = sys.argv[3]
a["cloned_from"] = sys.argv[1]
json.dump(a, open(sys.argv[4] + ".tmp", "w"), indent=1)
import os
os.replace(sys.argv[4] + ".tmp", sys.argv[4])
sys.exit(0)
PY
      then
        echo "── $base: CLONED readiness from $(basename "$sib") (identical keyword set)"
        CLONED=1
        break
      fi
    done
    if [[ "$CLONED" -eq 0 && "$DRY_RUN" == 1 ]]; then
      echo "── [dry-run] WOULD run variant-study.sh $cfg (artifact stale or hash-mismatched)"
    elif [[ "$CLONED" -eq 0 ]]; then
      echo "════ $(date -u '+%H:%M:%SZ') variant-study $base ════"
      if ! scripts/variant-study.sh "$cfg" "/tmp/variant-study-$base" 2>&1 | tail -25; then
        page "study FAILED: $base" "variant-study.sh exited nonzero for $cfg — the readiness gate will refuse this event. Diagnose tonight."
        PROBLEMS=$((PROBLEMS+1))
      fi
    fi
  fi
  # Read the result — an unread study is how ZETA armed with confirmed:0.
  DEAD="" ; STUDY_DIR="/tmp/variant-study-$base" ; RESOLUTION=""
  if [[ -f "$ready" ]]; then
    DEAD=$(dead_suffixes "$ready")
    if [[ -n "$DEAD" ]]; then
      # ── 1. AUTOPSY: only NO-EVIDENCE/WEAK words are a widening problem ──
      ORIG_DEAD="$DEAD"
      N_ORIG=$(wc -w <<<"$ORIG_DEAD" | tr -d ' ')
      echo "── $base: $N_ORIG dead word(s): $ORIG_DEAD"
      WIDEN=$(autopsy_widenable "$STUDY_DIR" $ORIG_DEAD)
      NEARMISS=""
      for d in $ORIG_DEAD; do grep -qw "$d" <<<"$WIDEN" || NEARMISS="$NEARMISS $d"; done
      NEARMISS="${NEARMISS# }"

      # ── 2. WIDEN + 3. RE-STUDY ────────────────────────────────────────
      WIDENED="" RESTUDY_FAILED=0
      if [[ -n "$WIDEN" && "$DRY_RUN" == 1 ]]; then
        echo "── [dry-run] WOULD widen: $WIDEN"
        python3 scripts/variant-widen.py --config "$cfg" \
          --only "$(tr ' ' ',' <<<"$WIDEN")" || true
        echo "── [dry-run] WOULD re-run variant-study.sh $cfg"
      elif [[ -n "$WIDEN" ]]; then
        # PROPOSES ONLY: variant-study.sh re-scores every candidate against the
        # real detector below, so a bad candidate comes back 0 and changes
        # nothing. --write deliberately invalidates keywords_sha256 — the
        # widened set is UNSTUDIED until the re-run rewrites the artifact, and
        # preflight must refuse it until then.
        if python3 scripts/variant-widen.py --config "$cfg" \
             --only "$(tr ' ' ',' <<<"$WIDEN")" --write > "/tmp/widen-$base.txt" 2>&1; then
          # awk, not sed: BSD sed does not read \t in a pattern as a tab.
          WIDENED=$(awk -F'\t' '$1=="WROTE"{print $2}' "/tmp/widen-$base.txt" | tr '\n' ' ')
          cat "/tmp/widen-$base.txt"
        else
          echo "⚠ variant-widen.py failed for $base" >&2; cat "/tmp/widen-$base.txt" >&2
        fi
        if [[ -n "$WIDENED" ]]; then
          echo "════ $(date -u '+%H:%M:%SZ') RE-study $base after widening: $WIDENED ════"
          scripts/variant-study.sh "$cfg" "$STUDY_DIR-widened" > "/tmp/restudy-$base.log" 2>&1
          RS_RC=$?
          tail -25 "/tmp/restudy-$base.log"
          STUDY_DIR="$STUDY_DIR-widened"
          # rc 1 = some keyword scored 0 (expected — that is the residue we ack
          # below). rc 2 = the study could NOT RUN (no binary, no ORT dylib, no
          # [detector] block). Those are opposite situations and conflating them
          # is how a widening that never executed gets acked as "still dead".
          if (( RS_RC >= 2 )); then
            page "RE-STUDY COULD NOT RUN: $base" \
              "Widened $WIDENED, but scripts/variant-study.sh exited $RS_RC — it did not measure anything (see /tmp/restudy-$base.log; the usual causes are a missing target/release/earshot or ORT_DYLIB_PATH). The config's keywords_sha256 has CHANGED, so the readiness artifact no longer matches and preflight WILL refuse this event until a study runs. Nothing was acked."
            PROBLEMS=$((PROBLEMS+1))
            RESTUDY_FAILED=1
          fi
          DEAD=$(dead_suffixes "$ready")
        fi
      fi

      # ── 4. ACK THE RESIDUE ─────────────────────────────────────────────
      ACK="configs/.acks/${base}-dead-word.ack"
      N_RESID=$(wc -w <<<"$DEAD" | tr -d ' ')
      if [[ "$RESTUDY_FAILED" == 1 ]]; then
        # The widened set was never MEASURED, so "still dead" is unproven and
        # acking it would claim evidence that does not exist. Do not ack. The
        # page above already fired; preflight below writes the RED verdict, so
        # this is a loud refusal, not the silent third state.
        RESOLUTION="NOT-ACKED (re-study did not run; widened set unmeasured)"
      elif [[ -z "$DEAD" ]]; then
        RESOLUTION="FIXED by widening ($WIDENED)"
        page "DEAD WORDS FIXED: $base" \
          "Widened $WIDENED and re-studied: ALL keywords now confirm. Was dead: $ORIG_DEAD. No ack needed; the board arms whole."
      elif (( N_RESID * 3 > n_kw )); then
        # GUARDRAIL (a): a third of the board unhearable is a BROKEN CONFIG —
        # wrong engine, wrong models, a bad `prepare` run — not a set of hard
        # words. Acking would arm a board we do not understand, which is worse
        # than not arming it. Refuse and page a human.
        RESOLUTION="REFUSED-TO-ACK ($N_RESID/$n_kw still dead after widening)"
        page "BROKEN CONFIG (not hard words): $base" \
          "$N_RESID of $n_kw keywords are STILL dead after widening: $DEAD (was $ORIG_DEAD). More than a third of the board cannot be heard — that is a config/engine/model fault, not a variant fault. REFUSING to auto-ack. Diagnose: scripts/dead-word-autopsy.sh --auto --study-dir $STUDY_DIR"
        PROBLEMS=$((PROBLEMS+1))
      else
        # GUARDRAIL (b): a token the phrase screen flags is a DESCOPE question
        # (the market cannot be resolved by any variant), not a detectability
        # one. Never ack it — page for scripts/descope-event.sh instead.
        FLAGGED=$(phrase_screen_flagged "$cfg")
        ACKABLE="" REFUSED=""
        for d in $DEAD; do
          if grep -qw "$d" <<<"$FLAGGED"; then REFUSED="$REFUSED $d"
          else ACKABLE="$ACKABLE $d"; fi
        done
        ACKABLE="${ACKABLE# }" REFUSED="${REFUSED# }"
        if [[ "$DRY_RUN" == 1 ]]; then
          echo "── [dry-run] WOULD ack $ACK naming: ${ACKABLE:-none}${REFUSED:+ (REFUSED, descope question: $REFUSED)}"
        elif [[ -n "$ACKABLE" ]]; then
          mkdir -p configs/.acks
          PREV=""
          [[ -f "$ACK" ]] && PREV=$(cat "$ACK")
          {
            echo "# dead-word acknowledgement — $base"
            echo "# date: $(date -u +%F)"
            echo "# WRITTEN AUTOMATICALLY by scripts/study-nightly.sh."
            echo "#"
            echo "# These tokens scored 0 Confirmed on clean studio TTS through the"
            echo "# REAL detector, and still scored 0 after their variants were widened"
            echo "# from configs/variant-lexicon.toml + scripts/variant-widen.py."
            echo "# A word that fails clean TTS will not fire live."
            echo "#"
            echo "# Acked so the event arms and trades its ${n_kw:-?}-keyword board minus these"
            echo "# instead of forfeiting all of it (the 2026-08-07 TTWO lesson: 3 dead"
            echo "# words of 12 vetoed a board that traded 69,658 contracts)."
            echo "# This risks no money: scripts/posture-from-study.py pins any keyword"
            echo "# with ratio = confirmed/clips below the floor to a 1c non-crossing"
            echo "# cap, and a dead word has ratio 0 — it provably cannot fill."
            echo "#"
            echo "# Their markets are knowingly UNSERVED. Do not read silence there as"
            echo "# detector success."
            [[ -n "$WIDENED" ]] && echo "# Widened first (and re-studied): ${WIDENED% }"
            [[ -n "$NEARMISS" ]] && echo "# NEAR-MISS per autopsy (threshold fix, widening cannot help): $NEARMISS"
            [[ -n "$REFUSED" ]] && echo "# NOT acked — phrase-screen flagged, descope question: $REFUSED"
            echo "# Tokens: $ACKABLE"
            if [[ -n "$PREV" ]]; then
              echo ""
              echo "# ── superseded acknowledgement, kept verbatim ──"
              sed 's/^/# | /' <<<"$PREV"
            fi
          } > "$ACK"
          echo "── wrote $ACK naming: $ACKABLE"
        fi
        if [[ -n "$REFUSED" ]]; then
          RESOLUTION="ACKED:${ACKABLE:-none} DESCOPE-NEEDED:$REFUSED"
          page "DESCOPE NEEDED (not a detectability bug): $base" \
            "These dead tokens are flagged by scripts/market-phrase-screen.py — their armed variants cannot RESOLVE their market (the AAPL 'iPhone 18' class: a perfectly-heard wrong word scores 16/16). NOT auto-acked: $REFUSED. Fix the phrase or scripts/descope-event.sh them; preflight will stay RED until you do. Acked as unserved: ${ACKABLE:-none}. Widened + fixed: ${WIDENED:-none}."
          PROBLEMS=$((PROBLEMS+1))
        else
          RESOLUTION="ACKED:$ACKABLE"
          page "DEAD WORDS ACKED: $base" \
            "Was dead: $ORIG_DEAD. Widened + re-studied: ${WIDENED:-none}. STILL dead and now acked as knowingly UNSERVED: $ACKABLE${NEARMISS:+ | NEAR-MISS (needs a confirmed_threshold change, not variants): $NEARMISS}. The other $((n_kw - N_RESID)) keywords arm normally. Their markets are unserved — do not read silence there as detector success."
        fi
      fi

      # ── 5. PREFLIGHT, so a verdict artifact EXISTS either way ──────────
      # The 08-07 chain died here: no preflight ran, so state/<E>/preflight-
      # result.json never existed, so assert-ready.sh refused at T-75 and the
      # launchd `&&` short-circuited arm-on-url.sh out of existence. A RED
      # verdict is a legal outcome; a MISSING one is the third state.
      ACCT="" ENVF=""
      IFS=$'\x1f' read -r ACCT ENVF <<<"$(account_env_for "$cfg")" || true
      if [[ "$DRY_RUN" == 1 ]]; then
        echo "── [dry-run] WOULD run preflight-event.sh --config $cfg --account ${ACCT:-<none>}"
      elif [[ -n "$ACCT" ]]; then
        echo "════ $(date -u '+%H:%M:%SZ') preflight $base (post-resolution verdict) ════"
        scripts/preflight-event.sh --config "$cfg" --account "$ACCT" \
          ${ENVF:+--event-env "$ENVF"} > "/tmp/preflight-$base.log" 2>&1
        PF_RC=$?
        echo "preflight-event.sh exit=$PF_RC (log /tmp/preflight-$base.log)"
        (( PF_RC == 0 )) || PROBLEMS=$((PROBLEMS+1))
        RESOLUTION="$RESOLUTION preflight=$PF_RC"
      else
        # No autorun env ⇒ no account ⇒ no full verdict artifact. Run the
        # readiness gate alone so the resolution is at least PROVEN, and page
        # that nothing will write state/<E>/preflight-result.json.
        scripts/preflight-event.sh --config "$cfg" --only-readiness \
          > "/tmp/preflight-$base.log" 2>&1
        PF_RC=$?
        echo "preflight --only-readiness exit=$PF_RC (no account env for $cfg)"
        page "NO PREFLIGHT VERDICT POSSIBLE: $base" \
          "Dead words were resolved ($RESOLUTION) but no configs/*-autorun.env names this config, so there is no ACCOUNT_ENV and preflight-event.sh cannot write state/<EVENT_ID>/preflight-result.json. assert-ready.sh WILL refuse at T-75 (readiness gate alone exited $PF_RC). Stage the event env."
        PROBLEMS=$((PROBLEMS+1))
        RESOLUTION="$RESOLUTION preflight=NONE(no-account)"
      fi
    fi
  else
    page "NO READINESS ARTIFACT: $base" "study did not produce $ready — event will be refused by the gate"
    PROBLEMS=$((PROBLEMS+1))
  fi
  # --dry-run must leave NO trace, journal included: a preview that writes
  # lifecycle entries makes the journal lie about what actually ran.
  [[ -n "$ev" && "$DRY_RUN" == 0 ]] && scripts/journal.sh --event "$ev" --actor study-nightly.sh --action study \
    --outcome "$([[ -z "${DEAD:-}" && -f "$ready" ]] && echo ok || echo fail)" \
    --detail "artifact=$ready${DEAD:+ dead:$DEAD}${RESOLUTION:+ resolution=$RESOLUTION}" --log "$ready" || true
done

echo "ALL STUDIES DONE $(date -u '+%H:%M:%SZ') — problems: $PROBLEMS"
[[ "$PROBLEMS" -eq 0 ]] || exit 1
