#!/usr/bin/env bash
# Merlin home SessionStart hook (Claude Code).
#
# Finds a Python 3.8 or later and runs home_context.py beside this file with the event on
# stdin. That script prints at most one JSON line, whose additionalContext tells Claude to
# open the Merlin home in the browser pane. No Python, or any failure: print nothing and
# exit 0, so a chat always starts.
#
# Uses bash builtins only, so it also behaves with an empty PATH.

set -u

HERE="${BASH_SOURCE[0]%/*}"
[ "$HERE" = "${BASH_SOURCE[0]}" ] && HERE="."

# Read the event JSON up to its end, so the caller never blocks on a full pipe, but for two
# seconds at most, so a caller that never closes the pipe never holds the hook up.
INPUT=""
if [ ! -t 0 ]; then IFS= read -r -d '' -t 2 INPUT 2>/dev/null || true; fi

# On Windows (Git Bash reports msys or MINGW) python3 can be the Microsoft Store stub, so
# the Python launcher is tried first there.
WIN=0
case "${OSTYPE:-}" in msys*|cygwin*|win32*|mingw*) WIN=1 ;; esac
case "${MSYSTEM:-}" in MINGW*|MSYS*) WIN=1 ;; esac
if [ "$WIN" = 1 ]; then
  CANDIDATES="py:-3 python3: python:"
else
  CANDIDATES="python3: python:"
fi

PY=""
PYARG=""
for c in $CANDIDATES; do
  exe="${c%%:*}"
  arg="${c#*:}"
  command -v "$exe" >/dev/null 2>&1 || continue
  if [ -n "$arg" ]; then
    "$exe" "$arg" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1 || continue
  else
    "$exe" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1 || continue
  fi
  PY="$exe"
  PYARG="$arg"
  break
done
[ -n "$PY" ] || exit 0

if [ -n "$PYARG" ]; then
  printf '%s' "$INPUT" | "$PY" "$PYARG" -I -B "$HERE/home_context.py" 2>/dev/null
else
  printf '%s' "$INPUT" | "$PY" -I -B "$HERE/home_context.py" 2>/dev/null
fi
exit 0
