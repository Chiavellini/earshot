acked 2026-08-11 by operator (bdelriom) via agent session

CAVA short token: "a i" (2 BPE tokens, variant "a i").

The trading decision being acknowledged is NARROW: this word is already
pinned to max_price_cents = 1 (OBSERVE) by the k<=2 override in
scripts/posture-from-study.py, so it provably CANNOT take real money
regardless of how it fires. Its study ratio 20/18 = 1.11 would otherwise
have earned FILL; the override denied it deliberately (SNAP "game" class:
a k=2 target assembles from partial audio at near-ceiling confidence and
no decision-layer gate can reach it under fanout OR-ing).

Accepted cost: false fires on KXEARNINGSMENTIONCAVA-26AUG11-AI pollute the
detection record at 1c non-crossing. Accepted because the word stays armed
and keeps producing recall evidence for the next study.
