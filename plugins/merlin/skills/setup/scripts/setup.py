#!/usr/bin/env python3
"""Merlin setup helper. Run through setup.sh, which checks python3 first.

Every change outside the business folder is recorded in ~/.merlin/setup-record.json,
so `--remove` reverses exactly what setup added and nothing else. Honours a HOME
override (every home path comes from os.path.expanduser). Never reads a plugin variable:
the plugin folder is found from this file's own location.

Modes (one per run):
  --check                 report only; writes nothing anywhere
  --suggest [NAME]        print the folders to offer, in order. First the open folder (the
                          working folder, or --open DIR) unless it is home, Desktop, Documents or
                          Downloads itself, the top of a synced folder, a system or hidden folder,
                          a folder named Merlin, or inside another business folder. Then
                          <Dropbox>/<NAME>, or ~/Documents/<NAME> with no Dropbox (My Business
                          when no usable name is given). Reports a prepared folder (one that
                          already holds Merlin's starter files), and `looks like a project` when the
                          open folder holds brief.md, CONTINUITY.md or CLAUDE.md, with its parent when
                          the parent could be offered. When this Mac already has a business folder,
                          prints `existing business folder: <path>` and nothing else to choose.
                          Each folder it names gets an `HQ in plain words:` line: the
                          <folder name>-HQ setup creates inside it, the HQ already there, or an
                          older flat layout that gets no new folder. Writes nothing.
                          --offer-drive-icloud also lets Google Drive, then iCloud Drive, be
                          suggested when Dropbox is absent; off until they are tested.
  --inspect DIR           report what DIR already holds (a prepared folder's business name and
                          what is filled in; when Praxis left its record in
                          praxis-concierge/prepared.json in the run-state folder, also the date,
                          the owner's first name and the open tasks). Writes nothing.
  --choose DIR [--yes]    record DIR as the business folder: create it if needed and
                          write the pointer ~/.merlin/instance.json (preview without --yes).
                          Refuses when --data (the plugin option) names a different folder, and
                          when this Mac already has a business folder (the option or the pointer
                          names a folder whose about-me holds more than README.md) and DIR is
                          another one: that is the change-folder step, --change-to.
  --change-to DIR [--yes] [--name N]
                          the change-folder step: name the current business folder, refuse DIR for
                          the same reasons --suggest refuses an open folder (or when --data names
                          another folder), and preview the pointer change. With --yes, write the
                          pointer and fill in only the starter files DIR is missing (its HQ by the
                          marker, an older flat layout in place, else a new <folder name>-HQ).
                          Never moves, copies or deletes; the old folder stays as it is
  --skeleton-only         fill in the starter files (never overwrites), then report; no
                          questions, no settings changes. They go into the HQ folder found by
                          its marker; into an older flat install at its flat places, with no
                          HQ created beside it; and into a new <folder name>-HQ when the
                          business folder has neither
  --skeleton [--name N]   same copy, used by the interactive setup; N, the business name,
                          goes into a manifest.yaml this run creates
  --set-name NAME         write NAME into manifest.yaml, only where it still reads
                          `name: My Business`; a name already there is never changed
  --grant [--yes]         add the business folder to permissions.additionalDirectories in
                          ~/.claude/settings.json (preview without --yes; backup first)
  --codex-status          report Codex: installed, Merlin present, start-up hook trusted
  --codex-block [--yes]   add the managed Merlin block to ~/.codex/AGENTS.md (preview
                          without --yes)
  --key-store NAME        store a key in the macOS Keychain, service "Merlin"; the value
                          is read from stdin and never printed
  --key-check NAME        print "ok" or "missing", never the value
  --explain-memory        print the plain-words note about Claude Code's own memory
  --where                 print the business folder, its HQ folder (where Merlin's own files sit)
                          and every data path, as JSON. With no HQ yet, new_hq names the one
                          the starter files would go into, and the data paths are inside it.
                          Writes nothing.
  --move-to-hq [DIR] [--yes]
                          turn an older flat business folder (DIR, default the business folder)
                          into the HQ layout. Preview lists each rename of Merlin's own items
                          (TASKS.md, .task-ids/, ABOUT-ME/, memory/, state/) into a new
                          <folder name>-HQ and each manifest edit; writes nothing. With --yes,
                          renames them in place, writes the marked manifest into the HQ in one
                          step, then renames the old manifest.yaml into the move record's folder
                          as manifest-before-move.yaml (never deleted). Keeps a record in
                          merlin-setup/move-to-hq.json in the run-state folder, then fills in
                          missing starter files. A move that stopped part-way is picked up from
                          its record when this runs again. Refuses a folder that is not the flat
                          layout, an existing <folder name>-HQ or target, and a folder it cannot
                          write. Nothing else in the folder is touched
  --remove [--yes] [--keys A,B]
                          reverse what setup and the start-up hook wrote outside the
                          business folder (preview without --yes); never touches that folder
Common: --data DIR, the plugin option (default: the folder named in ~/.merlin/instance.json)

Who writes the pointer: merlin:setup (--choose), after the user's yes in chat, and the
start-up hook, only while the plugin option is set. The option wins when both exist.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

BUILD_MARKER = '<!-- Generated by the plugin build.'
KEYCHAIN_SERVICE = 'Merlin'
CODEX_BEGIN = '<!-- BEGIN MERLIN (managed by merlin:setup; remove with merlin:setup --remove) -->'
CODEX_END = '<!-- END MERLIN -->'
CODEX_TRUST_RE = re.compile(r'^\[hooks\.state\."[^"]*/merlin/[^"]*hooks\.json:session_start:', re.M)

MEMORY_NOTE = """Claude's own memory, in plain words:
- What it is: Claude Code keeps its own short notes for each folder you open, without asking.
- Where it lives: on this computer, in ~/.claude/projects/<folder name>/memory/. The first 200 lines of the MEMORY.md file there load at the start of every session in that folder.
- What it may hold: how-to notes about this computer only, such as where a program is installed.
- Which one wins: your business folder, always. A decision, a preference or a project status goes into the business folder. When a Claude memory disagrees with a file there, the file wins and the memory gets corrected.
- Merlin does not move or change Claude's memory folder."""


# ------------------------------------------------------------------ paths

def home(*parts):
    return os.path.join(os.path.expanduser('~'), *parts)


POINTER = home('.merlin', 'instance.json')
RECORD = home('.merlin', 'setup-record.json')
SETTINGS = home('.claude', 'settings.json')
CODEX_DIR = home('.codex')
CODEX_AGENTS = home('.codex', 'AGENTS.md')


def plugin_root(explicit=None):
    if explicit:
        return os.path.abspath(explicit)
    d = os.path.dirname(os.path.abspath(__file__))
    while True:
        if os.path.isfile(os.path.join(d, '.claude-plugin', 'plugin.json')):
            return d
        nxt = os.path.dirname(d)
        if nxt == d:
            return None
        d = nxt


def canon(p):
    """Absolute, with ~ expanded, symlinks resolved through the deepest existing folder."""
    p = os.path.abspath(os.path.expanduser(p))
    head, tail = p, []
    while not os.path.exists(head):
        head, t = os.path.split(head)
        if not t:
            break
        tail.insert(0, t)
    return os.path.join(os.path.realpath(head), *tail)


def read_pointer():
    try:
        with open(POINTER, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def data_folder(arg):
    if arg and not arg.startswith('${') and arg.strip():
        return canon(arg.strip()), 'option'
    ptr = read_pointer()
    if ptr and ptr.get('data_root'):
        return canon(ptr['data_root']), 'pointer'
    return None, None


def sha256_file(p):
    with open(p, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def load_record():
    try:
        with open(RECORD, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_record(rec):
    os.makedirs(os.path.dirname(RECORD), exist_ok=True)
    tmp = RECORD + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(rec, f, indent=2)
        f.write('\n')
    os.replace(tmp, RECORD)


def stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def writable_dir(d):
    return os.path.isdir(d) and os.access(d, os.W_OK | os.X_OK)


def say(line=''):
    print(line)


# ------------------------------------------------------------------ checks

def pointer_check(data):
    """Report only. The pointer is written by --choose, or by the start-up hook while the
    plugin option is set."""
    ptr = read_pointer()
    if ptr is None:
        if os.path.exists(POINTER):
            return 'problem', 'The pointer file ~/.merlin/instance.json exists but cannot be read. Run setup\'s folder step to record the business folder again.'
        return 'missing', 'The pointer file ~/.merlin/instance.json does not exist yet. Run setup\'s folder step to record this business folder.'
    named = ptr.get('data_root')
    if data and named and canon(named) == data:
        return 'ok', 'The pointer file ~/.merlin/instance.json names this business folder.'
    return 'mismatch', 'The pointer file ~/.merlin/instance.json names %s, not this business folder. The plugin setting wins: the start-up hook rewrites the pointer from it at the next session start.' % named


def settings_has(data):
    try:
        with open(SETTINGS, encoding='utf-8') as f:
            s = json.load(f)
    except (OSError, ValueError):
        return False
    dirs = (s.get('permissions') or {}).get('additionalDirectories') or []
    return any(isinstance(x, str) and canon(x) == data for x in dirs)


def codex_status():
    present = os.path.isdir(CODEX_DIR)
    installed = False
    cache = os.path.join(CODEX_DIR, 'plugins', 'cache')
    if os.path.isdir(cache):
        for mkt in os.listdir(cache):
            if os.path.isdir(os.path.join(cache, mkt, 'merlin')):
                installed = True
    trusted = False
    try:
        with open(os.path.join(CODEX_DIR, 'config.toml'), encoding='utf-8') as f:
            trusted = bool(CODEX_TRUST_RE.search(f.read()))
    except OSError:
        pass
    block = False
    try:
        with open(CODEX_AGENTS, encoding='utf-8') as f:
            block = CODEX_BEGIN in f.read()
    except OSError:
        pass
    return {'codex': present, 'merlin_installed': installed, 'hook_trusted': trusted, 'block_present': block}


# ------------------------------------------------------------------ choose

def plugin_version(root):
    try:
        with open(os.path.join(root, '.claude-plugin', 'plugin.json'), encoding='utf-8') as f:
            return json.load(f).get('version') or 'unknown'
    except (OSError, ValueError, TypeError):
        return 'unknown'


def nearest_existing(p):
    while not os.path.exists(p):
        nxt = os.path.dirname(p)
        if nxt == p:
            break
        p = nxt
    return p


FALLBACK_NAME = 'My Business'


def suggest_name(name):
    """The folder name to suggest for a business: its own name, made safe as one folder name.

    The folder holds the whole business (about-me, memory, tasks, every project), so it is
    named after the business and never after Merlin: a folder named after the tool reads as
    deletable when the tool is removed. Falls back to FALLBACK_NAME.
    """
    n = ''.join(' ' if (ch in '/:\\' or ord(ch) < 32) else ch for ch in (name or ''))
    n = ' '.join(n.split()).strip(' .')
    if len(n) > 60:
        n = n[:60].rstrip(' .')
    if not n or n.casefold() == 'merlin':
        return FALLBACK_NAME
    return n


def is_within(p, parent):
    return p == parent or p.startswith(parent.rstrip(os.sep) + os.sep)


def tilde(p):
    h = canon(home())
    return '~' + p[len(h):] if is_within(p, h) and p != h else ('~' if p == h else p)


def dropbox_roots():
    """Every Dropbox folder on this Mac, real paths, the plain `Dropbox` folder first."""
    cands = []
    cs = home('Library', 'CloudStorage')
    if os.path.isdir(cs):
        cands += [os.path.join(cs, n) for n in sorted(os.listdir(cs))
                  if n.startswith('Dropbox') and os.path.isdir(os.path.join(cs, n))]
    if os.path.isdir(home('Dropbox')):
        cands.append(home('Dropbox'))
    out = []
    for c in cands:
        r = canon(c)
        if r not in out:
            out.append(r)
    return sorted(out, key=lambda r: os.path.basename(r) != 'Dropbox')


def drive_roots():
    cs = home('Library', 'CloudStorage')
    if not os.path.isdir(cs):
        return []
    return [canon(os.path.join(cs, n, 'My Drive')) for n in sorted(os.listdir(cs))
            if n.startswith('GoogleDrive-') and os.path.isdir(os.path.join(cs, n, 'My Drive'))]


def icloud_root():
    p = home('Library', 'Mobile Documents', 'com~apple~CloudDocs')
    return canon(p) if os.path.isdir(p) else None


def backed_up_place(drive_icloud):
    """(service, folder) to suggest the business folder in, or (None, None).

    Dropbox always comes first. Google Drive and iCloud Drive can keep files online-only, and
    neither has been tested with Merlin yet, so they count only with --offer-drive-icloud.
    """
    dbx = dropbox_roots()
    if dbx:
        return 'Dropbox', dbx[0]
    if drive_icloud:
        drv = drive_roots()
        if drv:
            return 'Google Drive', drv[0]
        ic = icloud_root()
        if ic:
            return 'iCloud Drive', ic
    return None, None


def service_name(provider):
    for prefix, svc in (('Dropbox', 'Dropbox'), ('GoogleDrive-', 'Google Drive'), ('OneDrive', 'OneDrive')):
        if provider.startswith(prefix):
            return svc
    return provider


PLAIN_PLACES = (('Desktop', 'your Desktop'), ('Documents', 'your Documents folder'), ('Downloads', 'your Downloads folder'))


def library_reason(p):
    """Why a folder inside ~/Library is not offered, or None when it sits inside a synced folder."""
    cs = canon(home('Library', 'CloudStorage'))
    ic = canon(home('Library', 'Mobile Documents', 'com~apple~CloudDocs'))
    if is_within(p, cs) and p != cs:
        parts = os.path.relpath(p, cs).split(os.sep)
        if len(parts) >= (3 if parts[0].startswith('GoogleDrive-') else 2):
            return None
        return 'it is the top of your %s, not a folder inside it' % service_name(parts[0])
    if is_within(p, ic):
        return None if p != ic else 'it is the top of your iCloud Drive, not a folder inside it'
    return "it is one of your Mac's own system folders"


# ------------------------------------------------------------------ the HQ folder

# Merlin's own files sit in one child of the business folder, its HQ, found by one top-level
# line in its manifest.yaml and never by its name. The same rule is written in the start-up
# hook and in the task engine's tasklists.py; the plugin's tests hold the three together with
# one fixture matrix, so a change here is a change in both of them.
#
# The match is on bytes, so `\s` is the six ASCII whitespace characters in all three places.
HQ_MARKER_RE = re.compile(br'^kind:\s*["\']?merlin-hq["\']?\s*(#.*)?$')
AMBIGUOUS = 'ambiguous'
LAYOUTS = {1: 'hq', 2: 'hq', 3: 'flat', 4: 'none', AMBIGUOUS: AMBIGUOUS}


def has_hq_marker(manifest):
    """True when MANIFEST is a file with a top-level `kind: merlin-hq` line. A trailing
    carriage return is stripped first; a manifest that cannot be read has no marker."""
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


def find_hq(d):
    """Where Merlin's own files sit for folder D: (rule, business, hq, clash).

      1  D is an HQ: its manifest carries the marker. Business is D's parent.
      2  D holds exactly one HQ: one immediate child's manifest carries it.
      3  the older flat layout: D has a manifest.yaml without the marker. Business and HQ are D.
      4  nothing: not set up. HQ is None.
      'ambiguous'  two or more children carry the marker. CLASH lists them, sorted.

    Hidden children are never looked at. A child reached through a symlink counts, and is
    named by its path inside D.
    """
    own = os.path.join(d, 'manifest.yaml')
    if os.path.isfile(own) and has_hq_marker(own):
        return 1, os.path.dirname(d), d, []
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
        return AMBIGUOUS, d, None, hqs
    if hqs:
        return 2, d, hqs[0], []
    if os.path.isfile(own):
        return 3, d, d, []
    return 4, d, None, []


DATA_KEYS = ('manifest', 'about_me', 'memory', 'task_list', 'task_ids', 'run_state')


def new_hq_name(d):
    """The name setup gives an HQ it creates in folder D. Nothing ever finds an HQ by it."""
    return '%s-HQ' % os.path.basename(d.rstrip(os.sep))


def hq_paths(hq):
    """The data paths inside an HQ folder: context/ for what you tell Merlin, live/ for what runs."""
    return {'manifest': os.path.join(hq, 'manifest.yaml'),
            'about_me': os.path.join(hq, 'context', 'ABOUT-ME'),
            'memory': os.path.join(hq, 'context', 'memory'),
            'task_list': os.path.join(hq, 'live', 'TASKS.md'),
            'task_ids': os.path.join(hq, 'live', '.task-ids'),
            'run_state': os.path.join(hq, 'live', 'state')}


def flat_paths(d):
    """The data paths of the older flat layout (1.0.11 installs): all in the business folder."""
    return {'manifest': os.path.join(d, 'manifest.yaml'),
            'about_me': os.path.join(d, 'ABOUT-ME'),
            'memory': os.path.join(d, 'memory'),
            'task_list': os.path.join(d, 'TASKS.md'),
            'task_ids': os.path.join(d, '.task-ids'),
            'run_state': os.path.join(d, 'state')}


def paths(d):
    """Every Merlin data path for folder D, by its layout. The one place a data path is joined.

    An HQ keeps its files in context/ and live/; the older flat layout keeps them in D itself.
    With no HQ yet (rule 4), `hq` stays None, `new_hq` names the HQ setup would create
    (<D name>-HQ inside D) and the data paths are the ones inside it. With two HQs (ambiguous)
    the data paths are None.
    """
    rule, business, hq, clash = find_hq(d)
    out = {'folder': d, 'rule': rule, 'layout': LAYOUTS[rule], 'business': business, 'hq': hq,
           'new_hq': None, 'ambiguous': clash}
    if rule in (1, 2):
        out.update(hq_paths(hq))
    elif rule == 3:
        out.update(flat_paths(d))
    elif rule == 4:
        out['new_hq'] = os.path.join(d, new_hq_name(d))
        out.update(hq_paths(out['new_hq']))
    else:
        out.update({k: None for k in DATA_KEYS})
    return out


def clash_words(clash):
    return ', '.join('"%s"' % os.path.basename(c) for c in clash)


# What to do about two HQ folders. Renaming one does not help: Merlin finds its HQ folder by a line in
# the folder's settings file, never by the folder's name.
TWO_HQ = ('each one is marked as Merlin\'s HQ in its settings file (manifest.yaml), so Merlin cannot tell '
          'which one holds its files')
TWO_HQ_FIX = ('Keep one and move the others out of the business folder (renaming one does not help, and a '
              'backup copy belongs outside the business folder)')
HQ_WORDS = 'Merlin\'s HQ folder (the folder where Merlin keeps its own files)'


def is_prepared(d):
    """A folder Merlin already set up. An HQ, or a folder holding one, counts on its marker
    alone. The older flat layout counts with its manifest plus a task list, a claim folder or
    ABOUT-ME."""
    p = paths(d)
    if p['rule'] in (1, 2):
        return True
    return p['rule'] == 3 and any(os.path.exists(p[k]) for k in ('task_ids', 'task_list', 'about_me'))


def business_of(d):
    """The business folder D belongs to when D is set up (an HQ, a folder holding one or two,
    or a prepared flat folder), else None."""
    rule, business, _hq, _clash = find_hq(d)
    if rule in (1, 2, AMBIGUOUS) or (rule == 3 and is_prepared(d)):
        return business
    return None


def enclosing_business(p):
    """The business folder P sits inside (never P itself), or None."""
    ptr = read_pointer() or {}
    named = canon(ptr['data_root']) if ptr.get('data_root') else None
    if named:
        # A pointer that names an HQ stands for the business folder around it.
        named = business_of(named) or named
    if named and is_within(p, named) and p != named:
        return named
    parts = p.split(os.sep)
    for i in range(len(parts) - 1, 0, -1):
        above = os.sep.join(parts[:i]) or os.sep
        owner = business_of(above)
        if owner and owner != p and is_within(p, owner):
            return owner
    return None


def open_folder_check(raw, allow_missing=False):
    """(offered, words): words is the plain place when offered, else the plain reason.

    ALLOW_MISSING (the change-folder step) lets a folder that does not exist yet pass when every
    other check passes and the nearest folder above it can be written: the step creates it."""
    if not raw or not raw.strip():
        return False, 'no open folder was given'
    p = canon(raw.strip())
    h = canon(home())
    if not os.path.isdir(p) and (not allow_missing or os.path.lexists(p)):
        return False, 'it is not a folder on this Mac'
    if p == h:
        return False, 'it is your home folder itself'
    if is_within(h, p):
        return False, 'it holds your whole home folder'
    for d, words in PLAIN_PLACES:
        if p == canon(home(d)):
            return False, 'it is %s itself' % words
    for r in dropbox_roots():
        if p == r:
            return False, 'it is the top of your Dropbox, not a folder inside it'
    if is_within(p, canon(home('Library'))):
        why = library_reason(p)
        if why:
            return False, why
    elif is_within(p, h) and any(x.startswith('.') for x in os.path.relpath(p, h).split(os.sep)):
        return False, 'it is a hidden folder'
    if os.path.basename(p).casefold() == 'merlin':
        return False, 'it is named Merlin, and a folder named after the tool reads as safe to delete when the tool is removed'
    owner = enclosing_business(p)
    if owner:
        return False, 'it is inside another business folder, "%s"' % os.path.basename(owner)
    if not writable_dir(p if os.path.isdir(p) else nearest_existing(p)):
        return False, 'this computer account cannot write to it'
    return True, place_words(p)


def synced_places():
    """[(root, plain words)] for every synced folder found, whatever the switch says."""
    places = [(r, 'your Dropbox') for r in dropbox_roots()] + [(r, 'your Google Drive') for r in drive_roots()]
    ic = icloud_root()
    if ic:
        places.append((ic, 'your iCloud Drive'))
    return places


def in_synced(p):
    return any(is_within(p, r) for r, _ in synced_places())


def place_words(p):
    """'"Acmeco" on your Desktop', in plain words, never a path."""
    name, parent = os.path.basename(p), os.path.dirname(p)
    for d, words in PLAIN_PLACES:
        if parent == canon(home(d)):
            return '"%s" %s' % (name, 'on your Desktop' if d == 'Desktop' else 'in %s' % words)
    if parent == canon(home()):
        return '"%s" in your home folder' % name
    for r, words in synced_places():
        if parent == r:
            return '"%s" in %s' % (name, words)
        if is_within(parent, r):
            return '"%s" in the folder "%s" in %s' % (name, os.path.basename(parent), words)
    return '"%s" in the folder "%s"' % (name, os.path.basename(parent) or parent)


def manifest_name(d):
    """The business name in folder D's manifest (the HQ's, or the flat one), or None."""
    manifest = paths(d)['manifest']
    if not manifest:
        return None
    try:
        with open(manifest, encoding='utf-8') as f:
            text = f.read()
    except OSError:
        return None
    m = re.search(r'^name:[ \t]*(.+?)[ \t]*$', text, re.M)
    if not m:
        return None
    v = m.group(1)
    q = re.match(r'"((?:[^"\\]|\\.)*)"', v)
    if q:
        try:
            v = json.loads(q.group(0))
        except ValueError:
            v = q.group(1)
    else:
        v = re.sub(r'\s+#.*$', '', v).strip("'")
    return v.strip() or None


# The record Praxis leaves in a folder it prepared, inside the run-state folder.
RECEIPT = os.path.join('praxis-concierge', 'prepared.json')
NAME_PLACEHOLDERS = ('name: %s' % FALLBACK_NAME, 'name: %s' % json.dumps(FALLBACK_NAME))


def read_receipt(d):
    """The record Praxis leaves in a folder it prepared, as (date, first name or None), or None.

    A missing, unreadable, truncated or odd-shaped record counts as no record at all, so the
    report is then exactly what it is for any prepared folder.
    """
    run_state = paths(d)['run_state']
    if not run_state:
        return None
    try:
        with open(os.path.join(run_state, RECEIPT), encoding='utf-8') as f:
            rec = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict):
        return None
    when = rec.get('prepared_on')
    if not isinstance(when, str) or not re.match(r'^\d{4}-\d{2}-\d{2}$', when):
        return None
    try:
        datetime.date.fromisoformat(when)
    except ValueError:
        return None
    first = rec.get('owner_first_name')
    first = ' '.join(first.split()) if isinstance(first, str) else ''
    return when, first or None


def open_tasks(d):
    """Open tasks on the folder's task list: the live `- [ ]` lines in TASKS.md."""
    task_list = paths(d)['task_list']
    if not task_list:
        return 0
    try:
        with open(task_list, encoding='utf-8') as f:
            text = f.read()
    except (OSError, ValueError):
        return 0
    text = re.sub(r'<!--.*?-->', '', text, flags=re.S)
    return len(re.findall(r'^- \[ \] ', text, re.M))


def prepared_lines(d):
    """What a prepared business folder already holds, one line each."""
    receipt = read_receipt(d)
    if receipt:
        lines = ['Prepared folder: yes. It already holds Merlin\'s starter files, so it was set up before, '
                 'by Praxis. Setup keeps every file in it and fills in only what is missing.']
        when, first = receipt
        lines.append('Prepared by Praxis on %s%s' % (when, ' for %s' % first if first else ''))
    else:
        lines = ['Prepared folder: yes. It already holds Merlin\'s starter files, so it was set up before, '
                 'possibly by Praxis. Setup keeps every file in it and fills in only what is missing.']
    p = paths(d)
    name = manifest_name(d)
    if name and name != FALLBACK_NAME:
        lines.append('Business name in this folder: %s' % name)
    filled = []
    if os.path.isfile(os.path.join(p['about_me'], 'about-me.md')):
        filled.append('notes about you')
    notes = 0
    for dirpath, _, files in os.walk(p['memory']):
        notes += sum(1 for f in files if f.endswith('.md') and f != 'README.md')
    if notes:
        filled.append('%d company note%s' % (notes, '' if notes == 1 else 's'))
    try:
        with open(p['manifest'], encoding='utf-8') as f:
            projects = len(re.findall(r'^\s+-\s+name:', f.read(), re.M))
    except OSError:
        projects = 0
    if projects:
        filled.append('%d project%s' % (projects, '' if projects == 1 else 's'))
    lines.append('Already filled in: %s' % ('; '.join(filled) if filled else 'nothing yet beyond the starter files'))
    if receipt:
        n = open_tasks(d)
        lines.append('Task list: %s' % ('no open tasks' if n == 0 else '%d open task%s' % (n, '' if n == 1 else 's')))
    return lines


def set_name(data, name):
    """Write the business name into a manifest.yaml that still has the starter name.

    Replaces only the starter line `name: My Business` (or its quoted form). A name that is
    already there is never changed.
    """
    name = ' '.join((name or '').split())
    if not name:
        say('Business name: no name was given. Nothing was written.')
        return 2
    if name == FALLBACK_NAME:
        say('Business name: "%s" is the starter name, not a business name. Nothing was written.' % FALLBACK_NAME)
        return 2
    blocked = move_blocker(data)
    if blocked:
        say(blocked)
        return 2
    p = paths(data)
    if p['rule'] == AMBIGUOUS:
        say('Business name: %s holds more than one HQ folder (%s): %s. %s. Nothing was written.'
            % (data, clash_words(p['ambiguous']), TWO_HQ, TWO_HQ_FIX))
        return 2
    path = p['manifest'] if p['rule'] != 4 else None
    where = os.path.dirname(path) if path else data
    try:
        if path is None:
            raise OSError('no manifest yet')
        with open(path, encoding='utf-8', newline='') as f:
            text = f.read()
    except (OSError, ValueError):
        say('Business name: Merlin\'s settings file (manifest.yaml) is not in %s or cannot be read, so there is '
            'nowhere to write the name. Run setup\'s starter-files step first. Nothing was written.' % where)
        return 2
    m = re.search(r'^name:[^\r\n]*', text, re.M)
    if not m:
        say('Business name: Merlin\'s settings file (manifest.yaml) in %s has no name line. Nothing was written.' % where)
        return 2
    if m.group(0) not in NAME_PLACEHOLDERS:
        have = manifest_name(data)
        if have == name:
            say('Business name: already "%s". Nothing to change.' % name)
            return 0
        say('Business name: this folder already has a name, "%s". Setup never changes a name that is '
            'already there, so nothing was written. To change it, edit the name line in Merlin\'s settings file '
            '(manifest.yaml).' % have)
        return 2
    if not os.access(path, os.W_OK) or not writable_dir(where):
        say('Business name: read-only. This computer account cannot write Merlin\'s settings file (manifest.yaml) in %s. '
            'Nothing was written.' % where)
        return 2
    new = text[:m.start()] + 'name: %s' % json.dumps(name) + text[m.end():]
    tmp = path + '.merlin-tmp'
    with open(tmp, 'w', encoding='utf-8', newline='') as f:
        f.write(new)
    shutil.copymode(path, tmp)
    os.replace(tmp, path)
    if manifest_name(data) != name:
        with open(tmp, 'w', encoding='utf-8', newline='') as f:
            f.write(text)
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
        say('Business name: the name could not be read back the same way, so Merlin\'s settings file (manifest.yaml) '
            'was put back as it was.')
        return 1
    say('Business name: set to "%s" in Merlin\'s settings file (manifest.yaml). Nothing else in the folder changed.' % name)
    return 0


def folder_status(path):
    if not os.path.exists(path):
        return ['It does not exist yet. Setup creates it after the user says yes.']
    if not os.path.isdir(path):
        return ['A file already has that name, so it cannot be the folder. Suggest another name.']
    rule, _business, _hq, clash = find_hq(path)
    if rule == AMBIGUOUS:
        return ['It holds more than one HQ folder (%s): %s. %s, then run setup again.'
                % (clash_words(clash), TWO_HQ, TWO_HQ_FIX)]
    if is_prepared(path):
        return prepared_lines(path)
    try:
        items = [x for x in os.listdir(path) if not x.startswith('.')]
    except OSError:
        return ['It exists, but this computer account cannot read it. Suggest another folder.']
    if items:
        return ['It exists and holds %d item(s). Setup adds its starter files beside them and overwrites nothing.' % len(items)]
    return ['It exists and is empty.']


ONE_MAC = ('Use the business folder from one Mac at a time: two Macs writing to it at once can leave '
           '"conflicted copy" files.')
CHANGE_STEP = 'say: change my business folder'


def option_value(option):
    """The plugin option as a folder, or None when it is unset or still an unfilled ${...}."""
    return option.strip() if option and not option.startswith('${') and option.strip() else None


def holds_about_me(d):
    """True when D's about-me folder (through paths()) holds any file other than README.md."""
    about = paths(d)['about_me']
    if not about or not os.path.isdir(about):
        return False
    for dirpath, dirnames, filenames in os.walk(about):
        dirnames[:] = [n for n in dirnames if not n.startswith('.')]
        if any(f != 'README.md' and not f.startswith('.') for f in filenames):
            return True
    return False


def existing_business(option):
    """The business folder this Mac already uses, or None. Pointer-based only, never a disk search:
    the folder the plugin option names, else the one the pointer names, when it is there and its
    about-me holds a file other than README.md, or when it holds two HQ folders. A folder named as an
    HQ stands for its business."""
    named = []
    opt = option_value(option)
    if opt:
        named.append(canon(opt))
    ptr = read_pointer() or {}
    if ptr.get('data_root'):
        named.append(canon(ptr['data_root']))
    for d in named:
        if not os.path.isdir(d):
            continue
        # Two HQ folders mean setup ran there before, whatever their about-me folders hold.
        if find_hq(d)[0] == AMBIGUOUS or holds_about_me(d):
            return business_of(d) or d
    return None


def same_business(p, business):
    """True when folder P is BUSINESS itself or the HQ folder inside it."""
    return p == business or business_of(p) == business


PROJECT_FILES = ('brief.md', 'CONTINUITY.md', 'CLAUDE.md')


def project_reason(d):
    """Why folder D looks like one project rather than a whole business, or None."""
    have = [n for n in PROJECT_FILES if os.path.isfile(os.path.join(d, n))]
    if not have:
        return None
    return 'it holds %s' % (' and '.join(have) if len(have) < 3 else '%s, %s and %s' % tuple(have))


def hq_words(d):
    """One plain line on where Merlin's own files go in business folder D, for the question that
    offers D, or None when there is no single answer (two HQ folders, a file, or D is an HQ)."""
    if os.path.lexists(d) and not os.path.isdir(d):
        return None
    p = paths(d)
    if p['rule'] == 4:
        return ('Merlin will keep its own files in a folder it creates inside it, called "%s". '
                'Your projects go next to that folder.' % os.path.basename(p['new_hq']))
    if p['rule'] == 2:
        return ('Merlin\'s own files are already in the folder "%s" inside it, so setup creates no new '
                'folder. Your projects go next to that folder.' % os.path.basename(p['hq']))
    if p['rule'] == 3:
        return ('Merlin\'s own files already sit in this folder itself, the way an older Merlin kept them, '
                'so setup creates no new folder.')
    return None


def suggest(name, open_dir, drive_icloud, option=None):
    """Print the folders to offer, in order: the open folder, then a backed-up place. Writes nothing.

    When this Mac already has a business folder, name it and offer nothing else."""
    existing = existing_business(option)
    if existing:
        say('existing business folder: %s' % existing)
        biz = manifest_name(existing)
        if biz and biz != FALLBACK_NAME:
            say('Business name in this folder: %s' % biz)
        hq = hq_words(existing)
        if hq:
            say('HQ folder in plain words: %s' % hq)
        say('This Mac already has a business folder, with notes about you in it. Merlin uses one business '
            'folder. Setup can run again on that folder. To use a different folder instead, %s.' % CHANGE_STEP)
        return 0
    folder = suggest_name(name)
    if open_dir is not None:
        offered, words = open_folder_check(open_dir)
        say('Open folder: %s' % (canon(open_dir) if open_dir.strip() else '(none)'))
        if offered:
            say('Open folder offer: yes')
            say('Open folder in plain words: %s' % words)
            od = canon(open_dir)
            why = None if is_prepared(od) else project_reason(od)
            if why:
                parent = os.path.dirname(od)
                up_ok, _ = open_folder_check(parent)
                say('looks like a project: %s%s' % (why, '; parent: %s' % parent if up_ok else ''))
            for ln in folder_status(od):
                say(ln)
            hq = hq_words(od)
            if hq:
                say('Open folder HQ in plain words: %s' % hq)
        else:
            say('Open folder offer: no (%s)' % words)
    svc, base = backed_up_place(drive_icloud)
    if svc:
        path = os.path.join(base, folder)
        say('Backed-up place: %s (%s)' % (svc, tilde(base)))
        say('Suggested business folder: %s' % tilde(path))
        say('Suggested in plain words: a folder named "%s" in your %s, which keeps it backed up.' % (folder, svc))
    else:
        docs = canon(home('Documents'))
        path = os.path.join(docs if os.path.isdir(docs) else canon(home()), folder)
        say('Backed-up place: none found (no Dropbox on this Mac)')
        say('Suggested business folder: %s' % tilde(path))
        say('Suggested in plain words: a folder named "%s" in your %s. Make sure it is backed up, for '
            'example with Time Machine.' % (folder, 'Documents folder' if os.path.isdir(docs) else 'home folder'))
    for ln in folder_status(path):
        say(ln)
    hq = hq_words(path)
    if hq:
        say('Suggested folder HQ in plain words: %s' % hq)
    if svc or (open_dir is not None and open_dir.strip() and in_synced(canon(open_dir))):
        say(ONE_MAC)
    say('Never move the business folder after setup records it. If it does move, run setup again to record the new place.')
    return 0


def inspect(target):
    """Report what a folder the user names already holds. Writes nothing."""
    p = canon(target)
    say('Folder: %s' % p)
    if os.path.isdir(p):
        offered, words = open_folder_check(p)
        if not offered:
            say('Not usable as the business folder: %s.' % words)
    for ln in folder_status(p):
        say(ln)
    return 0


def business_for(want):
    """WANT, or the business folder around it when WANT is an HQ folder (its manifest carries the
    marker). The pointer always names the business folder, so an HQ named here is never recorded."""
    if os.path.isdir(want):
        rule, business, _hq, _clash = find_hq(want)
        if rule == 1:
            say('setup: %s is %s, so setup uses the business folder around it, %s.' % (want, HQ_WORDS, business))
            return business
    return want


def choose(root, target, option, yes):
    """Record TARGET as the business folder: create it and write the pointer."""
    raw = (target or '').strip()
    if not raw:
        say('setup: say which folder to use, for example --choose "~/%s". Nothing was written.' % FALLBACK_NAME)
        return 2
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        say('setup: %s is not a full path. Use a path that starts with / or ~/, for example "~/%s". Nothing was written.' % (raw, FALLBACK_NAME))
        return 2
    want = business_for(canon(expanded))
    blocked = move_blocker(want)
    if blocked:
        say(blocked)
        return 2
    opt = option_value(option)
    if opt and canon(opt) != want:
        say('setup: the plugin setting "Your business folder" names %s, and that setting wins. '
            'To use %s instead, change the plugin setting. Nothing was written.' % (canon(opt), want))
        return 2
    existing = existing_business(option)
    if existing and not same_business(want, existing):
        say('setup: this Mac already has a business folder, %s, with notes about you in it. Merlin uses one '
            'business folder, so setup does not record a second one. Setup can run again on %s. To use %s '
            'instead, use the change-folder step (%s). Nothing was written.' % (existing, existing, want, CHANGE_STEP))
        return 2
    if os.path.exists(want) and not os.path.isdir(want):
        say('setup: %s is a file, not a folder. Pick another place. Nothing was written.' % want)
        return 2
    base = nearest_existing(want)
    if not os.path.isdir(want) and not writable_dir(base):
        say('setup: read-only. %s does not exist and %s cannot be written by this account. Nothing was written.' % (want, base))
        return 2
    if os.path.isdir(want) and not writable_dir(want):
        say('setup: read-only. This computer account cannot write to %s. Nothing was written.' % want)
        return 2
    ptr = read_pointer() or {}
    named = canon(ptr['data_root']) if ptr.get('data_root') else None
    steps = []
    if not os.path.isdir(want):
        steps.append('create the folder %s' % want)
    if named == want:
        pass
    elif named:
        steps.append('change the pointer file ~/.merlin/instance.json from %s to %s' % (named, want))
    else:
        steps.append('write the pointer file ~/.merlin/instance.json naming %s' % want)
    say('Business folder: %s' % want)
    if not steps:
        say('Already recorded: the folder exists and the pointer file names it. Nothing to do.')
        return 0
    say('Setup will:')
    for i, st in enumerate(steps, 1):
        say('  %d. %s' % (i, st))
    say('No file inside the folder is changed by this step.')
    if not yes:
        say('Preview only. Nothing was changed. Run again with --yes after the user says yes.')
        return 0
    if not os.path.isdir(want):
        os.makedirs(want)
    write_pointer(root, want)
    say('Done. The folder exists and ~/.merlin/instance.json names it. It counts from now on, in this session too.')
    return 0


def write_pointer(root, want):
    """Write ~/.merlin/instance.json naming WANT, atomically, and note it in setup's record."""
    os.makedirs(os.path.dirname(POINTER), exist_ok=True)
    body = {
        'data_root': want,
        'praxis_root': want,
        'written_by': 'merlin plugin %s, merlin:setup' % plugin_version(root) if root else 'merlin:setup',
        'written_at': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'data_access': 'local',
    }
    tmp = POINTER + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(body, f, indent=2, ensure_ascii=False)
        f.write('\n')
    os.replace(tmp, POINTER)
    rec = load_record()
    rec['pointer_written'] = True
    save_record(rec)


def skeleton_blocker(d):
    """Why the starter files cannot be filled in folder D, or None. The same refusals
    copy_skeleton makes, checked before anything is written."""
    if not os.path.isdir(d):
        return None
    p = paths(d)
    if p['rule'] == AMBIGUOUS:
        return 'it holds more than one HQ folder (%s): %s' % (clash_words(p['ambiguous']), TWO_HQ)
    if p['rule'] == 4:
        new = p['new_hq']
        if os.path.lexists(new) and not os.path.isdir(new):
            return 'it already holds a file named "%s", the name setup gives Merlin\'s HQ folder' % os.path.basename(new)
        if os.path.lexists(p['manifest']):
            return ('it already holds a folder named "%s" whose settings file (manifest.yaml) is not marked as '
                    'Merlin\'s HQ' % os.path.basename(new))
    return None


def change_to(root, target, option, yes, biz_name=None):
    """The change-folder step: record TARGET as the business folder in place of the current one,
    then fill in only the starter files TARGET is missing. Preview without YES. Never moves,
    copies or deletes anything, in the old folder or the new one."""
    raw = (target or '').strip()
    if not raw:
        say('setup: say which folder to change to, for example --change-to "~/Dropbox/%s". Nothing was written.' % FALLBACK_NAME)
        return 2
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        say('setup: %s is not a full path. Use a path that starts with / or ~/. Nothing was written.' % raw)
        return 2
    want = business_for(canon(expanded))
    blocked = move_blocker(want)
    if blocked:
        say(blocked)
        return 2
    ptr = read_pointer() or {}
    named = canon(ptr['data_root']) if ptr.get('data_root') else None
    opt = option_value(option)
    current = existing_business(option) or (canon(opt) if opt else None) or named
    say('Current business folder: %s' % (current or 'none recorded yet'))
    say('New business folder: %s' % want)
    if opt and canon(opt) != want:
        say('setup: the plugin setting "Your business folder" names %s, and that setting wins. To use %s '
            'instead, change the plugin setting. Nothing was written.' % (canon(opt), want))
        return 2
    if current and same_business(want, current) and named == want:
        say('Already the business folder: the pointer file names it. Nothing to change. To check it, run setup again.')
        return 0
    ok, words = open_folder_check(want, allow_missing=True)
    if not ok:
        say('setup: %s cannot be the business folder: %s. Nothing was written.' % (want, words))
        return 2
    blocker = skeleton_blocker(want)
    if blocker:
        say('setup: %s cannot be the business folder yet: %s. Move the extra folder out of it, then try again. '
            'Nothing was written.' % (want, blocker))
        return 2
    steps = []
    if not os.path.isdir(want):
        steps.append('create the folder %s' % want)
    if named:
        steps.append('change the pointer file ~/.merlin/instance.json from %s to %s' % (named, want))
    else:
        steps.append('write the pointer file ~/.merlin/instance.json naming %s' % want)
    p = paths(want) if os.path.isdir(want) else None
    if p and p['rule'] in (1, 2):
        steps.append('fill in only the starter files missing from its HQ folder (the folder where Merlin keeps '
                     'its own files), %s' % p['hq'])
    elif p and p['rule'] == 3:
        steps.append('fill in only the starter files missing from it, where Merlin\'s files already sit: loose in '
                     'the business folder, the way older versions kept them. No HQ folder is created')
    else:
        steps.append('create Merlin\'s HQ folder inside it, %s, holding the starter files. That is the folder '
                     'where Merlin keeps its own files' % os.path.join(want, new_hq_name(want)))
    say('Setup will:')
    for i, st in enumerate(steps, 1):
        say('  %d. %s' % (i, st))
    say('No file that already exists is overwritten.')
    if current and current != want:
        say('The old folder, %s, stays as it is: nothing in it is moved, copied or deleted.' % current)
    if not yes:
        say('Preview only. Nothing was changed. Run again with --yes after the user says yes.')
        return 0
    if not os.path.isdir(want):
        os.makedirs(want)
    write_pointer(root, want)
    created, kept = copy_skeleton(root, want, biz_name)
    say('Done. ~/.merlin/instance.json names %s. Starter files: %d created, %d already there and left as they '
        'were. It counts from now on, in this session too.' % (want, len(created), len(kept)))
    if current and current != want:
        say('The old folder, %s, was not changed.' % current)
    return 0


# ------------------------------------------------------------------ move to the HQ layout

# The task engine's lock files, held while --move-to-hq renames, so no engine run writes the old
# places part-way through.
ENGINE_LOCKS = ('TASKS.md.lock', 'manifest.yaml.lock')
MOVE_DIR = 'merlin-setup'
MOVE_RECORD = os.path.join(MOVE_DIR, 'move-to-hq.json')
# Where the move puts the business folder's old manifest.yaml once the marked copy is in the HQ: a
# rename, never a delete, into the move record's own folder.
MANIFEST_BEFORE = os.path.join(MOVE_DIR, 'manifest-before-move.yaml')
# What each rewritten manifest key means, for the preview the user reads.
EDIT_WORDS = {('projects', 'path'): 'where to find a project',
              ('task_lists', 'frozen_ledger'): 'where to find the task list',
              ('task_lists', 'rollup'): 'where to find the combined task list',
              ('task_lists', 'path'): 'where to find a folder\'s task list'}
TOP_KEY_RE = re.compile(r'^(?P<key>[A-Za-z_][\w-]*)[ \t]*:(?P<rest>.*)$')
# A `key: value` pair on one manifest line, block or flow style. The value is quoted, or bare up
# to a comma, a closing brace or bracket, or a comment.
PAIR_RE = re.compile(r'(?P<pre>(?:^|(?<=[\s{,]))(?P<key>[A-Za-z_]+)[ \t]*:[ \t]*)'
                     r'(?P<val>"(?:[^"\\]|\\.)*"|\'[^\']*\'|[^\s"\',{}\[\]#][^,{}\[\]#]*?)'
                     r'(?=[ \t]*(?:[,}\]]|#|$))')


def _scalar_in(val):
    """(text, quote) for a YAML scalar as written: quote is '"', "'" or ''."""
    if val.startswith('"'):
        try:
            return json.loads(val), '"'
        except ValueError:
            return val[1:-1], '"'
    if val.startswith("'"):
        return val[1:-1].replace("''", "'"), "'"
    return val, ''


def _scalar_out(text, quote):
    if quote == '"':
        return json.dumps(text, ensure_ascii=False)
    if quote == "'":
        return "'%s'" % text.replace("'", "''")
    return text


def _relative(p):
    """P with any leading ./ removed, or None when P is not relative to the manifest's folder
    (absolute, from home, or already pointing one level up)."""
    if not p or os.path.isabs(p) or p.startswith('~') or p == '..' or p.startswith('../'):
        return None
    while p.startswith('./'):
        p = p[2:]
    return p


def project_path(p):
    """A path written relative to the business folder, rewritten relative to an HQ inside it."""
    q = _relative(p)
    if q is None:
        return None
    return '..' if q in ('', '.') else '../' + q


def live_path(p):
    """A task list path written relative to the business folder, rewritten to the HQ's live/."""
    q = _relative(p)
    if q is None or q in ('', '.') or q == 'live' or q.startswith('live/'):
        return None
    return './live/' + q


def _top_blocks(lines):
    """{key: (first, last)} for each top-level key: its own line and the last line of its block."""
    out = {}
    cur = None
    for i, raw in enumerate(lines):
        l = raw.rstrip('\r')
        if not l.strip() or l.lstrip().startswith('#'):
            continue
        if l.startswith((' ', '\t', '-')):
            if cur:
                out[cur] = (out[cur][0], i)
            continue
        m = TOP_KEY_RE.match(l)
        cur = m.group('key') if m else None
        if cur and cur not in out:
            out[cur] = (i, i)
        elif cur:
            cur = None
    return out


def _rewrite_line(line, rules, changes, where):
    """LINE with each `key: value` pair whose key is in RULES rewritten by RULES[key]."""
    eol = '\r' if line.endswith('\r') else ''
    body = line[:-1] if eol else line
    if body.lstrip().startswith('#'):
        return line

    def sub(m):
        fn = rules.get(m.group('key'))
        if not fn:
            return m.group(0)
        text, quote = _scalar_in(m.group('val'))
        new = fn(text)
        if new is None or new == text:
            return m.group(0)
        label = EDIT_WORDS.get((where, m.group('key')), '%s %s' % (where, m.group('key')))
        changes.append('%s: %s becomes %s' % (label, text, new))
        return m.group('pre') + _scalar_out(new, quote)

    return PAIR_RE.sub(sub, body) + eol


def hq_manifest_text(text):
    """(new text, changes): a flat business folder's manifest as the HQ's manifest.

    Adds the `kind: merlin-hq` line, points the task_lists paths at live/ and every project path
    (and task_lists folder path) one level up, because the HQ sits inside the business folder.
    Every other line is kept byte for byte."""
    lines = text.split('\n')
    eol = '\r' if any(l.endswith('\r') for l in lines) else ''
    changes = []
    blocks = _top_blocks(lines)
    if 'projects' in blocks:
        a, b = blocks['projects']
        for i in range(a, b + 1):
            lines[i] = _rewrite_line(lines[i], {'path': project_path}, changes, 'projects')
    inserts = []
    if 'task_lists' in blocks:
        a, b = blocks['task_lists']
        rules = {'frozen_ledger': live_path, 'rollup': live_path, 'path': project_path}
        for i in range(a, b + 1):
            lines[i] = _rewrite_line(lines[i], rules, changes, 'task_lists')
        rest = TOP_KEY_RE.match(lines[a].rstrip('\r')).group('rest')
        if not rest.split('#', 1)[0].strip():
            found = [re.match(r'^([ \t]+)([A-Za-z_]+)[ \t]*:', lines[i]) for i in range(a + 1, b + 1)]
            found = [m for m in found if m]
            indent = found[0].group(1) if found else '  '
            keys = {m.group(2) for m in found if m.group(1) == indent}
            for key, value in (('frozen_ledger', './live/TASKS.md'), ('rollup', './live/task-rollup.md')):
                if key not in keys:
                    inserts.append((b + 1, '%s%s: %s%s' % (indent, key, value, eol)))
                    changes.append('add %s: %s' % (EDIT_WORDS[('task_lists', key)], value))
    else:
        tail = ['task_lists:', '  state: pre-cut', '  frozen_ledger: ./live/TASKS.md', '  rollup: ./live/task-rollup.md']
        while lines and not lines[-1].strip():
            lines.pop()
        lines += [l + eol for l in tail] + ['']
        changes.append('add the task list settings (task_lists): one task list, at ./live/TASKS.md, and the combined '
                       'task list at ./live/task-rollup.md')
    for at, line in reversed(inserts):
        lines.insert(at, line)
    mark = 'kind: merlin-hq' + eol
    blocks = _top_blocks(lines)
    if 'kind' in blocks:
        i = blocks['kind'][0]
        changes.insert(0, 'change the line %s so it marks the settings file of Merlin\'s HQ folder (kind: merlin-hq)'
                       % lines[i].rstrip('\r'))
        lines[i] = mark
    else:
        at = blocks['name'][1] + 1 if 'name' in blocks else 0
        lines.insert(at, mark)
        changes.insert(0, 'mark it as the settings file of Merlin\'s HQ folder (the line kind: merlin-hq)')
    return '\n'.join(lines), changes


def move_plan(d, hq):
    """[(name, target, is_dir)]: Merlin's own items in the flat folder D, in the order --move-to-hq
    renames them, each with its place in the new HQ. The data items, plus the task engine's side
    files beside the list and the manifest when they are there. state/ goes first, because the move
    record is written into it before anything moves and travels with it. The manifest itself is not
    renamed into the HQ: its marked copy is written there in one step, and only then is the old one
    renamed aside (see _execute_move)."""
    to = hq_paths(hq)
    live = os.path.dirname(to['task_list'])
    return [('state', to['run_state'], True), ('TASKS.md', to['task_list'], False),
            ('TASKS.md.bak', os.path.join(live, 'TASKS.md.bak'), False),
            ('TASKS.md.lock', os.path.join(live, 'TASKS.md.lock'), False),
            ('.task-tmp', os.path.join(live, '.task-tmp'), True), ('.task-ids', to['task_ids'], True),
            ('ABOUT-ME', to['about_me'], True), ('memory', to['memory'], True),
            ('manifest.yaml.lock', os.path.join(hq, 'manifest.yaml.lock'), False)]


def engine_busy(*folders):
    """(held, busy): the engine's lock files in FOLDERS, locked by this process, and whether a task
    engine run holds one right now. A lock file that is not there is not created."""
    import fcntl
    held = []
    for d in folders:
        for name in ENGINE_LOCKS:
            p = os.path.join(d, name)
            if not os.path.isfile(p) or os.path.islink(p):
                continue
            try:
                fd = os.open(p, os.O_RDWR)
            except OSError:
                continue
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                for h in held:
                    os.close(h)
                return [], True
            held.append(fd)
    return held, False


def write_json_atomic(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write('\n')
    os.replace(tmp, path)


def _move_record(path):
    """The move record at PATH when it is there and its status is not "complete", else None. A record
    that cannot be read counts as unfinished, as the start-up hook counts it, and comes back as {}."""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding='utf-8') as f:
            rec = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(rec, dict):
        return {}
    return None if rec.get('status') == 'complete' else rec


def unfinished_moves(d):
    """[(record path, record)]: each unfinished move record that belongs to folder D. The places are
    the ones the start-up hook checks: D's own flat run-state folder, D's HQ run-state folder when D
    is an HQ, and the HQ run-state folder of every child that is not hidden."""
    places = [os.path.join(d, 'state', MOVE_RECORD), os.path.join(d, 'live', 'state', MOVE_RECORD)]
    try:
        names = sorted(os.listdir(d))
    except OSError:
        names = []
    places += [os.path.join(d, n, 'live', 'state', MOVE_RECORD) for n in names
               if not n.startswith('.') and os.path.isdir(os.path.join(d, n))]
    out = []
    for path in places:
        rec = _move_record(path)
        if rec is not None:
            out.append((path, rec))
    return out


def move_blocker(d):
    """The plain refusal for a step that would write into folder D while a move into its HQ folder is
    unfinished, or None. Every writing step but the move itself refuses then, because it would
    write to places the move has half changed."""
    if not d or not os.path.isdir(d):
        return None
    pending = unfinished_moves(d)
    if not pending:
        return None
    return ('setup: moving Merlin\'s own files into the HQ folder of %s (the folder where Merlin keeps its own '
            'files) stopped before it finished. Nothing was deleted. The move record, %s, lists what moved. Finish '
            'the move first (say: move my Merlin files into the HQ folder), then run this again. Nothing was written.'
            % (d, pending[0][0]))


MOVE_HELP = ('To finish it, run merlin:setup\'s move step again (say: move my Merlin files into the HQ folder), '
             'which picks up where it stopped, or ask for help.')


def move_problem(rec_path):
    """The plain words for a move that stopped part-way, naming its record."""
    return ('Moving Merlin\'s own files into its HQ folder (the folder where Merlin keeps its own files) stopped '
            'before it finished. Nothing was deleted. The move record, %s, lists what moved and what did not. %s'
            % (rec_path, MOVE_HELP))


def _say_plan(d, hq, renames, manifest_changes, aside, record):
    """Print the steps a move will take: RENAMES [(from, to, is_dir)] relative to D, the manifest
    edits (None when the HQ's manifest is already written), and the old manifest's rename aside."""
    rel = lambda p: os.path.relpath(p, d)
    if renames:
        say('Setup will %srename these items into it:' % ('' if os.path.isdir(hq) else 'create that folder and '))
        for i, (src, t, is_dir) in enumerate(renames, 1):
            slash = '/' if is_dir else ''
            say('  %d. rename %s%s to %s%s' % (i, rel(src), slash, rel(t), slash))
    if manifest_changes is not None:
        say('Then it will write Merlin\'s settings file (manifest.yaml) into the HQ folder, in one step, with these '
            'changes:')
        for c in manifest_changes:
            say('  - %s' % c)
    if aside:
        say('Last, it will rename the business folder\'s old settings file to %s, so nothing is deleted.' % rel(aside))
    say('It keeps a record of the move in %s.' % rel(record))
    say('Nothing else in the folder is moved, copied, edited or deleted. Your projects stay where they are.')


def move_to_hq(root, target, data, yes):
    """Turn an older flat business folder into the HQ layout: rename Merlin's own items into a new
    <folder name>-HQ inside it, write the marked manifest there, then rename the old manifest aside.
    Preview without YES. Renames and one new file only, on the same disk; nothing else in the folder
    is moved, copied, edited or deleted. A move that stopped part-way is picked up from its record."""
    raw = (target or '').strip()
    if raw:
        expanded = os.path.expanduser(raw)
        if not os.path.isabs(expanded):
            say('setup: %s is not a full path. Use a path that starts with / or ~/. Nothing was written.' % raw)
            return 2
        d = canon(expanded)
    elif data:
        d = data
    else:
        say('setup: no business folder is recorded yet, so there is nothing to move. Name the folder, for example '
            '--move-to-hq "~/Dropbox/%s". Nothing was written.' % FALLBACK_NAME)
        return 2
    say('Business folder: %s' % d)
    if not os.path.isdir(d):
        say('setup: %s is not a folder. Nothing was written.' % d)
        return 2
    pending = unfinished_moves(d)
    if pending:
        return resume_move(root, d, pending, yes)
    p = paths(d)
    if p['rule'] == AMBIGUOUS:
        say('setup: %s already holds more than one HQ folder (%s): %s. %s. Nothing was written.'
            % (d, clash_words(p['ambiguous']), TWO_HQ, TWO_HQ_FIX))
        return 2
    if p['rule'] in (1, 2):
        say('setup: %s is already in the HQ layout: Merlin\'s own files sit in %s. Nothing to move. Nothing was written.'
            % (d, p['hq']))
        return 2
    if p['rule'] == 4:
        say('setup: %s is not set up: Merlin\'s settings file (manifest.yaml) is not in it, so there is nothing to '
            'move. Run setup to set it up. Nothing was written.' % d)
        return 2
    hq = os.path.join(d, new_hq_name(d))
    to = hq_paths(hq)
    moves = []
    for name, target, is_dir in move_plan(d, hq):
        src = os.path.join(d, name)
        if not os.path.lexists(src):
            continue
        if os.path.isdir(src) != is_dir or os.path.islink(src):
            say('setup: %s is %s, where Merlin keeps %s. Move it aside first. Nothing was written.'
                % (src, 'a link' if os.path.islink(src) else ('a folder' if not is_dir else 'a file'),
                   'a folder' if is_dir else 'a file'))
            return 2
        moves.append((name, src, target, is_dir))
    taken = [t for t in [t for _n, _s, t, _i in moves] + [to['manifest']] if os.path.lexists(t)]
    if taken:
        say('setup: these places in the new HQ folder already exist: %s. Nothing was written.'
            % ', '.join(os.path.relpath(t, d) for t in taken))
        return 2
    if os.path.lexists(hq):
        say('setup: %s already holds "%s", the name of the HQ folder the move would create. Rename or remove it first. '
            'Nothing was written.' % (d, os.path.basename(hq)))
        return 2
    if not writable_dir(d):
        say('setup: read-only. This computer account cannot write to %s. Nothing was written.' % d)
        return 2
    disk = os.stat(d).st_dev
    elsewhere = [s for _n, s, _t, _i in moves if os.lstat(s).st_dev != disk]
    if elsewhere:
        say('setup: %s sits on another disk than the business folder, so it cannot be renamed in place. Nothing was written.'
            % ', '.join(elsewhere))
        return 2
    flat_manifest = os.path.join(d, 'manifest.yaml')
    try:
        with open(flat_manifest, encoding='utf-8', newline='') as f:
            old_text = f.read()
    except (OSError, ValueError) as e:
        say('setup: cannot read Merlin\'s settings file, %s: %s. Nothing was written.' % (flat_manifest, e))
        return 2
    _new_text, changes = hq_manifest_text(old_text)
    record = os.path.join(to['run_state'], MOVE_RECORD)
    aside = os.path.join(to['run_state'], MANIFEST_BEFORE)
    say('New HQ folder: %s' % hq)
    _say_plan(d, hq, [(s, t, i) for _n, s, t, i in moves], changes, aside, record)
    held, busy = engine_busy(d)
    if busy or not yes:
        for fd in held:
            os.close(fd)
    if busy:
        say('setup: the task engine is writing the task list right now. Try again in a minute. Nothing was written.')
        return 2
    if not yes:
        say('Preview only. Nothing was changed. Run again with --yes after the user says yes.')
        return 0
    if root is None:
        for fd in held:
            os.close(fd)
        say('setup: cannot find the plugin folder from %s. Nothing was written.' % __file__)
        return 2

    # The record goes first: into the flat state/ folder when there is one, so it travels with that
    # folder's rename, else straight into the new HQ's run-state folder. It lists every planned rename
    # and is rewritten as each one lands, so a stop part-way through can be picked up again.
    rec = {'schema': 2, 'step': 'merlin:setup --move-to-hq', 'business': d, 'hq': hq,
           'started_at': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
           'status': 'in progress',
           'renames': [{'from': os.path.relpath(s, d), 'to': os.path.relpath(t, d), 'done': False} for _n, s, t, _i in moves],
           'manifest_edit': changes, 'manifest_edited': False,
           'manifest_before_move': os.path.relpath(aside, d), 'manifest_moved_aside': False,
           'note': 'Until status says complete, each rename marked done has landed and the others have not. '
                   'Running merlin:setup\'s move step again picks up where it stopped. To undo by hand, rename '
                   'each done item from its "to" place back to its "from" place, and manifest_before_move back to '
                   'manifest.yaml.'}
    state_moves = moves and moves[0][0] == 'state'
    rec_path = os.path.join(moves[0][1], MOVE_RECORD) if state_moves else record
    todo = [(i, s, t) for i, (_n, s, t, _d) in enumerate(moves)]
    try:
        return _execute_move(root, d, hq, rec, rec_path, todo, True, True)
    finally:
        for fd in held:
            os.close(fd)


def resume_move(root, d, pending, yes):
    """Pick up a move into an HQ folder that stopped part-way, from its record. Each rename still to
    do is read from the disk, not only from the record: an item still at its old place and not at its
    new one is renamed; one at its new place only has landed. Anything else is refused."""
    stuck = ('setup: an earlier move of Merlin\'s own files into its HQ folder (the folder where Merlin keeps '
             'its own files) stopped before it finished, and %s, so setup cannot pick it up safely. Nothing was '
             'deleted, and nothing was written now. Ask for help, and give them the record: %s.')
    if len(pending) > 1:
        say(stuck % ('there is more than one move record', ', '.join(x for x, _r in pending)))
        return 2
    rec_path, rec = pending[0]
    hq = rec.get('hq')
    renames = rec.get('renames')
    ok = (isinstance(hq, str) and os.path.isabs(hq) and os.path.dirname(hq) == d and rec.get('business') == d
          and isinstance(renames, list)
          and all(isinstance(r, dict) and isinstance(r.get('from'), str) and isinstance(r.get('to'), str)
                  and r['from'] and r['to'] and not os.path.isabs(r['from']) and not os.path.isabs(r['to'])
                  and '..' not in r['from'].split(os.sep) + r['to'].split(os.sep) for r in renames))
    if not ok:
        say(stuck % ('its record cannot be read or does not belong to this folder', rec_path))
        return 2
    to = hq_paths(hq)
    todo = []
    for i, r in enumerate(renames):
        src, t = os.path.join(d, r['from']), os.path.join(d, r['to'])
        here, there = os.path.lexists(src), os.path.lexists(t)
        if here and not there:
            todo.append((i, src, t))
        elif there and not here:
            r['done'] = True
        else:
            say(stuck % ('%s is %s' % (r['from'], 'in both its old and its new place' if here else 'in neither its old '
                                       'nor its new place'), rec_path))
            return 2
    flat_manifest = os.path.join(d, 'manifest.yaml')
    aside = os.path.join(to['run_state'], MANIFEST_BEFORE)
    marked = has_hq_marker(to['manifest'])
    has_flat = os.path.lexists(flat_manifest)
    if not marked and (not has_flat or os.path.lexists(to['manifest'])):
        say(stuck % ('Merlin\'s settings file (manifest.yaml) is not where the move left it', rec_path))
        return 2
    if marked and has_flat and os.path.lexists(aside):
        say(stuck % ('the old settings file is in both its old and its new place', rec_path))
        return 2
    changes = None
    if not marked:
        try:
            with open(flat_manifest, encoding='utf-8', newline='') as f:
                _t, changes = hq_manifest_text(f.read())
        except (OSError, ValueError):
            say(stuck % ('Merlin\'s settings file (manifest.yaml) cannot be read', rec_path))
            return 2
    say('An earlier move of Merlin\'s own files into its HQ folder, %s, stopped before it finished. Nothing was '
        'deleted. Its record: %s.' % (hq, rec_path))
    rel_record = os.path.join(to['run_state'], MOVE_RECORD)
    if not todo and marked and not has_flat:
        say('Every item is already in its new place. Setup will check the HQ folder and fill in any missing '
            'starter file.')
    else:
        say('Setup will finish it.')
    _say_plan(d, hq, [(s, t, os.path.isdir(s)) for _i, s, t in todo], changes, aside if has_flat else None, rel_record)
    held, busy = engine_busy(d, os.path.dirname(to['task_list']), hq)
    if busy or not yes:
        for fd in held:
            os.close(fd)
    if busy:
        say('setup: the task engine is writing the task list right now. Try again in a minute. Nothing was written.')
        return 2
    if not yes:
        say('Preview only. Nothing was changed. Run again with --yes after the user says yes.')
        return 0
    if root is None:
        for fd in held:
            os.close(fd)
        say('setup: cannot find the plugin folder from %s. Nothing was written.' % __file__)
        return 2
    rec['status'] = 'in progress'
    rec['resumed_at'] = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    try:
        return _execute_move(root, d, hq, rec, rec_path, todo, not marked, has_flat)
    finally:
        for fd in held:
            os.close(fd)


def _execute_move(root, d, hq, rec, rec_path, todo, write_manifest, move_aside):
    """The --yes half of a move, run while the engine's lock files are held. TODO [(index, from, to)]
    are the renames still to do. Then, when WRITE_MANIFEST, the marked manifest goes into the HQ by a
    temp file and one rename, so the HQ never holds a manifest without the marker; and only then, when
    MOVE_ASIDE, the business folder's old manifest is renamed into the record's folder. Until that
    last rename the old manifest stays where it was, so every reader sees either the flat layout or
    the finished HQ, never a folder with no manifest. A failure stops with plain words, the record
    says where it stopped, and nothing is deleted."""
    to = hq_paths(hq)
    flat_manifest = os.path.join(d, 'manifest.yaml')
    aside = os.path.join(to['run_state'], MANIFEST_BEFORE)
    # The first record write. When it fails nothing has moved yet, so the folder is put back exactly as
    # it was: any folder this step created for the record is removed again while it is still empty.
    parts = os.path.relpath(os.path.dirname(rec_path), d).split(os.sep)
    made = [p for p in (os.path.join(d, *parts[:n]) for n in range(len(parts), 0, -1)) if not os.path.isdir(p)]
    try:
        os.makedirs(os.path.dirname(rec_path), exist_ok=True)
        write_json_atomic(rec_path, rec)
    except OSError as e:
        for p in [rec_path + '.tmp'] + made:
            try:
                (os.rmdir if p in made else os.remove)(p)
            except OSError:
                pass
        say('setup: the move could not start: the record it keeps of the move could not be written (%s). Nothing '
            'was moved, and the business folder is as it was. Fix the cause, then run the move again. Nothing was '
            'written.' % (getattr(e, 'strerror', None) or e))
        return 1
    step = 'writing the move record'
    try:
        for sub in ('context', 'live'):
            step = 'creating the folder %s' % os.path.relpath(os.path.join(hq, sub), d)
            os.makedirs(os.path.join(hq, sub), exist_ok=True)
        for i, src, t in todo:
            step = 'renaming %s to %s' % (os.path.relpath(src, d), os.path.relpath(t, d))
            os.rename(src, t)
            rec['renames'][i]['done'] = True
            if rec_path == src or is_within(rec_path, src):
                rec_path = t + rec_path[len(src):]
            step = 'writing the move record'
            write_json_atomic(rec_path, rec)
        if write_manifest:
            step = 'writing Merlin\'s settings file (manifest.yaml) into the HQ folder'
            with open(flat_manifest, encoding='utf-8', newline='') as f:
                new_text, changes = hq_manifest_text(f.read())
            tmp = to['manifest'] + '.move-tmp'
            with open(tmp, 'w', encoding='utf-8', newline='') as f:
                f.write(new_text)
            os.replace(tmp, to['manifest'])
            rec['manifest_edit'] = changes
            rec['manifest_edited'] = True
            step = 'writing the move record'
            write_json_atomic(rec_path, rec)
        if move_aside:
            step = 'renaming the old settings file (manifest.yaml) to %s' % os.path.relpath(aside, d)
            os.makedirs(os.path.dirname(aside), exist_ok=True)
            os.rename(flat_manifest, aside)
            rec['manifest_moved_aside'] = True
            step = 'writing the move record'
            write_json_atomic(rec_path, rec)
    except (OSError, ValueError) as e:
        rec['status'] = 'stopped while %s' % step
        try:
            write_json_atomic(rec_path, rec)
        except OSError:
            pass
        say('setup: the move stopped part-way, while %s (%s). Nothing was deleted. The move record, %s, lists '
            'what moved and what did not. %s' % (step, getattr(e, 'strerror', None) or e, rec_path, MOVE_HELP))
        return 1

    after = paths(d)
    lost = [os.path.join(d, r['to']) for r in rec['renames'] if not os.path.lexists(os.path.join(d, r['to']))]
    if after['rule'] != 2 or after['hq'] != hq or lost:
        rec['status'] = 'stopped: the check after the move failed'
        write_json_atomic(rec_path, rec)
        say('setup: the move did not check out: %s. Nothing was deleted. The move record, %s, lists what moved. %s'
            % ('missing ' + ', '.join(lost) if lost else 'the folder does not read as having one HQ folder', rec_path,
               MOVE_HELP))
        return 1
    try:
        created, kept = copy_skeleton(root, d, during_move=True)
    except OSError as e:
        rec['status'] = 'stopped while filling in the starter files'
        write_json_atomic(rec_path, rec)
        say('setup: the move stopped part-way, while filling in the starter files (%s). Nothing was deleted. The move '
            'record, %s, lists what moved. %s' % (e.strerror or e, rec_path, MOVE_HELP))
        return 1
    missing = [after[k] for k in DATA_KEYS if not os.path.exists(after[k])]
    if missing:
        rec['status'] = 'stopped: starter files missing after the move'
        write_json_atomic(rec_path, rec)
        say('setup: after the move these are still missing: %s. Nothing was deleted. The move record, %s, lists what '
            'moved. %s' % (', '.join(os.path.relpath(m, d) for m in missing), rec_path, MOVE_HELP))
        return 1
    rec['status'] = 'complete'
    rec['completed_at'] = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    rec['starter_files_created'] = created
    write_json_atomic(rec_path, rec)
    say('Done. Merlin\'s own files now sit in the HQ folder %s.' % hq)
    say('Renamed: %d items. Settings file written with %d changes; the old one is kept as %s. Starter files: %d '
        'created, %d already there and left as they were.'
        % (len(rec['renames']), len(rec.get('manifest_edit') or []), os.path.relpath(aside, d), len(created), len(kept)))
    say('Move record: %s' % rec_path)
    say('Nothing else in %s was moved, copied, edited or deleted.' % d)
    return 0


# ------------------------------------------------------------------ skeleton

def skeleton_files(src):
    """[(key, rel, source)] for every starter file in SRC: the data path it belongs to (a key of
    paths()) and its place inside that path ('' for the path itself). The plugin's own starter
    files are laid out as an HQ, so their places come from paths() like an installed HQ's."""
    places = paths(src)
    if places['rule'] != 1:
        raise SystemExit('setup: the starter files in the plugin folder (%s) are not laid out as an HQ folder. '
                         'Reinstall the plugin.' % src)
    out = []
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames.sort()
        for name in sorted(filenames):
            if name == '.DS_Store':
                continue
            f = os.path.join(dirpath, name)
            key = next((k for k in DATA_KEYS if f == places[k] or is_within(f, places[k])), None)
            if key is None:
                raise SystemExit('setup: the starter file %s has no place in Merlin\'s layout. Reinstall the plugin.'
                                 % os.path.relpath(f, src))
            out.append((key, '' if f == places[key] else os.path.relpath(f, places[key]), f))
    return out


def copy_skeleton(root, data, biz_name=None, during_move=False):
    """Fill in Merlin's starter files for the business folder DATA. Never overwrites a file that exists.

    Where they go follows the HQ rule, through paths():
      - an HQ found by its marker (DATA is one, or holds one): missing files are filled in inside it;
      - the older flat layout: missing files are filled in at their flat places, and no HQ is
        created beside it;
      - nothing yet: DATA gets a new HQ folder, <DATA name>-HQ, holding every starter file;
      - two HQs: refused, nothing is written.
    BIZ_NAME, when given, replaces the default `name:` line in a manifest this run creates.
    """
    # The starter files sit one level down, in skeleton/HQ/, laid out as an HQ folder. Never at
    # skeleton/ itself: the plugin folder is one of the task engine's own ancestors, and the engine
    # looks for an HQ among the children of each ancestor, so a marked manifest in skeleton/ would
    # be taken for the user's HQ.
    src = os.path.join(root, 'skeleton', 'HQ')
    if not os.path.isdir(src):
        raise SystemExit('setup: the starter files are missing from the plugin folder (%s). Reinstall the plugin.' % src)
    files = skeleton_files(src)
    parent = os.path.dirname(data)
    if not os.path.isdir(data):
        if not writable_dir(parent):
            raise SystemExit('setup: read-only. The business folder %s does not exist and its parent folder cannot be written. Nothing was written.' % data)
        os.makedirs(data)
    if not writable_dir(data):
        raise SystemExit('setup: read-only. This computer account cannot write to %s. Nothing was written.' % data)
    # The move step fills in starter files as its own last step, while its record is still open.
    blocked = None if during_move else move_blocker(data)
    if blocked:
        raise SystemExit(blocked)
    p = paths(data)
    if p['rule'] == AMBIGUOUS:
        raise SystemExit('setup: %s holds more than one HQ folder (%s): %s. %s, then run setup again. '
                         'Nothing was written.' % (data, clash_words(p['ambiguous']), TWO_HQ, TWO_HQ_FIX))
    if p['rule'] == 4:
        new = p['new_hq']
        if os.path.lexists(new) and not os.path.isdir(new):
            raise SystemExit('setup: %s already holds a file named "%s", the name setup gives Merlin\'s HQ folder. '
                             'Rename that file, then run setup again. Nothing was written.' % (data, os.path.basename(new)))
        if os.path.lexists(p['manifest']):
            raise SystemExit('setup: %s already holds a folder named "%s" whose settings file (manifest.yaml) is not '
                             'marked as Merlin\'s HQ. Rename that folder, then run setup again. Nothing was written.'
                             % (data, os.path.basename(new)))
    created, kept = [], []
    for key, rel, f in files:
        t = os.path.join(p[key], rel) if rel else p[key]
        rel_t = os.path.relpath(t, data)
        here = data
        for part in os.path.relpath(os.path.dirname(t), data).split(os.sep):
            if part in ('', '.'):
                continue
            here = os.path.join(here, part)
            if not os.path.isdir(here):
                os.mkdir(here)
                created.append(os.path.relpath(here, data) + '/')
        if os.path.lexists(t):
            kept.append(rel_t)
            continue
        with open(f, encoding='utf-8') as fh:
            lines = [ln for ln in fh.read().split('\n') if not ln.startswith(BUILD_MARKER)]
        if biz_name and t == p['manifest']:
            lines = ['name: %s' % json.dumps(biz_name) if ln == 'name: %s' % FALLBACK_NAME else ln for ln in lines]
        fd = os.open(t, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write('\n'.join(lines))
        created.append(rel_t)
    return created, kept


# ------------------------------------------------------------------ settings grant

def grant(data, yes):
    if settings_has(data):
        say('Folder access: already granted. ~/.claude/settings.json lists %s. Nothing to change.' % data)
        return 0
    exists = os.path.exists(SETTINGS)
    if exists:
        try:
            with open(SETTINGS, encoding='utf-8') as f:
                settings = json.load(f)
        except ValueError:
            say('Folder access: ~/.claude/settings.json is not valid JSON, so setup will not edit it. Nothing was written.')
            return 2
        if not isinstance(settings, dict):
            say('Folder access: ~/.claude/settings.json is not a JSON object. Nothing was written.')
            return 2
    else:
        settings = {}
    perms = settings.get('permissions')
    if perms is not None and not isinstance(perms, dict):
        say('Folder access: "permissions" in ~/.claude/settings.json is not an object. Nothing was written.')
        return 2
    say('Folder access: setup will add one line to ~/.claude/settings.json, under permissions.additionalDirectories:')
    say('  "%s"' % data)
    say('This lets a session opened in one of your project folders read and write the whole business folder. Nothing else in the file changes.')
    if not yes:
        say('Preview only. Nothing was written. Run again with --yes after the user says yes.')
        return 0
    run_state = paths(data)['run_state']
    if not writable_dir(data) or run_state is None:
        say('Folder access: %s, so no backup can be kept. Nothing was written.' % (
            'read-only. The business folder cannot be written' if not writable_dir(data)
            else 'the business folder holds more than one HQ folder, so Merlin cannot tell where its files go'))
        return 2
    backups = os.path.join(run_state, 'setup', 'backups')
    backup = None
    if exists:
        os.makedirs(backups, exist_ok=True)
        backup = os.path.join(backups, 'claude-settings.json.%s' % stamp())
        shutil.copy2(SETTINGS, backup)
    created_perms = perms is None
    if created_perms:
        perms = {}
        settings['permissions'] = perms
    dirs = perms.get('additionalDirectories')
    created_dirs = dirs is None
    if created_dirs:
        dirs = []
    dirs = list(dirs) + [data]
    perms['additionalDirectories'] = dirs
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    tmp = SETTINGS + '.merlin-tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(settings, f, indent=2)
        f.write('\n')
    os.replace(tmp, SETTINGS)
    rec = load_record()
    rec['settings'] = {'added': data, 'created_file': not exists, 'created_permissions': created_perms,
                       'created_additional': created_dirs, 'backup': backup,
                       'written_sha256': sha256_file(SETTINGS)}
    save_record(rec)
    say('Folder access: added. %s' % ('Backup of the previous file: %s' % backup if backup else 'The file did not exist before; setup created it.'))
    return 0


# ------------------------------------------------------------------ Codex block

def codex_block_text(data):
    return '\n'.join([
        CODEX_BEGIN,
        '## Merlin',
        '',
        'Merlin is installed for this account. Your business folder is `%s`.' % data,
        'Before other Merlin work in a session, run the Merlin `rules` skill and follow it.',
        'Your business folder wins over any memory the assistant keeps.',
        CODEX_END,
        '',
    ])


def codex_block(data, yes):
    st = codex_status()
    if not st['codex']:
        say('Codex: not found on this computer (~/.codex is missing). Nothing to do.')
        return 0
    if st['hook_trusted']:
        say('Codex: Merlin\'s start-up hook is already trusted in Codex, so the rules load on their own. No block is needed.')
        return 0
    if st['block_present']:
        say('Codex: the Merlin block is already in ~/.codex/AGENTS.md. Nothing to change.')
        return 0
    text = codex_block_text(data)
    say('Codex: setup will add this block at the end of ~/.codex/AGENTS.md, between two marker lines, and change nothing else:')
    say(text)
    if not yes:
        say('Preview only. Nothing was written. Run again with --yes after the user says yes.')
        return 0
    existed = os.path.exists(CODEX_AGENTS)
    old = ''
    if existed:
        with open(CODEX_AGENTS, encoding='utf-8') as f:
            old = f.read()
    sep = '' if (not old or old.endswith('\n\n')) else ('\n' if old.endswith('\n') else '\n\n')
    with open(CODEX_AGENTS, 'a', encoding='utf-8') as f:
        f.write(sep + text)
    rec = load_record()
    rec['codex'] = {'created_file': not existed, 'separator': sep}
    save_record(rec)
    say('Codex: block added to ~/.codex/AGENTS.md.')
    return 0


# ------------------------------------------------------------------ Keychain

def key_store(name):
    if not re.match(r'^[A-Za-z0-9_.-]+$', name):
        say('Keys: the key name may use letters, digits, dot, dash and underscore only. Nothing was stored.')
        return 2
    value = sys.stdin.read().rstrip('\n')
    if not value:
        say('Keys: no value was given on standard input. Nothing was stored.')
        return 2
    r = subprocess.run(['security', 'add-generic-password', '-a', name, '-s', KEYCHAIN_SERVICE, '-U', '-w', value],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    value = None
    if r.returncode != 0:
        say('Keys: the macOS Keychain refused to store %s (exit %d). Nothing was stored.' % (name, r.returncode))
        return 1
    rec = load_record()
    keys = set(rec.get('keys') or [])
    keys.add(name)
    rec['keys'] = sorted(keys)
    save_record(rec)
    say('Keys: %s stored in the macOS Keychain under service "%s". The value was not printed.' % (name, KEYCHAIN_SERVICE))
    return 0


def key_check(name):
    r = subprocess.run(['security', 'find-generic-password', '-a', name, '-s', KEYCHAIN_SERVICE],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print('ok' if r.returncode == 0 else 'missing')
    return 0


# ------------------------------------------------------------------ remove

def remove(data, yes, keys):
    rec = load_record()
    plan = []
    if os.path.exists(POINTER):
        plan.append('delete the pointer file ~/.merlin/instance.json')
    s = rec.get('settings')
    if s:
        plan.append('take %s back out of ~/.claude/settings.json' % s['added'])
    elif data and settings_has(data):
        plan.append('leave the folder-access line for %s in ~/.claude/settings.json: setup has no record of adding it' % data)
    c = rec.get('codex')
    has_block = False
    try:
        with open(CODEX_AGENTS, encoding='utf-8') as f:
            has_block = CODEX_BEGIN in f.read()
    except OSError:
        pass
    if has_block:
        plan.append('remove the Merlin block from ~/.codex/AGENTS.md')
    for k in keys:
        plan.append('delete the Keychain key %s (service "%s")' % (k, KEYCHAIN_SERVICE))
    if os.path.exists(RECORD):
        plan.append('delete setup\'s own record, ~/.merlin/setup-record.json')
    say('Remove: this reverses what Merlin wrote outside your business folder.')
    if not plan:
        say('Nothing to remove.')
    for i, p in enumerate(plan, 1):
        say('  %d. %s' % (i, p))
    say('Your business folder and every file in it stay exactly as they are%s.' % (' (%s)' % data if data else ''))
    if not yes:
        say('Preview only. Nothing was changed. Run again with --yes after the user says yes.')
        return 0

    if s and os.path.exists(SETTINGS):
        same = sha256_file(SETTINGS) == s.get('written_sha256')
        if same and s.get('created_file'):
            os.remove(SETTINGS)
            say('Folder access: removed. Setup had created ~/.claude/settings.json and nothing else changed it since, so it was deleted.')
        elif same and s.get('backup') and os.path.exists(s['backup']):
            shutil.copyfile(s['backup'], SETTINGS)
            say('Folder access: removed. ~/.claude/settings.json is back to the copy saved before setup changed it.')
        else:
            try:
                with open(SETTINGS, encoding='utf-8') as f:
                    settings = json.load(f)
            except ValueError:
                say('Folder access: ~/.claude/settings.json is not valid JSON now, so setup did not edit it. Remove the line for %s by hand.' % s['added'])
                settings = None
            if settings is not None:
                perms = settings.get('permissions') or {}
                dirs = perms.get('additionalDirectories') or []
                keep = [x for x in dirs if not (isinstance(x, str) and canon(x) == s['added'])]
                if keep or not s.get('created_additional'):
                    perms['additionalDirectories'] = keep
                else:
                    perms.pop('additionalDirectories', None)
                if 'permissions' in settings and not perms and s.get('created_permissions'):
                    settings.pop('permissions')
                if not settings and s.get('created_file'):
                    os.remove(SETTINGS)
                else:
                    tmp = SETTINGS + '.merlin-tmp'
                    with open(tmp, 'w', encoding='utf-8') as f:
                        json.dump(settings, f, indent=2)
                        f.write('\n')
                    os.replace(tmp, SETTINGS)
                say('Folder access: removed the line for %s. The rest of ~/.claude/settings.json was kept; it had changed since setup.' % s['added'])
    if has_block:
        with open(CODEX_AGENTS, encoding='utf-8') as f:
            text = f.read()
        a = text.index(CODEX_BEGIN)
        b = text.index(CODEX_END, a) + len(CODEX_END)
        if text[b:b + 1] == '\n':
            b += 1
        sep = (c or {}).get('separator', '')
        if sep and text[a - len(sep):a] == sep:
            a -= len(sep)
        rest = text[:a] + text[b:]
        if not rest.strip() and (c or {}).get('created_file'):
            os.remove(CODEX_AGENTS)
        else:
            with open(CODEX_AGENTS, 'w', encoding='utf-8') as f:
                f.write(rest)
        say('Codex: the Merlin block was removed from ~/.codex/AGENTS.md.')
    for k in keys:
        r = subprocess.run(['security', 'delete-generic-password', '-a', k, '-s', KEYCHAIN_SERVICE],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        say('Keys: %s %s.' % (k, 'deleted' if r.returncode == 0 else 'was not in the Keychain'))
    if os.path.exists(POINTER):
        os.remove(POINTER)
        say('Pointer: ~/.merlin/instance.json deleted.')
    if os.path.exists(RECORD):
        os.remove(RECORD)
    try:
        os.rmdir(home('.merlin'))
    except OSError:
        pass
    say('Next: remove the Merlin plugin straight away. In the Claude app: the Plugins screen. In a terminal: '
        'claude plugin uninstall merlin@praxis-plugins. If the plugin setting "Your business folder" is set, '
        'the start-up hook writes the pointer file again at the next session start while the plugin stays installed.')
    return 0


# ------------------------------------------------------------------ report

# Each of Merlin's files as the report names it: plain words first, the file or folder name after.
STARTER_WORDS = {'manifest': 'Merlin\'s settings file (manifest.yaml)',
                 'task_list': 'the task list (TASKS.md)',
                 'about_me': 'the folder for notes about you (ABOUT-ME)',
                 'memory': 'the folder for notes about your business (memory)',
                 'task_ids': 'the folder that keeps task numbers from repeating (.task-ids)',
                 'run_state': 'the folder Merlin keeps its own working notes in (state)'}

def report(root, data, source, created=None, kept=None):
    say('Merlin setup report')
    say('')
    checks = []
    if data is None:
        checks.append(('missing', 'Business folder: none chosen yet. Run setup\'s folder step: it asks which folder to use and records it.'))
    else:
        where = 'plugin setting' if source == 'option' else 'pointer file'
        if os.path.isdir(data):
            checks.append(('ok', 'Business folder: %s (from the %s)' % (data, where)))
        else:
            checks.append(('missing', 'Business folder: %s (from the %s) is not there. If it was moved or renamed on purpose, %s' % (
                data, where, 'run setup\'s folder step to record the new place.' if source != 'option' else 'change the plugin setting to the new place.')))
        if os.path.isdir(data):
            checks.append(('ok' if writable_dir(data) else 'problem', 'Writable by this account' if writable_dir(data) else 'Not writable by this account: Merlin stays read-only'))
            for rec_path, _rec in unfinished_moves(data):
                checks.append(('problem', '%s Until then Merlin stays read-only.' % move_problem(rec_path)))
            p = paths(data)
            if p['rule'] == AMBIGUOUS:
                checks.append(('problem', 'More than one HQ folder in %s (%s): %s, so it stays read-only. %s.'
                               % (data, clash_words(p['ambiguous']), TWO_HQ, TWO_HQ_FIX)))
            else:
                if p['rule'] == 1:
                    checks.append(('ok', '%s: %s. It sits inside the business folder %s.' % (HQ_WORDS, data, p['business'])))
                elif p['rule'] == 2:
                    checks.append(('ok', '%s: %s' % (HQ_WORDS, p['hq'])))
                # Named in plain words, with the file or folder name in parentheses. With no HQ yet,
                # these are the files the starter-files step creates.
                missing = [STARTER_WORDS[k] for k in ('manifest', 'task_list', 'about_me', 'memory', 'task_ids', 'run_state')
                           if not os.path.exists(p[k])]
                checks.append(('ok' if not missing else 'missing', 'Starter files: in place' if not missing else
                               'Starter files missing: %s. Run setup again to fill them in; nothing that is there is '
                               'overwritten.' % ', '.join(missing)))
                about = os.path.join(p['about_me'], 'about-me.md')
                have_about = os.path.exists(about)
                checks.append(('ok' if have_about else 'missing', 'Notes about you (about-me.md): %s' % (
                    'written' if have_about else 'not written yet. Next: the about-you interview writes them (say: set up my profile)')))
        status, msg = pointer_check(data)
        checks.append(('problem' if status == 'mismatch' else status, msg))
        checks.append(('ok' if settings_has(data) else 'optional', 'Folder access in ~/.claude/settings.json: %s' % ('granted' if settings_has(data) else 'not granted (offered on a yes)')))
    cs = codex_status()
    if cs['codex']:
        checks.append(('ok' if (cs['hook_trusted'] or cs['block_present']) else 'optional',
                       'Codex: %s' % ('start-up hook trusted' if cs['hook_trusted'] else ('Merlin block in ~/.codex/AGENTS.md' if cs['block_present'] else 'hook not trusted and no block (the block is offered on a yes)'))))
    if created is not None:
        checks.append(('ok', 'Starter files copied now: %d created, %d already there and left as they were' % (len(created), len(kept))))
    for st, msg in checks:
        mark = {'ok': '[ok]', 'missing': '[missing]', 'problem': '[problem]', 'optional': '[optional]'}[st]
        say('%-10s %s' % (mark, msg))
    say('')
    say(MEMORY_NOTE)
    say('')
    nxt = next((m for s, m in checks if s in ('missing', 'problem')), None)
    say('Next action: %s' % (nxt if nxt else 'nothing is missing. Next: set up your first project, a folder inside your business folder for one job, client or area of work.'))
    return 0


# ------------------------------------------------------------------ where

def where(data, source):
    """Print the business folder, its HQ and every data path as JSON. Writes nothing.

    For a session where the start-up hook did not run (Codex), and for any script that needs a
    data path: it reads them here instead of joining its own.
    """
    if data is None:
        out = {'folder': None, 'source': None, 'rule': None, 'layout': 'unset', 'business': None,
               'hq': None, 'new_hq': None, 'ambiguous': []}
        out.update({k: None for k in DATA_KEYS})
    else:
        out = paths(data)
        out['source'] = source
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


# ------------------------------------------------------------------ main

def main(argv):
    ap = argparse.ArgumentParser(prog='setup', add_help=True)
    ap.add_argument('--data')
    ap.add_argument('--plugin-root')
    ap.add_argument('--yes', action='store_true')
    ap.add_argument('--keys', default='')
    ap.add_argument('--name', help='with --skeleton: the business name for a new manifest.yaml')
    ap.add_argument('--open', metavar='DIR', help='with --suggest: the folder the conversation is open on (default: the working folder)')
    ap.add_argument('--offer-drive-icloud', action='store_true', help='with --suggest: also suggest Google Drive or iCloud Drive (untested)')
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--choose', metavar='DIR')
    g.add_argument('--change-to', metavar='DIR', help='the change-folder step: record DIR in place of the current business folder (preview without --yes)')
    g.add_argument('--move-to-hq', metavar='DIR', nargs='?', const='', help='turn an older flat business folder into the HQ layout (preview without --yes)')
    g.add_argument('--suggest', metavar='NAME', nargs='?', const='')
    g.add_argument('--inspect', metavar='DIR')
    for m in ('check', 'skeleton-only', 'skeleton', 'grant', 'codex-status', 'codex-block', 'explain-memory', 'remove', 'where'):
        g.add_argument('--' + m, action='store_true')
    g.add_argument('--set-name', metavar='NAME', help='write the business name into a manifest.yaml that still says My Business')
    g.add_argument('--key-store', metavar='NAME')
    g.add_argument('--key-check', metavar='NAME')
    a = ap.parse_args(argv)
    root = plugin_root(a.plugin_root)
    data, source = data_folder(a.data)

    if a.explain_memory:
        say(MEMORY_NOTE)
        return 0
    if a.suggest is not None:
        if a.open is not None:
            open_dir = a.open
        else:
            try:
                open_dir = os.getcwd()
            except OSError:
                open_dir = ''
        return suggest(a.suggest, open_dir, a.offer_drive_icloud, a.data)
    if a.inspect is not None:
        return inspect(a.inspect)
    if a.key_store:
        return key_store(a.key_store)
    if a.key_check:
        return key_check(a.key_check)
    if a.codex_status:
        say(json.dumps(codex_status(), indent=2))
        return 0
    if a.remove:
        return remove(data, a.yes, [k for k in a.keys.split(',') if k])
    if a.choose is not None:
        return choose(root, a.choose, a.data, a.yes)
    if a.change_to is not None:
        if root is None:
            say('setup: cannot find the plugin folder from %s. Nothing was written.' % __file__)
            return 2
        return change_to(root, a.change_to, a.data, a.yes, (a.name or '').strip() or None)
    if a.move_to_hq is not None:
        return move_to_hq(root, a.move_to_hq, data, a.yes)
    if a.where:
        return where(data, source)
    if data is None and not a.check:
        say('setup: no business folder is chosen yet. Run setup\'s folder step first (--choose, for example --choose "~/%s"). Nothing was written.' % FALLBACK_NAME)
        return 2
    if a.set_name is not None:
        return set_name(data, a.set_name)
    if a.grant:
        return grant(data, a.yes)
    if a.codex_block:
        return codex_block(data, a.yes)
    if a.skeleton or a.skeleton_only:
        if root is None:
            say('setup: cannot find the plugin folder from %s. Nothing was written.' % __file__)
            return 2
        created, kept = copy_skeleton(root, data, (a.name or '').strip() or None)
        return report(root, data, source, created, kept)
    return report(root, data, source)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
