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

Host GO is necessary but not whole-event READY. After box dispatch, run
`python3 scripts/event-prep-audit.py --date YYYY-MM-DD`; a box-primary event
also needs schedule proof for every active host, the laptop failover install
proof, two loaded watchdogs, and (for `preresolved`) one PCM-gated URL-push job
that names the box and laptop together.

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
`Cargo.lock`). `~/ear` is an rsync'd copy, not a git checkout.

**Provenance is no longer unknown (2026-08-13).** After rsync and the Linux-ORT
invariant, `deploy-box.sh` atomically writes `~/ear/DEPLOYED-FROM` (mode 444)
with `commit=`, `dirty_paths=`, `deployed_utc=`. Preflight reads it and records
`provenance=DEPLOYED-FROM` in `pinned.info` instead of the old
`commit=UNKNOWN-NOT-A-GIT-CHECKOUT` WARN. Measured on the 26AUG14 box drill:

```
provenance=DEPLOYED-FROM
commit=b0c4210b295daecf03089153a2df8afe5ee819cb
dirty_paths=0
deployed_utc=2026-08-14T15:58:14Z
```

`dirty_paths` is the number to read. Deploy from a clean tree and a box pin
cites an immutable commit; deploy dirty and the pin's provenance is only as good
as that count (the HOST lane's first box pins were `dirty_paths=18` and `19`).
`deploy-box.sh` does **not** refuse a dirty tree — only `box-ready.sh --repair`
does, and only for binary-affecting paths.

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

## Webcast configs (2026-08-13/14)

`box-ready.sh --config <webcast cfg>` reaches a real GO/NO-GO, not a WARN
cascade. It additionally probes **`ffmpeg`, `curl`, `streamlink`, `yt-dlp`, and
`node`** — each missing tool is a FAIL. `node` is load-bearing because yt-dlp
runs `--js-runtimes node`.

The phone-only gates become explicit **not-applicable PASSes**, not WARNs: the
Caddy/TLS `/e/<port>` route and the digit store do not apply to a webcast
config. Schema 1 stays valid with `stream_url=null`.

A GO also reports any **parentless** webcast fetcher/decoder still running
(`ffmpeg`, `streamlink`, `yt-dlp`, `cdn-cachebust-fetch`, `hls-edge-fetch`) —
report only, it never kills them. The box once held 18 orphans. Measured on the
26AUG14 drill: a webcast config reached **GO with 27 PASS / 0 WARN / 0 FAIL**.

Run webcast preflight **on the box** after a fresh deploy, so the pin is built
where the arm will demand it (`state/<EVENT_ID>/earshot-pinned`, which
`arm-on-url.sh:139-140` requires executable — there is no cargo fallback).
Never push the laptop pin and never relax the `state/` / `var/runs/` excludes.

## TLS ingress

Phone lane only — a webcast config records these as not applicable.

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
