#!/usr/bin/env python3
"""Copy a folder from elsewhere on this Mac into the business folder, as a new project folder.

    python3 copy_in.py --where-json JSON_OR_PATH --from FOLDER --as NAME          preview
    python3 copy_in.py --where-json JSON_OR_PATH --from FOLDER --as NAME --yes    copy

--where-json takes the output of `setup.sh --where`, as adopt.py does: the JSON text itself, a
file holding it, or `-` to read it from standard input. The business folder, the HQ folder, the
manifest and the run-state folder come from that JSON and nothing else.

Without --yes it writes nothing. It prints, as JSON, the source, the target, the file count, the
total size, what is left out (.DS_Store and ._ files), what is skipped (named pipes, sockets and
devices: anything that is not a file, a folder or a link), every link that points outside the
folder, and any warning: a cloud-storage source, a size over 2 GB, a file that cannot be read, an
unfinished copy left by an earlier run.

With --yes it copies into a hidden temporary folder inside the business folder
(.merlin-copy-NAME.partial), removing a leftover one of that name first. Links are copied as links
and never followed; hidden folders are copied. Then it checks every file's size and SHA-256
against the original, renames the temporary folder to NAME, and adds one line to
<run-state>/project-setup/copies.jsonl: date, source, project, file count, bytes. Never file
content. The original folder is only read. On any failure it removes only its own temporary
folder and names the file that failed.

Refused, with nothing written: a source that is missing or not a folder; inside the business
folder (use the adopt list instead); holding the business folder; the home folder, Desktop,
Documents, Downloads, the top of a cloud-storage folder, the top of the disk or a system folder;
a source holding a Merlin HQ folder's settings file; a NAME that is taken, not a plain folder
name, or refused by adopt.py; a layout that is unset, none or ambiguous; an unfinished move of
Merlin's own files into the HQ folder.

Exit 0 on success, 1 on a refusal or a failed copy (the JSON says why), 2 on a usage error.
Standard library only.
"""

import sys

# Set before adopt.py is loaded, so no run leaves bytecode beside the scripts.
sys.dont_write_bytecode = True

import argparse  # noqa: E402
import hashlib  # noqa: E402
import importlib.util  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import stat  # noqa: E402
import tempfile  # noqa: E402
from datetime import datetime  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def load_adopt():
    """adopt.py from this folder, for its --where checks and its skip_reason()."""
    spec = importlib.util.spec_from_file_location('merlin_adopt', os.path.join(HERE, 'adopt.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


A = load_adopt()
Refusal = A.Refusal

PLAIN_NAME_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}')
# The same line setup.py and the start-up hook look for. Matched on bytes, a trailing \r stripped.
HQ_MARKER_RE = re.compile(br'^kind:\s*["\']?merlin-hq["\']?\s*(#.*)?$')
BIG = 2 * 1024 ** 3
CHUNK = 1024 * 1024
RECORD_OWNER = 'project-setup'
RECORD_FILE = 'copies.jsonl'
PLAIN_PLACES = (('Desktop', 'your Desktop'), ('Documents', 'your Documents folder'),
                ('Downloads', 'your Downloads folder'))
# Folders of the Mac itself: refused when the source is one of them or inside one.
SYSTEM_TREES = ('/System', '/Library', '/Applications', '/usr', '/bin', '/sbin', '/etc', '/dev', '/cores')
# Refused only as themselves: user folders can sit inside them.
SYSTEM_TOPS = ('/private', '/private/var', '/var', '/tmp', '/Volumes', '/Network', '/opt')
HOME_CLOUD = (('Dropbox', 'Dropbox'), ('OneDrive', 'OneDrive'), ('Google Drive', 'Google Drive'),
              ('iCloud Drive', 'iCloud Drive'))
CLOUD_PARTS = ('CloudStorage', 'Mobile Documents')


class CopyFailed(Exception):
    def __init__(self, rel, why, where=None, own=True):
        Exception.__init__(self, why)
        self.rel = rel
        self.why = why
        self.where = where  # the path to name, when it is not a path inside the source
        self.own = own      # False when the temporary folder is not this run's, so it is left alone


def emit(obj, code=0):
    print(json.dumps(obj, indent=2, ensure_ascii=False))
    return code


def canon(p):
    return os.path.realpath(os.path.abspath(os.path.expanduser(p)))


def within(p, d):
    """True when P is D or inside it. Both canonical."""
    return p == d or p.startswith(d.rstrip(os.sep) + os.sep)


def home(*parts):
    return os.path.join(os.path.expanduser('~'), *parts)


def human(n):
    if n < 1024:
        return '%d bytes' % n
    for unit in ('KB', 'MB', 'GB', 'TB'):
        n /= 1024.0
        if n < 1024 or unit == 'TB':
            return '%.1f %s' % (n, unit)


def cloud_service(name):
    for prefix, service in (('Dropbox', 'Dropbox'), ('GoogleDrive-', 'Google Drive'), ('OneDrive', 'OneDrive')):
        if name.startswith(prefix):
            return service
    return name


# ------------------------------------------------------------------ refusals

def name_refusal(w, business, manifest, hq, name):
    """Why NAME cannot be the new project folder, or None."""
    if not PLAIN_NAME_RE.fullmatch(name or ''):
        return Refusal('"%s" is not a plain folder name. Use letters, digits and hyphens, with no spaces, for '
                       'example Old-Website. Nothing was written.' % name)
    path = os.path.join(business, name)
    regs = A.registered(A.read_lines(manifest), manifest)
    data_paths = {A.real(w[k]) for k in A.DATA_KEYS if w.get(k)}
    why = A.skip_reason(name, path, hq, data_paths, regs)
    if why == 'registered':
        return Refusal('%s is already a project in Merlin\'s settings file (%s). Pick another name. Nothing was '
                       'written.' % (name, manifest))
    if why in ('hq', 'merlin-data'):
        return Refusal('%s is the name of a folder that holds Merlin\'s own files. Pick another name. Nothing was '
                       'written.' % name)
    if why:
        return Refusal('"%s" is not a plain folder name. Pick another name. Nothing was written.' % name)
    if os.path.lexists(path):
        if os.path.isdir(path) and not os.path.islink(path):
            return Refusal('There is already a folder named %s in the business folder %s. Pick another name. '
                           'Nothing was written.' % (name, business))
        return Refusal('There is already something named %s in the business folder %s. Pick another name. '
                       'Nothing was written.' % (name, business))
    return None


def place_reason(src):
    """Why SRC is a place that is never copied whole, or None."""
    h = canon(home())
    if src == os.sep:
        return 'it is the top of this Mac\'s disk'
    if src == h:
        return 'it is your home folder itself'
    if within(h, src):
        return 'it holds your whole home folder'
    for d, words in PLAIN_PLACES:
        if src == canon(home(d)):
            return 'it is %s itself' % words
    if os.path.dirname(src) == h:
        base = os.path.basename(src)
        for prefix, service in HOME_CLOUD:
            if base.startswith(prefix):
                return 'it is the top of your %s, not a folder inside it' % service
    if within(src, canon(home('Library'))):
        cs = canon(home('Library', 'CloudStorage'))
        ic = canon(home('Library', 'Mobile Documents', 'com~apple~CloudDocs'))
        if within(src, cs) and src != cs:
            parts = os.path.relpath(src, cs).split(os.sep)
            if len(parts) < (3 if parts[0].startswith('GoogleDrive-') else 2):
                return 'it is the top of your %s, not a folder inside it' % cloud_service(parts[0])
            return None
        if within(src, ic):
            return None if src != ic else 'it is the top of your iCloud Drive, not a folder inside it'
        return 'it is one of your Mac\'s own system folders'
    for d in SYSTEM_TREES:
        if within(src, canon(d)):
            return 'it is one of your Mac\'s own system folders'
    for d in SYSTEM_TOPS:
        if src == canon(d):
            return 'it is one of your Mac\'s own system folders'
    return None


def has_hq_marker(manifest):
    try:
        with open(manifest, 'rb') as f:
            data = f.read()
    except (OSError, ValueError):
        return False
    for line in data.split(b'\n'):
        if HQ_MARKER_RE.match(line[:-1] if line.endswith(b'\r') else line):
            return True
    return False


def source_refusal(raw, src, business):
    """Why the folder SRC (canonical; RAW as the user gave it) cannot be copied in, or None."""
    if not os.path.exists(src):
        return Refusal('The folder %s is not there. Check the path, or drag the folder into the chat. Nothing was '
                       'written.' % raw)
    if not os.path.isdir(src):
        return Refusal('%s is not a folder. Name the folder that holds the project. Nothing was written.' % src)
    why = place_reason(src)
    if not why:
        if within(src, business):
            return Refusal('%s is already inside your business folder, so there is nothing to copy. To make it a '
                           'project, say "adopt my existing projects" and pick it from the list. Nothing was '
                           'written.' % src)
        if within(business, src):
            why = 'it holds your business folder'
        elif has_hq_marker(os.path.join(src, 'manifest.yaml')):
            why = ('it is a Merlin HQ folder, where Merlin keeps its own files (its settings file, manifest.yaml, '
                   'says so). A copy would give your business folder a second HQ folder, and Merlin would stop '
                   'writing until one was removed')
    if why:
        return Refusal('%s cannot be copied in: %s. Pick another folder. Nothing was written.' % (src, why))
    return None


# ------------------------------------------------------------------ the survey

def kind_of(mode):
    if stat.S_ISFIFO(mode):
        return 'named pipe'
    if stat.S_ISSOCK(mode):
        return 'socket'
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        return 'device'
    return 'special file'


def survey(src):
    """Everything under SRC, by lstat, never following a link and never opening anything but a
    folder. Folders come before their contents."""
    s = {'dirs': [], 'files': [], 'links': [], 'left_out': [], 'skipped': [], 'unreadable': []}

    def walk(rel):
        path = os.path.join(src, rel) if rel else src
        try:
            with os.scandir(path) as it:
                names = sorted(e.name for e in it)
        except OSError:
            s['unreadable'].append(rel or '.')
            return
        for n in names:
            r = os.path.join(rel, n) if rel else n
            p = os.path.join(src, r)
            try:
                st = os.lstat(p)
            except OSError:
                s['unreadable'].append(r)
                continue
            mode = st.st_mode
            if stat.S_ISDIR(mode):
                s['dirs'].append(r)
                walk(r)
            elif n == '.DS_Store' or n.startswith('._'):
                s['left_out'].append(r)
            elif stat.S_ISLNK(mode):
                try:
                    s['links'].append((r, os.readlink(p)))
                except OSError:
                    s['unreadable'].append(r)
            elif stat.S_ISREG(mode):
                s['files'].append((r, st.st_size))
                if not os.access(p, os.R_OK):
                    s['unreadable'].append(r)
            else:
                s['skipped'].append({'path': r, 'kind': kind_of(mode)})

    walk('')
    return s


def leaves(rel, target):
    """True when a link at REL with TARGET points outside the folder it sits in."""
    if os.path.isabs(target):
        return True
    norm = os.path.normpath(os.path.join(os.path.dirname(rel), target))
    return norm == os.pardir or norm.startswith(os.pardir + os.sep)


def in_cloud(src):
    for d in (home('Library', 'CloudStorage'), home('Library', 'Mobile Documents')):
        if within(src, canon(d)):
            return True
    h = canon(home())
    if within(src, h) and src != h:
        top = os.path.relpath(src, h).split(os.sep)[0]
        if any(top.startswith(prefix) for prefix, _ in HOME_CLOUD):
            return True
    return any(part in CLOUD_PARTS for part in src.split(os.sep))


def report(src, target, s, stale):
    files = len(s['files'])
    size = sum(n for _, n in s['files'])
    outside = [{'path': r, 'target': t} for r, t in s['links'] if leaves(r, t)]
    warnings = []
    for l in outside:
        warnings.append('The link %s points to %s, outside the folder. The copy keeps it as a link to the same '
                        'place, so it may point back into the original.' % (l['path'], l['target']))
    for x in s['skipped']:
        warnings.append('%s is a special system item (a %s), not a document, so it is left out.'
                        % (x['path'], x['kind']))
    for r in s['unreadable']:
        warnings.append('%s cannot be opened by your account on this Mac, so the copy would stop there. Make '
                        'it readable first, for example in Finder with Get Info, then try again.' % r)
    if in_cloud(src):
        warnings.append('The folder is inside a cloud-storage folder. A file kept online only is not on this Mac '
                        'yet, so the copy cannot read it and stops. Make the folder available offline first.')
    if size > BIG:
        warnings.append('The folder holds %s, over 2 GB. The copy takes that much space in the business folder '
                        'and may take a while.' % human(size))
    if stale:
        warnings.append('An unfinished copy from an earlier run is in the business folder: %s. It holds only a '
                        'copy, and the next copy with this name removes it first.' % stale)
    return {'source': src, 'target': target, 'files': files, 'bytes': size, 'size': human(size),
            'folders': len(s['dirs']), 'links': len(s['links']), 'left_out': s['left_out'],
            'skipped': s['skipped'], 'links_outside': outside, 'unreadable': s['unreadable'],
            'stale_partial': stale, 'warnings': warnings}


# ------------------------------------------------------------------ the copy

def remove_own(path):
    """Remove this script's own temporary folder at PATH (or a leftover one), and nothing else."""
    if not os.path.lexists(path):
        return
    if os.path.islink(path) or not os.path.isdir(path):
        os.unlink(path)
        return
    for d, subs, _ in os.walk(path):
        for n in subs:
            p = os.path.join(d, n)
            if not os.path.islink(p):
                try:
                    os.chmod(p, 0o700)
                except OSError:
                    pass
    shutil.rmtree(path)


def copy_file(src_path, dst_path):
    """Copy one regular file, hashing what was read. Opened without following a link and without
    blocking, then refused unless it is still a regular file, so a pipe can never hang the copy."""
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(src_path, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError('it was replaced by something that is not a normal file')
        h = hashlib.sha256()
        n = 0
        with open(dst_path, 'xb') as out:
            while True:
                chunk = os.read(fd, CHUNK)
                if not chunk:
                    break
                h.update(chunk)
                out.write(chunk)
                n += len(chunk)
    finally:
        os.close(fd)
    return n, h.hexdigest(), st


def file_digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def keep_times(path, st, link=False):
    try:
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns), follow_symlinks=not link)
    except (NotImplementedError, OSError):
        if not link:
            raise


def copy_tree(src, tmp, s):
    """Copy the surveyed entries of SRC into TMP, check every file, then set modes and times."""
    done = {}
    rel = '.'
    try:
        os.mkdir(tmp, 0o700)
    except FileExistsError:
        raise CopyFailed('.', 'another copy with this name started at the same time', where=tmp, own=False)
    except OSError as e:
        raise CopyFailed('.', e.strerror or str(e), where=tmp, own=False)
    try:
        for rel in s['dirs']:
            os.mkdir(os.path.join(tmp, rel), 0o700)
        for rel, _ in s['files']:
            done[rel] = copy_file(os.path.join(src, rel), os.path.join(tmp, rel))
        for rel, target in s['links']:
            os.symlink(target, os.path.join(tmp, rel))
        # The check: every file's size and SHA-256 against what was read from the original.
        for rel, size in s['files']:
            n, digest, st = done[rel]
            now = os.lstat(os.path.join(src, rel))
            if n != size or now.st_size != size or now.st_mtime_ns != st.st_mtime_ns:
                raise OSError('it changed while it was being copied')
            dst = os.path.join(tmp, rel)
            if os.lstat(dst).st_size != size or file_digest(dst) != digest:
                raise OSError('the copy does not match the original')
        for rel, _ in s['files']:
            st = done[rel][2]
            dst = os.path.join(tmp, rel)
            os.chmod(dst, stat.S_IMODE(st.st_mode) & 0o777)
            keep_times(dst, st)
        for rel, _ in s['links']:
            keep_times(os.path.join(tmp, rel), os.lstat(os.path.join(src, rel)), link=True)
        # Folders last, deepest first, so filling them does not change their times. The owner
        # keeps full access, so Merlin can add the project's own files to the copy.
        for rel in list(reversed(s['dirs'])) + ['']:
            st = os.lstat(os.path.join(src, rel) if rel else src)
            dst = os.path.join(tmp, rel) if rel else tmp
            os.chmod(dst, (stat.S_IMODE(st.st_mode) & 0o777) | 0o700)
            keep_times(dst, st)
    except OSError as e:
        raise CopyFailed(rel or '.', e.strerror or str(e))


def write_record(run_state, row):
    """Add ROW as one line of <run-state>/project-setup/copies.jsonl, through a temporary file
    renamed into place."""
    folder = os.path.join(run_state, RECORD_OWNER)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, RECORD_FILE)
    old = b''
    if os.path.isfile(path):
        with open(path, 'rb') as f:
            old = f.read()
        if old and not old.endswith(b'\n'):
            old += b'\n'
    line = (json.dumps(row, ensure_ascii=False) + '\n').encode('utf-8')
    fd, tmp = tempfile.mkstemp(dir=folder, prefix='.%s.' % RECORD_FILE, suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(old + line)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


def copy_in(w, business, name, src, s, stale):
    target = os.path.join(business, name)
    tmp = os.path.join(business, '.merlin-copy-%s.partial' % name)
    if stale:
        remove_own(tmp)
    try:
        if s['unreadable']:
            # A folder that cannot be listed would otherwise be copied empty, and the check below
            # only sees files it listed. Stop before anything is written.
            raise CopyFailed(s['unreadable'][0], 'your account on this Mac cannot open it', own=False)
        copy_tree(src, tmp, s)
        if os.path.lexists(target):
            raise CopyFailed('.', 'something with that name appeared in the business folder during the copy',
                             where=target)
        os.rename(tmp, target)
    except CopyFailed as e:
        if e.own:
            remove_own(tmp)
        where = e.where or (os.path.join(src, e.rel) if e.rel != '.' else src)
        return emit({'failed': 'The copy stopped at %s: %s. Nothing was added to the business folder, and the '
                               'original folder is unchanged.' % (where, e.why),
                     'file': e.rel}, 1)
    except BaseException:
        remove_own(tmp)
        raise
    out = report(src, target, s, None)
    out['mode'] = 'copied'
    row = {'date': datetime.now().astimezone().isoformat(timespec='seconds'), 'source': src, 'project': name,
           'files': out['files'], 'bytes': out['bytes']}
    try:
        out['record'] = write_record(w['run_state'], row)
    except (OSError, KeyError, TypeError) as e:
        out['record'] = None
        out['warnings'].append('The copy is done, but its record in Merlin\'s working-notes folder could not '
                               'be written: %s.' % e)
    out['note'] = ('Copied and checked: every file matches the original. The original folder was not changed. '
                   'Next: the adopt steps, starting with reading the folder\'s own instructions.')
    return emit(out)


# ------------------------------------------------------------------ main

def main(argv):
    ap = argparse.ArgumentParser(prog='copy_in')
    ap.add_argument('--where-json', required=True, metavar='JSON_OR_PATH')
    ap.add_argument('--from', dest='source', required=True, metavar='FOLDER')
    ap.add_argument('--as', dest='name', required=True, metavar='NAME')
    ap.add_argument('--yes', action='store_true')
    a = ap.parse_args(argv)
    try:
        w = A.read_where(a.where_json)
        _layout, business, manifest, hq = A.check_where(w)
        blocked = A.move_refusal(w, business)
        if blocked:
            raise blocked
        business = canon(business)
        bad = name_refusal(w, business, manifest, hq, a.name)
        if bad:
            raise bad
        src = canon(a.source)
        bad = source_refusal(os.path.expanduser(a.source), src, business)
        if bad:
            raise bad
        s = survey(src)
        tmp = os.path.join(business, '.merlin-copy-%s.partial' % a.name)
        stale = tmp if os.path.lexists(tmp) else None
        if a.yes:
            return copy_in(w, business, a.name, src, s, stale)
        out = report(src, os.path.join(business, a.name), s, stale)
        out['mode'] = 'preview'
        out['note'] = ('Nothing was written. To copy, run the same command with --yes. The original folder is only '
                       'read, never changed.')
        return emit(out)
    except Refusal as e:
        return emit({'refused': str(e)}, 1)
    except OSError as e:
        return emit({'refused': 'Could not read or write a file: %s. Nothing was written.' % e}, 1)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
