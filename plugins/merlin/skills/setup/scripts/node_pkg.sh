#!/bin/bash
# node_pkg.sh: on a Mac without Homebrew, download the current Node 24 installer from nodejs.org
# and open it only when Apple says it is signed and notarized for the Node.js Foundation.
#
#   bash node_pkg.sh                 download, check, then open the installer for the user
#   bash node_pkg.sh --check <pkg>   check a downloaded installer only; never opens it
#   bash node_pkg.sh --verdict       read `pkgutil --check-signature` output on stdin and judge it
#
# Why the signature and not only the checksum: SHASUMS256.txt comes from the same server as the
# installer, so a matching checksum proves the download arrived whole, not that the server was
# genuine. Apple's signature check names who signed the installer. The team ID is pinned, not the
# certificate, because the certificate is replaced when it expires (the one in use expires
# 2027-02-01) and the team ID stays.
#
# Exit 0: opened (or, with --check and --verdict, the installer is genuine).
# Exit 3: refused, the signature is missing, not notarized, or from another team. Nothing opened.
# Exit 4: the download failed. Nothing opened.
set -u

NODE_TEAM_ID="HX7739G8FX"
BASE="https://nodejs.org/dist/latest-v24.x"

verdict() { # stdin: pkgutil --check-signature output. Prints one line, returns 0 or 3.
  local out leaf
  out="$(cat)"
  if ! printf '%s\n' "$out" | grep -q 'Status: signed by a developer certificate issued by Apple for distribution'; then
    echo "REFUSED: the installer is not signed with an Apple Developer ID."
    return 3
  fi
  if ! printf '%s\n' "$out" | grep -q 'Notarization: trusted by the Apple notary service'; then
    echo "REFUSED: Apple has not notarized this installer."
    return 3
  fi
  leaf="$(printf '%s\n' "$out" | grep -m1 -E '^[[:space:]]*1\. ' | sed -E 's/^[[:space:]]*1\. //')"
  case "$leaf" in
    "Developer ID Installer: "*"($NODE_TEAM_ID)")
      echo "GENUINE: signed by $leaf, notarized by Apple."
      return 0 ;;
  esac
  echo "REFUSED: the installer is signed by \"$leaf\", not the Node.js Foundation ($NODE_TEAM_ID)."
  return 3
}

check_pkg() { # $1: the .pkg file
  local sig
  sig="$(pkgutil --check-signature "$1" 2>&1)"
  printf '%s\n' "$sig" | verdict
}

case "${1:-}" in
  --verdict)
    verdict
    exit $? ;;
  --check)
    if [ -z "${2:-}" ] || [ ! -f "$2" ]; then echo "usage: bash node_pkg.sh --check <installer.pkg>" >&2; exit 2; fi
    check_pkg "$2"
    exit $? ;;
  "") ;;
  *) echo "usage: bash node_pkg.sh [--check <installer.pkg> | --verdict]" >&2; exit 2 ;;
esac

if [ "$(uname -s)" != "Darwin" ]; then
  echo "This step is for a Mac. On Windows use: winget install --id OpenJS.NodeJS.LTS -e --source winget"
  exit 2
fi

dir="$(mktemp -d)"
if ! curl -sSfL --proto '=https' -o "$dir/SHASUMS256.txt" "$BASE/SHASUMS256.txt"; then
  echo "DOWNLOAD FAILED: could not read $BASE/SHASUMS256.txt. Install Node from https://nodejs.org/en/download instead."
  exit 4
fi
pkg="$(grep -oE 'node-v[0-9]+\.[0-9]+\.[0-9]+\.pkg' "$dir/SHASUMS256.txt" | head -1)"
if [ -z "$pkg" ]; then
  echo "DOWNLOAD FAILED: no macOS installer listed in $BASE/SHASUMS256.txt. Install Node from https://nodejs.org/en/download instead."
  exit 4
fi
if ! curl -sSfL --proto '=https' -o "$dir/$pkg" "$BASE/$pkg"; then
  echo "DOWNLOAD FAILED: could not download $BASE/$pkg. Install Node from https://nodejs.org/en/download instead."
  exit 4
fi
# The checksum proves the file arrived whole. The signature below proves who made it.
if ! (cd "$dir" && grep " $pkg\$" SHASUMS256.txt | shasum -a 256 -c - >/dev/null 2>&1); then
  echo "REFUSED: $pkg does not match its checksum; the download is damaged. Nothing was opened. Try again, or install from https://nodejs.org/en/download."
  exit 3
fi
if ! check_pkg "$dir/$pkg"; then
  echo "Nothing was opened. Install Node from https://nodejs.org/en/download instead."
  exit 3
fi
open "$dir/$pkg"
echo "OPENED: $dir/$pkg. Click through the installer and enter your Mac password when it asks."
exit 0
