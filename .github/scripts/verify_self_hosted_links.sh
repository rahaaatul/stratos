#!/usr/bin/env bash
# Checks every self-hosted URL (icon/readme/changelog pointing back at this
# repo) in every track.yaml actually resolves. Catches filename-mismatch
# bugs (e.g. track.yaml says vector.png, real file is vector_icon.png)
# automatically instead of relying on someone noticing a broken image.
set -uo pipefail

REPO_MARKER="raw.githubusercontent.com/rahaaatul/stratos"
failed=0

for f in modules/*/track.yaml; do
  urls=$(grep -E "^(icon|readme|changelog):" "$f" | sed -E 's/^[a-z]+:[[:space:]]*//' | grep "$REPO_MARKER" || true)
  for url in $urls; do
    status=$(curl -s -o /dev/null -w "%{http_code}" "$url")
    if [ "$status" != "200" ]; then
      echo "BROKEN LINK ($status): $url  <- $f"
      failed=1
    fi
  done
done

if [ "$failed" -eq 1 ]; then
  echo ""
  echo "One or more self-hosted asset links are broken. Fix before merging."
  exit 1
fi

echo "All self-hosted asset links resolve correctly."
