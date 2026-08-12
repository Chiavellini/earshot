#!/usr/bin/env bash
# slate-stage.sh — slate ⇒ staged (W2.3): turn every discovered event ≤3 days
# out into a ready config pair + autorun env + a PRINTED timer install line.
#
#   scripts/slate-stage.sh [--dry-run] [--slate <json>] [--horizon-days N]
#                          [--speculative]
#
# WHY THIS EXISTS. On 2026-07-28 PYPL and BA were DISCOVERED by the slate job
# and then silently missed because no process owned turning "discovered" into
# "staged" — a human was absent and nothing fired. The standing run-all-events
# policy (2026-07-28) says: stage EVERYTHING in the window, never ask which.
# This script is that owner. Every event inside the horizon leaves this script
# in exactly one loudly-printed state:
#
#   STAGED:         config pair + autorun env written; timer line printed —
#                   TIMER-INSTALL for phone-lane events, WEBCAST-TIMER (a
#                   do-not-install-event-autorun reminder) for webcast-only
#                   ones, since event-autorun fail-stops on TO_NUMBER=TBD
#   ALREADY-STAGED: configs + generic env are complete and agree with the
#                   authoritative staged time; nothing rewritten (idempotent)
#   RECONCILED:     an unpinned config carrying an explicit starts_at TODO was
#                   completed from the verified slate time and/or its missing
#                   generic env was created; an immutable RunPlan blocks this
#                   repair loudly instead of being changed behind its hash
#   NEEDS-TIME:     slate has no verified call time (or the call_time_utc is
#                   malformed → a BAD-TIME line) — configs staged with the
#                   starts_at TODO kept; NO autorun env written (its HHMM
#                   windows must be real arithmetic) and timer NOT printed
#   NO-PHONE-LANE:  dial-ins.toml admission is not "held" — webcast lane only
#                   (THE MSFT RULE: paper sources prove numbers, not admission;
#                   only a bridge-probe `held` clears a phone arm — Q-ADMIT)
#   WINDOW-WRAP:    call window crosses UTC midnight — env written with the
#                   HHMM windows left TBD (event-autorun compares plain HHMM);
#                   set END/COMPLETE by hand before arming the timer
#   ENV-STALE:      the env is missing a generated field or conflicts with the
#                   current config/time/port; it is never rewritten silently
#   ERROR:          that event's stage failed; the failed artifact is removed
#                   (never left half-written) and the run continues
#
# and the run ends with one summary line + one notify.sh mail. No silent path.
#
# WHAT "STAGED" MEANS, per event (EV, issuer ISS, lc, datepart from EV):
#   configs/<lc>-<datepart>-raw.toml            raw `earshot prepare` output,
#                                               kept UNPATCHED as the audit copy
#   configs/webcast-<lc>-<datepart>-arm-wc.toml webcast lane: fanout template
#                                               via stage-webcast-arm.sh (reused,
#                                               not reinvented), [risk]+keywords
#                                               merged in from the raw output,
#                                               [event].id = <EV>-WEBCAST
#   configs/twilio-<lc>-<datepart>-arm.toml     phone lane (admission="held"
#                                               only): raw output + unique bind
#                                               port + Q-REDIAL source params
#   configs/<lc>-<datepart>-autorun.env         event-autorun.sh env contract —
#                                               written ONLY when the slate has
#                                               a verified call time: its
#                                               windows are real arithmetic
#                                               (END_HHMM_LIVE = call+150 min,
#                                               COMPLETE_EARLIEST = call+45 min,
#                                               FIRE_HHMM = call−25 min, all
#                                               UTC — preflight-nightly.sh
#                                               consumes FIRE_HHMM for its
#                                               timer check).
#                                               TO_NUMBER/PASSCODE="TBD" ON
#                                               PURPOSE — event-autorun.sh
#                                               FAIL-STOPS on TBD, so an env
#                                               that never got its number hunt
#                                               can never place a call
#
# POST-PATCHES (applied to the LANE configs, never the raw audit copy):
#   * starts_at — `prepare` still hardcodes a TODO placeholder (src/prepare.rs,
#     `starts_at = "2026-07-15T00:00:00Z"  # TODO...`). Until prepare grows a
#     --starts-at flag we patch the real call_time_utc in afterwards, exactly
#     like scripts/wnt-daily.sh does. No time in the slate ⇒ the TODO stays
#     (date corrected, comment kept) and the event is NEEDS-TIME.
#   * engine — prepare emits sherpa_kws; production runs streaming_ctc (19 of
#     30 arm configs). Benchmarking/arming the wrong engine is exactly what
#     made 2026-07-29's zero-recall result unactionable, so we pin the
#     production engine here (wnt-daily.sh pattern; kws block kept for a
#     one-line revert).
#   * bind port — collision-free ACROSS runs, not just within one: every
#     BIND_PORT= in configs/*-autorun.env and every [[sources]] bind in
#     configs/twilio-*.toml is scanned first; a new event takes the lowest
#     free port >= 8080, so concurrent same-day calls (and hand-staged
#     configs) never fight for a listener (MULTI-EVENT §8; the port also
#     lands in the env as BIND_PORT). PORT: lines print for ALREADY-STAGED
#     events too — the full day's port map is one grep.
#   * webcast merge safety — the [risk]+keywords merge runs in a TEMP file
#     and must pass a sanity grep (real <EV> tickers present, no template
#     -CAPT allowlist, no dummy guidance keyword, no __ placeholders) BEFORE
#     it is moved into configs/. A failed merge leaves NOTHING at the real
#     path; the event reports ERROR and the next run retries.
#
# THE TIMER LINE IS PRINTED, NEVER EXECUTED. Arming a box timer is the last
# irreversible step and the first real invocation of this script is reviewed
# by a human; printing the exact ssh line keeps the decision one paste away
# without this script ever owning it. (install-event-timer.sh fire times are
# BOX-LOCAL = UTC on the us-east box; all HHMMs below are computed in UTC.)
#
# NOT DONE HERE, on purpose:
#   * variant-study.sh (keyword readiness gate) — studied separately; wiring
#     the gate into staging is its own task. Do not add it here casually: it
#     needs Darwin + models and would make this script laptop-only.
#   * launching, preflight, number hunts — downstream, human-gated.
#
# Portability: macOS laptop + Linux box. All date math via python3 (BSD/GNU
# `date -d` diverge); no `sed -i`; rewrites go through python3.
set -euo pipefail

REPO="${EARSHOT_REPO:-$HOME/Code/ear}"
cd "$REPO" || { echo "ERROR: no repo at $REPO" >&2; exit 2; }

DRY=0
SLATE="slate/latest.json"
HORIZON=3
SPEC_FLAG=""
ONLY_ISSUER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)      DRY=1; shift ;;
    --slate)        SLATE="$2"; shift 2 ;;
    --horizon-days) HORIZON="$2"; shift 2 ;;
    --speculative)  SPEC_FLAG="--speculative"; shift ;;
    # Same-day promotion (2026-08-04, SPOT). A morning bridge-probe can flip
    # admission to `held` hours before the call, but the nightly sweep has
    # already run and will not run again in time. This re-stages ONE issuer on
    # demand, through this same script, so the promoted event gets the real
    # phone-lane derivation (Q-REDIAL values and all) instead of a hand-rolled
    # config. Idempotence above already handles "webcast exists, phone does
    # not" — that is exactly the state a freshly-promoted venue is in.
    --issuer)       ONLY_ISSUER=$(tr '[:lower:]' '[:upper:]' <<<"$2"); shift 2 ;;
    *) echo "usage: slate-stage.sh [--dry-run] [--slate <json>] [--horizon-days N] [--speculative] [--issuer TICKER]" >&2
       exit 2 ;;
  esac
done

note() { echo "[$(date -u +%T)] $*"; }
# One mail per run (plus precondition failures). --quiet: this runs unattended.
page() { if [[ $DRY -eq 1 ]]; then note "DRY: would page: $1"; else
           "$REPO/scripts/notify.sh" "$1" "$2" --quiet || true; fi; }

REPORT=""   # accumulated detail lines for the summary mail
report() { echo "$*"; REPORT+="$*"$'\n'; }
reconcile_staged_config() {  # $1 config, $2 authoritative UTC start
  if [[ $DRY -eq 1 ]]; then
    python3 scripts/stage-reconcile.py --repo "$REPO" --run-root "$RUN_ROOT" \
      config --config "$1" --starts-at "$2" --check-only
  else
    python3 scripts/stage-reconcile.py --repo "$REPO" --run-root "$RUN_ROOT" \
      config --config "$1" --starts-at "$2"
  fi
}

STAGED=0; NEEDTIME=0; NOPHONE=0; ERRORS=0; ALREADY=0

# ── preconditions (hard stops — a half-configured stager that limps along is
#    how PYPL/BA got missed; fail loud instead) ──────────────────────────────
command -v python3 >/dev/null || { note "ERROR: no python3"; exit 2; }
[[ -f "$SLATE" ]] || { note "ERROR: no slate at $SLATE"
  page "slate-stage: NO SLATE" "Expected $SLATE — did slate-daily run? Nothing staged."; exit 2; }
[[ -f configs/dial-ins.toml ]] || { note "ERROR: configs/dial-ins.toml missing"; exit 2; }
[[ -x scripts/stage-webcast-arm.sh ]] || { note "ERROR: scripts/stage-webcast-arm.sh missing"; exit 2; }
[[ -f scripts/stage-reconcile.py ]] || { note "ERROR: scripts/stage-reconcile.py missing"; exit 2; }
BIN="${EARSHOT_BIN:-$REPO/target/release/earshot}"
if [[ ! -x "$BIN" ]]; then
  note "WARN: no pinned binary at $BIN — falling back to 'cargo run --release' for prepare"
fi
# `prepare` hits the live Kalshi API; missing creds is a hard stop, not a
# silent skip (wnt-daily pattern).
if [[ -f "$HOME/.kalshi/earshot_env" ]]; then
  # shellcheck disable=SC1091
  source "$HOME/.kalshi/earshot_env"
elif [[ $DRY -eq 0 ]]; then
  note "ERROR: no ~/.kalshi/earshot_env — prepare needs live creds"
  page "slate-stage: NO CREDENTIALS" "~/.kalshi/earshot_env missing; nothing staged."; exit 2
fi

ACCOUNT_ENV_DEFAULT="${EARSHOT_ACCOUNT_ENV:-\$HOME/.earshot/ms.env}"
BOX="${EARSHOT_BOX:-ubuntu@3.212.174.94}"   # c6a.4xlarge + EIP (deploy-box.sh)
RUN_ROOT="${EARSHOT_RUN_ROOT:-$REPO/var/runs}"

TMP="$(mktemp -d "${TMPDIR:-/tmp}/slate-stage.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

run_prepare() {  # $1 = EV, $2 = out path
  if [[ -x "$BIN" ]]; then "$BIN" prepare --event-ticker "$1" --out "$2"
  else cargo run --release --quiet -- prepare --event-ticker "$1" --out "$2"; fi
}

# ── parse the slate: rows within [today, today+HORIZON], sorted so report
#    order is deterministic (ports are stable via the on-disk scan below) ─────
ELIG="$TMP/eligible.tsv"
WARN="$TMP/slate-warn.txt"
PARSE_OK=1
python3 - "$SLATE" "$HORIZON" >"$ELIG" 2>"$WARN" <<'PY' || PARSE_OK=0
import datetime, json, sys
path, horizon = sys.argv[1], int(sys.argv[2])
d = json.load(open(path))
today = datetime.datetime.now(datetime.timezone.utc).date()
# Staleness: staging tomorrow's calls from the day-before-yesterday's sweep
# means events discovered since are invisible — warn, don't block (a stale
# stage beats the empty one that missed PYPL/BA).
gen = d.get("generated_utc", "")
try:
    g = datetime.datetime.fromisoformat(gen)
    age_h = (datetime.datetime.now(datetime.timezone.utc) - g).total_seconds() / 3600
    if age_h > 48:
        print(f"STALE-SLATE: generated {age_h:.0f}h ago ({gen}) — rerun slate-daily", file=sys.stderr)
except ValueError:
    print(f"STALE-SLATE: unparseable generated_utc {gen!r}", file=sys.stderr)
rows = []
for e in d.get("events", []):
    ev, iss, cd = e.get("event_ticker"), e.get("issuer"), e.get("call_date")
    if not (ev and iss and cd):
        print(f"BAD-ROW: {e}", file=sys.stderr); continue
    try:
        cdd = datetime.date.fromisoformat(cd)
    except ValueError:
        print(f"BAD-ROW: {ev} unparseable call_date {cd!r}", file=sys.stderr); continue
    if not (today <= cdd <= today + datetime.timedelta(days=horizon)):
        continue
    t = e.get("call_time_utc") or "-"          # ISO ..T HH:MM:SSZ, usually null
    corrected = "1" if e.get("call_date_source") == "authoritative_correction" else "0"
    disputed = "1" if (
        e.get("call_date_disputed")
        or (e.get("ticker_date_misleading") and corrected == "0")
    ) else "0"
    rows.append((cd, ev, iss, t, disputed, corrected))
rows.sort()
for cd, ev, iss, t, disputed, corrected in rows:
    print("\t".join([ev, iss, cd, t, disputed, corrected]))
PY
if [[ $PARSE_OK -ne 1 ]]; then
  note "ERROR: slate parse failed ($SLATE)"; [[ -s "$WARN" ]] && cat "$WARN" >&2
  page "slate-stage: SLATE PARSE FAILED" "Could not parse $SLATE — nothing staged. $(cat "$WARN" 2>/dev/null)"
  exit 2
fi
# surface the parser's warning tags (STALE-SLATE / BAD-ROW) into the mail too
if [[ -s "$WARN" ]]; then
  while IFS= read -r wline; do report "$wline"; done < "$WARN"
fi
N_ELIG=$(wc -l < "$ELIG" | tr -d ' ')
note "slate: $SLATE — $N_ELIG event(s) within $HORIZON day(s)"
if [[ "$N_ELIG" -eq 0 ]]; then
  report "SUMMARY: STAGED: 0 | NEEDS-TIME: 0 | NO-PHONE-LANE: 0 | ERRORS: 0"
  # The mail carries the BAD-ROW/STALE-SLATE lines too — a fully-poisoned
  # slate must read as "N rows rejected", never as "nothing to do".
  page "slate-stage: nothing in window" "$(printf 'No slate events within %s days (%s).\n\n%s' \
    "$HORIZON" "$SLATE" "$REPORT")"
  exit 0
fi

admission_of() {  # $1 = issuer → prints admission state, "untested" or "no-entry"
  python3 - configs/dial-ins.toml "$1" <<'PY'
import re, sys
path, issuer = sys.argv[1], sys.argv[2]
cur, seen, adm = None, False, None
for line in open(path):
    m = re.match(r'^\[([A-Za-z0-9_.]+)\]\s*$', line)
    if m:
        cur = m.group(1)
        if cur == issuer: seen = True
        continue
    if cur == issuer:
        m = re.match(r'^admission\s*=\s*"([^"]+)"', line)
        if m: adm = m.group(1); break
print(adm if adm else ("untested" if seen else "no-entry"))
PY
}

# ── bind ports: collision-free ACROSS runs. Every port already claimed on
#    disk — BIND_PORT= in any configs/*-autorun.env, or a [[sources]] bind in
#    any configs/twilio-*.toml (hand-staged included) — is off-limits; a new
#    event takes the lowest free port >= 8080. ───────────────────────────────
USED_PORTS=""
port_used() { [[ " $USED_PORTS " == *" $1 "* ]]; }
port_of_existing() {  # $@: candidate files; prints the first staged port, if any
  local f p=""
  for f in "$@"; do
    if [[ -z "$p" && -f "$f" ]]; then
      p=$(sed -n -e 's/^BIND_PORT=\([0-9][0-9]*\).*/\1/p' \
                 -e 's/^bind = "127\.0\.0\.1:\([0-9][0-9]*\)".*/\1/p' "$f" | head -n1)
    fi
  done
  printf '%s' "$p"
}
seed_ports() {
  local f p
  for f in configs/*-autorun.env configs/twilio-*.toml; do
    [[ -f "$f" ]] || continue
    while IFS= read -r p; do
      if ! port_used "$p"; then USED_PORTS+=" $p"; fi
    done < <(sed -n -e 's/^BIND_PORT=\([0-9][0-9]*\).*/\1/p' \
                    -e 's/^bind = "127\.0\.0\.1:\([0-9][0-9]*\)".*/\1/p' "$f")
  done
}
alloc_port() {  # → $PORT: lowest free port >= 8080, claimed for the rest of this run
  PORT=8080
  while port_used "$PORT"; do PORT=$((PORT + 1)); done
  USED_PORTS+=" $PORT"
}
seed_ports
note "ports already claimed on disk:${USED_PORTS:- none}"

while IFS=$'\t' read -r EV ISS CDATE CTIME DISPUTED CORRECTED; do
  [[ -n "$ONLY_ISSUER" && "$ISS" != "$ONLY_ISSUER" ]] && continue
  LC=$(tr '[:upper:]' '[:lower:]' <<<"$ISS")
  DATEPART=$(tr '[:upper:]' '[:lower:]' <<<"${EV##*-}")
  RAW="configs/${LC}-${DATEPART}-raw.toml"
  PHONE="configs/twilio-${LC}-${DATEPART}-arm.toml"
  WC="configs/webcast-${LC}-${DATEPART}-arm-wc.toml"
  ENVF="configs/${LC}-${DATEPART}-autorun.env"
  UNDATED_WC="configs/webcast-${LC}-arm.toml"   # stage-webcast-arm.sh's output

  if ! ADM=$(admission_of "$ISS"); then
    report "ERROR: $EV admission lookup — python parse of configs/dial-ins.toml failed; event skipped"
    ERRORS=$((ERRORS + 1)); continue
  fi
  PHONE_OK=0; [[ "$ADM" == "held" ]] && PHONE_OK=1
  HAVE_TIME=0; [[ "$CTIME" != "-" ]] && HAVE_TIME=1
  EVENT_CHANGED=0

  note "── $EV ($ISS) call_date=$CDATE call_time=${CTIME} admission=$ADM"
  [[ "$DISPUTED" == "1" ]] && report "DATE-DISPUTED: $EV — slate flags the call date; verify before trusting the timer (Kalshi metadata lies in stereo — HOOD 07-28)"
  [[ "$CORRECTED" == "1" ]] && report "DATE-CORRECTED: $EV — typed authoritative correction selected $CDATE; provenance remains in the merged slate"

  # ── window/fire arithmetic, all derived from call_time_utc in ONE guarded
  #    place. A malformed (hand-edited) time is a BAD-TIME line + the event
  #    downgrades to NEEDS-TIME — never a mid-run abort, and never a bogus
  #    starts_at. END/COMPLETE are plain integer HHMMs (event-autorun compares
  #    with -ge; a leading zero would be a bad octal literal); FIRE_HHMM is
  #    ZERO-PADDED to match install-event-timer.sh --fire-hhmm slicing. ──────
  END_HHMM="TBD"; CE_HHMM="TBD"; FIRE_HHMM=""; FIRE_DATE=""
  if [[ $HAVE_TIME -eq 1 ]]; then
    if TIMEV=$(python3 - "$CTIME" <<'PY'
import datetime, sys
try:
    dt = datetime.datetime.strptime(sys.argv[1], "%Y-%m-%dT%H:%M:%SZ")
except ValueError:
    sys.exit(1)
end  = dt + datetime.timedelta(minutes=150)  # redial window close (END_HHMM_LIVE)
ce   = dt + datetime.timedelta(minutes=45)   # earliest a finished run counts as the call
fire = dt - datetime.timedelta(minutes=25)   # timer fire (T-25; own date, may be day-1)
if end.date() != dt.date() or ce.date() != dt.date():
    e = c = "WRAP"                           # HHMM window arithmetic cannot cross midnight
else:
    e, c = int(end.strftime("%H%M")), int(ce.strftime("%H%M"))
print(e, c, fire.strftime("%H%M"), fire.strftime("%Y-%m-%d"))
PY
    ); then
      read -r END_HHMM CE_HHMM FIRE_HHMM FIRE_DATE <<<"$TIMEV"
      if [[ "$END_HHMM" == "WRAP" ]]; then
        report "WINDOW-WRAP: $EV — call window crosses UTC midnight; END/COMPLETE HHMMs left TBD, set by hand"
        END_HHMM="TBD"; CE_HHMM="TBD"
      fi
    else
      report "BAD-TIME: $EV — call_time_utc '$CTIME' is not YYYY-MM-DDTHH:MM:SSZ; treated as NEEDS-TIME"
      HAVE_TIME=0
    fi
  fi

  # ── port: reuse what disk already pinned for this event, else lowest free ─
  EXIST_PORT=$(port_of_existing "$PHONE" "$ENVF")
  if [[ -n "$EXIST_PORT" ]]; then PORT="$EXIST_PORT"; else alloc_port; fi

  # ── starts_at for this event ──────────────────────────────────────────────
  if [[ $HAVE_TIME -eq 1 ]]; then
    START_ISO="$CTIME"
  else
    START_ISO="${CDATE}T00:00:00Z"     # placeholder; TODO comment re-attached below
  fi

  # ── 1. raw prepare output (audit copy; reused if a prior run made it) ─────
  if [[ -f "$RAW" ]]; then
    note "raw exists, reusing: $RAW"
  elif [[ $DRY -eq 1 ]]; then
    note "DRY: would run: prepare --event-ticker $EV --out $RAW"
  else
    # < /dev/null: cargo/prepare must never drain the event list off stdin
    if ! run_prepare "$EV" "$RAW" < /dev/null; then
      rm -f "$RAW"   # a truncated raw file must not be "reused" by the next run
      report "ERROR: $EV — prepare failed (creds? API? see output above); event skipped"
      ERRORS=$((ERRORS + 1)); continue
    fi
    note "prepared $RAW"
    EVENT_CHANGED=1
  fi

  # ── 2. webcast twin (stage-webcast-arm.sh, then rename + merge + patch) ───
  if [[ -f "$WC" ]]; then
    if [[ $HAVE_TIME -eq 1 ]]; then
      if ! CONFIG_STATE=$(reconcile_staged_config "$WC" "$CTIME" 2>&1); then
        report "ERROR: $EV — webcast config is not safely reconcilable: $CONFIG_STATE"
        ERRORS=$((ERRORS + 1)); continue
      fi
      note "webcast twin $CONFIG_STATE: $WC"
      if [[ "$CONFIG_STATE" == "RECONCILED" ]]; then
        report "RECONCILED: $EV — completed starts_at in $WC from typed slate time"
        EVENT_CHANGED=1
      fi
    else
      note "webcast twin exists but slate has no verified time: $WC (not ready)"
    fi
  elif [[ $DRY -eq 1 ]]; then
    if ! GUARD_STATE=$(python3 scripts/stage-reconcile.py --repo "$REPO" --run-root "$RUN_ROOT" \
        guard --config "$WC" 2>&1); then
      report "ERROR: $EV — webcast path cannot be staged: $GUARD_STATE"
      ERRORS=$((ERRORS + 1)); continue
    fi
    note "DRY: would run: EARDIR=$REPO scripts/stage-webcast-arm.sh $SPEC_FLAG $LC $EV $START_ISO"
    note "DRY: would merge [risk]+keywords from $RAW in a TEMP copy (id → $EV-WEBCAST, engine → streaming_ctc), sanity-grep it, then install as $WC"
  else
    if ! GUARD_STATE=$(python3 scripts/stage-reconcile.py --repo "$REPO" --run-root "$RUN_ROOT" \
        guard --config "$WC" 2>&1); then
      report "ERROR: $EV — webcast path cannot be staged: $GUARD_STATE"
      ERRORS=$((ERRORS + 1)); continue
    fi
    if [[ -f "$UNDATED_WC" ]]; then
      # A hand-staged undated config is live operator work; clobbering it to
      # mint our dated twin is exactly the frozen-path edit we must not make.
      report "ERROR: $EV — webcast twin NOT staged: $UNDATED_WC already exists and stage-webcast-arm.sh would overwrite it; resolve by renaming the hand-staged file"
      ERRORS=$((ERRORS + 1)); continue
    fi
    # < /dev/null: the stager must never drain the event list off stdin
    if ! EARDIR="$REPO" scripts/stage-webcast-arm.sh $SPEC_FLAG "$LC" "$EV" "$START_ISO" < /dev/null; then
      report "ERROR: $EV — stage-webcast-arm.sh failed (template missing?); webcast twin not staged"
      ERRORS=$((ERRORS + 1)); continue
    fi
    # Merge in a TEMP file and sanity-check it BEFORE anything lands at $WC:
    # a half-merged config at the real path is a booby trap for every launcher
    # that globs configs/; failure must leave the event UN-staged so the next
    # run retries cleanly.
    WCTMP="$TMP/webcast-${LC}-${DATEPART}.merge.toml"
    if ! mv "$UNDATED_WC" "$WCTMP"; then
      report "ERROR: $EV — could not move $UNDATED_WC aside for the merge; webcast twin not staged"
      ERRORS=$((ERRORS + 1)); continue
    fi
    # merge the real [risk]+[[keywords]] tail from the raw prepare output over
    # the template's __EVENT_TICKER__-CAPT placeholder (the manual "paste
    # prepare output over the placeholders" step, automated), retag the event
    # id so the phone lane and webcast lane get distinct state dirs
    # (PHONE-LANE-HANDOFF §7.3), and pin the production engine.
    if ! python3 - "$WCTMP" "$RAW" "$EV" "$HAVE_TIME" <<'PY'
import re, sys
wc, raw, ev, have_time = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "1"
def die(msg):
    print(f"merge: {msg}", file=sys.stderr); sys.exit(1)
w = open(wc).read().splitlines(keepends=True)
r = open(raw).read().splitlines(keepends=True)
try:
    wi = next(i for i, l in enumerate(w) if l.rstrip() == "[risk]")
    ri = next(i for i, l in enumerate(r) if l.rstrip() == "[risk]")
except StopIteration:
    die(f"no [risk] section in {wc} or {raw}")
s = "".join(w[:wi]) + "".join(r[ri:])
old_id = f'id        = "{ev}"'
if old_id not in s: die(f"event id line {old_id!r} not found in {wc}")
s = s.replace(old_id, f'id        = "{ev}-WEBCAST"', 1)
if 'engine = "streaming_ctc"' not in s:
    ctc = '''engine = "streaming_ctc"

[detector.streaming_ctc]
model_dir         = "models/sherpa-onnx-streaming-zipformer-ctc-small-2024-03-18"
num_threads       = 1
default_threshold = 0.15
refractory_ms     = 2000

# sherpa_kws block kept so the engine reverts with a one-line change above.
'''
    if 'engine = "sherpa_kws"\n' not in s: die(f"no engine line to patch in {wc}")
    s = s.replace('engine = "sherpa_kws"\n', ctc, 1)
if have_time is False:
    # keep the TODO loud — arm/preflight must not trust a midnight start
    s, n = re.subn(r'^(starts_at = "[^"]+")$',
                   r'\1  # TODO: set the real call time (UTC) — slate had none',
                   s, count=1, flags=re.M)
    if n != 1: die(f"could not re-attach starts_at TODO in {wc}")
open(wc, "w").write(s)
PY
    then
      rm -f "$WCTMP"
      report "ERROR: $EV — webcast merge failed; nothing staged at $WC (next run retries)"
      ERRORS=$((ERRORS + 1)); continue
    fi
    # sanity grep BEFORE install: real event tickers present, no template
    # -CAPT allowlist, no dummy guidance keyword (matched with the TEMPLATE's
    # exact spacing — a real prepare-emitted "guidance" market aligns
    # differently and must pass), no __ placeholders left.
    SANITY=""
    if ! grep -q "\"${EV}-WEBCAST\"" "$WCTMP"; then
      SANITY="event id ${EV}-WEBCAST missing"
    fi
    if ! sed -n '/allowlist_tickers = \[/,/\]/p' "$WCTMP" | grep -q "\"${EV}-"; then
      SANITY="${SANITY:+$SANITY; }no real ${EV}-* allowlist tickers"
    fi
    if grep -q -- '-CAPT"' "$WCTMP"; then
      SANITY="${SANITY:+$SANITY; }template -CAPT allowlist still present"
    fi
    if grep -qF 'phrase          = "guidance"' "$WCTMP"; then
      SANITY="${SANITY:+$SANITY; }template dummy guidance keyword still present"
    fi
    if grep -q '__' "$WCTMP"; then
      SANITY="${SANITY:+$SANITY; }__ placeholders left"
    fi
    if [[ -n "$SANITY" ]]; then
      rm -f "$WCTMP"
      report "ERROR: $EV — webcast sanity check failed ($SANITY); nothing staged at $WC (next run retries)"
      ERRORS=$((ERRORS + 1)); continue
    fi
    if ! mv "$WCTMP" "$WC"; then
      rm -f "$WCTMP"
      report "ERROR: $EV — could not install $WC; nothing staged (next run retries)"
      ERRORS=$((ERRORS + 1)); continue
    fi
    note "staged $WC (id ${EV}-WEBCAST, engine streaming_ctc, keywords merged, sanity-checked)"
    EVENT_CHANGED=1
  fi

  # ── 3. phone lane, gated on a PROBED bridge (admission="held" only) ───────
  if [[ $PHONE_OK -eq 0 ]]; then
    report "NO-PHONE-LANE: $ISS ($ADM)"
    NOPHONE=$((NOPHONE + 1))
  elif [[ -f "$PHONE" ]]; then
    if [[ $HAVE_TIME -eq 1 ]]; then
      if ! CONFIG_STATE=$(reconcile_staged_config "$PHONE" "$CTIME" 2>&1); then
        report "ERROR: $EV — phone config is not safely reconcilable: $CONFIG_STATE"
        ERRORS=$((ERRORS + 1)); continue
      fi
      note "phone config $CONFIG_STATE: $PHONE"
      if [[ "$CONFIG_STATE" == "RECONCILED" ]]; then
        report "RECONCILED: $EV — completed starts_at in $PHONE from typed slate time"
        EVENT_CHANGED=1
      fi
    else
      note "phone config exists but slate has no verified time: $PHONE (not ready)"
    fi
  elif [[ $DRY -eq 1 ]]; then
    if ! GUARD_STATE=$(python3 scripts/stage-reconcile.py --repo "$REPO" --run-root "$RUN_ROOT" \
        guard --config "$PHONE" 2>&1); then
      report "ERROR: $EV — phone path cannot be staged: $GUARD_STATE"
      ERRORS=$((ERRORS + 1)); continue
    fi
    note "DRY: would derive $PHONE from $RAW (starts_at=$START_ISO, engine streaming_ctc, bind :$PORT, Q-REDIAL params)"
  else
    if ! GUARD_STATE=$(python3 scripts/stage-reconcile.py --repo "$REPO" --run-root "$RUN_ROOT" \
        guard --config "$PHONE" 2>&1); then
      report "ERROR: $EV — phone path cannot be staged: $GUARD_STATE"
      ERRORS=$((ERRORS + 1)); continue
    fi
    if ! python3 - "$RAW" "$PHONE" "$EV" "$ISS" "$START_ISO" "$PORT" "$HAVE_TIME" <<'PY'
import re, sys
raw, phone, ev, iss, start, port, have_time = sys.argv[1:8]
have_time = have_time == "1"
def die(msg):
    print(f"phone-derive: {msg}", file=sys.stderr); sys.exit(1)
s = open(raw).read()
todo = "" if have_time else "  # TODO: set the real call time (UTC) — slate had none"
s, n = re.subn(r'^starts_at = .*$', f'starts_at = "{start}"{todo}', s, count=1, flags=re.M)
if n != 1: die(f"no starts_at line in {raw}")
ctc = '''engine = "streaming_ctc"

[detector.streaming_ctc]
model_dir         = "models/sherpa-onnx-streaming-zipformer-ctc-small-2024-03-18"
num_threads       = 1
default_threshold = 0.15
refractory_ms     = 2000

# sherpa_kws block kept so the engine reverts with a one-line change above.
'''
if 'engine = "sherpa_kws"\n' not in s: die(f"no engine line in {raw}")
s = s.replace('engine = "sherpa_kws"\n', ctc, 1)
old_bind = 'bind = "127.0.0.1:8080"\n'
if old_bind not in s: die(f"expected twilio bind :8080 in {raw} — prepare output changed shape?")
s = s.replace(old_bind, f'''bind = "127.0.0.1:{port}"
# Event-wide Q-REDIAL budget. event-autorun starts one Rust process; these are
# call-leg replacements behind its existing listener, never arm restarts.
max_reconnects    = 20
idle_timeout_secs = 8
''', 1)
hdr = f'''# TWILIO PHONE-LANE ARM — {iss} {start}. Derived by scripts/slate-stage.sh from
# {raw}: keywords/allowlist/risk IDENTICAL; [[sources]] is twilio_stream :{port}
# (unique per MULTI-EVENT §8). Runs CONCURRENTLY with the -WEBCAST lane
# (distinct [event].id ⇒ distinct state dir — PHONE-LANE-HANDOFF §7.3).
# ⚠ streaming_ctc dlopens onnxruntime: the launcher MUST export ORT_DYLIB_PATH
# or the detector build hangs AFTER the WS bind (07-29 phone-lane failure;
# launch-event.sh exports it).
'''
open(phone, "w").write(hdr + s)
PY
    then
      rm -f "$PHONE"   # never leave a partial phone config behind
      report "ERROR: $EV — phone-lane derivation failed; $PHONE not written"
      ERRORS=$((ERRORS + 1)); continue
    fi
    note "staged $PHONE (bind :$PORT)"
    EVENT_CHANGED=1
  fi

  # ── 4. autorun env — written ONLY with a verified call time (its HHMM
  #      windows are real arithmetic, computed above; the TBDs that DO stay
  #      are TO_NUMBER/PASSCODE, which event-autorun fail-stops on) ──────────
  LANE_CFG="$PHONE"; [[ $PHONE_OK -eq 0 ]] && LANE_CFG="$WC"
  if [[ $HAVE_TIME -eq 0 ]]; then
    note "no verified call time — autorun env NOT written (NEEDS-TIME below)"
  else
    if ENV_STATE=$(python3 scripts/stage-reconcile.py --repo "$REPO" --run-root "$RUN_ROOT" \
        env --config "$LANE_CFG" --env "$ENVF" --event-date "$CDATE" \
        --fire-hhmm "$FIRE_HHMM" --end-hhmm "$END_HHMM" \
        --complete-hhmm "$CE_HHMM" --bind-port "$PORT" 2>&1); then
      ENV_RC=0
    else
      ENV_RC=$?
    fi
    if [[ $ENV_RC -eq 0 ]]; then
      note "autorun env $ENV_STATE: $ENVF"
    elif [[ $ENV_RC -ne 3 ]]; then
      report "ENV-STALE: $EV — $ENV_STATE; env NOT rewritten"
      ERRORS=$((ERRORS + 1)); continue
    elif [[ $DRY -eq 1 ]]; then
      note "DRY: would write $ENVF (CONFIG_PATH=$LANE_CFG BIND_PORT=$PORT FIRE_HHMM=$FIRE_HHMM TO_NUMBER=TBD)"
    else
      TICKS=""
      if ! TICKS=$(python3 - "$RAW" <<'PY'
import re, sys
s = open(sys.argv[1]).read()
m = re.search(r'allowlist_tickers = \[(.*?)\]', s, re.S)
ticks = re.findall(r'"([A-Z0-9-]+)"', m.group(1)) if m else []
print(" ".join(t.rsplit("-", 1)[-1] for t in ticks))
PY
      ); then
        report "ERROR: $EV — ticker extraction from $RAW failed; autorun env not written"
        ERRORS=$((ERRORS + 1)); continue
      fi
      if ! ENVTMP=$(mktemp "configs/.${LC}-${DATEPART}-autorun.env.XXXXXX"); then
        report "ERROR: $EV — could not allocate a same-directory temporary env for $ENVF"
        ERRORS=$((ERRORS + 1)); continue
      fi
      if ! {
        echo "# Per-event autorun parameters — $ISS ($EV). Staged by scripts/slate-stage.sh $(date -u +%F)."
        echo "# Sourced by scripts/event-autorun.sh via EARSHOT_EVENT_ENV (systemd timer on the"
        echo "# box / launchd plist on the laptop). ALL HHMM TIMES ARE BOX-LOCAL = UTC (us-east)."
        echo "# TO_NUMBER/PASSCODE are TBD ON PURPOSE: event-autorun.sh FAIL-STOPS on TBD, so"
        echo "# this env cannot place a call until the number hunt lands real digits here."
        if [[ $PHONE_OK -eq 0 ]]; then
          echo "# NO PHONE LANE (dial-ins admission=\"$ADM\"): CONFIG_PATH points at the WEBCAST"
          echo "# lane; TO_NUMBER stays TBD so the phone autorun path can never fire this env."
        fi
        echo "EVENT_NAME=\"$ISS\""
        echo "EVENT_TICKER=\"$EV\"          # informational"
        echo "CONFIG_PATH=\"$LANE_CFG\""
        echo "WEBCAST_CONFIG_PATH=\"$WC\"   # informational"
        echo "TO_NUMBER=\"TBD\"             # number hunt / dial-ins number_src"
        echo "PASSCODE=\"TBD\""
        echo "LABEL=\"com.earshot.${LC}-${DATEPART}-autorun\""
        echo "EVENT_DATE=\"$CDATE\""
        echo "END_HHMM_LIVE=$END_HHMM             # call + 150 min UTC$( [[ "$END_HHMM" == TBD ]] && echo ' — WINDOW WRAPS MIDNIGHT, set by hand')"
        echo "COMPLETE_EARLIEST_HHMM=$CE_HHMM     # call + 45 min UTC$( [[ "$CE_HHMM" == TBD ]] && echo ' — WINDOW WRAPS MIDNIGHT, set by hand')"
        echo "EARSHOT_ACCOUNT_PROFILE=\"$ACCOUNT_ENV_DEFAULT\""
        echo "ACCOUNT_ENV=\"$ACCOUNT_ENV_DEFAULT\""
        echo "BIND_PORT=$PORT"
        echo "FIRE_HHMM=$FIRE_HHMM                # timer fire = call − 25 min, zero-padded HHMM UTC"
        echo "                                    # (matches install-event-timer.sh --fire-hhmm;"
        echo "                                    #  preflight-nightly.sh timer check reads it)"
        echo "TICKERS=\"$TICKS\""
      } > "$ENVTMP"; then
        rm -f "$ENVTMP"
        report "ERROR: $EV — writing $ENVF failed; temporary env removed"
        ERRORS=$((ERRORS + 1)); continue
      fi
      if ! mv "$ENVTMP" "$ENVF"; then
        rm -f "$ENVTMP"
        report "ERROR: $EV — could not atomically install $ENVF"
        ERRORS=$((ERRORS + 1)); continue
      fi
      note "staged $ENVF"
      report "RECONCILED: $EV — created missing generic env $ENVF"
      EVENT_CHANGED=1
    fi
  fi
  if [[ -f "$PHONE" || -f "$ENVF" ]]; then
    report "PORT: $EV → 127.0.0.1:$PORT"
  else
    report "PORT: $EV → 127.0.0.1:$PORT (planned; nothing on disk claims it yet)"
  fi

  # ── 5. timer install line — PRINTED, NEVER EXECUTED. Only phone-lane events
  #      get the event-autorun line: on a webcast-only event that timer would
  #      just fail-stop on TO_NUMBER=TBD, so the human must not be invited to
  #      arm it (FIRE_HHMM/FIRE_DATE come from the guarded block above). ─────
  if [[ $HAVE_TIME -eq 1 && $PHONE_OK -eq 1 ]]; then
    report "TIMER-INSTALL: ssh $BOX 'cd ~/ear && scripts/install-event-timer.sh --event-env $ENVF --fire-hhmm $FIRE_HHMM --date $FIRE_DATE'"
  elif [[ $HAVE_TIME -eq 1 ]]; then
    report "WEBCAST-TIMER: $EV — pending webcast-autorun installer (do NOT install event-autorun: it fail-stops on TO_NUMBER=TBD)"
  else
    report "NEEDS-TIME: $EV — no verified call time; env NOT written, timer NOT installed"
    NEEDTIME=$((NEEDTIME + 1))
  fi
  if [[ $EVENT_CHANGED -eq 1 || $DRY -eq 1 ]]; then
    STAGED=$((STAGED + 1))
  elif [[ $HAVE_TIME -eq 1 ]]; then
    report "ALREADY-STAGED: $EV — config$( [[ -f "$PHONE" ]] && echo 's' ) + generic env are complete, time-consistent, and untouched"
    ALREADY=$((ALREADY + 1))
  fi
done < "$ELIG"

# ── summary — one grep-able line + one mail, always ─────────────────────────
SUMMARY="STAGED: $STAGED | NEEDS-TIME: $NEEDTIME | NO-PHONE-LANE: $NOPHONE | ERRORS: $ERRORS"
echo "SUMMARY: $SUMMARY (already-staged: $ALREADY, window: ${HORIZON}d, dry-run: $DRY)"
page "slate-stage: $SUMMARY" "$(printf 'slate-stage %s over %s (window %sd, dry-run %s)\n\n%s' \
  "$(date -u +%FT%TZ)" "$SLATE" "$HORIZON" "$DRY" "$REPORT")"
[[ $ERRORS -eq 0 ]] || exit 1
