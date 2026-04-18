#!/usr/bin/env bash
# Daily backup of Claude Code memory folder → GCS.
# Runs on the operator's local Mac via launchd (see com.oncofiles.memory-backup.plist).
#
# Setup (one-time):
#   1. gcloud auth application-default login
#   2. cp scripts/com.oncofiles.memory-backup.plist ~/Library/LaunchAgents/
#   3. launchctl load ~/Library/LaunchAgents/com.oncofiles.memory-backup.plist
#
# Runs nightly at 02:30 local time. Logs to ~/Library/Logs/oncofiles-memory-backup.log.

set -euo pipefail

BUCKET="${BACKUP_BUCKET:-oncofiles-backups-eu}"
MEMORY_DIR="${HOME}/.claude/projects/-Users-peterfusek1980gmail-com-Projects-oncofiles/memory"
NOW="$(date -u +%Y%m%dT%H%M%SZ)"
DATE_PREFIX="$(date -u +%Y/%m/%d)"

if [[ ! -d "$MEMORY_DIR" ]]; then
    echo "$(date -u +%FT%TZ) memory dir not found: $MEMORY_DIR" >&2
    exit 1
fi

TMP="$(mktemp -t oncofiles-memory).tar.gz"
trap 'rm -f "$TMP"' EXIT

# Create archive (exclude transient files like .DS_Store)
tar --exclude='.DS_Store' -czf "$TMP" -C "$(dirname "$MEMORY_DIR")" "$(basename "$MEMORY_DIR")"

SIZE_BYTES=$(stat -f%z "$TMP" 2>/dev/null || stat -c%s "$TMP")
SHA256=$(shasum -a 256 "$TMP" | awk '{print $1}')

DEST="gs://${BUCKET}/memory/${DATE_PREFIX}/${NOW}_memory_snapshot.tar.gz"
echo "$(date -u +%FT%TZ) uploading $TMP ($SIZE_BYTES bytes, sha256=$SHA256) → $DEST"

# Rely on gcloud ADC (Application Default Credentials) — set up via:
#   gcloud auth application-default login
gcloud storage cp "$TMP" "$DEST" \
    --custom-metadata="sha256=${SHA256},source_host=$(hostname -s)" \
    --quiet

echo "$(date -u +%FT%TZ) memory backup complete"
