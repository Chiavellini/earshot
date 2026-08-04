#!/usr/bin/env bash
# Run the blocking keyword-readiness study over the four 2026-08-04 afternoon
# arm configs, in call order (earliest call first, so a slow study can never
# leave the nearest event ungated).
set -uo pipefail
cd /Users/bernardodelrio/Code/ear
export EARSHOT_BIN=target/release/earshot
for slug in spcx zeta amd tost; do
  cfg="configs/webcast-${slug}-26aug04-arm-wc.toml"
  echo "════ $(date -u '+%H:%M:%SZ') variant-study $slug ════"
  scripts/variant-study.sh "$cfg" "/tmp/variant-study-$slug" 2>&1 | tail -30
  echo "════ $(date -u '+%H:%M:%SZ') $slug exit=$? ════"
done
echo "ALL STUDIES DONE $(date -u '+%H:%M:%SZ')"
