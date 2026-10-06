#!/usr/bin/env python3
"""
claim_id.py: atomically allocate the next task id for a task list.

Why this exists: the `<!-- next-task-id: N -->` counter is a read-then-write
convention. Two sessions can read the same N before either writes, and both win,
which produces two tasks with the same id.

This tool does not touch that counter's correctness. It adds a second, independent
source of truth that IS collision-proof: a claim file per allocated id, created with
O_CREAT|O_EXCL, which POSIX guarantees is atomic even under concurrent callers on the
same filesystem. Two sessions racing on the same candidate id cannot both create the
same claim file; exactly one os.open() call succeeds, the other raises FileExistsError
and retries the next candidate. No lock, no daemon, no shared state beyond the
filesystem itself.

The counter in the list remains useful as a fast-path hint (it saves scanning upward
from 1 every time) but is no longer the thing that has to be correct. Claim files are
authoritative for "has this id ever been allocated." They are never deleted: an
unclaimed gap (an id claimed and then not used) is an expected, visible cost, not an
error.

Each list keeps its claims in a `.task-ids/` folder beside it, and an id carries the
list's prefix: `AB12`, or `T42` for a list with no prefix. Three things follow:

  * **The default output is a bare integer,** because callers that parse it as a
    number depend on that. `--full-id` prints `AB12`.
  * **The prefix comes from the list, not from a flag.** `<!-- task-prefix: AB -->`
    on line 2 of the list, or a `core/task-lists.json` registry, or `T` when a file
    has neither.
  * **Lanes.** `O_EXCL` is atomic on one filesystem and nowhere else, so two
    machines that sync the same folder while offline could each create
    `AB42.claimed`. Each enrolled machine holds one lane digit and allocates only
    ids ending in it. Enrollment is `--enroll`, one machine at a time.

Usage:
    python3 claim_id.py [--file <path to TASKS.md>] [--claims-dir <path>]
                        [--prefix XX] [--full-id]
    python3 claim_id.py --enroll

Prints the claimed id on stdout and nothing else on success, so it is
script-composable: `TID=$(python3 claim_id.py)`. On any failure, prints a message
to stderr and exits non-zero; it does NOT guess.

This tool does NOT bump the counter in the list and does NOT write the task line.
Both remain the caller's job; `triage.py add` does both.
"""

import argparse
import contextlib
import datetime
import fcntl
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tasklists  # noqa: E402

# The defaults live in the data root. An optional roots resolver names it; with
# no resolver in reach, or one that refuses, it is this engine's own tree three
# levels up. Callers normally pass --file explicitly, so the default rarely
# matters.
_DATA_ROOT = tasklists.data_root() or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
DEFAULT_TASKS_FILE = os.path.join(_DATA_ROOT, "TASKS.md")
DEFAULT_CLAIMS_DIR = os.path.join(_DATA_ROOT, ".task-ids")

# The counter predicate lives in tasklists.py so this file and triage.py cannot
# disagree about which line IS the counter. It was an unanchored search here.
COUNTER_RE = tasklists.COUNTER_RE


def line_id_re(prefix):
    """Line-leading ids only, for the given prefix.

    An id-shaped string quoted inside prose (an old id mentioned in a task's
    description, a test-fixture id quoted inside a body) must never be counted as
    a real allocation.
    """
    return re.compile(r"^- \[.\] %s(\d+)(?![\w-])" % re.escape(prefix), re.MULTILINE)


# Any two- or three-letter prefixed id heading a task line, struck through or
# not (`- [x] ~~AB30 ...`). `T` ids are one letter and never match. A list that
# changed prefix keeps its old ids, and a bare number must never name two tasks
# (no AB20 beside a CD20).
ANY_PREFIXED_LINE_RE = re.compile(r"^- \[.\] (?:~~)?([A-Z]{2,3})(\d+)(?![\w-])", re.MULTILINE)
CLAIM_NAME_RE = re.compile(r"^([A-Z]{2,3})(\d+)\.claimed$")


def other_prefix_numbers(claims_dir, prefix):
    """Numbers this folder already claimed under a different non-`T` prefix.

    Empty for `T`, whose unprefixed numbers are allowed to coexist with every
    prefixed number.
    """
    if prefix == 'T':
        return set()
    try:
        names = os.listdir(claims_dir)
    except OSError:
        return set()
    out = set()
    for name in names:
        m = CLAIM_NAME_RE.match(name)
        if m and m.group(1) != prefix:
            out.add(int(m.group(2)))
    return out


def resolve_prefix(tasks_file, explicit=None, registry=None):
    """The prefix for this list: the flag, the list header, the build, else `T`."""
    if explicit:
        return explicit
    p = tasklists.read_list_prefix(tasks_file, default=None)
    if p:
        return p
    if registry is not None and registry.source == 'build' and registry.self_prefix:
        # An empty folder has no list header to read a prefix from, and this is
        # where its first add gets one.
        return registry.self_prefix
    folder = registry.folder_by_list_path(tasks_file) if registry is not None else None
    if folder:
        return folder.prefix
    return 'T'


def resolve_claims_dir(tasks_file, explicit=None):
    """Claims live beside the list they number, in that folder's `.task-ids/`."""
    if explicit:
        return os.path.abspath(explicit)
    return os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(tasks_file)),
                                        tasklists.CLAIMS_DIRNAME))


def starting_candidate(tasks_file, prefix='T'):
    """Best-guess starting point. Never authoritative; the claim loop below is."""
    try:
        text = open(tasks_file, encoding="utf-8").read()
    except (FileNotFoundError, IsADirectoryError):
        return 1
    counter = tasklists.read_counter(text, default=1)
    line_ids = [int(m) for m in line_id_re(prefix).findall(text)]
    if prefix != 'T':
        # The floor also covers every other prefix in this list, closed lines
        # included, so a new prefix continues past the old one.
        line_ids += [int(n) for _p, n in ANY_PREFIXED_LINE_RE.findall(text)]
    max_line_id = max(line_ids) if line_ids else 0
    return max(counter, max_line_id + 1)


def claim(claims_dir, start, prefix='T', lane=None):
    """Reserve the next free id at or above `start`, in `lane` if one is set.

    A machine allocates only numbers whose last digit is its lane, so ids skip
    (AB3, AB13, AB23) and two machines that are both offline from a shared sync
    folder cannot mint the same number.
    """
    os.makedirs(claims_dir, exist_ok=True)
    # A number held by another prefix's claim file is taken. Read once:
    # only one prefix mints in a folder at a time, so no other-prefix claim
    # appears mid-loop, and the O_EXCL below still guards this prefix's own race.
    taken = other_prefix_numbers(claims_dir, prefix)
    candidate = tasklists.lane_candidates(lane, start)
    while True:
        if candidate in taken:
            candidate = tasklists.next_in_lane(candidate, lane)
            continue
        path = os.path.join(claims_dir, "%s%d.claimed" % (prefix, candidate))
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            candidate = tasklists.next_in_lane(candidate, lane)
            continue
        try:
            note = "claimed %s\n" % datetime.datetime.now().isoformat(timespec="seconds")
            os.write(fd, note.encode("utf-8"))
        finally:
            os.close(fd)
        return candidate


# --- enrollment (allocation lanes) --------------------------------------------

@contextlib.contextmanager
def _manifest_lock(manifest_path, timeout=15.0):
    lockpath = manifest_path + '.lock'
    fd = os.open(lockpath, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        deadline = time.time() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() >= deadline:
                    raise tasklists.TaskListError(
                        'lock-timeout',
                        'could not lock %s within %ss. Nothing was written.'
                        % (lockpath, timeout))
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass
        os.close(fd)


LANES_BLOCK_RE = re.compile(r'^(\s*)lanes:\s*(\[\s*\])?\s*$', re.MULTILINE)


def _render_lane_entry(lane, machine, today):
    return ("    - lane: %d\n      machine: %s\n      enrolled: %s\n"
            % (lane, machine, today))


def write_lane_entry(manifest_path, lane, machine, today):
    """Replace `lanes: []` with the first entry, or append to an existing list.

    A textual edit rather than a YAML round-trip, because the manifest carries
    comments that explain every block in it and a dump would erase them.
    """
    with open(manifest_path, encoding='utf-8') as f:
        text = f.read()
    m = LANES_BLOCK_RE.search(text)
    if not m:
        raise tasklists.TaskListError(
            'registry-no-lanes',
            '%s has no `lanes:` key under task_lists. Nothing was written.'
            % manifest_path)
    entry = _render_lane_entry(lane, machine, today)
    if m.group(2):                      # `lanes: []`, the empty form
        new = text[:m.start()] + m.group(1) + 'lanes:\n' + entry + text[m.end():]
    else:                               # already a list; append after its entries
        rest = text[m.end():]
        end = 0
        for line in rest.split('\n'):
            if line.strip() == '' or line.startswith('    ') or line.lstrip().startswith('#'):
                end += len(line) + 1
                if line.strip() and not line.startswith('    '):
                    break
            else:
                break
        new = text[:m.end()] + rest[:end] + entry + rest[end:]
    tmp = manifest_path + '.enroll.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(new)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, manifest_path)
    return new


def enroll(registry_path=None, today=None):
    """Take the lowest free lane for this machine. Supervised, one at a time.

    A second enrollment of the same machine returns its existing lane, and an
    eleventh machine is refused: ten lanes exist, and widening the lane to two
    digits is a change to the id grammar, not something one run may decide.
    """
    reg = tasklists.load_registry(registry_path, _cache=False)
    if reg.source != 'manifest':
        raise tasklists.TaskListError(
            'enroll-needs-manifest',
            'enrollment writes the lane into manifest.yaml and needs a '
            'process that can reach it. This process resolved a %s registry.'
            % reg.source)
    machine = tasklists.machine_hash()
    today = today or datetime.date.today().isoformat()
    with _manifest_lock(reg.path):
        reg = tasklists.load_registry(reg.path, _cache=False)
        for e in reg.lanes:
            if e.get('machine') == machine:
                lane = int(e.get('lane'))
                _write_lane_file(lane, machine)
                return {'lane': lane, 'machine': machine, 'already_enrolled': True}
        taken = set()
        for e in reg.lanes:
            try:
                taken.add(int(e.get('lane')))
            except (TypeError, ValueError):
                pass
        free = [n for n in range(tasklists.MAX_LANES) if n not in taken]
        if not free:
            raise tasklists.TaskListError(
                'no-free-lane',
                'all %d allocation lanes are taken. An eleventh machine cannot be '
                'enrolled without widening the lane to two digits, which is a '
                'deliberate change to the id grammar and not something this run '
                'decides. Nothing was written.' % tasklists.MAX_LANES)
        lane = free[0]
        write_lane_entry(reg.path, lane, machine, today)
        _write_lane_file(lane, machine)
        return {'lane': lane, 'machine': machine, 'already_enrolled': False}


def _write_lane_file(lane, machine):
    path = tasklists.lane_file_path()
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write('# Written by claim_id.py --enroll.\n')
        f.write('lane: %d\n' % lane)
        f.write('machine: %s\n' % machine)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", default=DEFAULT_TASKS_FILE, help="path to the task list (for the starting-point hint and the prefix header)")
    ap.add_argument("--claims-dir", default=None, help="path to the claim-file directory (default: `.task-ids/` beside the list)")
    ap.add_argument("--prefix", default=None, help="override the prefix; normally read from the list header")
    ap.add_argument("--full-id", action="store_true", help="print AB12 rather than the bare integer 12")
    ap.add_argument("--enroll", action="store_true", help="take an allocation lane for this machine")
    ap.add_argument("--registry", default=None, help="path to manifest.yaml or core/task-lists.json")
    args = ap.parse_args()

    try:
        if args.enroll:
            r = enroll(args.registry)
            print("lane %d recorded for machine %s%s"
                  % (r['lane'], r['machine'],
                     ' (already enrolled)' if r['already_enrolled'] else ''))
            return

        reg = tasklists.load_registry(args.registry,
                                      start=os.path.dirname(os.path.abspath(args.file)))
        tasklists.check_writable_state(reg, 'allocation')
        prefix = resolve_prefix(args.file, args.prefix, reg)
        claims_dir = resolve_claims_dir(args.file, args.claims_dir)
        if os.path.exists(os.path.join(claims_dir, tasklists.BARRIER_NAME)):
            raise tasklists.TaskListError(
                'barrier',
                'a rollback barrier is in place at %s. Nothing was allocated.'
                % os.path.join(claims_dir, tasklists.BARRIER_NAME))
        if reg.state == tasklists.ACTIVE and prefix == 'T' and reg.folders:
            raise tasklists.TaskListError(
                'legacy-id-frozen',
                'the legacy `T` counter froze at the cut and no new `T` id can be '
                'minted. Nothing was allocated. Claim against the folder that owns '
                'the task instead: --file <folder>/TASKS.md, whose '
                '`task-prefix` header supplies the prefix.')
        lane = tasklists.require_lane(reg, prefix, os.path.abspath(__file__))
        start = starting_candidate(args.file, prefix)
        n = claim(claims_dir, start, prefix, lane)
    except tasklists.TaskListError as e:
        tasklists.fail(e)
        return
    print(tasklists.format_id(prefix, n) if args.full_id else n)


if __name__ == "__main__":
    main()
