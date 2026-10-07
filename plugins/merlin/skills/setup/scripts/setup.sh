#!/usr/bin/env bash
# Merlin setup entry point. Checks the three programs Merlin needs, then hands every
# other step to setup.py beside this file. Installs nothing.
#
#   bash setup.sh --prereqs                 check git, python3 and node only
#   bash setup.sh --skeleton-only --data D  test mode: starter files plus the report
#   bash setup.sh <any setup.py mode> ...   see setup.py for the list
#
# Honours a HOME override. Never reads a plugin variable: it finds setup.py from its
# own location.

set -u
HERE="$(cd "$(dirname "$0")" && pwd)"

prereqs() {
  local missing=0
  if command -v git >/dev/null 2>&1; then
    echo "[ok]       git: $(git --version 2>/dev/null)"
  else
    echo "[missing]  git: not found. On a Mac, macOS offers Apple's free command line developer tools in a window: click Install, wait for it to finish, then run Merlin's setup again."
    missing=1
  fi
  if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys' >/dev/null 2>&1; then
    echo "[ok]       python3: $(python3 --version 2>&1)"
  else
    echo "[missing]  python3: not found. The task list and setup need it. On a Mac, macOS offers Apple's free command line developer tools in a window: click Install, wait for it to finish, then run Merlin's setup again. Or install Python from python.org."
    missing=1
  fi
  if command -v node >/dev/null 2>&1; then
    echo "[ok]       node: $(node --version 2>/dev/null)"
  else
    echo "[optional] node: not found. The core of Merlin does not need it. Some add-on plugins do; install it from nodejs.org when one asks."
  fi
  return $missing
}

case "${1:-}" in
  --prereqs)
    prereqs
    exit $?
    ;;
esac

PRE_OUT="$(prereqs)"
PRE_RC=$?
if [ $PRE_RC -ne 0 ]; then
  printf '%s\n' "$PRE_OUT"
  if ! command -v python3 >/dev/null 2>&1; then
    echo "Setup stopped: python3 is needed for the remaining steps. Nothing was written."
    exit 2
  fi
elif [ "${1:-}" = "--skeleton-only" ]; then
  printf '%s\n' "$PRE_OUT"
fi

exec python3 "$HERE/setup.py" "$@"
