# Box readiness — what each gate defends against

`scripts/box-ready.sh` is read-only by default and emits
`state/<EVENT_ID>/box-ready.json`. Every gate below exists because something
specific went wrong. Exit 0 = GO, 1 = NO-GO, 2 = usage.

The box: `ubuntu@3.212.174.94` (instance `i-04c50d76572aa092e`, c6a.4xlarge,
16 cores), key `~/.ssh/earshot-useast1.pem`, repo `~/ear`, TLS host
`media.3-212-174-94.sslip.io`. The Elastic IP was attached 2026-07-29 and is
stable; before that the address moved three times in three days and was hardcoded
stale in several places, so `box-ready.sh` and `deploy-box.sh` both resolve it
live from the instance id when the AWS CLI is available.

**The laptop repo is `$HOME/Code/ear`. The box repo is `$HOME/ear`.** A path
hardcoded for one silently yields empty on the other — this is exactly how
`configs/tln-26aug05-autorun.env` would have resolved `TO_NUMBER` to nothing on
the box.

---

## Source sync

`scripts/deploy-box.sh --check-only` (a plain `rsync --dry-run`) must show no
**run-affecting** differences: `src/`, `scripts/`, `configs/`, `deploy/`,
`tests/`, `Cargo.toml`, `Cargo.lock`. Docs, `report.json`, and session locks
differ constantly and are reported as a WARN, not a blocker.

`~/ear` is an **rsync'd copy, not a git checkout**. That is deliberate — but it
means the box cannot tell you what commit it is running, so provenance is
whatever the laptop was when `deploy-box.sh` last ran. Preflight records
`commit=UNKNOWN-NOT-A-GIT-CHECKOUT` there and WARNs.

`deploy-box.sh` excludes `target`, `models`, `.git`, `state`, `corpus`, and
**`vendor`**. `vendor/` is platform-specific: shipping the macOS onnxruntime to
the Linux box caused the Ford 9-minute deaf gap **twice**, and once `--delete`
removed the box's Linux ORT entirely. A missing lib and a wrong-platform lib hang
identically and silently in `ort`'s `dlopen`.

## Binary provenance — the 2026-08-05 blocker

The box binary must be **newer than every file under `~/ear/src`,
`Cargo.toml`, and `Cargo.lock`**. On 26AUG05 it was built Aug 4 19:20 and the
laptop had since changed `src/risk.rs`; nothing in the chain noticed until T-60.

Related and separately checked: `MENTION_FILL_CAP_CENTS` must **agree** between
the box's `src/risk.rs` and the laptop's. `mention_fill_gate`
(`src/kalshi/validator.rs:163`) refuses any cap above the value **compiled into
the binary**, so a config written against 97 dies instantly on a 50 binary.

## Cap arithmetic

Two independent ceilings, both derived from code:

1. `max_price_cents ≤ MENTION_FILL_CAP_CENTS` — else `mention_fill_gate` refuses
   the whole run before a single order is computed.
2. `max_price_cents × budget_contracts ≤ [risk].max_order_notional_cents` — else
   `check_order` returns `Refusal::OrderNotional` on **every** order
   (`src/risk.rs`). At cap 97 with a 100¢ notional cap, `budget_contracts` must
   be 1: 97×2 = 194 > 100.

## Detector runtime

- a Linux `libonnxruntime*.so*` under `~/ear/vendor`, and **no** `*.dylib`
- the resolved path must not be a `dSYM` bundle (`ORT_DYLIB_PATH` resolving to a
  dSYM was the 26AUG03 hang)
- `models/<model_dir>` present for the config's `[detector].engine`. `models/` is
  gitignored **and** rsync-excluded — it is host-local, fetched by
  `scripts/fetch-models.sh` on each machine.

## TLS ingress

- `caddy` active, cert days-left (Let's Encrypt renews at 30 d, so under 10 means
  renewal is already failing)
- **`/e/<the config's bind port>` is actually routed in `/etc/caddy/Caddyfile`.**
  Everything else hits `handle { respond 404 }`. Until 2026-08-05 the file routed
  only 8080–8085 and 8099 while `slate-stage.sh` allocated `BIND_PORT`
  monotonically up to 8103 — so most staged phone lanes would have 404'd. The
  repo file now enumerates 8080–8098; `box-ready.sh` reads the **installed**
  `/etc/caddy/Caddyfile`, so a box that has not re-run
  `scripts/install-tls-ingress.sh` still shows as un-widened. Repo file = intent,
  box file = truth.
- Caddy must never rewrite path or Host. Twilio signs the **exact** stream URL
  (Q-M3); a `uri strip_prefix` or `header_up Host` changes the signature base and
  every handshake 403s. Preflight checks this separately.

## Credentials (never deployed, always host-local)

| path | holds |
|---|---|
| `~/.kalshi/earshot_env` | Kalshi key id + private key path (`~/.kalshi/earshot.pem`) |
| `~/.kalshi/mail_env` | Gmail app password for the mailer |
| `~/.earshot/<acct>.env` | this event's Kalshi + Twilio bundle (`ms.env`, `intc.env`, …) |
| `~/.earshot/registration.env` | the standing analyst identity |
| `state/hunt/numbers.env` | probed digits, 0600, gitignored |

`~/.earshot/stub.env` is a placeholder (`FROM=+15550000000`) — never use it.
Note the four bundles share **one** Kalshi key and one Twilio SID, so they buy
isolation of state, not of rate limits.

`state/` is rsync-excluded, so `state/hunt/numbers.env` never arrives by deploy.
`scripts/box-event-install.sh` pushes it over stdin at schedule time. This is a
WARN in `box-ready.sh`, not a blocker.

## Clock

`timedatectl` must report NTP active and synchronized. The systemd timer's
`OnCalendar` is **box-local**, which on this host is UTC — an unsynced clock
fires at the wrong minute, and a large laptop-vs-box skew means fire arithmetic
done on the laptop lands somewhere else.

## Scheduler

- `loginctl show-user ubuntu` must show `Linger=yes`. Without lingering, user
  timers die with the ssh session and never fire unattended.
- The `systemd --user` environment is probed with `systemd-run --user`, not read
  from the login shell. Measured on the box: PATH is
  `/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/games:/usr/local/games:/snap/bin`
  — **no `~/.cargo/bin`**, so a fire-time compile is impossible and the pinned
  binary is the only unattended-safe artifact. `shasum` and `timeout` are present.
- `python3` under that PATH must import `tomllib`. On the box there is one
  interpreter (3.12.3) with both `tomllib` and `plistlib`; the interpreter split
  is a **laptop** problem (see `TRAPS.md`).
- Stale `com.earshot.*` timers are listed as a WARN. Several from finished events
  are still loaded.

## Host

- load1 vs `nproc`: FAIL above 2×, WARN above 0.75×. Three concurrent arms ran at
  load ~5 on 8 cores with ring-full warnings; five concurrent is untested.
- disk ≥ 5 GiB (matching preflight's own gate), ≥ 6 GiB when `--repair` will
  build. `target/release` is already warm (~1.6 GiB of cached deps), so the
  common repair is one crate recompiled and relinked, not a cold build. There is
  **no `target/debug`** on the box to reclaim; the usual candidates are stale
  `~/*-capture` directories and `~/icefall`. Never delete `target/release`.

## `--repair`

Refuses while `src/`, `Cargo.toml`, or `Cargo.lock` are dirty, then
`deploy-box.sh` followed by `cargo build --release` on the box. AGENTS.md:
"Do not deploy or stage the shared working tree. Deployment must identify an
immutable commit or release artifact."

After a repair, **re-run without `--repair`** to get a clean GO — the repair
does not re-derive the verdict it just invalidated.
