#!/usr/bin/env python3
"""Carry-forward invariant check for a meeting-dissect file (Step 6).

`carry_forward: COMPLETE` is writable only when this exits 0.

Checks, against the prior dissect named in `prior_dissect:` (or found by
date in the same folder):
  1. every prior item ID appears exactly once across Still open, Standing
     context and Closed;
  2. no ID in those three sections is foreign to the prior file;
  3. every Closed item has a closed Status (fulfilled/withdrawn/superseded)
     and a non-empty Pointer;
  4. frontmatter prior_items = N, carried = C, closed = K, N = C + K, and
     unfulfilled_carried = C.

A file without the new sections is legacy: exit 2, "legacy, not validated",
with the arithmetic printed for information. A DEFERRED stamp exits 0 with a
note, because nothing was computed to check.

Usage: validate.py <dissect.md> [--prior <prior dissect.md>]
Exit: 0 pass, 1 fail, 2 legacy or usage.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dissect_lib as dl  # noqa: E402
import carry_forward as cf  # noqa: E402


def resolve_prior(path, parsed, explicit):
    if explicit:
        return explicit
    named = parsed["frontmatter"].get("prior_dissect", "")
    if named and named != "none":
        for cand in (named, os.path.join(os.path.dirname(path), os.path.basename(named))):
            if os.path.isfile(cand):
                return cand
    date = parsed["frontmatter"].get("meeting_date")
    if not date:
        return None
    found, _ = cf.find_prior(os.path.dirname(os.path.abspath(path)), date, exclude=path,
                             client=parsed["frontmatter"].get("client") or None)
    return found[1] if found else None


def check(path, prior_path=None):
    """Returns (exit_code, messages)."""
    msgs = []
    p = dl.parse_dissect(path)
    fm = p["frontmatter"]
    stamp = (fm.get("carry_forward", "").split() or [""])[0].upper()
    if stamp == "DEFERRED":
        return 0, ["carry_forward: DEFERRED. Nothing computed, nothing to validate. Run --reconcile."]
    try:
        prior_path = resolve_prior(path, p, prior_path)
    except cf.MultiClientFolder as e:
        return 1, [f"FAIL: {e}; add prior_dissect: or client: to the frontmatter"]
    prior_ids = []
    if prior_path:
        prior = dl.parse_dissect(prior_path)
        prior_ids = [it["id"] for it in dl.prior_live_items(prior)]
    n = len(prior_ids)

    if not dl.is_new_format(p):
        carried = [it for it in p["items"] if it["section"] == dl.SEC_LEGACY_CARRIED]
        msgs.append(f"legacy, not validated: no Still open / Standing context / Closed sections in {os.path.basename(path)}.")
        msgs.append(f"info: prior file {os.path.basename(prior_path) if prior_path else 'none'} holds {n} items; "
                    f"this file itemizes {len(carried)} carried and 0 closed, so {len(carried)} of {n} are accounted for.")
        return 2, msgs

    fails = []
    carried = [it for it in p["items"] if it["section"] in (dl.SEC_STILL_OPEN, dl.SEC_STANDING)]
    closed = [it for it in p["items"] if it["section"] == dl.SEC_CLOSED]
    c, k = len(carried), len(closed)
    seen = {}
    for it in carried + closed:
        seen.setdefault(it["id"], []).append(it)
    for pid in prior_ids:
        if pid not in seen:
            fails.append(f"prior item {pid} is not accounted for (neither carried nor closed)")
        elif len(seen[pid]) > 1:
            fails.append(f"prior item {pid} appears {len(seen[pid])} times")
    foreign = sorted(set(seen) - set(prior_ids))
    for fid in foreign:
        fails.append(f"ID {fid} in a carry-forward section is not an item of the prior file")
    for it in carried + closed:
        if not it["id_explicit"]:
            fails.append(f"carried/closed item '{it['statement'][:60]}' has no ID: field")
    for it in closed:
        if not dl.is_closed(it):
            fails.append(f"closed item {it['id']} has Status '{it['status']}', not fulfilled/withdrawn/superseded")
        if not it["pointer"].strip():
            fails.append(f"closed item {it['id']} has no Pointer")
    for it in carried:
        if dl.is_closed(it):
            fails.append(f"item {it['id']} is Status {dl.status_word(it['status'])} but sits in a carried section")

    def fm_int(key):
        try:
            return int(fm.get(key, ""))
        except ValueError:
            return None

    for key, want in (("prior_items", n), ("carried", c), ("closed", k), ("unfulfilled_carried", c)):
        got = fm_int(key)
        if got != want:
            fails.append(f"frontmatter {key}: {fm.get(key, 'missing')} but the file has {want}")
    if n != c + k:
        fails.append(f"invariant broken: prior_items {n} != carried {c} + closed {k}")

    msgs.append(f"prior: {os.path.basename(prior_path) if prior_path else 'none'} · prior_items {n} · carried {c} · closed {k}")
    if fails:
        msgs.extend("FAIL: " + f for f in fails)
        if stamp == "COMPLETE":
            msgs.append("carry_forward: COMPLETE is not allowed on this file.")
        return 1, msgs
    msgs.append(f"PASS: {n} = {c} + {k}")
    return 0, msgs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dissect")
    ap.add_argument("--prior")
    a = ap.parse_args()
    if not os.path.isfile(a.dissect):
        print(f"no such file: {a.dissect}", file=sys.stderr)
        return 2
    code, msgs = check(a.dissect, a.prior)
    print("\n".join(msgs))
    return code


if __name__ == "__main__":
    sys.exit(main())
