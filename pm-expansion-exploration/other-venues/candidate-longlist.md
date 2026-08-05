# Candidate longlist — Pass A (census-mining, zero new network calls)

Source: `slate/census.json` (398 series, generated 2026-07-30T15:56Z).
**Reconciliation note**: `docs/EXPANSION.md`'s prose cites "~211/398
unclassified"; the live file actually shows **316/398** (58 earnings-call, 3
sports-tv, 11 speech-appearance, 6 recurring-broadcast, 3 press-conference,
1 hearing, 316 unclassified). Either the doc's count went stale or the
classifier/census changed since 2026-07-30 — worth a fresh
`mentions-census.py --classify` run before trusting either number further.
Of the 316 unclassified, 206 are non-earnings (the other 110 are more
`KXEARNINGSMENTION*`/`KXMENTIONEARN*` tickers the classifier's regex simply
missed — same class already covered, not new venues, excluded below).

**Discipline applied throughout**: per the WNT retraction and the
`KXALLINMENTION` title mismatch (doc said "All-In Podcast," live pull says
"All in with Chris Hayes," an MSNBC show) — every title below is what the
census last pulled live; **none of these are re-verified yet**, and several
tickers (`KXANVCD` = "dvdvsfv") are visibly test/junk data, not real
markets. Re-pull live before treating anything here as a real venue.

## Already covered — do not re-survey, cross-reference only

- `KXFEDMENTION` (Tier 2 #3, "the crown," already program-of-record) —
  present here only because the classifier missed it, not because it's new.
- `KXSECPRESSMENTION`/`KXLEAVITT*` (Tier 2 #6), `KXECBMENTION` (Tier 2 #6).
- `KXWORLDNEWSMENTION` (Tier 3 #8, PARKED — linear-TV blocker).
- Sports family — `KXMLBMENTION`(6 open)/`KXWNBAMENTION`(4 open)/`KXNBAMENTION`/
  `KXNBAFINALSMENTION`/`KXNFLMENTION`/`KXNHLMENTION`/`KXNCAABMENTION`/
  `KXNCAAMENTION`/`KXCFBMENTION`/`KXEPLMENTION`/`KXUEFAMENTION`/`KXATPMENTION`/
  `KXNASCARMENTION`/`KXWBCMENTION`/`KXWCMENTION`/`KXWOMENTION`(Winter Olympics)
  /`KXSNFMENTION`/`KXTNFMENTION` — Tier 3 #9, PARKED, same linear-feed blocker.
  All confirmed still in the census; no new sports venue class found.
- `KXTRUMPSAY`/`KXTRUMPSAYCOMPANY`/`KXTRUMPSAYMONTH`/`KXTRUMPSAYNICKNAME`/
  `KXTRUMPSAYEP`/`KXTRUMPSAYMAM`/`KXTRUMPSAYSCAM`/`KXTRUMPSAYTRUMP` —
  **CAUTION, do not assume KILLED.** `docs/EXPANSION.md`'s KILL/NOTE section
  kills "Text lanes (PM NYT-front-page, Trump/Elon posts; Kalshi
  `KXTRUMPSAY*` weekly/monthly cumulative boards) — different infra
  entirely (no audio; scraping + classification)." But that verdict was
  written about a *different*, unnamed `KXTRUMPSAY*` cumulative-count
  family than the 8 distinct tickers actually in the census — several
  (`KXTRUMPSAYNICKNAME`, `KXTRUMPSAYTRUMP`, `KXTRUMPSAYCOMPANY`) read like
  they could resolve on **spoken** nicknames/company-name mentions during a
  live Trump event, not written posts. **Re-verify each one's actual
  `rules_primary` live before assuming any of them are audio or text** — do
  not inherit the KILL verdict by name-similarity alone, the same mistake
  already made once with `KXALLINMENTION`.

## New candidate themes (not previously named in `docs/EXPANSION.md`)

### A. Government-official public remarks (largest new cluster, ~35 tickers)
Fed governors/officials beyond Powell: `KXFEDGOVMENTION`, `KXJPOWMENTION`,
`KXPOWELLMENTION`, `KXWALLERMENTION`, `KXWARSHMENTION`, `KXMIRANMENTION`,
`KXFEDMENTIONDURATION`. Treasury/Commerce: `KXBESSENTMTPMENTION`,
`KXLUTNICKFTNMENTION`. Cabinet/admin: `KXBONDIMENTION`(AG), `KXHEGSETHMENTION`
(Defense), `KXRUBIOMENTION`(State), `KXHOMANMENTION`, `KXVANCEMENTION`(VP).
Congress: `KXSCHUMERMENTION`, `KXJEFFRIESMENTION`, `KXJOHNSONMENTION`,
`KXPELOSIMENTION`, `KXAOCMENTION`, `KXMTGMENTION`, `KXCROCKETTMENTION`,
`KXKHANNAMENTION`, `KXPORTERMENTION`, `KXSPANBERGERMENTION`. Governors/mayors:
`KXNEWSOMMENTION`, `KXDESANTISMENTION`, `KXWALZMENTION`, `KXHOCHULMENTION`,
`KXFREYMENTION`(Minneapolis), `KXMAMDANIMENTION`(NYC mayoral, **1 open
event**), `KXTALARICOMENTION`(**1 open event**), `KXGOVERNORMENTION`(generic).
Foreign leaders: `KXSTARMERMENTION`/`KXSTARMERMENTIONB`(UK PM), `KXCOSTAMENTION`
(EU Council), `KXCARNEYMENTION`(Canada PM), `KXLAMMYMENTION`(UK FM),
`KXNETANYAHUMENTION`, `KXZELENSKYYMENTION`, `KXOBAMAMENTION`.
Judicial/institutional: `KXSCOTUSMENTION`, `KXCONGRESSMENTION`,
`KXPRESMENTION`(generic), `KXUNMENTION`, `KXWEFMENTION`(Davos).

**Why this matters**: unlike broadcast-TV classes, government press
conferences/hearings are very often livestreamed on official .gov/YouTube
channels — the same low-friction `SourceConfig::Pipe`/`Hls` pattern already
proven, and structurally NOT subject to the linear-TV blocker that parked
sports/WNT (no cable/network rights involved — see WEF, UN, and White House
precedent in EXPANSION.md's own `KXSECPRESSMENTION` recon). This cluster is
the single most promising new territory and should get first pass in the
checklist stage.

### B. Cable/broadcast news shows (~20 tickers)
`KXCOLBERTMENTION`, `KXKIMMELMENTION`, `KXHANNITYMENTION`, `KXINGRAHAMMENTION`
(Ingraham Angle), `KXGUTFELDMENTION`, `KXMADDOWMENTION`, `KXMAHERMENTION`,
`KXWATTERSMENTION`(Primetime), `KXSPECIALREPORTMENTION`, `KXFNSMENTION`(Fox
News Sunday), `KXFTNMENTION`(Face The Nation), `KXMTPMENTION`(Meet The
Press), `KXNEWSNATIONMENTION`, `KXTHEBEAT`(Ari Melber), `KXFOXNEWSMENTION`,
`KXFOXSATMENTION`, `KXLATENIGHTMENTION`, `KXTHEWEEKNIGHTMENTION`,
`KXZAKARIAMENTION`, `KX60MINMENTION`, `KXACOOPERMENTION`(Anderson Cooper),
`KXBONGINOMENTION`. **Structurally the same class as `KXWORLDNEWSMENTION`**
(already PARKED on the linear-TV blocker per the standing no-US-physical-
presence rule) — do NOT assume blanket-PARKED without checking, though.
Several cable networks (Fox especially, via Fox Nation/YouTube clips) have
different OTT availability than broadcast affiliates; worth a per-show
stream check rather than inheriting WNT's verdict wholesale.

### C. Podcasts / YouTube-native shows (~6 tickers, beyond the already-known `KXALLINMENTION`)
`KXROGANMENTION`(Joe Rogan — separate from the "first JRE episode" PM market
already seen in the Gamma pull above), `KXPORTNOYMENTION`(Dave Portnoy,
"DDTG"), `KXTHREADGUYMENTION`, `KXTBPNMENTION`(TBPN — a popular tech-news
YouTube livestream), `KXPEARLMANMENTION`(Oz Pearlman). **Most promising
technically** — YouTube-live is the exact pattern already proven feasible
(`SourceConfig::Pipe`, `yt-dlp | ffmpeg`), same infra as the WNT attempt,
minus WNT's actual failure mode (wrong channel) since podcast livestreams
are typically single-channel and unambiguous.

### D. Business/tech figures, ad-hoc appearances (~10 tickers)
`KXALTMANMENTION`(Sam Altman), `KXAMODEIMENTION`(Dario Amodei), `KXACKMANMENTION`
(Bill Ackman), `KXGRIFFINMENTION`(Ken Griffin), `KXBENIOFFMENTION`(Marc
Benioff), `KXARMSTRONGMENTION`(Brian Armstrong/Coinbase), `KXDIMONMENTION`
(Jamie Dimon), `KXFINKMENTION`(Larry Fink), `KXKARPMENTION`(Alex Karp),
`KXJENSENMENTION`(Jensen Huang), `KXVLADTENEVMENTION`(Vlad Tenev/Robinhood),
`KXZUCKMENTION`, `KXALTHOFFMENTION`(Judson Althoff/Salesforce),
`KXWALESMENTION`(Jimmy Wales). Harder class: no fixed recurring venue per
person — resolves on ad-hoc conference talks / interview appearances
(CNBC, Bloomberg, investor days), so the stream source has to be
re-discovered per event rather than pinned once. Lower priority than A/C
for that reason, but individually high-profile/high-volume when they occur.

### E. Entertainment/pop-culture (~15 tickers, mostly low-frequency/award-show)
`KXKARDASHIANMENTION`, `KXMINAJMENTION`(Nicki Minaj), `KXDRAKEMENTION`,
`KXSNOOPMENTION`, `KXSWIFTMENTION`(Taylor Swift), `KXLEBRONMENTION`,
`KXFURYMENTION`(Tyson Fury), `KXBARKLEYMENTION`, `KXCENAMENTION`(John Cena),
`KXSHAQMENTION`, `KXREEVESMENTION`(Keanu Reeves), `KXHARTMENTION`(Kevin
Hart), `KXZIWEMENTION`, `KXGILLISESPYS`(Shane Gillis/Espys),
`KXCMAMENTION`(CMA Awards), `KXBACHELORETTEMENTION`, `KXGOLDENMENTION`
(Golden Bachelor), `KXDWTSMENTION`, `KXBBMENTION`(Big Brother — note: PM
already lists 4 *separate* Big Brother episode markets seen in the live
Gamma pull, Task 1's capture-coverage finding), `KXSURVIVORMENTION`,
`KXSOUTHPARKMENTION`, `KXLOVEISLMENTION`(Love Island), `KXKARDASHIANMENTION`.
Mostly award-show/reality-TV — same linear-TV or awards-broadcast blocker
risk as class B; interview-appearance ones (podcasts, late night) fold into
C/B depending on venue.

### F. One-off/recurring named events (~10 tickers)
`KXDJTCPAC`(Trump at CPAC), `KXDJTRGA`(Trump at RGA), `KXDJTHANNITY`/
`KXKXDJTHANNITY`(dup?), `KXDJTLANDRY`, `KXDJTWOMENS`, `KXDJTCANG7`/
`KXDJTMEXG7`/`KXDJTUKRG7`(Trump at various G7 bilateral meetings),
`KXBUSINESSROUNDTABLE`, `KXIRANMOU`, `KXBILATERALMENTION`. Ad-hoc,
event-specific — each needs its own stream lookup when scheduled; low
individual cadence but officially-streamed (govt/press-pool) when they
happen, likely similar accessibility profile to class A.

### G. Generic/rotating-subject boards (needs per-instance investigation)
`KXMENTION`("What Will Someone Say?"), `KXPERSONMENTION`("General," **1 open
event**), `KXPOLITICSMENTION`, `KXCULTUREMENTION`, `KXENTMENTION`,
`KXATHLETEMENTION`, `KXAWARDMENTION`. The ticker doesn't name a fixed
person/venue — the actual subject/stream changes per event. Can't be
checklist-surveyed as a class; each open event needs its own row.

### Excluded as junk
`KXANVCD` ("dvdvsfv") — visibly test data, not a real market.

## Next: apply the per-venue checklist (`venue-survey.json`/`.md`) to a
shortlist drawn from clusters A and C first (government remarks, podcasts) —
highest apparent feasibility, lowest blocker risk, largest genuinely-new
territory — before spending checklist effort on B/E/F, which carry higher
a-priori blocker risk (linear-TV / ad-hoc-venue) similar to already-PARKED
classes.
