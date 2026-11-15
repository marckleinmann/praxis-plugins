#!/usr/bin/env python3
"""
granola_screenshots.py: save every screenshot Granola captured in one meeting,
beside that meeting's saved verbatim transcript.

WHY A SCRIPT. Granola's MCP `get_meeting_attachment` returns each screenshot as an
image inside the model's context, five at most per call, often one to three. A
142-screenshot install call would cost dozens of calls and fill the context. Granola's
official API (public-api.granola.ai, the same `grn_` key meeting-prep uses) returns each
attachment's direct image link and capture time, so this script downloads them all with
no image passing through the model.

TRANSCRIPT PRIMACY. The API also returns Granola's AI summary, its notes, and a
Granola-written description of every screenshot. This script DISCARDS all of them on
receipt and writes none to disk: it keeps only the image, its id, its type, its size,
its capture time, and the verbatim transcript words spoken at that moment. A screenshot
may be cited only after it is viewed, by file name and offset. The description never is.

OUTPUT, for transcript `meeting_<date>_<slug>.md`:
  meeting_<date>_<slug>_screenshots/
    001_06m05s.png ...   sequence, then offset from the first transcript line, the
                         same [m:ss] clock Granola's transcript view and the MCP use
    index.md             one row per file: offset, clock time, file, attachment id,
                         size, and the verbatim words being spoken at capture
    index.json           the same, machine-readable
A meeting with no screenshots creates no folder.

USAGE
  python3 granola_screenshots.py --meeting <uuid | not_... | Granola URL> \
      --transcript <path to meeting_<date>_<slug>.md>      # folder goes beside it
  python3 granola_screenshots.py --meeting <id> --out <dir>  # explicit folder
  --dry-run   list what would be fetched; write nothing
  --check     verify the key works; exit

CREDENTIAL. env GRANOLA_API_KEY, else macOS keychain service "Merlin" account
GRANOLA_API_KEY. Never printed. Store one with:
  security add-generic-password -a GRANOLA_API_KEY -s Merlin -U -w
(the key comes from the Granola app: Settings > Connectors > API keys).

EXIT CODES. 0 ok (including zero screenshots), 2 no key or bad key, 3 API error,
4 meeting not found, 5 some downloads failed after retries (the index lists them).
Stdout ends with one JSON line: {"status", "count", "saved", "failed", "folder"}.
Stdlib only.
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://public-api.granola.ai/v1"
KEYCHAIN_SERVICE = "Merlin"
KEYCHAIN_ACCOUNT = "GRANOLA_API_KEY"
UA = "merlin-granola-screenshots/1.0"
RETRIES = 5
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)

# The only attachment fields kept. Everything else Granola sends (description above
# all) is dropped here, before anything is written.
KEEP_ATTACHMENT = ("id", "type", "url", "width", "height", "captured_at")


def die(code, msg):
    print(msg, file=sys.stderr)
    print(json.dumps({"status": "error", "code": code, "message": msg.splitlines()[0]}))
    sys.exit(code)


def api_key():
    k = os.environ.get("GRANOLA_API_KEY", "").strip()
    if k:
        return k
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-a", KEYCHAIN_ACCOUNT, "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    die(2, "No Granola API key. Store it with:\n"
           "  security add-generic-password -a GRANOLA_API_KEY -s Merlin -U -w\n"
           "or follow the MCP fallback in meeting-dissect SKILL.md § Screenshots.")


def http(url, key=None, binary=False):
    """GET with retry on 429 and 5xx, honouring Retry-After. Returns bytes or parsed JSON."""
    headers = {"User-Agent": UA}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    delay = 2.0
    for attempt in range(1, RETRIES + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
                body = r.read()
            return body if binary else json.loads(body)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403) and key:
                die(2, f"Granola rejected the API key (HTTP {e.code}).")
            if e.code in (400, 404) and not binary:
                raise
            if e.code == 429 or e.code >= 500:
                wait = e.headers.get("Retry-After")
                time.sleep(float(wait) if wait and wait.replace(".", "").isdigit() else delay)
                delay = min(delay * 2, 60)
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            time.sleep(delay)
            delay = min(delay * 2, 60)
    raise RuntimeError(f"gave up after {RETRIES} tries")


def resolve_note_id(key, meeting):
    """Map an MCP meeting UUID or Granola URL to the API's not_ id."""
    if meeting.startswith("not_"):
        return meeting
    m = UUID_RE.search(meeting)
    if not m:
        die(4, f"Not a Granola meeting id or URL: {meeting}")
    uuid = m.group(0).lower()
    cursor, scanned = None, 0
    while scanned < 2000:
        q = {"page_size": "30"}
        if cursor:
            q["cursor"] = cursor
        page = http(f"{API}/notes?{urllib.parse.urlencode(q)}", key)
        for n in page.get("notes", []):
            scanned += 1
            full = http(f"{API}/notes/{n['id']}", key)
            if (full.get("web_url") or "").lower().rstrip("/").endswith(uuid):
                return n["id"]
        if not page.get("hasMore"):
            break
        cursor = page.get("cursor")
    die(4, f"No Granola note matches meeting {uuid} (scanned {scanned}). Pass the not_ id instead.")


def parse_ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def offset_label(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}m{seconds % 60:02d}s"


def spoken_at(transcript, when):
    """The verbatim words of the transcript block covering `when`, else the last one before it."""
    best = None
    for b in transcript:
        st = b.get("start")
        if st is None or st > when:
            continue
        best = b
        if b.get("end") and b["end"] >= when:
            break
    if not best:
        return ""
    t = " ".join((best.get("text") or "").split())
    return t[:140] + ("..." if len(t) > 140 else "")


def ext_for(url, ctype):
    if ctype and "jpeg" in ctype:
        return "jpg"
    if ctype and "webp" in ctype:
        return "webp"
    path = urllib.parse.urlparse(url).path.lower()
    for e in ("png", "jpg", "jpeg", "webp", "gif"):
        if path.endswith("." + e):
            return "jpg" if e == "jpeg" else e
    return "png"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--meeting", help="MCP meeting UUID, not_ id, or Granola note URL")
    ap.add_argument("--transcript", help="saved transcript; the folder goes beside it")
    ap.add_argument("--out", help="explicit output folder")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()

    key = api_key()
    if a.check:
        http(f"{API}/notes?page_size=1", key)
        print(json.dumps({"status": "ok", "check": "key works"}))
        return 0
    if not a.meeting or not (a.transcript or a.out):
        ap.error("--meeting and one of --transcript / --out are required")

    if a.out:
        folder = a.out
    else:
        base = os.path.basename(a.transcript)
        stem = base[:-3] if base.endswith(".md") else base
        folder = os.path.join(os.path.dirname(os.path.abspath(a.transcript)), stem + "_screenshots")

    note_id = resolve_note_id(key, a.meeting)
    try:
        full = http(f"{API}/notes/{note_id}?include=transcript", key)
    except urllib.error.HTTPError as e:
        die(4 if e.code in (400, 404) else 3, f"Granola API error {e.code} reading {note_id}.")
    except RuntimeError as e:
        die(3, f"Granola API unreachable: {e}")

    # Keep only what is admissible. Summary, notes and descriptions go no further.
    web_url = full.get("web_url") or ""
    title = full.get("title") or ""
    atts = [{k: x.get(k) for k in KEEP_ATTACHMENT} for x in (full.get("attachments") or [])]
    transcript = []
    for b in full.get("transcript") or []:
        try:
            transcript.append({
                "start": parse_ts(b["start_time"]),
                "end": parse_ts(b["end_time"]) if b.get("end_time") else None,
                "text": b.get("text") or "",
            })
        except (KeyError, ValueError):
            continue
    del full

    if not atts:
        print(f"0 screenshots for {title!r}. No folder created.")
        print(json.dumps({"status": "ok", "count": 0, "saved": 0, "failed": 0, "folder": None}))
        return 0

    for x in atts:
        x["_t"] = parse_ts(x["captured_at"]) if x.get("captured_at") else None
    atts.sort(key=lambda x: (x["_t"] is None, x["_t"] or dt.datetime.max.replace(tzinfo=dt.timezone.utc)))
    t0 = min([b["start"] for b in transcript] or [x["_t"] for x in atts if x["_t"]])

    rows, failed, saved = [], [], 0
    if not a.dry_run:
        os.makedirs(folder, exist_ok=True)
    for i, x in enumerate(atts, 1):
        off = (x["_t"] - t0).total_seconds() if x["_t"] else None
        label = offset_label(off) if off is not None else "unknown"
        row = {
            "n": i,
            "offset": label if off is not None else None,
            "captured_at": x.get("captured_at"),
            "attachment_id": x.get("id"),
            "type": x.get("type"),
            "width": x.get("width"),
            "height": x.get("height"),
            "spoken_at_capture": spoken_at(transcript, x["_t"]) if x["_t"] else "",
        }
        if a.dry_run:
            row["file"] = f"{i:03d}_{label}.<ext>"
            rows.append(row)
            continue
        existing = [f for f in os.listdir(folder) if f.startswith(f"{i:03d}_{label}.")]
        if existing:
            row["file"] = existing[0]
            with open(os.path.join(folder, existing[0]), "rb") as fh:
                row["sha256"] = hashlib.sha256(fh.read()).hexdigest()
            rows.append(row)
            saved += 1
            continue
        try:
            req = urllib.request.Request(x["url"], headers={"User-Agent": UA})
            body, ctype = None, None
            delay = 2.0
            for _ in range(RETRIES):
                try:
                    with urllib.request.urlopen(req, timeout=120) as r:
                        ctype = r.headers.get("Content-Type", "")
                        body = r.read()
                    break
                except urllib.error.HTTPError as e:
                    if e.code == 429 or e.code >= 500:
                        time.sleep(delay)
                        delay = min(delay * 2, 60)
                        continue
                    raise
                except (urllib.error.URLError, TimeoutError):
                    time.sleep(delay)
                    delay = min(delay * 2, 60)
            if not body:
                raise RuntimeError("empty or no response")
            fname = f"{i:03d}_{label}.{ext_for(x['url'], ctype)}"
            with open(os.path.join(folder, fname), "wb") as fh:
                fh.write(body)
            row["file"] = fname
            row["sha256"] = hashlib.sha256(body).hexdigest()
            saved += 1
        except Exception as e:  # one bad image never sinks the batch
            row["file"] = None
            row["error"] = str(e)[:200]
            failed.append(x.get("id"))
        rows.append(row)

    if a.dry_run:
        for r in rows:
            print(f"{r['n']:>3}  {r['offset'] or '?':>8}  {r['attachment_id']}")
        print(json.dumps({"status": "dry-run", "count": len(rows), "saved": 0, "failed": 0, "folder": folder}))
        return 0

    fetched = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta = {
        "source": "granola public API",
        "granola_note_id": note_id,
        "granola_url": web_url,
        "meeting_title": title,
        "fetched_at": fetched,
        "offset_origin": t0.isoformat(),
        "count": len(rows),
        "saved": saved,
        "failed": len(failed),
        "discarded_on_receipt": ["summary", "private notes", "attachment descriptions"],
    }
    with open(os.path.join(folder, "index.json"), "w") as fh:
        json.dump({"meta": meta, "screenshots": rows}, fh, indent=1, ensure_ascii=False)

    def cell(s):
        return (s or "").replace("|", "\\|").replace("\n", " ")

    lines = [
        "---",
        "type: meeting-screenshots",
        f"granola_note_id: {note_id}",
        f"granola_url: {web_url}",
        f"meeting_title: {json.dumps(title, ensure_ascii=False)}",
        f"fetched_at: {fetched}",
        f"offset_origin: {t0.isoformat()}",
        f"count: {len(rows)}",
        f"saved: {saved}",
        f"failed: {len(failed)}",
        "---",
        "",
        f"# Screenshots: {title}",
        "",
        "Offsets count from the first transcript line, the same [m:ss] clock as Granola's",
        "transcript. The last column is the verbatim transcript text being spoken when the",
        "screenshot was taken, so a row can be matched to its transcript line with grep.",
        "",
        "**Source rule.** These are the images only. Granola's description of each screenshot",
        "is AI-written and was discarded on receipt. Cite a screenshot only after viewing it,",
        "by file name and offset, for example `014_23m10s.png` at 23:10.",
        "",
        "| # | Offset | Clock (UTC) | File | Attachment id | Size | Spoken at capture (verbatim) |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        clock = (r.get("captured_at") or "")[11:19]
        size = f"{r.get('width')}x{r.get('height')}" if r.get("width") else ""
        f = r.get("file") or f"FAILED: {cell(r.get('error'))}"
        lines.append(f"| {r['n']} | {r['offset'] or '?'} | {clock} | {f} | {r['attachment_id']} | {size} | {cell(r['spoken_at_capture'])} |")
    with open(os.path.join(folder, "index.md"), "w") as fh:
        fh.write("\n".join(lines) + "\n")

    status = "ok" if not failed else "partial"
    print(f"{saved} of {len(rows)} screenshots saved to {folder}" + (f"; {len(failed)} failed" if failed else ""))
    print(json.dumps({"status": status, "count": len(rows), "saved": saved, "failed": len(failed), "folder": folder}))
    return 5 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
