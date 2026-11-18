#!/usr/bin/env python3
"""write_project.py: write a project's brief, status note and file history from facts read out of its files.

    write_project.py --where-json JSON_OR_PATH --facts FILE [--preview]
    write_project.py --where-json JSON_OR_PATH --client-folder --facts FILE [--preview]
    write_project.py --where-json JSON_OR_PATH --batch DIR --batch-id ID
    write_project.py --where-json JSON_OR_PATH --confirm FOLDER

--where-json is the output of setup.sh --where, as text, a file path, or - for standard input,
the same contract as adopt.py and inventory.py.

The facts file is JSON: {folder, name, facts: {key: {value, file, where}}, people: [{name, role,
email, file, where}], email_domains: []}. folder is the project folder; name is <Project>, or
<Client>/<Job> for a job inside a client folder, and is only checked against the folder's own
place (a disagreement is a refusal). A fact or a person with no source file, or whose file is not
in the folder, is dropped and listed in `dropped`. A facts file of the wrong shape is a refusal,
never an empty project.

--facts writes three files into the folder: brief.md, CONTINUITY.md and
updates/<YYYY-MM-DD>_history-from-files.md (-2, -3 when the name is taken). brief.md and
CONTINUITY.md open with the line `> Drafted from your files on <date>. Not yet checked by you.`
and follow project-setup's templates. A field the facts do not answer says "Not in the files
yet". Existing material (file counts, dates, plan sets with page counts) and the history come
from the script's own walk of the folder, leaving out hidden files, .DS_Store and Merlin's own
files. An existing brief.md or CONTINUITY.md is kept untouched and listed in `kept`. updates/ is
made only when missing. Every file goes through a temporary file in the same folder, is linked
into place only when the name is still free, then read back and compared. Then the project is
registered through adopt.py: the folder's own name for <business>/<Name>, <Client>/<Job> for a job
in a client folder. Already registered counts as ok ("already"). A folder deeper than two levels,
or two levels down outside a client folder, is written but not registered (registered: false and
a reason). Prints {ok, folder, project, wrote, kept, registered, reason, dropped}.

--preview prints the same plus `files`, the full text of each file it would write, and writes
nothing at all.

--client-folder --facts writes <folder>/CLAUDE.md from project-setup's client template, with the
marker line `<!-- merlin:client-folder -->` and an `Email domains:` line. A client folder sits
directly in the business folder and gets no brief or CONTINUITY.md. A CLAUDE.md without the
marker is refused, nothing is written, and `question` holds the plain question to ask. A
CLAUDE.md with the marker is kept and only reported: `email_domains_line` is present or missing.

--batch DIR --batch-id ID writes one facts file per folder (DIR/*.json), each on its own. A
malformed facts file fails that folder only. The batch ledger (inventory.py's) is marked
written, registered or failed with a reason. Prints {ok, batch, ledger, results, failed}.

--confirm FOLDER removes the "Drafted from your files" first line, and the blank line after it,
from brief.md and CONTINUITY.md, through a temporary file, then reads each back. Nothing else
changes. A file without the line is left as it is.

Refused, with nothing written: a folder that is missing, not a folder, or a link; a folder
outside the business folder (copy it in first with project-setup's copy_in.py); the business
folder, the HQ folder or anything inside it, or the folders that hold Merlin's own files; a hidden folder; a
client folder given to --facts; a folder this account cannot write; an unfinished move of
Merlin's own files into the HQ folder; a layout other than hq or flat (adopt.py's check_where).

Every exit prints one JSON object on standard output, an unexpected error included. Exit 0 ok,
1 refusal, 2 usage or crash. Standard library only.
"""

import sys

# Set before the sibling scripts are loaded, so no run leaves bytecode beside them.
sys.dont_write_bytecode = True

import argparse  # noqa: E402
import contextlib  # noqa: E402
import glob  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import tempfile  # noqa: E402
from datetime import datetime  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    import inventory as I  # noqa: E402
except SystemExit as e:  # inventory says so, as JSON, when project-setup's scripts are not beside it
    print(e.code if isinstance(e.code, str) else json.dumps({'ok': False, 'error': 'crash: inventory.py did not load'}))
    sys.exit(2)
except Exception as e:  # noqa: BLE001 every exit prints one JSON object
    print(json.dumps({'ok': False, 'error': 'crash: inventory.py did not load: %s: %s' % (type(e).__name__, e),
                      'crash': True}))
    sys.exit(2)

C = I.C  # copy_in.py, as inventory loaded it
A = I.A  # adopt.py, as copy_in loaded it, so Refusal is one class
Refusal = A.Refusal

NIF = 'Not in the files yet.'
MARK_RE = re.compile(r'\A> Drafted from your files on \d{4}-\d{2}-\d{2}\. Not yet checked by you\.(\r?\n)(\r?\n)?')
HISTORY_MAX = 300
PLANS_MAX = 20
FACTS_BYTES_MAX = 2 * 1024 * 1024
VALUE_MAX = 400
WHERE_MAX = 80
DOMAIN_RE = re.compile(r'(?=.{3,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+')
EMAIL_RE = re.compile(r'[^@\s]+@[^@\s]+\.[^@\s]+')
KEY_RE = re.compile(r'[a-z][a-z0-9_]{0,40}')
EMAIL_LINE_RE = re.compile(r'^Email domains:', re.M)
MERLIN_TOP = ('brief.md', 'CONTINUITY.md', 'CLAUDE.md', 'AGENTS.md')
HISTORY_RE = re.compile(r'^updates/\d{4}-\d{2}-\d{2}_history-from-files(-\d+)?\.md$')

LABEL = {'what_it_is': 'What it is', 'address': 'Address', 'client': 'Client', 'builder': 'Builder',
         'owner': 'Owner', 'architect': 'Architect', 'project_manager': 'Project manager', 'scope': 'Scope',
         'plan_date': 'Plan date', 'start': 'Start', 'due': 'Due'}
DOES = ('address', 'scope', 'plan_date', 'due')
PEOPLE = ('builder', 'owner', 'architect', 'project_manager')
KNOWN = set(LABEL)
TYPE_WORDS = {'pdf': ('PDF', 'PDFs'), 'image': ('image', 'images'), 'doc': ('document', 'documents'),
              'sheet': ('spreadsheet', 'spreadsheets'), 'email': ('email', 'emails'),
              'text': ('text file', 'text files'), 'other': ('other file', 'other files')}
KIND_RES = (('contract', re.compile(r'(?<![a-z])(contract|agreement)s?(?![a-z])', re.I)),
            ('quote', re.compile(r'(?<![a-z])(quote|quotation)s?(?![a-z])', re.I)),
            ('estimate', re.compile(r'(?<![a-z])estimates?(?![a-z])', re.I)))


class Usage(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Usage(message)


class Failed(Exception):
    """A write that did not land as intended. The JSON names the file."""


def emit(obj, code=0):
    print(json.dumps(obj, indent=2, ensure_ascii=False))
    return code


def today():
    return datetime.now().strftime('%Y-%m-%d')


# ------------------------------------------------------------------ where a folder may be

def place(w, folder_arg, business, hq):
    """(canonical folder, its parts below the business folder) or a Refusal. Checks the raw path for a
    link before resolving it, then the resolved path against every folder Merlin keeps to itself."""
    if not isinstance(folder_arg, str) or not folder_arg.strip():
        raise Refusal('No folder was given. Nothing was written.')
    raw = os.path.abspath(os.path.expanduser(folder_arg.strip()))
    if len(raw) > 1:
        raw = raw.rstrip(os.sep)
    if os.path.islink(raw):
        raise Refusal('%s is a link to another folder, not a folder. Give the folder itself. Nothing was written.' % raw)
    if not os.path.isdir(raw):
        raise Refusal('%s is not a folder I can find. Nothing was written.' % raw)
    src = C.canon(raw)
    biz = C.canon(business)
    if src == biz:
        raise Refusal('%s is the business folder itself, not one project. Nothing was written.' % src)
    if hq and C.within(src, C.canon(hq)):
        raise Refusal('%s is Merlin\'s HQ folder or inside it, where Merlin keeps its own files, not a project. '
                      'Nothing was written.' % src)
    for k in A.DATA_KEYS:
        if w.get(k) and C.within(src, C.canon(w[k])):
            raise Refusal('%s holds Merlin\'s own files, not a project. Nothing was written.' % src)
    if not C.within(src, biz):
        raise Refusal('%s is outside the business folder %s. Copy it in first with project-setup\'s copy step '
                      '(copy_in.py), then set up the copy. Nothing was written.' % (src, biz))
    parts = os.path.relpath(src, biz).split(os.sep)
    if any(p.startswith('.') for p in parts):
        raise Refusal('%s is a hidden folder or inside one; Merlin does not set up hidden folders. Nothing was '
                      'written.' % src)
    return src, parts


def writable(w, src, business):
    blocked = A.move_refusal(w, business)
    if blocked:
        raise blocked
    if not os.access(src, os.W_OK | os.X_OK):
        raise Refusal('This computer account cannot write to %s. Nothing was written.' % src)


def registration(business, src, parts):
    """(name to register or None, project name, reason when not registered)."""
    if len(parts) == 1:
        return parts[0], parts[0], None
    if len(parts) == 2:
        if A.is_client_folder(os.path.join(C.canon(business), parts[0])):
            return '/'.join(parts), '-'.join(parts), None
        return None, parts[-1], ('%s is inside %s, which is not a client folder (its CLAUDE.md has no line %s), so '
                                 'it was not added to the project list.' % (parts[1], parts[0], A.CLIENT_MARK))
    return None, parts[-1], ('%s is more than two levels inside the business folder, so it was not added to the '
                             'project list. Merlin projects are a folder in the business folder, or a job folder '
                             'inside a client folder.' % src)


def name_agrees(name, parts, regname):
    want = {'/'.join(parts)} if regname else {'/'.join(parts), parts[-1]}
    if name.strip() not in want:
        raise Refusal('The facts file names the project %r, but the folder says %s. Nothing was written.'
                      % (name, ' or '.join(sorted(repr(x) for x in want))))


def already(manifest, src, regname, pname):
    for r in A.registered(A.read_lines(manifest), manifest):
        if r['name'] == pname or (r['resolved'] and r['resolved'] == C.canon(src)):
            return True
    return False


# ------------------------------------------------------------------ the facts file

def load_facts(path):
    """The facts file as parsed JSON. A refusal when it cannot be read or is not JSON."""
    try:
        size = os.path.getsize(path)
    except OSError:
        raise Refusal('The facts file %s is not there. Nothing was written.' % path)
    if size > FACTS_BYTES_MAX:
        raise Refusal('The facts file %s is over 2 MB, too big to be a facts file. Nothing was written.' % path)
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (ValueError, UnicodeDecodeError) as e:
        raise Refusal('The facts file %s is not JSON: %s. Nothing was written.' % (path, e))


def clean(s, cap):
    s = ' '.join(str(s).split())
    return s if len(s) <= cap else s[:cap - 3].rstrip() + '...'


# Two sources that are not files: "chat" (the user said it in this chat, answering a gap) and
# "email" (the email a plan set came in, for a job made from the daily briefing). Kept as given and
# cited in words, never as a path.
SPOKEN = {'chat': 'you, in the chat', 'email': 'the email'}


def source(folder, f):
    """The fact's file as a path relative to FOLDER, "chat" or "email", or None when it is none of
    these."""
    if not isinstance(f, str) or not f.strip():
        return None
    f = f.strip()
    if f.lower() in SPOKEN:
        return f.lower()
    p = f if os.path.isabs(f) else os.path.join(folder, f)
    p = os.path.normpath(p)
    if not C.within(C.canon(p), folder) and not C.within(p, folder):
        return None
    if not os.path.lexists(p):
        return None
    rel = os.path.relpath(p, folder)
    return None if rel.startswith('..') else rel


def scalar(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str) and v.strip():
        return v
    return None


def normalize(raw, folder):
    """(facts, people, domains, dropped) from a parsed facts object for FOLDER (canonical). A Refusal
    when the object has the wrong shape; a single fact or person without a source is dropped."""
    facts, people, domains, dropped = {}, [], [], []
    rf = raw.get('facts', {})
    if not isinstance(rf, dict):
        raise Refusal('The facts file\'s "facts" is not an object of named facts. Nothing was written.')
    for key, f in rf.items():
        if not isinstance(key, str) or not KEY_RE.fullmatch(key):
            dropped.append({'fact': str(key), 'why': 'not a fact name'})
            continue
        if not isinstance(f, dict):
            dropped.append({'fact': key, 'why': 'no source file'})
            continue
        v = scalar(f.get('value'))
        if v is None:
            dropped.append({'fact': key, 'why': 'no value'})
            continue
        rel = source(folder, f.get('file'))
        if rel is None:
            dropped.append({'fact': key, 'why': 'no source file in the folder'})
            continue
        wh = scalar(f.get('where'))
        facts[key] = {'value': clean(v, VALUE_MAX), 'file': rel, 'where': clean(wh, WHERE_MAX) if wh else None}
    rp = raw.get('people', [])
    if not isinstance(rp, list):
        raise Refusal('The facts file\'s "people" is not a list. Nothing was written.')
    for i, p in enumerate(rp):
        if not isinstance(p, dict) or scalar(p.get('name')) is None:
            dropped.append({'person': i, 'why': 'no name'})
            continue
        rel = source(folder, p.get('file'))
        if rel is None:
            dropped.append({'person': clean(p['name'], 80), 'why': 'no source file in the folder'})
            continue
        e = scalar(p.get('email'))
        person = {'name': clean(p['name'], 120), 'role': clean(p['role'], 120) if scalar(p.get('role')) else None,
                  'email': clean(e, 200) if e and EMAIL_RE.fullmatch(e.strip()) else None, 'file': rel,
                  'where': clean(p['where'], WHERE_MAX) if scalar(p.get('where')) else None}
        people.append(person)
    rd = raw.get('email_domains', [])
    if not isinstance(rd, list):
        raise Refusal('The facts file\'s "email_domains" is not a list. Nothing was written.')
    for d in rd:
        d2 = d.strip().lower().lstrip('@') if isinstance(d, str) else ''
        if DOMAIN_RE.fullmatch(d2):
            if d2 not in domains:
                domains.append(d2)
        else:
            dropped.append({'email_domain': str(d), 'why': 'not a domain'})
    return facts, people, domains, dropped


def shape(raw):
    """folder and name from a parsed facts object, or a Refusal for the wrong shape."""
    if not isinstance(raw, dict):
        raise Refusal('The facts file is not a JSON object. Nothing was written.')
    for k in ('folder', 'name'):
        if not isinstance(raw.get(k), str) or not raw[k].strip():
            raise Refusal('The facts file has no "%s". Nothing was written.' % k)
    return raw['folder'], raw['name']


# ------------------------------------------------------------------ the folder's own files

def walk(src):
    """[(rel, size, mtime)] for the folder's files, newest first, leaving out hidden files, .DS_Store,
    links and Merlin's own files."""
    s = C.survey(src)
    out = []
    for rel, size in s['files']:
        relp = rel.replace(os.sep, '/')
        if any(x.startswith('.') for x in relp.split('/')):
            continue
        if relp in MERLIN_TOP or HISTORY_RE.match(relp):
            continue
        try:
            t = os.lstat(os.path.join(src, rel)).st_mtime
        except OSError:
            continue
        out.append((relp, size, t))
    out.sort(key=lambda x: (-x[2], x[0].lower()))
    return out


def label(rel, size):
    r = I.rank(rel, size)
    if r and r[1] == 'plan set':
        return 'plan set'
    base = os.path.splitext(os.path.basename(rel))[0]
    for kind, rx in KIND_RES:
        if rx.search(base):
            return kind
    return None


def day(t):
    return datetime.fromtimestamp(t).strftime('%Y-%m-%d')


def code(s):
    return '`` %s ``' % s if '`' in s else '`%s`' % s


def history_name(src, d):
    base = '%s_history-from-files' % d
    n = 1
    while True:
        name = '%s.md' % base if n == 1 else '%s-%d.md' % (base, n)
        if not os.path.lexists(os.path.join(src, 'updates', name)):
            return 'updates/' + name
        n += 1


# ------------------------------------------------------------------ the three files

def cite(rec):
    return SPOKEN[rec['file']] if rec['file'] in SPOKEN else code(rec['file'])


def fact_line(f):
    where = (', %s' % f['where']) if f.get('where') else ''
    return '%s (from %s%s)' % (f['value'], cite(f), where)


def person_line(p):
    bits = [p['name']] + [x for x in (p.get('role'), p.get('email')) if x]
    where = (', %s' % p['where']) if p.get('where') else ''
    return '- %s (from %s%s)' % (', '.join(bits), cite(p), where)


def materials(files, src, hist):
    counts = {}
    for rel, size, _ in files:
        t = I.ftype(rel)
        counts[t] = counts.get(t, 0) + 1
    lines = []
    if not files:
        return ['No files of the user\'s own in the folder yet.'], 0
    kinds = ', '.join('%d %s' % (counts[t], TYPE_WORDS[t][0 if counts[t] == 1 else 1])
                      for t in ('pdf', 'image', 'doc', 'sheet', 'email', 'text', 'other') if counts.get(t))
    lines.append('- %d files in this folder (%s). Oldest file %s, newest %s.'
                 % (len(files), kinds, day(min(f[2] for f in files)), day(max(f[2] for f in files))))
    plans = sorted((f for f in files if label(f[0], f[1]) == 'plan set'), key=lambda f: f[0].lower())
    if plans:
        lines.append('- Plan sets:')
        for rel, size, _ in plans[:PLANS_MAX]:
            pages = I.pdf_pages(os.path.join(src, rel), size) if I.ftype(rel) == 'pdf' else None
            lines.append('  - %s%s' % (code(rel), ' (%d pages)' % pages if pages else ''))
        if len(plans) > PLANS_MAX:
            lines.append('  - and %d more' % (len(plans) - PLANS_MAX))
    else:
        lines.append('- Plan sets: none found by name.')
    lines.append('- Every file by date: %s' % code(hist))
    return lines, len(plans)


def build(project, src, facts, people, files, hist, d):
    """{rel: text} for brief.md, CONTINUITY.md and the history file, and the gap list."""
    mark = '> Drafted from your files on %s. Not yet checked by you.' % d
    gaps = []

    def val(key, label_text=None):
        f = facts.get(key)
        if f:
            return fact_line(f)
        gaps.append(label_text or LABEL[key])
        return NIF

    client_key = 'client' if 'client' in facts else 'builder'
    purpose = val('what_it_is', 'What it is')
    client = val(client_key, 'Client or builder')
    started = val('start', 'Start date')
    does = ['- %s: %s' % (LABEL[k], val(k)) for k in DOES]
    extra = sorted(k for k in facts if k not in KNOWN)
    does += ['- %s: %s' % (k.replace('_', ' ').capitalize(), fact_line(facts[k])) for k in extra]
    roles = []
    for k in PEOPLE:
        if k == 'builder':
            # The builder is the client line's gap already when neither is known; a named client with no
            # builder leaves the builder line out.
            if 'builder' in facts:
                roles.append('- Builder: %s' % fact_line(facts['builder']))
            elif client_key == 'builder':
                roles.append('- Builder: %s' % NIF)
            continue
        roles.append('- %s: %s' % (LABEL[k], val(k)))
    roles += [person_line(p) for p in people]
    mat, _ = materials(files, src, hist)

    brief = [mark, '', '# %s' % project, '',
             '**Purpose:** %s' % purpose,
             '**Client:** %s' % client,
             '**Status:** Active',
             '**Started:** %s' % started, '',
             '## What it does', *does, '',
             '## What it is NOT', NIF, '',
             '## How it is done today', NIF, '',
             '## Existing material', *mat, '',
             '## Tools', NIF, '',
             '## People', *roles, '',
             '## Rules to always follow', NIF, '',
             '## What success looks like', NIF, '']

    newest = day(files[0][2]) if files else 'none'
    loops = ['- [ ] Check the brief with the user']
    seen = set()
    for g in gaps:
        if g not in seen:
            seen.add(g)
            loops.append('- [ ] %s: not in the files yet' % g)
    cont = [mark, '', '# CONTINUITY.md · %s' % project, '',
            '**Last updated:** %s (project set up from the files)' % d, '',
            '## Current state',
            'Set up from the files on %s: %d files, newest file %s. Brief drafted from the files and not yet '
            'checked by the user.' % (d, len(files), newest), '',
            '## Open loops', *loops, '',
            '## Risks and blockers', '',
            '## Best next move',
            'Check brief.md with the user.', '',
            '## Session log',
            '- %s: set up from the files. Drafted brief.md and CONTINUITY.md, listed the files in %s.' % (d, code(hist)),
            '']

    hist_lines = ['# History from the files · %s' % project, '',
                  'Written %s from the folder\'s files, newest first, by the date each file last changed. '
                  '%d files. Hidden files and Merlin\'s own files are left out.' % (d, len(files)), '']
    for rel, size, t in files[:HISTORY_MAX]:
        kind = label(rel, size)
        hist_lines.append('- %s · %s%s' % (day(t), code(rel), ' · %s' % kind if kind else ''))
    if len(files) > HISTORY_MAX:
        hist_lines.append('- and %d more' % (len(files) - HISTORY_MAX))
    if not files:
        hist_lines.append('No files of the user\'s own in the folder yet.')
    hist_lines.append('')
    return {'brief.md': '\n'.join(brief), 'CONTINUITY.md': '\n'.join(cont), hist: '\n'.join(hist_lines)}, sorted(seen)


# ------------------------------------------------------------------ writing

def create_new(path, text):
    """Write TEXT to PATH only when PATH does not exist: a temporary file in the same folder, linked into
    place (never over an existing name), then read back and compared."""
    data = text.encode('utf-8')
    folder = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix='.merlin-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        mask = os.umask(0)
        os.umask(mask)
        os.chmod(tmp, 0o666 & ~mask)
        try:
            os.link(tmp, path)
        except FileExistsError:
            raise Failed('%s appeared while Merlin was writing, so Merlin left it alone.' % path)
        except OSError:
            if os.path.lexists(path):
                raise Failed('%s appeared while Merlin was writing, so Merlin left it alone.' % path)
            os.rename(tmp, path)
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)
    readback(path, data)


def replace_file(path, text):
    """Replace PATH, a file Merlin wrote, through a temporary file renamed into place, then read back."""
    data = text.encode('utf-8')
    folder = os.path.dirname(path)
    mode = os.stat(path).st_mode & 0o7777
    fd, tmp = tempfile.mkstemp(dir=folder, prefix='.merlin-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)
    readback(path, data)


def readback(path, data):
    with open(path, 'rb') as f:
        got = f.read()
    if got != data:
        raise Failed('%s was written but does not read back the same. Check the file.' % path)


def register(w, layout, business, manifest, hq, regname):
    """adopt.py's register, with its printed report kept out of this script's one JSON object."""
    with contextlib.redirect_stdout(io.StringIO()):
        A.register(w, layout, business, manifest, hq, regname)


# ------------------------------------------------------------------ --facts

def project(ctx, raw, preview):
    w, layout, business, manifest, hq = ctx
    folder_arg, name = shape(raw)
    src, parts = place(w, folder_arg, business, hq)
    if A.is_client_folder(src):
        raise Refusal('%s is a client folder: it holds the client\'s facts and one folder per job, and is never a '
                      'project itself. Set up each job folder inside it. Nothing was written.' % src)
    regname, pname, why_not = registration(business, src, parts)
    name_agrees(name, parts, regname)
    facts, people, domains, dropped = normalize(raw, src)
    writable(w, src, business)
    upd = os.path.join(src, 'updates')
    if os.path.lexists(upd) and (os.path.islink(upd) or not os.path.isdir(upd)):
        raise Refusal('%s is not a plain folder, so Merlin has nowhere to put the history. Nothing was written.' % upd)
    d = today()
    hist = history_name(src, d)
    files = walk(src)
    texts, gaps = build(pname, src, facts, people, files, hist, d)
    kept = [n for n in ('brief.md', 'CONTINUITY.md') if os.path.lexists(os.path.join(src, n))]
    plan = [n for n in ('brief.md', 'CONTINUITY.md', hist) if n not in kept]
    was = already(manifest, src, regname, pname) if regname else False
    out = {'ok': True, 'folder': src, 'project': pname, 'register_as': regname, 'wrote': [], 'kept': kept,
           'registered': False, 'gaps': gaps, 'dropped': dropped, 'email_domains': domains,
           'files_counted': len(files)}
    if why_not:
        out['reason'] = why_not
    if preview:
        out.update({'preview': True, 'wrote': plan, 'already_registered': was,
                    'files': {n: texts[n] for n in plan}})
        return out
    made_updates = False
    if not os.path.isdir(upd):
        os.mkdir(upd)
        made_updates = True
    try:
        for n in plan:
            create_new(os.path.join(src, n), texts[n])
            out['wrote'].append(n)
    except Failed as e:
        out.update({'ok': False, 'reason': str(e), 'made_updates_folder': made_updates})
        return out
    out['made_updates_folder'] = made_updates
    if regname:
        if was:
            out['registered'] = 'already'
        else:
            try:
                register(w, layout, business, manifest, hq, regname)
                out['registered'] = True
            except Refusal as e:
                out['reason'] = 'The files were written, but the project was not added to the project list: %s' % e
    return out


# ------------------------------------------------------------------ --client-folder

def client_text(cname, facts, people, domains):
    who = ['- %s: %s' % (LABEL.get(k, k.replace('_', ' ').capitalize()), fact_line(facts[k]))
           for k in sorted(facts, key=lambda k: (k not in LABEL, list(LABEL).index(k) if k in LABEL else 0, k))]
    call = [person_line(p) for p in people]
    return '\n'.join(['# %s' % cname, '<!-- %s -->' % A.CLIENT_MARK, '',
                      'Client folder. Each subfolder is one job and one Merlin project',
                      '(%s-<Job>). Open chats on a job folder, not here.' % cname, '',
                      ('Email domains: %s' % ', '.join(domains)) if domains else 'Email domains:', '',
                      '## Who they are', *(who or [NIF]), '',
                      '## How they pay', NIF, '',
                      '## Who to call', *(call or [NIF]), '',
                      '## Their standards and preferences', NIF, '',
                      '## Rules for every job', NIF, ''])


def client_folder(ctx, raw, preview):
    w, layout, business, manifest, hq = ctx
    folder_arg, name = shape(raw)
    src, parts = place(w, folder_arg, business, hq)
    if len(parts) != 1:
        raise Refusal('%s is not directly in the business folder. A client folder sits in the business folder, with '
                      'one folder per job inside it (two levels only). Nothing was written.' % src)
    cname = parts[0]
    if name.strip() != cname:
        raise Refusal('The facts file names the client %r, but the folder is %r. Nothing was written.' % (name, cname))
    for r in A.registered(A.read_lines(manifest), manifest):
        if r['name'] == cname or (r['resolved'] and r['resolved'] == src):
            raise Refusal('%s is already a project in Merlin\'s project list, so it cannot be a client folder too. '
                          'Nothing was written.' % cname)
    claude = os.path.join(src, 'CLAUDE.md')
    out = {'ok': True, 'folder': src, 'client': cname, 'wrote': [], 'kept': []}
    if os.path.lexists(claude):
        if os.path.islink(claude) or not os.path.isfile(claude):
            raise Refusal('%s is not a plain file, so Merlin left it alone. Nothing was written.' % claude)
        if A.is_client_folder(src):
            with open(claude, encoding='utf-8', errors='replace') as f:
                text = f.read()
            out.update({'kept': ['CLAUDE.md'],
                        'email_domains_line': 'present' if EMAIL_LINE_RE.search(text) else 'missing'})
            return out
        out = {'ok': False, 'folder': src, 'client': cname, 'wrote': [], 'kept': ['CLAUDE.md'],
               'reason': '%s already has a CLAUDE.md without the client-folder line, so Merlin did not change it. '
                         'Nothing was written.' % cname,
               'question': 'Is %s a client\'s folder, with one folder per job inside it? Its CLAUDE.md is yours, so '
                           'Merlin will not change it. If it is a client folder, the line <!-- %s --> goes near '
                           'the top of that file.' % (cname, A.CLIENT_MARK)}
        return out
    for n in ('brief.md', 'CONTINUITY.md'):
        if os.path.lexists(os.path.join(src, n)):
            raise Refusal('%s holds %s, so it reads as one project, not a client folder. A client folder has no '
                          'brief or CONTINUITY.md. Nothing was written.' % (cname, n))
    facts, people, domains, dropped = normalize(raw, src)
    writable(w, src, business)
    text = client_text(cname, facts, people, domains)
    out.update({'dropped': dropped, 'email_domains': domains, 'email_domains_line': 'present'})
    if preview:
        out.update({'preview': True, 'wrote': ['CLAUDE.md'], 'files': {'CLAUDE.md': text}})
        return out
    create_new(claude, text)
    out['wrote'] = ['CLAUDE.md']
    return out


# ------------------------------------------------------------------ --batch

def batch_folder_hint(path, text, raw, ledger_folders):
    """The ledger folder a facts file is about, for marking it failed, or None."""
    cands = []
    if isinstance(raw, dict) and isinstance(raw.get('folder'), str):
        cands.append(raw['folder'])
    elif text:
        m = re.search(r'"folder"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
        if m:
            try:
                cands.append(json.loads('"%s"' % m.group(1)))
            except ValueError:
                pass
    for c in cands:
        try:
            k = C.canon(c)
        except Exception:  # noqa: BLE001
            continue
        if k in ledger_folders:
            return k
    stem = os.path.splitext(os.path.basename(path))[0]
    hits = [f for f in ledger_folders if os.path.basename(f) == stem]
    return hits[0] if len(hits) == 1 else None


def batch(ctx, folder, batch_id):
    w = ctx[0]
    lpath = I.ledger_path(w, batch_id)
    if not os.path.isfile(lpath):
        raise Refusal('There is no batch called %s. Start it with inventory.py ledger start. Nothing was written.'
                      % batch_id)
    ledger_folders = {r.get('folder') for r in I.read_ledger(lpath)}
    if not os.path.isdir(folder):
        raise Refusal('%s is not a folder of facts files. Nothing was written.' % folder)
    paths = sorted(glob.glob(os.path.join(folder, '*.json')))
    if not paths:
        raise Refusal('%s holds no facts files (*.json). Nothing was written.' % folder)
    results, done = [], set()

    def mark(f, stage, reason=None):
        ev = {'ts': I.now(), 'batch': batch_id, 'folder': f, 'stage': stage}
        if reason:
            ev['reason'] = reason
        I.append_events(lpath, [ev])

    for p in paths:
        res = {'facts_file': p, 'folder': None, 'ok': False}
        raw, text, key = None, None, None
        try:
            with open(p, encoding='utf-8', errors='replace') as f:
                text = f.read(FACTS_BYTES_MAX + 1)
            raw = load_facts(p)
            folder_arg, _ = shape(raw)
            key = C.canon(folder_arg)
            res['folder'] = key
            if key not in ledger_folders:
                key = None
                raise Refusal('%s is not in batch %s. Nothing was written for it.' % (folder_arg, batch_id))
            if key in done:
                raise Refusal('Two facts files name %s. Only the first was used.' % key)
            done.add(key)
            out = project(ctx, raw, False)
            res.update(out)
            res['facts_file'] = p
            if out['wrote'] or out['kept']:
                mark(key, 'written')
            if not out['ok']:
                mark(key, 'failed', out.get('reason') or 'the files did not write')
            elif out['registered']:
                mark(key, 'registered')
            else:
                res['ok'] = False
                mark(key, 'failed', 'Files written, not added to the project list: %s' % out.get('reason', ''))
        except Exception as e:  # noqa: BLE001 one bad facts file never stops the rest
            reason = str(e) if isinstance(e, (Refusal, Failed)) else '%s: %s' % (type(e).__name__, e)
            res.update({'ok': False, 'reason': reason})
            if key is None and res.get('folder') is None:
                key = batch_folder_hint(p, text, raw, ledger_folders)
            if key is not None and key in ledger_folders:
                res['folder'] = key
                try:
                    mark(key, 'failed', reason)
                except OSError as le:
                    res['ledger_error'] = str(le)
        results.append(res)
    failed = sum(1 for r in results if not r['ok'])
    return {'ok': True, 'batch': batch_id, 'ledger': lpath, 'results': results, 'failed': failed,
            'done': len(results) - failed}


# ------------------------------------------------------------------ --confirm

def confirm(ctx, folder_arg):
    w, layout, business, manifest, hq = ctx
    src, _ = place(w, folder_arg, business, hq)
    changed, unchanged, missing = [], [], []
    plan = []
    for n in ('brief.md', 'CONTINUITY.md'):
        p = os.path.join(src, n)
        if not os.path.lexists(p):
            missing.append(n)
            continue
        if os.path.islink(p) or not os.path.isfile(p):
            unchanged.append(n)
            continue
        with open(p, 'rb') as f:
            data = f.read()
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError:
            unchanged.append(n)
            continue
        m = MARK_RE.match(text)
        if not m:
            unchanged.append(n)
            continue
        plan.append((n, p, text[m.end():]))
    if plan:
        writable(w, src, business)
    for n, p, new in plan:
        replace_file(p, new)
        changed.append(n)
    return {'ok': True, 'folder': src, 'changed': changed, 'unchanged': unchanged, 'missing': missing}


# ------------------------------------------------------------------ command line

def main(argv):
    try:
        ap = Parser(prog='write_project', add_help=False)
        ap.add_argument('--where-json', required=True, metavar='JSON_OR_PATH')
        ap.add_argument('--facts', metavar='FILE')
        ap.add_argument('--preview', action='store_true')
        ap.add_argument('--client-folder', action='store_true')
        ap.add_argument('--batch', metavar='DIR')
        ap.add_argument('--batch-id', metavar='ID')
        ap.add_argument('--confirm', metavar='FOLDER')
        a = ap.parse_args(argv)
        modes = [x for x in ('facts', 'batch', 'confirm') if getattr(a, x) is not None]
        if len(modes) != 1:
            raise Usage('give one of --facts, --batch or --confirm')
        if a.client_folder and not a.facts:
            raise Usage('--client-folder goes with --facts')
        if a.preview and not a.facts:
            raise Usage('--preview goes with --facts')
        if (a.batch is None) != (a.batch_id is None):
            raise Usage('--batch and --batch-id go together')
        w = A.read_where(a.where_json)
        layout, business, manifest, hq = A.check_where(w)
        ctx = (w, layout, business, manifest, hq)
        if a.confirm is not None:
            return emit(confirm(ctx, a.confirm))
        if a.batch is not None:
            return emit(batch(ctx, os.path.expanduser(a.batch), a.batch_id))
        raw = load_facts(os.path.expanduser(a.facts))
        out = client_folder(ctx, raw, a.preview) if a.client_folder else project(ctx, raw, a.preview)
        return emit(out, 0 if out['ok'] else 1)
    except Usage as e:
        return emit({'ok': False, 'error': 'usage: %s' % e}, 2)
    except (Refusal, Failed) as e:
        return emit({'ok': False, 'reason': str(e)}, 1)
    except OSError as e:
        return emit({'ok': False, 'reason': 'Could not read or write a file: %s' % e}, 1)
    except Exception as e:  # noqa: BLE001 every exit prints one JSON object
        return emit({'ok': False, 'error': 'crash: %s: %s' % (type(e).__name__, e), 'crash': True}, 2)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
