# TBPN leg-1 (VOD/caption) build — findings

## Channel structure differs materially from WNT

`@TBPNLive` does NOT post one cleanly-titled, date-stamped "Full Broadcast"
VOD per day the way ABC does for WNT. The general `/videos` listing mixes in
short segment clips and "Diet TBPN" recap compilations (durations as low as
~1400s) with no date in any title. The `/streams` tab (archived livestreams)
is where the long, full-episode archive actually lives — durations there
cluster **6500-11500s** (~2-3 hours), consistent with the claimed 11am-2pm PT
daily window, but even there, identification required a **duration-window +
live-fetched `upload_date`** match (flat-playlist listings don't populate
`upload_date`; each candidate needs a full per-video metadata fetch — a real
cost `wnt-vod-fetch.sh`'s one-request date-in-title search avoided). This is
now built into `tbpn_vod_fetch.sh`, with the same refuse-don't-guess
discipline as WNT's AMBIGUOUS case if more than one candidate matches a date.

**Caveat carried into `meta.json` for every banked session**: there is no
explicit "this is the full episode" label anywhere — the duration-window
inference is not confirmed against ground truth beyond one day's manual
sample. Worth spot-checking a couple more dates before trusting this at
scale.

## Real test case banked: 2026-08-04

`_hMRv35F-0w`, 9,269s (2h34m), "Spoon = Bent, Apple vs OpenAI, Snap Rips,
Ads in BMW, The Most Feared (John B. Quinn) Live" — captions only
(`--no-audio`), banked to `corpus/20260804/`.

- **28,263 words, 183 words/min** — dense, continuous coverage; **zero gaps
  >5s** across the full 2h34m (no long uncaptured silence/dead-air stretches).
- **Caption quality is NOT uniform across the episode**, a difference from
  WNT worth flagging explicitly: the opening ~20s is a produced musical
  intro (rap-style jingle, "Boardroom hush, then the fear takes hold...")
  captioned in ALL CAPS with visible ASR garbling ("HEAR THAT malo through
  the smoking floor" — "malo" is not a real word here). The bulk of the
  episode (normal conversation) captions cleanly in natural lowercase with
  ordinary filler words ("you know", "right?") — good quality, comparable to
  what WNT's pattern already tolerates via its fuzzy-match fallback. The
  file's last 3 words are mid-word-truncated ALL CAPS fragments ("THEN THE
  VER..."), suggesting another produced outro segment same as the intro.
  **Implication for a future pack-lag join**: intro/outro musical segments
  should probably be excluded from word-matching (same spirit as WNT
  excluding non-speech `[music]`/`[applause]` cues), and the fuzzy-match
  tolerance tuned on WNT's scripted newscast should be re-validated against
  TBPN's casual conversational register before assuming it transfers.

## Market status — re-confirmed, and more nuanced than "never traded"

`KXTBPNMENTION` currently has **zero open events** (confirmed live), but it
is **not a market that's never traded** — it has **12 historical events**
going back to 2025-10-23, most recently `KXTBPNMENTION-26JUL23` (settled,
all markets `status: finalized`, e.g. "Intel", "TSMC / Taiwan", "Data
Center" strikes). So this is a real, periodically-active market that opens
opportunistically (roughly monthly-ish per the 12-events-over-~9-months
cadence) rather than a dormant/untested one — consistent with the census
`cadence: "monthly"` tag actually being closer to right for the *market*
even though the *show* airs daily. The leg-2 Kalshi join was explicitly
**not built** in this pass per scope — but a `KXTBPNMENTION-26JUL23` trade
tape likely exists and could be joined against a same-date `/streams` VOD by
a future pass without needing to wait for a new board to open.
