# Prototype configs — ILLUSTRATIVE ONLY

Not registered in `configs/`, never armed, never referenced by any
production script. Modeled directly on `configs/webcast-abcwnt-capture.toml`'s
already-proven capture-only shape (`capture-bakeoff`, not `arm` — no orders
can be placed by that command; the `[kalshi]`/`[[keywords]]` sections exist
only to satisfy config validation, exactly like WNT's own comment says).

Before actually running one of these:
1. Confirm the live stream URL resolves (`yt-dlp -g -f bestaudio "<channel>"`)
   and decodes cleanly — mirrors `scripts/venue-probe.sh`'s PCM-bytes gate.
2. Verify the source-identity risk flagged in `../venue-survey.json` for
   that candidate — confirm the channel actually carries the specific show/
   event, not an adjacent one (the WNT lesson).
3. If a real Kalshi board is open for that ticker at capture time, replace
   the placeholder `[[keywords]]` below with the board's actual
   `rules_primary`-derived words (`earshot prepare` does this automatically
   for a real event — see `docs/EXPANSION.md`'s "what a new event class
   must supply" contract). The words below are illustrative placeholders,
   NOT pulled from a real open board (none was open for TBPN at survey
   time) — do not treat them as verified.
