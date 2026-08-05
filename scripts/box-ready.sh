#!/usr/bin/env bash
# Is the us-east-1 box ACTUALLY able to run this event, right now?
#
#   scripts/box-ready.sh [--config configs/<event>.toml] [--account ~/.earshot/<a>.env]
#                        [--repair] [--json <path>] [--quiet]
#
# WHY THIS EXISTS (2026-08-05, TLN). The highest-priority event of the day — the
# only one with a participant dial-in — ran on the CDMX laptop over ngrok and
# lost its one contested race by 534 ms. The box was NOT down: load 0.00, Caddy
# active, models present, TLS answering. The single blocker was that
# ~/ear/target/release/earshot was one day stale and predated the
# MENTION_FILL_CAP_CENTS 50->97 raise, so mention_fill_gate (src/kalshi/
# validator.rs:163) would have refused the config's 97c cap outright. Fixing
# that meant a Rust rebuild plus artifact migration inside 60 minutes, on the
# event that mattered most, unrehearsed — so the laptop won on execution
# certainty (scripts/tln-phone-golive.sh:78-85 records the ruling).
#
# THE FAILURE WAS THE TIMING OF DISCOVERY, NOT THE CHOICE. Every fact below is
# knowable at prep time in about twenty seconds. This script is that check, at
# the hour it was meant to run. From CDMX the phone lane pays media.twiliocdn
# p50 55.3 ms with 4.0% PACKET LOSS versus 2.9 ms / 0% from the box
# (state/latprobe-*.json), plus ngrok's p99 226 ms vs 20 ms (Q-TLS) — so a box
# that could have run is a race given away.
#
# READ-ONLY BY DEFAULT. --repair is the only mode that changes the box, and it
# REFUSES a dirty binary-affecting tree (AGENTS.md: "Deployment must identify an
# immutable commit or release artifact") so the pin records a commit rather than
# UNKNOWN-NOT-A-GIT-CHECKOUT.
#
# Exit 0 = GO. Exit 1 = NO-GO (one blocker line names the cause). Exit 2 = usage.
# NEVER pipe this through head/tail — that masks $? and manufactures green.
set -uo pipefail   # NOT -e: every check must reach the verdict, not abort it

REPO="${EARSHOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO" || exit 2

CONFIG="" ACCOUNT="" REPAIR=0 JSON_OUT="" QUIET=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)  CONFIG="$2"; shift 2 ;;
    --account) ACCOUNT="$2"; shift 2 ;;
    --repair)  REPAIR=1; shift ;;
    --json)    JSON_OUT="$2"; shift 2 ;;
    --quiet)   QUIET=1; shift ;;
    -h|--help) sed -n '2,32p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# BOX ADDRESS — same resolution order as scripts/deploy-box.sh (platform.deploy
# owns both): live from the instance id when the AWS CLI is available, else the
# pinned Elastic IP. Three addresses in three days before the EIP landed.
EARSHOT_INSTANCE_ID="${EARSHOT_INSTANCE_ID:-i-04c50d76572aa092e}"
if [[ -z "${EARSHOT_BOX:-}" ]] && command -v aws >/dev/null 2>&1; then
  _ip="$(aws ec2 describe-instances --instance-ids "$EARSHOT_INSTANCE_ID" \
           --query 'Reservations[].Instances[].PublicIpAddress' --output text 2>/dev/null || true)"
  [[ "$_ip" =~ ^[0-9]+(\.[0-9]+){3}$ ]] && EARSHOT_BOX="ubuntu@$_ip"
fi
BOX="${EARSHOT_BOX:-ubuntu@3.212.174.94}"
KEY="${EARSHOT_BOX_KEY:-$HOME/.ssh/earshot-useast1.pem}"
BOX_REPO="${EARSHOT_BOX_REPO:-~/ear}"
SSH=(ssh -o ConnectTimeout=15 -o BatchMode=yes -i "$KEY" "$BOX")

# ── Interpreter pin (2026-08-05, measured on the CDMX laptop) ───────────────
# Homebrew python3 3.14.6 has tomllib but a BROKEN plistlib; /usr/bin/python3
# 3.9.6 is the exact inverse and predates tomllib entirely. A registry write
# silently failed under launchd's minimal PATH because of this split, and the
# failure was invisible until the artifact was inspected (configs/dial-ins.toml
# [TLN] admission_detail). Never inherit "python3" — resolve one that can
# actually parse TOML, and say which.
PY=""
for cand in "${EARSHOT_PY_TOML:-}" python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  [[ -n "$cand" ]] || continue
  command -v "$cand" >/dev/null 2>&1 || continue
  if "$cand" -c 'import tomllib' 2>/dev/null; then PY="$(command -v "$cand")"; break; fi
done
[[ -n "$PY" ]] || { echo "NO-GO: no python3 with tomllib on PATH (need >=3.11) — set EARSHOT_PY_TOML" >&2; exit 1; }

FAILS=0 WARNS=0
LINES=()
check() {  # check <PASS|FAIL|WARN> <message>
  local v="$1"; shift
  case "$v" in
    FAIL) FAILS=$((FAILS+1)) ;;
    WARN) WARNS=$((WARNS+1)) ;;
  esac
  LINES+=("$v"$'\t'"$*")
  [[ "$QUIET" == 1 && "$v" == PASS ]] && return 0
  printf '[%s] %s\n' "$v" "$*"
}

# ── What does THIS config need? ────────────────────────────────────────────
EVENT_ID="" BIND_PORT="" ENGINE="" MODEL_DIR="" CAP_CENTS="" BUDGET="" ORDER_NOTIONAL="" HAS_TWILIO=0
if [[ -n "$CONFIG" ]]; then
  if [[ ! -f "$CONFIG" ]]; then
    echo "NO-GO: config not found: $CONFIG" >&2; exit 1
  fi
  CFG_FACTS=$(CONFIG="$CONFIG" "$PY" - <<'PY'
import os, sys, tomllib
try:
    cfg = tomllib.load(open(os.environ["CONFIG"], "rb"))
except Exception as e:
    print(f"ERR\t{e}"); sys.exit(0)
ev = cfg.get("event", {})
det = cfg.get("detector", {})
engine = det.get("engine", "stub")
model_dir = (det.get(engine) or {}).get("model_dir", "")
binds = [s.get("bind", "") for s in cfg.get("sources", []) if s.get("kind") == "twilio_stream"]
port = binds[0].rsplit(":", 1)[-1] if binds else ""
kws = cfg.get("keywords", [])
cap = max((k.get("max_price_cents", 0) for k in kws), default=0)
# budget of the keyword carrying the max cap — that pair is what check_order sees
budget = max((k.get("budget_contracts", 0) for k in kws if k.get("max_price_cents", 0) == cap), default=0)
risk = cfg.get("risk", {})
print("OK")
print(f"EVENT_ID\t{ev.get('id','')}")
print(f"BIND_PORT\t{port}")
print(f"ENGINE\t{engine}")
print(f"MODEL_DIR\t{model_dir}")
print(f"CAP_CENTS\t{cap}")
print(f"BUDGET\t{budget}")
print(f"ORDER_NOTIONAL\t{risk.get('max_order_notional_cents', 100)}")
print(f"HAS_TWILIO\t{1 if binds else 0}")
PY
  )
  if [[ "$CFG_FACTS" == ERR* ]]; then
    echo "NO-GO: cannot parse $CONFIG: ${CFG_FACTS#ERR	}" >&2; exit 1
  fi
  while IFS=$'\t' read -r k v; do
    case "$k" in
      EVENT_ID) EVENT_ID="$v" ;; BIND_PORT) BIND_PORT="$v" ;; ENGINE) ENGINE="$v" ;;
      MODEL_DIR) MODEL_DIR="$v" ;; CAP_CENTS) CAP_CENTS="$v" ;; BUDGET) BUDGET="$v" ;;
      ORDER_NOTIONAL) ORDER_NOTIONAL="$v" ;; HAS_TWILIO) HAS_TWILIO="$v" ;;
    esac
  done <<<"$CFG_FACTS"
  check PASS "config $CONFIG → event $EVENT_ID (engine=$ENGINE, bind port=${BIND_PORT:-none}, cap=${CAP_CENTS}c x${BUDGET})"
  if [[ "$HAS_TWILIO" != 1 ]]; then
    check WARN "config has NO twilio_stream source — this is not a phone lane; box-ready still checks the host, but scripts/phone-event-prep refuses webcast-only events"
  fi
fi

# ── Laptop-side facts (the intended source of truth) ────────────────────────
LAPTOP_CAP=$(sed -n 's/^pub const MENTION_FILL_CAP_CENTS: u64 = \([0-9]*\);.*/\1/p' src/risk.rs | head -1)
[[ -n "$LAPTOP_CAP" ]] || check FAIL "cannot read MENTION_FILL_CAP_CENTS from src/risk.rs — the cap contract is unreadable"

# Binary-affecting dirt. This is the AGENTS.md rule made executable: a rsync from
# a dirty tree produces a box binary that no commit describes.
DIRTY_BUILD=$(git status --porcelain -- src Cargo.toml Cargo.lock 2>/dev/null | sed 's/^/      /')
[[ -n "$DIRTY_BUILD" ]] && DIRTY_BUILD="$DIRTY_BUILD"$'\n'
HEAD_SHA=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)

# ── One remote round-trip: collect everything, decide locally ───────────────
REMOTE=$("${SSH[@]}" "BOX_REPO=$BOX_REPO bash -s" <<'REMOTE_EOF' 2>/dev/null
set -uo pipefail
R="${BOX_REPO/#\~/$HOME}"
say() { printf '%s\t%s\n' "$1" "${2-}"; }

say HOSTNAME "$(hostname)"
say NPROC "$(nproc 2>/dev/null)"
say LOAD1 "$(cut -d' ' -f1 /proc/loadavg 2>/dev/null)"
say NOW_EPOCH "$(date -u +%s)"
say REPO_PRESENT "$([[ -d "$R" ]] && echo 1 || echo 0)"
say IS_GIT "$([[ -d "$R/.git" ]] && echo 1 || echo 0)"
say DISK_KB "$(df -kP "$HOME" | awk 'NR==2{print $4}')"

BIN="$R/target/release/earshot"
if [[ -x "$BIN" ]]; then
  say BIN_MTIME "$(date -u -r "$BIN" +%Y-%m-%dT%H:%M:%SZ)"
  say BIN_SHA "$(sha256sum "$BIN" | cut -c1-16)"
  # Sources newer than the binary = the binary does not contain them.
  NEWER=$(find "$R/src" "$R/Cargo.toml" "$R/Cargo.lock" -newer "$BIN" 2>/dev/null | head -5 | tr '\n' ' ')
  say BIN_STALE_PATHS "$NEWER"
else
  say BIN_MTIME ""; say BIN_SHA ""; say BIN_STALE_PATHS "NO-BINARY"
fi

say BOX_CAP "$(sed -n 's/^pub const MENTION_FILL_CAP_CENTS: u64 = \([0-9]*\);.*/\1/p' "$R/src/risk.rs" 2>/dev/null | head -1)"

say ORT_SO "$(find "$R/vendor" -name 'libonnxruntime*.so*' -not -path '*dSYM*' 2>/dev/null | head -1)"
say ORT_DYLIB "$(find "$R/vendor" -name '*.dylib' 2>/dev/null | head -1)"
say MODELS "$(ls "$R/models" 2>/dev/null | tr '\n' ',')"

say CADDY_ACTIVE "$(systemctl is-active caddy 2>/dev/null)"
say CADDY_ROUTES "$(grep -o '/e/[0-9]\{4\}' /etc/caddy/Caddyfile 2>/dev/null | sort -u | tr '\n' ' ')"
DOM=$(sed -n 's/^Environment=EARSHOT_DOMAIN=//p' /etc/systemd/system/caddy.service.d/earshot.conf 2>/dev/null | head -1)
say CADDY_DOMAIN "$DOM"
if [[ -n "$DOM" ]]; then
  END=$(echo | openssl s_client -servername "media.$DOM" -connect "media.$DOM:443" 2>/dev/null \
        | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
  say TLS_ENDDATE "$END"
  [[ -n "$END" ]] && say TLS_DAYS "$(( ( $(date -u -d "$END" +%s 2>/dev/null || echo 0) - $(date -u +%s) ) / 86400 ))"
fi

for f in "$HOME/.kalshi/earshot_env" "$HOME/.kalshi/mail_env"; do
  say "CRED_$(basename "$f")" "$([[ -f "$f" ]] && stat -c '%a' "$f" || echo MISSING)"
done
say ACCOUNTS "$(ls "$HOME/.earshot"/*.env 2>/dev/null | xargs -n1 basename 2>/dev/null | tr '\n' ',')"
say NUMBERS_ENV "$([[ -f "$R/state/hunt/numbers.env" ]] && stat -c '%a' "$R/state/hunt/numbers.env" || echo MISSING)"

say NTP_SYNC "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)"
say NTP_ACTIVE "$(timedatectl show -p NTP --value 2>/dev/null)"
say LINGER "$(loginctl show-user "$USER" 2>/dev/null | sed -n 's/^Linger=//p')"
say TIMERS "$(systemctl --user list-timers --all --no-legend 2>/dev/null | awk '{print $NF}' | grep -c . )"
say EARSHOT_TIMERS "$(systemctl --user list-timers --all --no-legend 2>/dev/null | grep -o 'com\.earshot\.[a-z0-9.-]*\.timer' | sort -u | tr '\n' ' ')"

# The scheduler's environment, not the login shell's. A fire-time compile is
# impossible here and a python without tomllib writes nothing (both measured).
UNIT_PROBE=$(systemd-run --user --wait --pipe --quiet /bin/sh -c '
  printf "PATH=%s\n" "$PATH"
  command -v cargo >/dev/null 2>&1 && echo CARGO=yes || echo CARGO=no
  python3 -c "import tomllib" 2>/dev/null && echo TOMLLIB=yes || echo TOMLLIB=no
  command -v shasum >/dev/null 2>&1 && echo SHASUM=yes || echo SHASUM=no
  command -v timeout >/dev/null 2>&1 && echo TIMEOUT=yes || echo TIMEOUT=no
' 2>/dev/null | tr '\n' ';')
say UNIT_ENV "$UNIT_PROBE"
REMOTE_EOF
)
RC=$?

if [[ $RC -ne 0 || -z "$REMOTE" ]]; then
  check FAIL "box UNREACHABLE over ssh ($BOX, key $KEY) — nothing below could be checked. Verify the instance is running and the EIP still points at it: aws ec2 describe-instances --instance-ids $EARSHOT_INSTANCE_ID"
  echo
  echo "NO-GO: box unreachable"
  exit 1
fi

# Read one KEY from the remote block. substr(), NOT `$1=""` — assigning to a
# field makes awk rebuild the record with OFS (a SPACE), which silently prefixes
# every value and makes every string comparison below false.
r() { awk -F'\t' -v k="$1" '$1==k{ print substr($0, index($0,"\t")+1); exit }' <<<"$REMOTE"; }

# ── Host ───────────────────────────────────────────────────────────────────
NPROC=$(r NPROC); LOAD1=$(r LOAD1)
if [[ -n "$NPROC" && -n "$LOAD1" ]]; then
  if awk -v l="$LOAD1" -v n="$NPROC" 'BEGIN{exit !(l > 2*n)}'; then
    check FAIL "box saturated: load1 $LOAD1 on $NPROC cores — whatever is running must finish before fire time"
  elif awk -v l="$LOAD1" -v n="$NPROC" 'BEGIN{exit !(l > 0.75*n)}'; then
    check WARN "box load1 $LOAD1 on $NPROC cores — something heavy is running"
  else
    check PASS "box $(r HOSTNAME): load1 $LOAD1 on $NPROC cores"
  fi
fi

[[ "$(r REPO_PRESENT)" == 1 ]] || check FAIL "no $BOX_REPO on the box — run scripts/deploy-box.sh"
if [[ "$(r IS_GIT)" == 1 ]]; then
  check WARN "$BOX_REPO IS a git checkout — this repo deploys by rsync (scripts/deploy-box.sh); two provenance paths is how a stale tree hides"
else
  check PASS "$BOX_REPO is the rsync-deployed tree (no .git) — provenance is the laptop commit"
fi

DISK_KB=$(r DISK_KB)
DISK_GB=$(( DISK_KB / 1024 / 1024 ))
DISK_H=$(awk -v k="$DISK_KB" 'BEGIN{printf "%.1f", k/1048576}')
# 5 GiB matches preflight's own gate. --repair asks for 6: target/release is
# already warm (~1.6 GiB of cached deps), so the common case is one crate
# recompiled and relinked, not a cold build.
NEED_GB=5; [[ "$REPAIR" == 1 ]] && NEED_GB=6
if (( DISK_GB < NEED_GB )); then
  check FAIL "disk: ${DISK_H} GiB free (< ${NEED_GB} needed$([[ "$REPAIR" == 1 ]] && echo ' for a rebuild')) — reclaim on the box: stale ~/*-capture dirs and ~/icefall are the usual candidates. NEVER delete target/release (a cold rebuild is the slow path you are trying to avoid)."
elif (( DISK_GB < 8 )); then
  check WARN "disk: ${DISK_H} GiB free — enough to arm, tight for a rebuild (preflight's own gate is 5 GiB)"
else
  check PASS "disk: ${DISK_H} GiB free"
fi

# ── Clock ──────────────────────────────────────────────────────────────────
SKEW=$(( $(date -u +%s) - $(r NOW_EPOCH) )); SKEW=${SKEW#-}
if [[ "$(r NTP_SYNC)" == yes && "$(r NTP_ACTIVE)" == yes ]]; then
  check PASS "clock: NTP active + synchronized, ${SKEW}s skew vs this host"
else
  check FAIL "clock: NTPSynchronized=$(r NTP_SYNC) NTP=$(r NTP_ACTIVE) — the timer fires on box-local time (=UTC here); an unsynced clock fires at the wrong minute"
fi
(( SKEW > 120 )) && check WARN "clock skew ${SKEW}s between this host and the box — fire-time arithmetic done here may land elsewhere"

# ── Source sync (is the box the tree we think it is?) ──────────────────────
# deploy-box.sh --check-only is a plain `rsync --dry-run -v`, so it also lists
# churn that cannot change a run (report.json, .claude/ session locks, docs
# prose). Only the paths that feed the binary, the launchers, or the arm decide
# GO — everything else is reported but does not block.
PENDING_ALL=$(scripts/deploy-box.sh --check-only 2>/dev/null \
  | grep -vE '^(sending|receiving) incremental|^Transfer starting|^sent |^total size|^$|^\./$|^deploy OK$|/$')
PENDING_RUN=$(printf '%s\n' "$PENDING_ALL" \
  | grep -E '^(src/|scripts/|configs/|deploy/|tests/|Cargo\.(toml|lock)$)' || true)
N_ALL=$(printf '%s' "$PENDING_ALL" | grep -c . )
N_RUN=$(printf '%s' "$PENDING_RUN" | grep -c . )
if (( N_RUN > 0 )); then
  check FAIL "source OUT OF SYNC: $N_RUN run-affecting path(s) differ — the box is not running this tree:
$(printf '%s\n' "$PENDING_RUN" | head -8 | sed 's/^/      /')
      Fix: scripts/box-ready.sh --repair (commits first), or scripts/deploy-box.sh"
elif (( N_ALL > 0 )); then
  check WARN "source in sync for src/scripts/configs/deploy/tests; $N_ALL non-run path(s) differ (docs, report.json, session locks) — harmless for this event"
else
  check PASS "source in sync with this tree (deploy-box.sh --check-only: nothing pending)"
fi

# ── Binary provenance — THE 2026-08-05 BLOCKER ─────────────────────────────
STALE=$(r BIN_STALE_PATHS)
if [[ "$STALE" == "NO-BINARY" ]]; then
  check FAIL "no $BOX_REPO/target/release/earshot — nothing to pin. Fix: scripts/box-ready.sh --repair"
elif [[ -n "$STALE" ]]; then
  check FAIL "box binary is STALE: built $(r BIN_MTIME), but these are newer — $STALE
      The binary does not contain them. This is the exact 2026-08-05 TLN blocker. Fix: scripts/box-ready.sh --repair"
else
  check PASS "box binary current: built $(r BIN_MTIME), sha256 $(r BIN_SHA)…, newer than every source"
fi

BOX_CAP=$(r BOX_CAP)
if [[ -z "$BOX_CAP" ]]; then
  check FAIL "cannot read MENTION_FILL_CAP_CENTS from the box's src/risk.rs"
elif [[ "$BOX_CAP" != "$LAPTOP_CAP" ]]; then
  check FAIL "MENTION_FILL_CAP_CENTS DISAGREES: box ${BOX_CAP}c vs this tree ${LAPTOP_CAP}c — mention_fill_gate refuses any cap above the box's value (src/kalshi/validator.rs:163). Fix: scripts/box-ready.sh --repair"
else
  check PASS "MENTION_FILL_CAP_CENTS agrees: ${BOX_CAP}c on both"
fi

# The cap arithmetic the config must satisfy, derived from code, never memory.
if [[ -n "$CAP_CENTS" && "$CAP_CENTS" != 0 ]]; then
  if [[ -n "$BOX_CAP" ]] && (( CAP_CENTS > BOX_CAP )); then
    check FAIL "config cap ${CAP_CENTS}c > box ceiling ${BOX_CAP}c — mention_fill_gate refuses before a single order is computed"
  else
    check PASS "config cap ${CAP_CENTS}c <= ceiling ${BOX_CAP}c"
  fi
  NOTIONAL=$(( CAP_CENTS * BUDGET ))
  if (( NOTIONAL > ORDER_NOTIONAL )); then
    check FAIL "Refusal::OrderNotional by construction: ${CAP_CENTS}c x ${BUDGET} = ${NOTIONAL}c > [risk].max_order_notional_cents=${ORDER_NOTIONAL}c (src/risk.rs check_order) — EVERY order would refuse"
  else
    check PASS "order notional ${CAP_CENTS}c x ${BUDGET} = ${NOTIONAL}c <= ${ORDER_NOTIONAL}c cap"
  fi
fi

# ── Detector runtime ───────────────────────────────────────────────────────
ORT_SO=$(r ORT_SO); ORT_DYLIB=$(r ORT_DYLIB)
if [[ -n "$ORT_DYLIB" ]]; then
  check FAIL "macOS dylib on the Linux box: $ORT_DYLIB — vendor/ is platform-specific and must never cross machines (it caused the Ford 9-min deaf gap twice). Remove it."
elif [[ -z "$ORT_SO" ]]; then
  check FAIL "no linux libonnxruntime under $BOX_REPO/vendor — the detector dlopen futex-HANGS silently (a missing lib and a wrong-platform lib hang identically). Fix: scripts/fetch-models.sh on the box"
else
  check PASS "ORT_DYLIB_PATH resolves on the box: $ORT_SO"
fi

if [[ -n "$MODEL_DIR" ]]; then
  MODELS=$(r MODELS)
  if [[ ",$MODELS" == *",$(basename "$MODEL_DIR"),"* ]]; then
    check PASS "model present for engine=$ENGINE: $(basename "$MODEL_DIR")"
  else
    check FAIL "model MISSING on the box for engine=$ENGINE: $MODEL_DIR (models/ is rsync-excluded and host-local). Fix: scripts/fetch-models.sh on the box"
  fi
fi

# ── TLS ingress ────────────────────────────────────────────────────────────
DOMAIN=$(r CADDY_DOMAIN)
[[ "$(r CADDY_ACTIVE)" == active ]] \
  && check PASS "caddy active (domain media.$DOMAIN)" \
  || check FAIL "caddy is $(r CADDY_ACTIVE) — Twilio cannot reach this box's media endpoint. Fix: sudo systemctl restart caddy"

TLS_DAYS=$(r TLS_DAYS)
if [[ -z "$TLS_DAYS" ]]; then
  check FAIL "TLS handshake to media.$DOMAIN:443 failed — check the A record still points at this box and :443 is open"
elif (( TLS_DAYS < 10 )); then
  check FAIL "TLS cert for media.$DOMAIN expires in ${TLS_DAYS}d — Let's Encrypt renews at 30d, so under 10 means renewal is already failing"
else
  check PASS "TLS cert for media.$DOMAIN valid ${TLS_DAYS}d ($(r TLS_ENDDATE))"
fi

# THE SECOND 2026-08-05 BLOCKER, found only in hindsight: the Caddyfile routes a
# FIXED set of ports, and TLN's phone config bound 8093. Even with a fresh
# binary that arm's wss://…/e/8093 would have hit the `respond 404` catch-all.
ROUTES=$(r CADDY_ROUTES)
if [[ -n "$BIND_PORT" ]]; then
  if [[ " $ROUTES " == *" /e/$BIND_PORT "* ]]; then
    check PASS "TLS ingress routes /e/$BIND_PORT → 127.0.0.1:$BIND_PORT"
    check PASS "stream URL for this event: wss://media.$DOMAIN/e/$BIND_PORT"
  else
    check FAIL "TLS ingress does NOT route /e/$BIND_PORT — Twilio's handshake would hit Caddy's 404 catch-all and no media would ever arrive. Routed today: ${ROUTES:-none}. Fix: rebind the config to a routed port, or add the route to deploy/caddy/Caddyfile and re-run scripts/install-tls-ingress.sh"
  fi
else
  check WARN "no --config: cannot check that this event's bind port is routed. Routed ports: ${ROUTES:-none}"
fi

# ── Credentials ────────────────────────────────────────────────────────────
for f in earshot_env mail_env; do
  p=$(r "CRED_$f")
  case "$p" in
    MISSING) check FAIL "credential MISSING on the box: ~/.kalshi/$f" ;;
    600|400) check PASS "credential ~/.kalshi/$f present ($p)" ;;
    *)       check WARN "credential ~/.kalshi/$f present but mode $p — tighten to 600" ;;
  esac
done
if [[ -n "$ACCOUNT" ]]; then
  AB=$(basename "$ACCOUNT")
  if [[ ",$(r ACCOUNTS)" == *",$AB,"* ]]; then
    check PASS "account env present on the box: ~/.earshot/$AB"
  else
    check FAIL "account env MISSING on the box: ~/.earshot/$AB (have: $(r ACCOUNTS)) — it holds this event's Kalshi + Twilio secrets and is never deployed"
  fi
fi
# state/ is rsync-EXCLUDED, so digits never arrive by deploy. This is a WARN,
# not a FAIL: scripts/box-event-install.sh pushes them at schedule time.
[[ "$(r NUMBERS_ENV)" == MISSING ]] \
  && check WARN "no $BOX_REPO/state/hunt/numbers.env on the box (state/ is rsync-excluded) — scripts/box-event-install.sh pushes the digits over stdin at schedule time" \
  || check PASS "state/hunt/numbers.env present on the box (mode $(r NUMBERS_ENV))"

# ── Scheduler ──────────────────────────────────────────────────────────────
[[ "$(r LINGER)" == yes ]] \
  && check PASS "systemd user lingering ON — timers fire without a login session" \
  || check FAIL "lingering is OFF — user timers die with the ssh session and never fire unattended. Fix: sudo loginctl enable-linger ubuntu"

UNIT_ENV=$(r UNIT_ENV)
case "$UNIT_ENV" in
  *CARGO=no*)  check PASS "scheduler env has NO cargo (as expected) — a fire-time compile is impossible; the pinned binary is the only unattended-safe path" ;;
  *CARGO=yes*) check WARN "scheduler env HAS cargo — do not rely on it; launchers still auto-select state/<E>/earshot-pinned" ;;
  *)           check WARN "could not probe the systemd user environment" ;;
esac
case "$UNIT_ENV" in
  *TOMLLIB=yes*) check PASS "scheduler python3 has tomllib — registry/config writes will not silently fail under the minimal PATH" ;;
  *TOMLLIB=no*)  check FAIL "scheduler python3 CANNOT import tomllib — this is what made registry-admit --promote fail silently on the 2026-08-05 TLN probe. Pin an interpreter explicitly in the unit." ;;
esac

STALE_TIMERS=$(r EARSHOT_TIMERS)
[[ -n "$STALE_TIMERS" ]] && check WARN "old earshot timers still loaded: $STALE_TIMERS — disarm what is finished: systemctl --user disable --now <name>"

# ── Repair ─────────────────────────────────────────────────────────────────
if [[ "$REPAIR" == 1 ]]; then
  echo
  if [[ -n "$DIRTY_BUILD" ]]; then
    # AGENTS.md: deployment must identify an immutable commit. A dirty rsync
    # produces a box binary no commit describes, and preflight can only record
    # commit=UNKNOWN-NOT-A-GIT-CHECKOUT for it.
    check FAIL "--repair REFUSED: binary-affecting paths are uncommitted, so the deployed binary would match no commit:
$DIRTY_BUILD      Commit them (or stash), then re-run. HEAD is $HEAD_SHA."
  else
    echo "repairing: deploy $HEAD_SHA → $BOX, then release build on the box"
    if scripts/deploy-box.sh; then
      check PASS "deployed $HEAD_SHA to $BOX"
      if "${SSH[@]}" "cd ${BOX_REPO} && source ~/.cargo/env && cargo build --release" ; then
        NEWBIN=$("${SSH[@]}" "sha256sum ${BOX_REPO}/target/release/earshot | cut -c1-16" 2>/dev/null)
        check PASS "box release build OK — binary sha256 ${NEWBIN}…  (re-run without --repair to confirm GO)"
      else
        check FAIL "box release build FAILED — see the output above"
      fi
    else
      check FAIL "deploy-box.sh FAILED — box not updated"
    fi
  fi
fi

# ── Verdict + artifact ─────────────────────────────────────────────────────
if [[ -z "$JSON_OUT" ]]; then
  if [[ -n "$EVENT_ID" ]]; then JSON_OUT="state/${EVENT_ID}/box-ready.json"
  else JSON_OUT="state/box-ready-$(date -u +%Y%m%dT%H%M%SZ).json"; fi
fi
mkdir -p "$(dirname "$JSON_OUT")" 2>/dev/null
# Gate lines go through a FILE, not a pipe: the python below is itself fed by a
# heredoc, so anything piped to it is discarded (this silently produced an
# artifact with "gates": [] on the first run).
GATES_TMP=$(mktemp "${TMPDIR:-/tmp}/box-ready-gates.XXXXXX")
trap 'rm -f "$GATES_TMP"' EXIT
printf '%s\n' "${LINES[@]}" > "$GATES_TMP"
GATES_TMP="$GATES_TMP" JSON_OUT="$JSON_OUT" FAILS="$FAILS" WARNS="$WARNS" \
  BOX="$BOX" EVENT_ID="$EVENT_ID" CONFIG="$CONFIG" HEAD_SHA="$HEAD_SHA" \
  BIN_SHA="$(r BIN_SHA)" BIN_MTIME="$(r BIN_MTIME)" BOX_CAP="$BOX_CAP" \
  STREAM_URL="${BIND_PORT:+wss://media.$DOMAIN/e/$BIND_PORT}" "$PY" - <<'PY'
import json, os, datetime
gates = []
for line in open(os.environ["GATES_TMP"]).read().splitlines():
    if not line.strip():
        continue
    v, _, m = line.partition("\t")
    gates.append({"verdict": v, "msg": m})
fails = int(os.environ["FAILS"])
out = {
    "schema": 1,
    "generated_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "box": os.environ["BOX"],
    "event_id": os.environ["EVENT_ID"] or None,
    "config": os.environ["CONFIG"] or None,
    "laptop_head": os.environ["HEAD_SHA"],
    "box_binary_sha256_prefix": os.environ["BIN_SHA"] or None,
    "box_binary_built": os.environ["BIN_MTIME"] or None,
    "box_mention_fill_cap_cents": int(os.environ["BOX_CAP"]) if os.environ["BOX_CAP"].isdigit() else None,
    "stream_url": os.environ["STREAM_URL"] or None,
    "go": fails == 0,
    "fails": fails,
    "warns": int(os.environ["WARNS"]),
    "gates": gates,
}
tmp = os.environ["JSON_OUT"] + ".tmp"
with open(tmp, "w") as f:
    json.dump(out, f, indent=1)
os.replace(tmp, os.environ["JSON_OUT"])
PY

echo
if (( FAILS == 0 )); then
  echo "GO: box $BOX ready${EVENT_ID:+ for $EVENT_ID}${BIND_PORT:+ on wss://media.$DOMAIN/e/$BIND_PORT} ($WARNS warn) → $JSON_OUT"
  exit 0
fi
BLOCKER=$(printf '%s\n' "${LINES[@]}" | awk -F'\t' '$1=="FAIL"{print $2; exit}')
echo "NO-GO ($FAILS blocker(s), $WARNS warn) → $JSON_OUT"
echo "first blocker: $BLOCKER"
exit 1
