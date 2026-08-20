# Node: `runtime.source.pipe`

## Job

Turn a declared decoder subprocess's raw PCM stdout into a live webcast
`AudioFrame` stream.

## Owns

Decoder command spawning, stdout framing, stderr draining, idle detection,
**leg respawn policy**, child cleanup, and HLS `ffmpeg` command construction in
`src/source/pipe.rs`, plus the continuous HLS edge-fetch runtime adapter in
`scripts/hls-edge-fetch.py`, the cache-busting HLS segment adapter in
`scripts/cdn-cachebust-fetch.sh`, and their deaf-leg diagnostic in
`scripts/test-deaf-leg.py`.

### The two survivor legs (C2b)

Both adapters above are production leg commands, not emergency tools. Each held
up a real event that the standard `streamlink`/tuned-`ffmpeg` pair could not —
`hls-edge-fetch.py` on COHR 2026-08-12, `cdn-cachebust-fetch.sh` on CBRS the
same day — and each has a MEASURED blind spot that the other covers, so a
fanout should carry both rather than choosing:

| Leg | Survives | Goes deaf on |
|---|---|---|
| `hls-edge-fetch.py` | variant rotation (re-resolves master→media after 12 failed polls) | a CDN-frozen playlist — it uses plain `urlopen`, no cache-busting |
| `cdn-cachebust-fetch.sh` | a CDN-frozen playlist (unique query param + `Cache-Control: no-cache` per reload) | variant rotation — it resolves master→variant ONCE at startup and never again |

Measured against `scripts/test-deaf-leg.py`'s rotating-variant fixture (the
origin swaps `mediaA`→`mediaB` at T+10 s): the edge fetcher emitted 18 segments
before the rotation and 24 after, logging `RE-RESOLVED` / `RECOVERED`; the
cache-buster emitted 8 before and **0** after, permanently.

That second failure is now contained rather than fatal. `cdn-cachebust-fetch.sh`
is deliberately crash-proof — a curl failure sleeps and retries forever — so a
deaf fetcher emits no bytes, the 30 s idle guard fires, and the respawn re-runs
the argv from the top, which re-resolves the master. Respawn is what gives that
leg its missing re-resolve, at a bounded ~30 s cost.

Leg recipes, for a config whose URL file holds the MASTER m3u8:

```toml
[[sources]]
kind = "pipe"
label = "edge_fetch"
sample_rate_hz = 16000
cmd = [
  "/bin/sh", "-c",
  "python3 scripts/hls-edge-fetch.py \"$(cat $HOME/<lc>-stream.url)\" 250 2 | ffmpeg -loglevel warning -fflags nobuffer -flags low_delay -i - -vn -f s16le -ac 1 -ar 16000 -",
]

[[sources]]
kind = "pipe"
label = "cachebust"
sample_rate_hz = 16000
cmd = [
  "/bin/sh", "-c",
  "scripts/cdn-cachebust-fetch.sh $HOME/<lc>-stream.url 1 | ffmpeg -loglevel warning -fflags nobuffer -flags low_delay -i - -vn -f s16le -ac 1 -ar 16000 -",
]
```

Note the argv difference: the edge fetcher takes a URL, the cache-buster takes a
URL FILE. Both must receive the master, not a variant — handed a variant,
neither has a recovery path.

Adding these to the staged template is `control.prepare`'s call, not this
node's: `configs/webcast-fanout-arm.toml` and the stage path belong to that
node. The request is filed in `docs/work/ACTIVE.md`.

## Does not own

It does not resolve venue URLs, decide which fetcher is fastest, supervise an
event, implement HLS/DASH codecs, process detector audio, or choose the command
for a production posture.

## Input contracts

`PipeSource::new(cmd, sample_rate_hz, source_id)` requires a non-empty argv and
nonzero rate; the command must emit mono signed 16-bit little-endian PCM on
stdout. `PipeSource::hls` is explicit 16 kHz `ffmpeg` sugar. When the edge
fetcher is in the argv chain, it consumes the preflight-approved master URL and
emits media bytes to the declared decoder; it does not resolve venue policy.

The respawn posture is read from the environment by `respawn_policy_from_env`:
`EARSHOT_PIPE_MAX_RESPAWNS` (default 0, clamped to `MAX_RESPAWN_BUDGET` = 20)
and `EARSHOT_PIPE_RESPAWN_BACKOFF_MS` (default 2000). `scripts/launch-webcast.sh`
exports both for an armed event and echoes them into the launch log;
`scripts/arm-on-url.sh` inherits them through it. Tests set them directly with
`with_respawn_budget`. This mirrors `twilio_ws::bind` reading
`EARSHOT_REDIAL_DIR` rather than taking it as a parameter, and is deliberate
rather than preferred: the field belongs in `SourceConfig::Pipe` with two lines
in `main.rs::build_leg` — the treatment `TwilioStream::max_reconnects` gets —
and both files belong to other nodes. That request is recorded in
`architecture/claims.toml` under `root.source_pipe_contract`.

## Output contracts

PCM is emitted in 20 ms frames with dense sequence numbers and arrival-stamped
timestamps. EOF flushes a final whole-sample partial frame and returns
`EndOfStream`. Sequence numbers are hoisted ACROSS respawns and stay dense for
the whole run: a leg that restarted its numbering would falsify every
downstream gap statistic.

## Side effects and exclusive resources

It owns one child process and its stdio for the lifetime of the source.
`kill_on_drop` plus explicit termination prevents orphan decoders. Network and
file effects performed by the argv are declared by the acquisition plan.

## Failure contract

Empty commands and rates fail before spawn. A decoder lifetime ends in exactly
one of three ways — idle past `idle_timeout` while the child is still alive,
a read error, or the child exiting (EOF) — and each is a LEG DEATH, including
EOF. Treating a decoder exit as benign was a defect, not a design: it returned
`Ok(EndOfStream)`, which the fanout composer does not count as a died leg, so a
run whose legs all exited early finished with a clean record while the venue
was still live.

While respawn budget remains, any of the three respawns the decoder after
`respawn_backoff`, logging one WARN with attempt, budget, reason, stderr tail
and frames delivered so far, so a grader can count the gaps. Each attempt gets
a fresh child, stderr-drain task and tail buffer. Re-running the argv is also
how a fresh URL reaches the leg: production commands are
`/bin/sh -c "… $(cat …-stream.url) …"`, so a respawn re-reads the URL file and
re-evaluates any cache-buster in it.

Once the budget is spent, each end returns exactly what it returned before the
budget existed — `SourceError::Protocol` naming the idle timeout with a bounded
stderr tail, `SourceError::Io`, or `Ok(EndOfStream)`. Budget 0 (the default) is
therefore the pre-2026-08-12 one-shot, byte-for-byte, which is what keeps
replay, bake-off and probe callers unchanged. Respawn is a way to survive a
sequence gap, never a way for a genuinely dead venue to look healthy.

Ring pressure produces throttled, counted ingress-drop warnings; that throttle
state is also hoisted across respawns.

## Parallel work boundary

Framing, child lifecycle, and continuous edge fetching stay in
`src/source/pipe.rs`, `scripts/hls-edge-fetch.py`,
`scripts/cdn-cachebust-fetch.sh`,
`scripts/test-deaf-leg.py`, and `tests/pipe_source.rs`. URL resolution and
command/posture selection belong to `control.acquire`; changing PCM format
requires the source-port contract.

## Verification

Run `cargo test --test pipe_source`, focused `cargo test source::pipe` tests,
and `python3 scripts/hls-edge-fetch.py --selftest`, including idle timeout,
child teardown, partial frame, and ring pressure. The respawn contract is
covered by four suite cases: a dead decoder respawning with dense seq across
the boundary, an idle (alive-but-silent) leg recovering, budget 0 spawning
exactly once, an over-large budget clamping to `MAX_RESPAWN_BUDGET`, and — at
pipeline altitude — `run_pipeline` continuing to capture after a real `kill -9`
of the decoder mid-stream. Note that the pipeline returns `Ok` in that case
even WITHOUT respawn, so the assertions are the respawn count and the frames
that arrive after the kill, never the exit status.

`python3 scripts/test-deaf-leg.py` exercises the edge fetcher against a
rotating-variant origin and the cache-busting fetcher against a ROST/CBRS-shaped
origin whose ordinary media-playlist response is frozen while unique `_cb`
reloads advance. Both subtests must report PASS. Running the rotating-variant
half against `scripts/cdn-cachebust-fetch.sh` is still expected to go deaf after
the rotation; that is the documented blind spot above, not a regression.

## Migration status

The generic adapter is isolated, but production commands are still embedded in
TOML and staging shell scripts. Move them behind a structured
`AcquiredMedia { argv, pcm_format, provenance }` artifact instead of shared
environment and URL-file conventions.
