# 26AUG20 venue routing (box-primary / laptop-failover)

Operator ruling 2026-08-19: "priority is capturing in the box with orders sent
from the box; if the box cannot ingest, capture and work from the laptop — one
or the other sends actual trades, never both." Decided from measured
reachability (2026-08-19T19:00Z), not assumption.

| event | call UTC | venue | class | PRIMARY | how resolved | laptop role |
|---|---|---|---|---|---|---|
| FUTU 26AUG20 | 11:30 | edge.media-server.com/mmc/p/ex2zp2eh | Notified MMC (reg wall) | **BOX** | laptop browser-resolve (profile futu, --linger --reject-endlist), pcm-gate, url-push-job → box+laptop | url-push-job source + failover arm |
| WMT 26AUG20 | 12:00 | youtube.com/live/hOdyv3MKhPE | YouTube Live | **LAPTOP** | youtube-resolve on the laptop (box is bot-walled at the segment layer) | primary capture |
| DE 26AUG20 | 14:00 | events.q4inc.com/attendee/531842209 | Q4 (no wall) | **BOX** | q4-api resolver ON the box (attendees.events.q4inc.com/rest/v1/event/531842209; broadcastUrl mints at call) | failover arm only |

Measured facts:
- DE: box `curl` of the Q4 attendee API returns the event JSON (eventStart
  2026-08-20T14:00:00Z, broadcastUrl empty pre-call) — box resolves itself, no
  laptop push needed for the primary.
- FUTU: box `curl` of the media playlist `…/HLSv4.m3u8` = 200/1877B, so the box
  CAN ingest; it just has no browser to pass the reg wall, so the laptop
  resolves+pushes. The player's OWN `metafile…` master yields 0 frames via
  streamlink (that is what killed TOL 26AUG19) — resolve the `media.main.pro2.mas…`
  playlist and pcm-gate before publishing.
- WMT: box `yt-dlp -g` on the live id → bot wall; laptop vantage only. Never
  co-locate a second live event on the laptop that day (WMT is the only laptop
  primary; FUTU/DE laptop legs are failover-only and mutually exclusive with
  their box legs).

READY (per this day) = for each primary host, the venue URL has been resolved
AND pcm-gate proved decodable audio FROM THAT URL ON THAT HOST, the resolve/push
is a LOADED launchd/systemd job, and a watchdog + failover are loaded.
