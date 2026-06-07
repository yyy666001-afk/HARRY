#!/usr/bin/env bash
set -euo pipefail
# Usage: ./scripts/update_config_and_publish.sh https://your-backend.example.com
if [ "$#" -ne 1 ]; then
  echo "Usage: $0 <backend_base_url>";
  exit 2;
fi
BACKEND_URL="$1"
WORKDIR=$(pwd)
TMP_BRANCH="gh-pages-update-$(date +%s)"

git checkout gh-pages
git checkout -b "$TMP_BRANCH"

if [ -f config.js ]; then
  sed -i "s|window.__API_BASE__ *= *'';|window.__API_BASE__ = '$BACKEND_URL';|" config.js || true
else
  echo "// generated config" > config.js
  echo "window.__API_BASE__ = '$BACKEND_URL';" >> config.js
fi

git add config.js
git commit -m "chore: update API base to $BACKEND_URL"
git push origin "$TMP_BRANCH":"gh-pages" --force

git checkout main
echo "Published gh-pages pointing to $BACKEND_URL"
