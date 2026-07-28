#!/usr/bin/env bash
# Unified alert path: mail + macOS notification + speech.
#   notify.sh <subject> <body> [--quiet]     (--quiet suppresses `say`)
#
# Channels, in order:
#   1. Gmail SMTP — only if ~/.kalshi/mail_env exists (MAIL_USER,
#      MAIL_APP_PASSWORD, MAIL_TO — same contract as event-autorun.sh).
#      A missing mailer WARNS and continues; it never silently no-ops.
#   2. macOS notification center (osascript) — Darwin only.
#   3. Spoken alert (`say`) — Darwin only, skipped with --quiet.
#
# Exit 0 if ANY channel delivered; exit 1 only if every channel failed.
# The 2026-07-28 PYPL/BA miss was silent because nothing owned alerting —
# this script exists so every scheduled job has one obvious thing to call.
set -u
SUBJ="${1:?usage: notify.sh <subject> <body> [--quiet]}"
BODY="${2:-}"
QUIET="${3:-}"
delivered=0

if [ -f "$HOME/.kalshi/mail_env" ]; then
  if (
    set -a
    # shellcheck disable=SC1091
    source "$HOME/.kalshi/mail_env"
    set +a
    N_SUBJ="$SUBJ" N_BODY="$BODY" python3 - <<'PY'
import os, smtplib
from email.message import EmailMessage
msg = EmailMessage()
msg["Subject"] = "[EARSHOT] " + os.environ["N_SUBJ"]
msg["From"] = os.environ["MAIL_USER"]
msg["To"] = os.environ["MAIL_TO"]
msg.set_content(os.environ.get("N_BODY", ""))
with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as s:
    s.login(os.environ["MAIL_USER"], os.environ["MAIL_APP_PASSWORD"])
    s.send_message(msg)
PY
  ); then
    delivered=1
  else
    echo "notify.sh: mail send FAILED (subject: $SUBJ)" >&2
  fi
else
  echo "notify.sh: ~/.kalshi/mail_env missing — mail channel skipped" >&2
fi

if [ "$(uname)" = "Darwin" ]; then
  esc_body=${BODY//\"/\\\"}; esc_subj=${SUBJ//\"/\\\"}
  if osascript -e "display notification \"${esc_body:0:200}\" with title \"EARSHOT\" subtitle \"${esc_subj:0:100}\"" 2>/dev/null; then
    delivered=1
  fi
  if [ "$QUIET" != "--quiet" ]; then
    say "Earshot alert: $SUBJ" 2>/dev/null && delivered=1
  fi
fi

[ "$delivered" = 1 ] || { echo "notify.sh: EVERY channel failed" >&2; exit 1; }
