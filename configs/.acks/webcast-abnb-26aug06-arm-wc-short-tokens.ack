# short-token acknowledgement — webcast-abnb-26aug06-arm-wc
# acked 2026-08-06 by claude-remote-prep (operator away; standing arm-all policy,
# fill posture reconfirmed by operator 2026-08-05 night)
#
# Tokens (k<=2), each named as the gate requires:
#     code (2 tokens)
#
# WHAT IS BEING ACCEPTED. scripts/posture-from-study.py applies a k<=2 OVERRIDE:
# CODE scored ratio 1.89 in the readiness study — comfortably above the 1.00
# fill floor — and was demoted to OBSERVE (max_price_cents = 1) anyway, because
# a 2-token target assembles from PARTIAL audio at near-ceiling confidence and
# no decision-layer confidence gate can reach it (SNAP "game" fired
# 0.587/0.932/0.953 and settled NO; raising the gate past one leg just lets
# another fire, because fanout ORs at the decision).
#
# A 1c order is NON-CROSSING and provably never sends: 65 one-cent orders across
# every banked run in state/, all with send_ts == null, zero sent. Event
# notional debits only on send (src/risk.rs:272-279), so this cannot crowd out
# the one real 97c order the 100c event cap allows.
#
# Accepted risk = a resting order that cannot fill. NOT a position.
