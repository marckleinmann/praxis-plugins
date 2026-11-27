#!/usr/bin/env python3
"""home_data.py: everything the Merlin home page shows, read from one business folder.

    python3 -I -B home_data.py "<business folder>" [--addons FILE] [--pretty]

Prints one JSON document on stdout and exits 0. Exits 2 on a usage error. It never writes
anything, anywhere: no file, no cache, no bytecode (the task engine is loaded with bytecode
switched off). Python 3.8 or later, standard library only. PyYAML is used when it imports,
and a narrow line reader stands in when it does not.

As a module (the home server imports it):

    import home_data
    data = home_data.read_home("<business folder>", addons_file=None, now=None)

`read_home` returns the dict described below and never raises for anything it finds on disk:
a source that is missing, empty or unreadable gives a null field, an empty list or a zero
count. Nothing is made up and no field is pulled out of free text: saved text is passed
through as it was saved.

Every home path comes from os.path.expanduser("~") at call time, so a HOME override works
and the server can import this once.

Folder layouts, by the same rules as setup.py, the start-up hook and the task engine:
  - HQ layout: one child of the business folder carries `kind: merlin-hq` in its
    manifest.yaml. Merlin's files sit in that HQ: context/ABOUT-ME/, context/memory/,
    live/TASKS.md. The folder given may also be the HQ itself.
  - Older flat layout: manifest.yaml without the marker sits in the business folder, with
    ABOUT-ME/, memory/ and TASKS.md beside it.
  - none: no manifest at all (not set up). ambiguous: two HQ folders; nothing is read.

The business name is `name:` in the manifest. The starter value `My Business` counts as no
name, and the folder's own name is used instead (name_source "folder").

Tasks are found through the task engine's own registry reader (tasklists.py), handed the
HQ manifest directly so it never walks up to another folder's registry. In the single-list
mode (`pre-cut`) that is the one list in the HQ; with per-folder lists (`active`) it is each
folder row's own TASKS.md. A manifest with no `task_lists:` block (an older flat install),
or an engine that cannot be loaded, falls back to the layout's one TASKS.md. A registry that
is present and will not parse gives no lists and sets `error`. An open task belongs to a
project by its `[Tag]`: the project's name (any case), or a tag the registry routes to a
folder of that name. An untagged line in a folder's own list belongs to that folder. Every
other open task counts only in `untracked_open`.

CONTINUITY.md is read by its level-two headings, in any case: the first sentence of the
first line under a heading starting "Current state", the first paragraph under a heading
naming "next move" or "first move", and up to 5 list items (else `###` headings) under a
heading naming "decision". A missing heading gives null. Text keeps its markdown as saved.
The updated date is the newest `Last updated:` date in the file, else its saved time.

Files are counted under outputs/, knowledge/meetings/ and updates/, hidden ones skipped. Past
5,000 files `truncated` is true, `count` is a floor and `newest` covers only the files walked.

Construction projects are folders holding job.json, found by a file check: the business
folder's subfolders down to 3 levels (a job.json at <business>/a/b/c/job.json is the deepest
found), hidden folders, links and the HQ skipped, no search inside a folder already found,
at most 200, inside a time limit. They are reported apart from `projects`, each flagged
whether it is also a listed project.

The JSON document (dates are YYYY-MM-DD, times are local YYYY-MM-DDTHH:MM:SS):

{
  "schema": 1,
  "today": date, "generated_at": time,
  "business": {"folder", "name", "name_source": "manifest" | "folder",
               "layout": "hq" | "flat" | "none" | "ambiguous",
               "hq": path or null, "manifest": path or null, "ambiguous": [paths]},
  "projects": [{
      "id": page id (below), "name", "path", "exists": bool, "has_job_json": bool,
      "continuity": null or {
          "file": "CONTINUITY.md", "saved": time,
          "status": first sentence of the current-state section, or null,
          "next_move": first paragraph of the best-next-move section, or null,
          "decisions": [up to 5 items of a section whose heading names decisions],
          "updated": {"date", "source": "date-line" | "saved-time"}},
      "tasks": {"open": n, "p1": n, "lines": [{"id", "priority", "status", "section",
                                               "date", "title", "list"}]},
      "files": {"outputs" | "meetings" | "updates":
                {"folder", "exists", "count", "truncated", "newest": [{"name", "path", "saved"}]}}
  }],
  "construction_projects": {"items": [{"id", "name", "path", "in_projects", "job": {"name",
                            "address", "stage"} or null, "continuity", "tasks", "files"}],
                            "count", "truncated", "timed_out", "limit", "depth"},
      each item's "continuity", "tasks" and "files" have the same shape as a project's, so
      the page can show the folder as a plain project card; its tasks are the open lines
      tagged with the folder's name (they still count in "untracked_open")
  "tasks": {"source": "registry" | "single-list" | "none", "state", "lists": [paths],
            "open": n, "p1": n, "untracked_open": n, "error": text or null},
  "about_me": {"folder", "about_me", "voice", "email_signature", "email_template"},
      each a saved file: null, or {"file", "path", "text", "saved", "fill_in": count of
      "[FILL IN]"}; voice adds "samples" (dated headings), the template adds "is_starter"
  "businesses": {"folder", "count", "saved", "items": [{"name", "saved", "files":
                 [{"file", "text", "saved"}]}]},
  "plugins": {"installed": [{"name", "version", "numbers", "enabled", "scopes"}],
              "merlin": {"version", "numbers", "source": "plugin" | "installed"} or null},
  "addons": [{"id", "name", "label", "icon", "plugin", "marker", "product_page", "sample",
              "sample_pages",
              "state": "activated" | "bought-not-installed" | "not-activated",
              "installed": bool, "enabled": bool, "in_catalog": bool, "version", "numbers"}],
  "addons_file": {"path", "found": bool},
  "install": {"business_folder", "merlin_files", "project_count",
              "construction_project_count"}
}

Add-on states: activated when the plugin's full installed name is in
~/.claude/plugins/installed_plugins.json and not switched off (`enabledPlugins` false) in
~/.claude/settings.json. Bought but not installed when it is not installed and
~/.praxis/catalog.json names it. Everything else is not activated. Several entries for one
plugin, and versions with a suffix such as 1.3.1-<commit>, are read by their three leading
numbers and the highest wins. catalog.json is read only if it exists, never fetched. Shapes
read: a list of names, or an object whose "plugins" (or "names") is a list of names or of
objects with a "name"; a name matches with or without its "@marketplace" part.

The add-on list comes from a JSON file, default addons/addons.json beside this script: a
list, or {"addons": [...]}, of objects with id, name, label, icon, plugin, marker,
product_page, sample and sample_pages (the page names a sample moves through by itself, or
null). A missing file means no add-ons are known.

Page ids: every project and construction item carries "id", the first 12 hex characters of
the SHA-1 of its real path as UTF-8 (page_id below). The page names a folder only by this id
(the open-folder action), and the server finds the path by reading the data again, so a page
never hands the server a path.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import time

SCHEMA = 1
JOB_SCAN_DEPTH = 3
JOB_SCAN_LIMIT = 200
JOB_SCAN_SECONDS = 2.0
WALK_LIMIT = 5000
NEWEST = 5
DECISIONS = 5
TASK_LINES = 200
STARTER_NAME = 'My Business'
ABOUT_ME_FILES = (('about_me', 'about-me.md'), ('voice', 'voice.md'),
                  ('email_signature', 'email-signature.md'), ('email_template', 'email-template.html'))
FILE_FOLDERS = (('outputs', ('outputs',)), ('meetings', ('knowledge', 'meetings')), ('updates', ('updates',)))
HQ_MARKER_RE = re.compile(br'^kind:\s*["\']?merlin-hq["\']?\s*(#.*)?$')
DATE_RE = re.compile(r'(\d{4}-\d{2}-\d{2})')
LAST_UPDATED_RE = re.compile(r'Last updated:?\**:?\s*\**\s*(\d{4}-\d{2}-\d{2})', re.I)
DATED_HEADING_RE = re.compile(r'^#{2,3}\s+.*\d{4}-\d{2}-\d{2}')


# ------------------------------------------------------------------ where things are

def _home(*parts):
    return os.path.join(os.path.expanduser('~'), *parts)


def _here_parts():
    return os.path.abspath(__file__).split(os.sep)


def plugin_root():
    """The plugin folder: this file sits at <plugin>/skills/home/home_data.py."""
    parts = _here_parts()
    return os.sep.join(parts[:-3]) or os.sep


def default_addons_file():
    return os.sep.join(_here_parts()[:-1] + ['addons', 'addons.json'])


def _real(p):
    try:
        return os.path.realpath(os.path.abspath(os.path.expanduser(p)))
    except (OSError, ValueError):
        return os.path.abspath(p)


def has_hq_marker(manifest):
    try:
        with open(manifest, 'rb') as f:
            data = f.read()
    except (OSError, ValueError):
        return False
    for line in data.split(b'\n'):
        if line.endswith(b'\r'):
            line = line[:-1]
        if HQ_MARKER_RE.match(line):
            return True
    return False


def find_layout(d):
    """(layout, business, hq, ambiguous) for folder D, by the rules in setup.py find_hq."""
    own = os.path.join(d, 'manifest.yaml')
    if os.path.isfile(own) and has_hq_marker(own):
        return 'hq', os.path.split(d)[0], d, []
    try:
        names = sorted(os.listdir(d))
    except OSError:
        names = []
    hqs = []
    for n in names:
        if n.startswith('.'):
            continue
        c = os.path.join(d, n)
        m = os.path.join(c, 'manifest.yaml')
        if os.path.isdir(c) and os.path.isfile(m) and has_hq_marker(m):
            hqs.append(c)
    if len(hqs) > 1:
        return 'ambiguous', d, None, hqs
    if hqs:
        return 'hq', d, hqs[0], []
    if os.path.isfile(own):
        return 'flat', d, d, []
    return 'none', d, None, []


def data_paths(layout, hq):
    if layout == 'hq':
        return {'manifest': os.path.join(hq, 'manifest.yaml'),
                'about_me': os.path.join(hq, 'context', 'ABOUT-ME'),
                'memory': os.path.join(hq, 'context', 'memory'),
                'task_list': os.path.join(hq, 'live', 'TASKS.md')}
    if layout == 'flat':
        return {'manifest': os.path.join(hq, 'manifest.yaml'),
                'about_me': os.path.join(hq, 'ABOUT-ME'),
                'memory': os.path.join(hq, 'memory'),
                'task_list': os.path.join(hq, 'TASKS.md')}
    return {'manifest': None, 'about_me': None, 'memory': None, 'task_list': None}


# ------------------------------------------------------------------ small readers

def _read_text(path, limit=4 * 1024 * 1024):
    try:
        with open(path, 'rb') as f:
            raw = f.read(limit)
    except (OSError, ValueError):
        return None
    return raw.decode('utf-8', errors='replace')


def _mtime(path):
    try:
        return os.stat(path).st_mtime
    except (OSError, ValueError):
        return None


def _iso_time(ts):
    if ts is None:
        return None
    return datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%dT%H:%M:%S')


def _iso_date(ts):
    if ts is None:
        return None
    return datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d')


def page_id(path):
    """The id the page uses for a project folder: 12 hex characters of the SHA-1 of its real path."""
    return hashlib.sha1(_real(path).encode('utf-8', 'surrogateescape')).hexdigest()[:12]


def _read_json(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _unquote(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in '"\'':
        return s[1:-1]
    return s


def _strip_comment(s):
    """S without a trailing `# comment`, leaving a `#` inside quotes alone."""
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


# ------------------------------------------------------------------ manifest

def _yaml_load(text):
    saved = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        import yaml  # noqa
    except ImportError:
        return None
    finally:
        sys.dont_write_bytecode = saved
    try:
        data = yaml.safe_load(text)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def fallback_name(text):
    """The top-level `name:` of a manifest, read line by line, or None."""
    for line in text.split('\n'):
        m = re.match(r'^name:[ \t]*(.*?)[ \t]*\r?$', line)
        if m:
            v = _unquote(_strip_comment(m.group(1)))
            return v or None
    return None


def fallback_projects(text):
    """[(name, path)] from a top-level `projects:` list, read line by line.

    Reads `projects: []`, and entries written as `- name: X` with their own keys on the
    following lines, or as a bare `- X`. Only the keys `name` and `path` at the entry's own
    indent are kept; nested lists and maps inside an entry are skipped.
    """
    lines = text.split('\n')
    start = None
    for i, raw in enumerate(lines):
        m = re.match(r'^projects:[ \t]*(.*?)[ \t]*\r?$', raw)
        if m:
            rest = _strip_comment(m.group(1))
            if rest.startswith('[') and rest.endswith(']'):
                inner = rest[1:-1].strip()
                return [(_unquote(x), None) for x in inner.split(',') if _unquote(x)] if inner else []
            start = i + 1
            break
    if start is None:
        return []
    out = []
    item_indent = None
    key_indent = None
    cur = None
    for raw in lines[start:]:
        line = raw.rstrip('\r')
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        indent = len(line) - len(line.lstrip(' \t'))
        if indent == 0 and not line.startswith('-'):
            break
        body = line.strip()
        if body.startswith('- ') or body == '-':
            if item_indent is None:
                item_indent = indent
            if indent == item_indent:
                if cur is not None:
                    out.append(cur)
                cur = {}
                after = body[1:]
                rest = _strip_comment(after.strip())
                key_indent = indent + 1 + len(after) - len(after.lstrip(' \t'))
                if rest:
                    k, sep, v = rest.partition(':')
                    if sep and re.match(r'^[A-Za-z_][\w-]*$', k.strip()):
                        if k.strip() in ('name', 'path') and _unquote(_strip_comment(v)):
                            cur[k.strip()] = _unquote(_strip_comment(v))
                    else:
                        cur['name'] = _unquote(rest)
                continue
        if cur is None or item_indent is None or indent <= item_indent:
            if item_indent is not None and indent < item_indent:
                break
            continue
        if indent != key_indent:
            continue
        k, sep, v = _strip_comment(body).partition(':')
        if sep and k.strip() in ('name', 'path') and _unquote(v):
            cur[k.strip()] = _unquote(v)
    if cur is not None:
        out.append(cur)
    return [(c.get('name'), c.get('path')) for c in out if c.get('name')]


def read_manifest(path):
    """(name, [(project name, project path or None)], text) from a manifest file."""
    text = _read_text(path) if path else None
    if text is None:
        return None, [], None
    data = _yaml_load(text)
    if data is not None:
        name = data.get('name')
        name = str(name).strip() if name is not None and str(name).strip() else None
        projects = []
        for row in data.get('projects') or []:
            if isinstance(row, dict) and row.get('name') is not None and str(row.get('name')).strip():
                p = row.get('path')
                projects.append((str(row['name']).strip(), str(p).strip() if p is not None else None))
            elif isinstance(row, str) and row.strip():
                projects.append((row.strip(), None))
        return name, projects, text
    return fallback_name(text), fallback_projects(text), text


# ------------------------------------------------------------------ CONTINUITY.md

def _sections(text):
    """[(heading lower-cased, [body lines])] for each level-two heading."""
    out = []
    cur = None
    for line in text.split('\n'):
        line = line.rstrip('\r')
        m = re.match(r'^##\s+(.*?)\s*$', line)
        if m and not line.startswith('###'):
            cur = (re.sub(r'[^a-z ]+', ' ', m.group(1).lower()).strip(), [])
            cur = (re.sub(r'\s+', ' ', cur[0]), cur[1])
            out.append(cur)
        elif cur is not None:
            cur[1].append(line)
    return out


def _find_section(sections, test):
    for head, body in sections:
        if test(head):
            return body
    return None


LIST_MARK_RE = re.compile(r'^(?:[-*+]\s+(?:\[[ xX]\]\s+)?|\d+[.)]\s+)')


def _first_line(body):
    for line in body:
        s = line.strip()
        if not s or s.startswith('#') or s in ('---', '***') or s.startswith('<!--'):
            continue
        if re.match(r'^\**Last updated', s, re.I):
            continue
        return LIST_MARK_RE.sub('', s)
    return None


# A sentence ends at . ! or ? followed by any closing marks (bold, code, quotes, brackets)
# and then a space or the end of the line.
SENTENCE_RE = re.compile('^(.+?[.!?][*_`\'"\u201d)\\]]*)(?:\\s|$)')


def first_sentence(s):
    if not s:
        return None
    m = SENTENCE_RE.match(s)
    return (m.group(1) if m else s).strip() or None


def _first_paragraph(body):
    para = []
    for line in body:
        s = line.strip()
        if s.startswith('#'):
            if para:
                break
            continue
        if not s:
            if para:
                break
            continue
        para.append(s)
    return '\n'.join(para) or None


def _decision_items(body):
    items = [LIST_MARK_RE.sub('', l.strip()) for l in body if LIST_MARK_RE.match(l)]
    if not items:
        items = [re.sub(r'^#+\s*', '', l.strip()) for l in body if re.match(r'^###\s+\S', l)]
    return [i for i in items if i][:DECISIONS]


def read_continuity(project):
    path = os.path.join(project, 'CONTINUITY.md')
    text = _read_text(path)
    if text is None:
        return None
    saved = _mtime(path)
    secs = _sections(text)
    state = _find_section(secs, lambda h: h.startswith('current state'))
    nxt = _find_section(secs, lambda h: 'next move' in h or 'first move' in h)
    dec = _find_section(secs, lambda h: 'decision' in h)
    dates = [m.group(1) for m in LAST_UPDATED_RE.finditer(text)]
    if dates:
        updated = {'date': max(dates), 'source': 'date-line'}
    else:
        updated = {'date': _iso_date(saved), 'source': 'saved-time'} if saved else None
    return {'file': 'CONTINUITY.md', 'saved': _iso_time(saved),
            'status': first_sentence(_first_line(state)) if state is not None else None,
            'next_move': _first_paragraph(nxt) if nxt is not None else None,
            'decisions': _decision_items(dec) if dec is not None else [],
            'updated': updated}


# ------------------------------------------------------------------ files

def newest_files(folder, now_limit=None):
    """Count every file under FOLDER (hidden ones skipped) and keep the newest few."""
    out = {'folder': folder, 'exists': os.path.isdir(folder), 'count': 0, 'truncated': False, 'newest': []}
    if not out['exists']:
        return out
    seen = []
    walked = 0
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = sorted(n for n in dirnames if not n.startswith('.'))
        for n in filenames:
            if n.startswith('.'):
                continue
            walked += 1
            if walked > WALK_LIMIT:
                out['truncated'] = True
                break
            p = os.path.join(dirpath, n)
            t = _mtime(p)
            if t is not None:
                seen.append((t, p))
        if out['truncated']:
            break
    out['count'] = min(walked, WALK_LIMIT)
    seen.sort(key=lambda x: (-x[0], x[1]))
    out['newest'] = [{'name': os.path.basename(p), 'path': os.path.relpath(p, folder).replace(os.sep, '/'),
                      'saved': _iso_time(t)} for t, p in seen[:NEWEST]]
    return out


# ------------------------------------------------------------------ tasks

OPEN_RE = re.compile(r'^- \[ \]\s*(?:((?:T|[A-Z]{2,3})\d+)(?![\w-])\s*)?(?:\[([^\]]+)\]\s*)?'
                     r'(?:\((\d{4}-\d{2}-\d{2})\)\s*)?((?:\{[^}]*\}\s*)*)(.*)$')
SECTION_RE = re.compile(r'^##\s+(Active|Waiting On|Upcoming)\s*$')
TAG_RE = re.compile(r'\{(P1|P2|P3|blocked|waiting|wip|next)\}')


def parse_open_tasks(text):
    """Open task lines in the leading part, Active, Waiting On and Upcoming (never Private
    or Done), as the task engine reads them."""
    out = []
    section = 'unclassified'
    for line in text.split('\n'):
        line = line.rstrip('\r')
        if re.match(r'^##\s+', line):
            m = SECTION_RE.match(line)
            section = {'Active': 'active', 'Waiting On': 'waiting', 'Upcoming': 'upcoming'}[m.group(1)] if m else None
            continue
        if section is None or not line.startswith('- [ ]'):
            continue
        m = OPEN_RE.match(line)
        if not m:
            continue
        tags = TAG_RE.findall(m.group(4) or '')
        rest = (m.group(5) or '').strip()
        b = re.match(r'^\*\*(.+?)\*\*', rest)
        out.append({'id': m.group(1), 'tag': m.group(2), 'date': m.group(3),
                    'priority': next((t for t in tags if t.startswith('P')), None),
                    'status': next((t for t in tags if not t.startswith('P')), None),
                    'section': section, 'title': (b.group(1) if b else rest) or None})
    return out


def _load_tasklists():
    path = os.path.join(plugin_root(), 'lib', 'engine', 'triage-tasks', 'tasklists.py')
    if not os.path.isfile(path):
        return None
    saved = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location('merlin_home_tasklists', path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None
    finally:
        sys.dont_write_bytecode = saved


def task_lists(manifest, default_list):
    """(info, [(list path, folder name or None)]) through the task engine's registry."""
    info = {'source': 'none', 'state': None, 'lists': [], 'open': 0, 'p1': 0,
            'untracked_open': 0, 'error': None}
    lists = []
    tl = _load_tasklists() if manifest and os.path.isfile(manifest) else None
    reg = None
    fall_back = tl is None
    if tl is not None:
        try:
            reg = tl.load_registry(explicit=manifest, _cache=False)
        except Exception as e:
            code = getattr(e, 'code', None)
            # Only a manifest with no task_lists block means the one default list. A registry
            # that will not parse gives no lists: in `active` the default list is frozen history.
            fall_back = code == 'registry-missing-block'
            if not fall_back:
                info['error'] = code or e.__class__.__name__
            reg = None
    if reg is not None:
        info['source'] = 'registry'
        info['state'] = reg.state
        if reg.state == tl.ACTIVE:
            for f in reg.folders:
                if f.list_path and os.path.isfile(f.list_path):
                    lists.append((f.list_path, f.name))
        elif reg.frozen_ledger and os.path.isfile(reg.frozen_ledger):
            lists.append((reg.frozen_ledger, None))
    elif fall_back and default_list and os.path.isfile(default_list):
        info['source'] = 'single-list'
        lists.append((default_list, None))
    info['lists'] = [p for p, _ in lists]
    return info, lists, reg


def tasks_by_project(names, lists, reg):
    by = {n: [] for n in names}
    lower = {n.lower(): n for n in names}
    untracked = 0
    total = []
    for path, owner in lists:
        text = _read_text(path)
        if text is None:
            continue
        for t in parse_open_tasks(text):
            t['list'] = path
            total.append(t)
            proj = None
            tag = t.get('tag')
            if tag:
                proj = tag if tag in by else lower.get(tag.lower())
                if proj is None and reg is not None:
                    try:
                        f = reg.folder_by_tag(tag)
                    except Exception:
                        f = None
                    if f is not None and f.name in by:
                        proj = f.name
            elif owner in by:
                proj = owner
            if proj is None:
                untracked += 1
            else:
                by[proj].append(t)
    return by, total, untracked


# ------------------------------------------------------------------ about me and businesses

def saved_file(path, starter=None):
    text = _read_text(path)
    if text is None:
        return None
    out = {'file': os.path.basename(path), 'path': path, 'text': text,
           'saved': _iso_time(_mtime(path)), 'fill_in': text.count('[FILL IN]')}
    if starter is not None:
        st = _read_text(starter)
        out['is_starter'] = st is not None and st == text
    return out


def read_about_me(folder):
    out = {'folder': folder}
    starter = os.path.join(plugin_root(), 'skeleton', 'HQ', 'context', 'ABOUT-ME', 'email-template.html')
    for key, name in ABOUT_ME_FILES:
        f = saved_file(os.path.join(folder, name), starter if key == 'email_template' else None) if folder else None
        if f is not None and key == 'voice':
            f['samples'] = sum(1 for l in f['text'].split('\n') if DATED_HEADING_RE.match(l))
        out[key] = f
    return out


def read_businesses(memory):
    folder = os.path.join(memory, 'businesses') if memory else None
    out = {'folder': folder, 'count': 0, 'saved': None, 'items': []}
    if not folder:
        return out
    try:
        names = sorted(n for n in os.listdir(folder) if not n.startswith('.'))
    except OSError:
        return out
    newest = None
    for n in names:
        d = os.path.join(folder, n)
        if not os.path.isdir(d) or os.path.islink(d):
            continue
        files = []
        try:
            inner = sorted(x for x in os.listdir(d) if not x.startswith('.'))
        except OSError:
            inner = []
        for x in inner:
            p = os.path.join(d, x)
            if os.path.isfile(p) and x.lower().endswith(('.md', '.txt')):
                f = saved_file(p)
                if f is not None:
                    files.append({'file': x, 'text': f['text'], 'saved': f['saved']})
        saved = max((f['saved'] for f in files), default=None)
        if saved and (newest is None or saved > newest):
            newest = saved
        out['items'].append({'name': n, 'saved': saved, 'files': files})
    out['count'] = len(out['items'])
    out['saved'] = newest
    return out


# ------------------------------------------------------------------ plugins and add-ons

def version_numbers(v):
    m = re.match(r'^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?', str(v or ''))
    if not m:
        return None
    return [int(m.group(i) or 0) for i in (1, 2, 3)]


def _within(child, parent):
    c, p = _real(child), _real(parent)
    return c == p or c.startswith(p.rstrip(os.sep) + os.sep)


def read_plugins(business):
    """Every plugin in installed_plugins.json that applies here: installed for the user,
    or for a project folder that is the business folder or holds it."""
    data = _read_json(_home('.claude', 'plugins', 'installed_plugins.json'))
    settings = _read_json(_home('.claude', 'settings.json'))
    enabled_map = settings.get('enabledPlugins') if isinstance(settings, dict) else None
    enabled_map = enabled_map if isinstance(enabled_map, dict) else {}
    plugins = data.get('plugins') if isinstance(data, dict) else None
    out = []
    for name, entries in sorted((plugins or {}).items()) if isinstance(plugins, dict) else []:
        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, list):
            continue
        best, scopes = None, []
        for e in entries:
            if not isinstance(e, dict):
                continue
            scope = e.get('scope') or 'user'
            pp = e.get('projectPath')
            if scope in ('project', 'local') and not (pp and business and _within(business, pp)):
                continue
            scopes.append(scope)
            nums = version_numbers(e.get('version'))
            if best is None or (nums or [-1, -1, -1]) > (best[0] or [-1, -1, -1]):
                best = (nums, e.get('version'))
        if best is None:
            continue
        out.append({'name': name, 'version': best[1], 'numbers': best[0],
                    'enabled': enabled_map.get(name) is not False, 'scopes': sorted(set(scopes))})
    return out


def merlin_version(installed):
    pj = _read_json(os.path.join(plugin_root(), '.claude-plugin', 'plugin.json'))
    if isinstance(pj, dict) and pj.get('version'):
        return {'version': str(pj['version']), 'numbers': version_numbers(pj['version']), 'source': 'plugin'}
    rows = [p for p in installed if p['name'].split('@', 1)[0] == 'merlin']
    rows.sort(key=lambda p: p['numbers'] or [-1, -1, -1], reverse=True)
    if rows:
        return {'version': rows[0]['version'], 'numbers': rows[0]['numbers'], 'source': 'installed'}
    return None


def read_catalog():
    data = _read_json(_home('.praxis', 'catalog.json'))
    rows = data
    if isinstance(data, dict):
        rows = data.get('plugins', data.get('names'))
    names = set()
    for r in rows if isinstance(rows, list) else []:
        n = r.get('name') if isinstance(r, dict) else r
        if isinstance(n, str) and n.strip():
            names.add(n.strip())
    return names


def read_addon_list(path):
    data = _read_json(path) if path else None
    rows = data.get('addons') if isinstance(data, dict) else data
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else [], data is not None


def addon_states(rows, installed, catalog):
    by = {p['name']: p for p in installed}
    out = []
    for r in rows:
        plugin = r.get('plugin') or ''
        p = by.get(plugin)
        bare = plugin.split('@', 1)[0]
        in_cat = plugin in catalog or bool(bare and bare in catalog)
        if p is not None and p['enabled']:
            state = 'activated'
        elif p is None and in_cat:
            state = 'bought-not-installed'
        else:
            state = 'not-activated'
        row = {k: r.get(k) for k in ('id', 'name', 'label', 'icon', 'plugin', 'marker', 'product_page', 'sample',
                                     'sample_pages')}
        row.update({'state': state, 'installed': p is not None, 'enabled': bool(p and p['enabled']),
                    'in_catalog': in_cat, 'version': p['version'] if p else None,
                    'numbers': p['numbers'] if p else None})
        out.append(row)
    return out


# ------------------------------------------------------------------ construction projects

def scan_jobs(business, skip=(), depth=JOB_SCAN_DEPTH, limit=JOB_SCAN_LIMIT, seconds=JOB_SCAN_SECONDS,
              marker='job.json'):
    """Folders holding MARKER, at most DEPTH levels below BUSINESS, at most LIMIT of them."""
    out = {'items': [], 'truncated': False, 'timed_out': False, 'limit': limit, 'depth': depth}
    skip = set(_real(s) for s in skip if s)
    deadline = time.monotonic() + seconds
    level = [business]
    for _ in range(depth):
        nxt = []
        for d in level:
            if time.monotonic() > deadline:
                out['timed_out'] = True
                return out
            try:
                names = sorted(os.listdir(d))
            except OSError:
                continue
            for n in names:
                if n.startswith('.'):
                    continue
                c = os.path.join(d, n)
                if os.path.islink(c) or not os.path.isdir(c) or _real(c) in skip:
                    continue
                if os.path.isfile(os.path.join(c, marker)):
                    if len(out['items']) >= limit:
                        out['truncated'] = True
                        return out
                    out['items'].append(c)
                    continue
                nxt.append(c)
        level = nxt
    return out


def job_facts(folder):
    data = _read_json(os.path.join(folder, 'job.json'))
    if not isinstance(data, dict):
        return None
    facts = data.get('facts') if isinstance(data.get('facts'), dict) else {}
    addr = facts.get('address', data.get('address'))
    if isinstance(addr, dict):
        addr = addr.get('full')
    name = data.get('job_name', data.get('name'))
    stage = data.get('stage')
    return {'name': name if isinstance(name, str) else None,
            'address': addr if isinstance(addr, str) else None,
            'stage': stage if isinstance(stage, str) else None}


# ------------------------------------------------------------------ the whole page

def _fill_record(p, ts):
    """Add continuity, tasks and files to a project or construction item P, in place."""
    p['continuity'] = read_continuity(p['path']) if p['exists'] else None
    p['tasks'] = {'open': len(ts), 'p1': sum(1 for t in ts if t['priority'] == 'P1'),
                  'lines': [{k: t[k] for k in ('id', 'priority', 'status', 'section', 'date', 'title', 'list')}
                            for t in ts[:TASK_LINES]]}
    p['files'] = {key: newest_files(os.path.join(p['path'], *rel)) for key, rel in FILE_FOLDERS}


def _inside(parent, child):
    """True when CHILD is PARENT or below it. Both are real paths."""
    try:
        return os.path.commonpath([parent, child]) == parent
    except ValueError:  # different drives on Windows
        return False


def read_home(folder, addons_file=None, now=None, job_seconds=JOB_SCAN_SECONDS):
    now = now or datetime.datetime.now()
    folder = _real(folder)
    layout, business, hq, clash = find_layout(folder)
    dp = data_paths(layout, hq)
    name, projects_raw, _text = read_manifest(dp['manifest'])
    name_source = 'manifest'
    if not name or name == STARTER_NAME:
        name, name_source = os.path.basename(business.rstrip(os.sep)) or business, 'folder'
    base = os.path.split(dp['manifest'])[0] if dp['manifest'] else business

    projects = []
    seen_names = []
    biz_real = _real(business)
    for pname, ppath in projects_raw:
        if pname in seen_names:
            continue
        seen_names.append(pname)
        path = _real(os.path.join(base, ppath)) if ppath else os.path.join(business, pname)
        # The home shows the business folder and nothing else on the computer: a row whose
        # real folder is the business folder itself, or lies outside it (a path that climbs
        # out, a link that leads out), is left off the page, as the hook leaves it out.
        if _real(path) == biz_real or not _inside(biz_real, _real(path)):
            continue
        projects.append({'id': page_id(path), 'name': pname, 'path': path, 'exists': os.path.isdir(path),
                         'has_job_json': os.path.isfile(os.path.join(path, 'job.json'))})

    info, lists, reg = task_lists(dp['manifest'], dp['task_list'])
    by, total, untracked = tasks_by_project([p['name'] for p in projects], lists, reg)
    info['open'] = len(total)
    info['p1'] = sum(1 for t in total if t['priority'] == 'P1')
    info['untracked_open'] = untracked

    for p in projects:
        _fill_record(p, by.get(p['name'], []))

    jobs = {'items': [], 'truncated': False, 'timed_out': False, 'limit': JOB_SCAN_LIMIT, 'depth': JOB_SCAN_DEPTH}
    if layout != 'ambiguous' and os.path.isdir(business):
        jobs = scan_jobs(business, skip=[hq] + clash, seconds=job_seconds)
    listed = set(_real(p['path']) for p in projects)
    jobs['items'] = [{'id': page_id(j), 'name': os.path.basename(j), 'path': j, 'in_projects': _real(j) in listed,
                      'job': job_facts(j), 'exists': True} for j in jobs['items']]
    jobs['count'] = len(jobs['items'])
    job_tasks, _t, _u = tasks_by_project([j['name'] for j in jobs['items']], lists, reg)
    for j in jobs['items']:
        _fill_record(j, job_tasks.get(j['name'], []))
        del j['exists']

    installed = read_plugins(business)
    addons_path = addons_file or default_addons_file()
    rows, found = read_addon_list(addons_path)

    return {
        'schema': SCHEMA,
        'today': now.strftime('%Y-%m-%d'),
        'generated_at': now.strftime('%Y-%m-%dT%H:%M:%S'),
        'business': {'folder': business, 'name': name, 'name_source': name_source, 'layout': layout,
                     'hq': hq, 'manifest': dp['manifest'] if dp['manifest'] and os.path.isfile(dp['manifest']) else None,
                     'ambiguous': clash},
        'projects': projects,
        'construction_projects': jobs,
        'tasks': info,
        'about_me': read_about_me(dp['about_me']),
        'businesses': read_businesses(dp['memory']),
        'plugins': {'installed': installed, 'merlin': merlin_version(installed)},
        'addons': addon_states(rows, installed, read_catalog()),
        'addons_file': {'path': addons_path, 'found': found},
        'install': {'business_folder': business, 'merlin_files': hq, 'project_count': len(projects),
                    'construction_project_count': jobs['count']},
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description='Print the Merlin home page data for one business folder, as JSON.')
    ap.add_argument('folder')
    ap.add_argument('--addons', default=None, help='the add-on list (default: addons/addons.json beside this script)')
    ap.add_argument('--pretty', action='store_true')
    a = ap.parse_args(argv)
    if not os.path.isdir(a.folder):
        sys.stderr.write('home_data: not a folder: %s\n' % a.folder)
        return 2
    data = read_home(a.folder, addons_file=a.addons)
    for stream in (sys.stdout,):
        if hasattr(stream, 'reconfigure'):
            try:
                stream.reconfigure(encoding='utf-8')
            except (ValueError, OSError):
                pass
    json.dump(data, sys.stdout, indent=2 if a.pretty else None, ensure_ascii=False)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
