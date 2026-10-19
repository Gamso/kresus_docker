#!/bin/bash
# Regenerates patches/ from a local Woob clone: diff between origin/master and the working branch.
#   scripts/make-patches.sh /path/to/woob [branch]
set -euo pipefail
WOOB=${1:?path of the woob clone}
BRANCH=${2:-feat/boursedirect-esalia}
OUT=$(cd "$(dirname "$0")/.." && pwd)/patches
git -C "$WOOB" fetch origin
git -C "$WOOB" diff --binary origin/master "$BRANCH" -- modules/boursedirect modules/esalia modules/fortuneo > "$OUT/0001-boursedirect-esalia-fortuneo.patch"
echo "patch written to $OUT ($(wc -l < "$OUT/0001-boursedirect-esalia-fortuneo.patch") lines), base: $(git -C "$WOOB" rev-parse --short origin/master)"
