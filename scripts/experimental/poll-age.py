#!/usr/bin/env python3
"""Measure ABSOLUTE content-age of an HLS consumer = wall_time_received − segment_PDT,
isolating the manifest-poll-interval effect. Runs one mode; compare two modes.

For each segment we first observe as newest, record: PDT of that segment, and the
wall-clock at which we FINISHED downloading it. age = recv_wall − PDT. This is the
latency of the freshest audio this consumer holds. The ONLY variable between the two
invocations is --reload (poll interval): 'segment' = reload once per segment-duration
(streamlink-like), a small number = aggressive phase-locked polling.

Usage: poll-age.py <media-or-master-url> <reload_ms|segment> <run_secs> <label>
Prints per-segment ages and a summary (mean/median/p95). No audio output — pure timing.
"""
import sys, time, urllib.request, urllib.parse, datetime, statistics

def get(url, timeout=4):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent":"poll-age/1"}), timeout=timeout).read()

def resolve(url):
    body=get(url).decode("utf-8","replace")
    if "#EXT-X-STREAM-INF" in body:
        for line in body.splitlines():
            line=line.strip()
            if line and not line.startswith("#"):
                return urllib.parse.urljoin(url,line)
    return url

def parse_pdt(s):
    s=s.strip().replace("Z","+00:00")
    return datetime.datetime.fromisoformat(s).timestamp()

def main():
    media=resolve(sys.argv[1]); mode=sys.argv[2]; run_secs=float(sys.argv[3]); label=sys.argv[4]
    base=media.rsplit("/",1)[0]+"/"
    # determine segment duration for 'segment' reload mode
    body=get(media).decode("utf-8","replace")
    segdur=6.0
    for l in body.splitlines():
        if l.startswith("#EXTINF:"):
            try: segdur=float(l.split(":")[1].split(",")[0]); break
            except: pass
    poll = segdur if mode=="segment" else float(mode)/1000.0
    seen=set(); ages=[]; t_end=time.time()+run_secs
    sys.stderr.write(f"[{label}] media={media} poll={poll*1000:.0f}ms segdur={segdur}s\n"); sys.stderr.flush()
    first=True
    while time.time()<t_end:
        t0=time.time()
        try: body=get(media,timeout=poll+2).decode("utf-8","replace")
        except Exception as e:
            time.sleep(poll); continue
        # pair each segment URI with its preceding PDT
        lines=body.splitlines(); pdt=None; pairs=[]
        for l in lines:
            l=l.strip()
            if l.startswith("#EXT-X-PROGRAM-DATE-TIME:"): pdt=parse_pdt(l.split(":",1)[1])
            elif l and not l.startswith("#"): pairs.append((l,pdt)); pdt=None
        newp=[(u,p) for (u,p) in pairs if u not in seen]
        for u,_ in newp: seen.add(u)
        if first: newp=newp[-1:]; first=False   # start at newest
        for u,p in newp:
            try:
                get(urllib.parse.urljoin(base,u), timeout=poll+3)   # download (measures fetch completion)
                recv=time.time()
                if p: ages.append(recv-p)
            except Exception: pass
        dt=time.time()-t0
        if dt<poll: time.sleep(poll-dt)
    if ages:
        ages.sort()
        p95=ages[int(len(ages)*0.95)-1] if len(ages)>1 else ages[0]
        print(f"[{label}] n={len(ages)} content_age mean={statistics.mean(ages):.2f}s median={statistics.median(ages):.2f}s p95={p95:.2f}s min={ages[0]:.2f}s")
    else:
        print(f"[{label}] no ages collected")

if __name__=="__main__": main()
