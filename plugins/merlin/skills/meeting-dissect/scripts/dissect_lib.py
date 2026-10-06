"""Shared parser for meeting-dissect files and numbered transcripts.

One implementation of "what is an item", "which lines does it cite", "is it
closed", and "which section is it in", so carry_forward.py, validate.py,
coverage.py and diff_passes.py cannot disagree.

Standard library only.
"""

import os
import re

CLOSED_STATUSES = {"fulfilled", "withdrawn", "superseded"}
# Owed work: stays under "Still open from prior meetings".
OWED_TAGS = {"ASK", "OPEN", "RISK", "COMMITMENT", "OFFER"}
# Facts that stay true until something changes them: "Standing context".
STANDING_TAGS = {"OBJECTION", "CONSTRAINT", "SPEC", "PRIORITY", "DECISION"}
ALL_TAGS = OWED_TAGS | STANDING_TAGS

UNLABELLED = "(unlabelled)"
# The user's own speaker labels. Pass the user's name with --operator-label
# (repeat the flag for each label, including "Me" and "Microphone"); a passed
# list replaces this default.
OPERATOR_LABELS = {"me", "microphone"}

SEC_STILL_OPEN = "still_open"
SEC_STANDING = "standing"
SEC_CLOSED = "closed"
SEC_LEGACY_CARRIED = "legacy_carried"
SEC_THIS_MEETING = "this_meeting"
SEC_OTHER = "other"

NEW_FORMAT_SECTIONS = {SEC_STILL_OPEN, SEC_STANDING, SEC_CLOSED}

# Item heads. The first is the shape SKILL.md Step 3 specifies. The other two
# exist in the live corpus (measured 2026-09-16 across 41 dissects) and are read
# so an older file is never silently parsed as empty.
ITEM_RE = re.compile(r"^- \[[ xX]\] `([A-Z]+)` \*\*(.+?)\*\*(.*)$")
ITEM_H3_RE = re.compile(r"^### \d+[a-z]?\. `?([A-Z]{3,})`?(?: \([^)]*\))?:? (.+)$")
ITEM_NUM_RE = re.compile(r"^\d+[a-z]?\. \*\*([A-Z]{3,})(?: [a-z]+)?, (.+?)\*\*(.*)$")
FIELD_RE = re.compile(r"^(?:\s{2,}|\s*- )([A-Za-z][A-Za-z /\-]*?(?: \d{4}-\d{2}-\d{2})?(?: \([^)]*\))?):\s?(.*)$")
# An L-reference. _cited_lines drops refs that follow another meeting's date
# in the same clause ("from 2026-09-14 L40, L42").
LREF_RE = re.compile(r"(?<![\w-])L(\d+)(?:\s*-\s*L?(\d+))?")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
CLAUSE_SPLIT_RE = re.compile(r"[)\n;\"]")
LINE_FIELD_NUM_RE = re.compile(r"(\d+)(?:\s*-\s*(\d+))?")


def classify_heading(text):
    t = text.strip().lower()
    if t.startswith("still open from prior"):
        return SEC_STILL_OPEN
    if t.startswith("standing context"):
        return SEC_STANDING
    if t.startswith("closed since"):
        return SEC_CLOSED
    if t.startswith("carried forward"):
        return SEC_LEGACY_CARRIED
    if t.startswith("this meeting"):
        return SEC_THIS_MEETING
    return SEC_OTHER


def parse_frontmatter(text):
    fm = {}
    if not text.startswith("---"):
        return fm, text
    end = text.find("\n---", 3)
    if end == -1:
        return fm, text
    for line in text[3:end].splitlines():
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if m:
            val = m.group(2)
            val = re.sub(r"\s+#.*$", "", val).strip()
            fm[m.group(1)] = val
    return fm, text[end + 4:]


def expand_range(a, b, max_span=None):
    """Lines a..b. When max_span is set and the range is wider, endpoints only."""
    a, b = int(a), int(b if b else a)
    if b < a:
        a, b = b, a
    if max_span is not None and (b - a) >= max_span:
        return [a, b], True
    return list(range(a, b + 1)), False


def status_word(status):
    m = re.match(r"\s*([A-Za-z]+)", status or "")
    return m.group(1).lower() if m else ""


def is_closed(item):
    return status_word(item.get("status")) in CLOSED_STATUSES


class Item(dict):
    pass


def _cited_lines(item, max_span):
    lines = set()
    wide = False
    for f in item["fields"]:
        key, val = f
        if key.lower() == "line":
            for a, b in LINE_FIELD_NUM_RE.findall(val):
                got, w = expand_range(a, b or a, max_span)
                lines.update(got)
                wide = wide or w
    # L123 style refs anywhere in the item. A ref that follows a date other
    # than this file's meeting date, in the same clause, points at another
    # meeting ("from 2026-09-14 L40, L42") and is excluded.
    own = item.get("_meeting_date", "")
    for chunk in CLAUSE_SPLIT_RE.split(item["raw"]):
        foreign_from = None
        for dm in DATE_RE.finditer(chunk):
            if dm.group(0) != own:
                foreign_from = dm.start()
                break
        for m in LREF_RE.finditer(chunk):
            if foreign_from is not None and m.start() > foreign_from:
                continue
            got, w = expand_range(m.group(1), m.group(2) or m.group(1), max_span)
            lines.update(got)
            wide = wide or w
    return lines, wide


def parse_dissect(path, max_span=3):
    """Parse a dissect (or a pass-B extraction) file.

    Returns dict(frontmatter, items, sections_present, path).
    Each item: tag, statement, section, subsection, fields (ordered list),
    id (explicit or derived), id_explicit, status, pointer, quote, lines,
    wide_range, raw (the verbatim block text).
    """
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    fm, body = parse_frontmatter(text)
    meeting_date = fm.get("meeting_date", "undated")
    items = []
    sections_present = set()
    section = SEC_OTHER
    subsection = ""
    cur = None
    counters = {}

    def close_item():
        nonlocal cur
        if cur is None:
            return
        cur["raw"] = "\n".join(cur["_rawlines"]).rstrip()
        del cur["_rawlines"]
        fields = {k.lower(): v for k, v in cur["fields"]}
        cur["status"] = fields.get("status", "")
        cur["pointer"] = fields.get("pointer", "")
        cur["quote"] = " ".join(v for k, v in cur["fields"] if k.lower().startswith("quote") or k.lower().startswith("restated"))
        explicit = fields.get("id", "").strip()
        if explicit:
            cur["id"] = explicit
            cur["id_explicit"] = True
        else:
            prefix = "c" if cur["section"] in (SEC_LEGACY_CARRIED, SEC_STILL_OPEN, SEC_STANDING, SEC_CLOSED) else ""
            key = prefix
            counters[key] = counters.get(key, 0) + 1
            cur["id"] = f"{meeting_date}#{prefix}{counters[key]:02d}"
            cur["id_explicit"] = False
        cur["lines"], cur["wide_range"] = _cited_lines(cur, max_span)
        del cur["_meeting_date"]
        items.append(cur)
        cur = None

    for line in body.splitlines():
        h2 = re.match(r"^## (.+)$", line)
        h3 = re.match(r"^### (.+)$", line)
        if h2:
            close_item()
            section = classify_heading(h2.group(1))
            subsection = ""
            sections_present.add(section)
            continue
        m = ITEM_RE.match(line) or ITEM_NUM_RE.match(line)
        if not m and h3:
            mh = ITEM_H3_RE.match(line)
            if mh and mh.group(1) in ALL_TAGS | {"CONTEXT"}:
                m = mh
        if h3 and not m:
            close_item()
            subsection = h3.group(1).strip()
            continue
        if m:
            close_item()
            trailer = m.group(3).strip() if m.re.groups >= 3 else ""
            cur = Item(tag=m.group(1), statement=m.group(2).strip().strip("*"), trailer=trailer,
                       section=section, subsection=subsection, fields=[], _rawlines=[line],
                       _meeting_date=meeting_date)
            continue
        if cur is not None:
            if line.strip() == "":
                cur["_rawlines"].append(line)
                continue
            fm_ = FIELD_RE.match(line)
            if fm_:
                cur["fields"].append((fm_.group(1).strip(), fm_.group(2).strip()))
                cur["_rawlines"].append(line)
                continue
            if line.startswith("      ") or line.startswith("\t"):
                if cur["fields"]:
                    k, v = cur["fields"][-1]
                    cur["fields"][-1] = (k, (v + " " + line.strip()).strip())
                cur["_rawlines"].append(line)
                continue
            close_item()
    close_item()
    return {"path": path, "frontmatter": fm, "items": items, "sections_present": sections_present}


def is_new_format(parsed):
    return bool(parsed["sections_present"] & NEW_FORMAT_SECTIONS)


def list_dissects(directory):
    out = []
    for name in sorted(os.listdir(directory)):
        if name.startswith("dissect_") and name.endswith(".md"):
            out.append(os.path.join(directory, name))
    return out


def prior_live_items(parsed):
    """The item set a later meeting must account for: every item in the file
    except the ones in its own Closed section. Already-closed items are
    included; carry_forward.py pre-closes them with a pointer so the invariant
    counts the whole prior file."""
    return [it for it in parsed["items"] if it["section"] != SEC_CLOSED]


def target_section(item):
    if is_closed(item):
        return SEC_CLOSED
    if item["tag"] == "OFFER" and status_word(item.get("status")) == "declined":
        return SEC_STANDING
    if item["tag"] in STANDING_TAGS:
        return SEC_STANDING
    return SEC_STILL_OPEN


def read_transcript(path):
    """Numbered speaker turns from a transcript file.

    Returns list of dict(line, speaker, text, words). Line numbers are the
    file's own, as Read returns them. Raises ValueError when the file has no
    ## Transcript section or no Label: text turns.
    """
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    start = None
    for i, l in enumerate(lines):
        if re.match(r"^## Transcript\s*$", l):
            start = i + 1
            break
    if start is None:
        raise ValueError(f"{path}: no '## Transcript' section")
    turns = []
    for i in range(start, len(lines)):
        l = lines[i]
        if re.match(r"^## ", l):
            break
        if not l.strip() or re.match(r"^---\s*$", l):
            continue
        m = re.match(r"^([A-Z][^:]{0,39}):\s(.*)$", l)
        if m and len(m.group(1).split()) <= 4:
            speaker, text = m.group(1).strip(), m.group(2).strip()
        else:
            speaker, text = UNLABELLED, l.strip()
        turns.append({"line": i + 1, "speaker": speaker, "text": text, "words": len(text.split())})
    if not turns:
        raise ValueError(f"{path}: '## Transcript' has no 'Label: text' turns")
    return turns


def client_turns(turns, operator_labels=None):
    """Turns not spoken by the operator. When every turn carries one label, or
    only operator labels appear, no turn can be excluded: returns all turns
    and a note saying why."""
    ops = {s.lower() for s in (operator_labels or OPERATOR_LABELS)}
    labelled = {t["speaker"] for t in turns if t["speaker"] != UNLABELLED}
    non_op = [t for t in turns if t["speaker"].lower() not in ops]
    notes = []
    if any(t["speaker"] == UNLABELLED for t in turns):
        notes.append("some lines carry no speaker label and are treated as candidates")
    if len(labelled) <= 1 and not any(t["speaker"] == UNLABELLED for t in turns) or not non_op:
        return list(turns), ("single speaker label or only operator labels found "
                             f"({', '.join(sorted(labelled)) or 'none'}): every turn treated as a candidate")
    return non_op, "; ".join(notes)


STOPWORDS = set("""the and that this with you your for are was were have has had not but they them their
there then than what when where which who how all any can could would should will just like yeah
yes okay know think going want from our out about into its it's i'm we're you're that's don't
really kind sort thing things get got one two some more very also been being because well""".split())


CLIENT_NOISE = {"self", "internal", "solo", "memo", "also", "present",
                "speaking", "throughout", "with", "call", "meeting", "company", "builders", "cohort"}


def client_key(value):
    """Name tokens of a `client:` frontmatter value. Two dissects belong to one
    client when their keys intersect. Label variants in the live corpus
    ("Jane Rivera", "The Board / Jane Rivera") share tokens; different
    clients sharing one project folder (one folder for every prospect) do not."""
    toks = re.findall(r"[A-Za-z][A-Za-z'-]{3,}", value or "")
    return {t.lower() for t in toks if t[0].isupper() and t.lower() not in CLIENT_NOISE}


def quote_tokens(text):
    return {w for w in re.findall(r"[a-z0-9']+", (text or "").lower()) if len(w) >= 3 and w not in STOPWORDS}
