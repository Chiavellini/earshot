#!/usr/bin/env python3
"""Fake operator — TwiML webhook for rehearsing the operator handler.

Point a Twilio number we OWN at this server (VoiceUrl) and it behaves like the
INCOM/Chorus operator screens observed live on 2026-07-29 (VERIFICATIONS
Q-ADMIT; corpus/bridge-probes/msft-2026-07-29-bridge-entry.txt,
cvna-2026-07-29-bridge-entry.txt): greeting, a spoken name question, a company
question, an admission announcement, then a keyword script — with the real
operators' mute-check-then-disconnect behavior on silence.

stdlib only; no Twilio SDK, no pip. Run:  python3 server.py [--port 8100]

Call flow (each Twilio webhook is answered with TwiML):
  /            greeting (verbatim from the MSFT transcript) +
               Gather(speech) "may I have your first and last name, please?"
  /step2       Gather(speech) company question
  /step3       "Thank you, you are now joined in listen-only mode." +
               the TTS rehearsal keyword script (gaming / Hollywood /
               competition / ad-supported / acquisition — mirrors
               corpus/bridge-probes/tts_rehearsal_2026_07_29.txt) + Pause 600
  /mute-check  a Gather timed out with NO speech: "Hello? Anyone on the
               line, are you on mute?" once, wait 15 s, announce and Hangup —
               mirrors the real operators' silent-caller policy (21-73 s).

This server asks questions and hangs up on silence; it never needs to be
smart. The thing under test is our side: the paged human (v1) or the clip
responder (v2) must satisfy it, then the detector must hear the keywords.
"""

from __future__ import annotations

import argparse
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# Verbatim from the MSFT 2026-07-29 bridge-entry transcript (brand name
# dropped); the question lines are the operators' own words.
GREETING = (
    "Welcome to the conference center. "
    "Please be ready to provide your full name with spelling. "
    "An operator will assist you momentarily."
)
Q_NAME = "Conference center, may I have your first and last name, please?"
Q_COMPANY = "Thank you. And may I have the name of your company, please?"
ADMITTED = "Thank you, you are now joined in listen-only mode."
# Mirrors the TTS rehearsal script (tts_rehearsal_2026_07_29): known test
# keywords the detector configs already carry (gaming, Hollywood, competition,
# ad-supported, acquisition).
KEYWORD_SCRIPT = (
    "Good morning and welcome to the rehearsal earnings call. "
    "We saw strong momentum in gaming this quarter, and our Hollywood slate "
    "performed very well. The competition remains intense, and our "
    "ad-supported tier continues to grow nicely. We also completed a small "
    "acquisition during the quarter."
)
MUTE_CHECK = "Hello? Anyone on the line, are you on mute?"
DISCONNECT = (
    "Apologies. Due to no response, I will now disconnect this caller. Thank you."
)

# Silence budget on each question before the mute-check branch fires.
GATHER_TIMEOUT_S = 8


def twiml(body: str) -> bytes:
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>'.encode()


def gather(question: str, action: str) -> str:
    """A speech Gather that falls through to /mute-check on silence."""
    return (
        f'<Gather input="speech" action="{action}" method="POST" '
        f'timeout="{GATHER_TIMEOUT_S}" speechTimeout="auto">'
        f"<Say>{question}</Say>"
        "</Gather>"
        # Gather only reaches the next verb when NOTHING was captured.
        '<Redirect method="POST">/mute-check</Redirect>'
    )


ROUTES = {
    "/": f"<Say>{GREETING}</Say>" + gather(Q_NAME, "/step2"),
    "/step2": gather(Q_COMPANY, "/step3"),
    "/step3": f"<Say>{ADMITTED}</Say><Say>{KEYWORD_SCRIPT}</Say>"
    '<Pause length="600"/>',
    # Real-operator silent-caller behavior: one mute check, ~15 s of grace,
    # then an announced hangup (MSFT held 42-73 s total; CVNA announced the
    # disconnect out loud at 15-18 s — we mirror the announcement).
    "/mute-check": f"<Say>{MUTE_CHECK}</Say>"
    '<Pause length="15"/>'
    f"<Say>{DISCONNECT}</Say><Hangup/>",
}


class FakeOperator(BaseHTTPRequestHandler):
    server_version = "earshot-fake-operator/1"

    base_path = ""  # set from --base-path in main(); stripped off inbound paths

    def _respond(self) -> None:
        path = urlparse(self.path).path
        if self.base_path and path.startswith(self.base_path):
            path = path[len(self.base_path) :]
        path = path.rstrip("/") or "/"
        body = ROUTES.get(path)
        if body is None:
            self.send_error(404, "no such step")
            return
        payload = twiml(body)
        self.send_response(200)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # Twilio can be configured for GET webhooks
        self._respond()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        speech = (form.get("SpeechResult") or [""])[0]
        sid = (form.get("CallSid") or ["?"])[0]
        if speech:
            print(
                f"{time.strftime('%H:%M:%S')} {sid} {self.path} heard: {speech!r}",
                flush=True,
            )
        self._respond()

    def log_message(self, fmt: str, *args) -> None:  # noqa: N802 (stdlib name)
        print(f"{time.strftime('%H:%M:%S')} {self.address_string()} {fmt % args}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument(
        "--base-path",
        default="",
        help="public URL prefix the reverse proxy leaves intact, e.g. "
        "/fake-operator — stripped off inbound paths and prepended to "
        "Gather action URLs (Twilio resolves host-absolute actions).",
    )
    args = parser.parse_args()
    base = args.base_path.rstrip("/")
    if base and not base.startswith("/"):
        parser.error("--base-path must start with /")
    FakeOperator.base_path = base
    if base:
        for key in list(ROUTES):
            ROUTES[key] = ROUTES[key].replace('action="/', f'action="{base}/')
    server = ThreadingHTTPServer((args.bind, args.port), FakeOperator)
    print(
        f"fake operator listening on http://{args.bind}:{args.port} "
        "(point a Twilio number's VoiceUrl here — see README.md)",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
