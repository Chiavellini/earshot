# short-token acknowledgement — webcast-dash-26aug05-arm-wc
# acked 2026-08-05 by operator (bdelriom) — MENTION-FILL posture, real money
#
# These k<=2 targets are the highest false-alarm risk in the slate. Accepted
# knowingly so the event arms and trades its remaining keywords rather than
# forfeiting the board.
#
# WHAT WE ARE ACCEPTING, precisely. preflight-event.sh:378-387 records that a
# k=2 target assembles at NEAR-CEILING confidence (SNAP "game": 0.587 traded /
# 0.932 pipe_tuned / 0.953 streamlink, settled NO), so a decision-layer
# min_confidence gate CANNOT be relied on to stop one: raise the gate past
# 0.587 and the 0.953 leg fires instead, because fanout ORs at the decision.
# The min_confidence_confirmed = 0.22 added to this config today is therefore
# NOT a mitigation for these tokens. It is aimed at a different, measured
# failure (26AUG05: all five NO markets fired 0.152-0.217).
#
# Partial counter-evidence from the 26AUG05 tape, recorded for honesty: the k=2
# token "oil" (UBER-OIL) fired at 0.152 and settled NO — that one WOULD have
# been caught by a 0.22 gate. And "a i" (DIS-ARTI) fired at 0.901 and settled
# YES. So k=2 confidence is not uniformly near-ceiling; it is unpredictable.
# Unpredictable is the risk being acked.
#
# Money bound if one of these does fire wrong: one contract at <=97c, capped by
# max_order_notional_cents=100 and the per-event $1 probe ledger. Max loss per
# event is that event's $1.
#
# Tokens:
           drone (2 tokens)
           waymo (2 tokens)
