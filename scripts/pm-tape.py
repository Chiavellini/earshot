#!/usr/bin/env python3
"""pm-tape.py — Polymarket PUSH recorder for the cross-venue lane (Q-XVENUE-WS).

WHY THIS EXISTS (2026-08-04). `pm-capture.py` polls `POST /books` at 1 Hz. That
was the right call when it was written and it is still the fallback, but it puts
an asymmetry at the centre of the one measurement this lane exists to make:
Kalshi pushes over a WebSocket while Polymarket was sampled on a 1 s grid, so a
lead/lag number could be biased by up to a full second toward Kalshi purely by
how we looked. Polymarket does have a push feed — verified live 2026-08-04 —
and this records it, so both venues arrive the same way.

Wire facts, all verified live 2026-08-04 from CDMX (pinned: VERIFICATIONS.md
Q-XVENUE-WS):
  * Endpoint `wss://ws-subscriptions-clob.polymarket.com/ws/market`. The host
    is `ws-subscriptions-clob`, NOT `ws-subscribe-clob` (that name does not
    resolve at all).
  * Python's own TLS stack completes the handshake here — the fingerprint wall
    that forces `pm-capture.py` through curl applies to `clob.polymarket.com`,
    not to this host. No curl subprocess needed.
  * Subscribe frame: `{"assets_ids": [...], "type": "market"}`.
  * Delivers `book` (full snapshot: bids/asks/hash/tick_size/last_trade_price)
    and `price_change` (deltas carrying `best_bid`/`best_ask` inline), both with
    a MILLISECOND string `timestamp`.
  * `tick_size` is 0.001 — Polymarket prices a tenth of a cent, which is why
    the canonical schema is micro-dollars and not cents.
  * The newer `ws-live-data.polymarket.com` service answers CLOB topics with
    "CLOB messages are not supported anymore"; its `activity/trades` topic is a
    live global trade firehose. Not used here: this lane wants per-asset books.
  * A connection survived 100 s with no client PING (docs suggest 10 s). We
    ping anyway at 10 s — untested beyond 100 s, and a recorder that quietly
    dies mid-call is the failure this lane cannot afford.

Output: newline-delimited JSON, UNCOMPRESSED, one row per frame, VERBATIM wire
text plus our clocks — the same envelope `examples/book-tape.rs` writes for
Kalshi, so `xvenue-normalize.py` reads one shape. Uncompressed on purpose: a
hard kill must leave a readable tape.

Read-only: no auth, no wallet, no key, no order endpoint.

Usage:
  scripts/pm-tape.py --map configs/pm-map-26aug03.json \\
      --event KXEARNINGSMENTIONAMD-26AUG04 --out state/xvenue/AMD/pm.jsonl --secs 5400
  scripts/pm-tape.py --self-test        # offline, no network

Exit codes: 0 recorded / 1 nothing recorded / 2 undetermined (setup failure).
"""

import argparse
import base64
import json
import os
import signal
import socket
import ssl
import struct
import sys
import time

# Frozen tape schema. Must match XV_SCHEMA in examples/book-tape.rs and
# scripts/xvenue-normalize.py; the normalizer refuses a version it does not
# know rather than guessing at a field that may have changed meaning.
XV_SCHEMA = 1

WS_HOST = "ws-subscriptions-clob.polymarket.com"
WS_PATH = "/ws/market"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
PING_INTERVAL_S = 10.0
RECONNECT_DELAY_S = 0.5


# ── minimal RFC6455 client ────────────────────────────────────────────────
# Hand-rolled because the repo's scripts are stdlib-only and this box has no
# websocket library, no websocat, and a curl without the `ws` protocol. Only
# the client half of the protocol is needed: text frames out, text frames in.

def ws_connect(host, path, timeout=15):
    raw = socket.create_connection((host, 443), timeout=timeout)
    raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall(
        f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\nUser-Agent: {UA}\r\n\r\n".encode()
    )
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("closed during handshake")
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    status = head.split(b"\r\n", 1)[0].decode("latin-1")
    if "101" not in status:
        raise ConnectionError(f"handshake refused: {status}")
    return sock, rest


def ws_send(sock, payload, opcode=0x1):
    data = payload.encode() if isinstance(payload, str) else payload
    mask = os.urandom(4)
    n = len(data)
    if n < 126:
        hdr = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
    elif n < 65536:
        hdr = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
    else:
        hdr = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
    sock.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))


class Framer:
    """Incremental frame reader. Keeps leftover bytes across recv boundaries —
    a frame split across TCP reads must not be lost or mis-parsed."""

    def __init__(self, sock, initial=b""):
        self.sock, self.buf = sock, initial

    def _need(self, n, deadline):
        while len(self.buf) < n:
            self.sock.settimeout(max(0.05, min(1.0, deadline - time.time())))
            try:
                chunk = self.sock.recv(65536)
            except (socket.timeout, ssl.SSLWantReadError):
                return False
            if not chunk:
                raise ConnectionError("peer closed")
            self.buf += chunk
        return True

    def next_frame(self, deadline):
        """-> (opcode, payload) or None when the read window expired."""
        if not self._need(2, deadline):
            return None
        b0, b1 = self.buf[0], self.buf[1]
        op, ln = b0 & 0x0F, b1 & 0x7F
        off = 2
        if ln == 126:
            if not self._need(4, deadline):
                return None
            ln, off = struct.unpack("!H", self.buf[2:4])[0], 4
        elif ln == 127:
            if not self._need(10, deadline):
                return None
            ln, off = struct.unpack("!Q", self.buf[2:10])[0], 10
        if not self._need(off + ln, deadline):
            return None
        payload, self.buf = self.buf[off:off + ln], self.buf[off + ln:]
        return op, payload


# ── recorder ──────────────────────────────────────────────────────────────

def load_assets(map_path, event):
    with open(map_path) as f:
        pmmap = json.load(f)
    spec = pmmap.get(event)
    if not spec:
        raise SystemExit(f"FATAL: {event} not in {map_path}")
    assets, questions = [], {}
    for m in spec["markets"]:
        # tokens[0] is YES (the Gamma clobTokenIds order pm-map-build.py
        # preserves). Both are recorded — the tape is raw — but the question
        # index lets the normalizer label them without re-reading the map.
        for i, tok in enumerate(m["tokens"][:2]):
            assets.append(str(tok))
            questions[str(tok)] = {"q": m.get("question"), "outcome": "YES" if i == 0 else "NO",
                                   "condition_id": m.get("condition_id")}
    return spec, assets, questions


def record(args):
    spec, assets, questions = load_assets(args.map, args.event)
    out = open(args.out, "w")
    header = {
        "t": "session",
        "schema_version": XV_SCHEMA,
        "venue": "polymarket",
        "feed_kind": "push",
        "endpoint": f"wss://{WS_HOST}{WS_PATH}",
        "channel": "market",
        "event": args.event,
        "label": args.label,
        "clock": spec.get("clock"),
        "pm_event_slug": spec.get("pm_event_slug"),
        "assets": questions,
        "host": socket.gethostname().split(".")[0],
        "start_wall_ns": time.time_ns(),
        "planned_secs": args.secs,
    }
    out.write(json.dumps(header, separators=(",", ":")) + "\n")
    out.flush()

    stop = {"flag": False, "why": "deadline"}

    def on_sig(signum, _frame):
        stop["flag"] = True
        stop["why"] = signal.Signals(signum).name
        print(f"pm-tape: {stop['why']}, closing", flush=True)

    # SIGTERM is what launchd and the event timer actually send; the repo's
    # unattended rule is that a drill on any other mechanism validates the
    # wrong path.
    signal.signal(signal.SIGTERM, on_sig)
    signal.signal(signal.SIGINT, on_sig)

    end = time.time() + args.secs
    rows = errors = reconnects = 0
    last_beat = time.time()
    sub = json.dumps({"assets_ids": assets, "type": "market"})

    print(f"pm-tape: {len(assets)} assets → {args.out} (schema {XV_SCHEMA}, {args.secs}s)",
          flush=True)

    while not stop["flag"] and time.time() < end:
        try:
            sock, rest = ws_connect(WS_HOST, WS_PATH)
        except Exception as e:                                  # noqa: BLE001
            errors += 1
            if errors % 20 == 1:
                print(f"pm-tape: connect failed ({e})", flush=True)
            time.sleep(RECONNECT_DELAY_S)
            continue
        # A fresh subscribe always yields a fresh `book` snapshot, so state can
        # never silently drift across a reconnect (same discipline as the
        # Kalshi reader).
        ws_send(sock, sub)
        framer = Framer(sock, rest)
        last_ping = time.time()
        try:
            while not stop["flag"] and time.time() < end:
                got = framer.next_frame(min(end, time.time() + 1.0))
                now = time.time()
                if now - last_ping >= PING_INTERVAL_S:
                    last_ping = now
                    ws_send(sock, b"", opcode=0x9)
                if got is None:
                    continue
                op, payload = got
                if op == 0x8:
                    raise ConnectionError("server close")
                if op == 0x9:
                    ws_send(sock, payload, opcode=0xA)
                    continue
                if op not in (0x1, 0x2):
                    continue
                text = payload.decode("utf-8", "replace")
                if not text.strip():
                    continue
                # Verbatim, embedded as a STRING: a malformed or unexpected
                # wire frame can never corrupt the tape it is evidence in.
                out.write(json.dumps(
                    {"t": "frame", "wall_ns": time.time_ns(),
                     "mono_ns": time.monotonic_ns(), "raw": text},
                    separators=(",", ":")) + "\n")
                rows += 1
                if rows % 100 == 0:
                    out.flush()
                if now - last_beat >= 60:
                    last_beat = now
                    out.flush()
                    print(f"pm-tape beat: rows={rows} reconnects={reconnects} errors={errors}",
                          flush=True)
        except Exception as e:                                  # noqa: BLE001
            errors += 1
            reconnects += 1
            print(f"pm-tape: stream error ({e}); reconnecting", flush=True)
            time.sleep(RECONNECT_DELAY_S)
        finally:
            try:
                sock.close()
            except Exception:                                   # noqa: BLE001
                pass

    out.write(json.dumps(
        {"t": "close", "reason": stop["why"] if stop["flag"] else "deadline",
         "rows": rows, "reconnects": reconnects, "errors": errors,
         "end_wall_ns": time.time_ns()}, separators=(",", ":")) + "\n")
    out.flush()
    out.close()
    print(f"pm-tape done: rows={rows} reconnects={reconnects} errors={errors}", flush=True)
    return 0 if rows else 1


# ── offline self-test ─────────────────────────────────────────────────────

def self_test():
    p = f = 0

    def chk(name, want, got):
        nonlocal p, f
        if want == got:
            print(f"  PASS  {name}")
            p += 1
        else:
            print(f"  FAIL  {name} (expected {want!r}, got {got!r})")
            f += 1

    print("pm-tape self-test:")
    chk("schema pinned", 1, XV_SCHEMA)
    chk("endpoint host", "ws-subscriptions-clob.polymarket.com", WS_HOST)

    # Frame reader must survive arbitrary TCP fragmentation: the bug class here
    # is a frame split across recv() boundaries being silently dropped.
    class FakeSock:
        def __init__(self, chunks):
            self.chunks = list(chunks)

        def settimeout(self, _):
            pass

        def recv(self, _n):
            if not self.chunks:
                raise socket.timeout()
            return self.chunks.pop(0)

    def server_text(s):
        d = s.encode()
        return struct.pack("!BB", 0x81, len(d)) + d

    wire = server_text('{"event_type":"book","a":1}') + server_text('{"event_type":"price_change"}')
    for split in (1, 3, 7, 12, len(wire) - 1):
        chunks = [wire[:split], wire[split:]]
        fr = Framer(FakeSock(chunks))
        got = []
        deadline = time.time() + 5
        while True:
            r = fr.next_frame(deadline)
            if r is None:
                break
            got.append(json.loads(r[1].decode())["event_type"])
        chk(f"fragmented at {split} yields both frames", ["book", "price_change"], got)

    # Masking is mandatory client→server; an unmasked frame is a protocol
    # violation the server closes on.
    sent = []

    class CapSock:
        def sendall(self, b):
            sent.append(b)

    ws_send(CapSock(), "hi")
    chk("client frame sets MASK bit", 0x80, sent[0][1] & 0x80)
    chk("payload is masked (not plaintext)", False, b"hi" in sent[0])

    print()
    if f:
        print(f"SELFTEST FAIL ({f} failed, {p} passed)")
        return 1
    print(f"SELFTEST PASS ({p} checks)")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", help="pm-map JSON (scripts/pm-map-build.py)")
    ap.add_argument("--event", help="Kalshi event ticker key inside the map")
    ap.add_argument("--out", help="output .jsonl path")
    ap.add_argument("--secs", type=int, default=300)
    ap.add_argument("--label", default="")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        return self_test()
    missing = [k for k in ("map", "event", "out") if not getattr(a, k)]
    if missing:
        print(f"FATAL: --{' --'.join(missing)} required (or --self-test)", file=sys.stderr)
        return 2
    try:
        return record(a)
    except SystemExit:
        raise
    except Exception as e:                                       # noqa: BLE001
        print(f"FATAL: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
