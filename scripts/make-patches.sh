#!/bin/bash
# Regenerates patches/ from a local Woob clone, one patch per module: diff between the commit pinned in the
# Dockerfile (WOOB_REF) and the working branch. No network access needed: the pinned commit must exist in
# the clone (it is the upstream master the patches were made against).
#   scripts/make-patches.sh /path/to/woob [branch]
#
# A module without changes gets no patch: when upstream integrates a fix, regenerate and its patch disappears.
# To patch another module, add its name to MODULES (the Dockerfile applies every patches/*.patch in order).
set -euo pipefail
WOOB=${1:?path of the woob clone}
BRANCH=${2:-feat/boursedirect-esalia}
MODULES="boursedirect esalia fortuneo"
ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT=$ROOT/patches
BASE=$(sed -n 's/^ARG WOOB_REF=//p' "$ROOT/Dockerfile" | head -1)
git -C "$WOOB" cat-file -e "$BASE^{commit}" 2>/dev/null || { echo "commit $BASE (WOOB_REF) not found in $WOOB: fetch upstream first"; exit 1; }

mkdir -p "$OUT"
rm -f "$OUT"/*.patch
n=0
for module in $MODULES; do
  patch=$(git -C "$WOOB" diff --binary "$BASE" "$BRANCH" -- "modules/$module")
  [ -n "$patch" ] || { echo "modules/$module: no change, no patch"; continue; }
  n=$((n + 1))
  file=$(printf '%s/%04d-%s.patch' "$OUT" "$n" "$module")
  printf '%s\n' "$patch" > "$file"
  echo "$(basename "$file"): $(wc -l < "$file") lines"
done
echo "base: $(git -C "$WOOB" rev-parse --short "$BASE"), $n patch(es) in $OUT"
