# short-token acknowledgement — webcast-lyft-26aug06-arm-wc
# acked 2026-08-06 by claude-remote-prep (operator away; standing arm-all policy,
# fill posture reconfirmed by operator 2026-08-05 night)
#
# Tokens (k<=2), each named as the gate requires:
#     oil (2 tokens)
#     gas (2 tokens)
#     waymo (2 tokens)
#
# WHAT IS BEING ACCEPTED — AND WHY IT IS SMALLER THAN ON 26AUG05.
# On 08-05 (dash/ebay/fig) this ack accepted k<=2 targets at MENTION-FILL
# posture: real money on a target that assembles from partial audio at
# near-ceiling confidence (SNAP "game": 0.587 traded / 0.932 pipe_tuned /
# 0.953 streamlink, settled NO). That is no longer what is being accepted.
#
# scripts/posture-from-study.py now applies a k<=2 OVERRIDE: any keyword with a
# <=2-token variant is forced to OBSERVE (max_price_cents = 1) REGARDLESS of how
# well it scored in the readiness study. Here that demoted:
#     OIL   ratio 2.62  (gas=2tok, oil=2tok)  — would have earned FILL, denied
# and WAYM (ratio 0.33) was already below the fill floor on its own.
#
# A 1c order is NON-CROSSING and provably never reaches the wire: across every
# banked run in state/, 65 orders priced at 1c have send_ts == null and ZERO
# were sent (risk.rs debits event notional only on send). So the residual risk
# accepted here is a resting order that cannot fill and cannot consume the
# 100c event notional budget — NOT a position.
#
# These markets are therefore knowingly UNTRADED, not unserved: they still
# detect, still log decisions, and still feed the next study. Their silence is
# not detector success, and their firing costs nothing.
