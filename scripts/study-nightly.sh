#!/usr/bin/env bash
# Nightly keyword-readiness studies for a slate date — the scheduled
# generalization of the ad-hoc study-afternoon-26aug04.sh (which was re-written
# by hand per event day; this one takes the date).
#
#   scripts/study-nightly.sh --date 2026-08-05
#
# For every immutable schema-3 RunPlan whose starts_at_utc falls on that UTC
# date, in CALL ORDER (so a slow study can never leave the nearest event
# ungated). Config filenames are labels only: neither issuer tokens nor a
# filename date participate in selection. The RunPlan pins the exact config
# and event env used below.
#   * if another selected config's keywords_sha256 equals a fresh artifact,
#     CLONE the artifact to this basename (recorded via "cloned_from" — honest:
#     the gate re-hashes the config itself);
#   * else run scripts/variant-study.sh (Darwin `say` — laptop only).
# Every failure belonging to a verified RunPlan raises one proof-bearing
# critical alert in that run's lifecycle ledger. Only failures that cannot be
# bound to a trustworthy RunPlan use the legacy date-level notifier. Journals
# remain per event. Exit nonzero if anything needs attention.
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
python3 - "$DATE" <<'PY' >/dev/null 2>&1 || {
import datetime, sys
datetime.date.fromisoformat(sys.argv[1])
PY
  echo "usage: study-nightly.sh --date YYYY-MM-DD [--dry-run]" >&2
  exit 2
}
ALERTCTL="${_EARSHOT_STUDY_ALERTCTL:-$REPO/scripts/alertctl.py}"
NOTIFY="${_EARSHOT_STUDY_NOTIFY:-$REPO/scripts/notify.sh}"

# This compatibility path is deliberately date-level. It is used only when no
# verified RunPlan can safely accept an AlertRequest.
legacy_page() {
  if [[ "$DRY_RUN" == 1 ]]; then
    echo "── [dry-run] WOULD PAGE (no trustworthy RunPlan): $1"
    echo "$2" | sed 's/^/      /'
  else
    "$NOTIFY" "$1" "$2" --quiet >/dev/null 2>&1 || true
  fi
}

# Alert delivery can fail, but it can never erase or replace the originating
# study failure. alertctl owns durable request/delivery facts and its bounded
# retry/ambiguity policy; callers invoke it once and never loop delivery.
critical_alert() {  # <run-dir> <event-id> <code> <dedupe-key> <body>
  local run_dir="$1" event_id="$2" code="$3" dedupe="$4" body="$5"
  if [[ "$DRY_RUN" == 1 ]]; then
    echo "── [dry-run] WOULD REQUIRE CRITICAL ALERT: $event_id code=$code dedupe=$dedupe"
    return 0
  fi
  if [[ ! -x "$ALERTCTL" ]]; then
    echo "ALERT REFUSED $event_id: alertctl adapter is unavailable at $ALERTCTL" >&2
    return 0
  fi
  EARSHOT_REPO="$REPO" "$ALERTCTL" raise \
    --run-dir "$run_dir" --severity critical --code "$code" \
    --dedupe-key "$dedupe" --subject "Nightly study failed: $event_id" \
    --body "$body" >/dev/null 2>&1 || {
      echo "ALERT DELIVERY FAILED $event_id: original study failure remains nonzero" >&2
      return 0
    }
}

alert_manifest_runs() {  # <code> <dedupe-key> <body>
  local code="$1" dedupe="$2" body="$3"
  local starts_at run_dir cfg ev event_env plan_sha
  while IFS=$'\x1f' read -r starts_at run_dir cfg ev event_env plan_sha; do
    [[ -n "$run_dir" ]] || continue
    critical_alert "$run_dir" "$ev" "$code" "$dedupe" "$body"
  done < "$MANIFEST"
}

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

# Build the complete date manifest before doing any work. RunPlans are the
# execution authority and are selected only by starts_at_utc. The config scan
# is a completeness check using parsed event/env metadata: if staging expected
# a config for this date but failed to compile its schema-3 plan, the entire
# study is loudly non-green instead of silently studying the surviving subset.
MANIFEST=$(mktemp "${TMPDIR:-/tmp}/earshot-study-manifest.XXXXXX") || exit 2
trap 'rm -f -- "$MANIFEST"' EXIT
if ! python3 - "$REPO" "$DATE" > "$MANIFEST" <<'PY'
import datetime as dt
import json
import sys
import tomllib
from fnmatch import fnmatch
from pathlib import Path

repo = Path(sys.argv[1]).resolve()
wanted = dt.date.fromisoformat(sys.argv[2])
sys.path.insert(0, str(repo / "scripts"))
import eventctl as ec

errors = []
expected = {}
env_scopes = {}


def within_repo(path: Path, label: str):
    try:
        return path.resolve().relative_to(repo)
    except ValueError:
        errors.append(f"{label} escapes the repository: {path}")
        return None


# Use the same literal parser as RunPlan compilation. EVENT_DATE is only a
# completeness signal; it never selects a plan or overrides starts_at_utc.
for envp in sorted((repo / "configs").glob("*.env")):
    try:
        parsed = ec.parse_event_env(envp)
    except OSError as exc:
        errors.append(f"cannot read staged env {envp}: {exc}")
        continue
    config_path = parsed.get("config_path")
    event_date = parsed.get("event_date")
    if not config_path:
        continue
    cfg_abs = (repo / config_path).resolve()
    cfg_rel = within_repo(cfg_abs, f"{envp.name} CONFIG_PATH")
    if cfg_rel is None:
        continue
    env_scopes.setdefault(cfg_rel.as_posix(), []).append((envp, event_date))
    if event_date == wanted.isoformat() and not cfg_abs.is_file():
        errors.append(
            f"expected staged event {envp.name} has missing CONFIG_PATH "
            f"{cfg_rel.as_posix()} and therefore no RunPlan"
        )


for cfgp in sorted((repo / "configs").glob("*.toml")):
    if any(fnmatch(cfgp.name, pattern) for pattern in ec.SKIP_CONFIG_GLOBS):
        continue
    cfg_rel = cfgp.resolve().relative_to(repo).as_posix()
    scoped_envs = env_scopes.get(cfg_rel, [])
    env_expects = any(event_date == wanted.isoformat()
                      for _envp, event_date in scoped_envs)
    try:
        raw = tomllib.loads(cfgp.read_text(errors="replace"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        if env_expects:
            errors.append(f"expected staged config {cfg_rel} is unreadable: {exc}")
        continue
    event = raw.get("event")
    if not isinstance(event, dict) or not event.get("id"):
        if env_expects:
            errors.append(f"expected staged config {cfg_rel} has no [event].id")
        continue
    starts_raw = event.get("starts_at")
    starts_date = None
    if isinstance(starts_raw, str):
        try:
            starts = dt.datetime.fromisoformat(starts_raw.replace("Z", "+00:00"))
            if starts.tzinfo is None:
                starts = starts.replace(tzinfo=dt.timezone.utc)
            starts_date = starts.astimezone(dt.timezone.utc).date()
        except ValueError:
            pass
    if starts_date != wanted and not env_expects:
        continue
    try:
        info = ec.load_event_config(cfgp)
    except (OSError, ec.Refusal) as exc:
        errors.append(f"expected staged config {cfg_rel} is invalid: {exc}")
        continue
    expected[cfg_rel] = info["event_id"]
    if env_expects and starts_date != wanted:
        names = ", ".join(envp.name for envp, date in scoped_envs
                          if date == wanted.isoformat())
        errors.append(
            f"expected staged env {names} says {wanted}, but {cfg_rel} "
            f"starts on {starts_date or 'an unparseable date'} UTC"
        )


selected = {}
legacy = {}
events = {}
for plan_path in sorted((repo / "var" / "runs").glob("*/*/control/run-plan.json")):
    try:
        plan = json.loads(plan_path.read_text())
        starts = dt.datetime.fromisoformat(
            plan["starts_at_utc"].replace("Z", "+00:00")
        )
        if starts.tzinfo is None:
            raise ValueError("starts_at_utc is timezone-naive")
        starts = starts.astimezone(dt.timezone.utc)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"invalid RunPlan candidate {plan_path}: {exc}")
        continue
    if starts.date() != wanted:
        continue
    config_path = plan.get("config_path")
    if not isinstance(config_path, str):
        errors.append(f"RunPlan {plan_path} has no string config_path")
        continue
    if plan.get("schema") != ec.PLAN_SCHEMA or plan.get("kind") != "run-plan":
        legacy.setdefault(config_path, []).append(
            f"schema={plan.get('schema')!r} at {plan_path}"
        )
        continue
    values = {
        "starts": starts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_dir": str(plan_path.parent.parent),
        "config": config_path,
        "event": plan.get("event_id"),
        "event_env": plan.get("event_env_path") or "",
        "plan_sha": plan.get("plan_sha256"),
    }
    if not all(isinstance(v, str) for v in values.values()):
        errors.append(f"RunPlan {plan_path} has non-string study identity fields")
        continue
    if any("\x1f" in v or "\n" in v for v in values.values()):
        errors.append(f"RunPlan {plan_path} has unsafe study identity fields")
        continue
    if config_path in selected:
        errors.append(
            f"multiple schema-{ec.PLAN_SCHEMA} RunPlans select {config_path} "
            f"for {wanted}: {selected[config_path]['run_dir']} and "
            f"{values['run_dir']}"
        )
        continue
    event_id = values["event"]
    if event_id in events:
        errors.append(
            f"multiple schema-{ec.PLAN_SCHEMA} RunPlans select event {event_id} "
            f"for {wanted}: {events[event_id]} and {values['run_dir']}"
        )
        continue
    selected[config_path] = values
    events[event_id] = values["run_dir"]

for config_path, event_id in sorted(expected.items()):
    if config_path in selected:
        continue
    historical = legacy.get(config_path, [])
    suffix = f"; only historical {'; '.join(historical)} exists" if historical else ""
    errors.append(
        f"expected staged event {event_id} ({config_path}) has no immutable "
        f"schema-{ec.PLAN_SCHEMA} RunPlan for {wanted}{suffix}"
    )

for config_path, values in sorted(selected.items()):
    if config_path not in expected:
        errors.append(
            f"schema-{ec.PLAN_SCHEMA} RunPlan {values['run_dir']} selects "
            f"{config_path}, but parsed staging metadata does not place that "
            f"config on {wanted}"
        )

if not selected:
    errors.append(f"no immutable schema-{ec.PLAN_SCHEMA} RunPlans exist for {wanted}")

if errors:
    for error in errors:
        print(f"REFUSED: study-nightly: {error}", file=sys.stderr)
    raise SystemExit(1)

for values in sorted(selected.values(), key=lambda item: (
    item["starts"], item["event"], item["run_dir"]
)):
    print("\x1f".join((
        values["starts"], values["run_dir"], values["config"],
        values["event"], values["event_env"], values["plan_sha"],
    )))
PY
then
  legacy_page "study-nightly RUN-PLAN COVERAGE FAILED for $DATE" \
    "RunPlan selection/completeness failed. No keyword study ran; fix staging so every expected event has one valid schema-3 RunPlan."
  exit 1
fi

# Verify every selected plan and its pinned config/env before the first study;
# otherwise a late corrupt candidate could leave only a partial date measured.
PLAN_PROBLEMS=0
while IFS=$'\x1f' read -r starts_at run_dir cfg ev event_env plan_sha; do
  [[ -n "$run_dir" ]] || continue
  if ! EARSHOT_REPO="$REPO" python3 scripts/eventctl.py verify \
      --run-dir "$run_dir" >/dev/null; then
    echo "REFUSED: study-nightly: RunPlan verification failed for $ev ($run_dir)" >&2
    PLAN_PROBLEMS=$((PLAN_PROBLEMS + 1))
  fi
done < "$MANIFEST"
if [[ "$PLAN_PROBLEMS" -ne 0 ]]; then
  legacy_page "study-nightly RUN-PLAN INTEGRITY FAILED for $DATE" \
    "$PLAN_PROBLEMS selected RunPlan(s) failed integrity or pinned-input verification. No keyword study ran."
  exit 1
fi

STUDY_PLATFORM="${_EARSHOT_STUDY_PLATFORM:-$(uname -s)}"
if [[ "$STUDY_PLATFORM" != Darwin ]]; then
  echo "variant-study needs macOS 'say' — laptop only" >&2
  alert_manifest_runs "study_platform_blocked" "nightly-study-platform" \
    "The RunPlan-bound keyword study cannot execute on this host. Run it on the supported study host before preflight."
  exit 2
fi
EARSHOT_BIN="${EARSHOT_BIN:-target/release/earshot}"
if [[ ! -x "$EARSHOT_BIN" ]]; then
  echo "study-nightly BLOCKED: no executable $EARSHOT_BIN — build first" >&2
  alert_manifest_runs "study_binary_missing" "nightly-study-binary" \
    "The pinned keyword-study binary is unavailable. Build and verify the study host before preflight."
  exit 1
fi
export EARSHOT_BIN

PROBLEMS=0
while IFS=$'\x1f' read -r starts_at run_dir cfg ev event_env plan_sha; do
  [[ -n "$run_dir" ]] || continue
  base=$(basename "$cfg" .toml)
  RUN_PROBLEM=0 STUDY_EXEC_FAILED=0
  ready="configs/.readiness/${base}.json"
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
    # Clone only from another config selected by this date's RunPlans and only
    # when its exact keyword extraction hash matches. Readiness filenames are
    # destinations, never event/date selectors.
    CLONED=0
    while IFS=$'\x1f' read -r _sib_starts _sib_run sib_cfg _sib_ev _sib_env _sib_sha; do
      sib="configs/.readiness/$(basename "$sib_cfg" .toml).json"
      [[ -f "$sib" && "$sib" != "$ready" ]] || continue
      if python3 - "$sib" "$kw_sha" "$base" "$ready" "$DRY_RUN" <<'PY'
import json, sys
from datetime import date, datetime
a = json.load(open(sys.argv[1]))
if a.get("keywords_sha256") != sys.argv[2]:
    sys.exit(1)
if (date.today() - datetime.strptime(a.get("date",""), "%Y-%m-%d").date()).days > 1:
    sys.exit(1)
if sys.argv[5] != "1":
    a["config"] = sys.argv[3]
    a["cloned_from"] = sys.argv[1]
    json.dump(a, open(sys.argv[4] + ".tmp", "w"), indent=1)
    import os
    os.replace(sys.argv[4] + ".tmp", sys.argv[4])
sys.exit(0)
PY
      then
        if [[ "$DRY_RUN" == 1 ]]; then
          echo "── [dry-run] WOULD CLONE readiness from $(basename "$sib") to $ready (identical keyword set)"
        else
          echo "── $base: CLONED readiness from $(basename "$sib") (identical keyword set)"
        fi
        CLONED=1
        break
      fi
    done < "$MANIFEST"
    if [[ "$CLONED" -eq 0 && "$DRY_RUN" == 1 ]]; then
      echo "── [dry-run] WOULD run variant-study.sh $cfg (artifact stale or hash-mismatched)"
    elif [[ "$CLONED" -eq 0 ]]; then
      echo "════ $(date -u '+%H:%M:%SZ') variant-study $base ════"
      scripts/variant-study.sh "$cfg" "/tmp/variant-study-$base" \
        > "/tmp/study-$base.log" 2>&1
      VS_RC=$?
      tail -25 "/tmp/study-$base.log"
      if (( VS_RC >= 2 )); then
        echo "study FAILED TO RUN: $base (variant-study.sh exit=$VS_RC; readiness gate will refuse)" >&2
        PROBLEMS=$((PROBLEMS+1))
        RUN_PROBLEM=1
        STUDY_EXEC_FAILED=1
      elif (( VS_RC == 1 )); then
        echo "study measured one or more dead words for $base; resolving below"
      fi
    fi
  fi
  # Read the result — an unread study is how ZETA armed with confirmed:0.
  DEAD="" ; STUDY_DIR="/tmp/variant-study-$base" ; RESOLUTION=""
  if [[ "$STUDY_EXEC_FAILED" == 1 ]]; then
    # A stale artifact may predate the failed invocation. It is not evidence
    # from this attempt and must never drive widening or acknowledgement.
    RESOLUTION="STUDY-COULD-NOT-RUN (prior readiness ignored)"
  elif [[ -f "$ready" ]]; then
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

      # ── 2. PROPOSE, NEVER MUTATE A PUBLISHED RUN ───────────────────────
      # The schema-3 RunPlan pins the config bytes. The historical --write +
      # re-study path changed those bytes underneath an immutable plan, which
      # invalidated the very ledger needed to report the failure. A widening
      # proposal is still useful, but applying it must produce a new config
      # hash and therefore a new RunPlan attempt before another measurement.
      PROPOSAL="/tmp/widen-proposal-$base.txt"
      if [[ -n "$WIDEN" ]]; then
        if [[ "$DRY_RUN" == 1 ]]; then
          echo "── [dry-run] WOULD PROPOSE widening for: $WIDEN"
          python3 scripts/variant-widen.py --config "$cfg" \
            --only "$(tr ' ' ',' <<<"$WIDEN")" || true
        else
          python3 scripts/variant-widen.py --config "$cfg" \
            --only "$(tr ' ' ',' <<<"$WIDEN")" > "$PROPOSAL" 2>&1 || true
          cat "$PROPOSAL"
        fi
        RESOLUTION="NEW-RUN-REQUIRED (widening proposed for: $WIDEN)"
        echo "REFUSED $base: widening is required for $WIDEN; the verified RunPlan pins $cfg, so no config bytes were changed and nothing was auto-acked" >&2
        PROBLEMS=$((PROBLEMS+1))
        RUN_PROBLEM=1
      fi

      # ── 3. ACK ONLY MEASURED RESIDUE THAT NEEDS NO CONFIG CHANGE ───────
      ACK="configs/.acks/${base}-dead-word.ack"
      N_RESID=$(wc -w <<<"$DEAD" | tr -d ' ')
      if [[ -n "$WIDEN" ]]; then
        : # A new RunPlan must measure the proposed variants before any ack.
      elif (( N_RESID * 3 > n_kw )); then
        # GUARDRAIL (a): a third of the board unhearable is a BROKEN CONFIG —
        # wrong engine, wrong models, a bad `prepare` run — not a set of hard
        # words. Acking would arm a board we do not understand, which is worse
        # than not arming it. Refuse; the single run-bound critical alert below
        # carries this failure to the operator.
        RESOLUTION="REFUSED-TO-ACK ($N_RESID/$n_kw measured dead)"
        echo "BROKEN CONFIG $base: $N_RESID of $n_kw measured keywords are dead; refusing to auto-ack" >&2
        PROBLEMS=$((PROBLEMS+1))
        RUN_PROBLEM=1
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
            echo "# REAL detector with the exact variants pinned by this RunPlan."
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
          echo "DESCOPE NEEDED $base: phrase-screen refused measured tokens $REFUSED" >&2
          PROBLEMS=$((PROBLEMS+1))
          RUN_PROBLEM=1
        else
          RESOLUTION="ACKED:$ACKABLE"
          echo "DEAD WORDS ACKED $base: knowingly unserved=$ACKABLE; $((n_kw - N_RESID)) measured keywords remain eligible"
        fi
      fi

    fi

    # Preflight is deliberately not embedded here. The canonical prepare-day
    # chain invokes preflight-nightly next, and control.preflight is the sole
    # authority that validates the readiness artifact, writes the verdict, and
    # records study_completed/preflight receipts.
    [[ -n "$DEAD" ]] || RESOLUTION="no dead words"
  else
    echo "NO READINESS ARTIFACT $base: study did not produce $ready; preflight must refuse" >&2
    PROBLEMS=$((PROBLEMS+1))
    RUN_PROBLEM=1
    RESOLUTION="NO-READINESS-ARTIFACT"
  fi
  # --dry-run must leave NO trace, journal included: a preview that writes
  # lifecycle entries makes the journal lie about what actually ran.
  [[ -n "$ev" && "$DRY_RUN" == 0 ]] && scripts/journal.sh --event "$ev" --actor study-nightly.sh --action study \
    --outcome "$([[ "$RUN_PROBLEM" == 1 ]] && echo fail || { [[ -n "${DEAD:-}" ]] && echo warn || echo ok; })" \
    --detail "artifact=$ready${DEAD:+ dead:$DEAD}${RESOLUTION:+ resolution=$RESOLUTION}" --log "$ready" || true
  if [[ "$RUN_PROBLEM" == 1 ]]; then
    critical_alert "$run_dir" "$ev" "study_failed" "nightly-study" \
      "The RunPlan-bound keyword study did not complete cleanly for config $cfg. Inspect study-nightly output and the readiness evidence; preflight and scheduling must remain blocked."
  fi
done < "$MANIFEST"

echo "ALL STUDIES DONE $(date -u '+%H:%M:%SZ') — problems: $PROBLEMS"
[[ "$PROBLEMS" -eq 0 ]] || exit 1
