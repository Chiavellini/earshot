#!/usr/bin/env bash
# Does event-autorun.sh reach for the operator handler on its own?
#
# HIMS 26AUG10: the venue was operator-screened, the verdict was already in
# configs/dial-ins.toml, and the loop still dialled 17 legs over 31m47s cycling
# DTMF variants of a credential that was never a passcode. The handler was
# engaged by hand 17 minutes into a live call. Nothing in the loop read the one
# fact that should have triggered it.
#
# These cases run the REAL script (EARSHOT_AUTORUN_DECIDE_ONLY=1 exits right
# after the decision, before any dial, pidfile claim, STOP clear or mail), so
# they test what ships rather than a copy of its sed expressions.
#
#   tests/operator_escalation.sh
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 2
REPO=$PWD
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
PASS=0; FAIL=0

mkconfig() {  # mkconfig <path> <event-id> <operator-block:yes|no>
  { echo '[event]'
    echo "id = \"$2\""
    echo 'starts_at = "2026-08-10T21:00:00Z"'
    echo
    if [[ "$3" == yes ]]; then
      echo '[operator_handler]'
      echo 'enabled           = true'
      echo 'cell_number       = "+15550000000"'
      echo 'conference_prefix = "earshot"'
      echo
    fi
    echo '[kalshi]'
    echo 'series = "KXEARNINGSMENTIONTEST"'
  } > "$1"
}

mkenv() {  # mkenv <path> <event-name> <config> [extra lines...]
  { echo "EVENT_NAME=\"$2\""
    echo "CONFIG_PATH=\"$3\""
    echo 'TO_NUMBER="+15550000001"'
    echo 'PASSCODE="1234#"'
    echo 'LABEL="com.earshot.test"'
    echo "EVENT_DATE=\"$(date +%Y-%m-%d)\""
    echo 'END_HHMM_LIVE=2330'
    echo 'COMPLETE_EARLIEST_HHMM=2145'
    echo 'ACCOUNT_ENV="$HOME/.earshot/ms.env"'
    echo 'TICKERS="TEST"'
    shift 3
    for l in "$@"; do echo "$l"; done
  } > "$1"
}

# The registry is read-only here; we point the script at a fixture copy so a
# test can never depend on (or mutate) the real venue table.
mkregistry() {  # mkregistry <path> <key> <admission> <rehearsal-or-empty> [reservation:yes|no]
  { echo "[$2]"
    echo 'tier          = "published"'
    echo "admission        = \"$3\""
    [[ -z "$4" ]] || echo "operator_handler = \"$4\""
    [[ "${5:-no}" == yes ]] && echo 'reservation_window = true'
    # admission_detail must NOT be picked up by a lookup for `admission` — a
    # sloppy prefix match here would read prose as a verdict.
    echo 'admission_detail = "operator_screened is mentioned in this prose"'
    echo 'listen_access = "yes"'
    echo
    echo '[ZZOTHER]'
    echo 'admission        = "held"'
  } > "$1"
}

run_case() {  # run_case <name> <admission> <has-block> <expect> [env-extra] [rehearsal|none] [reservation]
  local name="$1" admission="$2" block="$3" expect="$4" extra="${5:-}"
  local rehearsal="${6:-rehearsed-2026-07-30}"
  local reservation="${7:-no}"
  [[ "$rehearsal" == none ]] && rehearsal=""
  local d="$TMP/$name"; mkdir -p "$d/configs" "$d/state" "$d/scripts"
  # A throwaway repo root: real scripts/, fixture configs/, empty state/.
  cp -R "$REPO/scripts/." "$d/scripts/" 2>/dev/null
  mkconfig "$d/configs/arm.toml" "KXEARNINGSMENTIONTEST-26AUG10-$name" "$block"
  # admission="" means "this venue has no registry entry at all" — write the
  # fixture under a DIFFERENT key so the lookup genuinely misses.
  if [[ -n "$admission" ]]; then
    mkregistry "$d/configs/dial-ins.toml" TESTV "$admission" "$rehearsal" "$reservation"
  else
    mkregistry "$d/configs/dial-ins.toml" SOMEONEELSE held ""
  fi
  if [[ -n "$extra" ]]; then
    mkenv "$d/configs/ev.env" TESTV "configs/arm.toml" "$extra"
  else
    mkenv "$d/configs/ev.env" TESTV "configs/arm.toml"
  fi

  # Capture stderr too: the env-completeness and placeholder guards fire BEFORE
  # stdout is redirected into the log, so a case asserting on them has no log.
  ( cd "$d" && EARSHOT_REPO="$d" EARSHOT_AUTORUN_DECIDE_ONLY=1 \
      EARSHOT_EVENT_ENV="configs/ev.env" \
      bash "$d/scripts/event-autorun.sh" ) >"$d/out" 2>"$d/err"

  local log="$d/state/KXEARNINGSMENTIONTEST-26AUG10-$name/autorun.log"
  local got; got=$(cat "$log" "$d/err" "$d/out" 2>/dev/null)
  if grep -qF "$expect" <<<"$got"; then
    echo "  OK   $name"; PASS=$((PASS+1))
  else
    echo "  FAIL $name"
    echo "       expected substring: $expect"
    echo "       log was:"
    sed 's/^/         /' <<<"${got:-<no log written>}" | tail -8
    FAIL=$((FAIL+1))
  fi
}

echo "operator escalation (scripts/event-autorun.sh):"

# THE HIMS CASE. Registry says screened, config can handle it ⇒ escalate before
# the first dial, without anyone setting OPERATOR_HANDLER by hand.
run_case screened-with-handler operator_screened yes \
  "operator handler: AUTO-ESCALATED"

# Screened but the config has no usable block: fail before any dial. A known
# silent/screened leg is not coverage; it is a deterministic missed event.
run_case screened-no-handler operator_screened no \
  "lacks a usable [operator_handler]"

# A config block is not evidence that its topology was rehearsed for the venue.
run_case screened-unrehearsed operator_screened yes \
  'lacks operator_handler="rehearsed-YYYY-MM-DD"' '' none

# A held venue must be untouched: no page, no behavior change.
run_case held-venue held yes \
  "DECISION admission=held reservation_window=false handler_rehearsal=rehearsed-2026-07-30 operator_handler=0 first_drop_fallback=0"

# A screened venue cannot be forced back to the known-dead listen-only path.
run_case env-forces-off operator_screened yes \
  "tries to disable the handler" 'OPERATOR_HANDLER=0'
# Explicit enable is still allowed when both config and rehearsal are valid.
run_case env-forces-on held yes \
  "explicitly enabled and verified" 'OPERATOR_HANDLER=1'

# A venue absent from the registry cannot carry a per-venue rehearsal without
# first exposing its operator screen. A usable config therefore ARMS (but does
# not yet invoke) the first-drop wrapper automatically.
run_case unknown-venue "" yes \
  "first-drop operator fallback: ARMED"

# Missing capability is a refusal, not permission to redial a newly discovered
# screener through the plain listen-only path.
run_case unknown-no-handler "" no \
  "lacks a usable [operator_handler] enabled=true capability"

# Reservation-window venues are the circular-discovery case in production:
# off-window probes classify nothing. The generic topology/config gate is
# enough to arm fallback; no per-venue rehearsal pin is required.
run_case reservation-unknown untested yes \
  "first-drop operator fallback: ARMED" '' none yes

# An operator-screened venue has NO keyable credential, so PASSCODE is
# legitimately EMPTY. It was on the required-non-empty list, which meant
# event-autorun exited "event env incomplete" for exactly the venue class the
# operator handler exists for — the escalation above would never have been
# reached on a real HIMS run.
run_case empty-passcode-launches operator_screened yes \
  "operator handler: AUTO-ESCALATED" 'PASSCODE=""'

# ...but an UNRESOLVED dial-in must still fail loud. Empty means "none"; TBD
# means "the hunt never found it" and may never reach a dial.
run_case tbd-passcode-refuses operator_screened yes \
  "still TBD in" 'PASSCODE="TBD"'

# The real operator-redial wrapper gets two bounded chances to reach the
# configured cell, but it must close a failed page before originating another
# and it must dial the bridge exactly once, only after a joined page. Stubs keep
# this fully offline while preserving the wrapper's actual control flow.
RETRY="$TMP/page-retry"; mkdir -p "$RETRY/state" "$RETRY/repo/scripts"
mkconfig "$RETRY/operator.toml" "KXEARNINGSMENTIONTEST-26AUG10-page-retry" yes
cp "$REPO/scripts/operator-redial.sh" "$RETRY/repo/scripts/operator-redial.sh"
cat > "$RETRY/repo/scripts/operator-page.sh" <<'SH'
#!/usr/bin/env bash
N=$(cat "${EARSHOT_OPERATOR_TEST_TIMELINE}.page-count" 2>/dev/null || echo 0)
N=$((N + 1))
echo "$N" > "${EARSHOT_OPERATOR_TEST_TIMELINE}.page-count"
if [[ "$N" == 1 ]]; then SID="CA11111111111111111111111111111111"; else SID="CA22222222222222222222222222222222"; fi
echo "page${N}" >> "$EARSHOT_OPERATOR_TEST_TIMELINE"
prev=""
for a in "$@"; do
  [[ "$prev" == "--sid-file" ]] && echo "$SID" > "$a"
  prev="$a"
done
[[ "$N" == 1 ]] && exit 7
exit 0
SH
cat > "$RETRY/repo/scripts/twilio-hangup.sh" <<'SH'
#!/usr/bin/env bash
echo "hangup:$1" >> "$EARSHOT_OPERATOR_TEST_TIMELINE"
exit 0
SH
cat > "$RETRY/repo/scripts/twilio-operator-dial.sh" <<'SH'
#!/usr/bin/env bash
echo bridge >> "$EARSHOT_OPERATOR_TEST_TIMELINE"
exit 0
SH
chmod +x "$RETRY/repo/scripts/operator-redial.sh" "$RETRY/repo/scripts/operator-page.sh" \
  "$RETRY/repo/scripts/twilio-hangup.sh" "$RETRY/repo/scripts/twilio-operator-dial.sh"
TIMELINE="$RETRY/timeline"
EARSHOT_OPERATOR_CONFIG="$RETRY/operator.toml" \
EARSHOT_OPERATOR_TEST_TIMELINE="$TIMELINE" \
  bash "$RETRY/repo/scripts/operator-redial.sh" --to +15550000001 \
    --stream-url wss://example.invalid/media --sid-file "$RETRY/state/call.sid" \
    >"$RETRY/out" 2>&1
RETRY_RC=$?
EXPECTED=$'page1\nhangup:CA11111111111111111111111111111111\npage2\nbridge'
if [[ "$RETRY_RC" == 0 && "$(cat "$TIMELINE" 2>/dev/null)" == "$EXPECTED" \
      && ! -e "$RETRY/state/admission.receipt.json" \
      && ! -e "$RETRY/state/admission.assertions.jsonl" ]]; then
  echo "  OK   bounded-page-retry-before-single-bridge"; PASS=$((PASS+1))
else
  echo "  FAIL bounded-page-retry-before-single-bridge (rc=$RETRY_RC timeline=$(tr '\n' '|' < "$TIMELINE" 2>/dev/null))"
  FAIL=$((FAIL+1))
fi

# If page origination fails without a CallSid, its existence is ambiguous.
# Retrying could create a second pager leg, so the wrapper must stop after one
# attempt and never reach the bridge.
AMB="$TMP/page-ambiguous"; mkdir -p "$AMB/state" "$AMB/repo/scripts"
mkconfig "$AMB/operator.toml" "KXEARNINGSMENTIONTEST-26AUG10-page-ambiguous" yes
cp "$REPO/scripts/operator-redial.sh" "$AMB/repo/scripts/operator-redial.sh"
cat > "$AMB/repo/scripts/operator-page.sh" <<'SH'
#!/usr/bin/env bash
echo page >> "$EARSHOT_OPERATOR_TEST_TIMELINE"
exit 1
SH
cat > "$AMB/repo/scripts/twilio-hangup.sh" <<'SH'
#!/usr/bin/env bash
echo hangup >> "$EARSHOT_OPERATOR_TEST_TIMELINE"
exit 0
SH
cat > "$AMB/repo/scripts/twilio-operator-dial.sh" <<'SH'
#!/usr/bin/env bash
echo bridge >> "$EARSHOT_OPERATOR_TEST_TIMELINE"
exit 0
SH
chmod +x "$AMB/repo/scripts/operator-redial.sh" "$AMB/repo/scripts/operator-page.sh" \
  "$AMB/repo/scripts/twilio-hangup.sh" "$AMB/repo/scripts/twilio-operator-dial.sh"
EARSHOT_OPERATOR_CONFIG="$AMB/operator.toml" \
EARSHOT_OPERATOR_TEST_TIMELINE="$AMB/timeline" \
  bash "$AMB/repo/scripts/operator-redial.sh" --to +15550000001 \
    --stream-url wss://example.invalid/media --sid-file "$AMB/state/call.sid" \
    >"$AMB/out" 2>&1
AMB_RC=$?
if [[ "$AMB_RC" != 0 && "$(cat "$AMB/timeline" 2>/dev/null)" == page ]]; then
  echo "  OK   ambiguous-page-does-not-retry-or-dial"; PASS=$((PASS+1))
else
  echo "  FAIL ambiguous-page-does-not-retry-or-dial (rc=$AMB_RC timeline=$(tr '\n' '|' < "$AMB/timeline" 2>/dev/null))"
  FAIL=$((FAIL+1))
fi

# The scheduler invokes launch exactly once. A phone drop belongs to the
# redial-supervisor behind that long-lived arm; an outer relaunch would reset
# one-shot state and overwrite the report. TEST-only injection keeps this
# network-free while executing the real scheduler body through completion.
ONE="$TMP/one-arm"; mkdir -p "$ONE/configs" "$ONE/state" "$ONE/scripts"
cp -R "$REPO/scripts/." "$ONE/scripts/" 2>/dev/null
mkconfig "$ONE/configs/arm.toml" "KXEARNINGSMENTIONTEST-26AUG10-one-arm" no
mkregistry "$ONE/configs/dial-ins.toml" TESTV held ""
mkenv "$ONE/configs/ev.env" TESTV "configs/arm.toml" 'BIND_PORT=18991'
cat > "$ONE/scripts/launch-stub.sh" <<'SH'
#!/usr/bin/env bash
printf 'called\n' >> "$EARSHOT_LAUNCH_STUB_COUNT"
printf '%s|%s\n' "${EARSHOT_FIRST_DROP_DIAL_SCRIPT:-}" "${EARSHOT_FIRST_DROP_OPERATOR_CONFIG:-}" > "$EARSHOT_LAUNCH_STUB_FALLBACK"
echo 'twilio: stream stopped frames=50'
exit 0
SH
chmod +x "$ONE/scripts/launch-stub.sh"
( cd "$ONE" && \
  EARSHOT_REPO="$ONE" EARSHOT_AUTORUN_TEST=1 \
  EARSHOT_EVENT_ENV="configs/ev.env" \
  EARSHOT_LAUNCH_STUB_COUNT="$ONE/count" \
  EARSHOT_LAUNCH_STUB_FALLBACK="$ONE/fallback" \
  EARSHOT_AUTORUN_TEST_LAUNCH_SCRIPT="$ONE/scripts/launch-stub.sh" \
  bash "$ONE/scripts/event-autorun.sh" ) >/dev/null 2>&1
ONE_RC=$?
CALLS=$(wc -l < "$ONE/count" 2>/dev/null | tr -d ' ')
if [[ "$ONE_RC" == 0 && "$CALLS" == 1 && "$(cat "$ONE/fallback" 2>/dev/null)" == '|' ]]; then
  echo "  OK   one-event-one-arm"; PASS=$((PASS+1))
else
  echo "  FAIL one-event-one-arm (rc=$ONE_RC launch calls=${CALLS:-0}, fallback=$(cat "$ONE/fallback" 2>/dev/null), expected rc=0 + 1 plain held route)"
  FAIL=$((FAIL+1))
fi

# Sending a summary must not erase the sole launcher's terminal failure. The
# real scheduler waits on event-autorun, so this status is its only immediate
# proof that runtime/finalizer completion failed. The TEST launcher seam makes
# the case network-free and also proves the one-arm invariant is retained.
CHILD_FAIL="$TMP/child-failure"; mkdir -p "$CHILD_FAIL/configs" "$CHILD_FAIL/state" "$CHILD_FAIL/scripts"
cp -R "$REPO/scripts/." "$CHILD_FAIL/scripts/" 2>/dev/null
mkconfig "$CHILD_FAIL/configs/arm.toml" "KXEARNINGSMENTIONTEST-26AUG10-child-failure" no
mkregistry "$CHILD_FAIL/configs/dial-ins.toml" TESTV held ""
mkenv "$CHILD_FAIL/configs/ev.env" TESTV "configs/arm.toml" 'BIND_PORT=18993'
cat > "$CHILD_FAIL/scripts/launch-stub.sh" <<'SH'
#!/usr/bin/env bash
printf 'called\n' >> "$EARSHOT_LAUNCH_STUB_COUNT"
echo 'twilio: stream stopped frames=50'
exit 23
SH
chmod +x "$CHILD_FAIL/scripts/launch-stub.sh"
( cd "$CHILD_FAIL" && \
  EARSHOT_REPO="$CHILD_FAIL" EARSHOT_AUTORUN_TEST=1 \
  EARSHOT_EVENT_ENV="configs/ev.env" \
  EARSHOT_LAUNCH_STUB_COUNT="$CHILD_FAIL/count" \
  EARSHOT_AUTORUN_TEST_LAUNCH_SCRIPT="$CHILD_FAIL/scripts/launch-stub.sh" \
  bash "$CHILD_FAIL/scripts/event-autorun.sh" ) >/dev/null 2>&1
CHILD_FAIL_RC=$?
CHILD_FAIL_STATE="$CHILD_FAIL/state/KXEARNINGSMENTIONTEST-26AUG10-child-failure"
if [[ "$CHILD_FAIL_RC" == 23 \
      && "$(wc -l < "$CHILD_FAIL/count" 2>/dev/null | tr -d ' ')" == 1 \
      && -s "$CHILD_FAIL_STATE/autorun-summary.txt" \
      && ! -e "$CHILD_FAIL_STATE/autorun.pid" ]] \
      && grep -qF 'scheduler exit: 23' "$CHILD_FAIL_STATE/autorun-summary.txt" \
      && grep -qF 'event-autorun done (scheduler exit 23)' "$CHILD_FAIL_STATE/autorun.log"; then
  echo "  OK   launcher-failure-remains-scheduler-visible"; PASS=$((PASS+1))
else
  echo "  FAIL launcher-failure-remains-scheduler-visible (rc=$CHILD_FAIL_RC calls=$(wc -l < "$CHILD_FAIL/count" 2>/dev/null | tr -d ' ') summary=$(test -s "$CHILD_FAIL_STATE/autorun-summary.txt" && echo yes || echo no))"
  FAIL=$((FAIL+1))
fi

# Live supervision requires auditable detector-path audio even when the launcher
# itself returns 0. This TEST-only gate exercises the identical terminal check
# without selecting live posture or dialing; status 10 is the stable supervisor
# code for a clean child whose audio is below the configured floor.
NO_AUDIO="$TMP/no-audio"; mkdir -p "$NO_AUDIO/configs" "$NO_AUDIO/state" "$NO_AUDIO/scripts"
cp -R "$REPO/scripts/." "$NO_AUDIO/scripts/" 2>/dev/null
mkconfig "$NO_AUDIO/configs/arm.toml" "KXEARNINGSMENTIONTEST-26AUG10-no-audio" no
mkregistry "$NO_AUDIO/configs/dial-ins.toml" TESTV held ""
mkenv "$NO_AUDIO/configs/ev.env" TESTV "configs/arm.toml" 'BIND_PORT=18994'
cat > "$NO_AUDIO/scripts/launch-stub.sh" <<'SH'
#!/usr/bin/env bash
printf 'called\n' >> "$EARSHOT_LAUNCH_STUB_COUNT"
echo 'twilio: stream stopped frames=0'
exit 0
SH
chmod +x "$NO_AUDIO/scripts/launch-stub.sh"
( cd "$NO_AUDIO" && \
  EARSHOT_REPO="$NO_AUDIO" EARSHOT_AUTORUN_TEST=1 \
  EARSHOT_AUTORUN_TEST_REQUIRE_AUDITABLE_AUDIO=1 \
  EARSHOT_EVENT_ENV="configs/ev.env" \
  EARSHOT_LAUNCH_STUB_COUNT="$NO_AUDIO/count" \
  EARSHOT_AUTORUN_TEST_LAUNCH_SCRIPT="$NO_AUDIO/scripts/launch-stub.sh" \
  bash "$NO_AUDIO/scripts/event-autorun.sh" ) >/dev/null 2>&1
NO_AUDIO_RC=$?
NO_AUDIO_STATE="$NO_AUDIO/state/KXEARNINGSMENTIONTEST-26AUG10-no-audio"
if [[ "$NO_AUDIO_RC" == 10 \
      && "$(wc -l < "$NO_AUDIO/count" 2>/dev/null | tr -d ' ')" == 1 \
      && -s "$NO_AUDIO_STATE/autorun-summary.txt" \
      && ! -e "$NO_AUDIO_STATE/autorun.pid" ]] \
      && grep -qF 'scheduler exit: 10' "$NO_AUDIO_STATE/autorun-summary.txt" \
      && grep -qF 'UNAUDITABLE/NO AUDIO (0 frames)' "$NO_AUDIO_STATE/autorun.log"; then
  echo "  OK   unauditable-audio-is-nonzero"; PASS=$((PASS+1))
else
  echo "  FAIL unauditable-audio-is-nonzero (rc=$NO_AUDIO_RC calls=$(wc -l < "$NO_AUDIO/count" 2>/dev/null | tr -d ' ') summary=$(test -s "$NO_AUDIO_STATE/autorun-summary.txt" && echo yes || echo no))"
  FAIL=$((FAIL+1))
fi

# End-to-end scheduler handoff: this is what makes the new behavior automatic
# instead of another optional env nobody sets. The real autorun decision must
# export the exact wrapper/config into its unchanged launch child for an absent
# admission verdict; launch then naturally passes those env vars to the
# supervisor it starts.
AUTO="$TMP/auto-fallback-arm"; mkdir -p "$AUTO/configs" "$AUTO/state" "$AUTO/scripts"
cp -R "$REPO/scripts/." "$AUTO/scripts/" 2>/dev/null
mkconfig "$AUTO/configs/arm.toml" "KXEARNINGSMENTIONTEST-26AUG10-auto-fallback" yes
mkregistry "$AUTO/configs/dial-ins.toml" SOMEONEELSE held ""
mkenv "$AUTO/configs/ev.env" TESTV "configs/arm.toml" 'BIND_PORT=18992'
cat > "$AUTO/scripts/launch-stub.sh" <<'SH'
#!/usr/bin/env bash
printf '%s|%s\n' "${EARSHOT_FIRST_DROP_DIAL_SCRIPT:-}" "${EARSHOT_FIRST_DROP_OPERATOR_CONFIG:-}" > "$EARSHOT_LAUNCH_STUB_FALLBACK"
echo 'twilio: stream stopped frames=50'
exit 0
SH
chmod +x "$AUTO/scripts/launch-stub.sh"
( cd "$AUTO" && \
  EARSHOT_REPO="$AUTO" EARSHOT_AUTORUN_TEST=1 \
  EARSHOT_EVENT_ENV="configs/ev.env" \
  EARSHOT_LAUNCH_STUB_FALLBACK="$AUTO/fallback" \
  EARSHOT_AUTORUN_TEST_LAUNCH_SCRIPT="$AUTO/scripts/launch-stub.sh" \
  bash "$AUTO/scripts/event-autorun.sh" ) >/dev/null 2>&1
EXPECTED_FALLBACK="$AUTO/scripts/operator-redial.sh|$AUTO/configs/arm.toml"
if [[ "$(cat "$AUTO/fallback" 2>/dev/null)" == "$EXPECTED_FALLBACK" ]]; then
  echo "  OK   autorun-exports-first-drop-fallback"; PASS=$((PASS+1))
else
  echo "  FAIL autorun-exports-first-drop-fallback (got=$(cat "$AUTO/fallback" 2>/dev/null), expected=$EXPECTED_FALLBACK)"
  FAIL=$((FAIL+1))
fi

echo
if (( FAIL )); then
  echo "FAILED — $FAIL of $((PASS+FAIL))" >&2
  exit 1
fi
echo "operator escalation OK — $PASS cases"
