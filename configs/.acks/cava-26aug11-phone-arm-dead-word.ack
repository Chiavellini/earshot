# dead-word acknowledgement — webcast-cava-26aug11-arm-wc
# date: 2026-08-11
# WRITTEN AUTOMATICALLY by scripts/study-nightly.sh.
#
# These tokens scored 0 Confirmed on clean studio TTS through the
# REAL detector, and still scored 0 after their variants were widened
# from configs/variant-lexicon.toml + scripts/variant-widen.py.
# A word that fails clean TTS will not fire live.
#
# Acked so the event arms and trades its 17-keyword board minus these
# instead of forfeiting all of it (the 2026-08-07 TTWO lesson: 3 dead
# words of 12 vetoed a board that traded 69,658 contracts).
# This risks no money: scripts/posture-from-study.py pins any keyword
# with ratio = confirmed/clips below the floor to a 1c non-crossing
# cap, and a dead word has ratio 0 — it provably cannot fill.
#
# Their markets are knowingly UNSERVED. Do not read silence there as
# detector success.
# NEAR-MISS per autopsy (threshold fix, widening cannot help): PITA
# Tokens: PITA
