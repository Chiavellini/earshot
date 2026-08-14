# Work order H — HOST: the box can run a webcast arm

worker: box-host-lane · branch: `webcast-lane` · as_of 2026-08-13
Master: `docs/work/BOX-WEBCAST.md` (seams S-C, S-F bind this lane).
Ledger ids reference `docs/SYSTEM-STATE.md` §3.

## Mission

Prove — with gates, not hope — that the us-east-1 box can execute a webcast
arm: the tools exist, the identity resolves, the model and ORT runtime are
present, and the pinned binary an arm demands is built ON the box by
preflight. Resolver transport is lane R's job; scheduling is lane S's.
This lane makes `box-ready.sh --config <webcast cfg>` mean something and
makes preflight-on-box the supported pin path (seam S-C — decided; no
narrow `state/` push, no exclude changes, E3 stays law).

## What already exists — do not rebuild

- `launch-webcast.sh` needs NO portability work (master §2). Do not touch.
- `preflight-event.sh` is nearly box-ready: Darwin gates guarded
  (`:327-329`), Linux ORT resolution (`:405-412`), non-git checkout WARN
  (`:361`), webcast-only Twilio skip (`:579`). It already fixed one real
  Linux bug found "running this on the us-east box" (`:455-462`).
- `box-ready.sh` lane-agnostic gates that already work for webcast configs:
  binary staleness + `MENTION_FILL_CAP_CENTS` agreement (`:299-311`), Linux
  ORT presence (`:336`), model presence per `[detector]` (`:346` — the
  config parse at `:98-127` is lane-agnostic), disk ≥5 GiB (`:257-265`),
  NTP (`:268-273`), scheduler-env probe (`:410-416`).
- `fetch-models.sh` is Linux/x86_64-pinned since 2026-07-17 (`:22-23`).
- `~/.kalshi/mail_env` is provisioned on the box (`box-ready.sh:200-202`) —
  paging mail works; osascript/say are skipped on Linux by design
  (`notify.sh:54-62`).
- `execution-profile.sh` exists for host identity; the box's canonical id
  is `earshot-useast1` (`install-event-timer.sh:6`, `execution-profile.sh:11`).

## Work items (priority order)

1. **H1 — webcast-capable `box-ready.sh`.** A webcast config must reach a
   meaningful GO/NO-GO, not a WARN cascade: probe `ffmpeg`, `curl`,
   `streamlink`, `yt-dlp`, `node` (the `--js-runtimes node` dependency) on
   the box; make the Caddy/TLS/`/e/<port>` gates phone-conditional (today a
   webcast config degrades them to WARN at `:138`/`:377`); keep
   `state/<E>/box-ready.json` schema-compatible. GO for a webcast config
   must mean "this box can run THIS event's legs now."
2. **H2 — preflight-on-box path.** Verify `preflight-event.sh` end-to-end
   on the box over SSH from a clean deploy: pin lands at box
   `state/<E>/earshot-pinned` + `pinned.info`; detector-smoke passes with
   the Linux ORT; fix what breaks. Add the `DEPLOYED-FROM` provenance stamp
   to `deploy-box.sh` (commit + dirty count written into the box repo root)
   so `pinned.info` on a non-git checkout can cite real provenance — the
   stamp is an ADDITION; the `state/`/`var/runs/` excludes are untouchable
   (E3).
3. **H3 — host identity + account fingerprint.** Configure the webcast
   account env on the box so `execution-profile.sh resolve` returns
   `earshot-useast1` + the account fingerprint
   (`launch-webcast.sh:331-346` refuses on either mismatch). Document in
   the contract doc that `execution_host_id` is baked into `plan_sha256` —
   box events are compiled for the box at init (master invariant 2).
4. **H4 — models + ORT proof.** Run `fetch-models.sh` on the box for the
   webcast engine's `model_dir`; assert a Linux `libonnxruntime*.so*` and
   zero `*.dylib` (the deploy invariant at `deploy-box.sh:59-66`); fix the
   unparenthesized `find` at `fetch-models.sh:40` (the `-o` binds wrong —
   hint-line only, still wrong). Evidence: `box-ready.sh` model gate green
   against the real webcast config.
5. **H5 — post-run orphan sweep (low priority).** The box once held 18
   orphaned fetchers (`docs/LATENCY-PROOF.md:3318`); launch-webcast's group
   kill now prevents new ones, but add a cheap post-run check to
   `box-ready.sh` (report, don't kill) so regressions are visible.

## Owned nodes (claim under these; one open claim per node)

- `platform.deploy` — extend the existing open claim (`box-ready.sh`,
  `deploy-box.sh`, `docs/nodes/platform-deploy.md`).
- `control.preflight` — extend the existing open claim
  (`preflight-event.sh`, `docs/nodes/control-preflight.md`).
- `fetch-models.sh` and `execution-profile.sh`: check
  `architecture/nodes.toml` for the owning node FIRST; extend that node's
  open claim. If it is owned by a node another lane holds, boundary
  request — do not edit.

## Forbidden zones

- `scripts/schedule-nightly.sh`, `scripts/install-event-timer.sh`,
  `scripts/box-event-install.sh`, `scripts/schedule-proof.py` → lane S.
- `scripts/access-plan.py`, `scripts/arm-on-url.sh`, resolver scripts →
  lane R.
- The `--exclude state` / `--exclude /var/runs/` lines in `deploy-box.sh`
  (E3 — additions yes, exclude changes never).
- `scripts/launch-webcast.sh`, `src/**`, `src/risk.rs` money constants,
  `.claude/skills/**` (propose BOX-CHECKS.md text in your close-out).

## Rules

- Claim per `AGENTS.md` in `architecture/claims.toml` before touching a
  node; contract doc must match code before a claim closes.
- `docs/STATUS.md` / `docs/work/ACTIVE.md`: append your own section only.
- Money policy (§4) untouched; no dial, no arm against a live board — host
  proof uses drills and read-only probes only.
- Box work goes through `deploy-box.sh` + SSH; never hand-edit box files
  that a deploy would overwrite (fix the source, redeploy).

## Acceptance

- `box-ready.sh --config <real webcast cfg>` reaches GO on the real box,
  with the new tool probes green and phone-only gates correctly
  conditional.
- A real webcast config preflights on the box: pin + `pinned.info`
  (citing `DEPLOYED-FROM`), detector-smoke green, degradations loud.
- `execution-profile.sh resolve` on the box returns `earshot-useast1` +
  the webcast account fingerprint; `launch-webcast.sh`'s identity gate
  passes in a dry `--wait-live`-less invocation.
- Models + Linux ORT proven present for the armed engine; no dylib.
- Verification: `bash -n` on every touched script, `box-ready.sh --check`
  offline selftests where they exist, `python3 tests/preflight_proof.py`,
  `scripts/doc-lint.sh`, `python3 tests/ops_surface.py`, plus every new
  gate exercised once against the real box with output preserved.
