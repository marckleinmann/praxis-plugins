#!/usr/bin/env python3
"""Diff two independent extraction passes of one transcript (Step 3B).

Pass A is the dissect draft; pass B is the fresh-context extraction written in
the same item shape. Carried and closed items are ignored: only items from
this meeting are compared.

Matching, per pair of items:
  - MATCH: they share a cited line and their quotes share at least half of the
    shorter quote's content words (3+ letters, stopwords removed; a quote under
    4 content words is scored by Jaccard instead, so one word cannot match).
  - POSSIBLE: they share a cited line and 20 to 50 percent of words, or share
    no line and 60+ percent of words. Listed for the reviewer, never merged.
An item with no match and no possible match is ONLY-IN-A or ONLY-IN-B. Those
are the items the review gate must show flagged.

With --transcript, every quoted segment of an ONLY-IN item is checked
verbatim (case and whitespace folded) against the transcript text. A segment
not found is marked, because a fresh reader can produce a plausible quote that
was never said.

Usage: diff_passes.py <passA.md> <passB.md> [--transcript <meeting.md>] [--json]
Exit: 0 report produced, 2 unreadable input.
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dissect_lib as dl  # noqa: E402

MATCH = 0.5
POSSIBLE_LOW = 0.2
POSSIBLE_NO_LINE = 0.6


def meeting_items(path):
    p = dl.parse_dissect(path, max_span=10)
    keep = (dl.SEC_THIS_MEETING, dl.SEC_OTHER)
    return [it for it in p["items"] if it["section"] in keep]


def overlap(a, b):
    ta, tb = dl.quote_tokens(a["quote"]), dl.quote_tokens(b["quote"])
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    # Containment over the shorter quote, but a quote of under 4 content words
    # is scored by Jaccard so one shared word cannot make a match.
    if min(len(ta), len(tb)) < 4:
        return inter / len(ta | tb)
    return inter / min(len(ta), len(tb))


def classify(a, b):
    shared = bool(a["lines"] & b["lines"])
    o = overlap(a, b)
    if shared and o >= MATCH:
        return "match", o
    if (shared and o >= POSSIBLE_LOW) or (not shared and o >= POSSIBLE_NO_LINE):
        return "possible", o
    return None, o


def norm(s):
    s = s.lower().replace("\u2019", "'").replace("\u2018", "'")
    s = re.sub(r"[^a-z0-9' ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def unverified_segments(item_raw, transcript_text):
    """Quoted segments of an item that do not occur in the transcript."""
    missing = []
    for line in item_raw.split("\n"):
        if not re.match(r"\s+(Quote|Restated)[^:]*:", line):
            continue
        for seg in re.findall(r'"([^"]{8,})"', line):
            parts = [p for p in re.split(r"\s*(?:\.\.\.|\[\.\.\.\])\s*", seg) if len(norm(p)) >= 6]
            if any(norm(p) not in transcript_text for p in parts):
                missing.append(seg[:120])
    return missing


def diff(path_a, path_b, transcript=None):
    A, B = meeting_items(path_a), meeting_items(path_b)
    a_state = {i: None for i in range(len(A))}
    b_state = {j: None for j in range(len(B))}
    matches, possibles = [], []
    for i, a in enumerate(A):
        for j, b in enumerate(B):
            kind, o = classify(a, b)
            if kind == "match":
                matches.append((i, j, o))
                a_state[i] = b_state[j] = "match"
            elif kind == "possible":
                possibles.append((i, j, o))
    for i, j, o in possibles:
        if a_state[i] is None:
            a_state[i] = "possible"
        if b_state[j] is None:
            b_state[j] = "possible"

    ttext = None
    if transcript:
        ttext = norm(" ".join(t["text"] for t in dl.read_transcript(transcript)))

    def brief(it):
        out = {"id": it["id"], "tag": it["tag"], "statement": it["statement"],
               "lines": sorted(it["lines"]), "quote": it["quote"][:300]}
        if ttext is not None:
            out["unverified_quotes"] = unverified_segments(it["raw"], ttext)
        return out

    return {
        "pass_a": path_a, "pass_b": path_b, "a_items": len(A), "b_items": len(B),
        "matched_a": sum(1 for v in a_state.values() if v == "match"),
        "matched_b": sum(1 for v in b_state.values() if v == "match"),
        "only_in_a": [brief(A[i]) for i, v in a_state.items() if v is None],
        "only_in_b": [brief(B[j]) for j, v in b_state.items() if v is None],
        "possible": [{"a": brief(A[i]), "b": brief(B[j]), "overlap": round(o, 2)}
                     for i, j, o in possibles if a_state[i] != "match" or b_state[j] != "match"],
    }


def render(r):
    out = [f"Pass diff · A {r['a_items']} items ({r['matched_a']} matched) · B {r['b_items']} items ({r['matched_b']} matched)", ""]
    for key, label in (("only_in_b", "ONLY IN PASS B"), ("only_in_a", "ONLY IN PASS A")):
        out.append(f"{label} ({len(r[key])}):")
        for it in r[key]:
            ls = ", ".join(f"L{n}" for n in it["lines"]) or "no line"
            out.append(f"  `{it['tag']}` {it['statement']} · {ls}")
            if it["quote"]:
                out.append(f"      \"{it['quote']}\"")
            for seg in it.get("unverified_quotes", []):
                out.append(f"      NOT FOUND VERBATIM in transcript: \"{seg}\"")
        out.append("")
    out.append(f"POSSIBLE MATCHES, check ({len(r['possible'])}):")
    for pm in r["possible"]:
        out.append(f"  A `{pm['a']['tag']}` {pm['a']['statement']}  <>  B `{pm['b']['tag']}` {pm['b']['statement']} · overlap {pm['overlap']}")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pass_a")
    ap.add_argument("pass_b")
    ap.add_argument("--transcript")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    for p in [a.pass_a, a.pass_b] + ([a.transcript] if a.transcript else []):
        if not os.path.isfile(p):
            print(f"no such file: {p}", file=sys.stderr)
            return 2
    r = diff(a.pass_a, a.pass_b, a.transcript)
    print(json.dumps(r, indent=2) if a.json else render(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
