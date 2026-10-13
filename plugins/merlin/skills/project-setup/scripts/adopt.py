#!/usr/bin/env python3
"""Adopt folders the user already has as Merlin projects.

    python3 adopt.py --where-json JSON_OR_PATH --list
    python3 adopt.py --where-json JSON_OR_PATH --register NAME

--where-json takes the output of `setup.sh --where`: the JSON text itself, a file holding
it, or `-` to read it from standard input. This script holds no folder resolver of its own.
It reads the business folder, the HQ folder and the manifest from that JSON and nothing else.

--list    prints, as JSON, every folder in the business folder that is not yet a project,
          with what each one already holds (CLAUDE.md, AGENTS.md, .claude/skills/, brief.md,
          CONTINUITY.md). It skips the HQ folder, hidden folders, folders the manifest's
          `projects:` list already names (by name or by path), and Merlin's own folders
          (ABOUT-ME, memory and state in the older flat layout).
          Writes nothing.
--register NAME
          adds NAME to the manifest's `projects:` list, with the path written from the
          manifest's own folder: `../NAME` in an HQ manifest, `NAME` in the older flat layout.
          An empty `projects: []` becomes a list. Every other byte of the manifest stays as
          it was. Writes nothing but the manifest, through a temporary file renamed into place.
          Refused, with nothing written, while the --where JSON reports an unfinished move of
          Merlin's own files into the HQ folder (`unfinished_move`).

Exit 0 on success, 1 on a refusal (the JSON says why), 2 on a usage error. Standard library
only.
"""

import argparse
import json
import os
import re
import sys
import tempfile

HOLDS = ('CLAUDE.md', 'AGENTS.md', '.claude/skills/', 'brief.md', 'CONTINUITY.md')
DATA_KEYS = ('about_me', 'memory', 'task_list', 'task_ids', 'run_state')
PROJECTS_RE = re.compile(r'^projects:(?P<rest>.*?)(?P<nl>\r?\n)?$')
PLAIN_NAME_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9 _.()+-]*')


NO_WHERE = ('Merlin could not read where its own files are. Run merlin:setup\'s check (say: check my Merlin '
            'setup), then try again. Nothing was written.')


def no_where(detail):
    """The plain refusal for unreadable setup.sh --where output, with the detail kept for a developer."""
    return Refusal('%s For your developer: %s.' % (NO_WHERE, detail))


class Refusal(Exception):
    pass


def emit(obj, code=0):
    print(json.dumps(obj, indent=2, ensure_ascii=False))
    return code


def real(p):
    return os.path.realpath(p) if p else None


# ------------------------------------------------------------------ the --where JSON

def read_where(arg):
    """The setup.py --where JSON, from text, a file, or standard input."""
    if arg == '-':
        text = sys.stdin.read()
    elif arg.lstrip().startswith('{'):
        text = arg
    elif os.path.isfile(arg):
        with open(arg, encoding='utf-8') as f:
            text = f.read()
    else:
        raise no_where('the --where-json input is neither JSON nor a file: %s' % arg)
    try:
        w = json.loads(text)
    except ValueError as e:
        raise no_where('the --where-json input is not valid JSON: %s' % e)
    if not isinstance(w, dict) or 'layout' not in w:
        raise no_where('the --where-json input is not the output of setup.sh --where')
    return w


def check_where(w):
    """Refuse every layout but hq and flat, and a JSON whose paths do not hold together."""
    layout = w.get('layout')
    if layout == 'unset':
        raise Refusal('No business folder is chosen yet. Run merlin:setup first. Nothing was written.')
    if layout == 'none':
        raise Refusal('The business folder is not set up yet: Merlin\'s settings file (manifest.yaml) is not in '
                      'it. Run merlin:setup first. Nothing was written.')
    if layout == 'ambiguous':
        names = ', '.join(os.path.basename(x) for x in (w.get('ambiguous') or []))
        raise Refusal('The business folder holds more than one HQ folder (%s), the folder where Merlin keeps '
                      'its own files, so Merlin cannot tell which one to use. Keep one and move the others out '
                      'of the business folder (renaming one does not help), then run this again. Nothing was '
                      'written.' % names)
    if layout not in ('hq', 'flat'):
        raise no_where('an unknown layout, %r, in the --where-json input' % layout)
    business, manifest, hq = w.get('business'), w.get('manifest'), w.get('hq')
    if not business or not manifest:
        raise no_where('the --where-json input names no business folder or manifest')
    home = os.path.dirname(manifest)
    if layout == 'hq':
        if not hq or real(home) != real(hq) or real(os.path.dirname(hq)) != real(business):
            raise no_where('the --where-json input names a manifest outside its HQ folder, or an HQ folder '
                           'outside the business folder')
    elif real(home) != real(business):
        raise no_where('the --where-json input names a flat manifest outside the business folder')
    if not os.path.isdir(business):
        raise Refusal('The business folder %s is not there.' % business)
    if not os.path.isfile(manifest):
        raise Refusal('Merlin\'s settings file (manifest.yaml) is not there: %s. Run merlin:setup first. Nothing '
                      'was written.' % manifest)
    return layout, business, manifest, hq


# ------------------------------------------------------------------ reading projects:

def strip_comment(s):
    """S without a trailing `# comment`, unless the `#` sits inside quotes."""
    quote = None
    for i, ch in enumerate(s):
        if quote:
            if ch == quote:
                quote = None
        elif ch in '"\'':
            quote = ch
        elif ch == '#' and (i == 0 or s[i - 1] in ' \t'):
            return s[:i].rstrip()
    return s.rstrip()


def unquote(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in '"\'':
        return s[1:-1]
    return s


def projects_line(lines):
    """The index of the top-level `projects:` line, or None."""
    for i, l in enumerate(lines):
        if PROJECTS_RE.match(l):
            return i
    return None


def block_end(lines, start):
    """The index of the last entry line of the `projects:` block that starts at START
    (START itself when the block has no entries). A top-level line ends the block; blank
    lines and column-0 comments inside it belong to neither side."""
    last = start
    for i in range(start + 1, len(lines)):
        raw = lines[i].rstrip('\r\n')
        if not raw.strip() or raw.startswith('#'):
            continue
        if raw.startswith((' ', '\t', '-')):
            last = i
            continue
        break
    return last


def registered(lines, manifest):
    """The manifest's `projects:` entries as [{'name', 'path'}], path resolved or None."""
    i = projects_line(lines)
    if i is None:
        return []
    rest = strip_comment(PROJECTS_RE.match(lines[i]).group('rest')).strip()
    entries = []
    if rest.startswith('['):
        inner = rest.strip('[]').strip()
        for n in [x for x in inner.split(',') if x.strip()]:
            entries.append({'name': unquote(n), 'path': None})
    else:
        for raw in lines[i + 1:block_end(lines, i) + 1]:
            s = strip_comment(raw.rstrip('\r\n')).strip()
            if not s:
                continue
            if s.startswith('- '):
                s = s[2:].strip()
                entries.append({'name': None, 'path': None})
                if ':' not in s:
                    entries[-1]['name'] = unquote(s)
                    continue
            elif s == '-':
                entries.append({'name': None, 'path': None})
                continue
            if not entries:
                continue
            k, _, v = s.partition(':')
            if k.strip() in ('name', 'path') and v.strip():
                entries[-1][k.strip()] = unquote(v)
    home = os.path.dirname(manifest)
    out = []
    for e in entries:
        p = e['path'] if e['path'] is not None else e['name']
        out.append({'name': e['name'], 'path': e['path'],
                    'resolved': real(os.path.join(home, p)) if p else None})
    return out


def read_lines(manifest):
    with open(manifest, 'rb') as f:
        data = f.read()
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        raise Refusal('Merlin\'s settings file (manifest.yaml) cannot be read as text: %s. Nothing was written.'
                      % manifest)
    return text.splitlines(keepends=True)


# ------------------------------------------------------------------ --list

def skip_reason(name, path, hq, data_paths, regs):
    if name.startswith('.'):
        return 'hidden'
    rp = real(path)
    if hq and rp == real(hq):
        return 'hq'
    if rp in data_paths:
        return 'merlin-data'
    for r in regs:
        if r['name'] == name or (r['resolved'] and r['resolved'] == rp):
            return 'registered'
    return None


def candidates(w, business, manifest, hq):
    """(registered, candidates, skipped) for the business folder: the manifest's projects, every
    folder that is not one yet with what it already holds, and every folder skipped with why.
    Writes nothing. setup.py's report reads this too, so it names the same folders --list offers."""
    regs = registered(read_lines(manifest), manifest)
    data_paths = {real(w[k]) for k in DATA_KEYS if w.get(k)}
    found, skipped = [], []
    for name in sorted(os.listdir(business)):
        path = os.path.join(business, name)
        if not os.path.isdir(path):
            continue
        why = skip_reason(name, path, hq, data_paths, regs)
        if why:
            skipped.append({'name': name, 'reason': why})
            continue
        has = {}
        for item in HOLDS:
            p = os.path.join(path, item.rstrip('/'))
            has[item] = os.path.isdir(p) if item.endswith('/') else os.path.isfile(p)
        found.append({'name': name, 'path': path, 'has': has})
    return regs, found, skipped


def list_candidates(w, layout, business, manifest, hq):
    regs, found, skipped = candidates(w, business, manifest, hq)
    return emit({'business': business, 'layout': layout, 'manifest': manifest,
                 'registered': [{'name': r['name'], 'path': r['path']} for r in regs],
                 'candidates': found, 'skipped': skipped})


# ------------------------------------------------------------------ --register

def yaml_scalar(name):
    return name if PLAIN_NAME_RE.fullmatch(name) and not name.endswith(' ') else json.dumps(name, ensure_ascii=False)


def write_atomic(path, text):
    target = real(path)
    folder = os.path.dirname(target)
    mode = os.stat(target).st_mode & 0o7777
    fd, tmp = tempfile.mkstemp(dir=folder, prefix='.manifest.yaml.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(text.encode('utf-8'))
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def move_refusal(w, business):
    """The plain refusal while a move of Merlin's own files into the HQ folder is unfinished, or None.
    setup.py --where reports such a move as unfinished_move (the record's path), found by the same
    check setup's own writing steps use, so this script keeps no second copy of it."""
    rec = w.get('unfinished_move')
    if not rec:
        return None
    return Refusal('Merlin cannot add a project yet: moving Merlin\'s own files into the HQ folder of %s (the '
                   'folder where Merlin keeps its own files) stopped before it finished. Nothing was deleted. The '
                   'move record, %s, lists what moved. Finish the move first (say: move my Merlin files into the HQ '
                   'folder), then add the project again. Nothing was written.' % (business, rec))


def register(w, layout, business, manifest, hq, name):
    blocked = move_refusal(w, business)
    if blocked:
        raise blocked
    if not name or name in ('.', '..') or '/' in name or os.sep in name or '\n' in name or '\r' in name:
        raise Refusal('%r is not a folder name. Give the name of one folder in the business folder.' % name)
    path = os.path.join(business, name)
    if not os.path.isdir(path):
        raise Refusal('%s is not a folder in the business folder %s.' % (name, business))
    lines = read_lines(manifest)
    regs = registered(lines, manifest)
    data_paths = {real(w[k]) for k in DATA_KEYS if w.get(k)}
    why = skip_reason(name, path, hq, data_paths, regs)
    if why == 'registered':
        raise Refusal('%s is already a project in Merlin\'s settings file (%s).' % (name, manifest))
    if why == 'hidden':
        raise Refusal('%s is a hidden folder; Merlin does not adopt hidden folders.' % name)
    if why in ('hq', 'merlin-data'):
        raise Refusal('%s holds Merlin\'s own files, not a project.' % name)
    i = projects_line(lines)
    if i is None:
        raise Refusal('Merlin\'s settings file (%s) has no list of projects. Run merlin:setup to repair it, '
                      'then run this again. Nothing was written.' % manifest)

    nl = '\r\n' if lines[i].endswith('\r\n') else '\n'
    m = PROJECTS_RE.match(lines[i])
    rest = m.group('rest')
    value = strip_comment(rest).strip()
    if value:
        if re.fullmatch(r'\[\s*\]', value):
            comment = rest[len(rest.split('#', 1)[0]):] if '#' in rest else ''
            lines[i] = 'projects:' + ((' ' + comment.strip()) if comment else '') + (m.group('nl') or nl)
        else:
            raise Refusal('The list of projects in Merlin\'s settings file (%s) is written on one line (%s). '
                          'Write it as one project per line, then run this again. Nothing was written.'
                          % (manifest, value))

    end = block_end(lines, i)
    dash = '  '
    for l in lines[i + 1:end + 1]:
        dm = re.match(r'^(\s*)- ', l)
        if dm:
            dash = dm.group(1)
            break
    rel = '../' + name if layout == 'hq' else name
    plain = yaml_scalar(name) == name
    entry = ['%s- name: %s%s' % (dash, yaml_scalar(name), nl),
             '%s  path: %s%s' % (dash, rel if plain else json.dumps(rel, ensure_ascii=False), nl)]
    if not lines[end].endswith('\n'):
        lines[end] += nl
    lines[end + 1:end + 1] = entry
    write_atomic(manifest, ''.join(lines))

    after = registered(read_lines(manifest), manifest)
    if not any(r['name'] == name and r['resolved'] == real(path) for r in after):
        raise Refusal('Wrote Merlin\'s settings file (%s) but cannot read the project %s back from it. Check '
                      'the file.' % (manifest, name))
    return emit({'registered': name, 'path': rel, 'manifest': manifest})


# ------------------------------------------------------------------ main

def main(argv):
    ap = argparse.ArgumentParser(prog='adopt')
    ap.add_argument('--where-json', required=True, metavar='JSON_OR_PATH')
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--list', action='store_true')
    g.add_argument('--register', metavar='NAME')
    a = ap.parse_args(argv)
    try:
        w = read_where(a.where_json)
        layout, business, manifest, hq = check_where(w)
        if a.list:
            return list_candidates(w, layout, business, manifest, hq)
        return register(w, layout, business, manifest, hq, a.register)
    except Refusal as e:
        return emit({'refused': str(e)}, 1)
    except OSError as e:
        return emit({'refused': 'Could not read or write a file: %s' % e}, 1)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
