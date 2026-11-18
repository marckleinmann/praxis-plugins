#!/usr/bin/env python3
"""inventory.py: read a folder and say what is in it, before a project is made from it.

    inventory.py --where-json JSON_OR_PATH --folder PATH
    inventory.py --where-json JSON_OR_PATH --folders A B C
    inventory.py --where-json JSON_OR_PATH --list
    inventory.py --where-json JSON_OR_PATH ledger start --batch ID --folders A B C
    inventory.py --where-json JSON_OR_PATH ledger mark  --batch ID --folder A --stage S [--reason R]
    inventory.py --where-json JSON_OR_PATH ledger show  --batch ID

--where-json is the output of setup.sh --where, as text, a file path, or - for standard input,
the same contract as adopt.py.

--folder prints one JSON object: {ok, folder, place, shape, counts, bytes, dates, pdfs, read_first,
subfolders, unreadable}. counts is by type (pdf, image, doc, sheet, email, text, other). dates is
{oldest, newest} from file times. Each pdf is {path, pages, bytes}; pages is null when the page
counter cannot tell. read_first is at most 12 files ranked for setting up a project: plan sets
first, then contracts, proposals and quotes, then emails and small documents. place is inside when
the folder is in the business folder and outside when it is not. shape is builder (most
subfolders read as street addresses), job (the folder holds the files itself) or mixed.

--folders and --list print {ok, rows}: one row per folder, each isolated, so a bad folder becomes
{ok: false, folder, reason} and the rest go on. --list reads the candidate folders from
adopt.py --list.

The ledger is <run state>/project-from-folder/<batch id>.jsonl, one line per event. Stages:
listed, read, written, registered, failed (a reason is required). show prints each folder's latest
stage and next_round, the next up to 10 folders not yet registered or failed.

Writes nothing but the ledger. Every exit prints one JSON object on standard output, an unexpected
error included. Exit 0 ok, 1 refusal, 2 usage or crash. Standard library only.
"""

import sys

# Set before the project-setup scripts are loaded, so no run leaves bytecode beside them.
sys.dont_write_bytecode = True

import argparse  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import tempfile  # noqa: E402
from datetime import datetime  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SETUP_REL = os.path.join('project-setup', 'scripts', 'copy_in.py')


def find_setup_scripts():
    """The project-setup scripts folder: walk up from here to the skills folder that holds it.
    That is the same in the source tree and in the built plugin."""
    d = HERE
    while True:
        if os.path.isfile(os.path.join(d, SETUP_REL)):
            return os.path.dirname(os.path.join(d, SETUP_REL))
        nxt = os.path.dirname(d)
        if nxt == d:
            raise SystemExit(json.dumps({'ok': False, 'error': 'crash: the project-setup scripts are not beside this skill'}))
        d = nxt


sys.path.insert(0, find_setup_scripts())
import copy_in as C  # noqa: E402

A = C.A  # adopt.py as copy_in loaded it, so Refusal is one class
Refusal = A.Refusal

ROUND = 10
READ_FIRST_MAX = 12
PDF_LIST_MAX = 200
PDF_READ_MAX = 64 * 1024 * 1024
PLAN_BYTES_MAX = 50 * 1024 * 1024
HUGE = 25 * 1024 * 1024
SMALL = 1024 * 1024
STAGES = ('listed', 'read', 'written', 'registered', 'failed')
FINAL = ('registered', 'failed')
BATCH_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}')

TYPES = {
    'pdf': ('.pdf',),
    'image': ('.jpg', '.jpeg', '.png', '.gif', '.heic', '.heif', '.tif', '.tiff', '.webp', '.bmp'),
    'doc': ('.doc', '.docx', '.pages', '.rtf', '.odt'),
    'sheet': ('.xls', '.xlsx', '.xlsm', '.csv', '.numbers', '.ods'),
    'email': ('.eml', '.msg', '.mbox'),
    'text': ('.txt', '.md', '.markdown'),
}
EXT_TYPE = {e: t for t, es in TYPES.items() for e in es}

# A plan set by its name. "sheet" alone is left out: it matched "Sheet-Metal" bid packages on a real
# job folder. A "<kind> set" name (design, progress, permit, construction, bid, issued, CD, DD, SD)
# counts, because builders and architects name their sets that way.
PLAN_RE = re.compile(r'(?<![a-z])(plans?|drawings?|blueprints?|elevations?|floor[ _-]?plans?|architectural|'
                     r'(design|progress|permit|construction|bid|issued|issue|cd|dd|sd|ifc|pricing)[ _-]?(sets?|docs?)|'
                     r'a[-_ ]?\d{1,3}(\.\d+)?)(?![a-z])', re.I)
PAPER_RE = re.compile(r'(?<![a-z])(contract|agreement|proposal|quote|quotation|estimate|scope|bid|sow|'
                      r'change[ _-]?orders?|spec(ification)?s?[ _-]?book|specifications)(?![a-z])', re.I)
DIGITS_RE = re.compile(r'\d+')


def family(rel):
    """Files that differ only by a number after their first word (Pkg-01, Pkg-02, a revision date)
    share a family. read_first keeps one per family, so 15 bid packages take one place, not 12."""
    base = os.path.splitext(os.path.basename(rel))[0].lower()
    marked = DIGITS_RE.sub('#', base)
    cut = marked.find('#', 1)
    return (os.path.dirname(rel).lower(), marked[:cut] if cut > 0 else marked)
SUFFIXES = ('st', 'street', 'rd', 'road', 'ave', 'avenue', 'dr', 'drive', 'ln', 'lane', 'ct', 'court',
            'blvd', 'boulevard', 'way', 'pl', 'place', 'ter', 'terrace', 'cir', 'circle', 'hwy', 'pkwy')
ADDR_RE = re.compile(r'^\d{1,6}[A-Za-z]?(?:-\d{1,6})?\s+(?P<words>[A-Za-z][A-Za-z.\' -]*?)(?P<tail>\s*[-,_(].*)?$')


class Usage(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Usage(message)


def emit(obj, code=0):
    print(json.dumps(obj, indent=2, ensure_ascii=False))
    return code


# ------------------------------------------------------------------ what a folder looks like

def looks_like_address(name):
    """A number, then street words, then an optional street suffix: 22 Elm St, 14 Oak Ave, 9 Birch Ln.
    It needs at least two words after the number (Elm St), so 2024 Taxes is not an address."""
    m = ADDR_RE.match(name.strip())
    if not m:
        return False
    words = [w.strip('.').lower() for w in m.group('words').split() if w.strip('.')]
    if not words or len(words) > 5:
        return False
    return len(words) >= 2


def ftype(rel):
    return EXT_TYPE.get(os.path.splitext(rel)[1].lower(), 'other')


def pdf_pages(path, size):
    """Pages of a PDF from its own bytes, or None when this cannot tell. Reads the page tree's
    /Count, else counts /Type /Page. A file packed in object streams, or too big to read, gives None."""
    if size > PDF_READ_MAX:
        return None
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except OSError:
        return None
    if not data.startswith(b'%PDF-') and b'%PDF-' not in data[:1024]:
        return None
    counts = [int(m.group(1)) for m in re.finditer(rb'/Type\s*/Pages\b[^>]*?/Count\s+(\d+)', data)]
    counts += [int(m.group(1)) for m in re.finditer(rb'/Count\s+(\d+)[^>]*?/Type\s*/Pages\b', data)]
    if counts:
        return max(counts)
    n = len(re.findall(rb'/Type\s*/Page(?![A-Za-z])', data))
    return n or None


def rank(rel, size):
    """(score, why) for a file worth reading first, or None. Lower scores come first."""
    name = os.path.basename(rel)
    base = os.path.splitext(name)[0]
    t = ftype(rel)
    if t == 'pdf' and (PLAN_RE.search(base) or PLAN_RE.search(rel)) and size <= PLAN_BYTES_MAX:
        return 0, 'plan set'
    if size > HUGE:
        return None
    if t in ('pdf', 'doc', 'text', 'sheet', 'email') and PAPER_RE.search(base):
        return 1, 'contract, quote or spec'
    if t == 'email':
        return 2, 'email'
    if t in ('doc', 'text') and size <= SMALL:
        return 3, 'small document'
    if t == 'pdf':
        return 4, 'pdf'
    if t == 'sheet' and size <= SMALL:
        return 4, 'sheet'
    if t == 'image':
        return 9, 'image'
    return None


# Merlin's own files in a project folder. They are never a source for a new brief: a second run on a
# folder already set up would otherwise read Merlin's draft back as if the user had written it.
MERLIN_OWN = ('brief.md', 'CONTINUITY.md')
HISTORY_RE = re.compile(r'^updates/\d{4}-\d{2}-\d{2}_history-from-files(-\d+)?\.md$')


def read_first(files):
    ranked = []
    for rel, size in files:
        if rel in MERLIN_OWN or HISTORY_RE.match(rel.replace(os.sep, '/')):
            continue
        r = rank(rel, size)
        if r:
            ranked.append((r[0], rel, size, r[1]))
    # Within a family the newest name wins (revision dates sort last), then families by score.
    ranked.sort(key=lambda x: (x[0], x[1].lower()), reverse=False)
    best, more = {}, {}
    for item in sorted(ranked, key=lambda x: (x[0], x[1].lower()), reverse=True):
        key = family(item[1])
        more[key] = more.get(key, -1) + 1
        if key not in best or item[0] < best[key][0]:
            best[key] = item
    picked = sorted(best.values(), key=lambda x: (x[0], x[1].lower()))
    keep = [x for x in picked if x[0] < 9] or picked
    out = []
    for score, rel, size, why in keep[:READ_FIRST_MAX]:
        n = more.get(family(rel), 0)
        out.append({'path': rel, 'type': ftype(rel), 'bytes': size,
                    'why': why if not n else '%s, and %d more like it' % (why, n)})
    return out


def inventory(folder, business, hq):
    """The report for one folder. Raises Refusal for a folder Merlin will not read."""
    src = C.canon(folder)
    if not os.path.isdir(src):
        raise Refusal('%s is not a folder I can find.' % folder)
    if C.canon(business) == src or (hq and C.canon(hq) == src):
        raise Refusal('%s is the business folder or its HQ folder, not one project.' % folder)
    s = C.survey(src)
    files = list(s['files'])
    unreadable = list(s['unreadable'])
    counts = {k: 0 for k in ('pdf', 'image', 'doc', 'sheet', 'email', 'text', 'other')}
    total = 0
    times = []
    pdfs = []
    per_sub = {}
    for rel, size in files:
        counts[ftype(rel)] += 1
        total += size
        try:
            times.append(os.stat(os.path.join(src, rel)).st_mtime)
        except OSError:
            if rel not in unreadable:
                unreadable.append(rel)
        top = rel.split(os.sep, 1)[0] if os.sep in rel else None
        if top:
            per_sub[top] = per_sub.get(top, 0) + 1
        if ftype(rel) == 'pdf' and len(pdfs) < PDF_LIST_MAX:
            pages = pdf_pages(os.path.join(src, rel), size)
            pdfs.append({'path': rel, 'pages': pages, 'bytes': size})
    subs = []
    for d in s['dirs']:
        if os.sep in d or d.startswith('.'):
            continue
        subs.append({'name': d, 'files': per_sub.get(d, 0), 'looks_like_address': looks_like_address(d)})
    top_files = [rel for rel, _ in files if os.sep not in rel]
    addr = sum(1 for x in subs if x['looks_like_address'])
    if len(subs) >= 2 and addr >= 2 and addr * 2 > len(subs):
        shape = 'builder'
    elif top_files:
        shape = 'job'
    else:
        shape = 'mixed'
    day = lambda t: datetime.fromtimestamp(t).strftime('%Y-%m-%d')  # noqa: E731
    return {'ok': True, 'folder': src,
            'place': 'inside' if C.within(src, C.canon(business)) else 'outside',
            'shape': shape, 'counts': counts, 'bytes': total,
            'dates': {'oldest': day(min(times)) if times else None, 'newest': day(max(times)) if times else None},
            'pdfs': pdfs, 'pdfs_total': counts['pdf'], 'read_first': read_first(files),
            'subfolders': subs, 'unreadable': sorted(set(unreadable))}


def one_row(folder, business, hq):
    try:
        return inventory(folder, business, hq)
    except Refusal as e:
        return {'ok': False, 'folder': folder, 'reason': str(e)}
    except Exception as e:  # noqa: BLE001 one bad folder never stops the rest
        return {'ok': False, 'folder': folder, 'reason': 'Could not read it: %s: %s' % (type(e).__name__, e)}


# ------------------------------------------------------------------ the ledger

def ledger_path(w, batch):
    if not BATCH_RE.fullmatch(batch or '') or '..' in batch:
        raise Refusal('A batch id is letters, digits, dot, dash and underscore only, and starts with a letter or '
                      'digit. Nothing was written.')
    state = w.get('run_state')
    if not state:
        raise Refusal('The settings give no run-state folder, so there is nowhere to keep the batch ledger. '
                      'Run merlin:setup\'s check. Nothing was written.')
    return os.path.join(state, 'project-from-folder', batch + '.jsonl')


def read_ledger(path):
    rows = []
    if os.path.isfile(path):
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
    return rows


def append_events(path, events):
    """Add EVENTS as lines through a temporary file renamed into place."""
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    old = b''
    if os.path.isfile(path):
        with open(path, 'rb') as f:
            old = f.read()
        if old and not old.endswith(b'\n'):
            old += b'\n'
    new = ''.join(json.dumps(e, ensure_ascii=False) + '\n' for e in events).encode('utf-8')
    fd, tmp = tempfile.mkstemp(dir=folder, prefix='.ledger.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(old + new)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def summary(path, batch):
    latest, order = {}, []
    for r in read_ledger(path):
        f = r.get('folder')
        if f not in latest:
            order.append(f)
        latest[f] = {'folder': f, 'stage': r.get('stage'), 'reason': r.get('reason')}
    rows = [latest[f] for f in order]
    stages = {}
    for r in rows:
        stages[r['stage']] = stages.get(r['stage'], 0) + 1
    todo = [r['folder'] for r in rows if r['stage'] not in FINAL]
    return {'ok': True, 'batch': batch, 'ledger': path, 'folders': rows, 'stages': stages,
            'round_size': ROUND, 'next_round': todo[:ROUND], 'remaining': len(todo)}


def ledger(w, a):
    path = ledger_path(w, a.batch)
    if a.action == 'start':
        if os.path.exists(path):
            raise Refusal('Batch %s already started. Use show to see where it stopped. Nothing was written.' % a.batch)
        folders = [C.canon(f) for f in a.folders]
        if not folders:
            raise Refusal('No folders were given. Nothing was written.')
        t = now()
        append_events(path, [{'ts': t, 'batch': a.batch, 'folder': f, 'stage': 'listed'} for f in dict.fromkeys(folders)])
        return emit(summary(path, a.batch))
    if not os.path.isfile(path):
        raise Refusal('There is no batch called %s. Nothing was written.' % a.batch)
    if a.action == 'show':
        return emit(summary(path, a.batch))
    folder = C.canon(a.folder)
    if a.stage not in STAGES:
        raise Refusal('A stage is one of %s. Nothing was written.' % ', '.join(STAGES))
    if a.stage == 'failed' and not a.reason:
        raise Refusal('A failed folder needs a reason. Nothing was written.')
    if folder not in {r.get('folder') for r in read_ledger(path)}:
        raise Refusal('%s is not in batch %s. Nothing was written.' % (folder, a.batch))
    ev = {'ts': now(), 'batch': a.batch, 'folder': folder, 'stage': a.stage}
    if a.reason:
        ev['reason'] = a.reason
    append_events(path, [ev])
    return emit(summary(path, a.batch))


# ------------------------------------------------------------------ command line

def split_ledger(argv):
    """(before, after) the word ledger, where it opens a sub-command and is not a folder name."""
    in_list = False
    for i, t in enumerate(argv):
        if t == '--folders':
            in_list = True
        elif t.startswith('--'):
            in_list = False
        elif t == 'ledger' and not in_list and (i == 0 or not argv[i - 1] in ('--folder', '--where-json')):
            return argv[:i], argv[i + 1:]
    return argv, None


def main(argv):
    try:
        before, after = split_ledger(argv)
        ap = Parser(prog='inventory')
        ap.add_argument('--where-json', required=True, metavar='JSON_OR_PATH')
        g = ap.add_mutually_exclusive_group()
        g.add_argument('--folder', metavar='PATH')
        g.add_argument('--folders', nargs='+', metavar='PATH')
        g.add_argument('--list', action='store_true')
        a = ap.parse_args(before)
        if after is None and not (a.folder or a.folders or a.list):
            raise Usage('give --folder, --folders, --list or ledger')
        if after is not None and (a.folder or a.folders or a.list):
            raise Usage('ledger does not combine with --folder, --folders or --list')
        w = A.read_where(a.where_json)
        layout, business, manifest, hq = A.check_where(w)
        if after is not None:
            lp = Parser(prog='inventory ledger')
            sub = lp.add_subparsers(dest='action', required=True, parser_class=Parser)
            st = sub.add_parser('start')
            st.add_argument('--batch', required=True)
            st.add_argument('--folders', nargs='+', required=True)
            mk = sub.add_parser('mark')
            mk.add_argument('--batch', required=True)
            mk.add_argument('--folder', required=True)
            mk.add_argument('--stage', required=True)
            mk.add_argument('--reason')
            sh = sub.add_parser('show')
            sh.add_argument('--batch', required=True)
            return ledger(w, lp.parse_args(after))
        if a.folder:
            row = one_row(os.path.expanduser(a.folder), business, hq)
            return emit(row, 0 if row['ok'] else 1)
        if a.folders:
            folders = a.folders
        else:
            _, found, _ = A.candidates(w, business, manifest, hq)
            folders = [c['path'] for c in found]
        return emit({'ok': True, 'rows': [one_row(os.path.expanduser(f), business, hq) for f in folders]})
    except Usage as e:
        return emit({'ok': False, 'error': 'usage: %s' % e}, 2)
    except Refusal as e:
        return emit({'ok': False, 'refused': str(e)}, 1)
    except OSError as e:
        return emit({'ok': False, 'refused': 'Could not read or write a file: %s' % e}, 1)
    except Exception as e:  # noqa: BLE001 every exit prints one JSON object
        return emit({'ok': False, 'error': 'crash: %s: %s' % (type(e).__name__, e), 'crash': True}, 2)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
