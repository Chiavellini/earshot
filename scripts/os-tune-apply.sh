#!/usr/bin/env bash
# Apply the RUNTIME-settable OS tuning knobs (docs/OS-TUNING.md). Root, Linux,
# idempotent — safe to re-run; every setting is logged. Boot-time knobs
# (isolcpus/nohz_full/rcu_nocbs) can't be set here: this script only PRINTS
# the grub line for those, it never edits grub itself.
#
#   sudo scripts/os-tune-apply.sh [--cores 2-5]
set -euo pipefail

CORES="2-5"
[[ "${1:-}" == "--cores" ]] && CORES="$2"

[[ "$(uname -s)" == Linux ]] || { echo "Linux only" >&2; exit 2; }
[[ $EUID -eq 0 ]] || { echo "run as root (sudo)" >&2; exit 2; }

echo "== runtime knobs =="

# CPU governor → performance (skip quietly when hypervisor-controlled)
if [[ -d /sys/devices/system/cpu/cpu0/cpufreq ]]; then
  for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
    echo performance > "$g"
  done
  echo "governor: performance on all cores"
else
  echo "governor: no cpufreq sysfs (hypervisor-controlled) — skipped"
fi

# THP defrag off (compaction stalls)
if [[ -w /sys/kernel/mm/transparent_hugepage/defrag ]]; then
  echo never > /sys/kernel/mm/transparent_hugepage/defrag
  echo "thp defrag: never"
fi

# Socket busy-polling + RT escape hatch
sysctl -qw net.core.busy_poll=50 net.core.busy_read=50
sysctl -qw kernel.sched_rt_runtime_us=980000
echo "sysctls: busy_poll/busy_read=50µs, sched_rt_runtime_us=980000"

# Move movable IRQs off the isolated cores. Housekeeping mask = all cores
# minus $CORES. Unmovable IRQs (per-CPU, chained) fail silently — reported.
echo "== irq affinity (movable IRQs → housekeeping cores) =="
mapfile -t all < <(seq 0 $(( $(nproc) - 1 )))
isolated=$(python3 -c "
s='$CORES'; out=set()
for p in s.split(','):
    a,*b = p.split('-'); out.update(range(int(a), int(b[0] if b else a)+1))
print(' '.join(map(str, sorted(out))))")
housekeeping=""
for c in "${all[@]}"; do
  [[ " $isolated " == *" $c "* ]] || housekeeping+="${housekeeping:+,}$c"
done
echo "housekeeping mask: $housekeeping"
moved=0 unmovable=0
for d in /proc/irq/[0-9]*; do
  if echo "$housekeeping" > "$d/smp_affinity_list" 2>/dev/null; then
    moved=$((moved+1))
  else
    unmovable=$((unmovable+1))
  fi
done
echo "irqs: $moved moved, $unmovable unmovable (per-CPU/chained — expected)"

echo
echo "== boot-time knobs (NOT applied — add to GRUB_CMDLINE_LINUX and reboot) =="
echo "  isolcpus=$CORES nohz_full=$CORES rcu_nocbs=$CORES transparent_hugepage=madvise"
echo
echo "verify with: scripts/os-tune-check.sh --cores $CORES"
