#!/usr/bin/env python3
"""Coverage report for the meeting-dissect review gate (Step 7).

Lists the client turns where an item may be missing, with the turn text, so
the reviewer checks lines instead of remembering absences:

  1. UNCITED: a client turn of --min-words or more that no item cites.
  2. UNDER-EXTRACTED: a cited client turn whose words divided by citing items
     is at or above --words-per-item (default 60).
  3. COMPARISON: the rule as first approved (80+ words, exactly one item),
     printed so the two can be compared on real runs.

Why a density rule: on a real test transcript, 5 of the 11 missed items sat
in turns already cited 2 or 3 times, which a one-item rule never sees.
Measured: 60 words per item flagged 29 of 43 substantive turns and caught 10
of the 11.

A cited range of up to 3 lines counts every line; a wider range counts its
endpoints only, and the report says how many items that affected.

Usage: coverage.py <transcript.md> <dissect.md> [--min-words 30]
       [--words-per-item 60] [--operator-label Me ...] [--json] [--max-chars 400]
Exit: 0 report produced (findings are not a failure), 2 unreadable input.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dissect_lib as dl  # noqa: E402


def report(transcript, dissect, min_words=30, words_per_item=60, operator_labels=None, long_words=80):
    turns = dl.read_transcript(transcript)
    cands, note = dl.client_turns(turns, operator_labels)
    parsed = dl.parse_dissect(dissect)
    counts = {}
    wide = 0
    for it in parsed["items"]:
        if it["section"] == dl.SEC_CLOSED:
            continue
        if it["wide_range"]:
            wide += 1
        for ln in it["lines"]:
            counts[ln] = counts.get(ln, 0) + 1
    substantive = [t for t in cands if t["words"] >= min_words]
    uncited, under, comparison = [], [], []
    for t in substantive:
        n = counts.get(t["line"], 0)
        row = dict(t, items=n)
        if n == 0:
            uncited.append(row)
        elif t["words"] / n >= words_per_item:
            under.append(dict(row, words_per_item=round(t["words"] / n, 1)))
        if t["words"] >= long_words and n == 1:
            comparison.append(row)
    return {
        "transcript": transcript, "dissect": dissect, "note": note,
        "min_words": min_words, "words_per_item": words_per_item,
        "client_turns": len(cands), "substantive_turns": len(substantive),
        "wide_range_items": wide,
        "uncited": uncited, "under_extracted": under, "comparison_80_one_item": comparison,
    }


def render(r, max_chars):
    def clip(s):
        return s if len(s) <= max_chars else s[:max_chars].rstrip() + " [...]"
    out = [f"Coverage report · {os.path.basename(r['dissect'])}",
           f"{r['substantive_turns']} client turns of {r['min_words']}+ words (of {r['client_turns']} client turns)."]
    if r["note"]:
        out.append(f"Note: {r['note']}.")
    if r["wide_range_items"]:
        out.append(f"Note: {r['wide_range_items']} items cite a range wider than 3 lines; only its endpoints count.")
    out.append("")
    out.append(f"UNCITED ({len(r['uncited'])}): no item cites these turns.")
    for t in r["uncited"]:
        out.append(f"  L{t['line']} · {t['speaker']} · {t['words']} words · \"{clip(t['text'])}\"")
    out.append("")
    out.append(f"UNDER-EXTRACTED ({len(r['under_extracted'])}): {r['words_per_item']}+ words per citing item.")
    for t in r["under_extracted"]:
        out.append(f"  L{t['line']} · {t['speaker']} · {t['words']} words · {t['items']} items · \"{clip(t['text'])}\"")
    out.append("")
    cmp_lines = ", ".join(f"L{t['line']}" for t in r["comparison_80_one_item"]) or "none"
    out.append(f"COMPARISON, first-approved rule (80+ words, one item): {cmp_lines}")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("transcript")
    ap.add_argument("dissect")
    ap.add_argument("--min-words", type=int, default=30)
    ap.add_argument("--words-per-item", type=float, default=60)
    ap.add_argument("--operator-label", action="append")
    ap.add_argument("--max-chars", type=int, default=400)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    try:
        r = report(a.transcript, a.dissect, a.min_words, a.words_per_item, a.operator_label)
    except (OSError, ValueError) as e:
        print(f"coverage.py: {e}", file=sys.stderr)
        return 2
    print(json.dumps(r, indent=2) if a.json else render(r, a.max_chars))
    return 0


if __name__ == "__main__":
    sys.exit(main())
