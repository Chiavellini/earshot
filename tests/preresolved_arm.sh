#!/usr/bin/env bash
# Offline contract test for the preresolved resolver leg: a laptop-minted URL
# pushed to the executing host, and arm-on-url driving scripts/urlfile-wait.sh
# through it. Every OS, media, network and runtime boundary is a local stub —
# no ssh, no network, no dial, no runtime binary reaches the host.
#
# It exists because the box-webcast migration puts a WAIT where every other
# venue class has a resolver that mints. The failure this rig is built around is
# AMAT 2026-08-13: two attempts of one event, 11 minutes apart, sharing one
# $HOME/amat-stream.url and one 20:45:00Z deadline. `prior attempt is refused`
# is therefore the first case in the file, and it is the case that fails against
# any freshness rule made of timestamps.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/preresolved-arm.XXXXXX")
trap 'rm -rf -- "$TMP"' EXIT
REPO="$TMP/repo"
STUB="$TMP/stub"
mkdir -p "$REPO/scripts" "$REPO/configs" "$STUB/bin"
export HOME="$TMP/home"
mkdir -p "$HOME/.earshot"
printf 'EARSHOT_ACCOUNT_FIXTURE=1\n' > "$HOME/.earshot/account.env"

for path in eventctl.py access-plan.py capture-ready.py pcm-gate.sh \
  arm-on-url.sh push-url-file.sh install-t0-agents.sh; do
  cp "$ROOT/scripts/$path" "$REPO/scripts/$path"
done

# The resolver the plan binds is a thin wrapper over the REAL urlfile-wait.sh.
# It keeps the basename the AccessPlan pins while recording what the resolver
# child could actually see of arm-on-url's environment — the R5 question.
cat > "$REPO/scripts/urlfile-wait.sh" <<SH
#!/usr/bin/env bash
printf 'RESOLVER_DEADLINE_EPOCH=%s\n' "\${RESOLVER_DEADLINE_EPOCH:-<unset>}" >> "$STUB/wait-env"
exec "$ROOT/scripts/urlfile-wait.sh" "\$@"
SH

# Stubs. notify.sh APPENDS rather than exiting 0: "exits 3 with the page fired"
# is an acceptance bullet, and a page that is discarded cannot be asserted.
cat > "$REPO/scripts/notify.sh" <<SH
#!/usr/bin/env bash
printf '%s\n' "\$1" >> "$STUB/pages"
exit 0
SH
cat > "$REPO/scripts/launch-webcast.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
run="" live=0 fillable=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-dir) run=${2:?}; shift 2 ;;
    --i-am-live) live=1; shift ;;
    --fillable) fillable=1; shift ;;
    --config|--account|--bin) [[ -e "${2:?}" ]]; shift 2 ;;
    *) echo "unexpected launch argument: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$run" && "$live" == 1 && "$fillable" == 1 ]]
EARSHOT_REPO="$EARSHOT_REPO" python3 "$EARSHOT_REPO/scripts/eventctl.py" \
  record --run-dir "$run" --transition launched --actor preresolved-arm-test \
  --detail "one typed fire-time launch" >/dev/null
printf 'launch\n' >> "$LAUNCH_COUNT"
SH
printf '#!/usr/bin/env bash\nhead -c 192000 /dev/zero\n' > "$STUB/bin/ffmpeg"
# The playlist poller and the CDN fetch are both stubbed: this rig makes no
# outbound request, unlike a gate that merely points at an .invalid host.
printf 'print(\x27{"media_sequence": 101}\x27)\nprint(\x27{"media_sequence": 102}\x27)\n' \
  > "$STUB/pdt-stub.py"
printf '#!/usr/bin/env bash\nexit 0\n' > "$STUB/bin/curl"
# The ssh the laptop push talks to: records argv, then performs the remote
# command for real against this rig's HOME, so atomicity and the sidecar are
# exercised rather than asserted.
cat > "$STUB/bin/ssh" <<SH
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$STUB/ssh-argv"
CMD="\${@: -1}"
/bin/sh -c "\$CMD"
SH
chmod +x "$REPO/scripts/"* "$STUB/bin/"*

START=$(python3 - <<'PY'
import datetime as dt
print((dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ"))
PY
)
HOST_ID=earshot-test-box
FP="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
eventctl() { EARSHOT_REPO="$REPO" python3 "$REPO/scripts/eventctl.py" "$@" >/dev/null; }

# make_run <slug> <url-basename>  -> prints the run dir
make_run() {
  local slug="$1" urlbase="$2" event="KXEARNINGSMENTIONPRE${1}-TEST-WEBCAST"
  cat > "$REPO/configs/$slug.toml" <<EOF
[event]
id = "$event"
starts_at = "$START"
[[sources]]
kind = "pipe"
label = "primary"
cmd = ["/bin/sh", "-c", "streamlink --stdout \"\$(cat \$HOME/$urlbase)\" best | ffmpeg -i - -f s16le -"]
[[keywords]]
market_ticker = "KXEARNINGSMENTIONPRE${1}-TEST-FOO"
phrase = "foo"
EOF
  cat > "$REPO/configs/$slug.env" <<EOF
CONFIG_PATH="configs/$slug.toml"
ACCOUNT_ENV="\$HOME/.earshot/account.env"
EOF
  eventctl init --config "configs/$slug.toml" --event-env "configs/$slug.env" \
    --host-id "$HOST_ID" --account-fingerprint "$FP"
  local run
  run=$(find "$REPO/var/runs/$event" -mindepth 1 -maxdepth 1 -type d -print -quit)
  local transition
  for transition in prepared access_acquired study_completed preflight_passed; do
    eventctl record --run-dir "$run" --transition "$transition"
  done
  mkdir -p "$REPO/state/$event"
  printf 'fixture binary\n' > "$REPO/state/$event/earshot-pinned"
  chmod +x "$REPO/state/$event/earshot-pinned"
  printf '%s\n' "$run"
}

# bind_preresolved <run-dir> <url-basename> [deadline-utc]
bind_preresolved() {
  local run="$1" urlbase="$2" deadline="${3:-}"
  local extra=()
  [[ -n "$deadline" ]] && extra=(--deadline-utc "$deadline")
  EARSHOT_REPO="$REPO" python3 "$REPO/scripts/access-plan.py" bind \
    --run-dir "$run" --access-mode public --vantage "us-east1/rig" \
    --resolver-kind preresolved \
    --resolver-argv-json "[\"scripts/urlfile-wait.sh\",\"\$HOME/$urlbase\",\"--poll-secs\",\"1\"]" \
    --preresolve-kind browser-network \
    --preresolve-argv-json "[\"python3\",\"scripts/browser-resolve.py\",\"--out\",\"\$HOME/$urlbase\"]" \
    ${extra[@]+"${extra[@]}"} >/dev/null
  eventctl record --run-dir "$run" --transition scheduled
}

plan_sha() { python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["plan_sha256"])' "$1/control/run-plan.json"; }

arm() {  # arm <run-dir>  -> echoes the exit code, never aborts the rig
  local run="$1" rc=0
  EARSHOT_REPO="$REPO" EARSHOT_EXECUTION_HOST="$HOST_ID" \
  EARSHOT_RUN_PLAN_SHA256="$(plan_sha "$run")" \
  EARSHOT_ACCOUNT_ENV="$HOME/.earshot/account.env" \
  EARSHOT_FFMPEG="$STUB/bin/ffmpeg" EARSHOT_PDT_LOG="$STUB/pdt-stub.py" \
  EARSHOT_CURL="$STUB/bin/curl" PCM_GATE_SEQ_SECS=1 \
  EARSHOT_RESOLVER_RETRY_SECS=1 LAUNCH_COUNT="$STUB/launch-count" \
    "$REPO/scripts/arm-on-url.sh" --run-dir "$run" --i-am-live --fillable \
      --secs 6 >/dev/null 2>&1 || rc=$?
  printf '%s\n' "$rc"
}

launches() { [[ -f "$STUB/launch-count" ]] && wc -l < "$STUB/launch-count" | tr -d ' ' || echo 0; }
soon() { python3 -c "
import datetime as dt,sys
print((dt.datetime.now(dt.timezone.utc)+dt.timedelta(seconds=int(sys.argv[1]))).strftime('%Y-%m-%dT%H:%M:%SZ'))" "$1"; }

fail() { echo "FAIL: $*" >&2; exit 1; }

# ── Case 1: the laptop pushes, the box arms ───────────────────────────────────
# The push is the real scripts/push-url-file.sh over a stub ssh, so this also
# pins that the pusher and the waiter agree on the sidecar format.
RUN1=$(make_run 1 pre1-stream.url)
bind_preresolved "$RUN1" pre1-stream.url "$(soon 90)"
printf 'https://media.example.invalid/live/master.m3u8\n' > "$TMP/minted.url"
EARSHOT_REPO="$REPO" EARSHOT_SSH="$STUB/bin/ssh" \
  "$REPO/scripts/push-url-file.sh" --run-dir "$RUN1" --from-file "$TMP/minted.url" \
  --host stub-box --key "$TMP/no-key" > "$TMP/push1.log" 2>&1 \
  || fail "push-url-file refused a valid preresolved plan: $(tail -1 "$TMP/push1.log")"
[[ -f "$HOME/pre1-stream.url" ]] || fail "push did not publish the url-file"
[[ -f "$HOME/pre1-stream.url.meta" ]] || fail "push did not publish the sidecar"
grep -q 'URL-PUSHED' "$TMP/push1.log" || fail "push printed no receipt"
grep -q 'master.m3u8' "$TMP/push1.log" && fail "push leaked the URL into stdout"
grep -q 'master.m3u8' "$STUB/ssh-argv" && fail "push leaked the URL into ssh argv"
grep -q 'master.m3u8' "$REPO/state/KXEARNINGSMENTIONPRE1-TEST-WEBCAST/url-push.jsonl" \
  && fail "push journal recorded the URL instead of its hash"

RC=$(arm "$RUN1")
[[ "$RC" == 0 ]] || fail "case 1: arm exited $RC with a fresh pushed url-file"
[[ "$(launches)" == 1 ]] || fail "case 1: expected exactly one launch, got $(launches)"
[[ -f "$RUN1/control/pcm-proof.json" ]] || fail "case 1: no PCM proof was produced"
grep -q 'RESOLVER_DEADLINE_EPOCH=<unset>' "$STUB/wait-env" \
  && WAIT_ENV_VISIBLE=0 || WAIT_ENV_VISIBLE=1

# ── Case 2: a PRIOR attempt's url-file is refused ─────────────────────────────
# Case 1's file and sidecar are still sitting in $HOME. A different attempt
# reading the same path must refuse them — the AMAT r1/r2 shape. Every
# timestamp-based freshness rule passes this file; only attempt identity
# refuses it.
RUN2=$(make_run 2 pre1-stream.url)
bind_preresolved "$RUN2" pre1-stream.url "$(soon 8)"
[[ "$(plan_sha "$RUN2")" != "$(plan_sha "$RUN1")" ]] || fail "case 2: the two attempts share a plan hash"
BEFORE=$(launches)
RC=$(arm "$RUN2")
[[ "$RC" == 3 ]] || fail "case 2: a prior attempt's url-file armed (exit $RC)"
[[ "$(launches)" == "$BEFORE" ]] || fail "case 2: a prior attempt's url-file reached the launcher"
[[ ! -e "$RUN2/control/pcm-proof.json" ]] \
  || fail "case 2: the wait admitted the file and the PCM gate refused it — wrong gate"
grep -q "NOT ARMED — resolver failed" "$STUB/pages" || fail "case 2: no page was fired"

# ── Case 3: a stale-but-correct file is refused on the freshness bound ────────
RUN3=$(make_run 3 pre3-stream.url)
bind_preresolved "$RUN3" pre3-stream.url "$(soon 8)"
printf 'https://media.example.invalid/live/master.m3u8\n' > "$HOME/pre3-stream.url"
PLAN3=$(plan_sha "$RUN3") URLF="$HOME/pre3-stream.url" python3 - <<'PY'
import hashlib, json, os
from pathlib import Path
p = Path(os.environ["URLF"])
Path(str(p) + ".meta").write_text(json.dumps({
    "schema": 1, "kind": "url-file-push", "event_id": "E", "run_id": "R",
    "run_plan_sha256": os.environ["PLAN3"], "pushed_utc": "2026-08-01T00:00:00Z",
    "url_sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
}, sort_keys=True) + "\n")
PY
python3 -c 'import os,sys;os.utime(sys.argv[1],(1,1))' "$HOME/pre3-stream.url"
BEFORE=$(launches)
RC=$(arm "$RUN3")
[[ "$RC" == 3 ]] || fail "case 3: a two-day-old url-file armed (exit $RC)"
[[ "$(launches)" == "$BEFORE" ]] || fail "case 3: a stale url-file reached the launcher"
[[ ! -e "$RUN3/control/pcm-proof.json" ]] || fail "case 3: the wait admitted a stale file"

# ── Case 4: nothing ever arrives — bounded, exit 3, and the page fires ────────
RUN4=$(make_run 4 pre4-stream.url)
bind_preresolved "$RUN4" pre4-stream.url "$(soon 8)"
: > "$STUB/pages"
BEFORE=$(launches)
RC=$(arm "$RUN4")
[[ "$RC" == 3 ]] || fail "case 4: the wait did not exhaust its deadline (exit $RC)"
[[ "$(launches)" == "$BEFORE" ]] || fail "case 4: an unresolved plan reached the launcher"
[[ "$(grep -c 'NOT ARMED — resolver failed' "$STUB/pages")" == 1 ]] \
  || fail "case 4: expected exactly one deadline page"

# ── Seam S-E: the laptop installer must accept a preresolved plan ─────────────
# Verified, not assumed. install-t0-agents refuses static-public only; if this
# ever changes, lane S owns the fix and this line is the evidence.
RUN5=$(make_run 5 pre5-stream.url)
bind_preresolved "$RUN5" pre5-stream.url
if "$REPO/scripts/install-t0-agents.sh" --run-dir "$RUN5" --selftest-plan-gate \
     >/dev/null 2>&1; then :; fi
python3 - "$RUN5" "$REPO" <<'PY'
import json, sys
from pathlib import Path
run, repo = Path(sys.argv[1]), Path(sys.argv[2])
access = json.loads((run / "control/access-plan.json").read_text())
gate = (repo / "scripts/install-t0-agents.sh").read_text()
assert access["resolver"]["kind"] == "preresolved"
# The installer's only kind refusal is static-public; a preresolved plan is
# first-class to it.
assert 'resolver", {}).get("kind") == "static-public"' in gate, \
    "install-t0-agents kind gate changed shape; re-verify seam S-E"
assert "preresolved" not in gate, "install-t0-agents now names preresolved — re-verify seam S-E"
PY

# ── R5: what the resolver child can actually see of arm-on-url's environment ──
[[ -s "$STUB/wait-env" ]] || fail "the bound resolver never ran"
if (( WAIT_ENV_VISIBLE )); then
  grep -qE 'RESOLVER_DEADLINE_EPOCH=[0-9]+' "$STUB/wait-env" \
    || fail "RESOLVER_DEADLINE_EPOCH reached the resolver as neither a number nor <unset>"
else
  fail "arm-on-url computes RESOLVER_DEADLINE_EPOCH but never exports it, so no resolver can honour it"
fi

echo "preresolved_arm: PASS"
