# Box readiness — what each gate defends against

Carried forward from the retired phone-event-prep skill (2026-08-05); updated
2026-08-12 for the RunPlan-era install path. `scripts/box-ready.sh` is
read-only by default and emits `state/<EVENT_ID>/box-ready.json`. Exit 0 = GO,
1 = NO-GO, 2 = usage.

The box: `ubuntu@3.212.174.94` (instance `i-04c50d76572aa092e`, c6a.4xlarge,
16 cores), key `~/.ssh/earshot-useast1.pem`, repo `~/ear`, TLS host
`media.3-212-174-94.sslip.io`. The Elastic IP is stable since 2026-07-29;
`box-ready.sh` and `deploy-box.sh` resolve it live from the instance id when
the AWS CLI is available.

**The laptop repo is `$HOME/Code/ear`. The box repo is `$HOME/ear`.** A path
hardcoded for one silently yields empty on the other — a host-absolute
`ACCOUNT_ENV` path killed both CAVA and CRWV dials on 2026-08-11, and
`eventctl.py` now refuses host-absolute env paths outright.

## 2026-08-12 update — scheduling is RunPlan-bound

`scripts/box-event-install.sh` now **requires
`--run-dir var/runs/<EVENT_ID>/<RUN_ID>`** (except `--disarm`); the
`--event-env`-only form is refused: "scheduling by mutable event env alone is
no longer supported". It transfers the verified RunPlan + receipt prefix
through `scripts/run-host-boundary.py`, pushes `state/hunt/numbers.env` over
**stdin** (never argv — argv is visible in `ps`), asserts config `bind` ==
env `BIND_PORT` == `/e/<port>` in `EARSHOT_STREAM_URL`, and re-reads the
**loaded** unit. `deploy-box.sh` additionally excludes `var/runs/` — the box
owns the authoritative receipt extension, and a `--delete` deploy would replace
the box ledger with the laptop's shorter prefix.

## Source sync

`scripts/deploy-box.sh --check-only` must show no run-affecting differences
(`src/`, `scripts/`, `configs/`, `deploy/`, `tests/`, `Cargo.toml`,
`Cargo.lock`). `~/ear` is an rsync'd copy, not a git checkout — provenance is
whatever the laptop was at last deploy; preflight records
`commit=UNKNOWN-NOT-A-GIT-CHECKOUT` and WARNs.

Excluded: `target`, `models`, `.git`, `state`, `corpus`, `var/runs`, and
**`vendor`** — `vendor/` is platform-specific; shipping macOS onnxruntime to
the Linux box caused the Ford 9-minute deaf gap **twice**. A missing lib and a
wrong-platform lib hang identically and silently in `ort`'s dlopen.

## Binary provenance — the 2026-08-05 blocker

The box binary must be **newer than every file under `~/ear/src`, `Cargo.toml`,
`Cargo.lock`**. On 26AUG05 it was a day stale and nothing noticed until T-60.
Separately: `MENTION_FILL_CAP_CENTS` must agree between box and laptop
`src/risk.rs` — the validator refuses any cap above the value **compiled into
the binary**, so a config written against 97 dies instantly on a 50 binary.

## Cap arithmetic

Two independent ceilings, both from code: (1) `max_price_cents ≤
MENTION_FILL_CAP_CENTS` or the whole run refuses before any order; (2)
`max_price_cents × budget_contracts ≤ [risk].max_order_notional_cents` or every
order refuses on OrderNotional. At cap 97 with a 100¢ notional cap,
`budget_contracts` must be 1.

## Detector runtime

- Linux `libonnxruntime*.so*` under `~/ear/vendor`, no `*.dylib`, and never a
  `dSYM` path (the 26AUG03 hang).
- `models/<model_dir>` present for the config's engine — `models/` is
  gitignored AND rsync-excluded; fetch per host with `scripts/fetch-models.sh`.

## TLS ingress

- `caddy` active; cert not near expiry (renewal at 30 d; under 10 = failing).
- **`/e/<the config's bind port>` routed in the INSTALLED
  `/etc/caddy/Caddyfile`** — repo file = intent, box file = truth (the repo
  routed 8080–8085 while staging allocated up to 8103; most lanes would have
  404'd).
- Caddy must never rewrite path or Host — Twilio signs the exact stream URL and
  any rewrite 403s every handshake.

## Credentials (never deployed, always host-local)

| path | holds |
|---|---|
| `~/.kalshi/earshot_env` | Kalshi key id + private key path |
| `~/.kalshi/mail_env` | Gmail app password for the mailer |
| `~/.earshot/<acct>.env` | the event's Kalshi + Twilio bundle |
| `~/.earshot/registration.env` | the standing analyst identity |
| `state/hunt/numbers.env` | probed digits, 0600, gitignored |

The account bundles share ONE Kalshi key and one Twilio SID — they buy state
isolation, not rate-limit isolation. `state/` never arrives by deploy; digits
arrive over stdin at schedule time.

## Clock / scheduler / host

- `timedatectl`: NTP active + synchronized (OnCalendar is box-local = UTC).
  Current box drift rate: ~23.9 ppm — two clock anchors, never one, for any
  race arithmetic.
- `loginctl show-user ubuntu` must show `Linger=yes` or user timers die with
  the ssh session.
- The `systemd --user` PATH has **no `~/.cargo/bin`** — pinned binary only.
  `python3` under that PATH must import `tomllib` (it does: one 3.12.3
  interpreter).
- load1 vs nproc: FAIL >2×, WARN >0.75×. Disk ≥5 GiB (≥6 when `--repair`
  builds). Never delete `target/release`.

## `--repair`

Refuses while `src/`, `Cargo.toml`, or `Cargo.lock` are dirty, then deploys +
rebuilds. After a repair, **re-run without `--repair`** for a clean GO — the
repair does not re-derive the verdict it just invalidated.
