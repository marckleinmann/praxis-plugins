#!/usr/bin/env bash
# Merlin setup entry point. Checks the three programs Merlin needs, then hands every
# other step to setup.py beside this file. Installs nothing.
#
#   bash setup.sh --prereqs                 check git, python3 and node only
#   bash setup.sh --python3-copy [--yes]    Windows: preview, then add python3.exe beside python.exe
#   bash setup.sh --skeleton-only --data D  test mode: starter files plus the report
#   bash setup.sh <any setup.py mode> ...   see setup.py for the list
#
# Honours a HOME override. Never reads a plugin variable: it finds setup.py from its
# own location.

set -u
HERE="$(cd "$(dirname "$0")" && pwd)"

is_windows() {
  case "$(uname -s 2>/dev/null)" in MINGW*|MSYS*|CYGWIN*) return 0 ;; esac
  [ "${OS:-}" = "Windows_NT" ]
}

# A Python 3 that runs. On Windows, `python3` and `python` can be the Microsoft Store shortcut,
# which answers to the name but is not Python, so a name counts only when it runs a line of Python.
py_works() { "$@" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; }
PY=()
find_python() { # sets PY to the command for a working Python 3, or leaves it empty
  PY=()
  if command -v python3 >/dev/null 2>&1 && py_works python3; then PY=(python3); return 0; fi
  if command -v python >/dev/null 2>&1 && py_works python; then PY=(python); return 0; fi
  if command -v py >/dev/null 2>&1 && py_works py -3; then PY=(py -3); return 0; fi
  return 0
}

prereqs() {
  local missing=0
  find_python
  if command -v git >/dev/null 2>&1; then
    echo "[ok]       git: $(git --version 2>/dev/null)"
  elif is_windows; then
    echo "[missing]  git: not found. Install Git for Windows: ask Claude in the Code tab to run winget install --id Git.Git -e --source winget, or download it from git-scm.com. Then quit and reopen the Claude app and run Merlin's setup again."
    missing=1
  else
    echo "[missing]  git: not found. On a Mac, macOS offers Apple's free command line developer tools in a window: click Install, wait for it to finish, then run Merlin's setup again."
    missing=1
  fi
  if [ "${PY[0]:-}" = "python3" ]; then
    echo "[ok]       python3: $(python3 --version 2>&1)"
  elif [ "${PY[0]:-}" = "python" ] && is_windows; then
    echo "[fix]      python3: Python is installed as \"python\", but Merlin's skills run \"python3\". Run bash setup.sh --python3-copy to see the one-file fix: a copy of python.exe named python3.exe, beside it. It runs only after the user's yes."
    missing=1
  elif [ ${#PY[@]} -gt 0 ] && is_windows; then
    echo "[missing]  python3: Python is installed but not on the PATH, so Merlin's skills cannot find it. Install it again from python.org and tick \"Add python.exe to PATH\" on the first screen, or ask Claude to run winget install --id Python.Python.3.13 -e --source winget. Then quit and reopen the Claude app and run Merlin's setup again."
    missing=1
  elif is_windows; then
    echo "[missing]  python3: not found. The task list and setup need it. Ask Claude in the Code tab to run winget install --id Python.Python.3.13 -e --source winget, or download Python from python.org and tick \"Add python.exe to PATH\" on the first screen. Then quit and reopen the Claude app and run Merlin's setup again."
    missing=1
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

# Windows only: Merlin's skills run `python3`, and the python.org and winget installs create only
# python.exe. The fix is a copy of python.exe named python3.exe in the same folder, which is
# already on the PATH. Without --yes it only says what it would do. It writes outside the business
# folder, so it runs only after the user's yes.
python3_copy() {
  local yes=$1 src dir dst
  find_python
  if [ "${PY[0]:-}" = "python3" ]; then echo "python3 already works: $(python3 --version 2>&1). Nothing to do."; return 0; fi
  if ! is_windows; then echo "This fix is for Windows only. Nothing was changed."; return 2; fi
  if [ "${PY[0]:-}" != "python" ]; then
    echo "No Python on the PATH to copy. Install Python first (see bash setup.sh --prereqs). Nothing was changed."; return 2
  fi
  src=$(python -c 'import sys; print(sys.executable)' 2>/dev/null)
  command -v cygpath >/dev/null 2>&1 && src=$(cygpath -u "$src")
  dir=$(dirname "$src"); dst="$dir/python3.exe"
  if [ -e "$dst" ]; then echo "$dst is already there, but python3 does not run. Reinstall Python from python.org with \"Add python.exe to PATH\" ticked. Nothing was changed."; return 2; fi
  echo "Would copy: $src"
  echo "        to: $dst"
  if [ "$yes" != 1 ]; then echo "Preview only. Nothing was changed. Run again with --yes after the user says yes."; return 0; fi
  if cp "$src" "$dst" && py_works python3; then
    echo "Done. python3 now runs: $(python3 --version 2>&1)."
  else
    echo "The copy did not make python3 run. Quit and reopen the Claude app, then run bash setup.sh --prereqs again."; return 2
  fi
}

case "${1:-}" in
  --prereqs)
    prereqs
    exit $?
    ;;
  --python3-copy)
    python3_copy "$([ "${2:-}" = "--yes" ] && echo 1 || echo 0)"
    exit $?
    ;;
esac

PRE_OUT="$(prereqs)"
PRE_RC=$?
find_python
if [ $PRE_RC -ne 0 ]; then
  printf '%s\n' "$PRE_OUT"
  if [ ${#PY[@]} -eq 0 ]; then
    echo "Setup stopped: Python 3 is needed for the remaining steps. Nothing was written."
    exit 2
  fi
elif [ "${1:-}" = "--skeleton-only" ]; then
  printf '%s\n' "$PRE_OUT"
fi

exec "${PY[@]}" "$HERE/setup.py" "$@"
