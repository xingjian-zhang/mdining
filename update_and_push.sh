#!/usr/bin/env bash
# Daily job: regenerate menu site and push to GitHub
set -euo pipefail

cd "$(dirname "$0")"

LOG_FILE=".ralph/logs/cron_$(date +%Y-%m-%d).log"
exec >> "$LOG_FILE" 2>&1

echo "=== $(date) ==="

# Generate the site (with translation)
python3 generate_site.py

# Stage and push
git add site/index.html site/translations_cache.json
if git diff --cached --quiet; then
    echo "No changes to commit."
else
    git commit -m "chore: update daily menu $(date +%Y-%m-%d)"
    git push origin main
    echo "Pushed to GitHub."
fi

echo "=== Done ==="
