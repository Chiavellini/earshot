#!/usr/bin/env bash
# push-url-file — the laptop side of the resolver split: mint (or take) this
# attempt's stream URL and deliver it to the executing host, with the URL bytes
# on SSH STDIN and never in argv.
#
#   scripts/push-url-file.sh --run-dir var/runs/<EVENT>/<RUN> --resolve
#   scripts/push-url-file.sh --run-dir var/runs/<EVENT>/<RUN> --from-file <path>
#   scripts/push-url-file.sh --run-dir <laptop-hosted run> --from-file <path> --host local   # this host IS the executing host
#   scripts/push-url-file.sh --check          # offline self-test, stubbed ssh
#
# WHY THIS EXISTS (2026-08-13, the box-webcast migration): browser-class venues
# cannot be resolved on the us-east box — no playwright, and no operator to pass
# a registration wall (THE AMAT RULE). The laptop resolves, this pushes, and
# scripts/urlfile-wait.sh on the box waits. The AccessPlan records BOTH legs, so
# `--resolve` runs the exact recorded resolver instead of an operator retyping
# browser-resolve argv at T-75.
#
# WHY STDIN, NEVER ARGV: these URLs carry session tokens and argv is `ps`-visible
# to every user on the host. `box-event-install.sh` sets the precedent for the
# digit store; this follows it and adds atomicity — remote tmp in the TARGET's
# own directory, chmod, then rename (a cross-filesystem `mv` is a copy, and
# capture-ready.py re-hashes the file after the PCM proof, so a torn write is a
# refused arm).
#
# It also ships a sidecar `<url-file>.meta` naming the exact run_plan_sha256 this
# URL was minted for. That is what lets the box refuse a url-file left by a
# PRIOR attempt: the path is derived from the pinned config, so every attempt of
# one event reuses it (AMAT 2026-08-13 had two attempts 11 minutes apart on one
# $HOME/amat-stream.url).
#
# Log tags: URL-PUSHED URL-VERIFIED PUSH-REFUSED. The URL is NEVER printed or
#           journalled — url-sha is the durable identifier.
# Exit 0 pushed and verified / 1 refused / 2 usage. Never silent.
# Node: control.acquire.
set -uo pipefail

REPO="${EARSHOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
REPO=$(cd "$REPO" && pwd -P)
RUN_DIR="" FROM_FILE="" DO_RESOLVE=0 CHECK=0
BOX="${EARSHOT_BOX:-ubuntu@3.212.174.94}"
KEY="${EARSHOT_BOX_KEY:-$HOME/.ssh/earshot-useast1.pem}"
SSH_BIN="${EARSHOT_SSH:-ssh}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-dir) RUN_DIR="${2:?}"; shift 2 ;;
    --from-file) FROM_FILE="${2:?}"; shift 2 ;;
    --resolve) DO_RESOLVE=1; shift ;;
    --host) BOX="${2:?}"; shift 2 ;;
    --key) KEY="${2:?}"; shift 2 ;;
    --check) CHECK=1; shift ;;
    -h|--help) sed -n '2,33p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

refuse() { echo "PUSH-REFUSED: $*" >&2; exit 1; }

ssh_argv() {  # the ssh command line, WITHOUT the remote command
  if [[ -n "$KEY" && -f "$KEY" ]]; then
    printf '%s\n' "$SSH_BIN" -o ConnectTimeout=15 -o BatchMode=yes -i "$KEY" "$BOX"
  else
    printf '%s\n' "$SSH_BIN" -o ConnectTimeout=15 -o BatchMode=yes "$BOX"
  fi
}

sha_of() { python3 -c '
import hashlib,sys
print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$1"; }

# push_payload <local-file> <remote-basename>
# Atomically publishes the local bytes at $HOME/<remote-basename> on the remote
# host and proves the landed bytes by hash. The URL never appears in any argv:
# the only thing this sends on the command line is the BASENAME.
push_payload() {
  local local_file="$1" base="$2" want got remote_cmd
  [[ "$base" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] \
    || { echo "PUSH-REFUSED: unsafe remote basename: $base" >&2; return 1; }
  [[ -f "$local_file" ]] || { echo "PUSH-REFUSED: no local payload: $local_file" >&2; return 1; }
  want=$(sha_of "$local_file")
  # --host local: THIS host is the executing host (a laptop-hosted preresolved
  # plan, or the laptop leg of a box-primary/laptop-failover pair). Same
  # atomic publish, no ssh. 2026-08-19: the TGT/TOL box legs waited 75 min on a
  # url-file that had only ever been published on the laptop — a push must be
  # able to name EVERY executing host, and "local" must not need an ssh shim.
  if [[ "$BOX" == "local" ]]; then
    local T="$HOME/.${base}.$$.tmp"
    ( umask 077; cat "$local_file" > "$T" ) && chmod 644 "$T" && mv "$T" "$HOME/$base" \
      || { echo "PUSH-REFUSED: local publish failed for $base" >&2; return 1; }
    got=$(sha_of "$HOME/$base")
    [[ "$got" == "$want" ]] || { echo "PUSH-REFUSED: $base landed as $got, expected $want" >&2; return 1; }
    echo "URL-VERIFIED $base sha=${want:0:12} (local)"
    return 0
  fi
  # \$HOME and \$\$ stay literal so the REMOTE shell expands them: the executing
  # host's home is the only correct destination, and the tmp file must be born
  # in the target's own directory for the rename to be atomic.
  remote_cmd="umask 077; T=\"\$HOME/.${base}.\$\$.tmp\"; cat > \"\$T\" && chmod 644 \"\$T\" && mv \"\$T\" \"\$HOME/${base}\""
  local argv=()
  while IFS= read -r part; do argv+=("$part"); done < <(ssh_argv)
  "${argv[@]}" "$remote_cmd" < "$local_file" \
    || { echo "PUSH-REFUSED: transport failed for $base" >&2; return 1; }
  got=$("${argv[@]}" "sha256sum \"\$HOME/${base}\" 2>/dev/null || shasum -a 256 \"\$HOME/${base}\"" \
        2>/dev/null | awk '{print $1}')
  if [[ "$got" != "$want" ]]; then
    echo "PUSH-REFUSED: $base landed as ${got:-<unreadable>}, expected $want" >&2
    return 1
  fi
  echo "URL-VERIFIED $base sha=${want:0:12}"
  return 0
}

if (( CHECK )); then
  # Offline self-test of the TRANSPORT — the part that must never leak a URL and
  # must never leave a half-written file. Plan validation is covered by
  # tests/access_plan.py and tests/preresolved_arm.sh.
  P=0; F=0
  chk() { if [[ "$2" == "$3" ]]; then echo "  PASS  $1"; P=$((P+1));
          else echo "  FAIL  $1 (expected $2, got $3)"; F=$((F+1)); fi; }
  T="$(mktemp -d "${TMPDIR:-/tmp}/push-url-file-check.XXXXXX")"
  trap 'rm -rf -- "$T"' EXIT
  echo "push-url-file self-test:"
  mkdir -p "$T/fakehome" "$T/bin"
  cat > "$T/bin/ssh-stub" <<'SH'
#!/usr/bin/env bash
# Records every argument, then runs the remote command for real against a fake
# HOME so atomicity and permissions are exercised rather than asserted.
printf '%s\n' "$*" >> "$ARGV_LOG"
[[ "${STUB_FAIL:-0}" == 1 ]] && exit 255
CMD="${@: -1}"
HOME="$FAKE_HOME" /bin/sh -c "$CMD"
SH
  chmod +x "$T/bin/ssh-stub"
  export ARGV_LOG="$T/argv.log" FAKE_HOME="$T/fakehome"
  SSH_BIN="$T/bin/ssh-stub"; KEY=""; BOX="stub-host"
  printf 'https://cdn.invalid/live/master.m3u8?token=SUPERSECRETTOKENVALUE\n' > "$T/x-stream.url"
  WANT=$(sha_of "$T/x-stream.url")

  push_payload "$T/x-stream.url" "x-stream.url" > "$T/push.log" 2>&1
  chk "push succeeds"            0 "$?"
  chk "bytes landed"             "$WANT" "$(sha_of "$FAKE_HOME/x-stream.url" 2>/dev/null)"
  chk "mode is 0644"             644 "$(python3 -c '
import os,stat,sys
print(oct(stat.S_IMODE(os.stat(sys.argv[1]).st_mode))[2:])' "$FAKE_HOME/x-stream.url")"
  chk "no tmp left behind"       0 "$(find "$FAKE_HOME" -name '.*tmp*' | wc -l | tr -d ' ')"
  # THE transport invariant: a token is ps-visible in argv, so it must only ever
  # travel on fd 0.
  chk "no URL in any argv"       0 "$(grep -c 'http' "$ARGV_LOG" | tr -d ' ')"
  chk "no token in any argv"     0 "$(grep -c 'SUPERSECRETTOKEN' "$ARGV_LOG" | tr -d ' ')"
  chk "no token in stdout"       0 "$(grep -c 'SUPERSECRETTOKEN' "$T/push.log" | tr -d ' ')"
  # Two ssh calls carry the basename: the push and the hash readback that proves
  # the landed bytes. The basename is not a secret; the URL is.
  chk "basename reaches argv twice" 2 "$(grep -c 'x-stream.url' "$ARGV_LOG" | tr -d ' ')"
  chk "remote writes tmp first"  1 "$(grep -c 'cat > "\$T"' "$ARGV_LOG" | tr -d ' ')"
  chk "remote renames into place" 1 "$(grep -c 'mv "\$T" "\$HOME/x-stream.url"' "$ARGV_LOG" | tr -d ' ')"

  rm -f "$FAKE_HOME/x-stream.url"
  STUB_FAIL=1 push_payload "$T/x-stream.url" "x-stream.url" >/dev/null 2>&1
  chk "transport failure refuses" 1 "$?"
  chk "nothing published on failure" 0 "$(ls "$FAKE_HOME" | wc -l | tr -d ' ')"
  unset STUB_FAIL
  push_payload "$T/x-stream.url" 'evil;rm -rf /' >/dev/null 2>&1
  chk "unsafe basename refused"  1 "$?"
  push_payload "$T/missing.url" "x-stream.url" >/dev/null 2>&1
  chk "missing payload refused"  1 "$?"
  echo
  (( F )) && { echo "SELFTEST FAIL ($F failed, $P passed)"; exit 1; }
  echo "SELFTEST PASS ($P checks)"; exit 0
fi

cd "$REPO"
[[ -n "$RUN_DIR" ]] || { echo "USAGE-ERROR: --run-dir is required" >&2; exit 2; }
if (( DO_RESOLVE )) && [[ -n "$FROM_FILE" ]]; then
  echo "USAGE-ERROR: --resolve and --from-file are alternatives" >&2; exit 2
fi
if (( ! DO_RESOLVE )) && [[ -z "$FROM_FILE" ]]; then
  echo "USAGE-ERROR: choose --resolve (run the plan's recorded resolve leg) or --from-file" >&2
  exit 2
fi

VALIDATION=$(EARSHOT_REPO="$REPO" python3 scripts/access-plan.py validate \
  --run-dir "$RUN_DIR" --canonical 2>&1) \
  || refuse "canonical AccessPlan invalid: $(printf '%s' "$VALIDATION" | tail -1)"

# `2>&1` belongs on the python invocation itself: on a line of its own after the
# heredoc terminator it is a null command that returns 0 and masks the real exit
# status, which is how a refusal reaches the next line as an unbound variable.
FACTS=$(VALIDATION="$VALIDATION" EARSHOT_REPO="$REPO" python3 - "$REPO" "$RUN_DIR" 2>&1 <<'PY'
import json, os, shlex, subprocess, sys
from pathlib import Path

repo, raw_run = Path(sys.argv[1]), sys.argv[2]
run = Path(raw_run)
run = (run if run.is_absolute() else repo / run).resolve()
verdict = json.loads(os.environ["VALIDATION"])
if verdict["resolver_kind"] != "preresolved":
    raise SystemExit(
        f"AccessPlan resolver kind is {verdict['resolver_kind']!r}; only a"
        " preresolved plan consumes a pushed url-file (a native resolver runs"
        " on the executing host and needs no push)"
    )
access = json.loads((run / "control/access-plan.json").read_text())
plan = json.loads((run / "control/run-plan.json").read_text())

# A late push silently swaps what the detector listens to: real configs `cat`
# the url-file inside the pipe source, so the runtime re-reads it whenever a leg
# respawns. Once capture is proven, this attempt's endpoint is settled.
status = json.loads(subprocess.run(
    [sys.executable, str(repo / "scripts/eventctl.py"), "status",
     "--run-dir", str(run), "--json"],
    cwd=repo, env={**os.environ, "EARSHOT_REPO": str(repo)},
    text=True, capture_output=True, check=True,
).stdout)["state"]
reached = {x["transition"] for x in status["reached"]}
if "capture_ready" in reached:
    raise SystemExit(
        "run has already proven capture_ready; pushing a new URL now would swap"
        " the endpoint under a running arm. Compile a new RunPlan attempt."
    )
if not status["active"]:
    raise SystemExit("RunPlan is terminal/archived")

url_files = access["endpoint"]["url_files"]
if len(url_files) != 1:
    raise SystemExit("preresolved plans carry exactly one URL file")
portable = url_files[0]
base = portable[len("$HOME/"):]
if "/" in base:
    raise SystemExit(f"url-file must sit directly under $HOME, got {portable}")
pre = access["resolver"].get("preresolve") or {}
values = {
    "PLAN_EVENT": access["event_id"],
    "PLAN_RUN_ID": access["run_id"],
    "PLAN_SHA": access["run_plan_sha256"],
    "URL_BASE": base,
    "LOCAL_URL_FILE": str(Path.home() / base),
    "PRERESOLVE_KIND": pre.get("kind", ""),
    # Expanded exactly as arm-on-url expands a resolver argv: literal $HOME/
    # only, because the argv is executed directly with no shell.
    "PRERESOLVE_ARGV_JSON": json.dumps([
        str(Path.home() / x[len("$HOME/"):]) if x.startswith("$HOME/") else x
        for x in pre.get("argv", [])
    ], separators=(",", ":")),
}
assert plan["plan_sha256"] == access["run_plan_sha256"]
for key, value in values.items():
    print(f"{key}={shlex.quote(value)}")
PY
) || refuse "$(printf '%s' "$FACTS" | tail -1)"
# Only the KEY=value lines are evaluated: a stray diagnostic on either stream
# must never become an assignment.
eval "$(printf '%s\n' "$FACTS" | grep -E '^[A-Z_]+=')"

if (( DO_RESOLVE )); then
  RESOLVE_ARGV=()
  while IFS= read -r part; do RESOLVE_ARGV+=("$part"); done < <(
    python3 -c 'import json,sys
for x in json.loads(sys.argv[1]): print(x)' "$PRERESOLVE_ARGV_JSON")
  [[ "${#RESOLVE_ARGV[@]}" -gt 0 ]] || refuse "AccessPlan records no preresolve argv"
  echo "=== push-url-file resolve $(date -u +%FT%TZ) kind=$PRERESOLVE_KIND leg=${RESOLVE_ARGV[0]}"
  "${RESOLVE_ARGV[@]}" || refuse "recorded resolve leg exited $? — nothing pushed"
  FROM_FILE="$LOCAL_URL_FILE"
fi

[[ -f "$FROM_FILE" && ! -L "$FROM_FILE" ]] || refuse "no regular local url-file at $FROM_FILE"
URL_SHA=$(sha_of "$FROM_FILE")
python3 - "$FROM_FILE" <<'PY' || exit 1
import sys
raw = open(sys.argv[1], "rb").read()
if not raw.strip():
    raise SystemExit("PUSH-REFUSED: local url-file is empty")
first = raw.decode("utf-8", "replace").splitlines()[0].strip()
if not first.startswith(("http://", "https://")):
    raise SystemExit("PUSH-REFUSED: first line of the url-file is not an http(s) endpoint")
if ".m3u8" not in first.split("?", 1)[0].lower():
    # Not fatal — some venues serve a metafile — but pcm-gate.sh requires HLS
    # continuity, so a DASH/.mpd endpoint can never reach capture_ready.
    print(
        "WARN: the pushed endpoint does not look like an HLS playlist; the PCM"
        " gate requires HLS continuity and will refuse a DASH manifest",
        file=sys.stderr,
    )
PY

SIDECAR="$(mktemp "${TMPDIR:-/tmp}/earshot-url-meta.XXXXXX")"
trap 'rm -f -- "$SIDECAR"' EXIT
PLAN_SHA="$PLAN_SHA" URL_SHA="$URL_SHA" PLAN_EVENT="$PLAN_EVENT" \
PLAN_RUN_ID="$PLAN_RUN_ID" python3 - "$SIDECAR" <<'PY'
import datetime as dt, json, os, sys
from pathlib import Path
Path(sys.argv[1]).write_text(json.dumps({
    "schema": 1,
    "kind": "url-file-push",
    "event_id": os.environ["PLAN_EVENT"],
    "run_id": os.environ["PLAN_RUN_ID"],
    "run_plan_sha256": os.environ["PLAN_SHA"],
    "pushed_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "url_sha256": os.environ["URL_SHA"],
}, sort_keys=True) + "\n")
PY

# Sidecar first, url-file last: the url-file is what every consumer reads, so it
# is the last rename to land. Either order is fail-safe anyway — the wait
# requires sidecar.url_sha256 to equal the url-file's hash, so a half-updated
# pair is refused rather than armed.
push_payload "$SIDECAR" "${URL_BASE}.meta" || refuse "sidecar push failed"
push_payload "$FROM_FILE" "$URL_BASE" || refuse "url-file push failed"

JOURNAL="$REPO/state/$PLAN_EVENT/url-push.jsonl"
mkdir -p "$(dirname "$JOURNAL")"
JOURNAL="$JOURNAL" PLAN_EVENT="$PLAN_EVENT" PLAN_RUN_ID="$PLAN_RUN_ID" \
PLAN_SHA="$PLAN_SHA" URL_SHA="$URL_SHA" URL_BASE="$URL_BASE" BOX="$BOX" \
BYTES="$(wc -c < "$FROM_FILE" | tr -d ' ')" python3 - <<'PY'
import datetime as dt, json, os
# The URL itself is never written here: the sha is the durable identifier, and
# this journal outlives the token.
with open(os.environ["JOURNAL"], "a") as fh:
    fh.write(json.dumps({
        "ts_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event_id": os.environ["PLAN_EVENT"],
        "run_id": os.environ["PLAN_RUN_ID"],
        "run_plan_sha256": os.environ["PLAN_SHA"],
        "host": os.environ["BOX"],
        "remote_path": "$HOME/" + os.environ["URL_BASE"],
        "bytes": int(os.environ["BYTES"]),
        "url_sha256": os.environ["URL_SHA"],
        "verified": True,
    }, sort_keys=True) + "\n")
PY

echo "URL-PUSHED $BOX:\$HOME/$URL_BASE (+.meta) sha=${URL_SHA:0:12} attempt=${PLAN_SHA:0:12} → $JOURNAL"
