#!/usr/bin/env bash
# Pull box-hosted evidence into the laptop's grading roots without ever
# merging an unverified or divergent run in place.
#
#   scripts/pull-box-state.sh --date 2026-08-10
#   scripts/pull-box-state.sh --event KXEARNINGSMENTIONHIMS-26AUG10-PHONE
#   scripts/pull-box-state.sh --date 2026-08-10 --dry-run
#   scripts/pull-box-state.sh --event <E> --prelaunch    # pre-launch receipts
#   scripts/pull-box-state.sh --event <E> --supersede-legacy  # box re-preflighted
#   scripts/pull-box-state.sh --check                    # offline fixtures
#
# Two trees cross the host boundary:
#
#   ~/ear/state/<EVENT_ID>/            -> state/<EVENT_ID>/box/
#   ~/ear/var/runs/<EVENT_ID>/<RUN_ID> -> var/runs/<EVENT_ID>/<RUN_ID>/
#
# The legacy state copy stays under box/ so its provenance cannot be confused
# with a laptop-hosted lane. A typed run retains its immutable RunPlan host
# identity and receives an evidence_pulled receipt only after a complete,
# stable rsync snapshot is local and eventctl verifies the plan, ledger, and
# pinned inputs. The box is read-only throughout.
#
# --prelaunch (2026-08-14) returns the box's PRE-LAUNCH receipt prefix so a run
# preflighted on the box can be scheduled and verified from the laptop. Seam S-C
# mints preflight_passed on the box, but schedule-nightly.sh and
# box-webcast-install.sh both read that receipt from the LAPTOP ledger, and a
# box-only terminal receipt fails local verify outright — three symptoms of one
# missing door, all measured in the 2026-08-14 box rehearsal.
#
# It is bounded at `launched`, not at "active", and that bound is the whole
# safety argument. The default refusal below exists because a GROWING tape or
# ledger copied into the canonical root creates a second partial authority. A
# run that has not reached `launched` has no capture/, book/ or orders/ content
# at all — only atomically published control/ proofs and the append-only
# receipts.jsonl — so the hazard that refusal names cannot exist yet. Once a run
# reaches `launched`, --prelaunch refuses it like any other active run.
#
# It publishes through the same run-host-boundary prefix primitive as every
# other direction and records NO receipt: evidence_pulled requires
# completed|aborted|failed (eventctl.py:95), and a pre-launch return is not an
# evidence outcome. The returned receipts are their own proof.
#
# What every successful publish DOES do (E11, 2026-08-17): it returns this
# copy's ledger write authority (`eventctl authority --returned-from`, a
# host-private sidecar — not a receipt, never transported, never pulled). The
# installers hand authority to the box at dispatch so a laptop-side append
# before the box's receipts come back cannot fork the chain; the pull is the
# only supported way to get it back. Order of operations, always:
# schedule → pull → close → dispatch.
set -euo pipefail

EXIT_REFUSED=1
EXIT_USAGE=2
EXIT_NO_MATCH=3

SCRIPT_PATH="$(cd "$(dirname "$0")" && pwd -P)/$(basename "$0")"
DEFAULT_REPO="$(cd "$(dirname "$0")/.." && pwd -P)"
LOCAL_REPO="${_EARSHOT_PULL_REPO_ROOT:-$DEFAULT_REPO}"
SOURCE_ROOT="${_EARSHOT_PULL_SOURCE_ROOT:-}"
BOX_HOST="${EARSHOT_BOX:-ubuntu@3.212.174.94}"
BOX_KEY="${EARSHOT_BOX_KEY:-$HOME/.ssh/earshot-useast1.pem}"

# One rsync per run dir, twice (transfer + checksum stability), plus the two
# enumerations — an event with several attempts opens ~20 SSH connections in a
# few seconds and trips sshd's MaxStartups, which drops them at RANDOM. That
# surfaced as "rsync failed for run <a different run each retry>", which reads
# like evidence corruption rather than a connection limit. Multiplex them onto
# one carried connection instead of retrying into the same wall.
SSH_MUX_DIR=""
SSH_OPTS=(-i "$BOX_KEY" -o BatchMode=yes -o ConnectTimeout=15)
EVENTCTL="${LOCAL_REPO}/scripts/eventctl.py"
BOUNDARY="${LOCAL_REPO}/scripts/run-host-boundary.py"

DATE_FILTER=""
EVENT_FILTER=""
DRY_RUN=0
CHECK_ONLY=0
PRELAUNCH=0
SUPERSEDE_LEGACY=0
WORK_ROOT=""

usage() {
  sed -n '2,11p' "$SCRIPT_PATH"
}

refuse() {
  echo "REFUSED: $*" >&2
  exit "$EXIT_REFUSED"
}

usage_error() {
  echo "USAGE: $*" >&2
  exit "$EXIT_USAGE"
}

cleanup_work() {
  stop_ssh_mux
  if [[ -n "$WORK_ROOT" && -d "$WORK_ROOT" ]]; then
    case "$WORK_ROOT" in
      "$LOCAL_REPO"/var/.pull-box-state.*)
        rm -rf -- "$WORK_ROOT"
        ;;
      *)
        echo "REFUSED cleanup outside pull staging root: $WORK_ROOT" >&2
        ;;
    esac
  fi
}

validate_event_id() {
  local value="$1"
  [[ ${#value} -le 64 && "$value" =~ ^[A-Z0-9]+(-[A-Z0-9]+)*$ ]]
}

validate_run_id() {
  local value="$1"
  [[ ${#value} -le 64 && "$value" =~ ^[A-Za-z0-9]+(-[A-Za-z0-9]+)*$ ]]
}

# Production uses read-only ssh/rsync. `_EARSHOT_PULL_SOURCE_ROOT` is an
# intentionally private offline-test seam: --check points it at a temp tree,
# and no network command runs in that mode.
start_ssh_mux() {
  [[ -z "$SOURCE_ROOT" ]] || return 0
  # Unix socket paths are capped near 104 bytes and %C adds 40 hex chars, so
  # this must NOT use TMPDIR: macOS sets it to a ~50-char per-user path and
  # ssh refuses outright with "ControlPath too long". /tmp is the short base
  # the limit forces; mktemp -d gives it 0700.
  SSH_MUX_DIR=$(mktemp -d /tmp/ear-pull-mux.XXXXXX) || return 1
  SSH_OPTS+=(-o ControlMaster=auto -o "ControlPath=$SSH_MUX_DIR/%C" -o ControlPersist=120)
}

stop_ssh_mux() {
  [[ -n "$SSH_MUX_DIR" ]] || return 0
  ssh "${SSH_OPTS[@]}" -O exit "$BOX_HOST" >/dev/null 2>&1 || true
  rm -rf -- "$SSH_MUX_DIR"
  SSH_MUX_DIR=""
}

box_ssh() { ssh "${SSH_OPTS[@]}" "$BOX_HOST" "$@"; }

source_list_events() {
  local kind="$1"
  if [[ -n "$SOURCE_ROOT" ]]; then
    [[ -d "$SOURCE_ROOT/$kind" ]] || return 0
    find "$SOURCE_ROOT/$kind" -mindepth 1 -maxdepth 1 -type d \
      -name "$MATCH" -exec basename {} \; | LC_ALL=C sort -u
    return
  fi
  box_ssh \
    "root=\"\$HOME/ear/$kind\"; [ -d \"\$root\" ] || exit 0; find \"\$root\" -mindepth 1 -maxdepth 1 -type d -name '$MATCH' -exec basename {} \\;" |
    LC_ALL=C sort -u
}

source_list_runs() {
  local event_id="$1"
  if [[ -n "$SOURCE_ROOT" ]]; then
    [[ -d "$SOURCE_ROOT/var/runs/$event_id" ]] || return 0
    find "$SOURCE_ROOT/var/runs/$event_id" -mindepth 1 -maxdepth 1 \
      -type d -exec basename {} \; | LC_ALL=C sort -u
    return
  fi
  box_ssh \
    "root=\"\$HOME/ear/var/runs/$event_id\"; [ -d \"\$root\" ] || exit 0; find \"\$root\" -mindepth 1 -maxdepth 1 -type d -exec basename {} \\;" |
    LC_ALL=C sort -u
}

contains_line() {
  local haystack="$1" needle="$2"
  [[ -n "$haystack" ]] || return 1
  printf '%s\n' "$haystack" | grep -Fqx -- "$needle"
}

rsync_snapshot() {
  local kind="$1" event_id="$2" run_id="$3" destination="$4"
  local source_path="" stability=""
  local -a opts
  opts=(-a --delete)
  if [[ "$kind" == "state" ]]; then
    # These two are reproducible deployment products, not event evidence.
    opts+=(--exclude /earshot-pinned --exclude /rules)
  else
    # The execution host's own ledger-authority marker (E11) is host-private
    # bookkeeping about ITS copy; it must never land here, where this host's
    # marker lives. run-host-boundary.py ignores it too — two layers.
    opts+=(--exclude=/control/authority.json)
  fi

  mkdir -p "$destination"
  if [[ -n "$SOURCE_ROOT" ]]; then
    if [[ "$kind" == "state" ]]; then
      source_path="$SOURCE_ROOT/state/$event_id/"
    else
      source_path="$SOURCE_ROOT/var/runs/$event_id/$run_id/"
    fi
    rsync "${opts[@]}" "$source_path" "$destination/" ||
      refuse "rsync failed for $kind $event_id ${run_id:+/ $run_id}"
    stability=$(rsync -ani --checksum --delete \
      "${opts[@]:2}" "$source_path" "$destination/") ||
      refuse "stability check failed for $kind $event_id ${run_id:+/ $run_id}"
  else
    local remote_shell
    remote_shell="ssh ${SSH_OPTS[*]}"
    if [[ "$kind" == "state" ]]; then
      source_path="$BOX_HOST:~/ear/state/$event_id/"
    else
      source_path="$BOX_HOST:~/ear/var/runs/$event_id/$run_id/"
    fi
    rsync "${opts[@]}" -e "$remote_shell" "$source_path" "$destination/" ||
      refuse "rsync failed for $kind $event_id ${run_id:+/ $run_id}"
    stability=$(rsync -ani --checksum --delete \
      "${opts[@]:2}" -e "$remote_shell" "$source_path" "$destination/") ||
      refuse "stability check failed for $kind $event_id ${run_id:+/ $run_id}"
  fi
  [[ -z "$stability" ]] || {
    printf '%s\n' "$stability" >&2
    refuse "source changed or transfer was partial for $kind $event_id ${run_id:+/ $run_id}"
  }

  if find "$destination" -type l -print -quit | grep -q .; then
    refuse "symlink found in pulled $kind tree for $event_id ${run_id:+/ $run_id}"
  fi
}

# Legacy state has no typed identity, so an existing box-provenance tree must
# match byte-for-byte. No rsync ever points at the final path. The two excluded
# reproducible deployment products are not evidence and never enter staging.
assert_compatible_legacy_state() {
  local staged="$1" final="$2"
  python3 - "$staged" "$final" <<'PY'
import hashlib
import os
import stat
import sys
from pathlib import Path

staged_raw, final_raw = sys.argv[1:]
staged, final = Path(staged_raw), Path(final_raw)

def inventory(root, ignored=()):
    files, dirs = {}, set()
    ignored = set(ignored)
    for base, names, fnames in os.walk(root, topdown=True, followlinks=False):
        base_path = Path(base)
        rel_base = base_path.relative_to(root)
        kept = []
        for name in names:
            rel = (rel_base / name).as_posix()
            if rel in ignored:
                continue
            path = base_path / name
            mode = path.lstat().st_mode
            if not stat.S_ISDIR(mode):
                raise SystemExit(f"non-directory entry in evidence tree: {path}")
            dirs.add(rel)
            kept.append(name)
        names[:] = kept
        for name in fnames:
            path = base_path / name
            rel = (rel_base / name).as_posix()
            if rel in ignored:
                continue
            mode = path.lstat().st_mode
            if not stat.S_ISREG(mode):
                raise SystemExit(f"non-regular entry in evidence tree: {path}")
            files[rel] = path
    return files, dirs

ignored = ("earshot-pinned", "rules")
sf, sd = inventory(staged, ignored)
lf, ld = inventory(final, ignored)

extra_files = set(lf) - set(sf)
missing_files = set(sf) - set(lf)
extra_dirs = ld - sd
missing_dirs = sd - ld
if extra_files or missing_files or extra_dirs or missing_dirs:
    raise SystemExit(
        "tree shape differs"
        f" (local_only_files={sorted(extra_files)},"
        f" source_only_files={sorted(missing_files)},"
        f" local_only_dirs={sorted(extra_dirs)},"
        f" source_only_dirs={sorted(missing_dirs)})"
    )

def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.digest()

for rel in sorted(sf):
    source_path, local_path = sf[rel], lf[rel]
    if digest(source_path) != digest(local_path):
        raise SystemExit(f"file bytes differ: {rel}")
PY
}

verify_run() {
  local run_dir="$1"
  EARSHOT_REPO="$LOCAL_REPO" python3 "$EVENTCTL" verify --run-dir "$run_dir"
}

status_class() {
  local run_dir="$1"
  local payload
  payload=$(EARSHOT_REPO="$LOCAL_REPO" python3 "$EVENTCTL" \
    status --run-dir "$run_dir" --json) || return 1
  python3 -c '
import json, sys
p = json.load(sys.stdin)
reached = {r["transition"] for r in p["state"]["reached"]}
terminal = p["state"].get("terminal")
t = terminal.get("transition") if terminal else None
if "completed" in reached or t in {"aborted", "failed"}:
    print("terminal")
elif "launched" in reached:
    # Launched but not terminal: capture/book/order evidence is growing right
    # now. Never publishable mid-flight, --prelaunch or not.
    print("active")
else:
    # Pre-launch: only control/ proofs and the append-only ledger exist.
    print("prelaunch")
' <<< "$payload"
}

record_evidence_pulled() {
  local run_dir="$1" source_label="$2"
  local -a artifact_args
  artifact_args=(--artifact control/run-plan.json)
  local relative
  for relative in \
    capture/audio.commit.json \
    book/kalshi-tape.jsonl \
    orders/order-evidence.jsonl \
    orders/fix-observations.jsonl \
    report/report.json
  do
    [[ -f "$run_dir/$relative" ]] && artifact_args+=(--artifact "$relative")
  done
  EARSHOT_REPO="$LOCAL_REPO" python3 "$EVENTCTL" record \
    --run-dir "$run_dir" \
    --transition evidence_pulled \
    --actor pull-box-state \
    --detail "complete verified run tree pulled from $source_label" \
    "${artifact_args[@]}"
}

# E11: authority comes home with the receipts. Called right after the boundary
# primitive has published/advanced this run from the execution host's copy —
# and BEFORE record_evidence_pulled, which would otherwise refuse on the very
# marker the installer wrote at dispatch. Not a receipt; the sidecar is
# host-private and the box copy never carries one.
authority_returned() {  # authority_returned <final-run-dir> <expected-host>
  EARSHOT_REPO="$LOCAL_REPO" python3 "$EVENTCTL" authority \
    --run-dir "$1" --returned-from "$2" --by pull-box-state >/dev/null
}

# Assertion helpers. NOT sugar — a correctness requirement.
#
# macOS ships bash 3.2 as /bin/bash, and `#!/usr/bin/env bash` resolves to it.
# In bash 3.2 a failing `[[ ... ]]` does NOT trigger `set -e`, while a failing
# `[ ... ]` does. Every assertion in this self-check used to be a bare `[[ ... ]]`,
# so on the operator's own laptop the whole gate passed vacuously — it could not
# fail. Measured 2026-08-14: with the --prelaunch publication path deliberately
# disabled, `--check` still printed PASS.
#
# These helpers exit non-zero on failure and name the assertion, so a red gate
# is both real and self-diagnosing. Do not reintroduce a bare `[[ ]]` assertion.
selftest_fail() {
  echo "SELFTEST FAIL: $*" >&2
  exit 1
}

must() {
  "$@" || selftest_fail "expected to succeed: $*"
}

must_not() {
  if "$@"; then
    selftest_fail "expected to fail: $*"
  fi
}

run_self_check() {
  local check_root source_repo local_repo event_id run_dir run_id
  local legacy_event bad_event active_event active_run failed_event failed_run
  local descoped_event descoped_run launched_event launched_run
  check_root=$(mktemp -d /tmp/pull-box-state-check.XXXXXX)
  trap 'rm -rf -- "$check_root"' RETURN
  source_repo="$check_root/source"
  local_repo="$check_root/local"
  mkdir -p "$source_repo/scripts" "$source_repo/configs" \
    "$source_repo/state" "$local_repo/scripts" "$local_repo/configs" \
    "$local_repo/state"
  cp "$DEFAULT_REPO/scripts/eventctl.py" "$source_repo/scripts/eventctl.py"
  cp "$DEFAULT_REPO/scripts/eventctl.py" "$local_repo/scripts/eventctl.py"
  cp "$DEFAULT_REPO/scripts/run-host-boundary.py" \
    "$local_repo/scripts/run-host-boundary.py"

  event_id="KXEARNINGSMENTIONPULLTEST-26AUG12-PHONE"
  cat > "$source_repo/configs/pull-test.toml" <<EOF
[event]
id = "$event_id"
starts_at = "2026-08-12T21:00:00Z"

[[sources]]
kind = "twilio_stream"
bind = "127.0.0.1:8080"

[[keywords]]
market_ticker = "KXEARNINGSMENTIONPULLTEST-26AUG12-FOO"
phrase = "foo"
EOF
  cp "$source_repo/configs/pull-test.toml" "$local_repo/configs/pull-test.toml"
  EARSHOT_REPO="$source_repo" EARSHOT_HOST_ID="box-test" \
    EARSHOT_KALSHI_ACCOUNT_FINGERPRINT="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" \
    python3 "$source_repo/scripts/eventctl.py" init \
      --config configs/pull-test.toml >/dev/null
  run_dir=$(find "$source_repo/var/runs/$event_id" -mindepth 1 -maxdepth 1 -type d)
  run_id=$(basename "$run_dir")
  # The real host-boundary topology starts with this exact laptop preflight
  # prefix. The box then extends it; retrieval must advance rather than call
  # the two verified trees divergent.
  mkdir -p "$local_repo/var/runs/$event_id"
  cp -R "$run_dir" "$local_repo/var/runs/$event_id/$run_id"
  # E11: the installer handed this copy's ledger authority to the box at
  # dispatch. Until the pull returns it, a laptop-side append is refused —
  # that append is exactly the fork that killed the 08-16 attempt.
  authority_of() {  # authority_of <run-dir> → unset|local|remote  (+by)
    EARSHOT_REPO="$local_repo" python3 "$local_repo/scripts/eventctl.py" authority \
      --run-dir "$1" --show | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["write_authority"], d.get("by",""))'
  }
  EARSHOT_REPO="$local_repo" python3 "$local_repo/scripts/eventctl.py" authority \
    --run-dir "$local_repo/var/runs/$event_id/$run_id" --transfer-to box-test \
    --by selftest-installer >/dev/null
  must test "$(authority_of "$local_repo/var/runs/$event_id/$run_id")" = "remote selftest-installer"
  local receipts_before
  receipts_before=$(cat "$local_repo/var/runs/$event_id/$run_id/control/receipts.jsonl")
  if EARSHOT_REPO="$local_repo" python3 "$local_repo/scripts/eventctl.py" record \
      --run-dir "$local_repo/var/runs/$event_id/$run_id" --transition blocked \
      > "$check_root/e11-close.log" 2>&1; then
    selftest_fail "laptop-side close of a dispatched run was accepted (E11 fork)"
  fi
  must grep -q 'REFUSED: E11' "$check_root/e11-close.log"
  must grep -q -- '--prelaunch' "$check_root/e11-close.log"
  must test "$(cat "$local_repo/var/runs/$event_id/$run_id/control/receipts.jsonl")" = "$receipts_before"
  local transition
  for transition in prepared admission_resolved study_completed \
    preflight_passed scheduled capture_ready launched source_attached \
    supervising draining completed
  do
    EARSHOT_REPO="$source_repo" python3 "$source_repo/scripts/eventctl.py" \
      record --run-dir "$run_dir" --transition "$transition" >/dev/null
  done
  printf '{"finalized":true,"runtime_success":true}\n' > \
    "$run_dir/report/report.json"
  # A stray authority marker on the BOX side is that host's private business:
  # it must neither cross (rsync exclude) nor block the reconcile (boundary
  # filter). Plant garbage so a crossing would be loud.
  printf '{"junk":1}\n' > "$run_dir/control/authority.json"
  mkdir -p "$source_repo/state/$event_id/rules"
  printf '{"sent":2}\n' > "$source_repo/state/$event_id/report.json"
  printf 'reproducible binary\n' > "$source_repo/state/$event_id/earshot-pinned"
  printf 'reproducible rules\n' > "$source_repo/state/$event_id/rules/compiled"

  _EARSHOT_PULL_SOURCE_ROOT="$source_repo" \
    _EARSHOT_PULL_REPO_ROOT="$local_repo" \
    "$SCRIPT_PATH" --date 2026-08-12 >/dev/null
  must test -f "$local_repo/state/$event_id/box/report.json"
  must_not test -e "$local_repo/state/$event_id/box/earshot-pinned"
  must_not test -e "$local_repo/state/$event_id/box/rules"
  must test -f "$local_repo/var/runs/$event_id/$run_id/report/report.json"
  EARSHOT_REPO="$local_repo" python3 "$local_repo/scripts/eventctl.py" \
    status --run-dir "$local_repo/var/runs/$event_id/$run_id" --json |
    python3 -c 'import json,sys; assert "evidence_pulled" in {r["transition"] for r in json.load(sys.stdin)["state"]["reached"]}'
  # ...which was only possible because the pull returned authority FIRST; the
  # local marker is this host's (by pull-box-state), not the box's garbage.
  must test "$(authority_of "$local_repo/var/runs/$event_id/$run_id")" = "local pull-box-state"
  must_not grep -q junk "$local_repo/var/runs/$event_id/$run_id/control/authority.json"

  # A second pull accepts the verified local evidence_pulled tail and changes
  # no evidence bytes.
  _EARSHOT_PULL_SOURCE_ROOT="$source_repo" \
    _EARSHOT_PULL_REPO_ROOT="$local_repo" \
    "$SCRIPT_PATH" --event "$event_id" >/dev/null

  # Divergent remote evidence is refused; the existing local file survives.
  printf '{"finalized":false}\n' > "$run_dir/report/report.json"
  if _EARSHOT_PULL_SOURCE_ROOT="$source_repo" \
      _EARSHOT_PULL_REPO_ROOT="$local_repo" \
      "$SCRIPT_PATH" --event "$event_id" >/dev/null 2>&1
  then
    echo "self-check: divergent run was accepted" >&2
    return 1
  fi
  must grep -q '"runtime_success":true' \
    "$local_repo/var/runs/$event_id/$run_id/report/report.json"
  printf '{"finalized":true,"runtime_success":true}\n' > \
    "$run_dir/report/report.json"

  # Dry-run enumerates a legacy-only event and writes no destination.
  legacy_event="KXEARNINGSMENTIONLEGACY-26AUG12-WEBCAST"
  mkdir -p "$source_repo/state/$legacy_event"
  printf 'legacy\n' > "$source_repo/state/$legacy_event/launch.log"
  _EARSHOT_PULL_SOURCE_ROOT="$source_repo" \
    _EARSHOT_PULL_REPO_ROOT="$local_repo" \
    "$SCRIPT_PATH" --event "$legacy_event" --dry-run >/dev/null
  must_not test -e "$local_repo/state/$legacy_event"
  _EARSHOT_PULL_SOURCE_ROOT="$source_repo" \
    _EARSHOT_PULL_REPO_ROOT="$local_repo" \
    "$SCRIPT_PATH" --event "$legacy_event" >/dev/null
  must test -f "$local_repo/state/$legacy_event/box/launch.log"
  must_not test -e "$local_repo/var/runs/$legacy_event"

  # An unchanged legacy tree re-pulls as a deliberate no-op, and --supersede-
  # legacy must NOT invent a move when nothing diverged.
  _EARSHOT_PULL_SOURCE_ROOT="$source_repo" \
    _EARSHOT_PULL_REPO_ROOT="$local_repo" \
    "$SCRIPT_PATH" --event "$legacy_event" --supersede-legacy >/dev/null
  must_not eval 'compgen -G "$local_repo/state/$legacy_event/box.superseded-*" >/dev/null'

  # The box re-runs preflight, rewriting a legacy artefact. Default: refuse,
  # and say how to proceed. This is the E10 symptom, hit twice in one drill.
  printf 'legacy v2\n' > "$source_repo/state/$legacy_event/launch.log"
  # Capture to a file rather than a pipeline: `must` takes a command, and under
  # pipefail a `| grep -q` would mask the refusal as SIGPIPE. And redirect the
  # COMMAND, not the assertion helper — `must_not cmd > log 2>&1` sends
  # selftest_fail's own message into the log too, so a broken assertion here
  # would exit 1 with no output at all. (Cost me a bogus red proof.)
  if env _EARSHOT_PULL_SOURCE_ROOT="$source_repo" \
      _EARSHOT_PULL_REPO_ROOT="$local_repo" \
      "$SCRIPT_PATH" --event "$legacy_event" \
      > "$check_root/diverge.log" 2>&1; then
    selftest_fail "divergent legacy state was published without --supersede-legacy"
  fi
  must grep -q -- '--supersede-legacy' "$check_root/diverge.log"
  # The refusal must leave the local tree exactly as it was.
  must grep -q '^legacy$' "$local_repo/state/$legacy_event/box/launch.log"

  # With the flag: the box copy is published and the local tree is ARCHIVED,
  # not overwritten and not deleted — its original bytes must still be readable.
  _EARSHOT_PULL_SOURCE_ROOT="$source_repo" \
    _EARSHOT_PULL_REPO_ROOT="$local_repo" \
    "$SCRIPT_PATH" --event "$legacy_event" --supersede-legacy >/dev/null
  must grep -q '^legacy v2$' "$local_repo/state/$legacy_event/box/launch.log"
  legacy_archived=$(compgen -G "$local_repo/state/$legacy_event/box.superseded-*" | head -1)
  must test -n "$legacy_archived"
  must grep -q '^legacy$' "$legacy_archived/launch.log"

  # --supersede-legacy is a pull argument, so --check must refuse it like the rest.
  must_not "$SCRIPT_PATH" --check --supersede-legacy

  # A nonterminal RunPlan is inventoried and verified in private staging but
  # never published into the laptop's canonical run root BY DEFAULT. Growing
  # live files cannot become a second partial authority.
  active_event="KXEARNINGSMENTIONACTIVE-26AUG12-PHONE"
  sed "s/$event_id/$active_event/g" "$source_repo/configs/pull-test.toml" > \
    "$source_repo/configs/active.toml"
  cp "$source_repo/configs/active.toml" "$local_repo/configs/active.toml"
  EARSHOT_REPO="$source_repo" EARSHOT_HOST_ID="box-test" \
    EARSHOT_KALSHI_ACCOUNT_FINGERPRINT="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" \
    python3 "$source_repo/scripts/eventctl.py" init \
      --config configs/active.toml >/dev/null
  active_run=$(find "$source_repo/var/runs/$active_event" \
    -mindepth 1 -maxdepth 1 -type d)
  _EARSHOT_PULL_SOURCE_ROOT="$source_repo" \
    _EARSHOT_PULL_REPO_ROOT="$local_repo" \
    "$SCRIPT_PATH" --event "$active_event" >/dev/null
  must_not test -e "$local_repo/var/runs/$active_event/$(basename "$active_run")"

  # --prelaunch returns that same pre-launch run, because it has not reached
  # `launched` and therefore has no growing capture/book/order evidence. This is
  # the supported door for a run preflighted on the box (2026-08-14 D1).
  _EARSHOT_PULL_SOURCE_ROOT="$source_repo" \
    _EARSHOT_PULL_REPO_ROOT="$local_repo" \
    "$SCRIPT_PATH" --event "$active_event" --prelaunch >/dev/null
  must test -f "$local_repo/var/runs/$active_event/$(basename "$active_run")/control/run-plan.json"
  # ...and records NO receipt: evidence_pulled requires completed|aborted|failed
  # and a returned preflight prefix is not an evidence outcome. Assert the
  # ledger EXISTS first — otherwise a missing file would satisfy must_not
  # vacuously and prove nothing.
  must test -f "$local_repo/var/runs/$active_event/$(basename "$active_run")/control/receipts.jsonl"
  must_not grep -q '"transition":"evidence_pulled"' \
    "$local_repo/var/runs/$active_event/$(basename "$active_run")/control/receipts.jsonl"
  # ...but it DOES return ledger authority to this host (E11) — a sidecar,
  # not a receipt.
  local active_local
  active_local="$local_repo/var/runs/$active_event/$(basename "$active_run")"
  must test "$(authority_of "$active_local")" = "local pull-box-state"
  # Idempotent: the second return is a boundary no-op, not a divergence.
  _EARSHOT_PULL_SOURCE_ROOT="$source_repo" \
    _EARSHOT_PULL_REPO_ROOT="$local_repo" \
    "$SCRIPT_PATH" --event "$active_event" --prelaunch >/dev/null

  # E11, the 2026-08-16 incident replayed with the doctrine enforced:
  # schedule → pull → close → dispatch. The laptop dispatches (authority →
  # box), the box preflights and schedules (mints receipts the laptop lacks),
  # the operator tries to close on the laptop: REFUSED, naming the pull. Pull,
  # then the close is a clean extension the next dispatch can carry.
  EARSHOT_REPO="$local_repo" python3 "$local_repo/scripts/eventctl.py" authority \
    --run-dir "$active_local" --transfer-to box-test --by selftest-installer >/dev/null
  for transition in prepared admission_resolved study_completed \
    preflight_passed scheduled
  do
    EARSHOT_REPO="$source_repo" python3 "$source_repo/scripts/eventctl.py" \
      record --run-dir "$active_run" --transition "$transition" >/dev/null
  done
  if EARSHOT_REPO="$local_repo" python3 "$local_repo/scripts/eventctl.py" record \
      --run-dir "$active_local" --transition blocked \
      > "$check_root/e11-prelaunch-close.log" 2>&1; then
    selftest_fail "close-after-dispatch was accepted before the pull (E11 fork)"
  fi
  must grep -q 'REFUSED: E11' "$check_root/e11-prelaunch-close.log"
  _EARSHOT_PULL_SOURCE_ROOT="$source_repo" \
    _EARSHOT_PULL_REPO_ROOT="$local_repo" \
    "$SCRIPT_PATH" --event "$active_event" --prelaunch >/dev/null
  must grep -q '"transition":"scheduled"' "$active_local/control/receipts.jsonl"
  must test "$(authority_of "$active_local")" = "local pull-box-state"
  EARSHOT_REPO="$local_repo" python3 "$local_repo/scripts/eventctl.py" record \
    --run-dir "$active_local" --transition blocked >/dev/null
  EARSHOT_REPO="$local_repo" python3 "$local_repo/scripts/eventctl.py" verify \
    --run-dir "$active_local" >/dev/null
  # ...and the local tail is now a clean extension of the box's chain, i.e.
  # what --dispatch-only would carry back: the box copy is a byte prefix.
  must python3 -c 'import sys; a=open(sys.argv[1],"rb").read(); b=open(sys.argv[2],"rb").read(); sys.exit(0 if a.startswith(b) and a!=b else 1)' \
    "$active_local/control/receipts.jsonl" "$active_run/control/receipts.jsonl"

  # A run that HAS reached `launched` is refused even under --prelaunch: from
  # that transition on, capture/book/order evidence is being written and a
  # mid-flight copy is exactly the second partial authority the default refusal
  # exists to prevent.
  launched_event="KXEARNINGSMENTIONLAUNCHED-26AUG12-PHONE"
  sed "s/$event_id/$launched_event/g" "$source_repo/configs/pull-test.toml" > \
    "$source_repo/configs/launched.toml"
  cp "$source_repo/configs/launched.toml" "$local_repo/configs/launched.toml"
  EARSHOT_REPO="$source_repo" EARSHOT_HOST_ID="box-test" \
    EARSHOT_KALSHI_ACCOUNT_FINGERPRINT="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" \
    python3 "$source_repo/scripts/eventctl.py" init \
      --config configs/launched.toml >/dev/null
  launched_run=$(find "$source_repo/var/runs/$launched_event" \
    -mindepth 1 -maxdepth 1 -type d)
  for transition in prepared admission_resolved study_completed \
    preflight_passed scheduled capture_ready launched
  do
    EARSHOT_REPO="$source_repo" python3 "$source_repo/scripts/eventctl.py" \
      record --run-dir "$launched_run" --transition "$transition" >/dev/null
  done
  _EARSHOT_PULL_SOURCE_ROOT="$source_repo" \
    _EARSHOT_PULL_REPO_ROOT="$local_repo" \
    "$SCRIPT_PATH" --event "$launched_event" --prelaunch >/dev/null
  must_not test -e "$local_repo/var/runs/$launched_event/$(basename "$launched_run")"

  # Failed execution is terminal evidence and is still audit-worthy; a
  # pre-launch descope is terminal bookkeeping but produced no evidence.
  failed_event="KXEARNINGSMENTIONFAILED-26AUG12-PHONE"
  sed "s/$event_id/$failed_event/g" "$source_repo/configs/pull-test.toml" > \
    "$source_repo/configs/failed.toml"
  cp "$source_repo/configs/failed.toml" "$local_repo/configs/failed.toml"
  EARSHOT_REPO="$source_repo" EARSHOT_HOST_ID="box-test" \
    EARSHOT_KALSHI_ACCOUNT_FINGERPRINT="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" \
    python3 "$source_repo/scripts/eventctl.py" init \
      --config configs/failed.toml >/dev/null
  failed_run=$(find "$source_repo/var/runs/$failed_event" \
    -mindepth 1 -maxdepth 1 -type d)
  EARSHOT_REPO="$source_repo" python3 "$source_repo/scripts/eventctl.py" \
    record --run-dir "$failed_run" --transition failed >/dev/null
  _EARSHOT_PULL_SOURCE_ROOT="$source_repo" \
    _EARSHOT_PULL_REPO_ROOT="$local_repo" \
    "$SCRIPT_PATH" --event "$failed_event" >/dev/null
  must grep -q '"transition":"evidence_pulled"' \
    "$local_repo/var/runs/$failed_event/$(basename "$failed_run")/control/receipts.jsonl"

  descoped_event="KXEARNINGSMENTIONDESCOPED-26AUG12-PHONE"
  sed "s/$event_id/$descoped_event/g" "$source_repo/configs/pull-test.toml" > \
    "$source_repo/configs/descoped.toml"
  cp "$source_repo/configs/descoped.toml" "$local_repo/configs/descoped.toml"
  EARSHOT_REPO="$source_repo" EARSHOT_HOST_ID="box-test" \
    EARSHOT_KALSHI_ACCOUNT_FINGERPRINT="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" \
    python3 "$source_repo/scripts/eventctl.py" init \
      --config configs/descoped.toml >/dev/null
  descoped_run=$(find "$source_repo/var/runs/$descoped_event" \
    -mindepth 1 -maxdepth 1 -type d)
  EARSHOT_REPO="$source_repo" python3 "$source_repo/scripts/eventctl.py" \
    record --run-dir "$descoped_run" --transition descope >/dev/null
  _EARSHOT_PULL_SOURCE_ROOT="$source_repo" \
    _EARSHOT_PULL_REPO_ROOT="$local_repo" \
    "$SCRIPT_PATH" --event "$descoped_event" >/dev/null
  must_not test -e "$local_repo/var/runs/$descoped_event/$(basename "$descoped_run")"

  # Invalid run identity aborts the whole staged pull before even its legacy
  # state is installed.
  bad_event="KXEARNINGSMENTIONBADPULL-26AUG12-PHONE"
  mkdir -p "$source_repo/state/$bad_event" \
    "$source_repo/var/runs/$bad_event/20260812T210000Z-p3-deadbeefdead/control"
  printf 'must-not-land\n' > "$source_repo/state/$bad_event/launch.log"
  printf '{}\n' > \
    "$source_repo/var/runs/$bad_event/20260812T210000Z-p3-deadbeefdead/control/run-plan.json"
  if _EARSHOT_PULL_SOURCE_ROOT="$source_repo" \
      _EARSHOT_PULL_REPO_ROOT="$local_repo" \
      "$SCRIPT_PATH" --event "$bad_event" >/dev/null 2>&1
  then
    echo "self-check: invalid run was accepted" >&2
    return 1
  fi
  must_not test -e "$local_repo/state/$bad_event"
  must_not test -e "$local_repo/var/runs/$bad_event"

  echo "pull-box-state --check: PASS"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --date)
      [[ $# -ge 2 ]] || usage_error "--date needs YYYY-MM-DD"
      DATE_FILTER="$2"
      shift 2
      ;;
    --event)
      [[ $# -ge 2 ]] || usage_error "--event needs EVENT_ID"
      EVENT_FILTER="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --prelaunch)
      PRELAUNCH=1
      shift
      ;;
    --supersede-legacy)
      SUPERSEDE_LEGACY=1
      shift
      ;;
    --check)
      CHECK_ONLY=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage_error "unknown argument: $1"
      ;;
  esac
done

if [[ "$CHECK_ONLY" -eq 1 ]]; then
  [[ -z "$DATE_FILTER" && -z "$EVENT_FILTER" && "$DRY_RUN" -eq 0 && "$PRELAUNCH" -eq 0 && "$SUPERSEDE_LEGACY" -eq 0 ]] ||
    usage_error "--check cannot be combined with pull arguments"
  run_self_check
  exit 0
fi

[[ -d "$LOCAL_REPO" ]] || refuse "local repository does not exist: $LOCAL_REPO"
LOCAL_REPO="$(cd "$LOCAL_REPO" && pwd -P)"
EVENTCTL="$LOCAL_REPO/scripts/eventctl.py"
[[ -f "$EVENTCTL" ]] || refuse "eventctl is missing: $EVENTCTL"
BOUNDARY="$LOCAL_REPO/scripts/run-host-boundary.py"
[[ -f "$BOUNDARY" ]] || refuse "run-host-boundary helper is missing: $BOUNDARY"

[[ -n "$DATE_FILTER" || -n "$EVENT_FILTER" ]] ||
  usage_error "give exactly one of --date YYYY-MM-DD or --event EVENT_ID"
[[ -z "$DATE_FILTER" || -z "$EVENT_FILTER" ]] ||
  usage_error "--date and --event are mutually exclusive"

if [[ -n "$DATE_FILTER" ]]; then
  MATCH=$(python3 - "$DATE_FILTER" <<'PY'
import datetime as dt
import sys
try:
    value = dt.date.fromisoformat(sys.argv[1])
except ValueError:
    raise SystemExit(1)
print("*" + value.strftime("%y%b%d").upper() + "*")
PY
  ) || usage_error "--date '$DATE_FILTER' is not YYYY-MM-DD"
else
  validate_event_id "$EVENT_FILTER" ||
    usage_error "unsafe or invalid EVENT_ID: $EVENT_FILTER"
  MATCH="$EVENT_FILTER"
fi

if [[ -z "$SOURCE_ROOT" ]]; then
  [[ "$BOX_HOST" =~ ^[A-Za-z0-9_.@:-]+$ ]] ||
    refuse "EARSHOT_BOX contains unsafe characters"
  [[ "$BOX_KEY" != *[$'\t\r\n ']* ]] ||
    refuse "EARSHOT_BOX_KEY may not contain whitespace"
  [[ -f "$BOX_KEY" ]] || refuse "SSH key not found: $BOX_KEY"
else
  [[ -d "$SOURCE_ROOT" ]] || refuse "offline source root does not exist: $SOURCE_ROOT"
  SOURCE_ROOT="$(cd "$SOURCE_ROOT" && pwd -P)"
fi

start_ssh_mux || refuse "could not allocate the SSH multiplex directory"
trap cleanup_work EXIT
STATE_EVENTS=$(source_list_events state) || refuse "could not enumerate box state"
RUN_EVENTS=$(source_list_events var/runs) || refuse "could not enumerate box runs"
EVENTS=$(printf '%s\n%s\n' "$STATE_EVENTS" "$RUN_EVENTS" |
  sed '/^$/d' | LC_ALL=C sort -u)
[[ -n "$EVENTS" ]] || {
  echo "no box state or run dirs match '$MATCH' — nothing to pull"
  exit "$EXIT_NO_MATCH"
}

while IFS= read -r event_id; do
  [[ -n "$event_id" ]] || continue
  validate_event_id "$event_id" ||
    refuse "box returned unsafe or invalid EVENT_ID: $event_id"
done <<< "$EVENTS"

SOURCE_LABEL="$BOX_HOST:~/ear"
[[ -n "$SOURCE_ROOT" ]] && SOURCE_LABEL="offline:$SOURCE_ROOT"

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "dry run — read-only inventory from $SOURCE_LABEL"
  while IFS= read -r event_id; do
    [[ -n "$event_id" ]] || continue
    if contains_line "$STATE_EVENTS" "$event_id"; then
      echo "  state/$event_id/ -> state/$event_id/box/"
    fi
    if contains_line "$RUN_EVENTS" "$event_id"; then
      runs=$(source_list_runs "$event_id") ||
        refuse "could not enumerate runs for $event_id"
      while IFS= read -r run_id; do
        [[ -n "$run_id" ]] || continue
        validate_run_id "$run_id" ||
          refuse "box returned unsafe or invalid RUN_ID: $event_id/$run_id"
        echo "  var/runs/$event_id/$run_id/ -> var/runs/$event_id/$run_id/"
      done <<< "$runs"
    fi
  done <<< "$EVENTS"
  echo "dry run complete — no local path was created or modified"
  exit 0
fi

mkdir -p "$LOCAL_REPO/var"
WORK_ROOT=$(mktemp -d "$LOCAL_REPO/var/.pull-box-state.XXXXXX")
trap cleanup_work EXIT
RUN_MANIFEST="$WORK_ROOT/runs.tsv"
TERMINAL_MANIFEST="$WORK_ROOT/terminal-runs.tsv"
PRELAUNCH_MANIFEST="$WORK_ROOT/prelaunch-runs.tsv"
STATE_MANIFEST="$WORK_ROOT/state.txt"
: > "$RUN_MANIFEST"
: > "$TERMINAL_MANIFEST"
: > "$PRELAUNCH_MANIFEST"
: > "$STATE_MANIFEST"

echo "staging read-only evidence from $SOURCE_LABEL"
while IFS= read -r event_id; do
  [[ -n "$event_id" ]] || continue
  if contains_line "$STATE_EVENTS" "$event_id"; then
    echo "  staging legacy state: $event_id"
    rsync_snapshot state "$event_id" "" "$WORK_ROOT/state/$event_id/box"
    printf '%s\n' "$event_id" >> "$STATE_MANIFEST"
  fi
  if contains_line "$RUN_EVENTS" "$event_id"; then
    runs=$(source_list_runs "$event_id") ||
      refuse "could not enumerate runs for $event_id"
    [[ -n "$runs" ]] || refuse "run event directory became empty: $event_id"
    while IFS= read -r run_id; do
      [[ -n "$run_id" ]] || continue
      validate_run_id "$run_id" ||
        refuse "box returned unsafe or invalid RUN_ID: $event_id/$run_id"
      echo "  staging typed run: $event_id/$run_id"
      rsync_snapshot run "$event_id" "$run_id" \
        "$WORK_ROOT/runs/$event_id/$run_id"
      printf '%s\t%s\n' "$event_id" "$run_id" >> "$RUN_MANIFEST"
    done <<< "$runs"
  fi
done <<< "$EVENTS"

# Validate every staged run and every terminal-run collision before publishing
# any destination. Active runs remain private staged inventory only: copying a
# growing tape/ledger into the laptop's canonical root creates a second partial
# authority and makes the later terminal pull ambiguous.
while IFS=$'\t' read -r event_id run_id; do
  [[ -n "$event_id" ]] || continue
  staged="$WORK_ROOT/runs/$event_id/$run_id"
  final="$LOCAL_REPO/var/runs/$event_id/$run_id"
  verify_run "$staged" || refuse "staged RunPlan/ledger failed verification: $event_id/$run_id"
  class=$(status_class "$staged") ||
    refuse "could not derive staged terminal state: $event_id/$run_id"
  publish_to=""
  if [[ "$class" == "terminal" ]]; then
    publish_to="$TERMINAL_MANIFEST"
  elif [[ "$class" == "prelaunch" && "$PRELAUNCH" -eq 1 ]]; then
    publish_to="$PRELAUNCH_MANIFEST"
  fi
  if [[ -n "$publish_to" ]]; then
    expected_host=$(python3 -c \
      'import json,sys; print(json.load(open(sys.argv[1]))["execution_host_id"])' \
      "$staged/control/run-plan.json") ||
      refuse "could not read RunPlan host: $event_id/$run_id"
    EARSHOT_REPO="$LOCAL_REPO" python3 "$BOUNDARY" reconcile \
      --repo "$LOCAL_REPO" \
      --incoming "$staged" \
      --destination "$final" \
      --expected-host "$expected_host" \
      --check-only >/dev/null ||
      refuse "existing local run diverges from box: $event_id/$run_id"
    printf '%s\t%s\t%s\n' "$event_id" "$run_id" "$expected_host" >> \
      "$publish_to"
  elif [[ "$class" == "prelaunch" ]]; then
    echo "inventory only; not publishing pre-launch typed run (use --prelaunch): $event_id/$run_id"
  else
    echo "inventory only; not publishing nonterminal typed run: $event_id/$run_id"
  fi
done < "$RUN_MANIFEST"

while IFS= read -r event_id; do
  [[ -n "$event_id" ]] || continue
  staged="$WORK_ROOT/state/$event_id/box"
  final="$LOCAL_REPO/state/$event_id/box"
  if [[ -e "$final" || -L "$final" ]]; then
    [[ -d "$final" && ! -L "$final" ]] ||
      refuse "local box-state target is not a real directory: $final"
    if ! DIVERGENCE=$(assert_compatible_legacy_state "$staged" "$final" 2>&1); then
      # Divergence here is usually LEGITIMATE, not corruption: re-running
      # preflight on the box rewrites detector-smoke.log, preflight-build.log
      # and friends, so the next pull of that event refuses wholesale. Refusing
      # to merge in place stays right (E3) — but the operator was left hand-
      # moving the tree aside, at exactly the moment a red preflight is being
      # re-run before an event. --supersede-legacy does that move as a
      # first-class step: archive aside, never overwrite, never delete.
      if [[ "$SUPERSEDE_LEGACY" -eq 1 ]]; then
        archived="$final.superseded-$(date -u +%Y%m%dT%H%M%SZ)"
        suffix=1
        while [[ -e "$archived" || -L "$archived" ]]; do
          archived="$final.superseded-$(date -u +%Y%m%dT%H%M%SZ)-$suffix"
          suffix=$((suffix + 1))
          [[ "$suffix" -le 50 ]] ||
            refuse "cannot allocate an archive name for $final"
        done
        mv "$final" "$archived" ||
          refuse "could not archive diverged local box state: $event_id"
        echo "superseded local box state: $event_id -> $(basename "$archived")"
        echo "  reason: $DIVERGENCE"
      else
        printf '%s\n' "$DIVERGENCE" >&2
        echo "hint: if the box legitimately re-ran preflight, re-run with --supersede-legacy" >&2
        echo "      (archives the local tree aside as state/$event_id/box.superseded-<UTC>;" >&2
        echo "       it never overwrites and never deletes)" >&2
        refuse "existing local box state diverges from box: $event_id"
      fi
    fi
  fi
done < "$STATE_MANIFEST"

# Publish only absent, already-verified snapshots. Existing compatible trees
# are deliberate no-ops; they are never rsynced over.
while IFS= read -r event_id; do
  [[ -n "$event_id" ]] || continue
  staged="$WORK_ROOT/state/$event_id/box"
  final="$LOCAL_REPO/state/$event_id/box"
  if [[ ! -e "$final" && ! -L "$final" ]]; then
    [[ ! -L "$LOCAL_REPO/state" && ! -L "$LOCAL_REPO/state/$event_id" ]] ||
      refuse "symlink in local box-state destination: $event_id"
    mkdir -p "$LOCAL_REPO/state/$event_id"
    mv "$staged" "$final"
  fi
done < "$STATE_MANIFEST"

# Publish/advance terminal typed runs — and, under --prelaunch, pre-launch ones
# — through the same prefix primitive used for laptop->box dispatch. Every
# collision was check-only validated above before the first legacy or typed
# destination was created.
while IFS=$'\t' read -r event_id run_id expected_host; do
  [[ -n "$event_id" ]] || continue
  staged="$WORK_ROOT/runs/$event_id/$run_id"
  final="$LOCAL_REPO/var/runs/$event_id/$run_id"
  EARSHOT_REPO="$LOCAL_REPO" python3 "$BOUNDARY" reconcile \
    --repo "$LOCAL_REPO" \
    --incoming "$staged" \
    --destination "$final" \
    --expected-host "$expected_host" >/dev/null ||
    refuse "could not publish verified box extension: $event_id/$run_id"
  # E11: this copy now holds everything the box had minted as of the snapshot;
  # it may be written again. Not for --check-only or inventory-only passes —
  # they never reach this loop.
  authority_returned "$final" "$expected_host" ||
    refuse "published box extension but could not return ledger authority: $event_id/$run_id"
done < <(cat "$TERMINAL_MANIFEST" "$PRELAUNCH_MANIFEST")

# Pre-launch returns carry no receipt: evidence_pulled requires
# completed|aborted|failed, and returning a preflight prefix is not an evidence
# outcome. Verify what landed and say so; the receipts are their own proof.
prelaunch_count=0
while IFS=$'\t' read -r event_id run_id expected_host; do
  [[ -n "$event_id" ]] || continue
  final="$LOCAL_REPO/var/runs/$event_id/$run_id"
  verify_run "$final" ||
    refuse "published pre-launch RunPlan/ledger failed verification: $event_id/$run_id"
  echo "pre-launch receipts returned (no receipt recorded): $event_id/$run_id"
  prelaunch_count=$((prelaunch_count + 1))
done < "$PRELAUNCH_MANIFEST"

# The public eventctl door is the final authority. Only terminal execution
# outcomes are eligible: completed, aborted, or failed. Legacy-only state and
# active/pre-launch plans never receive a typed evidence receipt.
run_count=$(wc -l < "$RUN_MANIFEST" | tr -d ' ')
terminal_count=0
while IFS=$'\t' read -r event_id run_id expected_host; do
  [[ -n "$event_id" ]] || continue
  final="$LOCAL_REPO/var/runs/$event_id/$run_id"
  verify_run "$final" || refuse "published RunPlan/ledger failed verification: $event_id/$run_id"
  record_evidence_pulled "$final" "$SOURCE_LABEL" ||
    refuse "could not record evidence_pulled: $event_id/$run_id"
  verify_run "$final" ||
    refuse "ledger failed after evidence_pulled: $event_id/$run_id"
  terminal_count=$((terminal_count + 1))
done < "$TERMINAL_MANIFEST"

state_count=$(wc -l < "$STATE_MANIFEST" | tr -d ' ')
echo "pull complete: $state_count legacy state tree(s), $run_count typed run(s), $terminal_count terminal receipt(s), $prelaunch_count pre-launch return(s)"
echo "next: python3 scripts/missed-arm-check.py --date ${DATE_FILTER:-<the event date>}"
