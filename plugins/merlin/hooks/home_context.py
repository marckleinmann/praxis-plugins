#!/usr/bin/env python3
"""Merlin home SessionStart hook: open the Merlin home in the browser pane on a new chat.

Python standard library only. Runs on Windows and Mac, Python 3.8 or later.

Reads the SessionStart event on stdin. On a new chat (startup, clear or fork, never resume or
compact) whose folder is the business folder, Merlin's HQ folder or a project listed in the
manifest's projects: list (or, while the construction add-on is not activated, a construction
folder holding job.json, which the home shows as a plain project card), it:

  1. registers the business folder with the home server (an id made from the secret in
     ~/.merlin/home/secret, recorded in ~/.merlin/home/folders.json);
  2. makes sure the one home server per computer is running (skills/home/home_serve.py),
     started detached with no output of its own held open; a server of another version is
     stopped by its own process number and a new one started;
  3. prints one JSON line telling Claude to open the home in the browser pane before
     answering: on Projects for the business or HQ folder, on the project's own page for a
     project folder.

It prints nothing and starts no server: on a resume or a compaction; when Merlin is not set
up or its business folder is read-only or cannot be reached; when the off switch exists
(home/off in Merlin's run-state folder: live/state/ in the HQ folder, state/ in an older flat
install); in a construction folder (one holding job.json, construction/settings.json or
Jobs.html), or a folder inside one, while the construction add-on is installed and switched
on; outside the business folder; in a folder inside it that is neither a listed project nor a
construction folder; and on any failure.

It never writes in a business folder. Its own files are the home server's, in
~/.merlin/home/ only. Everything it does fits a 4 second budget counted from its start. Each
read in the business folder (which may sit on a synced drive that keeps files online only)
runs on a worker thread joined with the time left, so one stuck read cannot hold it; past the
budget it prints nothing and exits 0. Its text stays at or under 2,000 characters.

The home skill runs it as a command too:

    home_context.py address "<folder>"   registers, makes sure the server runs, prints the
                                         address to open; works with the off switch present
    home_context.py off "<folder>"       creates the off switch file (the only write this
                                         file ever makes in a business folder)
    home_context.py on "<folder>"        removes it
    home_context.py stop                 stops this computer's home server, when one of ours
                                         answers; another program on the port is left alone

<folder> may be the business folder, its HQ folder or a folder inside the business folder.
Each command exits non-zero with one plain line on stderr when it cannot do its work.

For tests: MERLIN_HOME_NO_START names a file. With it set, no server is ever started or
stopped: each start the run would make is added to that file as one JSON line, and the run
goes on as if that server had started. MERLIN_HOME_PORTS ("lo-hi") replaces the port list.
"""
import sys

sys.dont_write_bytecode = True  # the server module is loaded by its path: leave no bytecode beside it

import time

START = time.monotonic()

import errno
import importlib.util
import json
import os
import re
import signal
import subprocess
import threading
from datetime import datetime
from urllib.parse import quote

BUDGET = 4.0        # seconds for a hook run, counted from START
CMD_BUDGET = 8.0    # seconds for the address command
MAX_CHARS = 2000
MAX_STARTS = 3      # servers one run may start: a replace, a port whose bind fails, one more
START_WAIT = 1.5    # seconds a started server gets to answer in a hook run
CMD_START_WAIT = 3.0
GRACE = 0.3
NEW_CHAT = ("startup", "clear", "fork")
NO_START_VAR = "MERLIN_HOME_NO_START"
OPTION_VAR = "CLAUDE_PLUGIN_OPTION_DATA_DIR"
MOVE_RECORD = os.path.join("merlin-setup", "move-to-hq.json")
NAME_MAX = 120
FRAGMENT_SAFE = "-_.!~*'()"  # what encodeURIComponent leaves as it is

TRACE = {}


# ---------------------------------------------------------------- the time budget

class OutOfTime(Exception):
    """The budget ran out, or a read did not finish within it."""


class Budget:
    def __init__(self, seconds, start=None):
        self.end = (time.monotonic() if start is None else start) + seconds
        self.stuck = False

    def left(self):
        return self.end - time.monotonic()

    def call(self, fn, *args):
        """fn(*args) on a worker thread, joined with the time left. Raises OutOfTime when no
        time is left or the call does not finish in it; the stuck thread is left behind."""
        if self.left() <= 0:
            raise OutOfTime()
        box = {}

        def work():
            try:
                box["value"] = fn(*args)
            except BaseException as exc:  # handed back to the caller below
                box["error"] = exc

        t = threading.Thread(target=work)
        t.daemon = True
        t.start()
        t.join(max(0.0, self.left()))
        if t.is_alive():
            self.stuck = True
            raise OutOfTime()
        if "error" in box:
            raise box["error"]
        return box.get("value")


# ---------------------------------------------------------------- places

def plugin_dir():
    """The plugin folder: this file sits at <plugin>/hooks/home_context.py."""
    return os.sep.join(os.path.abspath(__file__).split(os.sep)[:-2]) or os.sep


def home_skill_dir():
    return os.path.join(plugin_dir(), "skills", "home")


def serve_script():
    return os.path.join(home_skill_dir(), "home_serve.py")


def addons_file():
    return os.path.join(home_skill_dir(), "addons", "addons.json")


def pointer_file():
    return os.path.join(os.path.expanduser("~"), ".merlin", "instance.json")


_MODS = {}


def serve_mod():
    """home_serve.py, loaded by its path once (Python run with -I leaves its folder off the path)."""
    if "serve" not in _MODS:
        spec = importlib.util.spec_from_file_location("merlin_home_serve", serve_script())
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _MODS["serve"] = mod
    return _MODS["serve"]


def data_mod():
    return serve_mod().home_data()


def real(p):
    return os.path.realpath(os.path.abspath(p))


def inside(parent, child):
    """True when child is parent or below it. Both are real paths."""
    try:
        return os.path.commonpath([parent, child]) == parent
    except ValueError:  # different drives on Windows
        return False


def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def clean_name(s):
    s = " ".join(str(s).replace('"', "'").split())
    return s[:NAME_MAX]


# ---------------------------------------------------------------- the business folder

def configured_folder():
    """The business folder as the start-up hook finds it: the plugin option when it is a full
    path, else data_root in ~/.merlin/instance.json. None when neither names one."""
    opt = (os.environ.get(OPTION_VAR) or "").strip()
    if opt and not opt.startswith("${"):
        p = os.path.expanduser(opt)
        if os.path.isabs(p):
            return p
    ptr = read_json(pointer_file())
    v = ptr.get("data_root") if isinstance(ptr, dict) else None
    return v if isinstance(v, str) and v.strip() else None


def move_unfinished(folder):
    """A move into an HQ folder that did not finish, in the places the start-up hook checks."""
    places = [os.path.join(folder, "state", MOVE_RECORD), os.path.join(folder, "live", "state", MOVE_RECORD)]
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        names = []
    places += [os.path.join(folder, n, "live", "state", MOVE_RECORD) for n in names
               if not n.startswith(".") and os.path.isdir(os.path.join(folder, n))]
    for p in places:
        if os.path.isfile(p):
            rec = read_json(p)
            if not isinstance(rec, dict) or rec.get("status") != "complete":
                return True
    return False


def business_info(raw):
    """{"state": "ready" | "none" | "notsetup" | "readonly", "why": plain words, and for a ready
    folder "business", "hq", "layout", "manifest", "run_state"}. Reads the business folder:
    call it through the budget."""
    if not raw:
        return {"state": "none", "why": "Merlin is not set up on this computer yet"}
    p = os.path.expanduser(raw)
    if not os.path.isabs(p):
        return {"state": "readonly", "why": "the business folder setting is not a full path"}
    if not os.path.exists(p):
        return {"state": "readonly", "why": "the business folder cannot be reached (moved, renamed, or a drive that is not connected)"}
    if not os.path.isdir(p):
        return {"state": "readonly", "why": "the business folder setting names a file, not a folder"}
    if not os.access(p, os.W_OK | os.X_OK):
        return {"state": "readonly", "why": "this computer account cannot write to the business folder"}
    p = real(p)
    layout, business, hq, _clash = data_mod().find_layout(p)
    if layout == "ambiguous":
        return {"state": "readonly", "why": "the business folder holds more than one HQ folder"}
    if layout not in ("hq", "flat"):
        return {"state": "notsetup", "why": "Merlin's setup has not run in the business folder yet"}
    if move_unfinished(p):
        return {"state": "readonly", "why": "moving Merlin's own files into the HQ folder stopped before it finished"}
    run_state = os.path.join(hq, "live", "state") if layout == "hq" else os.path.join(hq, "state")
    return {"state": "ready", "business": real(business), "hq": real(hq), "layout": layout,
            "manifest": os.path.join(hq, "manifest.yaml"), "run_state": run_state}


def off_switch(info):
    return os.path.join(info["run_state"], "home", "off")


def business_name(info):
    name, _projects, _text = data_mod().read_manifest(info["manifest"])
    if not name or name == getattr(data_mod(), "STARTER_NAME", "My Business"):
        name = os.path.basename(info["business"].rstrip(os.sep)) or info["business"]
    return clean_name(name)


def place(folder, info):
    """Where FOLDER sits: ("business" | "hq" | "project" | "job" | "other" | "outside", project
    name or None). A project is a folder in the manifest's projects: list, matched by its real
    path; a folder below a project counts as that project, the deepest one when they nest. A
    folder that is in no listed project but is or sits in a construction folder is "job", named
    as the home names that folder's card."""
    c = real(folder)
    biz, hq = info["business"], info["hq"]
    if c == biz:
        return "business", None
    if info["layout"] == "hq" and inside(hq, c):
        return "hq", None
    if not inside(biz, c):
        return "outside", None
    _name, projects, _text = data_mod().read_manifest(info["manifest"])
    base = os.path.split(info["manifest"])[0]
    best = None
    for pname, ppath in projects:
        path = real(os.path.join(base, ppath) if ppath else os.path.join(biz, pname))
        if path == biz or not inside(biz, path):
            continue
        if inside(path, c) and (best is None or len(path) > len(best[1])):
            best = (pname, path)
    if best:
        return "project", best[0]
    job = job_folder(biz, c)
    if job:
        return "job", os.path.basename(job)
    return "other", None


JOB_MARKER = "job.json"  # the construction marker home_data's folder scan looks for


def job_folder(biz, c):
    """The construction folder C is or sits in, as the home's own scan finds it: the outermost
    folder holding job.json within the scan's depth below the business folder, reached with no
    hidden folder and no link on the way. The home shows it as a plain project card named after
    the folder while the construction add-on is not activated. None when there is none."""
    rel = os.path.relpath(c, biz)
    if rel == os.curdir or rel.startswith(os.pardir):
        return None
    depth = getattr(data_mod(), "JOB_SCAN_DEPTH", 3)
    parts = rel.split(os.sep)
    for i in range(1, min(depth, len(parts)) + 1):
        if parts[i - 1].startswith("."):
            return None
        d = os.path.join(biz, *parts[:i])
        if os.path.islink(d) or not os.path.isdir(d):
            return None
        if os.path.isfile(os.path.join(d, JOB_MARKER)):
            return d
    return None


def construction_quiet(folder, info, job=False):
    """True when FOLDER is a construction folder (or, with job, sits in one) and that add-on is
    installed and switched on here. With no add-on list, no add-on is known and this is never True."""
    hd = data_mod()
    rows, _found = hd.read_addon_list(addons_file())
    if not rows:
        return False
    states = hd.addon_states(rows, hd.read_plugins(info["business"]), set())
    for a in states:
        if a.get("state") != "activated":
            continue
        marks = []
        if isinstance(a.get("marker"), str) and a["marker"]:
            marks.append(a["marker"])
        if a.get("id") == "construction":
            if job:
                return True
            marks += [os.path.join("construction", "settings.json"), "Jobs.html"]
        if any(os.path.isfile(os.path.join(folder, m)) for m in marks):
            return True
    return False


# ---------------------------------------------------------------- the server

def start_server(argv):
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
          "close_fds": True, "cwd": os.path.expanduser("~")}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x08000000 | 0x00000200  # detached, no window, own group
    else:
        kw["start_new_session"] = True
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop(NO_START_VAR, None)
    kw["env"] = env
    return subprocess.Popen(argv, **kw)


def no_start_record(entry):
    """With the test switch set, add the start this run would make to its file. True when set."""
    rec = os.environ.get(NO_START_VAR)
    if not rec:
        return False
    with open(rec, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    return True


def probe_time(end, cap):
    return max(0.02, min(cap, end - time.time()))


def wait_started(hs, proc, port, version, secret, seconds):
    """'up', 'gone' (it stopped before it answered: its bind failed) or 'slow'."""
    end = time.time() + seconds
    gone = False
    while True:
        st, info = hs.probe(port, secret, timeout=probe_time(end, 0.2))
        if st == "ours" and info.get("version") == version:
            return "up"
        if not gone and proc is not None and proc.poll() is not None:
            gone = True
            end = min(end, time.time() + GRACE)
        if time.time() >= end:
            return "gone" if gone else "slow"
        time.sleep(0.05)


def wait_closed(hs, port, secret, seconds):
    end = time.time() + seconds
    while time.time() < end:
        if hs.probe(port, secret, timeout=probe_time(end, 0.1))[0] == "closed":
            return True
        time.sleep(0.05)
    return False


def stop_pid(pid):
    """Stop one process by its number, never by name. True when the signal went. The number
    must come from a probe that answered 'ours': there it sits inside a proof only a holder of
    this account's home secret can make, so it is never a number another program chose."""
    if not isinstance(pid, int) or pid <= 0 or pid == os.getpid():
        return False
    try:
        os.kill(pid, signal.SIGTERM)  # on Windows this ends the process
        return True
    except OSError:
        return False


def ensure_server(secret, budget, start_wait):
    """(port, outcome) of a running home server of this version that proved it holds this
    account's home secret, or (None, outcome). A port held by anything else is passed over and
    never stopped, whatever it answers."""
    hs = serve_mod()
    script = serve_script()
    if not os.path.isfile(script):
        return None, "no_script"
    version = hs.script_version(script)
    rec = hs.recorded_port()
    ports = hs.port_range()
    order = ([rec] if rec in ports else []) + [p for p in ports if p != rec]
    starts = 0
    for port in order:
        left = budget.left()
        if left <= 0:
            return None, "budget_out"
        st, info = hs.probe(port, secret, timeout=max(0.05, min(0.25, left)))
        if st == "other":
            continue
        if st == "ours" and info.get("version") == version:
            return port, "up"
        if starts >= MAX_STARTS:
            return None, "start_cap"
        replacing = st == "ours"
        argv = hs.server_argv(port)
        if no_start_record({"port": port, "argv": argv, "replacing": replacing,
                            "pid": info.get("pid") if replacing else None}):
            return port, "would_start"
        if replacing:
            stop_pid(info.get("pid"))
            if not wait_closed(hs, port, secret, min(0.5, max(0.0, budget.left()))):
                continue  # it will not let go in time: leave it and try the next port
        proc = start_server(argv)
        starts += 1
        TRACE["starts"] = starts
        got = wait_started(hs, proc, port, version, secret, min(start_wait, max(0.0, budget.left())))
        if got == "up":
            return port, ("replaced" if replacing else "started")
        if got == "gone":
            continue  # it could not bind this port: try the next one
        return None, "start_slow"  # a second server on another port would race this one
    return None, "all_taken"


def page_address(port, fid, project):
    addr = serve_mod().home_address(port, fid)
    if project:
        addr += "#project/" + quote(project, safe=FRAGMENT_SAFE)
    return addr


# ---------------------------------------------------------------- text

PANE = ('Show the browser pane beside the chat: the show-pane tool with pane "preview". '
        "Load that tool first if it is only listed.")
OWN = ("This holds when the message asks for something else or starts a skill: no skill opens the pane. "
       "Never say the home is open unless a browser pane tool opened it in this chat.")
TAIL = ("Never open the home in another browser or another app. If the pane cannot load it, say so in one line "
        "and give the address.\n"
        "If this chat has no browser pane tools (a terminal, a scheduled or headless run), do none of this "
        "and say nothing about it.")


def home_text(where, biz, project, addr):
    if where in ("project", "job"):
        head = ('Merlin home. This chat is open on the project "%s" in the business folder "%s". '
                "The Merlin home opens on that project's page." % (project, biz))
    elif where == "hq":
        head = ('Merlin home. This chat is open on Merlin\'s HQ folder for "%s". '
                "The Merlin home opens on its Projects page." % biz)
    else:
        head = ('Merlin home. This chat is open on the business folder "%s". '
                "The Merlin home opens on its Projects page." % biz)
    steps = [PANE, "Open that address in the browser pane.",
             "Leave the home open and answer the message. Mention the home in one short line at most, or not at all."]
    return "\n".join([head, "Its address: " + addr, "Before you answer the first message in this chat, whatever it says:"]
                     + ["%d. %s" % (i + 1, s) for i, s in enumerate(steps)]
                     + ["Do this once per chat. The home reads the business folder each time it loads.", OWN, TAIL])


# ---------------------------------------------------------------- the hook run

def read_event():
    """The event as text, decoded as UTF-8 (Python on Windows would read a pipe in the system
    code page)."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return ""
        buf = getattr(sys.stdin, "buffer", None)
        data = buf.read() if buf is not None else sys.stdin.read()
    except (OSError, ValueError):
        return ""
    if isinstance(data, str):
        return data
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("utf-8", "replace")


def look(folder, raw):
    """Every read in the business folder for one hook run: (info, where, project, quiet?)."""
    info = business_info(raw)
    if info["state"] != "ready":
        return info, None, None, None
    where, project = place(folder, info)
    if where in ("outside", "other"):
        return info, where, None, None
    if os.path.exists(off_switch(info)):
        return info, "off", None, None
    if construction_quiet(folder, info, job=where == "job"):
        return info, "construction", None, None
    return info, where, project, business_name(info)


def run(raw, budget):
    TRACE.clear()
    try:
        event = json.loads(raw) if raw.strip() else {}
    except ValueError:
        event = {}
    if not isinstance(event, dict):
        event = {}
    src = event.get("source")
    TRACE["src"] = src if src in NEW_CHAT + ("resume", "compact") else "other"
    if src not in NEW_CHAT:
        TRACE["out"] = "not_new_chat"
        return None
    cwd = event.get("cwd") if isinstance(event.get("cwd"), str) and event.get("cwd") else os.getcwd()
    if not os.path.isdir(cwd):
        cwd = os.getcwd()
    try:
        info, where, project, biz = budget.call(look, cwd, configured_folder())
    except OutOfTime:
        TRACE["out"] = "out_of_time"
        return None
    if info["state"] != "ready":
        TRACE["out"] = info["state"]
        return None
    if biz is None:
        TRACE["out"] = where
        return None
    hs = serve_mod()
    secret = hs.ensure_secret()
    try:
        fid = budget.call(hs.register, info["business"])
    except OutOfTime:
        TRACE["out"] = "out_of_time"
        return None
    port, out = ensure_server(secret, budget, START_WAIT)
    TRACE["out"], TRACE["port"] = out, port
    if not port:
        return None
    text = home_text(where, biz, project, page_address(port, fid, project if where in ("project", "job") else None))
    if len(text) > MAX_CHARS:
        TRACE["out"] = "too_long"
        return None
    return text


def hook_log():
    """One line in ~/.merlin/home/home.log: codes and numbers only. Never creates the folder."""
    try:
        sdir = serve_mod().state_dir() if "serve" in _MODS else None
        if not sdir or not os.path.isdir(sdir):
            return
        fields = [("pid", os.getpid()), ("os", "win" if os.name == "nt" else ("mac" if sys.platform == "darwin" else "other")),
                  ("src", TRACE.get("src")), ("out", TRACE.get("out")), ("port", TRACE.get("port")),
                  ("starts", TRACE.get("starts")), ("ms", int((time.monotonic() - START) * 1000))]
        parts = [datetime.now().astimezone().isoformat(timespec="seconds"), "hook"]
        parts += ["%s=%s" % (k, re.sub(r"[^0-9A-Za-z._+-]", "_", str(v))[:40]) for k, v in fields if v is not None]
        line = " ".join(parts).encode("ascii", "replace")[:199] + b"\n"
        fd = os.open(os.path.join(sdir, "home.log"), os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except BaseException:
        pass


def hook_main():
    budget = Budget(BUDGET, START)
    raw = read_event()
    try:
        text = run(raw, budget)
    except Exception as exc:
        text = None
        TRACE["out"] = "error_" + type(exc).__name__  # the class only: a message can hold a path
    if text and budget.left() > -0.5:
        out = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
        sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()
    hook_log()
    if budget.stuck:
        os._exit(0)  # a worker is still blocked in a read: leave without waiting for it
    return 0


# ---------------------------------------------------------------- commands for the home skill

class Refusal(Exception):
    """A plain one-line reason a command could not do its work."""


def command_business(folder):
    """The ready business folder for a command: the configured one, else FOLDER itself when it
    is a set-up business folder or HQ folder."""
    info = business_info(configured_folder())
    if info["state"] == "ready":
        return info
    if folder and os.path.isdir(folder):
        own = business_info(folder)
        if own["state"] == "ready":
            return own
    raise Refusal(info["why"])


def address_cmd(folder):
    budget = Budget(CMD_BUDGET)
    info = command_business(folder)
    where, project = place(folder, info) if folder and os.path.isdir(folder) else ("business", None)
    hs = serve_mod()
    secret = hs.ensure_secret()
    fid = hs.register(info["business"])
    port, out = ensure_server(secret, budget, CMD_START_WAIT)
    if not port:
        raise Refusal("the home server did not start on this computer (%s)" % out.replace("_", " "))
    return page_address(port, fid, project if where in ("project", "job") else None)


def switch_cmd(folder, turn_off):
    info = command_business(folder)
    path = off_switch(info)
    if turn_off:
        if not os.path.exists(path):
            os.makedirs(os.path.split(path)[0], exist_ok=True)
            try:
                os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644))
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise
        return ("The Merlin home is off for this business folder: a new chat no longer opens it. "
                "The switch is the empty file %s. To undo: say \"turn the Merlin home back on\"." % path)
    if os.path.exists(path):
        os.remove(path)
        try:
            os.rmdir(os.path.split(path)[0])  # the home's own folder, only when it is empty
        except OSError:
            pass
    return ("The Merlin home is on for this business folder: the next new chat opens it. "
            "To undo: say \"turn off the Merlin home\".")


def stop_cmd():
    """Stops this account's home server on any port where one proves it holds this account's
    home secret. Anything else on those ports is left running. Returns the words."""
    hs = serve_mod()
    secret = hs.read_secret()
    if not secret:
        return "No Merlin home server is running on this computer."
    rec = hs.recorded_port()
    ports = hs.port_range()
    order = ([rec] if isinstance(rec, int) and rec not in ports else []) + ports
    stopped, stuck = 0, 0
    for port in order:
        st, info = hs.probe(port, secret, timeout=0.25)
        if st != "ours":
            continue
        if stop_pid(info.get("pid")) and wait_closed(hs, port, secret, 3.0):
            stopped += 1
        else:
            stuck += 1
    state = hs.state_file("server.json")
    data = read_json(state)
    if isinstance(data, dict) and hs.probe(data.get("port") or 0, secret, timeout=0.25)[0] != "ours":
        try:
            os.remove(state)  # left by a server that was stopped without its own clean-up
        except OSError:
            pass
    if stuck:
        raise Refusal("a Merlin home server on this computer did not stop")
    if stopped:
        return "Stopped the Merlin home server on this computer."
    return "No Merlin home server is running on this computer."


def command_main(args):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    mode, rest = args[0], args[1:]
    usage = 'usage: home_context.py address|off|on "<folder>", or home_context.py stop'
    try:
        if mode == "stop":
            if rest:
                raise Refusal(usage)
            print(stop_cmd())
            return 0
        if len(rest) != 1 or not rest[0].strip():
            raise Refusal(usage)
        folder = real(os.path.expanduser(rest[0]))
        if mode == "address":
            print(address_cmd(folder))
        else:
            print(switch_cmd(folder, mode == "off"))
        return 0
    except Refusal as exc:
        print("Merlin home: %s." % str(exc).rstrip("."), file=sys.stderr)
        return 2
    except OSError as exc:
        print("Merlin home: a file could not be read or written (%s)." % type(exc).__name__, file=sys.stderr)
        return 3


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("address", "off", "on", "stop"):
        return command_main(sys.argv[1:])
    return hook_main()


if __name__ == "__main__":
    sys.exit(main())
