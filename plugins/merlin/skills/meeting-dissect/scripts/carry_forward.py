#!/usr/bin/env python3
"""Mechanical carry-forward for meeting-dissect (Step 5A).

Finds the latest prior dissect for a client corpus and emits every item it
holds, whatever its tag, sorted into the three sections a new dissect writes:

  ## Still open from prior meetings   owed work (ASK, OPEN, RISK, COMMITMENT, open OFFER)
  ## Standing context                 facts (OBJECTION, CONSTRAINT, SPEC, PRIORITY, DECISION, declined OFFER)
  ## Closed since last meeting        items already closed in the prior file, with a pointer

Nothing is filtered by meaning. The session then moves items into Closed (with
a pointer) as this meeting closes them, and validate.py checks the arithmetic.

Usage:
  carry_forward.py --dir <project>/knowledge/meetings --before YYYY-MM-DD --client "<client: value>"
                   [--exclude <this dissect>] [--json] [--accept-partial]

Exit codes: 0 ok (stamp COMPLETE possible), 4 ok but stamp must be PARTIAL,
3 refused (latest prior is not COMPLETE, or it parses to fewer items than its
frontmatter declares), 2 usage error.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dissect_lib as dl  # noqa: E402

HEADINGS = {
    dl.SEC_STILL_OPEN: "## Still open from prior meetings",
    dl.SEC_STANDING: "## Standing context",
    dl.SEC_CLOSED: "## Closed since last meeting",
}


class MultiClientFolder(Exception):
    pass


def find_prior(directory, before, exclude=None, client=None):
    """Latest dissect for this client with meeting_date < before.
    TRANSCRIPT-ABSENT files hold no receipted items, so they are skipped and
    reported, as are files with no client: label when a client is given. A folder holding more than one client (one project folder for
    every prospect, say) requires `client`; without it this raises MultiClientFolder
    rather than carry one client's items into another's file."""
    cands, skipped, unlabelled = [], [], []
    want = dl.client_key(client) if client else None
    keys = []
    for path in dl.list_dissects(directory):
        if exclude and os.path.abspath(path) == os.path.abspath(exclude):
            continue
        p = dl.parse_dissect(path)
        key = dl.client_key(p["frontmatter"].get("client", ""))
        d = p["frontmatter"].get("meeting_date", "")
        if key:
            keys.append(key)
        if want is not None and not (key & want):
            if not key and d and d < before:
                unlabelled.append(path)
            continue
        if not d or d >= before:
            continue
        if p["frontmatter"].get("provenance", "").upper().startswith("TRANSCRIPT-ABSENT"):
            skipped.append(path)
            continue
        cands.append((d, path, p))
    if want is None:
        groups = []
        for k in keys:
            for g in groups:
                if g & k:
                    g |= k
                    break
            else:
                groups.append(set(k))
        if len(groups) > 1:
            raise MultiClientFolder(f"{directory} holds dissects for {len(groups)} different clients; pass --client")
    # A dated file with no client: label cannot be ruled in or out; report it
    # with the skipped files so the stamp drops to PARTIAL.
    skipped = skipped + unlabelled
    if not cands:
        return None, skipped
    cands.sort(key=lambda c: (c[0], c[1]))
    return cands[-1], skipped


def _int(v):
    try:
        return int(str(v).split()[0])
    except (ValueError, IndexError):
        return None


def parse_shortfall(parsed):
    """Fail closed when the parser read fewer items than the file declares.
    Measured 2026-09-16: 9 of 41 live dissects use item shapes this parser
    cannot fully read, one of them parsing to 0 of 34. Reading more than
    declared is fine (hand counts drift low, as the 2026-09-16 file did)."""
    fm = parsed["frontmatter"]
    problems = []
    this = [i for i in parsed["items"] if i["section"] in (dl.SEC_THIS_MEETING, dl.SEC_OTHER)]
    declared = _int(fm.get("item_count"))
    if declared is None:
        problems.append("no numeric item_count to check the parse against")
    elif len(this) < declared:
        problems.append(f"parsed {len(this)} of {declared} declared this-meeting items")
    if dl.is_new_format(parsed):
        carried = [i for i in parsed["items"] if i["section"] in (dl.SEC_STILL_OPEN, dl.SEC_STANDING)]
        want = _int(fm.get("carried"))
    else:
        carried = [i for i in parsed["items"] if i["section"] == dl.SEC_LEGACY_CARRIED]
        want = _int(fm.get("unfulfilled_carried"))
    if want is not None and len(carried) < want:
        problems.append(f"parsed {len(carried)} of {want} declared carried items")
    return "; ".join(problems)


def with_id(item):
    """The item's verbatim block with ID and origin fields guaranteed."""
    lines = item["raw"].split("\n")
    head, rest = lines[0], lines[1:]
    extra = []
    if not item["id_explicit"]:
        extra.append(f"      ID: {item['id']}")
    return "\n".join([head] + extra + rest)


def build(directory, before, exclude=None, accept_partial=False, client=None):
    result = {"prior_dissect": None, "skipped_transcript_absent": [], "prior_items": 0,
              "items": [], "stamp": "COMPLETE", "refused": None, "shortfall": ""}
    try:
        found, skipped = find_prior(directory, before, exclude, client)
    except MultiClientFolder as e:
        result["refused"] = str(e)
        return result
    result["skipped_transcript_absent"] = skipped
    if skipped:
        result["stamp"] = "PARTIAL"
    if found is None:
        return result
    date, path, parsed = found
    result["prior_dissect"] = path
    stamp = parsed["frontmatter"].get("carry_forward", "").split()[0:1]
    stamp = stamp[0].upper() if stamp else ""
    if stamp != "COMPLETE":
        if not accept_partial:
            result["refused"] = (f"latest prior dissect {os.path.basename(path)} is stamped "
                                 f"carry_forward: {stamp or 'MISSING'}. Run meeting-dissect --reconcile "
                                 "first, or pass --accept-partial and stamp this run PARTIAL.")
            return result
        result["stamp"] = "PARTIAL"
    shortfall = parse_shortfall(parsed)
    if shortfall:
        result["shortfall"] = shortfall
        if not accept_partial:
            result["refused"] = (f"{os.path.basename(path)}: {shortfall}. Its item shape is not machine-readable, "
                                 "so carrying from it would silently drop items. Normalize it to the SKILL.md "
                                 "Step 3 shape (meeting-dissect --reconcile), or pass --accept-partial and "
                                 "name this shortfall in carry_forward_basis.")
            return result
        result["stamp"] = "PARTIAL"
    live = dl.prior_live_items(parsed)
    ids = [it["id"] for it in live]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        result["refused"] = f"prior dissect has duplicate item IDs: {', '.join(dupes)}"
        return result
    for it in live:
        result["items"].append({
            "id": it["id"], "tag": it["tag"], "statement": it["statement"],
            "status": it["status"], "target_section": dl.target_section(it),
            "origin_section": it["section"], "block": with_id(it),
            "prior_meeting_date": date,
        })
    result["prior_items"] = len(live)
    return result


def skeleton(result):
    out = []
    by = {k: [i for i in result["items"] if i["target_section"] == k] for k in HEADINGS}
    for sec in (dl.SEC_STILL_OPEN, dl.SEC_STANDING, dl.SEC_CLOSED):
        out.append(HEADINGS[sec])
        out.append("")
        if not by[sec]:
            out.append("None.")
            out.append("")
        for it in by[sec]:
            block = it["block"]
            if sec == dl.SEC_CLOSED and "Pointer:" not in block:
                block += (f"\n      Pointer: closed in {os.path.basename(result['prior_dissect'])} "
                          f"(Status: {it['status']})")
            out.append(block.rstrip())
            out.append("")
    n = result["prior_items"]
    c = len(by[dl.SEC_STILL_OPEN]) + len(by[dl.SEC_STANDING])
    k = len(by[dl.SEC_CLOSED])
    fm = [
        f"prior_dissect: {result['prior_dissect'] or 'none'}",
        f"prior_items: {n}",
        f"carried: {c}",
        f"closed: {k}",
        f"unfulfilled_carried: {c}",
    ]
    return "\n".join(fm), "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", required=True, help="<project>/knowledge/meetings")
    ap.add_argument("--before", required=True, help="this meeting's date, YYYY-MM-DD")
    ap.add_argument("--exclude", help="this meeting's own dissect path, if it already exists")
    ap.add_argument("--client", help="this dissect's client: value; required when the folder holds several clients")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--accept-partial", action="store_true")
    a = ap.parse_args()
    if not os.path.isdir(a.dir):
        print(f"not a directory: {a.dir}", file=sys.stderr)
        return 2
    r = build(a.dir, a.before, a.exclude, a.accept_partial, a.client)
    if r["refused"]:
        print("REFUSED: " + r["refused"], file=sys.stderr)
        return 3
    if a.json:
        print(json.dumps({k: v for k, v in r.items()}, indent=2))
    else:
        fm, body = skeleton(r)
        print("# frontmatter keys to write (validate.py re-derives prior_items from prior_dissect)")
        print(fm)
        print(f"# stamp this run at most: carry_forward: {r['stamp']}")
        if r["shortfall"]:
            print(f"# accepted parse shortfall, name it in carry_forward_basis: {r['shortfall']}")
        for s in r["skipped_transcript_absent"]:
            print(f"# skipped prior (TRANSCRIPT-ABSENT, or no client: label), name it in carry_forward_basis: {s}")
        print()
        print(body)
    return 4 if r["stamp"] == "PARTIAL" else 0


if __name__ == "__main__":
    sys.exit(main())
