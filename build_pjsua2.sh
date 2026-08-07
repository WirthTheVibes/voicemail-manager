#!/bin/bash
# Builds pjproject + the pjsua2 Python bindings from source and installs them
# system-wide, so dial_and_play.py can `import pjsua2` under the same system
# python3 that vm-manager itself runs under (see vm-manager.service).
#
# There is no working prebuilt `pjsua2` wheel on PyPI — the sdist that exists
# there is just the SWIG bindings source pulled out of a full pjproject
# checkout, and expects to be built inside one (it does a relative-path
# `open('../../../../version.mak')`). Rebuilding from upstream source here,
# rather than copying the prebuilt .so files from another server, means this
# can't drift out of sync with whatever glibc/openssl/python3 a given 3CX box
# actually has.
#
# Usage: copy this file to the target server (it's part of the app's code,
# same as install.sh — see DEPLOY.md), then: sudo ./build_pjsua2.sh
set -euo pipefail

PJPROJECT_VERSION="2.15.1"
SRC_DIR="/usr/local/src/pjproject"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root (sudo ./build_pjsua2.sh)." >&2
  exit 1
fi

echo "== Checking build dependencies =="
REQUIRED_PKGS=(curl swig libssl-dev libasound2-dev build-essential python3-dev pkg-config)
MISSING_PKGS=()
for pkg in "${REQUIRED_PKGS[@]}"; do
  dpkg -s "$pkg" >/dev/null 2>&1 || MISSING_PKGS+=("$pkg")
done

if [ "${#MISSING_PKGS[@]}" -eq 0 ]; then
  echo "All build dependencies already installed."
else
  echo "Missing: ${MISSING_PKGS[*]}"
  # swig/libssl-dev/libasound2-dev aren't in 3CX's own apt repos, only in the
  # standard Debian archive. Point at it just long enough to install what's
  # missing, then remove the pointer — no need to leave this box's apt config
  # pointed outside 3CX's repos permanently.
  TMP_LIST="/etc/apt/sources.list.d/build-pjsua2-tmp.list"
  cleanup() {
    if [ -f "$TMP_LIST" ]; then
      rm -f "$TMP_LIST"
      apt-get update >/dev/null
    fi
  }
  trap cleanup EXIT
  echo "deb http://deb.debian.org/debian bookworm main" > "$TMP_LIST"
  apt-get update
  apt-get install -y "${MISSING_PKGS[@]}"
fi

echo "== Fetching pjproject $PJPROJECT_VERSION =="
if [ -d "$SRC_DIR" ]; then
  echo "$SRC_DIR already exists — reusing it (delete it first for a clean fetch)."
else
  mkdir -p "$SRC_DIR"
  curl -fsSL "https://github.com/pjsip/pjproject/archive/refs/tags/$PJPROJECT_VERSION.tar.gz" \
    | tar -xz -C "$SRC_DIR" --strip-components=1
fi
cd "$SRC_DIR"

echo "== Configuring and building pjproject (several minutes) =="
./configure --enable-shared
make dep
make -j"$(nproc)"

echo "== Installing pjproject system-wide (/usr/local/lib, /usr/local/include) =="
make install
ldconfig

echo "== Building the pjsua2 Python (SWIG) bindings =="
cd pjsip-apps/src/swig
make python

echo "== Installing pjsua2 into system python3 (matches how install.sh installs vm-manager's own deps) =="
pip3 install --break-system-packages ./python

echo "== Verifying =="
python3 -c "import pjsua2; print('pjsua2', pjsua2.Endpoint().libVersion().full, 'import OK')"

echo "Done — dial_and_play.py can now run under the system python3."
