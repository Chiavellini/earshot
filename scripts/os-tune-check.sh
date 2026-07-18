#!/usr/bin/env bash
# Read-only verifier for the us-east box OS tuning (docs/OS-TUNING.md).
# Prints PASS/WARN/FAIL per knob; exit 0 iff no FAIL. Safe to run anywhere —
# on a non-Linux host it reports and exits 2 (skip, not failure).
#
#   scripts/os-tune-check.sh [--cores 2-5]     # isolated-core range to expect
set -uo pipefail

CORES="2-5"
[[ "${1:-}" == "--cores" ]] && CORES="$2"

if [[ "$(uname -s)" != Linux ]]; then
  echo "SKIP: os-tune-check is Linux-only (this is $(uname -s))"
  exit 2
fi

fail=0
report() { # status name detail
  printf '%-4s %-28s %s\n' "$1" "$2" "$3"
  [[ "$1" == FAIL ]] && fail=1 || true
}

cmdline=$(cat /proc/cmdline)

# Boot args
for arg in isolcpus nohz_full rcu_nocbs; do
  if grep -q "$arg=$CORES" <<<"$cmdline"; then
    report PASS "$arg" "$arg=$CORES"
  elif grep -q "$arg=" <<<"$cmdline"; then
    report WARN "$arg" "present but not =$CORES: $(grep -o "$arg=[^ ]*" <<<"$cmdline")"
  else
    report FAIL "$arg" "missing from /proc/cmdline"
  fi
done

# Governor (virtualized instances often expose no cpufreq — WARN, not FAIL)
if [[ -d /sys/devices/system/cpu/cpu0/cpufreq ]]; then
  govs=$(cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor | sort -u)
  if [[ "$govs" == performance ]]; then
    report PASS "cpu governor" "performance on all cores"
  else
    report FAIL "cpu governor" "got: $(tr '\n' ' ' <<<"$govs")"
  fi
else
  report WARN "cpu governor" "no cpufreq sysfs (hypervisor-controlled — typical on EC2)"
fi

# THP defrag
if [[ -r /sys/kernel/mm/transparent_hugepage/defrag ]]; then
  if grep -q '\[never\]' /sys/kernel/mm/transparent_hugepage/defrag; then
    report PASS "thp defrag" "never"
  else
    report FAIL "thp defrag" "$(cat /sys/kernel/mm/transparent_hugepage/defrag)"
  fi
fi

# busy poll sysctls
for k in net.core.busy_poll net.core.busy_read; do
  v=$(sysctl -n "$k" 2>/dev/null || echo "?")
  if [[ "$v" =~ ^[0-9]+$ ]] && (( v > 0 )); then
    report PASS "$k" "$v µs"
  else
    report WARN "$k" "0 or unavailable (got: $v)"
  fi
done

# RT throttling escape hatch: must NOT be -1 (unlimited) — SIGTERM/kill-drill
# needs the non-RT breathing room.
rt=$(sysctl -n kernel.sched_rt_runtime_us 2>/dev/null || echo "?")
if [[ "$rt" == "-1" ]]; then
  report FAIL "sched_rt_runtime_us" "-1 (unlimited RT — a runaway spin could wedge the box)"
elif [[ "$rt" =~ ^[0-9]+$ ]]; then
  report PASS "sched_rt_runtime_us" "$rt"
else
  report WARN "sched_rt_runtime_us" "$rt"
fi

# IRQs on isolated cores? (movable IRQs should sit on housekeeping cores)
bad_irqs=0
if [[ -d /proc/irq ]]; then
  for d in /proc/irq/[0-9]*; do
    aff=$(cat "$d/smp_affinity_list" 2>/dev/null) || continue
    # crude overlap test: any listed CPU inside the isolated range?
    if python3 - "$aff" "$CORES" <<'EOF'
import sys
def expand(s):
    out = set()
    for part in s.split(','):
        if '-' in part:
            a, b = part.split('-'); out.update(range(int(a), int(b) + 1))
        elif part:
            out.add(int(part))
    return out
sys.exit(0 if expand(sys.argv[1]) & expand(sys.argv[2]) else 1)
EOF
    then bad_irqs=$((bad_irqs + 1)); fi
  done
  if (( bad_irqs == 0 )); then
    report PASS "irq affinity" "no IRQs on cores $CORES"
  else
    report WARN "irq affinity" "$bad_irqs IRQ(s) include cores $CORES (some are unmovable — check os-tune-apply output)"
  fi
fi

# rtprio available to this user (for SCHED_FIFO without root)
rtprio=$(ulimit -r 2>/dev/null || echo "?")
if [[ "$rtprio" =~ ^[0-9]+$ ]] && (( rtprio >= 80 )); then
  report PASS "ulimit rtprio" "$rtprio"
else
  report WARN "ulimit rtprio" "$rtprio — SCHED_FIFO will WARN+degrade unless the service grants CAP_SYS_NICE/LimitRTPRIO"
fi

exit $fail
