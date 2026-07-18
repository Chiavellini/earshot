# OS tuning — the us-east event box (the "M5 OS tuning" milestone)

Purpose: make the hot path *deterministic*, not just fast. The mean was never
the problem (`exec→send` is µs-scale); the enemy is tail jitter — a scheduler
preemption, a DVFS ramp, or a C-state wake at the wrong moment is a lost race
(INGRESS-BAKEOFF.md: one 300 ms stall on the one word that matters = total
loss). Technique lineage: Red Hat low-latency tuning guide, LMAX/mechanical-
sympathy busy-spin, "The Tail at Scale."

Layers:

1. **Host tuning** (this doc + `scripts/os-tune-check.sh` /
   `scripts/os-tune-apply.sh`) — kernel boot args, governor, IRQ steering.
   Done once per box, verified before every event.
2. **Process hooks** (`src/perf.rs`, `[perf]` config section) — per-thread
   core pinning + SCHED_FIFO, applied at spawn inside `run_pipeline`. Off by
   default; a config that omits `[perf]` behaves exactly as before on any box.

Nothing here changes *what* fires — risk gates, caps, and decision logic are
untouched. This is scheduling and locality only.

## Core layout

The box needs ≥6 cores to isolate 4. Layout used by the `[perf]` examples
below (adjust to the actual box in the event config, not here):

| Core | Role | Why |
|---|---|---|
| 0–1 | housekeeping | kernel threads, IRQs, tokio 2-worker runtime (WS, presign refresher, conn warmer, persister), sshd, node_exporter |
| 2 | `core_source` | Twilio WS ingress + capture timestamps |
| 3 | `core_preproc` + `core_chunker` | both are light (µs/frame); may share one isolated core — **park mode only**: with `spin_hot_rings` every spinning consumer needs its own core (config validation enforces it; a ≥8-core box gives each its own) |
| 4 | `core_detect` | the heavy thread: sherpa decode every chunk |
| 5 | `core_executor` | the executor's dedicated current-thread runtime (order fire) |

## Boot-time knobs (grub, require reboot)

Append to `GRUB_CMDLINE_LINUX` (then `update-grub` + reboot):

```
isolcpus=2-5 nohz_full=2-5 rcu_nocbs=2-5 transparent_hugepage=madvise
```

- `isolcpus` — the general scheduler never places tasks on 2–5; only explicit
  affinity (our `sched_setaffinity`) runs there.
- `nohz_full` — no scheduler tick on the isolated cores while a single task
  runs: removes the periodic ~µs tick interruption.
- `rcu_nocbs` — RCU callbacks offloaded to housekeeping cores.
- C-states / `idle=poll`: prefer `intel_idle.max_cstate=1 processor.max_cstate=1`
  over full `idle=poll` on a cloud instance (poll burns a core per idle CPU and
  buys little under virtualization). On EC2, deep C-states are largely
  hypervisor-controlled anyway — measure with `os-tune-check.sh` before paying
  for more.

**EC2 reality check:** on virtualized instances (m7i/c7i), `isolcpus` and
affinity work as advertised, but the hypervisor can still steal time. Prefer a
dedicated-tenancy or metal instance class if the edge report shows steal
(`%st` in `mpstat`) above ~0.1%.

## Runtime knobs (`scripts/os-tune-apply.sh`, root, idempotent)

- CPU governor `performance` on all cores (kills DVFS ramp jitter).
- `transparent_hugepage/defrag = never` (compaction stalls).
- IRQ affinity: move every movable IRQ (NIC queues included) onto cores 0–1 so
  an ingress interrupt never preempts the detect thread.
- `net.core.busy_poll` / `net.core.busy_read = 50` (µs): softirq shaving for
  socket reads.
- `kernel.sched_rt_runtime_us = 980000`: SCHED_FIFO threads may use 98% of a
  period — keeps the 2% escape hatch so a runaway RT spin can't wedge the box
  (SIGTERM/kill-drill must always get through; see Rehearsal below).

### Socket options (`SO_BUSY_POLL`, `TCP_QUICKACK`) — spike result

reqwest does not expose the underlying fd or a socket-configuration hook on
its pooled connections, so per-socket `SO_BUSY_POLL`/`TCP_QUICKACK` on the
Kalshi egress would require replacing the connector (hyper custom connector
carrying `socket2` options). **Deferred**: the host-wide
`net.core.busy_poll`/`busy_read` sysctls above capture most of the win without
touching the REST layer, and the ack path (`send→hdrs` ~102 ms from CDMX) is
RTT-dominated — re-evaluate from the us-east box's edge report where the RTT
term shrinks to single-digit ms and the softirq term becomes visible.

## `[perf]` config section

```toml
[perf]
realtime      = true   # master switch; false/absent = no-op everywhere
core_source   = 2
core_preproc  = 3
core_chunker  = 3      # sharing OK in park mode; distinct cores if spinning
core_detect   = 4
core_executor = 5      # the executor's dedicated runtime thread
rt_priority   = 80     # SCHED_FIFO 1..=99
# spin_hot_rings = true  # busy-spin ring consumers (~100 ns reaction).
#   Requires realtime AND pairwise-distinct hot cores — validation refuses
#   shared cores because two spinning FIFO threads starve each other.
```

Failure policy (`src/perf.rs`): every miss (no CAP_SYS_NICE, bad core index,
untuned kernel) is a WARN and the thread runs with default scheduling — an
event run on a half-tuned box still trades. Hard validation belongs in
`os-tune-check.sh`/preflight, never the hot path.

SCHED_FIFO needs either root, `CAP_SYS_NICE`, or an rtprio ulimit. For the
systemd user service (`scripts/install-event-timer.sh`), grant it via a
drop-in on the event service:

```ini
[Service]
AmbientCapabilities=CAP_SYS_NICE
LimitRTPRIO=85
```

## Verification / rehearsal (gates, per CLAUDE.md)

1. `scripts/os-tune-check.sh` — run before every event on the box; every row
   PASS (or a consciously accepted WARN).
2. `cargo test --release -- --ignored perf_` **on the box**, with and without
   `[perf] realtime` — the on-box baseline; jitter is the p99−p50 gap in the
   printed numbers and in a paced replay's edge report.
3. **The production-mechanism rehearsal**: systemd-timer-fired run with the
   tuned config, killed with real SIGTERM under the minimal env
   (`tests/kill_sigterm.rs` posture). SCHED_FIFO must not delay SIGTERM
   handling or the kill drill — that rehearsal, not a manual run, is what
   validates `realtime = true` for an event.
4. `scripts/preflight-event.sh` the night before, as always.

An event config may only set `realtime = true` after (1)–(3) pass on that box.
