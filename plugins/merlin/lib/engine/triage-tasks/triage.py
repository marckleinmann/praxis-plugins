#!/usr/bin/env python3
"""
triage-tasks engine: deterministic parse and edit of a TASKS.md task list.

Any other reader of the list (a dashboard, a second parser) MUST derive a
task's fields the same way, so the parsing rules are stated here once:
  - tid: a durable id token right after the checkbox (`AB12`, or `T42` on a
    list with no prefix), assigned once and never reused. '' on lines that
    carry none.
  - project: an explicit `[Code]` / `[General]` bracket right after the tid,
    else a known project code anywhere in the line, else the code in the
    nearest `### heading` (Active only), else "General". The known project
    codes are the user's own: the registry's folder names and tags and the
    manifest's `projects:` names (see `configure_codes`).
  - date: an explicit `(YYYY-MM-DD)` stamp after the tid/bracket, else the
    date in the nearest `### heading` (Active only), else "" (unknown).
  - section: which open section the task lives in: active | waiting | upcoming | unclassified.
  - context: the indented lines directly under the task (preserved originals,
    sub-bullets), markdown-stripped. They travel with the task on close/move.
  - stale: date older than --stale-days (default 60), or --stale-days-p1
    (default 21) for a {P1} task. P1 gets the shorter fuse because the 60-day
    line is too slow for top-priority work.

Canonical line grammar (enforced by `lint`):
  - [ ] AB17 [Website] (2026-06-17) {P2}{waiting} **Title.** optional desc
  (older form, still parsed: `Title -- optional desc`)

Subcommands, JSON in / JSON out, never edits without an explicit --commit:

  list        --file TASKS.md [--stale-days 60] [--all] [--section S] [--today D]
  add         --tag Code --text "body with no id" [--section S] [--list P] [--commit]
              The one action that CREATES a task. Claims an id, composes the line,
              writes it. Where it lands is decided by the registry's state, never
              by which files exist: the single list in pre-cut, the tag's own
              folder list in active, nothing in cutting or rollback. `--list`
              names the list directly and skips the router.
  find        <id> [--list P]        resolve an id to its list, and say whether
              that list is writable. The one resolver; every consumer calls it.
  rollup-path                        the file a reader opens for all open tasks
  insert-block --list P --header H --lines-file F [--commit]
  version                            engine stamp + a sha256 per engine file
  apply       --file TASKS.md --ops ops.json [--today D] [--commit]
              ops.json = [{"tid": "AB42"|"id": int,
                           "action": "close|keep|move|update|append|retag",
                           "text": "new body (update) | text to add (append)"?,
                           "to": "active|waiting|upcoming"?,
                           "priority": "P1|P2|P3"?, "status": "blocked|waiting|wip|next"?,
                           "expect": "sha256 of the line as the caller last saw it"?}]
              `expect` is compared INSIDE the lock and refuses the op with
              `stale-expectation` when the line has changed since. An automated
              caller should set it on every op it sends.
              append/retag read the current line here, inside the lock, so a caller
              never has to read the whole list. One op per task per call: more exits 2
              with op-conflict, except append-then-close, which composes.
  assign-ids  --file TASKS.md [--commit]   idempotent; fills missing tids
  lint        --file TASKS.md [--section S]  structural violations, exit 1 if any
              Two scopes on purpose: date / project-code checks run over OPEN
              tasks; id uniqueness runs over the WHOLE file, open and closed,
              because a closed line still owns its id. `--section` narrows to
              the open set and therefore skips the id scan.

`id` (integer) is the task's index across all open sections at parse time and
remains valid within a session; `tid` is the durable cross-session handle and
is what ops should use once assigned.
"""
import sys, os, re, json, argparse, difflib, datetime, tempfile, hashlib
import contextlib, fcntl, time
import claim_id
import tasklists


@contextlib.contextmanager
def file_lock(path, timeout=15.0):
    """Exclusive lock over a whole read-modify-write of `path`.

    The compare-on-write check in `write_result` is check-then-act: it re-reads
    the file, then calls os.replace. A concurrent writer landing in that window
    is not detected, both processes report success, and the earlier writer's
    edit vanishes with no error and no artifact; concurrent runs without this
    lock lose edits that way. The comparison alone is therefore a backstop, not
    the mechanism; this is the mechanism.

    Locks a SIDECAR `<path>.lock`, never `path` itself, because os.replace swaps
    the inode: a lock held on the original file would not cover the file that
    ends up in its place. flock is released by the kernel when the process exits,
    so a crashed run cannot leave a stale lock behind, which is why this is used
    rather than an O_EXCL lock file.
    """
    lockpath = path + '.lock'
    fd = os.open(lockpath, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        deadline = time.time() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() >= deadline:
                    print(json.dumps({
                        'error': 'lock-timeout',
                        'message': ('could not lock %s within %ss. Another writer is '
                                    'holding it. Nothing was written; re-run.'
                                    % (lockpath, timeout)),
                        'committed': False,
                    }, ensure_ascii=False, indent=2), file=sys.stderr)
                    sys.exit(3)
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass
        os.close(fd)

# --- Project codes: the user's own, never a built-in list ----------------------
# A project code is recognised in three places: a `[Code]` bracket right after
# the id, a code written anywhere in a line, and a `### heading`. The engine
# ships with NO built-in project names. The codes come from the registry the
# list belongs to:
#
#   * NAMES: every registry folder name and legacy tag, plus every `name:` in
#     the manifest's top-level `projects:` list. Matched as whole words.
#   * STEMS: the part before the hyphen of any of those names that has one
#     (`Web` for `Web-Shop`). A stem is a FAMILY: once `Web` is known, any
#     `Web-<anything>` reads as a project code, including one no row names.
#
# With nothing registered, CODE matches nothing, so only an explicit
# `[General]` bracket or the "General" fallback names a project, and no
# ordinary word in a task line is ever mistaken for a project.
#
# The vocabulary is computed once at import from whatever registry is in reach
# of the engine itself, and again by `main()` from the registry in reach of the
# list being read, so the data folder's own manifest decides.
NEVER = r'(?!)'
# Words that mark a routing preamble ahead of the real title, such as
# `Website, needs a session: ` or `priority: `. Project codes are added to
# these by `configure_codes`.
PREAMBLE_WORDS = ('session', 'needs', 'priority', 'backfill', 'optional', 'gate', 'due')
DATE_RE = re.compile(r'(\d{4}-\d{2}-\d{2})')


def code_vocabulary(reg):
    """(stems, names) for a registry; both empty when there is none."""
    if reg is None:
        return [], []
    names = [n for n in reg.project_names() if n]
    return sorted(reg.code_family()), names


def configure_codes(reg=None, stems=None, names=None):
    """Build CODE, CODE_RE, PREAMBLE_RE and CODE_COLON_RE from the user's codes.

    Pass a registry, or `stems` and `names` directly. Longer names are tried
    first so `Web-Shop` wins over `Web` when both are registered.
    """
    global CODE_STEMS, CODE_NAMES, CODE, CODE_RE, PREAMBLE_RE, CODE_COLON_RE
    if reg is not None:
        stems, names = code_vocabulary(reg)
    CODE_STEMS = sorted(set(stems or []))
    CODE_NAMES = sorted(set(names or []), key=lambda n: (-len(n), n))
    alts = []
    if CODE_STEMS:
        alts.append(r'(?:' + '|'.join(re.escape(x) for x in CODE_STEMS) + r')-[A-Za-z]\w*')
    if CODE_NAMES:
        alts.append(r'(?:' + '|'.join(re.escape(x) for x in CODE_NAMES) + r')(?!\w)')
    CODE = r'(?:' + '|'.join(alts) + r')' if alts else NEVER
    CODE_RE = re.compile(r'\b(' + CODE + r')')
    words = list(PREAMBLE_WORDS) + [re.escape(x) + '-' for x in CODE_STEMS]
    pre = r'(' + '|'.join(words) + (r'|' + CODE if alts else '') + r')'
    PREAMBLE_RE = re.compile(pre, re.I)
    CODE_COLON_RE = re.compile(r'^(?:' + CODE + r'):\s+')


def _import_time_registry():
    try:
        return tasklists.load_registry()
    except Exception:
        # A registry that will not parse fails the WRITE path loudly, in
        # guard_write below. It must not take the parser down with it: a reader
        # that cannot group a task by project is a better outcome than none.
        return None


configure_codes(_import_time_registry())
# Recognized inline tags (priority + status). An unknown {token} is NOT a tag:
# it halts extraction and stays in the title.
TAG_RE = re.compile(r'^\{(P1|P2|P3|blocked|waiting|wip|next)\}\s*')
# A `{needs X session}` routing tag, consumed and discarded after the tag block
# (no field assignment). Only the standard `{needs <text> session}` form matches;
# non-standard variants (comma suffix, `+` suffix, no "session" word) stay in the
# title by design.
ROUTING_RE = re.compile(r'^\{needs [^}]+ session\}\s*')
PRIORITIES = {'P1', 'P2', 'P3'}
# The one id grammar, from tasklists.py: `T\d+` or `[A-Z]{2,3}\d+`. Every regex
# below that names an id splices this fragment in rather than writing `T\d+`
# again, so widening the grammar is one edit and not twelve.
ID = tasklists.ID_TOKEN
TID_RE = re.compile(r'^(' + ID + r')\s+')
# Raw-line prefix probes (checkbox, then tid, then [bracket], then (date)).
LINE_TID_RE = re.compile(r'^- \[ \]\s*(' + ID + r')(?![\w-])')

# --- Whole-file id-uniqueness probes ------------------------------------------
# ANY_TID_RE is deliberately NOT LINE_TID_RE. Three differences, each closing a
# blind spot that would let a duplicate id sit undetected:
#
#   1. `[ x]` not `[ ]`. LINE_TID_RE matches only OPEN lines, so a duplicate
#      where one instance is closed and the other open would be structurally
#      invisible to the detector. Id uniqueness spans the whole file, not the
#      open set, because a closed line still owns its id forever.
#   2. `(?![\w-])` not `\b`. `\b` sits between the `8` and the `-` of
#      `AB58-OLD`, so LINE_TID_RE reads that archival companion as a second
#      `AB58` and reports a phantom duplicate. The negative lookahead rejects a
#      trailing hyphen and any word char, so `AB58-OLD` matches nothing here and
#      `AB580` reads as itself rather than as `AB58`.
#
# Suffixed companions (`AB24-orig`, `AB58-OLD`) are a supported convention: a
# closed line plus the preserved original text. They are matched separately so
# they are exempt from the duplicate scan without being mistaken for un-idded
# lines.
#
#   3. `(?:~~)?`. `make_done` writes a closed task in strikethrough form,
#      `- [x] ~~AB12 [Website] ...~~`. Without this, a scan that claims "whole
#      file, open + closed" would skip every closed line and report zero
#      duplicates while real ones sat in the gap. A detector that names a scope
#      it does not cover is worse than one that admits a narrow scope, because
#      the clean run is read as evidence.
ANY_TID_RE = re.compile(r'^- \[[ x]\]\s*(?:~~)?\s*(' + ID + r')(?![\w-])')
COMPANION_TID_RE = re.compile(r'^- \[[ x]\]\s*(?:~~)?\s*(' + ID + r')-([A-Za-z][\w-]*)')
LINE_BRACKET_RE = re.compile(r'^- \[ \]\s*(?:' + ID + r'\s+)?\[([^\]]+)\]')
LINE_DATE_RE = re.compile(r'^- \[ \]\s*(?:' + ID + r'\s+)?(?:\[[^\]]+\]\s*)?\((\d{4}-\d{2}-\d{2})\)')
# A list's own prefix header, written when a list is created.
PREFIX_COMMENT_RE = tasklists.LIST_PREFIX_RE
COUNTER_RE = tasklists.COUNTER_RE   # one predicate, shared with claim_id.py
# Anchored to end-of-line, not \b. A task line that loses its head to a
# concurrent write leaves a tail, and a tail beginning `## Upcoming` followed by
# prose would match \b happily, so section_bounds() would open a section on the
# wreckage and file every following task in the wrong section, invisibly to lint
# and to coverage because the text WAS parsed, just filed wrong. Every heading
# this file legitimately carries is bare, and SECTION_HEADERS below writes them bare.
SECTIONS = [('active', re.compile(r'^##\s+Active\s*$')),
            ('waiting', re.compile(r'^##\s+Waiting On\s*$')),
            ('upcoming', re.compile(r'^##\s+Upcoming\s*$'))]
SECTION_HEADERS = {'active': '## Active', 'waiting': '## Waiting On', 'upcoming': '## Upcoming'}
# Every level-two heading TASKS.md legitimately carries. A line matching `^##\s+`
# that is not one of these is a clobbered task line wearing a heading's clothes.
# Anchoring alone only converts the failure from mis-filed to dropped, so the lint
# check in cmd_lint is what makes the next one announce itself.
KNOWN_HEADING_RE = re.compile(r'^##\s+(Active|Waiting On|Upcoming|Private|Done)\s*$')


def strip_md(s):
    s = re.sub(r'`([^`]*)`', r'\1', s)
    s = s.replace('**', '')
    s = re.sub(r'\[([^\]]+)\]\(([^)]*)\)', r'\1', s)
    s = re.sub(r'[*_]', '', s)
    return re.sub(r'\s+', ' ', s).strip()


def clip(s, n):
    s = s.strip()
    return (s[:n - 1].rstrip() + '…') if len(s) > n else s


def strip_preamble(t):
    """Strip a leading routing preamble, such as `Website, needs a session: `,
    up to the first spaced em dash or `: ` whose preceding segment actually reads
    as a preamble (matches PREAMBLE_RE within 72 chars). A real title with a
    stray spaced em dash (no preamble marker) is left whole."""
    dash = t.find(EM_DASH_SEP)
    if 0 <= dash < 72 and PREAMBLE_RE.search(t[:dash]):
        return t[dash + 3:].strip()
    colon = t.find(': ')
    if 0 <= colon < 72 and PREAMBLE_RE.search(t[:colon]):
        return t[colon + 2:].strip()
    return t


BOLD_SPAN_RE = re.compile(r'\*\*([^*]+)\*\*')
# A spaced em dash, written as an escape so this file carries no literal one.
# Older task lines use it as a title separator, so the parser still reads it.
EM_DASH_SEP = ' \u2014 '


def bold_split(raw_line, t):
    """A `**bold headline.**` opening the remainder is the canonical title form,
    and the most common one in practice. strip_md erases `**` before
    parse_fields ever splits, so without this the headline would be invisible
    and every such line would parse as one 140-char title with an EMPTY desc.
    Detection therefore runs on the RAW line.

    The match is anchored, not free: a bold span counts only when its cleaned
    text is a PREFIX of the already-prefix-stripped remainder. That is what makes
    it safe against the two bold spans that are not titles: a `**Website:**`
    code prefix and a `**Website, needs a session:**` routing preamble are both
    consumed upstream, so the remainder no longer starts with them and they never
    match. Mid-line emphasis fails the same test.

    Returns {'title','desc'} or None."""
    rest = t
    for m in BOLD_SPAN_RE.finditer(raw_line):
        head = strip_md(m.group(1))
        if not head:
            continue
        # A span ending in ':' is a label, not a headline: `**ARCH-VIZ:**`,
        # `**Notes:**`, `**[ON HOLD] Web-Shop:**`. Those are prefixes that
        # CODE_COLON_RE leaves in place (it only knows registered codes), so the
        # remainder DOES still start with them and the anchor alone would
        # promote a bare label to title. Step the cursor past it so a real
        # headline behind an unregistered label is still reachable. The cursor
        # only governs subsequent bold matching: when no headline follows, the
        # fallback title is the untouched remainder and the label DOES remain
        # in it.
        if head.endswith(':'):
            if rest.startswith(head):
                rest = rest[len(head):].strip()
            continue
        if rest.startswith(head):
            return {'title': head,
                    'desc': re.sub(r'^[\s.:;,\u2013\u2014-]+', '', rest[len(head):]).strip()}
    return None


def parse_fields(raw_line, clip_to=140):
    """Canonical parse pipeline, in order: strip checkbox, strip tid, strip
      [code]/(date) prefix, strip a clean `**Code:**` prefix, extract the
      leading {tag} block, strip a routing preamble, then split the remainder on
      a leading `**bold headline**` (canonical), else on the FIRST ` -- ` (or,
      on a tagged line, an older spaced em dash) into title + desc, and clip
      each to 140.
    Returns {'tid','priority','status','title','desc'}. clip_to=None skips the
    clip; the default is the shared-fixture behavior and must not change
    independently of any other parser of the same lists."""
    t = strip_md(re.sub(r'^- \[ \]\s*', '', raw_line))
    tid = ''
    tm = TID_RE.match(t)
    if tm:
        tid = tm.group(1)
        t = t[tm.end():]
    t = re.sub(r'^\[[^\]]+\]\s*', '', t)
    t = re.sub(r'^\(\d{4}-\d{2}-\d{2}\)\s*', '', t)
    cc = CODE_COLON_RE.match(t)
    if cc:
        t = t[cc.end():].strip()
    # Extract the leading {tag} block (known vocabulary only; unknown halts it).
    priority, status = '', ''
    while True:
        m = TAG_RE.match(t)
        if not m:
            break
        tok = m.group(1)
        if tok in PRIORITIES:
            priority = priority or tok
        else:
            status = status or tok
        t = t[m.end():]
    rm = ROUTING_RE.match(t)
    if rm:
        t = t[rm.end():]
    t_pre = t.strip()
    t = strip_preamble(t_pre)
    has_tags = bool(priority or status)
    # Split title / desc: a leading bold headline wins (canonical), then the
    # first ` -- `, then an older spaced em dash on a tagged line. Bold goes first because
    # it is explicit author-marked structure and it survives prose that happens
    # to contain a ` -- ` further along the line.
    #
    # Anchor against the PRE-preamble text first. strip_preamble cuts at the
    # first spaced em dash or `: ` within 72 chars whose left side matches
    # PREAMBLE_RE (priority|session|gate|due|a project code), which beheads any
    # headline carrying its own internal colon: `**HIGHEST PRIORITY: resume the
    # launch.**` loses its first two words, so the remainder no longer starts
    # with the headline and the anchor can never match. That line would then
    # parse with an EMPTY desc, which is precisely the defect this split exists
    # to remove. A bold span is explicit
    # author structure and outranks a heuristic preamble guess, so it is tried
    # against the untouched text first; the stripped text stays as the fallback
    # for a NON-bold preamble followed by a bold headline.
    title, desc = t, ''
    b = bold_split(raw_line, t_pre) or bold_split(raw_line, t)
    if b:
        title, desc = b['title'], b['desc']
    else:
        d = t.find(' -- ')
        if d >= 0:
            title, desc = t[:d], t[d + 4:]
        elif has_tags:
            e = t.find(EM_DASH_SEP)
            if e >= 0:
                title, desc = t[:e], t[e + 3:]
    if clip_to is None:
        return {'tid': tid, 'priority': priority, 'status': status,
                'title': title.strip(), 'desc': desc.strip()}
    return {'tid': tid, 'priority': priority, 'status': status,
            'title': clip(title.strip(), clip_to), 'desc': clip(desc.strip(), clip_to)}


def clean_text(raw_line):
    """Clean display title (back-compat: the title field of parse_fields)."""
    return parse_fields(raw_line)['title']


def clean_context(line):
    """Context sub-bullet → display text: strip md, drop the bullet/checkbox."""
    return re.sub(r'^[-*]\s+(\[[ x~]\]\s+)?', '', strip_md(line))


def age_days(date_str, today):
    if not date_str:
        return None
    try:
        d = datetime.date.fromisoformat(date_str)
    except ValueError:
        return None
    return (today - d).days


def section_bounds(lines):
    """Open sections in document order, including unsectioned leading tasks.

    Leading records are unclassified, never implicitly Active. Private, Done,
    and unknown level-two sections are not open sections. Repeated recognized
    headings each retain their own bounds and inheritance context.
    """
    headers = [(i, l) for i, l in enumerate(lines) if re.match(r'^##\s+', l)]
    out = [('unclassified', -1, headers[0][0] if headers else len(lines))]
    for pos, (start, line) in enumerate(headers):
        section = next((name for name, rx in SECTIONS if rx.match(line)), None)
        if section:
            end = headers[pos + 1][0] if pos + 1 < len(headers) else len(lines)
            out.append((section, start, end))
    return out


def coverage_report(lines, tasks):
    """Account for unchecked rows without disclosing intentionally excluded ones."""
    parsed = {t['lineno'] for t in tasks}
    excluded, unparsed = [], []
    private_or_done = False
    for i, line in enumerate(lines):
        if re.match(r'^##\s+', line):
            private_or_done = bool(re.match(r'^##\s+(Private|Done)\s*$', line))  # anchored, like SECTIONS
        if re.match(r'^- \[ \]', line) and i not in parsed:
            (excluded if private_or_done else unparsed).append(i + 1)
    return {'parsedOpenTasks': len(tasks),
            'unclassifiedOpenTasks': sum(t['section'] == 'unclassified' for t in tasks),
            'excludedSourceLines': excluded, 'unparsedSourceLines': unparsed}


def active_bounds(lines):
    """Back-compat: bounds of ## Active only."""
    for name, start, end in section_bounds(lines):
        if name == 'active':
            return start, end
    return None, None


STALE_DAYS_P1 = 21


def parse_open(lines, stale_days, today, stale_days_p1=STALE_DAYS_P1):
    """Open tasks across named sections plus unsectioned leading tasks, document order.
    id = position across the whole open set.

    Stale is priority-aware: P1 uses the shorter `stale_days_p1` fuse,
    everything else uses `stale_days`. The general 60-day line is too slow for
    top-priority work, so a P1 untouched for three weeks is already rotting.
    """
    tasks = []
    for section, start, end in section_bounds(lines):
        head_proj, head_date = '', ''
        cur = None                     # last parsed task, for context capture
        for i in range(start + 1, end):
            l = lines[i]
            h = re.match(r'^###\s+(.*)$', l)
            if h:
                m = CODE_RE.search(h.group(1)); head_proj = m.group(1) if m else ''
                m = DATE_RE.search(h.group(1)); head_date = m.group(1) if m else ''
                cur = None
                continue
            if re.match(r'^\s+\S', l) and cur is not None:
                cur['context'].append(clean_context(l))
                cur['ctx_linenos'].append(i)
                continue
            if not re.match(r'^- \[ \]', l):
                if l.strip() != '':
                    cur = None
                elif cur is not None:
                    cur = None         # blank line ends the context block
                continue
            em = LINE_DATE_RE.match(l)
            explicit_date = em.group(1) if em else ''
            bm = LINE_BRACKET_RE.match(l)
            bracket = bm.group(1) if bm else ''
            if bracket and (re.fullmatch(CODE, bracket) or bracket == 'General'):
                proj, inline_code = bracket, True
            else:
                cm = CODE_RE.search(l)
                inline_code = bool(cm)
                proj = (cm.group(1) if cm else '') or (head_proj if section == 'active' else '')
            date = explicit_date or (head_date if section == 'active' else '')
            age = age_days(date, today)
            f = parse_fields(l)
            cur = {
                'id': len(tasks),
                'tid': f['tid'],
                'lineno': i,
                'section': section,
                'project': proj or 'General',
                'date': date,
                'age': age,
                'stale': age is not None and age >= (
                    stale_days_p1 if f['priority'] == 'P1' else stale_days),
                'priority': f['priority'],
                'status': f['status'],
                'title': f['title'],
                'desc': f['desc'],
                'text': f['title'],
                'raw': l.rstrip('\n'),
                'context': [],
                'ctx_linenos': [],
                'explicit_date': bool(explicit_date),
                'inline_code': inline_code,
            }
            tasks.append(cur)
    return tasks


def parse_active(lines, stale_days, today):
    """Back-compat alias: all open tasks (now section-aware)."""
    return parse_open(lines, stale_days, today)


OUT_KEYS = ('id', 'tid', 'section', 'date', 'age', 'stale', 'priority',
            'status', 'title', 'desc', 'text', 'raw', 'context')


def cmd_list(args, today):
    with open(args.file, encoding='utf-8') as f:
        lines = f.read().split('\n')
    tasks = parse_open(lines, args.stale_days, today, args.stale_days_p1)
    coverage = coverage_report(lines, tasks)
    if args.section:
        tasks = [t for t in tasks if t['section'] == args.section]
    shown = tasks if args.all else [t for t in tasks if t['stale']]
    by_proj = {}
    for t in shown:
        by_proj.setdefault(t['project'], []).append({k: t[k] for k in OUT_KEYS})
    # most-stale-first, General last (mirror the panel ordering)
    def keyf(item):
        name, ts = item
        return (0 if name == 'General' else 1,
                -sum(1 for x in ts if x['stale']), -len(ts), name)
    projects = [{'project': n, 'tasks': ts} for n, ts in sorted(by_proj.items(), key=keyf)]
    print(json.dumps({'staleCount': sum(1 for t in tasks if t['stale']),
                      'total': len(tasks), 'projects': projects, 'coverage': coverage}, ensure_ascii=False, indent=2))


def restamp_date(raw, today):
    """Set the added-date stamp to today, preserving tid + [code] prefixes.

    The tid group takes every id shape (`AB12` as well as `T42`). A pattern that
    knew only `T` ids would read a prefixed id as body text and put the new date
    in front of it, so the task would lose its id."""
    iso = today.isoformat()
    m = re.match(r'^(- \[ \]\s*)(' + ID + r'\s+)?(\[[^\]]+\]\s*)?(\(\d{4}-\d{2}-\d{2}\)\s*)?(.*)$', raw)
    if not m:
        return raw
    box, tid, code, _olddate, rest = (m.group(1), m.group(2) or '',
                                      m.group(3) or '', '', m.group(5))
    return f'{box}{tid}{code}({iso}) {rest}'


def make_done(raw, today):
    """Format a closed task for the Done section, preserving its body."""
    body = re.sub(r'^- \[ \]\s*', '', raw).strip()
    return f'- [x] ~~{body}~~ (closed {today.isoformat()})'


def resolve_op_task(op, by_tid, by_pos):
    """An op names a task by durable tid ("AB42", preferred) or positional int."""
    ref = op.get('tid', op.get('id'))
    if isinstance(ref, str) and tasklists.is_id(ref):
        return by_tid.get(ref)
    if isinstance(ref, int):
        return by_pos.get(ref)
    return None


# --- Per-line edit actions ------------------------------------------------------
# `update` replaces a task's top line, but it makes the caller supply the WHOLE
# new line. On a large list, obtaining that line means a whole-file
# read-modify-write, which is the exact unsafe window file_lock exists to close.
# `append` and `retag` take only a tid and the change, and read the current line
# HERE, inside the lock.

STATUSES = {'blocked', 'waiting', 'wip', 'next'}


class OpConflict(Exception):
    """Two ops target one task in a single ops file. Raised rather than resolved;
    see group_ops_by_task for why this fails closed instead of picking an order."""


# A newline in op text would insert a physical line carrying no `- [ ]` prefix.
# parse_open (above) treats such a line as neither task nor context, so it
# survives on disk and is silently dropped by every future read. Refused rather than stripped: quietly altering a caller's
# annotation is its own surprise. Applied to every text-bearing action, which
# also closes the identical latent hole in `update`.
NEWLINE_RE = re.compile(r'[\r\n]')


def validate_op_text(text):
    """Return an error string, or None when the text is safe to write."""
    if NEWLINE_RE.search(text or ''):
        return 'text contains a newline; a task entry is one physical line'
    return None


def append_to_raw(raw, text):
    """Append to a task's top line. Deliberately no parse: the canonical grammar
    constrains a line's PREFIX, and the body is opaque here."""
    return raw.rstrip('\r\n') + text


# Same prefix shape as restamp_date: checkbox, optional tid (any id shape),
# optional [code], optional (date). Then the tag block, an optional routing tag,
# then the body. A tid group that knew only `T` ids would leave a prefixed id in
# the body, and the new tag block would be written in front of the id.
RETAG_PREFIX_RE = re.compile(
    r'^(- \[ \]\s*(?:' + ID + r'\s+)?(?:\[[^\]]+\]\s*)?(?:\(\d{4}-\d{2}-\d{2}\)\s*)?)(.*)$')
# Any `{...}` sitting where a tag block would. Used only to REFUSE, never to parse:
# TAG_RE is the tag vocabulary and ROUTING_RE the routing form, so a brace token
# matching neither means the line is malformed at exactly the position retag writes.
BRACE_TOKEN_RE = re.compile(r'^\{[^}]*\}')


def retag_raw(raw, priority=None, status=None):
    """Replace the {P?} and/or {status} tokens on a task's top line, preserving the
    body and any `{needs X session}` routing tag. Returns (new_line, error).

    None leaves a field alone; '' clears it. A line with NO tag block is the common
    case rather than an edge one (both tags are optional), so
    a fresh block is inserted where parse_fields expects it: after the (date) field
    and before any routing tag."""
    m = RETAG_PREFIX_RE.match(raw)
    if not m:
        return None, 'line does not match the canonical task-line prefix'
    prefix, rest = m.group(1), m.group(2)
    cur_p, cur_s = '', ''
    while True:
        tm = TAG_RE.match(rest)
        if not tm:
            break
        tok = tm.group(1)
        if tok in PRIORITIES:
            cur_p = cur_p or tok
        else:
            cur_s = cur_s or tok
        rest = rest[tm.end():]
    # A malformed tag block: TAG_RE halts on an unknown {token}, so any recognized
    # tag BEHIND it was never consumed and a fresh block would be inserted in front
    # of it. `{foo}{P2}` retagged to P1 becomes `{P1}{foo}{P2}`, which parse_fields
    # then reads as the literal title `{foo}{P2} Weird. desc` with the description
    # silently dropped. Refuse rather than guess: a skipped op is visible, a
    # corrupted line is not. A routing tag here is legitimate and passes through.
    if BRACE_TOKEN_RE.match(rest) and not ROUTING_RE.match(rest):
        return None, ('unrecognized {token} at the tag position (%s); refusing to '
                      'guess. Fix the line by hand, or use update with the full line.'
                      % BRACE_TOKEN_RE.match(rest).group(0))
    new_p = cur_p if priority is None else priority
    new_s = cur_s if status is None else status
    if new_p and new_p not in PRIORITIES:
        return None, 'unknown priority %r (want one of %s)' % (new_p, sorted(PRIORITIES))
    if new_s and new_s not in STATUSES:
        return None, 'unknown status %r (want one of %s)' % (new_s, sorted(STATUSES))
    if priority is None and status is None:
        return None, 'retag needs "priority" and/or "status"'
    tags = ('{%s}' % new_p if new_p else '') + ('{%s}' % new_s if new_s else '')
    # Separator. On a line that HAD tags, TAG_RE consumed the space after them, so
    # `rest` begins at the routing tag or the body. On a line that had none, the
    # prefix already consumed the only space, so inserting a fresh block would
    # yield `{P1}**Title**`. A routing tag abuts the block by convention
    # (`{P2}{needs X session} **Title**`), so no space before a `{`.
    if tags and rest and not rest.startswith('{') and not rest[:1].isspace():
        rest = ' ' + rest
    return prefix + tags + rest, None


# The one composable pair, in this order. append-then-close is the motivating
# case: annotate a line with why it closed, then close it.
def line_hash(raw):
    """sha256 of a task line, the unit `expect` and its caller both compare on."""
    return hashlib.sha256((raw or '').rstrip('\n').encode('utf-8')).hexdigest()


def line_id(raw):
    """The id a task line carries, open or closed. '' when it carries none."""
    m = ANY_TID_RE.match(raw or '')
    return m.group(1) if m else ''


def keep_id(old_raw, new_line):
    """(line, error). An `update` may not drop or change the task's id."""
    old = line_id(old_raw)
    if not old:
        return new_line, None
    new = line_id(new_line)
    if new == old:
        return new_line, None
    if not new:
        return re.sub(r'^- \[ \]\s*', '- [ ] %s ' % old, new_line, count=1), None
    return new_line, ('update would change the id %s -> %s. An id survives close, '
                      'move, retag and update, and a claimed id is never reused, '
                      'so this is refused rather than renumbered.' % (old, new))


def get_registry(args=None):
    """The registry this run routes by. Cached; raises on one that will not parse.

    `--registry` first, then `tasklists.load_registry`'s own order: the env
    var, a `core/task-lists.json` beside a copied engine, an optional resolver's
    data-root manifest, then the plain walk from the engine and from the list's
    own folder. The list's folder is passed as `start` so the last step can
    still find a registry beside a list the other steps did not reach.
    """
    explicit = getattr(args, 'registry', None) if args is not None else None
    start = None
    f = getattr(args, 'file', None) or getattr(args, 'list', None) if args is not None else None
    if f:
        start = os.path.dirname(os.path.abspath(f))
    return tasklists.load_registry(explicit, start=start)


def guard_write(path, reg, allow_frozen=False):
    """Every refusal that has to happen BEFORE a byte is written.

    Four of them, in this order: the local barrier first, because a folder that
    cannot see the registry cannot read the state field at all; then the state;
    then the frozen ledger, which `write_result` refuses in `active` whatever
    route a caller took to reach it; then the GENERATED marker, which covers any
    generated roll-up file.
    """
    tasklists.check_barrier(path)
    tasklists.check_writable_state(reg)
    if (not allow_frozen and reg.state == tasklists.ACTIVE
            and tasklists.is_frozen_ledger(reg, path)):
        raise tasklists.TaskListError(
            'frozen-ledger',
            '%s is frozen. Nothing was written. Open the folder\'s own TASKS.md, '
            'or let the engine route: it rewrites a command that names the ledger '
            'to the list the task actually lives in.' % path)
    if tasklists.is_generated(path):
        raise tasklists.TaskListError(
            'generated-file',
            '%s carries the GENERATED marker and is rebuilt from the task lists. '
            'Nothing was written. Edit the folder\'s own TASKS.md instead.' % path)




# --- the resolver, the router and the two actions they exist for -------------
#
# Everything from here to `cmd_apply` is what makes a move from one list to one
# list per folder safe. The rule is one sentence: **routing is decided by the
# registry's state, never by whether a file happens to exist.** In `pre-cut`
# every write lands in the single list. Once the state flips to `active`, the
# same command lands in the right folder, so a caller written for the single
# list keeps working with no window in which caller and engine disagree.


def _scan_list_for_id(path, tid):
    """(lineno, raw, closed, section) for `tid` in `path`, or None.

    Open and closed both, and `## Private` too: a closed line still owns its id
    forever, and a private task is excluded from derived views, never from the
    resolver.
    """
    try:
        with open(path, encoding='utf-8') as f:
            lines = f.read().split('\n')
    except (IOError, OSError):
        return None
    section = ''
    for i, l in enumerate(lines):
        hm = re.match(r'^##\s+(.*?)\s*$', l)
        if hm:
            section = hm.group(1)
            continue
        m = ANY_TID_RE.match(l)
        if m and m.group(1) == tid:
            return (i, l, l.startswith('- [x]'), section)
    return None


def candidate_lists(reg, tid):
    """Where to look for `tid`, in the order the contract states.

    In `active`: a prefixed id goes through the registry to its own folder's list;
    a `T` id is looked for in every folder list first, open and done; and only
    then in the frozen ledger. That order matters, because the frozen file still
    shows every migrated line as OPEN by design, so it must answer only for an id
    that is in no folder list at all.

    In `pre-cut`, including after a rollback, every id resolves in the single
    list and the folder lists are not consulted.
    """
    out = []
    if reg.state == tasklists.ACTIVE:
        folder = reg.folder_for_id(tid)
        if folder:
            # An unreachable row has no list_path. It contributes nothing rather
            # than a bare relative `TASKS.md`, which would resolve against the
            # caller's own working directory and answer with a foreign folder's
            # task. The id then resolves nowhere here, which is correct: this
            # process cannot see that list, and `route_apply` refuses.
            if folder.path:
                out.append(folder.list_path)
        else:
            out.extend(f.list_path for f in reg.folders if f.path)
    if reg.frozen_ledger:
        out.append(reg.frozen_ledger)
    return [p for p in out if p]


def is_writable_list(reg, path):
    if reg.state in (tasklists.CUTTING, tasklists.ROLLBACK):
        return False
    if reg.state == tasklists.ACTIVE:
        return not tasklists.is_frozen_ledger(reg, path)
    return True


def resolve_id(reg, tid, extra=None):
    """The one resolver. Every consumer that looks a task up by id calls this."""
    out = {'id': tid, 'state': reg.state, 'found': False, 'list': None,
           'lineno': None, 'closed': None, 'section': None, 'writable': False,
           'raw': None, 'searched': []}
    if not tasklists.is_id(tid):
        out['error'] = 'not an id: %r' % (tid,)
        return out
    paths = list(extra or []) + candidate_lists(reg, tid)
    seen = set()
    for path in paths:
        if not path:
            continue
        ap = os.path.abspath(path)
        if ap in seen:
            continue
        seen.add(ap)
        out['searched'].append(ap)
        hit = _scan_list_for_id(path, tid)
        if hit:
            out.update({'found': True, 'list': ap, 'lineno': hit[0] + 1,
                        'raw': hit[1], 'closed': hit[2], 'section': hit[3],
                        'writable': is_writable_list(reg, path),
                        'hash': line_hash(hit[1])})
            return out
    return out


def cmd_find(args, today):
    reg = get_registry(args)
    res = resolve_id(reg, args.id, extra=[args.list] if args.list else None)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    sys.exit(0 if res['found'] else 1)


def engine_fingerprint():
    """The version stamp and a sha256 per engine file.

    A copy of this engine can run far from its source with no way to notice
    that the source has moved on. Comparing this stamp and these hashes makes a
    copy running an old router a reported condition rather than a silent one.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    files = {}
    for name in ('triage.py', 'claim_id.py', 'tasklists.py'):
        path = os.path.join(here, name)
        try:
            with open(path, 'rb') as f:
                files[name] = hashlib.sha256(f.read()).hexdigest()
        except (IOError, OSError):
            files[name] = None
    return {'engine_version': tasklists.ENGINE_VERSION, 'engine_dir': here,
            'files': files}


def cmd_version(args, today):
    print(json.dumps(engine_fingerprint(), ensure_ascii=False, indent=2))


def cmd_rollup_path(args, today):
    """The file a reader opens for all open tasks, by state.

    A reader that calls this keeps working when the registry's state changes:
    until the state flips to `active` it answers the single list.
    """
    reg = get_registry(args)
    path = tasklists.rollup_path(reg)
    if not path:
        print(json.dumps({'error': 'no-registry', 'message':
                          'no task_lists registry is in reach, so there is no '
                          'roll-up to name.'}, indent=2), file=sys.stderr)
        sys.exit(4)
    print(path)


def ensure_list(reg, folder, today):
    """Lazy creation, `active` only, registered folders only.

    An O_CREAT|O_EXCL create of the file with its two header comments, under that
    folder's own TASKS.md.lock, which is the same lock a rollback holds while it
    writes the BARRIER.
    """
    # Before anything touches a path. An unreachable row's `list_path` is None.
    # A bare relative `TASKS.md` there would be answered by `os.path.exists`
    # against the caller's working directory, so the early return below would
    # hand back the CALLER's own list for a foreign folder.
    tasklists.check_reachable(folder, 'task list')
    path = folder.list_path
    if os.path.exists(path):
        return path, False
    if reg.state != tasklists.ACTIVE:
        raise tasklists.TaskListError(
            'lazy-create-refused',
            'a task list is created lazily only in the active state, and the '
            'state is %s. Nothing was written.' % reg.state)
    if not os.path.isdir(folder.path):
        raise tasklists.TaskListError(
            'no-folder',
            '%s is registered at %s, which is not a directory in reach of this '
            'session. Nothing was written.' % (folder.name, folder.path))
    with file_lock(path):
        tasklists.check_barrier(path)
        if os.path.exists(path):
            return path, False
        header = '\n'.join(tasklists.list_header(folder.prefix)) + '\n'
        header += '## Active\n\n## Waiting On\n\n## Upcoming\n\n## Done\n'
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        try:
            os.write(fd, header.encode('utf-8'))
        finally:
            os.close(fd)
    return path, True


LEADING_BRACKET_RE = re.compile(r'^\[[^\]]+\]')
LEADING_DATE_RE = re.compile(r'^\(\d{4}-\d{2}-\d{2}\)')


def compose_line(tid, tag, text, today):
    """`- [ ] AB12 [Website] (2026-09-20) {P2} **Headline.** desc`.

    The caller supplies the body and nothing else. A body that already opens with
    its own `[Tag]` or `(date)` keeps it, so a caller that writes the whole line
    is not fought with.
    """
    body = (text or '').strip()
    body = re.sub(r'^- \[ \]\s*', '', body)
    m = ANY_TID_RE.match('- [ ] ' + body)
    if m:
        body = body[len(m.group(1)):].lstrip()
    if not LEADING_BRACKET_RE.match(body) and tag:
        body = '[%s] %s' % (tag, body)
    rest = LEADING_BRACKET_RE.sub('', body, count=1).lstrip() if tag else body
    if not LEADING_DATE_RE.match(rest):
        head = body[:len(body) - len(rest)]
        body = '%s(%s) %s' % (head, today.isoformat(), rest)
    return '- [ ] %s %s' % (tid, body.strip())


def cmd_add(args, today):
    """Create a task. The one action that does.

    `append` annotates a line that already exists and never creates one. This
    claims an id and writes the line under the list's lock, which is what a
    hand edit of the file cannot do safely.
    """
    reg = get_registry(args)
    try:
        tasklists.check_writable_state(reg, 'add')
        orphaned = False
        folder = None
        if args.list:
            path = os.path.abspath(args.list)
        elif reg.state == tasklists.ACTIVE:
            if not reg.folders:
                raise tasklists.TaskListError(
                    'no-registry', 'no task_lists registry is in reach, so a tag '
                    'cannot be routed. Pass --list. Nothing was written.')
            folder, orphaned = reg.route(args.tag)
            if folder is None:
                raise tasklists.TaskListError(
                    'no-orphan-default',
                    'tag %r is in no registry row and the registry names no '
                    'orphan_default to route it to. Nothing was written.' % args.tag)
            # The routed folder may be one this process cannot reach: a JSON
            # registry carries every folder's name and prefix and one reachable path.
            # Refuse before a claim, before a lock and before a byte, and say that
            # the outbox is the way across. `ensure_list` refuses the same case,
            # and the check is here as well so the message names `add` and the
            # refusal happens before an id is claimed.
            tasklists.check_reachable(folder, 'add')
            path, _created = ensure_list(reg, folder, today)
        else:
            path = reg.frozen_ledger
            if not path:
                raise tasklists.TaskListError(
                    'no-registry', 'no task_lists registry is in reach and no '
                    '--list was given, so there is nowhere to add. Nothing was '
                    'written.')
        guard_write(path, reg)
        prefix = tasklists.read_list_prefix(path)
        claims_dir = claim_id.resolve_claims_dir(path, args.claims_dir)
        lane = tasklists.require_lane(reg, prefix, claim_id.__file__)
    except tasklists.TaskListError as e:
        tasklists.fail(e)
        return

    args.file = path
    with file_lock(path):
        with open(path, encoding='utf-8') as f:
            original = f.read()
        lines = original.split('\n')
        start = claim_id.starting_candidate(path, prefix)
        if args.commit:
            n = claim_id.claim(claims_dir, start, prefix, lane)
        else:
            # Preview only. Nothing is reserved, exactly as assign-ids does it,
            # so a dry run can never consume id-space for a run that writes
            # nothing.
            n = tasklists.lane_candidates(lane, start)
        tid = tasklists.format_id(prefix, n)
        line = compose_line(tid, args.tag, args.text, today)
        err = validate_op_text(line)
        if err:
            print(json.dumps({'error': 'bad-text', 'message': err,
                              'committed': False}, indent=2), file=sys.stderr)
            sys.exit(2)
        header = SECTION_HEADERS.get(args.section, '## Active')
        new_lines = insert_into_section(lines, header, [line])
        new_lines = set_counter(new_lines, tasklists.next_in_lane(n, lane))
        report = [{'id': tid, 'status': 'added', 'list': path, 'section': args.section,
                   'tag': args.tag, 'orphan_routed': orphaned,
                   'folder': folder.name if folder else None,
                   'claimsDir': claims_dir, 'lane': lane, 'line': line,
                   'hash': line_hash(line)}]
        if not args.commit:
            report[0]['note'] = ('preview only: this id is NOT claimed and may '
                                 'differ on commit')
        write_result(args, original, '\n'.join(new_lines), report)


def cmd_insert_block(args, today):
    """Insert lines under a named heading while holding the list's lock.

    It exists for an automated writer that keeps its own section of the list.
    A writer in another language may have no flock of its own, so this is how
    every supported writer ends up going through the locked engine.
    """
    reg = get_registry(args)
    path = args.list
    try:
        guard_write(path, reg)
    except tasklists.TaskListError as e:
        tasklists.fail(e)
        return
    with open(args.lines_file, encoding='utf-8') as f:
        block = f.read().rstrip('\n').split('\n')
    args.file = path
    with file_lock(path):
        with open(path, encoding='utf-8') as f:
            original = f.read()
        lines = original.split('\n')
        # One predicate with insert_into_section, deliberately: a guard that
        # finds the heading while the inserter does not would append a
        # duplicate heading below `## Done` and still report `committed: true`.
        if args.require_header and not any(heading_matches(l, args.header)
                                           for l in lines):
            print(json.dumps({'error': 'no-header', 'message':
                              '%s has no %r heading and --require-header was '
                              'given. Nothing was written.' % (path, args.header),
                              'committed': False}, indent=2), file=sys.stderr)
            sys.exit(2)
        new_lines = insert_into_section(lines, args.header, block)
        write_result(args, original, '\n'.join(new_lines),
                     [{'status': 'inserted', 'list': path, 'header': args.header,
                       'lines': len(block)}])


def route_apply(args, reg):
    """Resolve every op's id before anything is written, and reroute if needed.

    Two refusals, and both happen here rather than in `apply_ops`, because that
    function reports `skipped: unknown id` and then falls through to the
    unconditional `os.replace` in `write_result`: an id that resolves nowhere,
    and an id that resolves only to the frozen file, which is an old done line
    and is readable, never writable.

    A command that names the frozen single list is rerouted and not refused,
    so a caller written before the move keeps working.
    """
    if reg.state != tasklists.ACTIVE or not reg.folders:
        return args.file, []
    with open(args.ops, encoding='utf-8') as f:
        ops = json.load(f)
    targets, notes = set(), []
    for op in ops:
        ref = op.get('tid', op.get('id'))
        if not tasklists.is_id(ref):
            continue
        res = resolve_id(reg, ref)
        if not res['found']:
            raise tasklists.TaskListError(
                'unresolved-id',
                '%s is in no task list this process can reach. Nothing was '
                'written. Check the id, or run `triage.py find %s` to see where '
                'the engine looked.' % (ref, ref))
        if not res['writable']:
            raise tasklists.TaskListError(
                'frozen-only-id',
                '%s lives only in the frozen %s, which means it is an old '
                'closed line. It is readable and never writable. Nothing was '
                'written.' % (ref, res['list']))
        targets.add(res['list'])
        notes.append({'id': ref, 'list': res['list']})
    if not targets:
        return args.file, notes
    if len(targets) > 1:
        raise tasklists.TaskListError(
            'multi-list-ops',
            'the ops in this call name tasks in more than one list (%s). The '
            'engine writes one list per call under one lock. Nothing was '
            'written; split them.' % ', '.join(sorted(targets)))
    target = sorted(targets)[0]
    if not tasklists.same_file(target, args.file):
        notes.append({'rerouted_from': os.path.abspath(args.file),
                      'rerouted_to': target})
    return target, notes


COMPOSABLE = ('append', 'close')


def group_ops_by_task(ops, by_tid, by_pos):
    """Resolve each op to a task and group by line. Returns (groups, unresolved).

    More than one op on one task FAILS THE WHOLE CALL, because apply_ops computes
    every op from the ORIGINAL line (by_tid is built once, before the loop) and the
    assembly loop tests `remove` before `replace`. So a second op on a task silently
    loses while the report still claims both applied, which is the precise
    silent-clobber signature this engine exists to prevent. Refused rather than
    ordered: guessing a caller's intent is how the loss becomes invisible again.
    The single COMPOSABLE pair is allowed because its order is unambiguous."""
    groups, unresolved = {}, []
    for op in ops:
        t = resolve_op_task(op, by_tid, by_pos)
        if not t:
            unresolved.append(op)
            continue
        groups.setdefault(t['lineno'], (t, []))[1].append(op)
    for ln, (t, ops_here) in groups.items():
        if len(ops_here) == 1:
            continue
        actions = tuple(o.get('action') for o in ops_here)
        if actions == COMPOSABLE:
            continue
        raise OpConflict(
            '%s has %d ops in one call (%s). Nothing was written. The engine '
            'computes every op from the original line, so all but one would be '
            'silently dropped while the report claimed success. Split them into '
            'separate calls, or use the one composable pair %s.'
            % (t['tid'] or t['id'], len(ops_here), ', '.join(map(repr, actions)),
               ' then '.join(COMPOSABLE)))
    return groups, unresolved


def apply_ops(lines, ops, tasks, today):
    """Return new lines. close → remove block (task + context) from its section,
    queue for Done with context preserved; update → replace the top line only;
    append → concatenate to the top line; retag → swap its {tag} block;
    keep → restamp date; move → relocate the whole block to another section."""
    by_tid = {t['tid']: t for t in tasks if t['tid']}
    by_pos = {t['id']: t for t in tasks}
    remove = set()
    replace = {}                     # lineno -> new line
    done_blocks = []                 # closed blocks, top of Done
    move_blocks = {'active': [], 'waiting': [], 'upcoming': []}
    report = []
    # Fails closed on a same-task collision before anything is computed.
    group_ops_by_task(ops, by_tid, by_pos)
    for op in ops:
        t = resolve_op_task(op, by_tid, by_pos)
        if not t:
            report.append({'id': op.get('tid', op.get('id')), 'status': 'skipped: unknown id'})
            continue
        action = op.get('action')
        ln, block = t['lineno'], [t['lineno']] + t['ctx_linenos']
        ref = t['tid'] or t['id']
        # `expect`, the precondition. The caller sends the sha256 of the line as
        # it last saw it, and this comparison happens inside cmd_apply's flock.
        # Checking before the lock is not enough: the case this exists for is a
        # writer that changes the line between the caller's read and the engine
        # taking the lock, which is exactly the window a pre-lock check cannot
        # see.
        expect = op.get('expect')
        if expect and line_hash(t['raw']) != expect:
            report.append({'id': ref, 'status': 'stale-expectation',
                           'expected': expect, 'found': line_hash(t['raw'])})
            continue
        if action == 'close':
            remove.update(block)
            # replace.get(ln, ...) rather than t['raw'], so a composed
            # append-then-close carries the annotation into the Done block. With
            # no preceding append this is exactly the old behavior.
            done_blocks.append([make_done(replace.get(ln, t['raw']), today)]
                               + [lines[j] for j in t['ctx_linenos']])
            report.append({'id': ref, 'status': 'closed', 'text': t['text']})
        elif action == 'keep':
            replace[ln] = restamp_date(t['raw'], today)
            report.append({'id': ref, 'status': 'kept (restamped %s)' % today.isoformat()})
        elif action == 'update':
            new_body = (op.get('text') or '').strip()
            if not new_body:
                report.append({'id': ref, 'status': 'skipped: update with no text'})
                continue
            err = validate_op_text(new_body)
            if err:
                report.append({'id': ref, 'status': 'skipped: ' + err})
                continue
            line = new_body if new_body.startswith('- [ ]') else ('- [ ] ' + new_body)
            # An id cannot be edited away. `update` replaces the whole line, and
            # nothing else stops the caller's text from dropping the id or
            # carrying a different one. The resolver finds a task by its id, so
            # a line that loses its id is a task no reference can reach. Text
            # with no id gets the original back; text with a different id is
            # refused rather than silently renumbered.
            line, iderr = keep_id(t['raw'], line)
            if iderr:
                report.append({'id': ref, 'status': 'skipped: ' + iderr})
                continue
            replace[ln] = line
            report.append({'id': ref, 'status': 'updated', 'text': clean_text(line)})
        elif action == 'append':
            text = op.get('text') or ''
            if not text.strip():
                report.append({'id': ref, 'status': 'skipped: append with no text'})
                continue
            err = validate_op_text(text)
            if err:
                report.append({'id': ref, 'status': 'skipped: ' + err})
                continue
            line = append_to_raw(replace.get(ln, t['raw']), text)
            replace[ln] = line
            report.append({'id': ref, 'status': 'appended',
                           'added_bytes': len(text.encode('utf-8')),
                           'line_length': len(line)})
        elif action == 'retag':
            if 'priority' not in op and 'status' not in op:
                report.append({'id': ref, 'status': 'skipped: retag needs "priority" and/or "status"'})
                continue
            line, err = retag_raw(replace.get(ln, t['raw']),
                                  priority=op.get('priority'), status=op.get('status'))
            if err:
                report.append({'id': ref, 'status': 'skipped: ' + err})
                continue
            replace[ln] = line
            report.append({'id': ref, 'status': 'retagged', 'text': clean_text(line)})
        elif action == 'move':
            to = op.get('to')
            if to not in move_blocks:
                report.append({'id': ref, 'status': 'skipped: move needs "to": active|waiting|upcoming'})
                continue
            if to == t['section']:
                report.append({'id': ref, 'status': 'skipped: already in %s' % to})
                continue
            remove.update(block)
            move_blocks[to].append([lines[j] for j in block])
            report.append({'id': ref, 'status': 'moved to %s' % to, 'text': t['text']})
        else:
            report.append({'id': ref, 'status': 'skipped: unknown action %r' % action})

    out = []
    for i, l in enumerate(lines):
        if i in remove:
            continue
        out.append(replace.get(i, l))
    for section, blocks in move_blocks.items():
        for block in reversed(blocks):
            out = insert_into_section(out, SECTION_HEADERS[section], block)
    if done_blocks:
        flat = [l for b in done_blocks for l in b]
        out = insert_into_section(out, '## Done', flat)
    return out, report


def heading_key(s):
    """Normalized form of a heading line, for comparing one heading to another."""
    return re.sub(r'\s+', ' ', s).strip()


def heading_matches(line, header):
    r"""True when `line` IS `header`, ignoring only whitespace.

    Section detection is end-anchored, and so is this. A pattern of
    `re.escape(header) + r'\b'` can never match a heading that ends in a
    non-word character, because `\b` at end of line needs a word character
    after it. A heading such as `### Inbound leads (rolling)` ends in `)`, so it
    would never be found and the create-it-if-absent branch would append a
    second copy of it, with the new task under it, at the end of the file below
    `## Done`.

    `--require-header` compares whole lines, so a guard and an inserter using
    different predicates would disagree and the caller would get exit 0. Both
    call this one predicate, so they cannot disagree. Exact and not prefix: a
    prefix match makes `## Active` match `## Active Loops`, which would mis-file
    tasks, and no caller carries a trailing annotation on a heading.
    """
    return heading_key(line) == heading_key(header)


def insert_into_section(lines, header, new_lines):
    """Insert new_lines just under the given `## Header` (create it if absent)."""
    idx = next((i for i, l in enumerate(lines) if heading_matches(l, header)), -1)
    if idx < 0:
        tail = ['', header, ''] + new_lines
        while lines and lines[-1].strip() == '':
            lines.pop()
        return lines + tail
    insert_at = idx + 1
    if insert_at < len(lines) and lines[insert_at].strip() == '':
        insert_at += 1
    return lines[:insert_at] + new_lines + lines[insert_at:]


def insert_into_done(lines, done_lines):
    """Back-compat wrapper."""
    return insert_into_section(lines, '## Done', done_lines)


# A batch in which every op is skipped must not look like success. Without the
# rules below, an ops file keyed `op` instead of `action` would produce
# `skipped: unknown action None`, print `(no changes)`, write a `.bak`, and still
# report `committed: true` with exit 0. Any caller that batches task edits reads
# that field, so the work would be lost with a success signal on top of it.
#
# Two rules, and they are separate:
#   `committed` is true only when at least one op actually changed a line.
#   A batch carrying ANY skipped op exits non-zero and names what was skipped.
# So a partial batch still writes what it could and still exits non-zero: the
# caller is told, in the exit code, that its batch was not fully applied.
#
# Scoped to `apply` through `enforce_ops`. `add`, `insert-block` and `assign-ids`
# keep the old shape on purpose: `assign-ids` legitimately changes nothing when
# every task already carries an id, and that is success, not a skipped op.
OPS_SKIPPED_EXIT = 3


def skipped_ops(report):
    """The entries of an apply report whose status starts `skipped`."""
    return [r for r in report
            if isinstance(r, dict) and str(r.get('status', '')).startswith('skipped')]


def write_result(args, original, new_text, report, enforce_ops=False):
    # The guard runs on a dry run too. A preview that prints a clean diff for a
    # write the commit would refuse is a misleading report.
    try:
        guard_write(args.file, get_registry(args),
                    allow_frozen=getattr(args, 'allow_frozen', False))
    except tasklists.TaskListError as e:
        tasklists.fail(e)
    diff = ''.join(difflib.unified_diff(
        original.splitlines(keepends=True), new_text.splitlines(keepends=True),
        fromfile='TASKS.md (before)', tofile='TASKS.md (after)', n=1))
    changed = new_text != original
    skipped = skipped_ops(report) if enforce_ops else []
    if not args.commit:
        print(diff if diff else '(no changes)')
        print('\n--- DRY RUN (no write). Re-run with --commit to apply. ---')
        payload = {'report': report, 'committed': False}
        if enforce_ops:
            # A dry run reports the same two facts the commit path does, so a
            # preview cannot look healthier than the write it is previewing.
            payload['would_change'] = changed
            if skipped:
                payload['error'] = 'ops-skipped'
                payload['skipped'] = skipped
                payload['message'] = _skipped_message(skipped, committed=False)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if skipped:
            sys.exit(OPS_SKIPPED_EXIT)
        return
    # Compare-on-write guard. `original` is the text this command read at
    # the start of its run. If the file on disk no longer matches it, a concurrent
    # writer landed in between, and writing `new_text` now would silently revert
    # that writer's edit with no error and no artifact left behind. Fail closed and
    # let the caller re-run: a stale task line is visible, a clobbered one is not.
    #
    # Deliberately NOT an internal retry. This engine's contract is one predictable
    # dry-run/commit pass per invocation; retrying inside a single call would hide
    # the conflict from whoever invoked it.
    try:
        with open(args.file, encoding='utf-8') as f:
            on_disk = f.read()
    except FileNotFoundError:
        on_disk = None
    if on_disk != original:
        print(json.dumps({
            'error': 'write-conflict',
            'message': ('%s changed on disk since this command read it. Nothing was '
                        'written. Re-read the file and re-run; do not force this.'
                        % args.file),
            'committed': False,
        }, ensure_ascii=False, indent=2), file=sys.stderr)
        sys.exit(2)

    if enforce_ops and not changed:
        # Nothing changed, so nothing is written: no `.bak`, no temp file, no
        # replace. The old code fell straight through to the write below and
        # left a backup of a file it had not touched, which is how a batch that
        # applied nothing still looked like a commit.
        print(json.dumps({
            'report': report,
            'committed': False,
            'error': 'no-op-applied',
            'skipped': skipped,
            'message': _skipped_message(skipped, committed=False),
        }, ensure_ascii=False, indent=2))
        sys.exit(OPS_SKIPPED_EXIT)

    bak = args.file + '.bak'
    with open(bak, 'w', encoding='utf-8') as f:
        f.write(original)
    d = os.path.dirname(os.path.abspath(args.file))
    # Keep temporary writes in one grantable directory on the list's filesystem.
    d = os.path.join(d, '.task-tmp')
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix='.tmp')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(new_text)
    os.replace(tmp, args.file)
    out = {'report': report, 'committed': True, 'backup': bak}
    if skipped:
        out['error'] = 'ops-skipped'
        out['skipped'] = skipped
        out['message'] = _skipped_message(skipped, committed=True)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if skipped:
        sys.exit(OPS_SKIPPED_EXIT)


def _skipped_message(skipped, committed):
    if not skipped:
        return ('no operation in this batch changed a line, so nothing was '
                'written. Check the ops file: the key for an operation is '
                '"action", and an op is skipped when its id resolves nowhere or '
                'its action is unknown.')
    named = ', '.join('%s: %s' % (r.get('id'), r.get('status')) for r in skipped)
    head = ('%d of the operations in this batch were skipped and are NOT applied'
            % len(skipped))
    tail = (' The rest of the batch was written.' if committed
            else ' Nothing was written.')
    return ('%s (%s).%s The key for an operation is "action"; "op" is not read.'
            % (head, named, tail))


def cmd_apply(args, today):
    reg = get_registry(args)
    try:
        target, routing = route_apply(args, reg)
    except tasklists.TaskListError as e:
        tasklists.fail(e)
        return
    args.file = target
    with file_lock(args.file):
        with open(args.file, encoding='utf-8') as f:
            original = f.read()
        lines = original.split('\n')
        tasks = parse_open(lines, args.stale_days, today, args.stale_days_p1)
        with open(args.ops, encoding='utf-8') as f:
            ops = json.load(f)
        try:
            new_lines, report = apply_ops(lines, ops, tasks, today)
        except OpConflict as e:
            # Fail closed, inside the lock, before any write. Same exit code and
            # shape as the write-conflict guard, because it is the same class of
            # failure: an edit that would be silently lost.
            print(json.dumps({'error': 'op-conflict', 'message': str(e),
                              'committed': False}, ensure_ascii=False, indent=2),
                  file=sys.stderr)
            sys.exit(2)
        if routing:
            report = list(report) + [{'routing': routing}]
        write_result(args, original, '\n'.join(new_lines), report,
                     enforce_ops=True)


def read_counter(lines):
    for l in lines:
        m = COUNTER_RE.match(l)
        if m:
            return int(m.group(1))
    return None


def set_counter(lines, value):
    """Update the `<!-- next-task-id: N -->` comment, inserting after the
    `# Tasks` title (or at the top) if missing."""
    for i, l in enumerate(lines):
        if COUNTER_RE.match(l):
            lines[i] = f'<!-- next-task-id: {value} -->'
            return lines
    at = next((i + 1 for i, l in enumerate(lines) if re.match(r'^#\s+', l)), 0)
    return lines[:at] + [f'<!-- next-task-id: {value} -->'] + lines[at:]


def cmd_assign_ids(args, today):
    """Idempotent: give every open task without a tid the next free id.

    Ids come from `claim_id.claim()`, which reserves each number by creating
    `<claims-dir>/<prefix><n>.claimed` with O_CREAT|O_EXCL. That is atomic on a
    shared filesystem, so two sessions running this concurrently cannot be
    handed the same number, as they could if both read the counter before
    either wrote.

    The `<!-- next-task-id: N -->` counter is still read as a starting hint and
    still bumped, but it is no longer what guarantees uniqueness. The claim files
    are. Never reuses an id.

    `--claims-dir` is always passed explicitly by this function rather than left
    to `claim_id`'s own CLI default, so a test or scratch run cannot silently
    consume production id-space. Defaults to the real directory when omitted, so
    ordinary use is unchanged.
    """
    with file_lock(args.file):
        _assign_ids_locked(args, today)


def _assign_ids_locked(args, today):
    reg = get_registry(args)
    prefix = tasklists.read_list_prefix(args.file)
    claims_dir = claim_id.resolve_claims_dir(args.file, args.claims_dir)
    try:
        lane = tasklists.require_lane(reg, prefix, claim_id.__file__)
    except tasklists.TaskListError as e:
        tasklists.fail(e)
        return
    with open(args.file, encoding='utf-8') as f:
        original = f.read()
    lines = original.split('\n')
    tasks = parse_open(lines, 60, today)
    counter = read_counter(lines)
    # starting_candidate scans LINE-LEADING ids across the WHOLE file, including
    # closed lines, which is stricter than the open-sections-only max this used to
    # compute and is what stops a Done-section id from being handed out a second
    # time. Line-leading only is deliberate: an id-shaped string quoted inside
    # prose must not push the counter forward.
    nxt = claim_id.starting_candidate(args.file, prefix)
    assigned = []
    for t in tasks:
        if t['tid']:
            continue
        if args.commit:
            # Claim for real. Only on commit: a claim file is permanent, so
            # claiming during a preview would consume id-space for a run that
            # writes nothing, breaking this engine's never-edits-without---commit
            # contract.
            n = claim_id.claim(claims_dir, nxt, prefix, lane)
        else:
            # Preview only. Nothing is reserved, so a concurrent claim can take
            # this number before any real commit happens. The ids shown by a dry
            # run are therefore indicative, not promised; the report says so.
            n = nxt
        tid = tasklists.format_id(prefix, n)
        lines[t['lineno']] = re.sub(r'^- \[ \]\s*', '- [ ] %s ' % tid,
                                    lines[t['lineno']], count=1)
        assigned.append({'id': t['id'], 'tid': tid, 'text': t['text']})
        nxt = tasklists.next_in_lane(n, lane)
    new_lines = set_counter(lines, nxt) if assigned or counter != nxt else lines
    report = {'assigned': assigned, 'nextId': nxt, 'claimsDir': claims_dir,
              'prefix': prefix}
    if not args.commit and assigned:
        report['note'] = ('preview only: these ids are NOT claimed and may differ '
                          'on commit')
    write_result(args, original, '\n'.join(new_lines), [report])


# The floor sits in the data root's `.task-ids/`. An optional resolver names
# the data root; with no resolver in reach it is the engine's own tree, three
# levels up. With no floor file the unclaimed-id check is off.
CLAIM_FLOOR_FILE = os.path.join(
    tasklists.data_root() or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          '..', '..', '..'),
    '.task-ids', 'CLAIM-FLOOR')


def read_claim_floor(path=None):
    """Lowest id the unclaimed-id check will judge. None means "do not check".

    The check needs a floor when a list holds ids created before claim files
    existed. Those ids are deliberately NOT backfilled: a retro-claim would
    assert a history that did not happen. Reporting them on every lint run
    would bury the one new violation that matters under many old ones, so they
    sit below the floor.

    THE FLOOR NEVER ADVANCES ON ITS OWN, and that is the whole design. A check
    that moves its own watermark past an unresolved finding consumes the drift
    signal it exists to raise and reports clean forever after. So a bypassed id
    above the floor stays a violation until someone claims it.

    A missing floor file disables the check rather than failing the run, so an
    install that never seeded one is unaffected. That is a
    real blind spot, not a clean result, which is why cmd_lint echoes the scope
    on every run instead of letting silence read as compliance.
    """
    try:
        with open(path or CLAIM_FLOOR_FILE, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    return int(line)
    except (FileNotFoundError, ValueError):
        return None
    return None


def scan_unclaimed(holders, claims_dir, floor):
    """Task-line ids at or above `floor` with no claim file. Sorted by id.

    Filesystem truth, not text truth. It asks whether `<id>.claimed` exists and
    reads nothing on the line itself, so unlike the project-code check (whose
    `CODE_RE.search(l)` fallback can be satisfied by prose that merely mentions
    a project code) no amount of annotation can clear it.
    The only ways it goes quiet are the honest one, someone claims the id, and
    the two disclosed ones: the id sits below the floor, or no floor is set.
    """
    out = []
    for tid, spots in holders.items():
        parts = tasklists.split_id(tid)
        if not parts:
            continue
        # The floor is a watermark for unprefixed `T` ids and judges nothing
        # else. A prefixed id is always minted by a claim, so every one of them
        # is in scope from its first day.
        if parts[0] == 'T' and parts[1] < floor:
            continue
        if not os.path.exists(os.path.join(claims_dir, '%s.claimed' % tid)):
            out.append((tid, spots[0]))
    return sorted(out, key=lambda x: tasklists.id_sort_key(x[0]))


def scan_all_tids(lines):
    """Every id on a task line, open or closed, across the whole file.

    Returns (holders, companions): holders maps tid -> [lineno, ...] in document
    order; companions is a list of (base_tid, suffix, lineno) for archival
    `AB58-OLD` style lines.

    Deliberately not section-scoped and deliberately not open-only. An id is
    owned by whichever line claimed it first, in any section, in either checkbox
    state, forever. Scoping this to `## Active` or to `- [ ]` would hide a
    collision between an open line and a closed one.
    """
    holders, companions = {}, []
    for i, l in enumerate(lines):
        cm = COMPANION_TID_RE.match(l)
        if cm:
            companions.append((cm.group(1), cm.group(2), i))
            continue
        m = ANY_TID_RE.match(l)
        if m:
            holders.setdefault(m.group(1), []).append(i)
    return holders, companions


def cmd_lint(args, today):
    """Structural lint. Violations (exit 1): missing tid, duplicate tid, orphan
    archival companion, missing explicit inline date, missing inline project
    code. Warnings (exit 0): title longer than 100 chars.

    Scope differs by check, on purpose. Date and project-code checks are
    properties of an OPEN task and run over `parse_open`. Id uniqueness is a
    property of the whole FILE and runs over every task line in either checkbox
    state, because a closed line still owns its id.
    """
    with open(args.file, encoding='utf-8') as f:
        lines = f.read().split('\n')
    tasks = parse_open(lines, 60, today)
    if args.section:
        tasks = [t for t in tasks if t['section'] == args.section]
    violations, warnings = [], []

    # Id uniqueness: whole file, both checkbox states, companions excluded.
    # Skipped under --section, which narrows to one section and so cannot make a
    # sound uniqueness claim about the file.
    dupes = []
    unclaimed_scope = 'skipped (--section narrows the file)'
    if not args.section:
        holders, companions = scan_all_tids(lines)
        # Unclaimed ids: filesystem check, whole file, floor-gated. See
        # read_claim_floor for why the floor exists and why it never advances.
        floor_path = getattr(args, 'claim_floor_file', None) or CLAIM_FLOOR_FILE
        floor = read_claim_floor(floor_path)
        if floor is None:
            # Missing and unreadable are both UNCHECKED, but they need different
            # fixes, so the message says which one rather than always blaming a
            # missing file and sending the reader to look for one that is there.
            why = ('no CLAIM-FLOOR file at %s' % floor_path
                   if not os.path.exists(floor_path)
                   else 'CLAIM-FLOOR at %s has no readable integer' % floor_path)
            unclaimed_scope = 'skipped (%s) -- UNCHECKED, not clean' % why
        else:
            claims_dir = claim_id.resolve_claims_dir(args.file, args.claims_dir)
            unclaimed_scope = 'ids >= T%d (and every prefixed id), against %s' % (floor, claims_dir)
            for tid, ln in scan_unclaimed(holders, claims_dir, floor):
                dupes.append({
                    'tid': tid, 'id': None, 'section': '', 'lineno': ln + 1,
                    'title': clip(strip_md(lines[ln]).lstrip('- [x] '), 90),
                    'problems': ['unclaimed id: no %s.claimed in %s. Allocate with '
                                 'claim_id.py, never the counter alone' % (tid, claims_dir)]})
        for tid, spots in holders.items():
            for extra in spots[1:]:
                dupes.append({
                    'tid': tid, 'id': None, 'section': '', 'lineno': extra + 1,
                    'title': clip(strip_md(lines[extra]).lstrip('- [x] '), 90),
                    'problems': ['duplicate tid (also line %d)' % (spots[0] + 1)]})
        for base, suffix, ln in companions:
            if base not in holders:
                dupes.append({
                    'tid': '%s-%s' % (base, suffix), 'id': None, 'section': '',
                    'lineno': ln + 1,
                    'title': clip(strip_md(lines[ln]).lstrip('- [x] '), 90),
                    'problems': ['orphan companion: no %s line to attach to' % base]})

    for t in tasks:
        probs = []
        if not t['tid']:
            probs.append('missing tid')
        if not t['explicit_date']:
            probs.append('no explicit (YYYY-MM-DD) date on the line')
        if not t['inline_code']:
            probs.append('no inline project code')
        if probs:
            violations.append({'tid': t['tid'], 'id': t['id'], 'section': t['section'],
                               'lineno': t['lineno'] + 1, 'title': t['title'], 'problems': probs})
        if len(t['title']) > 100:
            warnings.append({'tid': t['tid'], 'id': t['id'], 'section': t['section'],
                             'lineno': t['lineno'] + 1, 'problem': 'title over 100 chars'})
    # A level-two heading that is not one of the five TASKS.md carries is a
    # clobbered task line, not a heading. Anchoring SECTIONS stops it mis-filing
    # tasks, but section_bounds() still ends the preceding span at ANY `^##` line,
    # so the tasks after a corrupted one fall into no span and vanish from the
    # parse. This is the check that names the bad line instead.
    for i, line in enumerate(lines):
        if re.match(r'^##\s+', line) and not KNOWN_HEADING_RE.match(line):
            violations.append({
                'tid': '', 'id': None, 'section': '', 'lineno': i + 1,
                'title': clip(strip_md(line), 90),
                'problems': ['corrupted heading: a `## ` line that is not one of '
                             'Active / Waiting On / Upcoming / Private / Done. '
                             'Almost certainly a task line that lost its head to a '
                             'concurrent write. Every task line after it, up to the '
                             'next heading, is dropped from the parse']})

    violations = sorted(violations + dupes, key=lambda v: v['lineno'])
    print(json.dumps({'checked': len(tasks), 'id_scan_scope':
                      'open-only (--section)' if args.section else 'whole file, open + closed',
                      'unclaimed_scope': unclaimed_scope,
                      'violations': violations, 'warnings': warnings},
                     ensure_ascii=False, indent=2))
    sys.exit(1 if violations else 0)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    pl = sub.add_parser('list')
    pl.add_argument('--file', required=True)
    pl.add_argument('--stale-days', type=int, default=60)
    pl.add_argument('--stale-days-p1', type=int, default=STALE_DAYS_P1)
    pl.add_argument('--all', action='store_true')
    pl.add_argument('--section', choices=['active', 'waiting', 'upcoming', 'unclassified'])
    pl.add_argument('--today')
    pa = sub.add_parser('apply')
    pa.add_argument('--file', required=True)
    pa.add_argument('--ops', required=True)
    pa.add_argument('--stale-days', type=int, default=60)
    pa.add_argument('--stale-days-p1', type=int, default=STALE_DAYS_P1)
    pa.add_argument('--today')
    pa.add_argument('--commit', action='store_true')
    pa.add_argument('--registry', default=None)
    pad = sub.add_parser('add', help='create a task: claim an id and write the line')
    pad.add_argument('--tag', default='', help='the [Tag] that routes the task')
    pad.add_argument('--text', required=True, help='the task body, with no id')
    pad.add_argument('--section', choices=['active', 'waiting', 'upcoming'], default='active')
    pad.add_argument('--list', default=None, help='write this list, bypassing the router')
    pad.add_argument('--claims-dir', default=None)
    pad.add_argument('--today')
    pad.add_argument('--commit', action='store_true')
    pad.add_argument('--registry', default=None)
    pf = sub.add_parser('find', help='resolve an id to its list, and say whether that list is writable')
    pf.add_argument('id')
    pf.add_argument('--list', default=None, help='look here first')
    pf.add_argument('--today')
    pf.add_argument('--registry', default=None)
    pv = sub.add_parser('version', help='the engine version stamp and a sha256 per engine file')
    pv.add_argument('--today')
    pv.add_argument('--registry', default=None)
    prp = sub.add_parser('rollup-path', help='the file a reader opens for all open tasks')
    prp.add_argument('--today')
    prp.add_argument('--registry', default=None)
    pib = sub.add_parser('insert-block', help='insert lines under a heading, under the list lock')
    pib.add_argument('--list', required=True)
    pib.add_argument('--header', required=True)
    pib.add_argument('--lines-file', required=True)
    pib.add_argument('--require-header', action='store_true')
    pib.add_argument('--today')
    pib.add_argument('--commit', action='store_true')
    pib.add_argument('--registry', default=None)
    pi = sub.add_parser('assign-ids')
    pi.add_argument('--file', required=True)
    pi.add_argument('--today')
    pi.add_argument('--commit', action='store_true')
    pi.add_argument('--registry', default=None)
    # Point this at a scratch directory for any test or dry run against a copy of
    # TASKS.md. Claim files are permanent by design, so a run against a scratch
    # file with the production claims dir would still burn real id-space.
    # Default None, resolved to `.task-ids/` beside the list, so every list
    # uses its own folder's claims.
    pi.add_argument('--claims-dir', default=None)
    pn = sub.add_parser('lint')
    pn.add_argument('--file', required=True)
    pn.add_argument('--section', choices=['active', 'waiting', 'upcoming', 'unclassified'])
    pn.add_argument('--today')
    # Same scratch-isolation reasoning as assign-ids above: a lint run against a
    # copy of TASKS.md must be able to point at a scratch claims dir, or every
    # test id reads as unclaimed against production.
    pn.add_argument('--claims-dir', default=None)
    pn.add_argument('--claim-floor-file', default=None)
    pn.add_argument('--registry', default=None)
    args = ap.parse_args()
    today = datetime.date.fromisoformat(args.today) if args.today else datetime.date.today()
    # Project codes come from the registry in reach of the list this run reads or
    # writes, so the data folder's own manifest decides what a project code is.
    # A registry that will not parse is left to the write path, which refuses
    # loudly; the parser keeps the vocabulary it had.
    try:
        configure_codes(get_registry(args))
    except Exception:
        pass
    {'list': cmd_list, 'apply': cmd_apply, 'add': cmd_add, 'find': cmd_find,
     'rollup-path': cmd_rollup_path, 'insert-block': cmd_insert_block,
     'version': cmd_version,
     'assign-ids': cmd_assign_ids, 'lint': cmd_lint}[args.cmd](args, today)


if __name__ == '__main__':
    main()
