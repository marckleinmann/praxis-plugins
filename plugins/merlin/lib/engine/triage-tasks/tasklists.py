#!/usr/bin/env python3
"""tasklists.py: the prefix registry, the cut state and the id grammar.

One module, imported by `triage.py` and `claim_id.py`, so the two of them cannot
drift on where a task goes.

Three things live here and nowhere else:

  1. **The id grammar.** `T\\d+` or `[A-Z]{2,3}\\d+`, no dash, no zero padding.
     `T` alone is reserved for unprefixed ids. `split_id` and `id_sort_key` are
     the only places a caller may take an id apart; `int(tid[1:])` is wrong for a
     two- or three-letter prefix.

  2. **The registry.** The `task_lists:` block in the data folder's
     `manifest.yaml`, or a `core/task-lists.json` registry beside a copied
     engine. Same object either way, so nothing above this line has to know
     which one it got. `load_registry` states the search order; an optional
     roots resolver can name the data folder when the code and the data live in
     two separate folders.

  3. **The cut state** (single list or one list per folder), and the local
     `BARRIER` file that reaches a folder the state field cannot.

**Fail closed on a registry that exists and will not parse.** A missing
manifest is single-list mode: state `pre-cut`, no folders, every write to the
file it was handed. A manifest that is present and unreadable is not that mode,
because in `active` the difference between the two is which file a write lands
in.

**No hard dependency on PyYAML.** The engine may run under a bare system
interpreter. `yaml` is used when it imports and a narrow line parser for the
`task_lists:` block is used when it does not.
"""

import datetime
import hashlib
import json
import os
import re
import sys

# Bumped whenever the engine's on-disk behaviour changes, so a copy of the engine
# running an old router can be detected by comparing stamps.
ENGINE_VERSION = '1.2.0'

PRE_CUT = 'pre-cut'
CUTTING = 'cutting'
ACTIVE = 'active'
ROLLBACK = 'rollback'
STATES = (PRE_CUT, CUTTING, ACTIVE, ROLLBACK)

# The one id grammar. Used as a fragment inside the line regexes in triage.py,
# so it carries no anchors and no capture group of its own.
ID_TOKEN = r'(?:T\d+|[A-Z]{2,3}\d+)'
ID_SPLIT_RE = re.compile(r'^(T|[A-Z]{2,3})(\d+)$')

LIST_PREFIX_RE = re.compile(r'^<!--\s*task-prefix:\s*([A-Z]{1,3})\s*-->\s*$')
# The id counter, one predicate for every reader and writer of it. Anchored to a
# whole line, like LIST_PREFIX_RE above: `claim_id.py` used to read it with an
# unanchored `search` over the whole file, so a task body carrying the literal
# `next-task-id: 9999` ahead of the real header would have been read as the
# counter. A task line may well mention the counter in its own text. `triage.py`
# matched it per line and anchored, so the two disagreed about what the counter
# IS. They now share this.
COUNTER_RE = re.compile(r'^<!--\s*next-task-id:\s*(\d+)\s*-->\s*$')


def read_counter(text, default=None):
    """The `<!-- next-task-id: N -->` value, or `default` when there is none."""
    for line in text.split('\n'):
        m = COUNTER_RE.match(line)
        if m:
            return int(m.group(1))
    return default
# The marker a generated file (a roll-up of several lists) carries on line 2.
# Anchored to an HTML comment on purpose. A bare `GENERATED` anywhere in the first
# few lines is not a marker: a real task line near the top of a list may contain
# the word, and a loose probe would read the whole list as a generated file and
# refuse every write to it.
GENERATED_RE = re.compile(r'^<!--\s*GENERATED\b')

BARRIER_NAME = 'BARRIER'
CLAIMS_DIRNAME = '.task-ids'
LANE_FILE = os.path.join(os.path.expanduser('~'), '.claude', 'merlin-task-lane')
MAX_LANES = 10


class TaskListError(Exception):
    """A refusal. Carries the machine-readable error code the CLI prints."""

    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code = code
        self.message = message


# --- the id ------------------------------------------------------------------

def split_id(tid):
    """('AB', 12) for 'AB12', ('T', 42) for 'T42', None for anything else."""
    if not isinstance(tid, str):
        return None
    m = ID_SPLIT_RE.match(tid.strip())
    if not m:
        return None
    return (m.group(1), int(m.group(2)))


def is_id(tid):
    return split_id(tid) is not None


def id_sort_key(tid):
    """Sort key for a mixed-prefix set: prefix first, then the number as a number.

    A plain string sort puts T10 before T9 and interleaves prefixes, which
    is wrong for any caller that sorts a mixed set of ids.
    """
    parts = split_id(tid)
    if not parts:
        return ('￿', 0, str(tid or ''))
    return (parts[0], parts[1], '')


def format_id(prefix, n):
    """`T` ids keep a three-digit zero padding (`T` then `007`); every other
    prefix has none."""
    if prefix == 'T':
        return 'T%03d' % n
    return '%s%d' % (prefix, n)


# --- the registry ------------------------------------------------------------

class Folder(object):
    """A registry row. `path` is None when this folder is NOT reachable from here.

    A build-local registry (`core/task-lists.json`) carries every folder's name
    and prefix so an outbox `target:` can be checked locally, and a reachable
    path for the folder the build belongs to and for no other. An unreachable row
    therefore has no path, and `list_path` and `claims_dir` are None rather than
    a bare `TASKS.md`. A relative path resolves against the CALLER's working
    directory, so a task filed for another folder would land in the caller's
    own list and report success. None cannot be silently written to; `TASKS.md`
    can.
    """

    def __init__(self, name, prefix, path, legacy_tags, legacy_prefixes=()):
        self.name = name
        self.prefix = prefix
        # '' is normalised to None here so no caller has to remember to test for
        # both. Every consumer tests `folder.path` or `folder.list_path`.
        self.path = path or None
        self.legacy_tags = list(legacy_tags or [])
        # Prefixes this folder minted under before its current `prefix`. Ids
        # carrying them are never renumbered, so they route here forever. New
        # ids always take `prefix`.
        self.legacy_prefixes = list(legacy_prefixes or [])

    @property
    def reachable(self):
        """True when this process can actually open this folder's list."""
        return bool(self.path)

    @property
    def list_path(self):
        return os.path.join(self.path, 'TASKS.md') if self.path else None

    @property
    def claims_dir(self):
        return os.path.join(self.path, CLAIMS_DIRNAME) if self.path else None

    def as_dict(self):
        return {'name': self.name, 'prefix': self.prefix, 'path': self.path,
                'legacy_tags': self.legacy_tags,
                'legacy_prefixes': self.legacy_prefixes}


def json_safe(v):
    """A value that came out of a YAML manifest, reduced to one json.dumps takes.

    PyYAML resolves an unquoted date to a `datetime.date`, and the lane row
    `claim_id.py --enroll` writes carries exactly that shape. A caller that runs
    `json.dumps(load_registry(...).as_dict())` would raise on it, so the first
    enrolled machine would break every such read.

    A date becomes its ISO string, which is what it was written as. Anything
    else json cannot carry becomes `str(v)`: this is the serialisation boundary
    and a lossy string there is better than a registry nobody can read.
    """
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (datetime.datetime, datetime.date, datetime.time)):
        return v.isoformat()
    if isinstance(v, dict):
        return {str(k): json_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [json_safe(x) for x in v]
    return str(v)


class Registry(object):
    """The task_lists block, however it reached this process."""

    def __init__(self, source, path, state, rollup, frozen_ledger,
                 orphan_default, untagged, lanes, folders,
                 self_name=None, self_prefix=None, projects=None):
        self.source = source              # 'manifest' | 'build' | 'none'
        self.path = path
        self.state = state
        self.rollup = rollup
        self.frozen_ledger = frozen_ledger
        self.orphan_default = orphan_default
        # Normalised here, once, so every consumer sees the same types whichever
        # source the registry came from and `as_dict()` cannot raise. See
        # `json_safe`.
        self.untagged = json_safe(dict(untagged or {}))
        self.lanes = [json_safe(e) for e in (lanes or [])]
        self.folders = list(folders or [])
        self.self_name = self_name
        self.self_prefix = self_prefix
        # The manifest's top-level `projects:` names. They route nothing; they
        # are project codes the parser recognises. See `project_names`.
        self.projects = [str(x).strip() for x in (projects or []) if str(x).strip()]

    # -- lookups
    def folder_by_name(self, name):
        for f in self.folders:
            if f.name == name:
                return f
        return None

    def folder_by_prefix(self, prefix):
        """The folder that owns ids with this prefix: its current `prefix` or one
        of its `legacy_prefixes`. The registry refuses a prefix claimed twice in
        either field, so at most one folder matches."""
        for f in self.folders:
            if f.prefix == prefix or prefix in f.legacy_prefixes:
                return f
        return None

    def folder_by_tag(self, tag):
        if not tag:
            return None
        for f in self.folders:
            if tag == f.name or tag in f.legacy_tags:
                return f
        return None

    def folder_by_list_path(self, list_path):
        """The folder whose own list is this path. None for the frozen ledger."""
        if not list_path:
            return None
        target = os.path.abspath(list_path)
        for f in self.folders:
            if f.path and os.path.abspath(f.list_path) == target:
                return f
        return None

    def folder_for_id(self, tid):
        parts = split_id(tid)
        if not parts or parts[0] == 'T':
            return None
        return self.folder_by_prefix(parts[0])

    def route(self, tag):
        """The folder a `[Tag]` resolves to. Never None when a registry exists:
        an unregistered tag routes to `orphan_default` with its tag kept, so a
        typo loses a task's filing and never the task."""
        f = self.folder_by_tag(tag)
        if f:
            return f, False
        return self.folder_by_name(self.orphan_default), True

    def project_names(self):
        """Every project name the user registered, in a stable order with no
        duplicates: folder names, their legacy tags, then the manifest's
        `projects:` names. This is the whole of the parser's project
        vocabulary; the engine carries no built-in list."""
        out = []
        for f in self.folders:
            for n in [f.name] + list(f.legacy_tags):
                if n and n not in out:
                    out.append(n)
        for n in self.projects:
            if n not in out:
                out.append(n)
        return out

    def code_family(self):
        """Hyphenated code stems from the registry, such as {'Web'} for a
        folder named `Web-Shop`.

        The engine's CODE pattern is a family (`Web-<anything>`), not a list:
        once a stem is known, any project tag with that stem resolves, including
        one no registry row names exactly.

        The stem shape is deliberately narrow, an initial capital and one to
        three more letters. A lowercase tag such as `my-site` contributes no
        stem, so widening the family can never change how an ordinary
        lowercase hyphenated word in a task line parses.
        """
        out = set()
        for tag in self.project_names():
            if '-' in tag:
                stem = tag.split('-', 1)[0]
                if re.match(r'^[A-Z][A-Za-z]{1,3}$', stem):
                    out.add(stem)
        return out

    def as_dict(self):
        """The registry as plain JSON-safe data. Callers round-trip this through
        `json.dumps`, so every value passes `json_safe` on the way out as well
        as on the way in."""
        return json_safe({
            'source': self.source, 'path': self.path, 'state': self.state,
            'rollup': self.rollup, 'frozen_ledger': self.frozen_ledger,
            'orphan_default': self.orphan_default, 'untagged': self.untagged,
            'lanes': self.lanes, 'self': self.self_name,
            'folders': [f.as_dict() for f in self.folders],
            'projects': self.projects,
        })


def _empty_registry():
    """Single-list mode. No manifest anywhere, so nothing routes and nothing is
    frozen."""
    return Registry('none', None, PRE_CUT, None, None, None, {}, [], [])


# -- YAML, with a fallback that needs no dependency ---------------------------

def _yaml_load(text):
    try:
        import yaml  # noqa
    except ImportError:
        return None
    try:
        return yaml.safe_load(text)
    except Exception as e:
        raise TaskListError('registry-unreadable', 'manifest YAML did not parse: %s' % e)


_INLINE_LIST_RE = re.compile(r'^\[(.*)\]$')


def _split_inline_list(s):
    """`[Website, "Website or Shop"]` -> the two strings."""
    m = _INLINE_LIST_RE.match(s.strip())
    if not m:
        return []
    body = m.group(1).strip()
    if not body:
        return []
    out, cur, quote = [], '', None
    for ch in body:
        if quote:
            if ch == quote:
                quote = None
            else:
                cur += ch
        elif ch in '"\'':
            quote = ch
        elif ch == ',':
            out.append(cur.strip())
            cur = ''
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return [x for x in out if x]


def _unquote(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in '"\'':
        return s[1:-1]
    return s


def _fallback_task_lists(text):
    """Line parser for the `task_lists:` block only. Used when PyYAML is absent.

    Deliberately narrow: it reads the block this engine owns and ignores the
    rest of the manifest, so it cannot be mistaken for a YAML implementation.
    """
    lines = text.split('\n')
    start = None
    for i, l in enumerate(lines):
        if re.match(r'^task_lists:\s*$', l):
            start = i
            break
    if start is None:
        return None
    block = []
    for l in lines[start + 1:]:
        if l.strip() == '' or l.startswith((' ', '\t')):
            block.append(l)
        elif l.lstrip().startswith('#'):
            block.append(l)
        else:
            break
    out = {'folders': [], 'untagged': {}, 'lanes': []}
    section = None
    cur = None
    for raw in block:
        l = raw.split('#', 1)[0].rstrip() if not _in_quotes_hash(raw) else raw.rstrip()
        if not l.strip():
            continue
        indent = len(l) - len(l.lstrip())
        s = l.strip()
        if indent == 2 and s.endswith(':') and ':' in s:
            key = s[:-1].strip()
            if key in ('untagged', 'lanes', 'folders'):
                section = key
                cur = None
                continue
            section = None
            continue
        if indent == 2 and ':' in s:
            k, v = s.split(':', 1)
            k, v = k.strip(), v.strip()
            section = None
            if k in ('state', 'rollup', 'frozen_ledger', 'orphan_default'):
                out[k] = _unquote(v)
            elif k == 'lanes' and v == '[]':
                out['lanes'] = []
            continue
        if section == 'untagged' and indent >= 4 and ':' in s:
            k, v = s.split(':', 1)
            out['untagged'][_unquote(k)] = _unquote(v)
            continue
        if section == 'folders':
            if s.startswith('- '):
                cur = {}
                out['folders'].append(cur)
                s = s[2:].strip()
            if cur is None or ':' not in s:
                continue
            k, v = s.split(':', 1)
            k, v = k.strip(), v.strip()
            if k == 'legacy_tags':
                cur[k] = _split_inline_list(v)
            elif k == 'legacy_prefixes' and v.strip().startswith('['):
                # A scalar falls through to `_unquote` and is read as one prefix
                # by `_legacy_prefixes`, the same as the YAML path.
                cur[k] = _split_inline_list(v)
            else:
                cur[k] = _unquote(v)
            continue
        if section == 'lanes':
            if s.startswith('- '):
                cur = {}
                out['lanes'].append(cur)
                s = s[2:].strip()
            if cur is None or ':' not in s:
                continue
            k, v = s.split(':', 1)
            cur[k.strip()] = _unquote(v)
    return out


def _fallback_projects(text):
    """Names in a top-level `projects:` list, read without PyYAML.

    Accepts `projects: []`, and entries written as `- name: Website` (with any
    further keys of the entry on their own lines) or as a bare `- Website`, at
    any indent. Everything else in the manifest is ignored.
    """
    lines = text.split('\n')
    start = None
    for i, l in enumerate(lines):
        m = re.match(r'^projects:\s*(\[\s*\])?\s*$', l)
        if m:
            if m.group(1):
                return []
            start = i
            break
    if start is None:
        return []
    out = []
    for raw in lines[start + 1:]:
        if raw.strip() and not raw.startswith((' ', '\t', '#', '-')):
            break
        l = raw.split('#', 1)[0].rstrip() if not _in_quotes_hash(raw) else raw.rstrip()
        s = l.strip()
        if not s:
            continue
        if s.startswith('- '):
            s = s[2:].strip()
            if ':' not in s:
                out.append(_unquote(s))
                continue
        k, _, v = s.partition(':')
        if k.strip() == 'name' and v.strip():
            out.append(_unquote(v))
    return [x for x in out if x]


def _project_names(data, text):
    """The manifest's `projects:` names, from parsed YAML when there is some."""
    raw = data.get('projects') if isinstance(data, dict) else _fallback_projects(text)
    out = []
    for row in raw or []:
        name = row.get('name') if isinstance(row, dict) else row
        if isinstance(name, str) and name.strip():
            out.append(name.strip())
    return out


def _in_quotes_hash(line):
    """True when a `#` in the line sits inside quotes, so it is not a comment."""
    h = line.find('#')
    if h < 0:
        return False
    return line[:h].count('"') % 2 == 1 or line[:h].count("'") % 2 == 1


LEGACY_PREFIX_RE = re.compile(r'^[A-Z]{2,3}$')


def _legacy_prefixes(value):
    """A row's `legacy_prefixes` as a list. A bare scalar is one prefix."""
    if value is None or value == '':
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


def _registry_from_manifest(path):
    with open(path, encoding='utf-8') as f:
        text = f.read()
    data = _yaml_load(text)
    block = None
    if isinstance(data, dict):
        block = data.get('task_lists')
    if block is None:
        block = _fallback_task_lists(text)
    if block is None:
        raise TaskListError(
            'registry-missing-block',
            '%s has no task_lists: block. The registry is what decides where a '
            'task goes; nothing guesses it.' % path)
    base = os.path.dirname(os.path.abspath(path))
    state = str(block.get('state') or PRE_CUT)
    if state not in STATES:
        raise TaskListError('registry-bad-state',
                            'task_lists.state is %r, not one of %s' % (state, ', '.join(STATES)))
    folders = []
    seen = {}
    for row in block.get('folders') or []:
        name = row.get('name')
        prefix = row.get('prefix')
        rel = row.get('path')
        if not (name and prefix and rel):
            raise TaskListError('registry-bad-folder',
                                'a folders row is missing name, prefix or path: %r' % (row,))
        if prefix == 'T':
            raise TaskListError('registry-bad-prefix',
                                '%s claims prefix T, which is reserved for unprefixed ids' % name)
        legacy = _legacy_prefixes(row.get('legacy_prefixes'))
        for lp in legacy:
            if not isinstance(lp, str) or lp == 'T' or not LEGACY_PREFIX_RE.match(lp):
                raise TaskListError('registry-bad-prefix',
                                    '%s lists legacy prefix %r. A legacy prefix is two or '
                                    'three uppercase letters, and never T.' % (name, lp))
        # One map over current AND legacy prefixes: an id prefix must name one
        # folder, so a legacy prefix is reserved exactly like a current one.
        for p in [prefix] + legacy:
            if p in seen:
                raise TaskListError('registry-duplicate-prefix',
                                    'prefix %s is claimed by both %s and %s'
                                    % (p, seen[p], name))
            seen[p] = name
        folders.append(Folder(name, prefix, os.path.normpath(os.path.join(base, rel)),
                              row.get('legacy_tags') or [], legacy))
    rollup = block.get('rollup') or './task-rollup.md'
    frozen = block.get('frozen_ledger') or './TASKS.md'
    return Registry(
        'manifest', os.path.abspath(path), state,
        os.path.normpath(os.path.join(base, rollup)),
        os.path.normpath(os.path.join(base, frozen)),
        block.get('orphan_default'), block.get('untagged') or {},
        block.get('lanes') or [], folders,
        projects=_project_names(data, text))


def _registry_from_build(path):
    """`core/task-lists.json`, the JSON form of the registry, written beside a
    copied engine. Paths in it resolve against the folder that holds `core/`."""
    with open(path, encoding='utf-8') as f:
        try:
            data = json.load(f)
        except ValueError as e:
            raise TaskListError('registry-unreadable',
                                '%s did not parse as JSON: %s' % (path, e))
    core_dir = os.path.dirname(os.path.abspath(path))
    base = os.path.dirname(core_dir)    # the folder that holds core/
    state = str(data.get('state') or PRE_CUT)
    if state not in STATES:
        raise TaskListError('registry-bad-state',
                            'task-lists.json state is %r, not one of %s' % (state, ', '.join(STATES)))
    folders = []
    for row in data.get('folders') or []:
        rel = row.get('path')
        # A build carries the registry's NAMES and PREFIXES so an outbox target
        # can be checked locally. It does not carry reachable paths for other
        # folders, and must not pretend to: only the folder's own row has one.
        p = os.path.normpath(os.path.join(base, rel)) if rel else None
        folders.append(Folder(row.get('name'), row.get('prefix'), p,
                              row.get('legacy_tags') or [],
                              _legacy_prefixes(row.get('legacy_prefixes'))))
    return Registry('build', os.path.abspath(path), state, None, None,
                    data.get('orphan_default'), data.get('untagged') or {},
                    data.get('lanes') or [], folders,
                    self_name=data.get('self'), self_prefix=data.get('prefix'),
                    projects=_project_names(data, ''))


def _ancestors(start):
    d = os.path.abspath(start)
    out = [d]
    while True:
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
        out.append(d)
    return out


_CACHE = {}

# An optional roots resolver, looked for at `<code>/scripts/lib/`, three levels
# above this engine. When the file is absent (the usual case), the registry
# search is the plain ancestor walk below and nothing else.
_RESOLVER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              '..', '..', '..', 'scripts', 'lib', 'merlin_roots.py')
_RESOLVER_ENV = ('MERLIN_CODE_ROOT', 'MERLIN_DATA_ROOT', 'PRAXIS_ROOT')

# The last refusal message from an importable resolver, or None. data_root() still answers
# None on a refusal (every caller's fallback is unchanged), but load_registry() reads this to
# tell "no resolver" from "a resolver that said no", and to fail loudly on the second.
_LAST_REFUSAL = None
_SPLIT_UNFINISHED = 'finish or roll back the split'


def data_root():
    """The data root from the resolver, or None when there is no answer.

    None covers three cases on purpose: no resolver file in reach, a resolver
    that will not import, and a resolver that refuses. Every caller treats None
    as "use the path you would use with no resolver", so a missing or refusing
    resolver can never route a write somewhere new.
    """
    global _LAST_REFUSAL
    _LAST_REFUSAL = None
    if not os.path.isfile(_RESOLVER_PATH):
        return None
    # No bytecode cache: a `__pycache__/` left in `scripts/lib/` is litter in
    # the code tree, and this import runs on every engine call.
    saved = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location('merlin_roots', _RESOLVER_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception:
        return None    # a resolver that will not import is skipped, never fatal
    finally:
        sys.dont_write_bytecode = saved
    try:
        return mod.resolve()['data_root']
    except Exception as e:
        _LAST_REFUSAL = str(e) or e.__class__.__name__
        return None


def _walk_for_registry(roots):
    """The plain search: at each level `core/task-lists.json` first, then a
    `manifest.yaml` that carries a `task_lists:` block."""
    for d in roots:
        j = os.path.join(d, 'core', 'task-lists.json')
        if os.path.exists(j):
            return _registry_from_build(j)
        m = os.path.join(d, 'manifest.yaml')
        if os.path.exists(m):
            try:
                return _registry_from_manifest(m)
            except TaskListError as e:
                if e.code == 'registry-missing-block':
                    continue   # some other manifest.yaml; keep walking
                raise
    return None


def load_registry(explicit=None, start=None, _cache=True):
    """Find the registry. Order:

      1. `explicit` (the `--registry` flag).
      2. `MERLIN_TASK_REGISTRY`.
      3. An ancestor walk from this engine for `core/task-lists.json` ONLY, for
         an engine copied beside its own JSON registry.
      4. `<data_root>/manifest.yaml`, when an optional roots resolver is in
         reach and names a data root.
      5. The plain walk: the engine's own ancestors, then the caller's start
         directory, both files accepted at every level. Reached when the
         resolver is absent, refuses, or names a manifest with no
         `task_lists:` block. With the list's own folder as the start, this is
         how a data folder's `manifest.yaml` is normally found.

    Two refusals:
      - the resolver refuses because it found two data folders with a
        manifest each (a move between folders left half done):
        `registry-split-unfinished`, raised at step 4, before the walk could
        load the stale one.
      - the resolver imported and refused, and the walk found nothing:
        `registry-unresolved`, instead of the empty single-list registry.
    """
    key = (explicit, start, os.environ.get('MERLIN_TASK_REGISTRY')) + tuple(
        os.environ.get(k) for k in _RESOLVER_ENV)
    if _cache and key in _CACHE:
        return _CACHE[key]
    cand = explicit or os.environ.get('MERLIN_TASK_REGISTRY') or None
    reg = None
    if cand:
        if not os.path.exists(cand):
            raise TaskListError('registry-missing',
                                'registry %s does not exist' % cand)
        reg = (_registry_from_build(cand) if cand.endswith('.json')
               else _registry_from_manifest(cand))
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        # 3. build-local registry beside a copied engine
        for d in _ancestors(here):
            j = os.path.join(d, 'core', 'task-lists.json')
            if os.path.exists(j):
                reg = _registry_from_build(j)
                break
        # 4. the data root's manifest, via the resolver
        refusal = None
        if reg is None:
            droot = data_root()
            refusal = _LAST_REFUSAL
            # Two data folders with a manifest each: a half-finished move. The walk below
            # could find the stale manifest and load it without a word, so this refuses
            # here instead.
            if refusal and _SPLIT_UNFINISHED in refusal:
                raise TaskListError('registry-split-unfinished', refusal)
            m = os.path.join(droot, 'manifest.yaml') if droot else None
            if m and os.path.exists(m):
                try:
                    reg = _registry_from_manifest(m)
                except TaskListError as e:
                    if e.code != 'registry-missing-block':
                        raise
        # 5. the plain walk
        if reg is None:
            roots = _ancestors(here)
            if start:
                for d in _ancestors(start):
                    if d not in roots:
                        roots.append(d)
            reg = _walk_for_registry(roots)
        # A resolver that imported and refused means a data tree is in reach and its
        # manifest is not. Single-list defaults there would route writes to the wrong file,
        # so this refuses loudly instead. No resolver at all, or one that will not import,
        # keeps the empty single-list registry.
        if reg is None and refusal:
            raise TaskListError('registry-unresolved',
                                'no task_lists registry could be found, and the Merlin roots '
                                'resolver refused: %s' % refusal)
        if reg is None:
            reg = _empty_registry()
    if _cache:
        _CACHE[key] = reg
    return reg


def clear_cache():
    _CACHE.clear()


# --- the list header ---------------------------------------------------------

def read_list_prefix(list_path, default='T'):
    """The `<!-- task-prefix: XX -->` header. A list with none has prefix T.

    That one rule covers a single shared list and any list written before
    prefixes existed, so none of them needs a special case.
    """
    try:
        with open(list_path, encoding='utf-8') as f:
            for i, line in enumerate(f):
                if i > 8:
                    break
                m = LIST_PREFIX_RE.match(line.rstrip('\n'))
                if m:
                    return m.group(1)
    except (IOError, OSError):
        return default
    return default


def list_header(prefix, next_id=1):
    return ['# Tasks', '<!-- task-prefix: %s -->' % prefix,
            '<!-- next-task-id: %d -->' % next_id, '']


def is_generated(path):
    """True when the file carries the GENERATED marker in its first few lines."""
    try:
        with open(path, encoding='utf-8') as f:
            for i, line in enumerate(f):
                if i > 4:
                    break
                if GENERATED_RE.match(line.lstrip('\ufeff')):
                    return True
    except (IOError, OSError):
        return False
    return False


# --- the local barrier -------------------------------------------------------

def barrier_for(list_path):
    """`<folder>/.task-ids/BARRIER` beside the list. None when absent."""
    d = os.path.dirname(os.path.abspath(list_path))
    p = os.path.join(d, CLAIMS_DIRNAME, BARRIER_NAME)
    return p if os.path.exists(p) else None


def check_barrier(list_path):
    p = barrier_for(list_path)
    if p:
        raise TaskListError(
            'barrier',
            'a rollback barrier is in place at %s. Nothing was written. This '
            'folder\'s task list is being merged back into the ledger; wait for '
            'the rollback to finish.' % p)


# --- the cut state -----------------------------------------------------------

def check_writable_state(reg, what='write'):
    if reg.state in (CUTTING, ROLLBACK):
        raise TaskListError(
            'state-refused',
            'task_lists.state is %s, so every %s is refused. Nothing was '
            'written. The migration barrier is up; re-run when the state reads '
            '%s or %s.' % (reg.state, what, PRE_CUT, ACTIVE))


def check_reachable(folder, what='write'):
    """Refuse when a registry row names a folder this session cannot open.

    A JSON registry beside a copied engine carries every folder's name and
    prefix and a reachable path for one folder only, so a row for any OTHER
    folder has no path at all. A process in that position cannot reach another
    folder's list, so the right move is to leave a pending outbox record for a
    later delivery run. Without this check, `Folder.list_path` would return a
    bare relative `TASKS.md` for such a row, which resolves against the CALLER's
    own working directory: a task filed for another folder would be written into
    the caller's own list and reported as `committed: true`.

    The refusal is the signal that an outbox record is the right next step, so it
    says so, and it carries a machine-readable code like every other refusal.
    """
    if folder is None:
        raise TaskListError(
            'no-folder', 'no registry row resolves for this %s. Nothing was '
            'written.' % what)
    if not folder.path:
        raise TaskListError(
            'unreachable-folder',
            '%s is a registered folder that this session cannot reach: the '
            'registry in reach (%s) carries its name and prefix so a target can '
            'be checked, and no path, because only this folder\'s own row has '
            'one. Nothing was written, and nothing was written to this folder\'s '
            'own list either. File it across the boundary instead: write an '
            'outbox record naming `target: %s` and the next pass run delivers it.'
            % (folder.name, 'core/task-lists.json', folder.name))


def same_file(a, b):
    if not a or not b:
        return False
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.abspath(a) == os.path.abspath(b)


def is_frozen_ledger(reg, path):
    return reg.frozen_ledger is not None and same_file(reg.frozen_ledger, path)


def rollup_path(reg):
    """The file a reader opens for all open tasks, by state."""
    if reg.state == ACTIVE:
        return reg.rollup
    return reg.frozen_ledger


# --- allocation lanes ---------------------------------------------------------

def machine_hash():
    """First 16 hex of sha256 of this machine's hardware UUID.

    The lane file alone does not make lanes disjoint, because nothing stops two
    machines writing lane 3 by hand. The manifest entry plus this hash is what
    makes the claim checkable, and a migrated home folder is what a mismatch
    looks like.
    """
    # Imported here and not at module scope. A caller's script named `select.py`
    # shadows the stdlib `select` module when it is run from its own directory,
    # and `subprocess` imports `selectors`, which imports `select`. A top-level
    # import here would break such a script the moment it imported triage.
    import subprocess
    uuid = os.environ.get('MERLIN_MACHINE_UUID')
    if not uuid:
        try:
            out = subprocess.check_output(
                ['/usr/sbin/ioreg', '-rd1', '-c', 'IOPlatformExpertDevice'],
                stderr=subprocess.DEVNULL).decode('utf-8', 'replace')
            m = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out)
            uuid = m.group(1) if m else None
        except Exception:
            uuid = None
    if not uuid:
        try:
            uuid = subprocess.check_output(['/bin/hostname'],
                                           stderr=subprocess.DEVNULL).decode().strip()
        except Exception:
            uuid = 'unknown-machine'
    return hashlib.sha256(uuid.encode('utf-8')).hexdigest()[:16]


def lane_file_path():
    return os.environ.get('MERLIN_TASK_LANE_FILE') or LANE_FILE


def read_lane():
    """(lane:int, machine:str) from the local file, or None when unenrolled."""
    try:
        with open(lane_file_path(), encoding='utf-8') as f:
            text = f.read()
    except (IOError, OSError):
        return None
    lane, mach = None, None
    for line in text.split('\n'):
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        if ':' in line:
            k, v = line.split(':', 1)
            k, v = k.strip(), v.strip()
            if k == 'lane':
                try:
                    lane = int(v)
                except ValueError:
                    return None
            elif k == 'machine':
                mach = v
        elif lane is None:
            try:
                lane = int(line)
            except ValueError:
                return None
    if lane is None or not (0 <= lane < MAX_LANES):
        return None
    return (lane, mach)


ENROLL_HINT = ('python3 %s --enroll   (run it once on this machine, with the '
               'shared folder fully synced)')


def require_lane(reg, prefix, claim_script):
    """The lane this process may allocate in, or None when no lane applies.

    A lane is required only for a prefixed id in `active`. An unprefixed `T` id
    never needs one, because no new `T` id can be minted in `active` anyway, and
    `pre-cut` (single-list) allocation needs no lane.
    """
    if reg.state != ACTIVE or prefix == 'T':
        return None
    local = read_lane()
    if local is None:
        raise TaskListError(
            'no-lane',
            'this machine has no allocation lane, so a %s id cannot be claimed '
            'without risking a duplicate against another machine. Nothing was '
            'written. Set it with:\n    %s'
            % (prefix, ENROLL_HINT % claim_script))
    lane, mach = local
    if mach and mach != machine_hash():
        raise TaskListError(
            'lane-machine-mismatch',
            'the lane file at %s records machine %s and this machine is %s. That '
            'is what a copied or migrated home folder looks like, and allocating '
            'in a lane another machine owns is exactly the duplicate this design '
            'removes. Nothing was written. Re-enrol with:\n    %s'
            % (lane_file_path(), mach, machine_hash(), ENROLL_HINT % claim_script))
    recorded = [e for e in reg.lanes if str(e.get('lane')) == str(lane)]
    if recorded and mach and recorded[0].get('machine') not in (None, mach):
        raise TaskListError(
            'lane-not-registered',
            'lane %d is recorded in the manifest against machine %s, not %s. '
            'Nothing was written.' % (lane, recorded[0].get('machine'), mach))
    return lane


def lane_candidates(lane, start):
    """The first candidate at or above `start` whose last digit is `lane`."""
    if lane is None:
        return start
    n = max(start, 1)
    while n % 10 != lane:
        n += 1
    return n


def next_in_lane(n, lane):
    return n + 1 if lane is None else n + 10


def fail(err, stream=None):
    """Print a refusal in the engine's JSON shape and exit 4.

    Same shape as the write-conflict and op-conflict guards above it in
    triage.py, because it is the same class of event: nothing was written and
    the caller has to do something different.
    """
    print(json.dumps({'error': err.code, 'message': err.message,
                      'committed': False}, ensure_ascii=False, indent=2),
          file=stream or sys.stderr)
    sys.exit(4)
