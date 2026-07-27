#!/usr/bin/env python3
"""A4 — the PHYSICAL BOUND: could ANY consumer of the public feed have held this
audio at the instant the competitor swept?

WHY THIS IS DIFFERENT FROM EVERYTHING ELSE IN THE CAMPAIGN. Every other race
instrument is COMPARATIVE — B1 compares our legs to each other, V3/V5/A2 compare
two legs of ours, Δrace compares us to a co-listener. `docs/LATENCY-PROOF.md`
names why that class is blind: "Any buffering shared by both legs cancels exactly
and is invisible to that instrument." This one is ABSOLUTE. Its reference is the
encoder's own `EXT-X-PROGRAM-DATE-TIME`, so it bounds what is POSSIBLE rather
than what we happened to do.

THE MODEL, stated exactly, because a subtly wrong version of it passed a first
draft and was caught only by the planted control below.

A segment announced with `PDT = T` carries media `[T, T + EXTINF]`. A word whose
end sits `u` seconds into that segment was SPOKEN at wall time `T + u`. Nobody —
however close to the origin — can hold the bytes carrying `T + u` before the
encoder has produced and flushed them:

    earliest_possible(word_end) = T + u + origin_flush

⚠ `u` CANNOT be derived from when the audio reached us. Under a buffered segment
fetch the WHOLE segment lands at once, so arrival is `T + our_lag` for every word
in the segment regardless of `u`. `u` comes only from FORCED ALIGNMENT — the
word's position on the media timeline — joined to the segment boundaries. That
join is what `--arrival-log` (PDT per segment) plus cumulative `EXTINF` provides.

TWO MODES.

  * `--races` + `--pdt-log`  — exact. `u` is measured, the verdict is a per-race
    binary.
  * `--races` alone — no PDT banked for that race. Then `u` is unknown and the
    honest output is the THRESHOLD `u*` above which the sweep is impossible,
    plus what fraction of a uniformly-placed word would exceed it. That is a
    real result, not a degraded one: it is the form the 2026-07-27 analysis used
    to show that at `u > 1.379 s` on IVS the competitor swept before word-end.

`origin_flush` is the one term not directly measured — bounded below by 0 (an
origin flushing at encoder cadence) and estimated at ~0.60 s for IVS from the
measured absolute-lag decomposition (2.62 s complete-lag − 2.00 s segment). Every
verdict is evaluated at BOTH ends and a race counts as DECIDED only when both
ends agree. A bound that depends on the loosest unmeasured term is not a bound.

WHAT IT ANSWERS THAT R1 COULD NOT. R1 asked this statistically and got
`corr = +0.792 at n=3` where significance needs `|r| > 0.997` — NOT RESOLVABLE.
This is a per-race binary on measured quantities: one race can settle it.

Usage:
  physical-bound.py --races races.json [--pdt-log pypl-arrival.log]
  physical-bound.py --selfcheck
"""

import argparse
import json
from datetime import datetime, timedelta, timezone

# IVS, measured 2026-07-27 from the banked V8 gate (n=2697 `seg age` samples):
# PDT -> fetch-complete p50 2.62 s (p10-p90 2.52-2.72), no drift over 90 min.
# 2.00 s of that is the segment itself; the rest is origin write completion +
# CDN propagation + a 16 ms fetch.
DEFAULT_FLUSH_MAX = 0.60
DEFAULT_SEG_S = 2.0
DEFAULT_OUR_LAG_S = 2.62


def parse_utc(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def sec(x):
    return timedelta(seconds=x)


def load_pdt_log(path):
    """`--arrival-log` rows: `{pdt} {t_done} {fetch_secs} {mode} {t_first}`.
    Returns sorted [(pdt_epoch, t_first_epoch)] — one row per delivered segment."""
    rows = []
    for line in open(path):
        p = line.split()
        if len(p) < 5:
            continue
        try:
            rows.append((float(p[0]), float(p[4])))
        except ValueError:
            continue
    return sorted(rows)


def u_from_pdt_log(rows, spoken_epoch, seg_s):
    """Exact `u`: find the segment whose [PDT, PDT+seg) brackets the spoken
    instant. Returns (u, segment_pdt) or None if no segment covers it."""
    for pdt, _ in rows:
        if pdt <= spoken_epoch < pdt + seg_s:
            return spoken_epoch - pdt, pdt
    return None


def decide(margin_lo, margin_hi):
    if margin_lo < 0 and margin_hi < 0:
        return "IMPOSSIBLE"
    if margin_lo >= 0 and margin_hi >= 0:
        return "POSSIBLE"
    return "UNDECIDED"


def run(races, pdt_rows, flush_max):
    print("=" * 78)
    print("A4 PHYSICAL BOUND — could any public-feed consumer have held this audio?")
    print("=" * 78)
    print(f"origin_flush evaluated at 0.00 s and {flush_max:.2f} s; a race is DECIDED")
    print("only when both ends agree.\n")

    tally = {"IMPOSSIBLE": [], "POSSIBLE": [], "UNDECIDED": [], "THRESHOLD-ONLY": []}

    for r in races:
        name = r.get("race", "?")
        seg = float(r.get("seg_s", DEFAULT_SEG_S))
        our_lag = float(r.get("our_lag_s", DEFAULT_OUR_LAG_S))
        sweep = parse_utc(r["sweep_utc"])
        arrival = parse_utc(r["word_end_utc"])      # word-end audio AT OUR BOX
        print(f"--- {name} ---")

        # `spoken` = arrival - (our_lag - u). With u unknown this is a family of
        # candidates; with a banked PDT it is a single value.
        exact = None
        if pdt_rows:
            # Solve: spoken = arrival - our_lag + u AND u = spoken - PDT_k.
            # => PDT_k = arrival - our_lag. Look that instant up directly.
            seg_start_guess = (arrival - sec(our_lag)).timestamp()
            hit = u_from_pdt_log(pdt_rows, seg_start_guess + 1e-6, seg)
            if hit:
                _, pdt_k = hit
                # the word is somewhere in THIS segment; its offset is what the
                # forced alignment supplies
                if "word_end_media_s" in r and "seg_pdt_utc" in r:
                    u = float(r["word_end_media_s"]) - parse_utc(r["seg_pdt_utc"]).timestamp()
                    exact = (u, pdt_k)

        if "u_s" in r:                       # alignment supplied it directly
            exact = (float(r["u_s"]), None)

        if exact is not None:
            u, _ = exact
            spoken = arrival - sec(our_lag) + sec(u)
            m_lo = (sweep - spoken).total_seconds()
            m_hi = (sweep - (spoken + sec(flush_max))).total_seconds()
            v = decide(m_lo, m_hi)
            print(f"    u (word offset in segment) = {u:.3f} s   [MEASURED]")
            print(f"    word spoken                = {spoken.isoformat()}")
            print(f"    sweep − earliest-possible  = {m_lo:+.3f} s (flush 0) .. "
                  f"{m_hi:+.3f} s (flush {flush_max:.2f})")
            if v == "IMPOSSIBLE":
                print("    ⇒ IMPOSSIBLE on this feed — they swept BEFORE the audio could")
                print("      exist anywhere. Prefix-firing, or a channel we have not found.")
                print("      No fetch or transport work can ever beat this race.")
            elif v == "POSSIBLE":
                print(f"    ⇒ POSSIBLE on this feed. Headroom still available to any")
                print(f"      consumer: {min(m_lo, m_hi):.3f} .. {max(m_lo, m_hi):.3f} s")
            else:
                print("    ⇒ UNDECIDED — flips inside the origin-flush range. Measure the")
                print("      origin's flush granularity before claiming either way.")
            tally[v].append(name)
        else:
            # No banked PDT: report the THRESHOLD instead of inventing u.
            # spoken = arrival - our_lag + u; impossible when sweep < spoken + flush
            # => u > sweep - arrival + our_lag - flush
            gap = (arrival - sweep).total_seconds()      # how far behind we were
            u_star_lo = our_lag - gap - flush_max
            u_star_hi = our_lag - gap
            lo, hi = sorted((u_star_lo, u_star_hi))
            frac = max(0.0, min(1.0, (seg - hi) / seg))
            print(f"    u NOT banked — reporting the threshold instead of guessing.")
            print(f"    we were {gap:.3f} s behind the sweep")
            print(f"    IMPOSSIBLE iff u > u* , u* = {lo:.3f} .. {hi:.3f} s "
                  f"(flush {flush_max:.2f} .. 0)")
            if hi >= seg:
                print(f"    ⇒ u* ≥ segment length ({seg:.2f} s): reachable on-feed for ANY")
                print("      word position. This race does not discriminate.")
            elif lo <= 0:
                print("    ⇒ u* ≤ 0: impossible on-feed for ANY word position.")
                tally["IMPOSSIBLE"].append(name)
                print()
                continue
            else:
                print(f"    ⇒ a uniformly-placed word exceeds u* about {100*frac:.0f}% of")
                print("      the time. Bank PDT via --arrival-log to convert this to a")
                print("      per-race binary.")
            tally["THRESHOLD-ONLY"].append(name)
        print()

    print("=" * 78)
    for k in ("IMPOSSIBLE", "POSSIBLE", "UNDECIDED", "THRESHOLD-ONLY"):
        print(f"{k:<16}: {len(tally[k])}  {tally[k]}")
    print()
    if tally["IMPOSSIBLE"]:
        print("⇒ At least one race was won BEFORE the audio physically existed at any")
        print("  public consumer. That is a PROOF, not a correlation. It redirects the")
        print("  campaign from FEED to prefix-firing or channel recon (W4) — and it does")
        print("  NOT generalise: other words may still be winnable on-feed.")
    elif tally["POSSIBLE"] and not (tally["UNDECIDED"] or tally["THRESHOLD-ONLY"]):
        print("⇒ Every scored race was reachable on the public feed. Their edge is")
        print("  consumption, not access, and the printed headroom is the ceiling on")
        print("  what any further FEED work can return.")
    else:
        print("⇒ Not settled. Do not quote a direction from this run.")


def selfcheck():
    """Planted races with known answers. This campaign has shipped three
    self-consistent-but-wrong instruments (S4 keep-alive −250 ms, E0c, D1c's
    350 ms artifact) — and the FIRST draft of this very script was a fourth,
    caught here. A new instrument does not get believed without a control."""
    print("=" * 78)
    print("SELFCHECK — planted races, exact-u mode")
    print("=" * 78)
    T0 = parse_utc("2026-07-24T12:00:00.000Z")     # segment PDT
    lag = DEFAULT_OUR_LAG_S

    def mk(name, u, sweep_rel_spoken):
        spoken = T0 + sec(u)
        return {"race": name, "u_s": u, "our_lag_s": lag, "seg_s": DEFAULT_SEG_S,
                "word_end_utc": (spoken + sec(lag - u)).isoformat().replace("+00:00", "Z"),
                "sweep_utc": (spoken + sec(sweep_rel_spoken)).isoformat().replace("+00:00", "Z")}

    cases = [mk("PLANT-impossible", 1.8, -0.40),   # swept 0.4 s before it was spoken
             mk("PLANT-possible",   0.2, +1.50),   # swept 1.5 s after — clear
             mk("PLANT-undecided",  1.0, +0.30)]   # 0.30 s sits inside 0..0.60
    run(cases, [], DEFAULT_FLUSH_MAX)

    print("\nEXPECTED: impossible ⇒ IMPOSSIBLE, possible ⇒ POSSIBLE, undecided ⇒ UNDECIDED")
    print("\n" + "=" * 78)
    print("SELFCHECK — threshold mode (no PDT banked), AXP NFL as measured")
    print("=" * 78)
    run([{"race": "AXP NFL (real)", "our_lag_s": lag, "seg_s": DEFAULT_SEG_S,
          "word_end_utc": "2026-07-24T12:37:28.754Z",
          "sweep_utc": "2026-07-24T12:37:27.513Z"}], [], DEFAULT_FLUSH_MAX)
    print("\nEXPECTED: u* ≈ 1.379 s at flush 0 — matching the 2026-07-27 hand analysis.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--races", help="races JSON (see module docstring)")
    ap.add_argument("--pdt-log", help="an --arrival-log file, for exact u")
    ap.add_argument("--origin-flush-max", type=float, default=DEFAULT_FLUSH_MAX)
    ap.add_argument("--selfcheck", action="store_true")
    a = ap.parse_args()
    if a.selfcheck:
        selfcheck()
    elif a.races:
        run(json.load(open(a.races)),
            load_pdt_log(a.pdt_log) if a.pdt_log else [],
            a.origin_flush_max)
    else:
        ap.error("need --races or --selfcheck")
