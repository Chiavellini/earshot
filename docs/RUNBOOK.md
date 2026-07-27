# NEXT EVENT — Monday 2026-07-20, Domino's (DPZ) Q2 call — PHONE-FIRST (number hunt)

> **2026-07-16, revised three times.** Per-issuer access now lives in
> `configs/dial-ins.toml` + `docs/DIAL-INS.md` — **that registry is the source of
> truth; this blockquote is a summary.**
>
> (1) DPZ 07-20, GEV 07-22, TSLA 07-22, INTC 07-23 — and UAL — publish NO public
> dial-in; announcements are webcast-only. (2) BUT the operator corrected the read:
> a participant phone bridge EXISTS for most — the analyst Q&A runs by phone.
> Verified 2026-07-16 against primary sources + verbatim transcripts:
> **DPZ + INTC** operators say "press star 11 on your telephone" (real PSTN bridge)
> — CONFIRMED. **TSLA alone** has NO phone leg (no operator at all; IR runs
> "raise hand"/"unmute") — CONFIRMED.
>
> Two corrections to the previous revision:
> - **GEV is NOT "a moderated bridge (analysts pre-cleared)"** — that was asserted
>   without evidence and is **refuted**. GEV runs the *same* self-service queue as
>   DPZ/INTC ("Please press 11 on your telephone"). There is a coordinator, but no
>   pre-clearance. Do not model GEV as structurally different.
> - **⚠ Do NOT group UAL with TSLA.** The old line "UAL/TSLA confirmed webcast-only
>   in their own 8-K exhibits" is literally true and **dangerously misleading**.
>   UAL's 8-K says "a live, listen-only webcast" — phrasing identical in kind to
>   Tesla's — yet UAL's operator runs a live phone Q&A ("press star, then the number
>   one on your telephone keypad"). **8-K silence is not evidence of absence.**
>   Acting on that grouping forfeits a tradeable event. The tell that actually
>   separates them: Tesla's call has **no operator at all**.
>
> Also newly tracked: **GOOGL 07-22** and **AAL 07-23** have open mention events that
> this runbook previously omitted. GOOGL is the largest near-term name — bridge
> confirmed from Alphabet's own transcript ("press \*1 on your telephone"), number
> unpublished, so it needs the same Track A hunt as DPZ.
>
> Phone is preferred — whoever holds the bridge number beats every webcast listener.
> ⚠ The "~18-30 s" webcast lag previously quoted here is **unsourced** (`:246` and
> `docs/TWILIO.md:66` both call it an unverified estimate; the "18" appears nowhere
> else). **Quote no number until it is measured.** Monday's DPZ three-leg
> capture is designed to produce the first measured numbers: manifest-probe
> PDT lag (absolute, encoder-clock-bounded) + tuned-vs-default xcorr delta.
>
> **How to actually measure it (and why NFLX cannot).** The lag is a *difference*
> between two paths, so it needs a call carrying BOTH a phone bridge and a webcast,
> captured simultaneously and cross-correlated. **NFLX is exactly the wrong subject:
> it has no phone leg, so there is no reference to subtract from** — the 2026-07-16
> capture (`corpus/nflx-q2-2026.wav`, 39:25, verified speech throughout) is corpus
> material only, NOT a latency datapoint. An earlier revision of this line claimed it
> would yield "the first real measurement"; that was wrong.
> Run the measurement on a **Tier-A published-number** issuer instead (MSFT, or a
> bank — `configs/dial-ins.toml`), where both paths are reachable at once.
>
> The webcast (HLS) adapter is **NOT built** (`src/main.rs`: `SourceConfig::Hls =>
> Err("HLS adapter not built")`), and no design artifact exists — a previous revision
> claiming the design was "COMPLETE but DEFERRED" was an overclaim. Operator decision
> 2026-07-16: phone-only this week. **TSLA and NFLX are therefore untradeable** — not
> deferred by choice, but with no capture path at all.
>
> **DPZ number hunt (Track A) — the event is GO only if one lands by 07-19:**
> 1. IR ask: investorrelations@dominos.com (VP IR Greg Lemenchick) — operator
>    sends from their own account.
> 2. Legacy operator numbers from the 2015 8-K: **888-400-9978 /
>    +1-706-634-4947, "ask for the Domino's Pizza conference call"** — probe
>    via a user-approved Twilio bridge call (operator live on the leg), 07-17.
> 3. Call-morning: the live webcast player often shows a "listen by phone"
>    tab. ⚠ ir.dominos.com (and ir.united.com) GEO-BLOCK this box (Akamai,
>    Mexico) — needs a US VPN or the operator's manual check.

## Track A execution kit (prepared 2026-07-16)

### 1. IR email — ready to send (operator's own account, TODAY)

To: `investorrelations@dominos.com` · Subject: **Dial-in access for the Q2
2026 earnings call (Monday, July 20)**

> Dear Domino's Investor Relations,
>
> I plan to follow Monday's Q2 2026 earnings conference call (July 20,
> 8:30 a.m. ET) live. The announcement lists the webcast, but streaming is
> unreliable where I will be that morning — is a telephone dial-in available
> to listen to the call? Listen-only access is all I need.
>
> Thank you very much,
> Bernardo Del Rio

Notes: no shareholder claim is made (we hold none); listen-only is stated
up front. If IR replies with a registration link instead of digits, register
immediately — platform-issued dial-in + PIN counts as a number landing.

### 2. Legacy-number probe — Thursday 2026-07-17, US business hours (ET)

⚠ USER-APPROVED, OPERATOR LIVE ON THE LEG. Do not run unattended; a human
answers. Two-leg bridge so the call works from Mexico and presents the US
Twilio caller ID: Twilio calls YOUR phone first; when you answer it bridges
you to the legacy number.

```bash
source ~/.twilio/earshot_env          # or the per-event account env
YOUR_PHONE="+52..."                   # the phone you will answer
LEGACY="+18884009978"                 # attempt 1; attempt 2: +17066344947
curl -s -X POST "https://api.twilio.com/2010-04-01/Accounts/${TWILIO_ACCOUNT_SID}/Calls.json" \
  --data-urlencode "To=${YOUR_PHONE}" \
  --data-urlencode "From=${TWILIO_FROM_NUMBER}" \
  --data-urlencode "Twiml=<Response><Dial callerId=\"${TWILIO_FROM_NUMBER}\">${LEGACY}</Dial></Response>" \
  -u "${TWILIO_ACCOUNT_SID}:${TWILIO_AUTH_TOKEN}"
```

Say: *"Hi — I'm calling about the Domino's Pizza conference call on Monday,
July 20. Could you give me the dial-in details to listen to that call?"*

**OUTCOME 2026-07-17: BOTH legacy numbers DEAD.** 888-400-9978 → reassigned
to an unrelated marketing line (medical devices). +1-706-634-4947 → dead
(evening probe, user live on the leg). **IR EMAIL ANSWERED same day:** "All
public access to our earnings call is through our website" + a link to the
events page — the definitive polite no. Track A is exhausted except the
Sunday page check and the Monday player check. **The operative Monday plan
is the WEBCAST PIVOT** (capture-only from the us-east box — see the
webcast-lane sections below and the plan file): zero creds, zero orders,
corpus + cadence + virtual-race analysis against the public Kalshi tape.
The phone path runs only if a number lands Sunday/Monday.

Record, whichever way it goes: (a) does the number still route to a
conferencing operator at all, and which provider (greeting usually names it);
(b) participant number + passcode if offered → `configs/dpz-autorun.env`;
(c) "operator-assisted, ask for the call by name" → set `TO_NUMBER` to the
legacy number, `PASSCODE=""`, and note that the autorun DTMF path is unused —
an operator-assisted join needs a human on the morning leg, which demotes
the event from autonomous to attended; (d) dead/reassigned → step 3 is the
last chance. Hang up politely either way; one probe per number, no retries
(these are staffed lines).

### 3. Manual checks only the operator can do (geo-blocked box)

- **07-17/18, US VPN**: ir.dominos.com → Events → the Q2 2026 call entry.
  Look for any "listen by phone" / "telephone access" / registration link
  that the SEC-visible announcement lacks.
- **07-20 ~06:00–06:20 local**: open the live webcast player (US VPN) — the
  player chrome sometimes exposes a phone tab only once the event is live.
  If digits appear: update `configs/dpz-autorun.env`, then
  `launchctl kickstart` is NOT needed — the 06:05 fire reads the env at
  launch; before 06:05 an edit is safely picked up.
- After ANY number lands: fill `TO_NUMBER`/`PASSCODE`, run
  `scripts/preflight-event.sh --config configs/twilio-live-dpz.toml
  --account ~/.earshot/ms.env --fire-hhmm 0605` the night of 07-19.
  Preflight/supervisor FAIL-STOP on TBD is already validated.
>
> - **Call**: Monday 2026-07-20, 8:30 AM ET = 6:30 AM CDMX local. Fire 06:05,
>   wake 06:00, END_HHMM 0740, COMPLETE_EARLIEST 0710.
> - **Markets**: `KXEARNINGSMENTIONDPZ-26JUL20` — 14 tradeable (prepared +
>   compile-keywords + dry-run test-arm 14/14 PASSED 2026-07-16; DoorDash
>   "5+ times" auto-skipped). Config: `configs/twilio-live-dpz.toml`
>   (tiered fillable: TARI only, cap 35¢). Env: `configs/dpz-autorun.env`
>   (TO_NUMBER/PASSCODE TBD — the supervisor refuses a live fire while TBD).
> - **Supervisor**: `scripts/event-autorun.sh` (generalized 2026-07-16,
>   follow-up 3 closed) driven by a per-event env file named in the launchd
>   plist via `EARSHOT_EVENT_ENV`; launchd-fired rehearsal PASSED 2026-07-16
>   08:53 (env sourced, TTS dial, 7124 frames, 3 detections, rc=0).

## DPZ MONDAY — webcast-capture choreography (pivot; phone path only if a number lands)

Everything below is STAGED and smoke-tested on the us-east box (2026-07-17:
capture-bakeoff ran 2 legs → 20 MB each, distinct files, xcorr peak 1.00;
identical-label filename collision found and fixed — legs are now pipe+hls).
Zero creds on the box, zero orders possible. Worst case = lost recording.

**Sunday evening:** start the box (`aws ec2 start-instances --instance-ids
i-04c50d76572aa092e`), note the new IP, `ssh ubuntu@<ip>` sanity: `ls
~/run-dpz-capture.sh ~/ear/configs/webcast-dpz-capture.toml`. Also the
LAST phone check: the events page via the user's US-VPN browser — a number
→ abort pivot, run the drilled phone path (preflight etc.).
⚠ The 07-17 tuned/streamlink-leg changes REQUIRE a Sunday rebuild + re-smoke
on the box (the pre-change binary rejects the new `label` config field at
parse):
1. `cd ~/ear && git fetch && git checkout webcast-lane && git pull &&
   cargo build --release`
2. Install streamlink (leg 4): `pipx install streamlink` (or
   `apt install streamlink`); `streamlink --version` (flags pinned on 8.4.0).
3. `ffmpeg -h demuxer=hls | grep -E 'live_start_index|http_persistent'` —
   confirm the pinned defaults (config comment) on the box's ffmpeg version.
4. Re-smoke the FOUR-leg config against a real **https** HLS stream (a
   direct non-YouTube manifest — YouTube bot-walls the box, W1-pre; the
   tuned leg's -reconnect flags are http(s)-only and exit 8 on file
   inputs): `echo "<m3u8>" > ~/dpz-stream.url`, sed a COPY of the config,
   `capture-bakeoff --secs 90 --record-dir ~/dpz-smoke` → expect 4 distinct
   WAVs, `pipe → pipe_tuned` ≈ −(2 × cadence) and `pipe → streamlink`
   slightly larger, peaks > 0.5, plus a `scripts/manifest-probe.sh` run.
   A dead leg 3/4 in the smoke → delete that TOML block, legs 1–2 are the
   drilled baseline; do NOT ship an unsmoked leg to Monday.
5. `free -h` — four in-RAM legs ≈ 870 MB for 110 min.
6. While on the box (largest-gap program, Track 3): `scripts/os-tune-check.sh`,
   `cargo test --release -- --ignored perf_` (perf baseline + chunk ladder
   RTF — closes on-box gates 1–2 for the TSLA armed run).

**Monday 06:10–06:25 CDMX (8:10–8:25 ET):**
1. User opens ir.dominos.com → events → Q2 webcast player (US VPN, Chrome).
2. Claude pulls the HLS manifest URL from the browser's network requests
   (claude-in-chrome `read_network_requests`, filter `.m3u8`).
3. On the box: `echo "<m3u8>" > ~/dpz-stream.url` and
   `sed -i "s|__DPZ_URL__|<m3u8>|" ~/ear/configs/webcast-dpz-capture.toml`
   (leg 3 `pipe_tuned` needs no sed — it reads `~/dpz-stream.url` like leg 1).
4. `~/ear/scripts/manifest-probe.sh "<m3u8>" | tee ~/dpz-capture/manifest-probe.txt`
   — from the box (segment URLs can be requester-IP-locked): cadence
   (TARGETDURATION/EXTINF), LL-HLS tags present or not, and if the manifest
   carries EXT-X-PROGRAM-DATE-TIME, the ABSOLUTE manifest-edge lag. Re-run
   once mid-call. This output also decides the streamlink/LL-HLS follow-on.
5. `~/run-dpz-capture.sh` (refuses if the URL/placeholder steps were missed).
   110-min capture → `~/dpz-capture/{pipe,hls,pipe_tuned,streamlink}.wav +
   .jsonl + report.json`. Legs 3–4 are the low-latency experiments (tuned
   ffmpeg / streamlink) — if either dies, legs 1–2 are unaffected; do NOT
   debug them during the call.
6. Watch `~/dpz-capture/run.log` for "stream started"-class output.

**Post-call (CDMX):** scp the WAV; replay through the detector with the DPZ
config vocabulary; line detection times against the public Kalshi trades
tape (VERIFICATIONS Q-MSPM method) → the VIRTUAL RACE: were asks swept
before webcast-visible detections (bridge money present) or did they sit
(webcast = the effective floor for this event class)? Either answer sets
Wednesday's GEV/GOOGL webcast-fallback posture. Also transcribe the FIRST
MEASURED webcast-lag numbers into this file (replacing the retracted
estimate at the top): the `pipe → pipe_tuned` xcorr delta (what the
low-latency ffmpeg flags buy — expect ≈ −2 × segment cadence) and the
manifest-probe PDT lag (absolute encoder→edge, encoder-clock-bounded).

**OUTCOME 2026-07-20 (recorded that evening): THE CAPTURE NEVER RAN.**
Box boot history (`last reboot`, checked 07-20 ~19:30 CDMX after starting
the instance): last boot before that check was Fri 07-17 21:38 UTC — the
box was never started Sunday evening or Monday morning; no `~/dpz-capture/`
exists, `~/dpz-stream.url` was never written. The Sunday box session
(rebuild + streamlink install + 4-leg smoke) also never happened — those
steps roll forward to the Wed 07-22 events. NO webcast-lag numbers exist
yet; the retracted estimate stays retracted; Wednesday (GEV/GOOGL/TSLA) is
now the first measurement opportunity. The PUBLIC TAPE was still fetched
and analyzed (tape-only — no Δw possible without our capture):
`state/KXEARNINGSMENTIONDPZ-26JUL20/tape-2026-07-20.json`, findings in
COMPETITOR-ANATOMY §Per-tape results (headline: fast population essentially
ABSENT — one machine sweep all call; two YES words never swept during the
call, repriced only ~1 h later by transcript readers).

## Same-floor W1-pre findings (2026-07-17 evening) — the YouTube access terrain

Three live-measured facts that shape the webcast lane's design:

1. **CGNAT breaks direct-manifest fetching from the CDMX box.** googlevideo
   segment URLs are IP-locked (the manifest bakes in `/ip/<requester>/`);
   this connection rotates egress IPs mid-session → ffmpeg-on-manifest dies
   with TLS/IO errors. yt-dlp (which re-resolves per session) worked: 80 s
   captured. Direct-manifest strategies REQUIRE a stable-IP host.
2. **YouTube bot-walls datacenter IPs.** From the us-east EC2 box, current
   yt-dlp gets "Sign in to confirm you're not a bot" — YouTube capture from
   AWS needs authenticated cookies (`--cookies`), a residential egress, or a
   non-YouTube simulcast. Also: Ubuntu 24.04's node 18 is not accepted as a
   JS runtime by yt-dlp 2026.07 — install deno on the VPS.
3. **Cadence probe methodology works** (runs Wednesday against TSLA's
   stream): resolve manifest → `EXT-X-TARGETDURATION`/`EXTINF`. Sky News
   reference: 5 s segments.
4. **Acquisition ladder MEASURED (2026-07-17 evening, CDMX vantage,
   Sky News live via streamlink-resolved manifest, 150 s,
   `configs/webcast-ladder-bakeoff.toml`):** default ffmpeg → tuned ffmpeg
   = **−10.6 s** (xcorr peak 0.98; the predicted ~2 × 5 s-segment
   `live_start_index` win, now empirical); tuned → streamlink = a further
   **−1.1 s** (peak 1.00) with the best stall profile (42 stalls vs 61/66).
   Manifest-probe on the same stream: 5 s segments, NO LL-HLS tags,
   PDT edge lag ~36 s. Implications: (a) the tuned flag set is validated
   pre-DPZ; (b) streamlink leads the ladder on standard HLS → DPZ leg 4;
   (c) no LL-HLS on this reference — the native sub-second fetcher stays
   gated on event venues actually serving LL-HLS (Monday's probe decides).

Implications: (a) W1's strategy race needs the operator's YouTube cookies on
the VPS (ordinary yt-dlp practice, ToS-gray — operator decision) or an owned
test stream + cookies; (b) non-YouTube corporate webcast players (Notified/
Q4/media-server HLS — INTC, most issuers) have no bot-wall and are the
friendlier same-floor venue for direct low-latency fetching; (c) Wednesday's
TSLA v0 runs from the CDMX box via yt-dlp (proven path) — proximity matters
less for a v0 whose goal is cadence + corpus.

## ▶ TUESDAY 2026-07-28 · PayPal · DAY-OF CHECKLIST (execute top to bottom)

Everything below is pre-validated. **Two steps need a live decision: step 3
(surface the right stream) and step 4b (the prefetch decision table).** Step 4b
is new for 2026-07-28 — the campaign found the only `FEED` lever that works at
this venue, and which tier is safe to arm depends on what the probe reports.

**Call: 8:00 AM ET (12:00 UTC).** Config `configs/webcast-pypl-26jul28-arm.toml`
— speculative CTC fanout, 16 markets, observe posture, every cap 1¢, no fill
possible. Venue **webcast-only** (IR page + EDGAR + the Q1 2026 SEC-filed
release), platform **Q4 Inc** = AXP's ⇒ expect **Amazon IVS, 2 s segments**.

**BOTH Tuesday events are staged and keyword-validated on the CTC vocab:**

| | config | call ET | keywords | encode check |
|---|---|---|---|---|
| **PayPal** | `configs/webcast-pypl-26jul28-arm.toml` | **8:00 AM** | 16 (37 variants) | ✅ 0 gaps, shortest target **2 tokens** (`stash`) |
| **Ford** | `configs/webcast-f-26jul28-arm.toml` | **5:00 PM** | 15 (32 variants) | ✅ 0 gaps, shortest target **3 tokens** |

They do not conflict. Run PayPal first; it is the safer detection problem (long
rare words vs Ford's short common earnings language). **If only one runs, run
PayPal.** The steps below apply to both — substitute `ford` for `pypl` in the
stream-url path and config name.

**Ford's extra constraint:** its player is empty until 5:00 PM ET, so step 4's
probe cannot be done in advance and the venue class is unknown going in. Surface
the m3u8 on the **logged-in laptop** and hand the URL to the box — a
laptop-minted `fastly_token` is box-consumable (F1c: HTTP 200 from us-east).

**WEDNESDAY 2026-07-29 · FOMC / Warsh — STAGED (`configs/webcast-fed-26jul29-arm.toml`).**
Event ticker **`KXFEDMENTION-26JUL`** — *"What will Kevin Warsh say during his
July press conference"* — **found 2026-07-26; it had never been pinned.**
43 markets (≈3× Tuesday's), 83 variants, CTC encode **0 gaps**, observe posture.
Runs the **NON-speculative** legs by standing decision.

**⚠⚠ THE BOARD CONTAINS AN OPENING-GREETING MARKET.** `good afternoon` (`-GOOD`)
and `good day` are near-certain in the **first seconds** of the presser and
resolve **purely on the race**.

**RULED 2026-07-27 — non-speculative HOLDS, with one pre-registered day-of
override.** Two corrections to how this was framed:

1. **Prefetch is not the lever here.** Campaign 2's entire measured `FEED` gain is
   `#EXT-X-PREFETCH`, an **Amazon IVS / Twitch** tag. FOMC is
   **Brightcove/boltdns/Fastly**, and the archived June presser has no
   `EXT-X-PART` and no `SERVER-CONTROL`. This decision is about `--speculative`
   only — whose rate-limit risk, the basis of the standing decision, is entirely
   unchanged by the existence of a greeting market.
2. **Speculation is not instant.** `pattern_is_predictable` needs 3 consecutive
   real segments before speculation activates. At 10 s segments that is ~30 s of
   stream. Join late and you pay the risk and get none of the benefit.

**Override condition, written before the probe runs — arm `--speculative` only if
ALL FOUR hold:** (a) `== speculative posture ==` reports **GO**; (b) measured
**`L` ≥ 1.00 s** (the lowest L ever measured at any venue); (c) live cadence
**≤ 10 s**; (d) launch **≥ 60 s** before the first word. Any one failing ⇒ the
staged legs run unchanged. **Do not re-negotiate after seeing the output.**
Full text in `configs/webcast-fed-26jul29-arm.toml`.

**Recon that is free and should happen regardless:** run
`scripts/manifest-probe.sh` on the live tokenized master and pin the
`== EXT-X-PREFETCH ==` result. It costs zero extra HTTP and is the first LHLS
reading on a third venue class. (It cannot be armed as a leg flag here — both
FOMC legs are `streamlink`/`ffmpeg`, not `hls-edge-fetch.py`, so there is nowhere
to put `--prefetch-report` without changing the proven stack.)

**Highest false-alarm risk in this set (2-token targets):** `egg`, `gas`, `oil`,
and `good day`. `trump` also carries a HIGH-RISK collision flag (fires on
"strong"). `event does not qualify` is a Kalshi housekeeping market, not a phrase
Warsh will utter — harmless, but it occupies a slot.

**Full week's mention board (verified against the API 2026-07-26):**

| day | events |
|---|---|
| Tue 07-28 | **PYPL** (8:00 AM), **F** (5:00 PM) — both staged |
| Wed 07-29 | **KXFEDMENTION-26JUL** (2:30 PM, staged), **META (4:30 PM ET — see below)**, CVNA, WING |

> **▣ META IS THE BEST MEASUREMENT TARGET OF THE WEEK — venue recon 2026-07-27.**
> `https://events.q4inc.com/attendee/922331606`, **Wed 20:30 UTC** (1:30 PM PT).
> **Q4 Inc ⇒ Amazon IVS — the SAME venue class as PayPal**, so the streamed
> prefetch lever applies and Tuesday's result can be **replicated on a second
> real venue**, which is precisely what §V8/§A2 lack (both are one channel).
>
> ⚠ **The crown TRADING target and the best MEASUREMENT target are different
> events this week.** FOMC is the marquee board (43 markets) but runs on
> **Brightcove**, where none of Campaign 2's levers apply — no known LHLS path.
> They do not conflict: **FOMC 18:30 UTC, META 20:30 UTC**, ~2 h apart.
>
> AMZN (Thu) venue is **UNRESOLVED**: `ir.aboutamazon.com` renders its events
> list client-side, so WebFetch returns an empty shell. Needs a browser.
>
> Recon was done from the laptop via WebFetch, **not from the box** — deliberately,
> so nothing touches the box's IP before Tuesday's stream.
| Thu 07-30 | AMZN, RDDT, YUM, RACE (Ferrari) |

⚠ The unfiltered `/events?status=open` endpoint paginates past the mention
series — query `?series_ticker=KXEARNINGSMENTION<TICKER>` directly. And it
**429s** under rapid repeat queries; space them out.

**T−1 evening**
1. `scripts/preflight-event.sh` — pins the binary, live-probes creds, mailer,
   disk, scheduled wake. **Event runs REQUIRE this** (CLAUDE.md).

   ✅ **RUN 2026-07-27 on the box for BOTH Tuesday events.** Binary pinned for
   each (`sha256=17b3af8d02…`, same build), allowlists verified **16/16 (PYPL)**
   and **15/15 (F)**, keywords BPE-encodable on the **armed** (`streaming_ctc`)
   vocab, Kalshi API 200, disk 9 GiB. Ford additionally reports **no 1–2 token
   keyword targets** — PayPal does not, because of `stash`, which is the FA risk
   to watch on that board.

   ⚠ **Both events end on `PREFLIGHT: 1 FAILURE — mail env missing:
   ~/.kalshi/mail_env`, and that is NOT waved away — it is scoped.** Verified by
   grep, not assumed: `mail_env` is read only by `scripts/preflight-event.sh`
   (checking it) and `scripts/event-autorun.sh` (the **unattended** path).
   `launch-webcast.sh` — tomorrow's **attended** path — never references it. So
   it does not block Tuesday, and it **does** block anything scheduled
   unattended. Fix before any autorun event.

   ⚠ The pin records `commit=UNKNOWN-NOT-A-GIT-CHECKOUT` because the box is
   rsync-deployed. Provenance is therefore the **laptop tree** — confirm it is
   committed and clean before trusting the pin.
2. Box up, `df -h /` (it hit 100 % on 07-14), `ps -eo cmd --no-headers | grep -c "[h]ls-edge-fetch"` must be **0**.
   ✅ Verified 2026-07-27: box `running` (t3a.xlarge), **0** orphaned fetchers,
   9 GiB free, load 0.00. ⚠ Re-resolve the public IP on the day — there is no
   Elastic IP and it changed on the 07-27 resize.

> ### ▣ THE WEBCAST URL IS ALREADY KNOWN — surfaced 2026-07-27 evening
>
> ## `https://events.q4inc.com/attendee/222501806`
>
> PayPal Q2 2026, from the IR event-details page. **Verified reachable from the
> box: HTTP 200.** You do NOT have to navigate `investor.pypl.com` at 05:40 local
> to find it — that site 403s both the box and plain `curl` from the laptop
> (bot-wall, ~0.1 s edge reject, not geo-blocking).
>
> ⚠ **You still need a REAL BROWSER for the last step.** The Q4 attendee page is
> a **React SPA** — a 3.7 KB shell with the stream URL nowhere in the HTML — so
> `curl` cannot extract the m3u8 and neither can the box. Open the URL above in
> Chrome with the network tab and copy the `.m3u8`. What is removed is the
> *hunting*; what remains is one copy-paste.
>
> ⚠ **THE IR PAGE SAYS "08:00 AM EST" AND THAT IS WRONG.** July is **EDT**
> (UTC−4), so the call is **12:00 UTC**, which is what `starts_at` holds. Taken
> literally, "EST" would mean 13:00 UTC and **you would join an hour late, after
> the prepared remarks.** Do not "correct" the config to match the page.
>
> **Standing dependency, stated because it is the single point of failure:**
> 12:00 UTC = **06:00 CDMX local**, so a human must be at a browser by ~11:40 UTC
> (05:40 local). Nothing armed today runs without that one paste.
>
> **Q4 = Amazon IVS, confirmed from the platform's own API schema (2026-07-27).**
> The attendee SPA's bundle carries the field triple `ingestEndpoint` /
> `streamKey` / `playbackUrl` — the IVS shape — plus
> `query getPublicEventDetails($meetingId: Int)`. Until now "Q4 ⇒ IVS" rested on
> AXP's URL host alone; this is independent, and it supports the prefetch posture.
>
> ⛔ **Do NOT chase the playbackUrl through Q4's API on an event eve.**
> `attendees.events.q4inc.com/graphql` rejects `getPublicEventDetails` (it lives
> on another host), so finding it means guessing at an unfamiliar API surface
> **from the box that must consume Q4's stream hours later**. This project's own
> rule is that a rate-limit or block hits EVERY leg on the box — risking that to
> save one copy-paste is a bad trade. Worth doing **after** an event, from the
> laptop, if automating the surfacing step is ever wanted.

**T−20 min — surface the stream**
3. **First: `rm -f $HOME/pypl-stream.url` and confirm it is gone.** This file is a
   loaded gun between runs — on 2026-07-27 it was left pointing at an AWS IVS
   *demo channel* after a dry-run, and an arm started with it would have captured
   **the demo channel instead of PayPal while looking entirely normal**. Then get
   the m3u8 from the IR player and write it fresh:
   `echo "<m3u8>" > $HOME/pypl-stream.url`

   > ⚠ **WRITE THE MASTER, NOT A VARIANT (new 2026-07-27).** The network tab
   > shows both; take the **channel/master** playlist. A media/variant playlist
   > URL can DIE mid-call while the channel stays live — measured this day, a leg
   > took 10,860 consecutive poll 404s over 45 minutes after its resolved media
   > URL expired (LATENCY-PROOF §V6). `hls-edge-fetch.py` now re-resolves and
   > recovers in ~3 s — **but only if it was given a master.** Handed a variant it
   > prints `⚠ CANNOT RE-RESOLVE` and stays deaf until restarted, and the leg
   > then dies on the 30 s `idle_timeout`, forfeiting every race for the rest of
   > the call. On IVS the master looks like
   > `…playback.live-video.net/api/video/v1/…m3u8`; a variant looks like
   > `…playlist.live-video.net/v1/playlist/<long-token>.m3u8`.
   > Confirm at launch: the leg's stderr must NOT contain `CANNOT RE-RESOLVE`.

   Then write the **same** m3u8 to the control-legs file:
   `echo "<m3u8>" > $HOME/pypl-pcmab.url`  (deliberately a separate file — the
   runsheet's URL file and the instrument's must never alias each other)
   ⚠ **Never start `arm` by hand without `ORT_DYLIB_PATH`.** A missing dylib makes
   the arm **hang silently** at `book snapshots live for all armed markets` — no
   error, no pipeline threads, no leg processes, and it looks healthy.
   `scripts/launch-webcast.sh` sets it and guards it; use the launcher.
4. `scripts/manifest-probe.sh "$(cat $HOME/pypl-stream.url)"`

   > ### ⚠ READ THIS BEFORE THE PROBE OUTPUT SCARES YOU
   >
   > **`SPECULATIVE: NO-GO` is the EXPECTED reading at this venue and requires NO
   > ACTION.** PayPal is Q4 Inc ⇒ Amazon IVS, whose segment URLs are opaque signed
   > tokens, so numeric URL prediction cannot work and `hls-edge-fetch.py`
   > **self-disables internally** and falls back to manifest-driven fetching. That
   > is a designed fallback, not a fault — confirmed in every IVS `SUMMARY` line
   > we have (`spec=OFF spec_hits=0 spec_404=0`, `dropped=0`).
   >
   > **A previous revision of this step said "NO-GO ⇒ swap to
   > `configs/webcast-fanout-arm.toml`". DO NOT. That is the bare template — it
   > carries NO PayPal keywords and NO allowlist, so arming it would refuse every
   > order at fire time and void the event.** Leaving the config alone is correct.
   >
   > The section that actually decides the posture at an IVS venue is
   > **`== EXT-X-PREFETCH ==`** (`scripts/manifest-probe.sh:70`). See the table
   > below.

   Read four things:
   - **`== EXT-X-PREFETCH ==`** → PRESENT or ABSENT. **This is the decision input.**
   - `== speculative posture ==` → expect NO-GO on IVS (see above). On a
     non-IVS venue a GO plus a measured `L` still means something.
   - `== all renditions ==` → is there an **audio-only** rendition? (worth ~10 ms,
     F1c — take it if free, do **not** delay the launch for it)
   - segment duration → expect ~2 s; if it is 6–10 s the exposure maths change

   **4b. THE PREFETCH DECISION TABLE — pick leg 1's flags from the probe.**

   `#EXT-X-PREFETCH` is the IVS low-latency path and the only `FEED` lever that
   works at this venue (LATENCY-PROOF §V1–V5): buffered `+0.168 s`, streamed
   `+1.121 s` average — a **segment-phase distribution** (`+0.168 s` at the tail,
   `+2.073 s` at the head), never a single number.

   ⚠ **Those are ARRIVAL numbers. At FIRE time the streamed tier is worth
   ~`+0.605 s` at p50, not `+1.121 s`** (§V7) — roughly 516 ms of the arrival lead
   never reaches the decision point. Quote the fire-time figure when reasoning about
   Δrace; quote the arrival figure only when reasoning about the fetcher.

   ⚠ **Both tiers are now drop-safety gated** — buffered in §V2, streamed in §V8
   (90 min, 179 samples, never behind, `short=0`, `dropped=0`). **All of it was
   measured on the AWS IVS *demo* channel, not Q4 Inc's**, so the chunked-body check
   below is still mandatory and this event is still the first real-venue test.

   ⚠⚠ **THE STAGED DEFAULT CHANGED 2026-07-27 AND THIS TABLE INVERTED WITH IT.**
   Leg 1 of `configs/webcast-pypl-26jul28-arm.toml` now ships **row 1**
   (`--speculative --prefetch --stream-body --prefetch-report`), armed on the §V8
   pass. So **"leave it alone" is now the AGGRESSIVE choice, and rows 2–3 require
   you to EDIT THE CONFIG DOWN.** That is the reverse of what this table meant
   yesterday. Read the "action" column, not your memory of it.

   | probe says | leg-1 flags | action | why |
   |---|---|---|---|
   | PRESENT **and** bodies **chunked / no `Content-Length`** | `--speculative --prefetch --stream-body --prefetch-report` | **none — as staged** | streamed tier, **V8-gated** (90 min, `short=0`, never behind); worth **~+0.605 s at FIRE time**, not the +1.121 s arrival figure — §V7 |
   | PRESENT but bodies carry `Content-Length` | `--speculative --prefetch --prefetch-report` | **EDIT: drop `--stream-body`** | buffered tier only (+0.168 s, V2-gated). `--stream-body` on a whole-body origin degrades **every** fetch on the leg |
   | **ABSENT** | `--speculative --prefetch-report` | **EDIT: drop `--prefetch --stream-body`** | nothing to prefetch |

   ⚠ Whichever row you land on, **step 4c's 90 s smoke is not optional if you
   edited** — and it is the cheap way to catch a wrong row before the call.

   ⚠ **`--stream-body` is NOT prefetch-scoped.** It is passed to `fetch_segment`
   at all three call sites (`scripts/hls-edge-fetch.py:455,518,568`) — normal,
   prefetch and speculative. On DW/Akamai, whose bodies arrive whole, it was
   **REFUTED** (median −0.197 s against a ≥0.3 s bar) *and* cost **3/32/62 short
   reads per run vs 0/0/0 buffered**, with jitter p99 12–18 ms vs ~1 ms
   (LATENCY-PROOF 2026-07-24 part 4). On a venue that does not serve chunked
   bodies it degrades **every** fetch on the leg. Hence rows 2 and 3 — the check
   is mandatory, not advisory.

   Check chunking directly if the probe does not print it:
   `curl -sI "<a prefetch URL>" | grep -iE 'transfer-encoding|content-length'`

**T−15 min — smoke the armed leg-1 command (ALWAYS, changed or not)**
4c. ⚠ **This step used to read "only if you changed it in 4b". That was correct
   only while the staged default was the CONSERVATIVE row.** Since 2026-07-27 the
   config ships the streamed tier, so **"unchanged" is now the path that has never
   run against Q4 Inc's channel** — exactly the one that most needs 90 seconds of
   evidence. Smoke it every time; it costs 90 s and it is the only real-venue
   check that exists before the call.

   Run the exact leg-1 command against the live m3u8 for ~90 s, stdout to
   `/dev/null`, stderr to a scratch file. Require **`prefetch_n > 0`**,
   **`seg LOST = 0`**, **`short = 0`** in the `SUMMARY` line.

   ⚠ **`short > 0` is the specific tell that this venue does not want
   `--stream-body`** — that is precisely how the flag failed on DW/Akamai
   (3/32/62 short reads vs 0/0/0 buffered), and §V8's pass was `short=0` across
   every SUMMARY line for 90 minutes on the demo channel. Any failure ⇒ **drop to
   the next row of the table** (one-line edit), then re-smoke. Dropping a row is
   cheap; arming a leg that sheds audio is a recall bug that looks like a
   detection failure.

**T−10 min — launch**
5. ```
   bash scripts/launch-webcast.sh \
     --config configs/webcast-pypl-26jul28-arm.toml \
     --account $HOME/.kalshi/earshot_env \
     --i-am-live
   ```

   ⚠ **`--i-am-live` IS REQUIRED FOR THIS TO PLACE ORDERS.** Without it the
   launcher arms a **dry run** and the risk gate is never exercised — which is what
   the staged command did before W7 was lifted 2026-07-27, on an event billed as
   the campaign's live Δrace measurement. The flag is safe here because the posture
   is enforced in `arm`, not in the launcher: `--allow-mention` alone is the
   mention-**PROBE** gate, so `max_price_cents` must be ≤ 1¢ and orders are
   non-crossing — **a live arm on this config still cannot fill.** `--fillable`
   remains refused by the launcher (gated on the G4 fill-tier verdict).

   Run it and **read the `posture:` line back** before the call starts. It now
   states what the run actually is; if it says `DRY RUN` you forgot the flag.

6. **Confirm all four lines appear before the call starts** — each one is a
   thing that has broken before:
   - `streaming_ctc: ORT_DYLIB_PATH=…` — without it the CTC detector cannot
     build and the arm dies at startup
   - `book snapshots live for all armed markets` — Kalshi validated all 16
   - `fanout: per-leg detect chains, OR at decision legs=2`
   - `pipe source: spawning decoder` **×2**

   Live-only, and it is the point of arming live: `ARMED for live orders —
   MENTION-PROBE posture (allowlist, Mentions permitted, cap ≤ 1¢, $1 ledger)`.
   Its absence means the arm is in dry run.

**T−5 min — start the CONTROL legs (this is what makes Tuesday's number mean anything)**
6b. ```
   cd $HOME/ear && ./target/release/earshot capture-bakeoff \
     --config configs/pcm-lead-pypl.toml --secs 5400 --record-dir $HOME/pcmab-pypl
   ```
   Two **passive** legs — `plain` vs `--prefetch --stream-body` — differing in
   those flags only. `capture-bakeoff` "does NOT route through the detector
   pipeline" (`src/capture_bakeoff/mod.rs:12`), so this is fetch+ffmpeg only:
   ~1–2 % CPU each, measured, and it cannot contend with the armed CTC legs.

   **Why it is not optional.** The standing rule is *interleaved A/B within one
   run, never across runs* (the laptop shows 3.5× run variance). Without a
   same-run baseline, whatever Δrace PayPal produces is one number with nothing
   to attribute it to. These legs are that baseline, and they yield the
   real-venue lead at high n where Δrace yields a handful of draws.

   Total consumers on Q4's origin: 4 (2 armed + 2 passive). Negligible for IVS —
   but **drop the passive pair immediately** if anything rate-limit-shaped
   appears (429/403), because a block hits every leg on the box.

**During**
7. Leave it alone. Detections log as `decision intent … ticker=… tier=Confirmed`.
   Repeats inside the refractory window log as `detection ignored (keyword state
   machine)` — that is the one-shot dedupe, not a fault.

**After**
8. `grep SUMMARY $HOME/spec_edge.err` → **`seg LOST` must be 0.** A non-zero
   value is silently lost audio, which is a recall bug (this cost 86 s once).
   **Also (new 2026-07-27) `deaf_s` must be 0 and `poll_err` small.** A clean
   `seg LOST` is NOT sufficient: a leg whose playlist URL died reports
   `dropped=0 short=0 seg_err=0` forever while delivering nothing. If `deaf_s>0`,
   read `reresolve=` — non-zero means the leg self-healed and the run is still
   good; a `DEAF-NOW` marker on the last line means it never came back.
   Also read `prefetch_n / lead_med / lead_min / lead_max` from the same line —
   **that is `L_prefetch` on a REAL Q4 Inc channel, the first venue confirmation
   that is not the AWS demo.** Demo-channel reference: `lead_med=4.00s`,
   `lead_min=0.75s`, `lead_max=4.25s`. ⚠ Announcement lead is **not** achieved
   audio lead — on the demo channel a 4.00 s announcement lead converted to
   +0.168 s buffered / +1.121 s streamed. Do not quote one as the other.
9. `state/KXEARNINGSMENTIONPYPL-26JUL28/report.json` → `ledger_spent_cents`
   must be **0** in observe posture. ⚠ **Live 1¢ arm (W7 partial lift):** orders
   now really are sent, so expect the arm to log `ARMED for live orders —
   MENTION-PROBE posture`. Spend still lands at 0 because the orders are 1¢
   non-crossing; a **non-zero** `ledger_spent_cents` means something filled that
   should not have been able to, and is a stop-and-investigate.
10. Score Δrace against the µs tape on the **price-moving** sweep, never a flat
    print — that scoring error inverted three figures once already.
10b. **Settle R1 — word-START vs word-END firing.** This is the cheapest
    high-value result of the day and it needs no extra capture:

    ```
    scripts/rival-decompose.py --per-market ~/pypl-races.json \
      --manifest corpus/manifests/pypl-q2-2026.toml --label "PYPL 07-28"
    ```

    One row per contested market: `{"market","phrase","gap_s","word_dur_s"}`,
    `gap_s` **positive = we are behind**. The two models are point predictions on
    the slope of gap vs word duration (**word-start ⇒ +1.0, word-end ⇒ 0.0**), and
    the slope is invariant to an additive `OURS`, so **Δrace works — a true FEED
    gap is better but is not required.**

    ⚠ **Do not read the OLS row.** The estimator is Theil-Sen, because `OURS` is a
    segment-phase *lottery* (~25.8 % of words pay ~2.05 s at IVS) and that is
    bimodal contamination: simulated on E0's own numbers, Theil-Sen picks the
    right model 93–99 % of runs at n=5–8 while OLS resolves ~25 %. Run
    `scripts/rival-decompose.py --selfcheck` to reproduce that.

    ⚠ **PayPal alone may not settle it.** The board has 16 markets but only the
    *contested* ones score, and at n=4 the readout is only ~81 % reliable. If
    fewer than ~5 markets are contested, report it as **NOT RESOLVED** and pool
    with Ford/FOMC rather than calling it on one thin event.

10c. **Run the A4 physical bound — the only ABSOLUTE race test we have.**

    ```
    scripts/physical-bound.py --races ~/pypl-races.json \
      --pdt-log $HOME/pypl-arrival.log
    ```

    Asks whether the competitor's sweep preceded the instant *any* public-feed
    consumer could have held that audio. **A race that comes back IMPOSSIBLE can
    never be won by fetch or transport work of any kind** — it is prefix-firing
    or a channel we have not found (→ W4). One race settles it; unlike R1's
    slope test this is a per-race binary, not a regression.

    Needs `u`, the word-end's offset inside its segment — which comes from
    forced alignment joined to the PDT log, **never** from when the audio reached
    us (under a buffered fetch the whole segment lands at once, so arrival is
    identical for every word in it and carries no information about `u`). Without
    a banked PDT the script reports the threshold `u*` instead of guessing.

    ⚠ Read the verdict, not the margin: a race counts only when it holds at
    **both** ends of the origin-flush range (0 .. 0.60 s). `--selfcheck` plants
    three known verdicts and reproduces `u* = 1.379 s` for AXP NFL.
11. **KEEP `$HOME/pypl-arrival.log`** — one line per segment,
    `{pdt} {t_done} {fetch_secs} {mode} {t_first}`. This is what makes the race
    **boundable** rather than merely comparable (LATENCY-PROOF §R1/§A1): with the
    encoder's own PDT you can ask whether *any* public-feed consumer could have
    held that audio when the competitor swept. No banked race has it; every race
    from here does. Demo-channel reference from the armed leg: first-byte lag
    **p50 0.484 s** behind PDT, complete **p50 2.393 s**.
12. **Score the control legs:** `scripts/leg-lag.py $HOME/pcmab-pypl plain pfstream`.
    Honour its NOT-STEADY guard — a wide spread is **the segment-phase
    distribution**, not noise and not a number to quote. Report the median with
    n and spread, plus min/max as the tail/head of the distribution.

### ▣ PRE-REGISTERED PREDICTIONS FOR TUESDAY — written 2026-07-27 20:15 UTC, BEFORE the event

All four are checkable from `$HOME/pypl-arrival.log` alone (no alignment, no
tape). Demo-channel references in brackets. **Do not adjust these after seeing
the numbers.**

| # | prediction | pass band | demo ref |
|---|---|---|---|
| 1 | first-byte lag behind PDT, p50 | **0.30 – 0.70 s** | 0.484 s |
| 2 | complete lag behind PDT, p50 | **seg_dur + 0.2 … +0.7 s** | 2.393 s @ 2 s seg |
| 3 | span (complete − first byte), p50 | **seg_dur − 0.3 … seg_dur** | 1.909 s @ 2 s seg |
| 4 | `mode=prefetch` share of segments | **> 90 %** | 29/30 |

**FALSIFIER, and it is the one that matters:** if **span ≈ 0** then the bodies
are NOT arriving chunked and the streamed tier bought nothing at this venue —
read the run as the buffered tier, and do not credit `--stream-body`. (The plain
leg measured span **exactly 0.000 s**, so this is a sharp discriminator, not a
judgement call.)

**Δrace prediction (model-dependent, stated separately because it is weaker):**
the streamed tier is worth **≈1.117 s** of fire-time gain (§A2, mean over word
position; 2.03 s at a segment head, 0.12 s at a tail). Against the four banked
races that would move us from **1 of 4** races won to **2 of 4**. PayPal will
yield few races, so treat any single Δrace as one draw from a ±2 s distribution,
**not** as a verdict on the tier.

**⚠ State this expectation NOW, so it cannot be rounded toward afterwards.**
PayPal will produce only a handful of price-moving sweeps, and `OURS` is a
segment-phase lottery with ~±2 s spread (E0). **A handful of Δrace draws cannot
resolve a 1.12 s improvement** — anyone claiming Tuesday "proved" the streamed
tier from the Δrace column is over-reading it. The high-n results Tuesday can
legitimately deliver are (a) the PCM-domain lead from step 11 and (b) the
`prefetch lead=` distribution from step 8.

**Known risks, decided in advance:**
- `stash` → `▁STA SH`, 2 tokens, the shortest target of the 37. Watch it; if it
  fires loose, drop it rather than tune it.
- `regulation` inherits `regulator`/`regulatory` from the market rules, and
  "regulatory" is said constantly on earnings calls ⇒ REGU likely resolves
  early and is decided by the RACE, not by detection.
- Engine reverts to `sherpa_kws` with a one-line edit to `engine =` if anything
  about CTC misbehaves; the tuned sherpa block is still in the config.

**Ford is the same day at 5:00 PM ET** — also webcast-only, but LOGIN-GATED with
an empty player until call time, so its m3u8 cannot be probed in advance. A
laptop-surfaced token IS box-consumable (F1c: HTTP 200 from us-east), so surface
on the logged-in laptop and hand the URL over.

## Week of 07-20 — mention-event slate + webcast study monitors (staged 2026-07-20 evening)

Slate from the public API (`GET /events?status=open`, 45 open MENTION events,
2026-07-20) + venue recon (issuer PRs/8-Ks/past transcripts; sources in the
07-20 session log). Access classes: `public-dialin` / `bridge (star-11
confirmed)` / `bridge-likely` (operator-run analyst queue in transcripts, no
published number) / `webcast-only`.

| Event | Call (ET) | Access | Study monitor |
|---|---|---|---|
| ALLY Tue 07-21 | 9:00 AM | **public-dialin via registration** (register-conf.media-server.com — emails dial-in + ID; register the night before) | box `~/run-ally-capture.sh` |
| BA "26JUL21" | **⚠ MISDATED ticker: Boeing's call is Tue 07-28 10:30 AM** | webcast + analyst bridge | not this week; re-stage for 07-28 |
| ALK Wed 07-22 | 11:30 AM (results filed Tue after close!) | bridge-likely | box `~/run-alk-capture.sh` |
| GEV Wed 07-22 | 7:30 AM | bridge-likely (operator queue) + phone autorun staged | box `~/run-gev-capture.sh` |
| GOOGL Wed 07-22 | 4:30 PM | webcast-only, **YouTube URL live: youtube.com/watch?v=LzExSq9DU9w** | box `~/run-googl-capture.sh` if a non-YouTube m3u8 surfaces; else laptop yt-dlp (googlevideo manifests are requester-IP-locked; box is bot-walled) |
| TSLA Wed 07-22 | **5:30 PM (8-K confirmed)** | webcast-only (IR player) | laptop `configs/webcast-tsla-capture.toml` (yt-dlp legs) + observe arm `configs/webcast-tsla.toml` |
| AAL Thu 07-23 | 8:30 AM | **bridge (star-11 confirmed, Q1 transcript)** | box `~/run-aal-capture.sh` |
| INTC Thu 07-23 | 5:00 PM | **bridge (star-11 confirmed)** | box `~/run-intc-capture.sh` |
| AXP Fri 07-24 | 8:30 AM | **webcast on Q4 Inc** (`events.q4inc.com/attendee/475264006`, from the IR event page 07-23; no public dial-in in the press release — UNKNOWN platform, `manifest-probe.sh` FIRST) | box `~/run-axp-capture.sh` (re-staged 07-23, probesize-fixed) |
| VZ Fri 07-24 | 8:30 AM | **webcast, self-hosted player** — link posts ~7:00 AM ET day-of at `verizon.com/about/investors/second-quarter-2026-earnings`; no public dial-in in the press release | box `~/run-vz-capture.sh` (re-staged 07-23, probesize-fixed) |

Same-floor broadcast class this week: House Financial Services FinCEN
hearing Tue 10:00 AM (committee stream); Navy-commission "Maritime
Seapower" hearing Wed 10:00 AM (stream link unverified); the "AI Workforce"
Fri hearing was NOT verifiable on 07-20. **FOMC: Warsh presser is Wed 07-29
2:30 PM (next week) — the crown same-floor target.**
**⚠ SLATE CORRECTION 2026-07-24 (night) — BA IS NOT THE TUESDAY EVENT.**
Kalshi lists `KXEARNINGSMENTIONBA-26JUL21` (**already past**) and no future BA
event. Queried the API for every earnings-mention series with an event dated
Jul 25–31:

| date | events |
|---|---|
| **Tue 07-28** | `KXEARNINGSMENTIONF-26JUL28` (Ford), `KXEARNINGSMENTIONPYPL-26JUL28` (PayPal) |
| Wed 07-29 | Carvana, **Meta**, Wingstop (+ FOMC 14:30 ET) |
| Thu 07-30 | **Amazon**, Ferrari, Reddit, Yum! Brands |

**✅ RESOLVED 2026-07-25 — THE WHOLE TUESDAY SLATE IS WEBCAST-ONLY. NO DIAL-INS.**
Verified in-browser, post-registration, not inferred:

| Event | Call ET | Access | Platform | Notes |
|---|---|---|---|---|
| **PayPal** | **Tue 8:00 AM** | **WEBCAST ONLY — SEC-CONFIRMED** | **Q4 Inc** (`investor.pypl.com`, footer "Powered By Q4 Inc.") | **Same platform that fronted AXP 07-24** → AXP resolved to **Amazon IVS, 2 s segments**. ⚠ **CORRECTED 07-27:** this cell used to add "the venue class where `L` was measured three times (1.40/1.50/1.51 s)". **It is not.** Those three readings were on `demo.unified-streaming.com` — similar segment duration, different origin. `L` had never been measured on IVS, and **our numeric-prediction speculative fetcher buys ZERO there** (IVS segment URLs are opaque signed tokens, so `hls-edge-fetch.py` self-disables). What IVS *does* ship is `#EXT-X-PREFETCH`, measured at **4.18 s of announcement lead / ~2.3 s of first-byte gain** on the AWS demo channel — unimplemented by us, and **unconfirmed on Q4's channel**. Day-of: run the patched `manifest-probe.sh` and read the new `== EXT-X-PREFETCH ==` section BEFORE trusting any speculation estimate. See LATENCY-PROOF §V1. |
| **Ford** | **Tue 5:00 PM** | **WEBCAST ONLY** | OpenExchange | Registered; post-login the ONLY access is a "WEBCAST" link. **AND the IR "Advisory" PDF was read** (`s205.q4cdn.com/882619693/files/doc_events/2026/Jul/28/Ford-Q2-2026-Earnings-Advisory.pdf`) — it lists only the webcast + replay. **No bridge, no passcode.** |
| Meta | Wed 4:30 PM | webcast-only (high conf) | — | release names webcast only |
| Amazon | Thu 5:00 PM | webcast-only (high conf) | — | release names webcast only |

**They do NOT conflict** — PayPal morning, Ford evening. Both runnable.

**⚠ FORD CAPTURE RISK — the stream is LOGIN-GATED.** The webcast lives behind
the OpenExchange session (`/webcast`, nav shows Sign Out), and the player is
**empty until 5:00 PM ET**, so no m3u8 exists to probe beforehand. Two
consequences: (1) the venue probe must run day-of, at call time; (2) the manifest
may be session/token-bound like the FOMC Brightcove case, in which case the
us-east box cannot fetch it with a bare URL and capture has to carry the session
(or run from the logged-in laptop). **Plan for tokenized.**

**⚠ METHOD CORRECTION — I nearly closed this on one source.** I first declared
Ford webcast-only from the *registration portal alone*. The operator pushed back:
issuers often bury the dial-in in an **"Advisory" PDF** on the IR events page, not
in the portal or the press-release body. That is the correct method and it is what
this runbook's own phone-first note already said ("check transcripts / registration
/ IR / legacy 8-K numbers before calling an event webcast-only"). **Always open the
Advisory / 8-K before writing off an event.**

Having done so for Ford Q2 2026: the Advisory contains **no conference bridge**.
Its only phone numbers are IR staff contacts — Media 1.773.682.7954, Equity
Investment Community 1.248.510.9092, Fixed Income 1.313.248.1587, Shareholder
Inquiries 1.800.555.5259 — which do **not** join the call. Ford reportedly
published a participant number in earlier quarters (1-844-282-4573), so this looks
like an issuer that **dropped** its dial-in for OpenExchange — evidence the
webcast-only trend is visible inside a single issuer's own document set.

**Boeing is the counter-example and it does not help us.** BA publishes toll-free
numbers + passcode openly on `investors.boeing.com` with no gate — but **Kalshi has
no BA mention event next week** (`KXEARNINGSMENTIONBA-26JUL21` is misdated and
already past; the Jul 25–31 API sweep returned only F, PYPL, Carvana, Meta,
Wingstop, Amazon, Ferrari, Reddit, Yum). Easiest bridge on the street, nothing to
trade on it. **Access class and tradability are independent — check both.**

**PAYPAL — rule 3 applied, closed on the AUTHORITATIVE source (2026-07-25).**
Checked three ways, not one:
1. IR event page (`investor.pypl.com/…/PayPals-Second-Quarter-2026-Earnings-Call`)
   — access listed as "Webcast", nothing else.
2. **EDGAR: PayPal has filed NO 8-K since 2026-05-21**, so there is no Q2
   call-announcement filing to hide a number in. PayPal does not 8-K its call
   dates; the quarterly results release is the operative template.
3. **The Q1 2026 results release** (8-K exhibit `pypl1q-26earningsrelease.htm`,
   acc-no 0001633917-26-000065, filed 2026-05-05) — its **"Conference Call &
   Webcast"** section offers only a live webcast via the IR website, plus a 90-day
   archive. **No dial-in, no toll-free number, no international number, no
   passcode, no conference ID.**

⇒ **PayPal is webcast-only, confirmed from an SEC-filed document.** Combined with
the Q4 Inc platform (= AXP's, which resolved to Amazon IVS / 2 s segments), Tuesday
morning is a known venue class and the speculative-posture numbers should transfer.

**Operator note:** registration accepted "Individual investor" as an affiliation —
the portal is not analyst-gated — and the confirmation says only *"return to this
site … to view the presentation"*. That wording alone was the first signal there
is no dial-in.

**⚠ SUPERSEDED — the pre-registration reconnaissance below is kept for method.**
**TUESDAY 07-28 PAYPAL — STAGED AND VALIDATED (2026-07-25).**
`configs/webcast-pypl-26jul28-arm.toml` — speculative CTC fanout + 16 markets
generated live from Kalshi. Observe posture, every cap 1¢.

**Keyword encode check PASSED on the vocab we will actually arm.** All 16
keywords (37 variants) compile on the streaming-CTC LibriSpeech BPE-500 with
**zero gaps** — token cuts differ from gigaspeech, as expected, but nothing
falls out of vocab.

**⚠ OPS TRAP — `compile-keywords` validated the WRONG vocab by default.** The
arm config keeps a `[detector.sherpa_kws]` block so the engine can be reverted
with a one-line edit; `compile-keywords` then resolves *that* `model_dir` and
silently checks the **gigaspeech** BPE while the config runs `streaming_ctc`.
A keyword out of the CTC vocab would never fire and the check would have said
it was fine. **Always pass `--model-dir` explicitly when the config carries
more than one detector block:**
```
earshot compile-keywords --config <cfg> \
  --model-dir models/sherpa-onnx-streaming-zipformer-ctc-small-2024-03-18
```

**Highest false-alarm risk in this set: `stash` → `▁STA SH`, 2 tokens** — the
shortest target of the 37 and the acoustic analogue of the 2-character variant
problem that gave Ford 38 false alarms. Watch it; consider dropping if the
day-of screen shows it firing loose.

**`regulation` carries `regulator` / `regulators` / `regulatory` from the market
rules.** "Regulatory" is said constantly on earnings calls, so REGU is likely to
resolve early — a market where the RACE, not detection, decides it.

**Before arming:** put the m3u8 in `$HOME/pypl-stream.url`, run
`scripts/manifest-probe.sh` for the speculative GO/NO-GO and the venue's `L`.
Q4 Inc = AXP's platform → expect Amazon IVS, 2 s segments. ⚠ **NOT** `L` ≈ 1.4–1.5 s
(corrected 07-27 — that `L` is `demo.unified-streaming.com`, a different origin;
numeric speculation buys **zero** on IVS). Expect `#EXT-X-PREFETCH` instead and
confirm it with `manifest-probe.sh` day-of. LATENCY-PROOF §V1.

**⚠ FORD HAS AN OPEN REGISTRATION PORTAL — VERIFIED 2026-07-25 (browser).**
`https://ford-motor-company-q2-2026-earnings-call.open-exchange.net/registration`
is a real OpenExchange gate for "Q2 2026 Financial Results, TUESDAY, JULY 28, 2026".
Fields: First/Last name, Email, Organization, **Affiliation** — and the affiliation
list is **Buy-side / Sell-side / Individual investor / Media / Employee / Other**.
**"Individual investor" is accepted, so registration is NOT analyst-gated.**

Whether it yields a **telephone dial-in** (vs webcast-only access) is only knowable
after completing registration, which means submitting real identity details — an
operator decision, not an automated one. **Unresolved; ask before registering.**
If it does yield a number, Ford becomes a PHONE-capture target (~20 s ahead of the
webcast per the phone-first finding), which would outweigh its keyword-collision
problem and change Tuesday's plan.

**Timing (both Tuesday runnable — they do NOT conflict):**
- **PayPal 8:00 AM ET** (results pre-market)
- **Ford 5:00 PM ET** (results 4:05 PM ET) — `shareholder.ford.com`

**⚠ RE-EVALUATED 2026-07-25 — the Ford blocker is LIFTED (but PayPal still wins).**
The "Ford is armable only after those variants are fixed" verdict below predates
the `prepare` short-variant fix. Regenerating Ford's set with current `prepare`:
**15 keywords, 4 ultra-short variants dropped loudly (`ev`/`evs`/`ai`/`ais` — the
exact 38-of-65 false alarms), 0 high-risk collisions flagged, shortest surviving
variant 4 chars.** The stated blocker no longer exists.

PayPal remains the pick if only one runs, on different grounds than before:
- **Vocabulary distinctiveness.** PayPal's words are long and rare — `agentic
  commerce`, `stablecoin`, `braintree`, `perplexity`, `european payments
  council`. Ford's are short, common earnings words — `tariff`, `china`,
  `headwind`, `tailwind`, `market share`, `subsidy` — which is a harder
  detection problem regardless of variant length.
- **Timing.** PayPal 8:00 AM ET vs Ford 5:00 PM ET, so BOTH are runnable; this
  is not either/or.
- **Ford's stream is login-gated** with an empty player until call time, so its
  m3u8 cannot be probed in advance. PayPal's Q4 Inc venue is a known class.

**RESOLVED — `recall` is an OPPORTUNITY, not a risk.** I first logged this as a
false-alarm risk ("as I recall" is common speech). Wrong framing. Kalshi's own
market title is **"Recall / Recalled"** — it explicitly names the verb form, so
the market scores the **word**, not a product-recall event. `prepare` emits
variants `recall / recalls / recalled / recalleds` accordingly.

Consequence: "as I recall" is a **true positive that settles YES**, and on a long
earnings call it is close to inevitable. RECA is therefore a high-probability,
early-resolving market — like PayPal's REGU — where the **RACE decides the
outcome, not detection**. Those are the markets worth arming the speculative
posture for.

(`recalleds` is a generated plural of a non-word; harmless — never spoken — but
another instance of the pluraliser not checking.)

**Arm PayPal, not Ford, if only one.** The pre-arm collision screen
(`docs/BAKEOFF.md` §Collision screen) measured Ford's generated keyword set at
**65 false alarms** over 2.5 h of unrelated calls vs PayPal's **12** — 38 of
Ford's from the two-character `ev`/`evs`/`ai`/`ais` variants that `prepare`
emits. Ford is armable only after those variants are fixed.

**FOMC VENUE RECON (pinned 2026-07-24 ~00:40, from the June 17 archived
presser played with network capture):** the Fed's own player is
**Brightcove** — account `66043936001`, player
`players.brightcove.com/66043936001/default_default`, HLS via
`manifest.prod.boltdns.net/manifest/v1/hls/v4/clear/66043936001/…/master.m3u8`
with a short-lived **`fastly_token`** query param; VOD segments are 10 s
(`/10s/` path — live cadence TBD via manifest-probe on the day). Surfacing
paths, fastest first: (1) **Brightcove Playback API** — extract the
`policy_key` from the player JS (public, in `players.brightcove.com/…/index.min.js`)
and poll `edge.api.brightcove.com/playback/v1/accounts/66043936001/videos/…`
for the live event → m3u8 seconds after it exists, no page scraping;
(2) the standard Chrome network-capture recipe on
federalreserve.gov/monetarypolicy/fomcpresconf20260729.htm; (3) YouTube
simulcast (youtube.com/federalreserve) — laptop yt-dlp leg only (googlevideo
is requester-IP-locked; box is bot-walled, per GOOGL). Tokenized manifests
mean the box legs must consume the freshly-surfaced tokenized URL; token
lifetime to be measured day-of. This is a THIRD venue class (Brightcove/
boltdns/Fastly) — manifest-probe FIRST, fetch-ladder verdict unknown.

**RUN `scripts/manifest-probe.sh <m3u8>` THE MOMENT THE m3u8 IS SURFACED.** It
now ends with a `== speculative posture ==` section (`scripts/spec-probe.py`)
that answers, for that venue: are segment URLs numerically predictable, and how
far ahead of the manifest can a segment be fetched (`L`)? That is the term the
speculative leg converts into race position — measured `L` +1.07 s at 6 s
segments (Akamai) and +1.50 s at 1.92 s (Unified Streaming), both **GO**.
**Brightcove is UNKNOWN and cannot be probed before the stream is live**, because
the manifest carries a short-lived `fastly_token`. Two venue-specific risks to
watch in the probe output: (a) if each SEGMENT carries its own token, N+1 is not
nameable → NO-GO; (b) if the live cadence really is the VOD's 10 s, one segment
is a very long time to be wrong about.
FOMC 07-29 runs the **non-speculative** template regardless (standing decision —
a rate-limit would take every leg on the box down); the probe informs the NEXT
event, and pins `L` for the race ledger either way.

**SPECULATIVE POSTURE IS NOW SAFETY-CLEARED (2026-07-25).** The 90-minute
drop-safety gate that blocked `configs/webcast-fanout-arm-spec.toml` is GREEN
(`docs/LATENCY-PROOF.md` §S7 re-validation): `seg_err=0`, 46 transient 404s all
recovered, `dropped=0`, and `spec_edge` was the only leg to emit full wall-clock
audio. What this changes:

- **Tue 07-28 (Ford / PayPal) MAY run the speculative posture.** The standing
  "non-speculative only" decision was specific to **FOMC 07-29**, where a
  rate-limit would take every leg down on the quarter's marquee event. Tuesday is
  not that event.
- **Gate it on the venue probe, day-of.** Run `scripts/manifest-probe.sh <m3u8>`
  the moment the stream is surfaced; it ends with a `SPECULATIVE: GO/NO-GO` line
  and the venue's `L`. GO ⇒ stage with `scripts/stage-webcast-arm.sh
  --speculative`. NO-GO ⇒ the default template, and expect no improvement.
- **Still true regardless:** arm PayPal over Ford on precision grounds
  (`docs/BAKEOFF.md` §Collision screen), and check `~/spec_edge.err` `SUMMARY`
  after any speculative run — `seg LOST` must be 0.

**⚠ OPS TRAP — `pkill -f <pattern>` kills its own shell (2026-07-25).**
`pkill -f hls-edge-fetch.py` over ssh matches ANY process whose command line
contains that string — including the `bash -c` wrapper running the command
itself. The session dies mid-command and returns EMPTY output, intermittently
(it is a race between the signal and the echo). This was very nearly written off
as a CDMX→AWS network failure. Use the bracket form, which does not match itself:
```
pkill -f "[h]ls-edge-fetch\.py" ; pkill -f "[c]apture-bakeoff"
ps -eo cmd --no-headers | grep -c "[h]ls-edge-fetch"    # verify 0; pgrep -cf lies too
```
**Bracketing alone is NOT enough** — it only helps if the bracketed pattern is
the ONLY occurrence in your command line. Killing and launching in one ssh call
still self-kills, because the `nohup ...capture-bakeoff...` payload puts the
PLAIN string there too and `[c]apture-bakeoff` matches it. Bit twice on
2026-07-25; the second time the launch silently never ran.
**Rule: reap in one ssh invocation, launch in a separate one.** Never mention
the process you are killing in the same command line.

When the box "goes quiet", diagnose in this order before blaming the link:
TCP 22 reachable → `aws ec2 describe-instance-status` → only then the network.
A saturated box fails at **SSH banner exchange**, which looks exactly like a
dead link: seen 2026-07-25 at load 18 on 2 vCPU while `describe-instance-status`
reported `ok/ok`. Back off and retry; do not conclude anything about the network.

**⚠ OPS TRAP — `cargo: not found` over ssh, and a pipe that hides it (2026-07-25).**
Two defects stacked, and together they produced a *silently stale* measurement.

1. **Root cause — minimal env.** `ssh box 'sh -c "cargo build …"'` runs
   NON-INTERACTIVE: `~/.profile` / `~/.bashrc` are not sourced, so `~/.cargo/env`
   never runs and cargo is not on PATH. The box printed `sh: 1: cargo: not
   found`. Use an absolute path or source the env explicitly:
   ```
   ~/.cargo/bin/cargo build --release       # or: . ~/.cargo/env && cargo …
   ```
   Same class as the launchd-minimal-env rule in `CLAUDE.md` — an unattended
   path validated interactively is not validated.
2. **What hid it.** The launcher was
   `cargo build --release 2>&1 | tail -5 && echo BUILD-OK && ./target/release/earshot …`
   The `&&` tests **`tail`'s** exit code, not cargo's, so BUILD-OK printed and
   the chain ran the **previous** binary — 20 h old. The result was a complete
   bench run, well-formed JSON, plausible recall, entirely from pre-change code.
   Caught only because one expected new field was absent; a run whose output
   merely *looked* right would have been believed.

Gate the run on the build actually having happened:
```
set -o pipefail                      # or drop the pipe entirely
~/.cargo/bin/cargo build --release || exit 1
test target/release/earshot -nt src/bench/mod.rs || { echo STALE; exit 1; }
```
**Defect 2 is the same one as commit `7c6081c`** (pipefail/SIGPIPE swallowing the
live-flip in `launch-webcast.sh`). The lesson was already banked in this repo and
still got repeated — when driving the box, never put a pipe between a build and
the `&&` that gates the run on it.

**Capture outcomes (per-event, running log):**
- **ALLY Tue 07-21 — NO CAPTURE.** The m3u8 was never surfaced / the box was
  never driven, so `~/ally-capture/` was never created — no xcorr/manifest/lag
  numbers. Tape-only analysis stands (COMPETITOR-ANATOMY §Per-tape ALLY:
  CONTESTED — machine sweeps on all 5 YES words; supports the phone-access-not-
  cap contestedness hypothesis). Lesson for Wed: the capture only happens if
  someone runs `~/run-<ev>-capture.sh` after surfacing the URL — the alarms
  cue it but do not execute it.
- **GEV Wed 07-22 — NO CAPTURE** (`~/gev-capture/` never created; the prep
  cues queued while the session sat idle overnight — nobody surfaced the
  m3u8). The 07-17 autorun artifacts in `state/KXEARNINGSMENTIONGEV-26JUL22/`
  are a TEST/dry-run rehearsal, not this call. Tape-only analysis:
  COMPETITOR-ANATOMY §Per-tape GEV — CONTESTED, but 7/8 YES words pre-priced
  ≥83¢; the one live race (ADMI 36¢→99¢, wave-1/wave-2 2.8 s apart) was
  machine-fast, with a wrong-way IRAN sweep 6.8 s earlier proving live
  listening.
- **ALK Wed 07-22 — NO CAPTURE** (same failure mode, same morning). Tape-only:
  COMPETITOR-ANATOMY §Per-tape ALK — CONTESTED (machine sweeps in-call on
  HEAD/AMAZ/ONE/TAIL/OIL incl. an 85 ms double-burst on ONE; markets not yet
  settled at analysis time 18:00 UTC).
- **⚠ Contestedness hypothesis WEAKENED at 4 samples**: GEV and ALK are both
  bridge-likely with NO public dial-in, yet both contested — "contestedness
  tracks phone accessibility" (2-sample read from ALLY/DPZ) does not survive.
  Current best read: contestedness tracks event liquidity/prominence (DPZ =
  small-cap thin; ALLY/GEV/ALK = liquid, contested). The GOOGL/TSLA Δw
  samples remain the discriminator that matters — **those captures MUST run
  today (GOOGL 2:30 PM, TSLA 3:30 PM CDMX) or the week produces zero Δw.**
- **GOOGL Wed 07-22 — CAPTURED (partial: 14:38–15:33; stream self-ended).**
  Missed 14:30–14:38: the operator (Claude) killed a healthy capture on a
  wrong health signal — see the health-check rule above, added from this.
  Full analysis: COMPETITOR-ANATOMY §Per-tape GOOGL.
- **TSLA Wed 07-22 — CAPTURED (~43 min) + LIVE OBSERVE-ARM RUN — the week's
  designed sample DELIVERED.** First measured webcast Δrace: steady-state
  **−8.7 s mean** (−7.3/−8.5/−10.3) vs the fastest racers, from a CDMX
  laptop with default yt-dlp join; drain-phase pairs (−135..−224 s) were
  fetcher join-position artifact. Verdict H1/H3 co-listeners, WINNABLE.
  Recall 8/11 live (TARI leg-dependent, SAFE/TARA true misses); precision
  8/11 (IRAN/EURO/CHIN false fires, all settled NO). Full tables:
  COMPETITOR-ANATOMY §Per-tape TSLA. Say-prior sample #1: FAIL
  (docs/SAY-QA-PRIOR.md).
  **Ops post-mortems (all hit today — every fix CODIFIED 2026-07-22 evening):**
  1. `--wait-for-video` stalls at the pre-live→live transition (GOOGL legs
     08:00 lost) → CODIFIED: `scripts/launch-webcast.sh --wait-live <url|file>
     --wait-kind youtube|hls` polls `is_live` (youtube) or the manifest (hls)
     and launches on the flip; the launcher REFUSES any config that still
     carries `--wait-for-video` in a source cmd.
  2. An `is_live` gate INSIDE a pipe-source cmd trips the pipeline's 30 s
     idle guard → CODIFIED: the `--wait-live` poll runs in the launcher
     BEFORE arm starts; armed-leg cmds carry no liveness logic.
  3. A sandboxed arm process was hard-killed ~60 s in (no signal handler
     ran) → CODIFIED: launch-webcast.sh runs a startup self-check (SIGTERM
     trap installable + state dir writable, refuses otherwise); launch from
     a plain shell/nohup only. Validation stays per the standing convention:
     the exact production mechanism at the next event, not a lookalike drill.
  4. Live arm and capture legs heard DIFFERENT audio (arm's own yt-dlp
     stream missed "tariff"; the capture leg's audio contains it) →
     CODIFIED: `[arbiter] mode = "fanout"` (`pipeline::run_pipeline_fanout`)
     runs a full detect chain per leg, OR-ed at the shared one-shot decision
     table; per-leg attribution in `report.per_leg` + `DetectionRecord.leg`.
     Armed template: `configs/webcast-fanout-arm.toml` (streamlink edge-join
     primary + tuned-ffmpeg backstop) staged via `scripts/stage-webcast-arm.sh`.
     Tests: `tests/e2e_fanout.rs`.

> ⚠ **BOX ADDRESS — re-read this before every event.** Instance
> `i-04c50d76572aa092e` has **NO Elastic IP** (verified 2026-07-27 via
> `NetworkInterfaces[0].Association.IpOwnerId == "amazon"`, i.e. an
> auto-assigned public IP). **The address changes on every stop/start.** It
> went `44.197.147.18` → `3.239.237.41` on the 07-27 resize, and every doc
> line below that names the old address was stale within minutes. Resolve it
> live, never from a doc:
> `aws ec2 describe-instances --instance-ids i-04c50d76572aa092e --query 'Reservations[].Instances[].PublicIpAddress' --output text`
> Instance type is **t3a.xlarge** since 2026-07-27 (4 vCPU / **2 physical
> cores**; it was t3a.large = 2 vCPU / 1 physical core before).

**Monitor staging (all validated 2026-07-20 evening, box `44.197.147.18`
— address now stale, see the box-address note above; instance
i-04c50d76572aa092e, UP through Thu by operator decision):**
- Box binary REBUILT from the current webcast-lane tree (the 07-17 binary
  rejected the `label` field — the Sunday-rebuild trap fired exactly as the
  DPZ prep note predicted). streamlink 8.4.0 installed (pipx) + symlinked.
- 4-leg smoke vs DW live HLS (90 s): all legs alive; **xcorr from the
  us-east vantage: pipe→pipe_tuned −11.65 s (peak 0.999), pipe→streamlink
  −13.65 s (peak 1.0)** — the 07-17 CDMX ladder replicates from the box
  (reference stream, NOT an event venue; event numbers still pending W1).
- Per-event kits generated by `scripts/stage-webcast-capture.sh` (template =
  `configs/webcast-gev-capture.toml`): config + `~/run-<ev>-capture.sh` with
  the URL-file + placeholder REFUSING guards. A generated config was
  live-validated (ALK copy, 15 s, 4/4 legs). URL files are cleaned — guards
  armed.
- Post-event tape pulls: `scripts/fetch-tape.sh <EVENT_TICKER>` (public
  trades endpoint, no auth) → `state/<EVENT>/tape-<date>.json`.
- **G2 fanout box smoke PASSED (2026-07-23 04:01 UTC, box, DW live HLS,
  90 s bounded legs, Kalshi-less via `replay`):** binary rebuilt from the
  fanout tree (1m54s incremental); 2 legs (streamlink edge-join +
  pipe_tuned) both alive, 4462/4494 frames, 0 drops, clean EndOfStream.
  UKRA + PRES heard on BOTH legs — first leg fired exactly one intent each,
  the other leg's copies ~450 ms later absorbed by the shared one-shot
  ("detection ignored"), incl. a refractory-spaced repeat. `per_leg` +
  per-detection `leg` labels verified in the JSON
  (`state/fanout-smoke-report-2026-07-22.json` on the laptop). Detect
  latency 160–240 ms class, `detect→decision` p99 = 1 µs (mutex fan-in
  cost invisible). Fanout is GO for the Thu mixed-leg runs (G3).
- **Thu 07-23 G3 staging (done overnight 07-22→23):** Track A verdicts —
  ALL THREE candidates **webcast-only** (full sweeps pinned in
  `configs/dial-ins.toml`): INTC no dial-in since ≥2011; AAL none ever
  (2015→2026), analyst bridge = private Zoom-style webinar; **LMT none in
  25 years of filings AND its call time MOVED to 8:30 AM ET** (since Q4'25 —
  the 11 AM prior is stale), so LMT conflicts with AAL. **Slate revision
  (user decision): LMT is the 8:30 PRIMARY** (arm + box capture; player
  `edge.media-server.com/mmc/p/k8rv9ajf`), AAL demoted to best-effort
  secondary capture (`…/mmc/p/jkbexuu7`); INTC unchanged at 5 PM. Both
  morning events + INTC run AUTONOMOUSLY (operator absent): registration
  identity bdelriom@actinver.mx / Actinver on all Notified players (watch
  for analyst-grade differences — the player has a dial_in_instructions
  surface); Chrome-automation surfaces the m3u8 (read network requests on
  the player), then the standard launch chain. LMT staged same as
  AAL/INTC: `webcast-lmt-arm.toml` committed, preflight PASS, box kit up. Probe configs generated (`twilio-live-{aal,intc}.toml`) +
  webcast fanout arm variants (`webcast-{aal,intc}-arm.toml`, committed);
  both preflights PASS (pinned binaries in `state/<EV>/earshot-pinned`);
  box kits staged; per-event account envs `~/.earshot/{aal,intc}.env`.
  ⚠ **INTC player auth gate**: `edge.media-server.com/mmc/p/rdz8ppwz/`
  (start 21:00Z, confirmed via player API) returns
  `authentication_required=true` + empty playlists pre-auth — an in-player
  email guestbook likely gates the m3u8. DRY-RUN the guestbook well before
  5 PM ET; if email-allowlisted, capture may be blocked (record the finding
  either way — it's a venue-table row).
- **07-23 ~2:20 AM dress rehearsal (exact autonomous mechanism, DW live) —
  PASSED after fixing two launcher bugs:** (1) CRLF manifests (DW/Akamai)
  leave a trailing `\r` on the variant URL → curl "malformed URL" → gate
  never flips (fixed: `tr -d '\r'`); (2) `curl | grep -q` SIGPIPEs curl on
  first match and under `set -o pipefail` the pipeline rc goes nonzero →
  `&& break` silently never fires (fixed: capture body, then grep). After
  the fixes: gate flipped on the live manifest, fanout arm booted off the
  pinned binary (13 LMT markets active, books live, dry-run confirmed),
  both legs spawned, STOP → kill-to-cancel 16 ms + disarm + ledger flush.
  All three players registered (Actinver identity, keep-me-logged-in):
  LMT + AAL straight through; **INTC's auth guestbook ACCEPTED the
  registration** — the allowlist risk is retired. ⚠ NEW FINDING: after a
  STOP-kill, a later SIGTERM does NOT terminate the arm (kill watcher is
  one-shot) — for endless webcast streams, wind down by killing the leg
  subprocesses (ffmpeg/streamlink) so sources EOF and the pipeline drains
  a clean report; fix the watcher before relying on STOP→SIGTERM sequences.
  Morning caveat: initialize network-request tracking BEFORE clicking play
  (the first `read_network_requests` call starts capture).
- **Fri 07-24 AXP/VZ staging (2026-07-23 evening; scope per user decision:
  capture BOTH, arm ONE in probe posture):** access recon from press
  releases/IR pages — AXP webcast on **Q4 Inc** (`events.q4inc.com/
  attendee/475264006`), VZ **self-hosted**, player link posts ~7:00 AM ET
  day-of; NO public dial-in found for either (IR-reply inbox check blocked —
  Outlook session expired, needs operator password). Both platforms are
  NEW (not Notified): run `manifest-probe.sh` FIRST — a long-window venue
  re-activates the fetch ladder (handoff §4), LL-HLS opens W3. Arm target
  chosen by Kalshi mention-market liquidity (recon JSON in box
  `~/friday-stage/`); arm runs ON THE BOX via `launch-webcast.sh
  --wait-live` (probe posture; refuses fill flags by design), account
  `~/.kalshi/earshot_env`.
- **💰 AWS COST CAP (standing user directive, raised 2026-07-25): $60/month
  GROSS usage** (was $30, pinned 2026-07-23) until the system is profitable.
  Measured via Cost Explorer 07-23: July 1–23 gross $5.07, promo credits
  −$5.07, net charged $0.00. Box = **t3a.xlarge since 07-27** ($0.1504/h ≈
  $3.61/day + EBS ~$3/mo) ⇒ 24/7 running breaches even the raised cap
  (~$110/mo gross) — the **stop-when-idle policy is mandatory** and is now
  twice as load-bearing as it was on t3a.large: stop after each event's
  post-mortem, start T−1 evening (night-before preflight cadence).
  EBS + WAVs persist across stops. ⚠ There is **no Elastic IP** — an earlier
  version of this line claimed one keeps the address stable and that was
  never true; see the box-address note above. Budget tripwire
  `earshot-monthly-cap-30usd` (acct 986420598115) emails
  bdelrio333@gmail.com at 50/80/100 % actual — **still armed at the OLD $30
  threshold; it will now fire early rather than late** (acceptable, but
  re-point it if the noise gets in the way). GPU bursts (P2/G2b,
  g5.xlarge ≈ $1.01/h) must be provision→run→terminate, hours not days.
  ⚠ NETWORK INCIDENT 07-23 evening: the CDMX ISP
  began dropping ~all traffic into AWS IP space (box SSH, Kalshi API, EC2
  API, CloudFront incl. q4inc + Notified media hosts) — traceroute dies at
  the carrier edge (201.174.x). All box work queued behind retry loops;
  laptop fallback = phone hotspot. AXP player registration deferred to a
  working window (Q4 attendee pages typically register-at-join).
  ⚠ **`prepare` keyword-generator BUG found 07-23 night (OPEN — code fix
  needed in the prepare layer):** numeric/symbol market subtitles produce
  degenerate keywords — INTC "…/ 18A" → variants `["a","as"]`; VZ "5G" →
  **phrase** `"g"` with variants `["g","gs"]` (a single-letter keyword =
  constant false fires). Also written-vs-spoken mismatches: "NII" needs
  variant "net interest income" (ULTR-class lesson: the detector hears
  SPOKEN forms; markets settle on rule text). 07-24 configs were hand-
  sanitized on the box (5G → "five g"; degenerates stripped; nii variant
  added) and re-validated with compile-keywords. Until the generator is
  fixed, EVERY prepare-generated config gets a variant audit before arming.

**Scheduled routines (armed 2026-07-20 evening):** 15 one-shot local cron
jobs in the 07-20 Claude session — per-event prep reminders (~40 min
pre-call) + post-event tape pulls/analysis (~2.5 h after call start), plus a
Tue-evening staging check for Wednesday. ⚠ SESSION-BOUND: they die if that
Claude session closes. If it does, re-arm by asking a new session to
"recreate the week's webcast monitor cron jobs from RUNBOOK §Week of 07-20"
— every prompt is reconstructible from this section (prep = runsheet
reminder + box check; post = `scripts/fetch-tape.sh <EVENT>` + box
report.json collection + tape/Δw analysis per COMPETITOR-ANATOMY §Webcast
hypotheses). Cloud routines were considered and rejected: the branch is
unpushed, no GitHub App on the repo, and the box SSH key is laptop-local.

**⚠ Capture health-check rule (learned GOOGL 2026-07-22, cost 8 min of tape):**
`capture-bakeoff` holds all frames IN MEMORY and writes `<label>.wav` + `report.json`
only at END of run — an empty record-dir mid-run is NORMAL. Mid-run health =
process-level only: the legs' ffmpeg/yt-dlp children exist and consume CPU
(`ps -o pid,pcpu,etime,command`). Never kill/restart a capture without
process-level evidence of death; a kill mid-run loses everything recorded so far.
Also: `yt-dlp --wait-for-video` pre-live is unvalidated at the live transition —
the codified home of that fix is `scripts/launch-webcast.sh --wait-live`
(polls `is_live`, launches on True; refuses configs still carrying the flag).

**Morning runsheet per event (attended; ~15 min before the call):**
1. Open the issuer IR player in a US-VPN browser; surface the `.m3u8` from
   network requests.
2. `ssh ubuntu@$(aws ec2 describe-instances --instance-ids i-04c50d76572aa092e --query 'Reservations[].Instances[].PublicIpAddress' --output text)`
   — **resolve the address, never paste it from here** (no Elastic IP; it
   changes on every stop/start). Then:
   `echo "<m3u8>" > ~/<ev>-stream.url`,
   `sed -i "s|__<EV>_URL__|<m3u8>|" ~/ear/configs/webcast-<ev>-capture.toml`,
   `~/ear/scripts/manifest-probe.sh "<m3u8>" | tee ~/<ev>-capture/manifest-probe.txt`
   (cadence / LL-HLS gate / PDT absolute lag — re-run once mid-call),
   `~/run-<ev>-capture.sh`, watch `~/<ev>-capture/run.log`.
3. **Fanout arm (when armed alongside the capture):** stage once with
   `scripts/stage-webcast-arm.sh <lc> <EVENT_TICKER> <START_ISO>`, paste the
   `prepare` keywords, then launch from a PLAIN shell:
   `scripts/launch-webcast.sh --config configs/webcast-<lc>-arm.toml
   --account <env> --wait-live ~/<lc>-stream.url --wait-kind hls`
   (the launcher waits for the live flip itself — post-mortems #1–#3).
   Same-audio note: the arm's legs and the capture's legs share the URL file.
   **Mixed-leg variant (bridge-class events, e.g. Thu AAL/INTC):** take the
   `prepare`-generated Twilio arm config, add ONE webcast `[[sources]]` pipe
   leg (tuned-ffmpeg cmd from the fanout template, distinct `label`) and
   `[arbiter] mode = "fanout"` — the bridge leg fires first as always; the
   webcast leg is the TARI-class recall backstop, and its late detections
   are absorbed by the one-shot (proven in `tests/e2e_fanout.rs`). Launch
   via the normal launch-event.sh phone flow (the webcast leg needs its URL
   file written pre-call; a dead webcast leg is leg-local and harmless).
4. Never debug legs 3–4 during a call — legs 1–2 are the baseline.
5. Post-call: `scripts/fetch-tape.sh <EVENT_TICKER>`; scp report.json +
   manifest-probe.txt; Δw analysis per COMPETITOR-ANATOMY §Webcast
   hypotheses; numbers → this file, verdicts → §Per-tape results. Fanout
   reports: check `per_leg` (died legs, per-leg detection counts) and
   per-detection `leg` labels for OR'd-recall wins (words one leg caught).

## Webcast lane (branch `webcast-lane`, 2026-07-17 — MERGE ONLY AFTER DPZ)

> Strategy + competitor model for this lane: `RACE-STRATEGY.md` (cross-lane
> synthesis) and `COMPETITOR-ANATOMY.md` §Webcast hypotheses; executable task
> list: `RACE-CHECKLIST.md` (2026-07-20).

`SourceConfig::Hls`/`Pipe` are now real (`src/source/pipe.rs`: ffmpeg/yt-dlp
subprocess → s16le PCM → AudioFrames; 6 tests incl. pipeline detection parity
vs replay). Launch: `scripts/launch-webcast.sh` — **observe posture enforced**
(no fillable/live flags accepted) until the webcast lag is measured. Purpose:
no-phone issuers (TSLA/NFLX class), webinar-shape unknowns, and ANY event
whose number hunt fails — degraded-GO instead of NO-GO. First target:
**GOOGL 07-22 (YouTube-only per Alphabet's PR), attended.** ⚠ The 07-19 DPZ
preflight pins from main HEAD — this branch stays unmerged until Monday
evening precisely so Monday runs exactly what was drilled.

Sequential, not concurrent (times pinned 2026-07-17 from issuer sources):
**GEV 7:30 AM ET = 5:30 CDMX** (gevernova.com PR) → fire 05:05, wake 05:00;
**GOOGL 4:30 PM ET = 2:30 PM CDMX** (abc.xyz IR page) → fire 14:05, operator
can attend. Same accounts, per-event state dirs. TSLA same day: NO-GO (no
phone leg). Prep state (2026-07-17):

- Configs generated via `prepare --fillable` + test-armed dry-run:
  `configs/twilio-live-gev.toml` (**12/12 markets active**),
  `configs/twilio-live-googl.toml` (**18/18 active**); keyword compile clean.
- ⚠ **Every word on both events is corpus-UNVALIDATED → all caps are 1¢
  observe.** As generated, Wednesday is observe-only. Making any word
  fillable requires the validation-loop promotion (or the risky `--fill-all`
  override) — **posture freeze Monday evening, after the DPZ outcome.**
- Env files staged with TBD numbers (`configs/gev-autorun.env`,
  `configs/googl-autorun.env`); preflight TBD gate governs go/no-go
  independently per event (preflights night of 07-21).
- Number hunts: GEV — IR ask to **investors@gevernova.com** and the direct
  IR line **+1 617 674 7568** (Michael Lapides, VP IR); GOOGL — IR ask to
  **investor-relations@abc.xyz**; plus registration/player checks per Track A.
- GOOGL expectation per VERIFICATIONS Q-MSPM: marquee-name hot words likely
  lose races at tight caps — value is detection data + any sitting asks.

## Fire-path latency pass (2026-07-17, branch `webcast-lane`) — code landed, on-box gates OPEN

**Measured A/B verdicts for every claim in this section: `docs/LATENCY-PROOF.md`**
(proven: ring wake ~17–21×/hop, warm-fire −645 µs on the first fire; measured
regression: burst-intake p95 ~+70 µs from the no-spawn executor; below-noise:
the rest on the laptop). Mechanical-latency-only pass (no change to what/when
we fire; risk gates and caps untouched). Landed: `book_age` histogram fix + new `ring_wake`/
`handoff→exec` edges; fire-first ordering on the detect thread (telemetry/logs
after `try_send`); executor telemetry locks deferred to after the wire
(`PendingEdges`); executor on a dedicated OS thread + current-thread runtime,
intents overlapped via `FuturesUnordered` (no per-intent spawn); ring
unpark-on-send (wake ≈ µs, not the 100 µs park backstop); `[perf]` section +
`src/perf.rs` (Linux core pinning / SCHED_FIFO / ring spin — default OFF,
no-op on every existing config); arm-time shadow warm-fire (signed GET, id
that cannot exist — places nothing); criterion bench `benches/hot_path.rs` +
`perf_cold_first_fire`; `perf_detector_rtf` now measures the chunk ladder
160/80/40/20. Host-tuning kit: `docs/OS-TUNING.md`,
`scripts/os-tune-{check,apply}.sh`.

**Gates that remain OPEN (all on the us-east box, none required for the next
event — every default keeps today's behavior):**
1. On-box baseline: `cargo test --release -- --ignored perf_` + `cargo bench`
   before/after `os-tune-apply` (jitter = p99−p50).
2. ~~`chunk_ms` 80→40~~ **CLOSED as WON'T-DO for sherpa (2026-07-17):**
   the paced QCOM 80-vs-40 grid measured the dial at **~0 ms** (onset→emit
   p50 837→838 ms, accuracy identical) — sherpa's internal chunk-16 quantum
   absorbs chunker timing, so earlier delivery just waits in the model's
   buffer. The dial becomes real again only on a small-quantum engine
   (streaming_ctc). Numbers: `docs/LATENCY-PROOF.md`.
3. `[perf] realtime = true` for an event: only after the systemd-fired
   rehearsal on the tuned box passes with real SIGTERM (kill latency must not
   regress; `tests/kill_sigterm.rs` posture) + `os-tune-check.sh` all-PASS.
4. `spin_hot_rings`: after (3), distinct cores per spinning thread (config
   validation enforces).

## Re-validation rehearsal 2026-07-17 (after the Linux-seam script changes) — PASSED

The platform-seam changes to `event-autorun.sh` / `launch-event.sh` /
`preflight-event.sh` (us-east Phase 1 work) touched the DPZ live path, so the
launchd-fired TEST rehearsal was re-run the same day: kickstart 10:53:15 →
armed **configs/rehearsal-gev.toml** (fresh `prepare` output — the old
rehearsal config's NFLX markets had FINALIZED and arm correctly refused them;
lesson: **rehearsal configs go stale as events settle — re-prepare against a
live event each week**) → real TTS dial → 7129 frames / 1783 chunks / 0 drops
→ clean long-stream close → rc=0 → [TEST] SUMMARY. Caveat, stated exactly:
**0 detections, expected** — the TTS loopback speaks NFLX-tailored text and
this run armed GEV vocabulary; the detection leg is binary-level, unchanged
since its 3/3 TTS pass on 07-16. The changed (shell) surface is fully
re-validated.

## DPZ readiness scorecard (2026-07-16) — graded by evidence class

Classes, strongest first (the CLAUDE.md standard: *validated = rehearsed with
the exact production mechanism*):
**WIRE** = live evidence against the real external system ·
**PROD-REHEARSED** = launchd-fired / real signal / minimal env ·
**PAPER** = primary source read, never exercised ·
**ASSERTED** = no evidence — treat as false until shown.

| Claim | Class | Evidence |
|---|---|---|
| Twilio capture vs a real bridge | WIRE | MS bridge 07-14 (CA5831c4, 3585 frames, 0 drops) + 85-min live run 07-15 |
| Full unattended chain (launchd fire → dial → detect → order → email) | WIRE | MS run 07-15 completed rc-clean; SUMMARY sent (FAILED email was the self-bootout bug, fixed) |
| Kalshi mention-category order acceptance | WIRE | probe orders on TARI/RECE 07-14 + live RECE IOC ack 07-15 |
| Warm order path (~102 ms send→hdrs) | WIRE | MS run report.json; VERIFICATIONS Q-MSPM |
| Supervisor generalized to per-event env | PROD-REHEARSED | launchd-fired rehearsal 07-16 08:53 (env sourced, TTS dial, 7124 frames, 3 detections, rc=0) |
| SIGTERM kill path / STOP / TERM→KILL escalation / frames guard | PROD-REHEARSED | 07-15 dress rehearsal (SIGTERM 6 ms; STOP 152 ms; escalation fired for real) |
| DPZ config arms | PROD-REHEARSED | dry-run test-arm 14/14 vs pinned binary 07-16 |
| prepare encode-validation + preflight keyword compile | PROD-REHEARSED | follow-up 6 closed 07-16, force-fail tested |
| notify() retry | PROD-REHEARSED | force-fail verified 07-16 (both attempts logged) |
| Preflight TBD-number FAIL-STOP (`--event-env`) | PROD-REHEARSED | full real preflight run 07-16 vs the DPZ event: gate FAILed on TBD/TBD → NO-GO, exit nonzero, PREFLIGHT email sent. Same run also PASSed: pin, keyword compile, both cred probes, mailer; flagged missing pmset wake + battery power (both expected to be set 07-19) |
| Bank published dial-ins (JPM/GS/C) | PAPER | primary-source PRs read; **never dialed** (only MS is wire-verified) |
| AAPL listen-line confirmation code | PAPER | IR page renders all states at once; code not date-stamped — re-verify at call time |
| RKLB bridge evidence | PAPER (weak) | search summary only — re-derive before acting |
| **Detection-triggered mention FILL** | **ASSERTED** | **has NEVER happened.** One real fill ever = M2 plumbing 07-10 (non-mention 1¢). DPZ is this test |
| **DPZ dial-in number** | **ASSERTED** | TBD — event is NO-GO without it (Track A above; deadline 07-19) |
| Competitive fill at tight caps | **ASSERTED (evidence against)** | Q-MSPM: best competitor's whole loop beat our *audio arrival* by ~400 ms on MS-RECE. Until audio transport + detector chunking shrink, expect to lose sub-50¢ races on contested words. ⚠ 2026-07-27: **RECE settled NO** — the latency deficit is real but that race had **negative EV** (−26¢ at our cap), so this row evidences *lateness*, not *forgone profit* |
| Mid-call stream recovery | ASSERTED | follow-up 1 open: a 30 s stall ends the run; recovery = full redial re-paying ~30 s warmup |
| Post-STOP arm SIGTERM immunity | KNOWN BUG (covered) | follow-up 5: TERM→KILL escalation covers it, fired for real 07-15 |
| Book freshness at decision | INSTRUMENTED, UNPROVEN | `book_age` added 07-16 after the MS RECE 20¢-vs-98¢ discrepancy; first data next live run |

Bottom line for DPZ: **plumbing is wire- or production-validated end to end;
the two things that have never been true remain the two go/no-go items — a
number to dial, and a mention fill.** The fill test itself carries a measured
headwind (the 400 ms audio deficit), so a DPZ zero-fill outcome with clean
detections would be *consistent with the known latency gap*, not a new
failure — judge it against `book_age` + the public tape, per Q-MSPM.

## Fills — precise status (do not over- or under-claim)

The system HAS filled one real order: the **M2 acceptance fill, 2026-07-10**
(1¢ × 1 IOC on a non-mention market, 231 ms round-trip, fee $0.0007 — wire
evidence pinned in `VERIFICATIONS.md` M2 live-drill pins). What has NEVER
happened is a **detection-triggered mention fill**: the full hear-keyword →
cross-ask → fill loop. The MS run sent 1 order (canceled unfilled, IOC).
Ledger `spent_cents` values prove sends, not fills (caps are debited on
send, never refunded — `src/risk.rs::record_send`). The next live event with
the fillable posture is that validation.

⚠ The widely-repeated "0 fills, **4 LOST_RACE**" headline for the MS run is
a misread (corrected 2026-07-16, post-mortem below): all 4 "LOST_RACE"
records were **1¢ observe-tier intents against 21–70¢ asks — the designed
non-crossing outcome**, not lost races. `ExecReport` now splits the counters
(`lost_races` vs `observe_noncross`, `src/kalshi/executor.rs`). The run had
exactly ONE genuine race (RECE), and its post-mortem is follow-up 2.

# LIVE RUN — Wednesday 2026-07-15, Morgan Stanley Q2 call (COMPLETED)

> The JPM section below is retained as the original template; the JPM call
> (2026-07-14) passed without a live run — its markets are finalized. The MS
> run below was fully drilled the night of 2026-07-14 (all drills PASSED:
> Twilio hold probe, kill-drill 140 ms, mention probes on TARI/RECE, dry-run
> arm 14/14 markets, loopback rehearsal with 3/3 keyword detections, and an
> end-to-end `launch-event.sh` dress rehearsal against the real MS bridge).

Posture: **mention-fill, tiered** (`--i-am-live --allow-mention --fillable`) —
recession (cap 26¢) and tariff (cap 45¢; live ask was 38¢ on 07-14 evening,
the generated 14¢ cap could never fill) may cross the ask and FILL; the other
12 words observe at 1¢ non-crossing. Worst case 83¢, backstopped by the 100¢
event notional cap and the per-event $1 ledger (fresh:
`state/KXEARNINGSMENTIONMS-26JUL15/probe_ledger.json` = 0¢).

- **Call**: Wednesday 2026-07-15 **8:30 AM ET = 6:30 AM CDMX local**
  (results ~7:30 AM ET = 5:30 AM local).
- **Dial-in** (primary source morganstanley.com IR, verified 2026-07-14):
  **+1-844-886-2200** (intl +1-646-307-1096), passcode **400700#**.
- **Markets**: `KXEARNINGSMENTIONMS-26JUL15` — 14 active (verified via API
  2026-07-14); rules stored `state/KXEARNINGSMENTIONMS-26JUL15/rules/`.
- **Account env**: `~/.earshot/ms.env` (Twilio + Kalshi combined, created
  2026-07-14, mode 600).

## ⚡ AUTONOMOUS MODE — ARMED (operator asleep is OK)

`launchd` job `com.earshot.ms-autorun` fires **06:05 local** on 2026-07-15 and
runs `scripts/ms-autorun.sh` unattended: launch → dial → auto-redial on the
bridge's ~60 s pre-conference disconnect (DTMF wait variants cycle per
attempt) → redial window closes 07:40 → strike-close table fetched → SUMMARY
email to MAIL_TO. LAUNCHED and SUMMARY/FAILED emails bracket the run. All
output: `state/KXEARNINGSMENTIONMS-26JUL15/autorun.log` (+ summary + strike-close.json).
Verified the night before: mailer live-tested, full dry-run with a REAL
auto-redial against the bridge, and a launchd-fired dry-run (runs=1, exit 0).
- Abort before 06:05: `launchctl bootout gui/$(id -u)/com.earshot.ms-autorun`
- Abort mid-run: `touch state/KXEARNINGSMENTIONMS-26JUL15/STOP` (cancels
  orders + stops redials), then `scripts/twilio-hangup.sh <CallSid in log>`
- Requires: AC power + lid open (box never sleeps — resident caffeinates).
- Phase-0 note: the **webcast leg is skipped** (needs a human); the
  strike-close table (step 2) and warm-path report.json pin (step 4) are
  captured automatically.
- The job self-disarms (bootout) after the live run; a date guard in the
  script is the backstop. Self-disarm is the script's **final act** — booting
  out its own label SIGTERMs the script, so nothing may run after it. Before
  the 2026-07-15 fix this ran before `finished=1`, so a live run sent a
  spurious "FAILED (script died)" email right after the SUMMARY email (the
  2026-07-15 MS run itself completed fine).

### Hardening added 2026-07-15 (post-run audit — applies to EVERY future unattended event)

**DRESS-REHEARSED 2026-07-15 (production mechanisms, per the CLAUDE.md
convention).** Three launchd-fired runs against `configs/rehearsal-nflx.toml`
(active NFLX mention markets, dry-run, real Twilio calls to the dedicated
auto-answering TTS number **+1 762 721 2978** — voice webhook plays the
keyword script; bought 2026-07-15, ~$1.15/mo, reusable for every preflight).
Evidence in `state/KXEARNINGSMENTIONNFLX-26JUL02/autorun.log`:
- launchd fire + pinned binary (sha in `pinned.info`, clean commit) armed in
  <1 s — no fire-time compile;
- real 8 kHz μ-law stream, live detections (gaming/hollywood/acquisition),
  executor consulted the live book (centi-contract quantities visible);
- **SIGTERM mid-live-stream**: kill switch ran orders-cancel + disarm +
  ledger flush in 6 ms, exit 143, supervisor alerted + redialed;
- **STOP mid-attempt**: arm cancel/flush in 152 ms AND the supervisor watcher
  hung up the call leg and stopped the loop;
- **frames evidence**: two long zero-frame attempts were refused as "call
  complete" (no-audio alert + redial); the real 180 s / 7126-frame stream
  was accepted;
- **TERM→KILL escalation fired for real**: a post-STOP arm ignored SIGTERM
  (orphaned handler — follow-up 5 below) and was SIGKILLed by cleanup;
- rehearsal also caught `prepare` emitting a phrase the detector cannot
  BPE-encode ("warner bros." — the period): arm REFUSES the whole config.
  **Preflight/operator must test-arm any newly generated config** until
  prepare sanitizes phrases (follow-up 6).

- **Preflight is REQUIRED the night before**: `scripts/preflight-event.sh
  --config <cfg> --account <env> --fire-hhmm <HHMM>`. It builds and PINS the
  binary into the event state dir (`earshot-pinned` + `pinned.info` with
  commit/sha256), live-probes Twilio + Kalshi auth, checks disk ≥ 5 GiB,
  tools, timezone, a USER-scheduled `pmset` wake before fire time, and
  live-tests the mailer. The supervisor runs the pin — never a fire-time
  `cargo run` (a dirty tree / cold build / full disk at 06:05 is unrecoverable
  with nobody watching; the disk hit 100% TWICE this week).
- **Mid-window alerting**: every early-exit guard (wrong date, already
  running, window already closed) emails; the first failed attempt and every
  5th consecutive failure email WITH the log tail; a long call leg with no
  audio frames emails and keeps redialing ("wall time ≠ audio" — a dead
  tunnel parked on `<Pause>` looks like a 90-min success otherwise).
- **STOP now works mid-call**: a watcher hangs up the recorded CallSid
  (`state/<event>/call.sid`) and SIGTERMs the launcher; arm's kill watcher
  (which now handles SIGTERM — the supervisor's actual kill signal) cancels
  session orders, flushes the ledger, writes a last-words report, and exits.
- **Cleanup kills recorded PIDs only** (`arm.pid`/`ngrok.pid`, TERM→5 s→KILL),
  with a logged port-8080 sweep as last resort.
- **Test mode places no real call** by default (`--no-dial`);
  `EARSHOT_AUTORUN_TEST_DIAL=1` restores the full dial rehearsal.

Deferred follow-ups from the same audit (tracked, deliberately NOT done yet):
1. Mid-call stream recovery: a 30 s audio stall or socket close ends the whole
   run (`twilio_ws.rs` idle/EndOfStream); recovery is a full shell redial that
   re-pays ~30 s of warmup. Consider in-process re-accept within a call window.
2. ~~Warm-path order latency: connection/presign reuse is a LOST_RACE lever~~
   **POST-MORTEM COMPLETE 2026-07-16 — the premise was wrong on every count.**
   Evidence: `state/KXEARNINGSMENTIONMS-26JUL15/report.json` + the public
   Kalshi trades tape for `…MS-26JUL15-RECE` (pinned in `VERIFICATIONS.md`
   Q-MSPM). Findings, in order of importance:
   - **The RECE race was lost upstream of everything tunable in the order
     path.** Tape: the first competitor fills printed **13:27:10.274Z**,
     sweeping RECE YES 27¢→95¢ (then to 98¢ by 10.507). Our audio chunk
     containing "recession" reached the pipeline **≈13:27:10.672** (decision
     wall-time minus the 91.3 ms capture→decision edge) — i.e. **the
     competitor's entire hear→fill loop finished ~400 ms before our audio
     even arrived**. Our internal 91 ms and the 104 ms decision→ack could
     both be zero and we still would not have filled at ≤26¢. The binding
     constraint is **audio-arrival latency** (bridge → Twilio media →
     ngrok → us), plus the detector's 80 ms chunking (~60 ms of the 91 ms
     internal edge is chunk assembly — `preproc→chunk` p50 60.5 ms).
     That sets the low-latency milestone's measured target: audio transport
     must shed hundreds of ms; the detector edge tens.
   - **`presign_age` 1.17 s is not a latency cost.** The presigner refreshes
     every 10 s and `get()` is a wait-free ArcSwap read; 1.17 s is merely
     time-since-refresh, far under the stale-fallback threshold (no "inline
     signing" warning in the run log). Nothing to fix.
   - **`send→hdrs` 102 ms is consistent with an already-warm h2 socket**
     (CDMX→Kalshi RTT + matching-engine time; a cold TLS handshake would
     read 250 ms+). The conn-warmer + presigner were live during the run
     (commit 8421741). Remaining order-path lever is geography (a us-east
     execution box), not code — deferred, likely moot given the 400 ms
     upstream deficit.
   - **Open forensic hole, now instrumented:** the decision-time book showed
     best_ask 20¢ at 10.763 although the tape had swept to 98¢ by 10.507 —
     either our WS book was ≥250 ms stale or an MM briefly re-quoted 20¢.
     Undecidable post-hoc then; the executor now records `book_age`
     (snapshot age at decision) per order so the next run settles it.
3. ~~Event windows hard-coded per event in `ms-autorun.sh`~~ DONE 2026-07-16:
   renamed `scripts/event-autorun.sh`, all event-specific values (name, config,
   number, passcode, label, date guard, `END_HHMM_LIVE`,
   `COMPLETE_EARLIEST_HHMM`, account env, tickers) come from a per-event env
   file (`configs/<event>-autorun.env`) named via `EARSHOT_EVENT_ENV` in the
   plist. Missing/incomplete env → refuse + email. `prepare`-generating the
   env file remains the follow-on.
4. ~~Test-mode rehearsals share the LIVE event's state dir~~ DONE 2026-07-15:
   `EARSHOT_AUTORUN_TEST_CONFIG` gives rehearsals their own event id/state dir.
5. After a STOP-triggered kill, the still-running disarmed arm is
   SIGTERM-immune (the kill watcher task returned; the installed handler's
   stream is orphaned, so TERM is swallowed). The supervisor's TERM→KILL
   escalation covers it (fired for real in the 2026-07-15 rehearsal), but the
   watcher should either keep draining signals or exit the process after a
   STOP kill.
6. ~~`prepare` can emit detector-unencodable phrases~~ DONE 2026-07-16:
   `prepare` now sanitizes variants into the detector alphabet AND
   encode-validates each against the real vocab, dropping (with comments) what
   cannot encode; `preflight-event.sh` additionally runs `compile-keywords`
   against the pinned binary. NOTE the second failure class found while
   fixing: the vocab contains `-`/`'` pieces, so "non-stop" ENCODES to tokens
   speech never produces — a silently dead entry that encode-checking alone
   passes; sanitization is what prevents it (see `docs/layers/prepare.md`).
7. ~~`notify()` sends are single-shot~~ DONE 2026-07-16: one retry with a
   30 s backoff (immediate retry would re-hit the send-rate throttle);
   still best-effort/never-fatal. Force-fail verified (both attempts logged).

## Morning checklist (manual fallback) — ⚠ call is **6:30 AM local (CDMX, UTC-6)** = 8:30 ET

This box runs America/Mexico_City (UTC-6, no DST). Operator arrives 6:20 →
**10-minute window**. ONE command does everything (cleanup → sanity → launch
→ dial); it was dress-rehearsed end-to-end the night before:

```
cd ~/Code/ear && scripts/ms-morning.sh        # at 6:20:00 sharp
```

Timeline: ~6:20 launch → arm validates 14 markets + books (~40 s) → tunnel +
dial → "stream started" by ~6:22 → bridge hold until the 6:30 call start.
The bridge IVR disconnects silent callers after ~60 s pre-conference
(measured); if that happens pre-6:25, the script's dial can be re-run —
`scripts/twilio-dial.sh` line is printed on exit. Do NOT dial before ~6:15.

Second device: public MS webcast open before 6:30 — the Phase-0 resolver-race
measurement below is REQUIRED this run (wall-clock notes only; no recording).

Kill (either leg, any time):
- `touch state/KXEARNINGSMENTIONMS-26JUL15/STOP`  → orders: cancel + disarm
- `scripts/twilio-hangup.sh <CallSid printed at dial>` → audio leg
Report after the call: `state/KXEARNINGSMENTIONMS-26JUL15/report.json`.

If the DTMF passcode isn't accepted (bridge prompt timing), hang up and
re-dial manually with more leading waits: `--dtmf "wwww400700#"` (each w =
0.5 s). The dress rehearsal confirmed the bridge answers and streams audio;
passcode acceptance can only be proven when the conference is open.

### Keyword rehearsal results (TTS through real Twilio, night of 07-14)

All 14 words produced ≥1 Confirmed detection via the live detector settings
(fp32, chunk 80 ms). Detect latency 64–127 ms past capture. Caveats to
expect live:
- **tariff** (fillable, cap 45¢): fired on mid-sentence "tariff"/"tariffs";
  MISSED sentence-initial "Tariff…" after silence, twice. Matches the known
  onset weakness (corpus recall 0.90). A missed first mention may be caught
  on a later mention — budget stays armed until filled.
- **tailwind** (observe 1¢): 2/3 phrasings fired; weakest of the 14.
- **recession** (fillable, cap 26¢): fired cleanly on TTS; still has zero
  corpus positives — live recall unproven.
- middle east/iran, oil (2nd mention), tailwind (2nd) correctly suppressed
  by refractory/budget after the first hit.

## Phase-0 measurement — resolver race + warm path (REQUIRED this run)

The JPM template's "thesis measurement" (below) stays in force. This section
adds the **webcast leg**, which answers the single most important open
question (TELEPHONY-DETECTION-SPEC §10.3): do Kalshi resolvers act at
webcast speed (~10–30 s behind our PSTN feed → recall is the product) or at
PSTN speed (→ every 100 ms matters)? The answer steers the next milestone
(docs/ROADMAP.md, Phase 2 vs 2.5).

**No call-audio recording on any leg** — the standing policy (item 5 of the
thesis measurement) applies to the webcast too. Everything below is
wall-clock notes + data the system already writes.

1. **Webcast leg (second device, required).** Before the call, open the
   public MS webcast player and confirm audio. During the call, for each
   armed keyword you hear ON THE WEBCAST, note wall-clock to the second
   (paper or a text file with `date` pasted per line). The arm log's
   `decision intent` tracing lines carry our detection wall-clock for the
   same words on the PSTN leg.
   → **PSTN lead ≈ webcast-heard time − detection-log time** (our detector
   adds ~1–1.2 s after the PSTN utterance; subtract that mentally). First
   real measurement of the structural cushion (TWILIO.md pins it at an
   unverified ~10–30 s research estimate).
2. **Strike-close interval.** Post-call (already thesis step 3): for every
   strike that fired or should have, `GET /markets/{ticker}` →
   `close_time`. Tabulate per keyword: webcast-heard t, detection t, our
   ack t (report.json edges are monotonic; use the log line wall-clock),
   strike close t.
3. **Decision rule.** If strike-close consistently trails our ack by
   **> 5 s**, resolvers are webcast-speed → the recall workstream leads the
   next milestone. If closes land within ~1–2 s of our ack, the race is
   PSTN-speed → latency work leads. Pin the table + verdict in
   VERIFICATIONS.md as a dated entry.
4. **Warm-path pin.** Keep `report.json`. Post-call, read `send→hdrs`,
   `hdrs→ack`, `send→ack`, `presign_age` — this run is the first warm-path
   sample from an armed process (cold CLI probes were 128–134 ms send→ack;
   VERIFICATIONS open item 6). Pin the numbers and, if `RUST_LOG` included
   `earshot::kalshi::rest=debug`, the negotiated `http_version` to the
   orders origin.

---

# First live run — Tuesday 2026-07-14 (JPMorgan Q2 call)

Posture: **mention-probe** — 1¢ IOC × 1 contract per keyword, $1 cumulative
ledger, Mentions category permitted via `--allow-mention`. The goal is a
timing + executionability verification, not P&L. Monday 07-13 is the full
dress-rehearsal day (no Kalshi mention calls exist that day — verified
against the open-events API 2026-07-11).

## Call targets (verified 2026-07-11 from issuer press releases + Kalshi API)

All five banks publish **static dial-ins, no registration**:

| Rank | Call (ET) | Kalshi event | Dial-in | Passcode |
|---|---|---|---|---|
| PRIMARY | JPMorgan 8:30 AM | `KXEARNINGSMENTIONJPM-26JUL14` (18 mkts) | 888-324-3618 / +1-312-470-7119 | `1364784#` |
| backup | Goldman 9:30 AM | `KXEARNINGSMENTIONGS-26JUL14` (13) | 1-800-330-6730 | `7042022` |
| backup | Wells Fargo 10:00 AM | `KXEARNINGSMENTIONWFC-26JUL14` (16) | 1-888-673-9782 | `8320644#` |
| backup | Citi 11:00 AM | `KXEARNINGSMENTIONC-26JUL14` (14) | 1-805-309-2350 | `7142026#` |
| skip | BofA 8:30 AM (conflicts with JPM) | `KXEARNINGSMENTIONBAC-26JUL14` (16) | 1-877-200-4456 | id `79795` |

Re-verify the JPM number on jpmorganchase.com IR Monday (sourced from a
syndicated reprint). A same-morning relay (JPM→GS→WFC→C) is possible later;
first run is JPM only. `configs/twilio-live.toml` carries the JPM tickers
(TARI corpus-tuned, RECE corpus-FA-tuned, INFL engine defaults).

## Prerequisites (user actions, do Saturday — external latency)

1. **Twilio**: create account, UPGRADE from trial (trial injects a voice
   preamble and restricts destinations), buy a voice-capable number, enable
   US outbound dialing. Put creds in `~/.twilio/earshot_env`:
   `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`.
2. **ngrok**: install; ideally reserve a stable domain (free hostnames
   rotate per session).
3. **Fee schedule**: fetch https://kalshi.com/docs/kalshi-fee-schedule.pdf
   in a real browser (bot-blocked), diff vs VERIFICATIONS §Q5 Feb-2026
   formula. Blocks trusting the EV gate live.

## Sunday drills (in order, each blocks the next; ~$0.03 total worst case)

Check ledger headroom first: `cat state/probe_ledger.json` (cap 100¢).

```bash
source ~/.kalshi/earshot_env
cargo build --release

# 1. Kill switch still <1 s, unrelated orders untouched (non-mention ticker)
cargo run --release -- kill-drill --ticker <liquid KXBTCD strike> --auto

# 2. Ack shape unchanged (1¢ IOC, non-mention)
cargo run --release -- probe-order --ticker <same>

# 3. NEW: mention-market order acceptance at $0 (refuses if 1¢ would cross)
cargo run --release -- probe-order --ticker KXEARNINGSMENTIONJPM-26JUL14-TARI --allow-mention

# 4. Dry-run arm against the real JPM markets: validator stores rules
cargo run --release -- arm --config configs/twilio-live.toml --allow-mention
#    → READ state/rules/KXEARNINGSMENTIONJPM-*.json and fix variants in the
#      config if rules_primary differs from ["tariff","tariffs"] etc.
#      Exclude any "said N times" strike (one-shot machine can't trade it).

# 5. Twilio loopback rehearsal (docs/TWILIO.md §Loopback):
#    ngrok http 8080 → dial a <Play>-fixture TwiML Bin → 3 tone detections,
#    measured capture latency, raw JSONL captured; then repeat with sherpa
#    + spoken keywords; finally --i-am-live (probe, non-mention 1¢).
```

## Tuesday timeline (ET)

- **T−60 m (7:30)**: `cargo build --release`; NTP sane (arm enforces ±10 s);
  start ngrok, note host; `book --ticker KXEARNINGSMENTIONJPM-26JUL14-TARI
  --secs 30` (live book sanity, note asks); confirm config allowlist =
  exactly the armed tickers.
- **T−20 m (8:10)**: `rm -f state/STOP`; then
  `cargo run --release -- arm --config configs/twilio-live.toml --i-am-live --allow-mention`
  (validator → gate summary → WS snapshots → presigner → executor idle).
- **T−15 m**: `scripts/twilio-dial.sh --to +18883243618 --dtmf "1364784#"
  --stream-url wss://<ngrok-host>/media` — hold music should start flowing:
  watch frame logs, seq-gap warnings, drop counters.
- **Live from the start.** The 1¢ × 1 posture IS the safety margin, and
  mention words can land in the CFO's first sentence. Only if the feed looks
  unhealthy at T−5 (drops climbing, gaps, silence) restart the call leg or
  fall back to a shadow session.

> **Two events at once (relay/parallel calls):** this timeline is single-event.
> To run two simultaneous calls on two separate Kalshi + Twilio accounts, use
> `scripts/launch-event.sh` once per event (each with its own config, account
> env file, `--bind-port`, and `--state-dir state/<id>`) instead of the manual
> arm/dial above — see `docs/MULTI-EVENT.md` §8 for the full setup + checklist.

## Event-prep + tiered fill (auto-config, M3+)

Instead of hand-cloning a config, generate it from the Kalshi event:

```
earshot prepare --event-ticker KXEARNINGSMENTION<ISSUER>-<YYMMMDD> --fillable
  # → configs/twilio-live-<issuer>.toml. TIERED: only precision-vetted words
  #   (tariff, recession — the tuned_library) fill at the live ask; every other
  #   word is 1¢ observe (logs fires, spends nothing, can't starve the $1 ledger).
  # Review: set [event].starts_at, research the dial-in, sanity-check variants.
```

Live: `arm --config <cfg> --i-am-live --allow-mention --fillable`. Fillable
orders cross the ask up to `MENTION_FILL_CAP_CENTS` (50¢), bounded by the config
notional caps + the $1 probe ledger. `--fill-all` makes EVERY word fillable —
deliberate only (false-alarm + ledger-starvation risk on unvalidated words).

Post-call, promote unvalidated words WITHOUT recording (respects "do not record"
below): `scripts/validate-from-log.py --log <arm.log> --config <cfg> --transcript
<public-transcript>` → per-word precision/recall from the observe-mode fire log.
A word that fires cleanly (~0 false alarms) graduates into `prepare::tuned_library`
and auto-fills next call. The rigorous path (record → corpus v2 → `bench-detector`)
is the durable follow-on for the recall ceiling.

## Kill criteria — any one → `touch state/STOP` then `scripts/twilio-hangup.sh <CallSid>`

- an order fires on a word you did not hear;
- frame drops / Twilio seq gaps climbing, or `feed_age` refusals appear;
- any refusal you don't understand;
- ledger at/near cap.

## The thesis measurement (capture during + after the call)

1. Note wall-clock time when you HEAR each keyword (your ear, rough).
2. Detection log line timestamp (tracing) + `report.json` (all 11 edges).
3. After the call: fetch each strike's actual close/settle time via REST —
   `close_time`/settlement on `GET /markets/{ticker}` — Kalshi humans close
   the market on utterance. **our-ack-time vs market-close-time is the
   measured edge**; positive margin = the strategy works.
4. `cargo run --release -- orders` + fills reconciliation; pin any new wire
   facts (mention-market ack on IOC, close behavior) in VERIFICATIONS.md
   with the evidence; write the run log.
5. Do NOT record call audio anywhere.

## Post-run

Verdict on: capture health (drops/gaps), detection correctness (per ear),
order acceptance (ack shapes), timing edge (ack vs close). THEN the M4 caps
conversation, with data.
