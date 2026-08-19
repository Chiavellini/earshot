acked 2026-08-18 by event-prep (operator-absent, standing run-all-events policy)
Short tokens present: oil (2 tok), rei (2 tok), reis (2 tok).
TRADING DECISION ACKNOWLEDGED = DO NOT TRADE THEM. All three carry max_price_cents = 1,
which src/risk.rs:42-45 records as non-crossing (nothing rests at 1c on a mention board),
so they take NO position and cannot lose money to the SNAP-"game" failure mode.
oil was demoted from ratio 1.78 by posture-from-study.py's own k<=2 override, not by hand.
rei is 2/36 = 0.06 after widening; reis is its variant.
