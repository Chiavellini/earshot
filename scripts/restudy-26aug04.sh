#!/usr/bin/env bash
set -uo pipefail
cd /Users/bernardodelrio/Code/ear
export EARSHOT_BIN=target/release/earshot
for slug in spcx zeta tost; do
  echo "════ $(date -u '+%H:%M:%SZ') restudy $slug ════"
  scripts/variant-study.sh "configs/webcast-${slug}-26aug04-arm-wc.toml" "/tmp/variant-study-$slug-v2" 2>&1 | tail -22
done
echo "RESTUDY DONE $(date -u '+%H:%M:%SZ')"
