#!/usr/bin/env python3
"""Merlin home server: serves the Merlin home page to the browser pane on this computer only.

Python standard library only. Runs on Windows and Mac, Python 3.8 or later.

    home_serve.py --port N [--idle SECONDS] [--addons FILE]

The home hook starts it, detached, when a chat opens on a business folder. One server per
computer, however many chats and folders.

What it answers, and nothing else:
  /__merlin                  its name. With ?c=<32 hex characters, fresh from the caller> also
                             its version, process number and port, and a proof made with this
                             account's home secret, so the hook can tell its own server from
                             another program or another account's
  /h/<id>/home.html          the home page for one registered business folder, with its
                             data-mode and data-layout values set to the saved settings
  /h/<id>/<shell file>       the page's own files: home.css, home.js, fonts.css, framed.js,
                             fonts-OFL.txt and the two logo files, and nothing else
  /h/<id>/data.json          the page's data, read from the business folder on every request
  /h/<id>/samples/<add-on>/<path>
                             a file inside that add-on's folder under addons/samples/

  /h/<id>/__settings?k=<key>&menu=top|side&look=dark|light
                             saves the two settings in ~/.merlin/home/settings.json
  /h/<id>/__open-url?k=<key>&url=<address>
                             opens an https://www.praxisatwork.com/ address in the computer's
                             own browser, and no other address
  /h/<id>/__open-folder?k=<key>&id=<page id>
                             opens one project folder in Finder or File Explorer. The folder is
                             found by reading the business folder's own data again; a path is
                             never taken from the request

Each action answers 204 when it worked. Each carries a key of its own: HMAC-SHA256 of the
action's label and the folder id with this computer's home secret. The proof /__merlin
hands out is a different HMAC, over a first line no key and no folder id can start with, and
the secret itself is never sent. A page on another website
can make the browser ask for these addresses, but it cannot know the key, and a request the
browser marks as coming from another website is refused before the key is read. data.json is
refused the same way.

One more thing runs while data.json is built: for an add-on that is activated and whose
definition in the add-on list carries a "bridge" entry, {"script": <path inside the installed
plugin's folder>, "args": [...]}, the server runs that one script with its own Python (-I -B),
no shell, no window, a 4 second limit, the argument "{business_folder}" replaced by the
registered folder. The first line it prints is the add-on's page address, taken only when it is
an http://127.0.0.1:<port>/ address. Any other output, an error or a timeout gives no address.
The answer, address or none, is kept for 30 seconds per folder and add-on.

Its own files live in ~/.merlin/home/ only: the secret (secret), the registered folders
(folders.json), the running server (server.json), the two settings (settings.json) and a log
(home.log) holding fixed codes, numbers and versions, never a path, a folder name or an id.
It never writes in a business folder.

It listens on 127.0.0.1, refuses any Host other than the literal 127.0.0.1 (localhost
included), answers GET and HEAD only, never lists a folder, never follows a path or a link out
of where it may serve from, and never serves a dot file or CLAUDE.md. It stops by itself after
8 hours without a request. The hook's /__merlin probe is not counted as a request.

For tests: MERLIN_HOME_PORTS ("lo-hi") replaces the port list, MERLIN_HOME_IDLE the idle
time, MERLIN_HOME_VERSION the version. MERLIN_HOME_OPEN_RECORD names a file: with it set,
nothing is ever opened, and each open is added to that file as one JSON line instead.

The helpers the hook uses (state_dir, ensure_secret, read_secret, folder_id, read_registry,
register, port_range, probe, home_address, recorded_port, server_argv) import with no side
effect. Run with -I, Python leaves this file's folder off the import path, so a caller loads
this file by its path (importlib), as this file loads home_data.py.
"""
import time
_T0 = time.monotonic()  # process start, so the log's start time counts Python's own start
import argparse
import email.utils
import errno
import hashlib
import hmac
import http.client
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

NAME = "merlin-home"
IDLE_DEFAULT = 8 * 60 * 60
PORTS_DEFAULT = (47620, 47629)
HOST = "127.0.0.1"

SHELL_FILES = ("home.html", "home.css", "home.js", "fonts.css", "fonts-OFL.txt", "framed.js",
               "merlin-logo-master-dark.svg", "merlin-logo-master-light.svg")

# The three actions, by the last part of their address, and the label each key is made from.
ACT_SETTINGS = "__settings"
ACT_OPEN_URL = "__open-url"
ACT_OPEN_FOLDER = "__open-folder"
ACTIONS = {ACT_SETTINGS: "action:settings", ACT_OPEN_URL: "action:open-url",
           ACT_OPEN_FOLDER: "action:open-folder"}
ACTION_NAMES = {ACT_SETTINGS: "settings", ACT_OPEN_URL: "open_url", ACT_OPEN_FOLDER: "open_folder"}

SITE = "https://www.praxisatwork.com/"
SITE_HOST = "www.praxisatwork.com"

DEFAULT_SETTINGS = {"menu": "top", "look": "dark"}
MENUS = ("top", "side")
LOOKS = ("dark", "light")

BRIDGE_WAIT = 4.0     # seconds an add-on's command may run
BRIDGE_KEEP = 30.0    # seconds its answer is kept
BRIDGE_LINE = re.compile(r"^http://127\.0\.0\.1:(\d{1,5})/[^\s\"'<>\\`]*$")
BUSINESS_ARG = "{business_folder}"
NO_WINDOW = 0x08000000                     # Windows: start with no console window

TYPES = {
    ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8", ".txt": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".ico": "image/x-icon",
    ".woff2": "font/woff2", ".woff": "font/woff",
}

# The home page: its own files only, fonts as data:, frames of pages on this computer, and
# never framed by a page of another origin.
HOME_CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; "
            "frame-src 'self' http://127.0.0.1:*; frame-ancestors 'self'; "
            "base-uri 'none'; form-action 'none'")
# A sample page runs its own scripts, inline ones included, and loads only its own files. It is
# not sandboxed: a sandboxed page has no origin of its own, so the home's look message, aimed
# at this server's origin, would never reach it. It may not call this server (connect-src
# 'none'), frame anything, or be framed by a page of another origin.
SAMPLE_CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
              "img-src 'self' data:; font-src 'self' data:; media-src 'self'; connect-src 'none'; "
              "frame-src 'none'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'")


# ---------------------------------------------------------------- places (read at call time)

def home_root():
    return os.path.expanduser("~")


def state_dir():
    """~/.merlin/home: every file this server and the hook keep, and nothing else."""
    return os.path.join(home_root(), ".merlin", "home")


def state_file(name):
    return os.path.join(state_dir(), name)


def ensure_state_dir():
    d = state_dir()
    if not os.path.isdir(d):
        try:
            os.makedirs(d, 0o700)
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise
    return d


def here_parts():
    return os.path.abspath(__file__).split(os.sep)


def skill_dir():
    """The folder this file sits in, skills/home inside the plugin."""
    return os.sep.join(here_parts()[:-1])


def shell_dir():
    return os.path.join(skill_dir(), "shell")


def default_addons_file():
    return os.path.join(skill_dir(), "addons", "addons.json")


def port_range():
    raw = os.environ.get("MERLIN_HOME_PORTS") or ""
    try:
        lo, hi = (int(x) for x in raw.split("-", 1))
        if 1024 <= lo <= hi <= 65535 and hi - lo < 50:
            return list(range(lo, hi + 1))
    except ValueError:
        pass
    return list(range(PORTS_DEFAULT[0], PORTS_DEFAULT[1] + 1))


def own_version():
    """The first 16 hex characters of this file's SHA-256, unless MERLIN_HOME_VERSION is set.
    The hook computes the same value from the script it would start, so a server running
    older code reports a different version and is replaced."""
    return script_version(os.path.abspath(__file__))


def script_version(path):
    v = os.environ.get("MERLIN_HOME_VERSION")
    if v:
        return v
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()[:16]


def server_argv(port, python=None):
    """The command the hook starts the server with."""
    return [python or sys.executable, "-I", "-B", os.path.abspath(__file__), "--port", str(port)]


def home_address(port, fid):
    """The home page address for one registered folder. Always the literal 127.0.0.1."""
    return "http://%s:%d/h/%s/home.html" % (HOST, port, fid)


# ---------------------------------------------------------------- small files

def replace_retry(src, dst, tries=10, wait=0.1):
    """os.replace, tried again while Windows says another program has the file open."""
    for n in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if os.name != "nt" or n == tries - 1:
                raise
            time.sleep(wait)


def write_whole(path, data):
    """Write bytes to path whole and at once: a new file beside it, then a replace."""
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident() % 100000)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        replace_retry(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


class FileLock:
    """A lock file made with O_EXCL; one left by a run that died over 10 seconds ago is stale."""

    def __init__(self, path):
        self.path = path
        self.held = False

    def __enter__(self):
        for _ in range(40):
            try:
                os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
                self.held = True
                return self
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise
                try:
                    if time.time() - os.path.getmtime(self.path) > 10:
                        os.remove(self.path)
                        continue
                except OSError:
                    pass
            time.sleep(0.05)
        raise OSError("busy")

    def __exit__(self, *exc):
        if self.held:
            try:
                os.remove(self.path)
            except OSError:
                pass
        return False


# ---------------------------------------------------------------- secret, ids, registry

def read_secret():
    """This account's 32-byte home secret, or None while there is none. Never sent."""
    try:
        with open(state_file("secret"), "rb") as fh:
            s = fh.read()
    except OSError:
        return None
    return s if len(s) == 32 else None


def ensure_secret():
    """The secret, made once if there is none. Raises OSError when it cannot be made."""
    s = read_secret()
    if s is not None:
        return s
    ensure_state_dir()
    with FileLock(state_file("secret.lock")):
        s = read_secret()
        if s is None:
            write_whole(state_file("secret"), os.urandom(32))
            s = read_secret()
    if s is None:
        raise OSError("no secret")
    return s


PROBE_FIRST_LINE = b"merlin-home probe 1\n"   # no folder path and no action label starts so
PROBE_VALUE = re.compile(r"[0-9a-f]{32}")
PROBE_VERSION = re.compile(r"[0-9A-Za-z._+-]{1,64}")


def probe_proof(secret, value, port, pid, version):
    """What a home server answers to a probe, and what the caller works out to check it:
    HMAC-SHA256 with this account's home secret over a fixed first line, the caller's fresh
    value, and the server's own port, process number and version. Only a program that can read
    the secret can make it, the fresh value means an old answer cannot be sent again, and the
    port means an answer cannot be passed on from a server on another port. "" when a part
    is not in its fixed form. Never a key to anything."""
    if not secret or not isinstance(value, str) or not PROBE_VALUE.fullmatch(value):
        return ""
    if not isinstance(version, str) or not PROBE_VERSION.fullmatch(version):
        return ""
    for n in (port, pid):
        if not isinstance(n, int) or isinstance(n, bool) or n <= 0:
            return ""
    msg = PROBE_FIRST_LINE + ("%s\n%d\n%d\n%s" % (value, port, pid, version)).encode("ascii")
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def real_path(path):
    return os.path.realpath(os.path.abspath(path))


def folder_id(secret, path):
    """The folder's address part: 20 hex characters of HMAC-SHA256 over its real path."""
    return hmac.new(secret, real_path(path).encode("utf-8", "surrogateescape"),
                    hashlib.sha256).hexdigest()[:20]


def action_key(secret, action, fid):
    """The key one action carries for one folder: 32 hex characters of HMAC-SHA256 over the
    action's label and the folder id. A probe's proof is made over a different first line, so
    it is never a key."""
    label = ACTIONS.get(action)
    if not label or not secret or not fid:
        return ""
    return hmac.new(secret, ("%s:%s" % (label, fid)).encode("ascii"), hashlib.sha256).hexdigest()[:32]


def read_registry():
    """{id: {"kind": "business", "path": real path}} from ~/.merlin/home/folders.json."""
    data = read_json(state_file("folders.json"))
    folders = data.get("folders") if isinstance(data, dict) else None
    return folders if isinstance(folders, dict) else {}


def register(path, secret=None):
    """Record a business folder and return its id. Writes only when it is new."""
    secret = secret or ensure_secret()
    real = real_path(path)
    fid = folder_id(secret, real)
    entry = {"kind": "business", "path": real}
    if read_registry().get(fid) == entry:
        return fid
    ensure_state_dir()
    with FileLock(state_file("folders.lock")):
        folders = read_registry()
        if folders.get(fid) != entry:
            folders[fid] = entry
            body = json.dumps({"schema": 1, "folders": folders}, indent=1, sort_keys=True)
            write_whole(state_file("folders.json"), body.encode("utf-8"))
    return fid


def recorded_port():
    """The port the last server that started wrote in server.json, or None."""
    data = read_json(state_file("server.json"))
    p = data.get("port") if isinstance(data, dict) else None
    return p if isinstance(p, int) else None


def probe(port, secret, timeout=0.25):
    """('ours', info) when a home server that holds this account's secret answers on the port,
    info being its name, version, process number and port; ('closed', None) when nothing
    listens; ('other', None) when anything else holds it. "Ours" is proved, never claimed: the
    answer must carry probe_proof() over a value made fresh for this one call and over the
    port this call dialled. So the process number in info is one a holder of the secret gave."""
    conn = http.client.HTTPConnection(HOST, port, timeout=timeout)
    try:
        conn.connect()
    except OSError:
        conn.close()
        return "closed", None
    try:
        value = os.urandom(16).hex()
        conn.request("GET", "/__merlin?c=" + value, headers={"Host": "%s:%d" % (HOST, port)})
        r = conn.getresponse()
        body = r.read(4096)
        if r.status == 200 and secret:
            info = json.loads(body.decode("utf-8"))
            if isinstance(info, dict) and info.get("name") == NAME:
                pid, version, proof = info.get("pid"), info.get("version"), info.get("proof")
                want = probe_proof(secret, value, port, pid, version)
                if (want and isinstance(proof, str)
                        and hmac.compare_digest(proof.encode("utf-8", "replace"), want.encode("ascii"))):
                    return "ours", {"name": NAME, "version": version, "pid": pid, "port": port}
        return "other", None
    except (OSError, ValueError, http.client.HTTPException):
        return "other", None
    finally:
        conn.close()


# ---------------------------------------------------------------- settings

def clean_settings(data):
    """Both values, each one of its two allowed words, else its default."""
    out = dict(DEFAULT_SETTINGS)
    if isinstance(data, dict):
        if data.get("menu") in MENUS:
            out["menu"] = data["menu"]
        if data.get("look") in LOOKS:
            out["look"] = data["look"]
    return out


def read_settings():
    return clean_settings(read_json(state_file("settings.json")))


def write_settings(menu, look):
    if menu not in MENUS or look not in LOOKS:
        raise ValueError("bad settings")
    ensure_state_dir()
    body = json.dumps({"schema": 1, "menu": menu, "look": look}, sort_keys=True)
    write_whole(state_file("settings.json"), body.encode("utf-8"))


# ---------------------------------------------------------------- paths

def inside(root, path):
    """True when path is root or below it. Both are real paths."""
    try:
        return os.path.commonpath([root, path]) == root
    except ValueError:  # different drives on Windows
        return False


def same_path(a, b):
    return os.path.normcase(a) == os.path.normcase(b)


def parts_ok(parts):
    for p in parts:
        if not p or p.startswith(".") or p.endswith(".") or p.endswith(" "):
            return False
        if p.lower() == "claude.md":
            return False
    return True


def clean_parts(raw):
    """Split a request path into safe parts, or None to refuse. Refused: a backslash, a colon,
    %5c, NUL, an empty part (a folder), '.', '..', any part starting with a dot, CLAUDE.md in
    any case, a part ending in a dot or a space."""
    if "\\" in raw or "%5c" in raw.lower() or "%00" in raw:
        return None
    path = unquote(raw)
    if "\\" in path or ":" in path or "\x00" in path:
        return None
    parts = path.split("/")
    return parts if parts_ok(parts) else None


def rel_parts(root, target):
    return os.path.relpath(target, root).replace("\\", "/").split("/")


def file_inside(root, parts):
    """The real path of the file parts name below root, or None when it is not a regular file
    inside root, or when where it really leads breaks the same rules."""
    try:
        root = real_path(root)
        target = os.path.realpath(os.path.join(root, *parts))
    except (OSError, ValueError):
        return None
    if not os.path.isdir(root) or not inside(root, target) or same_path(root, target):
        return None
    if not os.path.isfile(target) or not parts_ok(rel_parts(root, target)):
        return None
    return target


def etag(st):
    return '"%x-%x"' % (st.st_size, st.st_mtime_ns)


# ---------------------------------------------------------------- opening things

def open_record(kind, target):
    """With MERLIN_HOME_OPEN_RECORD set, record the open there instead. True when recorded."""
    rec = os.environ.get("MERLIN_HOME_OPEN_RECORD")
    if not rec:
        return None
    try:
        with open(rec, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": kind, "target": target}) + "\n")
        return True
    except OSError:
        return False


def child_kw():
    kw = {"stdin": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kw["creationflags"] = NO_WINDOW
    return kw


def open_url(url):
    """Open an address in this computer's own default browser. True when it went."""
    recorded = open_record("url", url)
    if recorded is not None:
        return recorded
    try:
        if os.name == "nt":
            os.startfile(url)
            return True
        if sys.platform == "darwin":
            return subprocess.run(["/usr/bin/open", url], timeout=15, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, **child_kw()).returncode == 0
        return bool(webbrowser.open(url))
    except (OSError, subprocess.SubprocessError, webbrowser.Error):
        return False


def open_folder(path):
    """Show a folder in Finder or File Explorer. True when it went. On a Mac a folder whose
    name has an extension may be a bundle that open would start, so that one is shown
    selected in its parent instead; nothing is ever run."""
    recorded = open_record("folder", path)
    if recorded is not None:
        return recorded
    if not os.path.isabs(path) or not os.path.isdir(path):
        return False
    try:
        if os.name == "nt":
            os.startfile(path)
            return True
        if sys.platform == "darwin":
            argv = ["/usr/bin/open"]
            if os.path.splitext(os.path.basename(path.rstrip(os.sep)))[1]:
                argv.append("-R")
            return subprocess.run(argv + ["--", path], timeout=15, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, **child_kw()).returncode == 0
        opener = shutil.which("xdg-open")
        if not opener:
            return False
        return subprocess.run([opener, path], timeout=15, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, **child_kw()).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def site_url_ok(url):
    """True only for an https address on www.praxisatwork.com: no user info, no port, nothing
    else."""
    if not isinstance(url, str) or not url.startswith(SITE) or len(url) > 2000:
        return False
    if any(ord(c) < 33 or ord(c) == 127 or c in '"<>\\`' for c in url):
        return False
    try:
        u = urlsplit(url)
        port = u.port
    except ValueError:
        return False
    return (u.scheme == "https" and u.netloc == SITE_HOST and u.hostname == SITE_HOST
            and port is None and u.username is None and u.password is None)


# ---------------------------------------------------------------- the page's data

_HOME_DATA = []
_HOME_DATA_LOCK = threading.Lock()


def home_data():
    """home_data.py beside this file, loaded by its path once (-I leaves this folder off the
    import path)."""
    with _HOME_DATA_LOCK:
        if not _HOME_DATA:
            path = os.path.join(skill_dir(), "home_data.py")
            spec = importlib.util.spec_from_file_location("merlin_home_data", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _HOME_DATA.append(mod)
        return _HOME_DATA[0]


def addon_definitions(addons_file):
    """{add-on id: definition} from the add-on list, read again on every call."""
    data = read_json(addons_file) if addons_file else None
    rows = data.get("addons") if isinstance(data, dict) else data
    out = {}
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict) and isinstance(r.get("id"), str):
            out.setdefault(r["id"], r)
    return out


def version_key(v):
    m = re.match(r"^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", str(v or ""))
    return [int(m.group(i) or 0) for i in (1, 2, 3)] if m else [-1, -1, -1]


def within(child, parent):
    c, p = real_path(child), real_path(parent)
    return c == p or c.startswith(p.rstrip(os.sep) + os.sep)


def install_folder(plugin, business):
    """The install folder of the highest installed version of a plugin that applies to this
    business folder, by the same rules home_data uses for the add-on state. None if none."""
    data = read_json(os.path.join(home_root(), ".claude", "plugins", "installed_plugins.json"))
    plugins = data.get("plugins") if isinstance(data, dict) else None
    entries = plugins.get(plugin) if isinstance(plugins, dict) and isinstance(plugin, str) else None
    if isinstance(entries, dict):
        entries = [entries]
    best = None
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict) or not isinstance(e.get("installPath"), str):
            continue
        scope = e.get("scope") or "user"
        pp = e.get("projectPath")
        if scope in ("project", "local") and not (pp and business and within(business, pp)):
            continue
        k = version_key(e.get("version"))
        if best is None or k > best[0]:
            best = (k, e["installPath"])
    if best is None or not os.path.isdir(best[1]):
        return None
    return real_path(best[1])


def bridge_command(definition, plugin_dir, business):
    """The argument list for an add-on's one named command, or None when it is not allowed:
    a script that is not a regular file inside the plugin's install folder, or arguments
    that are not plain strings."""
    bridge = definition.get("bridge") if isinstance(definition, dict) else None
    if not isinstance(bridge, dict) or not plugin_dir:
        return None
    script, args = bridge.get("script"), bridge.get("args", [])
    if not isinstance(script, str) or not script or not isinstance(args, list):
        return None
    if any(not isinstance(a, str) for a in args):
        return None
    if os.path.isabs(script) or "\x00" in script:
        return None
    parts = script.replace("\\", "/").split("/")
    if not parts_ok(parts):
        return None
    target = file_inside(plugin_dir, parts)
    if not target:
        return None
    return [sys.executable, "-I", "-B", target] + [business if a == BUSINESS_ARG else a for a in args]


def run_bridge(argv, cwd):
    """The first line the command prints, when it is a page address on this computer, else None."""
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("MERLIN_HOME_OPEN_RECORD", None)
    try:
        r = subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=BRIDGE_WAIT, **child_kw())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    line = r.stdout.decode("utf-8", "replace").split("\n", 1)[0].strip()
    if len(line) > 2000:
        return None
    m = BRIDGE_LINE.match(line)
    if not m or not 1 <= int(m.group(1)) <= 65535:
        return None
    return line


class BridgeCache:
    """The answer of each add-on's command, per folder and add-on, kept BRIDGE_KEEP seconds."""

    def __init__(self):
        self.lock = threading.Lock()
        self.kept = {}

    def get(self, key, make):
        with self.lock:
            hit = self.kept.get(key)
            if hit and time.monotonic() - hit[0] < BRIDGE_KEEP:
                return hit[1]
            value = make()
            self.kept[key] = (time.monotonic(), value)
            return value


def sample_address(fid, addon_id, definition, samples_root):
    """The address of an add-on's sample page, when that file exists under
    samples/<add-on id>/, else None."""
    sample = definition.get("sample") if isinstance(definition, dict) else None
    if not isinstance(sample, str) or not sample or not isinstance(addon_id, str):
        return None
    parts = sample.replace("\\", "/").split("/")
    if len(parts) > 2 and parts[0] == "samples" and parts[1] == addon_id:
        parts = parts[2:]
    if not parts_ok([addon_id]) or not parts_ok(parts):
        return None
    if not file_inside(os.path.join(samples_root, addon_id), parts):
        return None
    return "/h/%s/samples/%s/%s" % (fid, quote(addon_id, safe=""), "/".join(quote(p, safe="") for p in parts))


# ---------------------------------------------------------------- the server

class Handler(BaseHTTPRequestHandler):
    server_version = "MerlinHome"
    sys_version = ""
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):  # nothing is logged per request
        pass

    def version_string(self):
        return self.server_version

    def answer(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=None):
        self.send_response(code)
        if code != 204:
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command == "HEAD" or code == 204:
            return
        try:
            if body:
                self.wfile.write(body)
        except OSError:
            pass

    def refuse(self, code, text):
        self.answer(code, (text + "\n").encode("utf-8"))

    def send_error(self, code, message=None, explain=None):
        # A malformed request still gets the same headers and no server detail. The request
        # line was not read, so the answer is written as HTTP/1.0, with its headers.
        self.close_connection = True
        self.request_version = "HTTP/1.0"
        try:
            self.answer(code, b"bad request\n")
        except Exception:
            pass

    def host_ok(self):
        host = (self.headers.get("Host") or "").strip().lower()
        return host in (HOST, "%s:%d" % (HOST, self.server.server_address[1]))

    def cross_site(self):
        return (self.headers.get("Sec-Fetch-Site") or "").strip().lower() == "cross-site"

    def do_GET(self):
        # The hook's probe is not use: a server only probed still stops when idle.
        if self.path.split("?", 1)[0].split("#", 1)[0] != "/__merlin":
            self.server.touch()
        try:
            self.route()
        except Exception:  # never leak a trace to the caller
            try:
                self.refuse(500, "error")
            except Exception:
                pass

    do_HEAD = do_GET

    def not_allowed(self):
        self.server.touch()
        self.answer(405, b"method not allowed\n", extra={"Allow": "GET, HEAD"})

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = do_CONNECT = not_allowed

    def query(self):
        q = self.path.split("#", 1)[0].partition("?")[2]
        try:
            got = parse_qs(q, keep_blank_values=True, strict_parsing=False, max_num_fields=8)
        except (TypeError, ValueError):
            return {}
        return {k: v[0] for k, v in got.items() if v}

    def route(self):
        if not self.host_ok():
            return self.refuse(403, "forbidden")
        raw = self.path.split("?", 1)[0].split("#", 1)[0]
        if raw == "/__merlin":
            info = {"name": NAME}
            value = self.query().get("c", "")
            port, pid = self.server.server_address[1], os.getpid()  # the socket's own port
            proof = probe_proof(read_secret(), value, port, pid, self.server.version)
            if proof:  # only to a caller that sent a fresh value in its fixed form
                info.update({"version": self.server.version, "pid": pid, "port": port, "proof": proof})
            return self.answer(200, json.dumps(info).encode("utf-8"), "application/json; charset=utf-8")
        segs = raw.split("/", 3)
        if len(segs) < 4 or segs[0] != "" or segs[1] != "h":
            return self.refuse(404, "not found")
        fid = segs[2]
        entry = read_registry().get(fid)
        if not isinstance(entry, dict) or entry.get("kind") != "business" or not isinstance(entry.get("path"), str):
            return self.refuse(404, "not found")
        business = entry["path"]
        if not os.path.isdir(business):
            return self.refuse(404, "not found")
        parts = clean_parts(segs[3])
        if parts is None:
            return self.refuse(404, "not found")
        if len(parts) == 1 and parts[0] in ACTIONS:
            return self.action(parts[0], fid, business)
        if parts == ["data.json"]:
            return self.data(fid, business)
        if len(parts) >= 3 and parts[0] == "samples":
            return self.sample(parts[1], parts[2:])
        if len(parts) == 1 and parts[0] in SHELL_FILES:
            return self.shell_file(parts[0])
        return self.refuse(404, "not found")

    # ------------------------------------------------------------ files

    def send_file(self, path, extra_headers=None, rewrite=None):
        try:
            with open(path, "rb") as fh:
                st = os.fstat(fh.fileno())
                body = fh.read()
        except OSError:
            return self.refuse(404, "not found")
        extra = {"Last-Modified": email.utils.formatdate(st.st_mtime, usegmt=True)}
        if rewrite is None:
            extra["ETag"] = etag(st)
        else:
            body = rewrite(body)
        extra.update(extra_headers or {})
        ctype = TYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")
        self.answer(200, body, ctype, extra=extra)

    def shell_file(self, name):
        path = file_inside(shell_dir(), [name])
        if not path:
            return self.refuse(404, "not found")
        if name != "home.html":
            return self.send_file(path)
        s = read_settings()

        def rewrite(body):
            text = body.decode("utf-8")
            text = text.replace('data-mode="dark"', 'data-mode="%s"' % s["look"], 1)
            text = text.replace('data-layout="top"', 'data-layout="%s"' % s["menu"], 1)
            return text.encode("utf-8")

        self.send_file(path, {"Content-Security-Policy": HOME_CSP, "X-Frame-Options": "SAMEORIGIN"}, rewrite)

    def sample(self, addon_id, parts):
        root = os.path.join(self.server.samples_root(), addon_id)
        path = file_inside(root, parts) if parts_ok([addon_id]) else None
        if not path:
            return self.refuse(404, "not found")
        extra = {}
        if os.path.splitext(path)[1].lower() in (".html", ".htm", ".svg"):
            extra["Content-Security-Policy"] = SAMPLE_CSP
        self.send_file(path, extra)

    # ------------------------------------------------------------ data.json

    def data(self, fid, business):
        if self.cross_site():
            return self.refuse(403, "forbidden")
        addons_file = self.server.addons_file()
        home = home_data().read_home(business, addons_file=addons_file)
        defs = addon_definitions(addons_file)
        samples_root = self.server.samples_root()
        secret = read_secret()
        pages = {}
        for a in home.get("addons") or []:
            aid = a.get("id")
            if not isinstance(aid, str):
                continue
            d = defs.get(aid, {})
            url = None
            if a.get("state") == "activated":
                url = self.server.bridge.get((fid, aid), lambda: self.bridge_url(d, a, business))
            pages[aid] = {"state": a.get("state"), "url": url,
                          "sample": sample_address(fid, aid, d, samples_root)}
        actions = {}
        for act, key_name in ACTION_NAMES.items():
            k = action_key(secret, act, fid)
            if k:
                actions[key_name] = "/h/%s/%s?k=%s" % (fid, act, k)
        doc = {"home": home, "settings": read_settings(), "actions": actions, "addon_pages": pages}
        body = json.dumps(doc, ensure_ascii=False).encode("utf-8")
        self.answer(200, body, "application/json; charset=utf-8")

    def bridge_url(self, definition, addon, business):
        plugin_dir = install_folder(addon.get("plugin"), business)
        argv = bridge_command(definition, plugin_dir, business)
        if not argv:
            return None
        return self.server.run_bridge(argv, plugin_dir)

    # ------------------------------------------------------------ the three actions

    def action(self, name, fid, business):
        """Nothing runs on HEAD, from another website, or without the right key."""
        if self.command != "GET":
            return self.answer(405, b"method not allowed\n", extra={"Allow": "GET"})
        if self.cross_site():
            return self.refuse(403, "forbidden")
        q = self.query()
        given = q.get("k", "")
        want = action_key(read_secret(), name, fid)
        if not want or not hmac.compare_digest(given.encode("utf-8", "replace"), want.encode("ascii")):
            return self.refuse(403, "forbidden")
        if name == ACT_SETTINGS:
            menu, look = q.get("menu"), q.get("look")
            if menu not in MENUS or look not in LOOKS:
                return self.refuse(400, "bad settings")
            with self.server.settings_lock:
                try:
                    write_settings(menu, look)
                except OSError:
                    return self.refuse(500, "not saved")
            return self.answer(204)
        if name == ACT_OPEN_URL:
            url = q.get("url", "")
            if not site_url_ok(url):
                return self.refuse(400, "not an allowed address")
            return self.answer(204) if self.server.open_url(url) else self.refuse(500, "not opened")
        if name == ACT_OPEN_FOLDER:
            ident = q.get("id", "")
            if not re.fullmatch(r"[0-9a-f]{12}", ident or ""):
                return self.refuse(404, "not found")
            path = self.project_path(business, ident)
            if not path:
                return self.refuse(404, "not found")
            return self.answer(204) if self.server.open_folder(path) else self.refuse(500, "not opened")
        return self.refuse(404, "not found")

    def project_path(self, business, ident):
        """The folder of the project or construction project with this page id, found by
        reading the business folder's own data again."""
        home = home_data().read_home(business, addons_file=self.server.addons_file())
        rows = list(home.get("projects") or [])
        rows += list((home.get("construction_projects") or {}).get("items") or [])
        for r in rows:
            if isinstance(r, dict) and r.get("id") == ident and isinstance(r.get("path"), str):
                p = real_path(r["path"])
                return p if os.path.isdir(p) else None
        return None


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # On a Mac the same port must be reusable at once after an older server stops. On
    # Windows the same option would let two servers listen on one port, so it is off there.
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        ThreadingHTTPServer.server_bind(self)

    reqs = 0             # requests counted as use, for the stop line in the log
    idle_stop = False    # set when the idle watch stops it
    addons_path = None

    def setup_parts(self, addons_path=None):
        self.version = own_version()
        self.addons_path = addons_path
        self.bridge = BridgeCache()
        self.settings_lock = threading.Lock()
        self.open_url = open_url
        self.open_folder = open_folder
        self.run_bridge = run_bridge
        self.last = time.time()

    def addons_file(self):
        return self.addons_path or default_addons_file()

    def samples_root(self):
        return os.path.join(os.path.split(self.addons_file())[0], "samples")

    def touch(self):
        self.last = time.time()
        self.reqs += 1


# ---------------------------------------------------------------- log

LOG_LINE = 200     # bytes per line, its end of line included
LOG_MAX = 256 * 1024


def plugin_version():
    """The plugin's version from its .claude-plugin/plugin.json, or "source"."""
    root = os.sep.join(here_parts()[:-3])
    try:
        with open(os.path.join(root, ".claude-plugin", "plugin.json"), encoding="utf-8") as fh:
            v = json.load(fh).get("version")
        return v if isinstance(v, str) and re.fullmatch(r"[0-9A-Za-z.+-]{1,40}", v) else "source"
    except (OSError, ValueError, AttributeError, TypeError):
        return "source"


def home_log(step, out, port, **more):
    """One line in ~/.merlin/home/home.log: codes and numbers only, never a path or an id.
    Never creates the folder and never raises."""
    try:
        sdir = state_dir()
        if not os.path.isdir(sdir):
            return
        path = os.path.join(sdir, "home.log")
        try:
            if os.path.getsize(path) > LOG_MAX:
                os.replace(path, path + ".1")
        except OSError:
            pass
        fields = [("pid", os.getpid()), ("v", plugin_version()),
                  ("os", "win" if os.name == "nt" else ("mac" if sys.platform == "darwin" else "other")),
                  ("step", step), ("out", out), ("port", port), ("ms", int((time.monotonic() - _T0) * 1000))]
        fields += sorted(more.items())
        parts = [datetime.now().astimezone().isoformat(timespec="seconds"), "serve"]
        parts += ["%s=%s" % (k, re.sub(r"[^0-9A-Za-z._+-]", "_", str(v))[:40]) for k, v in fields if v is not None]
        line = " ".join(parts).encode("ascii", "replace")[:LOG_LINE - 1] + b"\n"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except BaseException:
        pass


def remove_state_if_mine(path):
    data = read_json(path)
    if isinstance(data, dict) and data.get("pid") == os.getpid():
        try:
            os.remove(path)
        except OSError:
            pass


def main(argv):
    ap = argparse.ArgumentParser(description="Merlin home server")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--idle", type=float, default=None, help="seconds without a request before it stops")
    ap.add_argument("--addons", default=None, help="the add-on list (default: addons/addons.json beside this file)")
    a = ap.parse_args(argv)
    idle = a.idle if a.idle is not None else float(os.environ.get("MERLIN_HOME_IDLE") or IDLE_DEFAULT)
    if a.port not in port_range():
        home_log("bind", "range", a.port)
        return 2
    try:
        httpd = Server((HOST, a.port), Handler)
    except OSError as exc:
        home_log("bind", "fail", a.port, err=getattr(exc, "winerror", None) or exc.errno or 0)
        return 1  # the port is taken: another server already answers there
    httpd.setup_parts(os.path.abspath(a.addons) if a.addons else None)
    state = state_file("server.json")
    try:
        ensure_state_dir()
        write_whole(state, json.dumps({"name": NAME, "port": a.port, "pid": os.getpid(),
                                       "version": httpd.version}).encode("utf-8"))
    except OSError:
        pass
    home_log("listen", "ok", a.port, sv=httpd.version)
    began = time.time()

    def watch():
        step = max(0.2, min(30.0, idle / 4.0))
        while True:
            time.sleep(step)
            if time.time() - httpd.last > idle:
                httpd.idle_stop = True
                httpd.shutdown()
                return

    threading.Thread(target=watch, daemon=True).start()
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:  # Ctrl-C is a plain stop
        pass
    finally:
        httpd.server_close()
        remove_state_if_mine(state)
        home_log("stop", "idle" if httpd.idle_stop else "other", a.port,
                 up_s=int(time.time() - began), reqs=httpd.reqs)
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):  # Windows consoles are not UTF-8
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass
    sys.exit(main(sys.argv[1:]))
