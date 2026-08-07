#!/bin/bash
# Pulls the latest vm-manager release from GitHub and applies it to this host.
#
# Deliberately does NOT use git — target 3CX hosts don't have it installed,
# and we don't want to add it just for this. Instead this downloads a
# tarball of the latest commit on `main` via the GitHub API (works for
# private repos with a token) using curl/tar/rsync, all already present on
# a stock 3CX/Debian box. See DEPLOY.md for the full picture.
#
# Usage: sudo ./update.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="WirthTheVibes/voicemail-manager"
BRANCH="main"
TOKEN_FILE="$APP_DIR/.github_token"
VERSION_FILE="$APP_DIR/.deployed_version"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root (sudo ./update.sh) -- needed to re-run install.sh and restart services." >&2
  exit 1
fi

if [ ! -f "$TOKEN_FILE" ]; then
  echo "ERROR: $TOKEN_FILE not found." >&2
  echo "Create a GitHub fine-grained PAT scoped to just this repo with" >&2
  echo "'Contents: Read-only' (no write needed here), and save it there." >&2
  exit 1
fi
TOKEN="$(cat "$TOKEN_FILE")"

echo "== Checking latest commit on $REPO@$BRANCH =="
latest_sha="$(curl -sf -H "Authorization: Bearer ${TOKEN}" -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/${REPO}/commits/${BRANCH}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha"])')"

if [ -z "$latest_sha" ]; then
  echo "ERROR: couldn't determine latest commit sha (bad token, network, or repo/branch name)." >&2
  exit 1
fi

current_sha="$(cat "$VERSION_FILE" 2>/dev/null || echo "none")"

if [ "$latest_sha" = "$current_sha" ]; then
  echo "Already up to date ($current_sha)."
  exit 0
fi

echo "Updating: $current_sha -> $latest_sha"

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

echo "== Downloading tarball =="
curl -sfL -H "Authorization: Bearer ${TOKEN}" -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/${REPO}/tarball/${latest_sha}" -o "$tmpdir/release.tar.gz"
tar -xzf "$tmpdir/release.tar.gz" -C "$tmpdir"
extracted_dir="$(find "$tmpdir" -mindepth 1 -maxdepth 1 -type d)"

echo "== Applying to $APP_DIR (preserving local state) =="
rsync -av --delete \
  --exclude='.env' \
  --exclude='*.db' \
  --exclude='*.db-journal' \
  --exclude='models/' \
  --exclude='core' \
  --exclude='__pycache__/' \
  --exclude='.git/' \
  --exclude='.github_token' \
  --exclude='.deployed_version' \
  "$extracted_dir"/ "$APP_DIR"/

echo "$latest_sha" > "$VERSION_FILE"

echo "== Re-running install.sh (deps, systemd unit, nginx snippet) =="
"$APP_DIR/install.sh"

echo "== Restarting services =="
systemctl restart vm-manager.service
systemctl restart sip-reject-watch.service

echo "Done. Now running $latest_sha."
