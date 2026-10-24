#!/usr/bin/env python3
"""
Diff frontend/src/api/types.ts against the backend's real payloads.

The interfaces in types.ts are hand-written, because partE.py annotates its
endpoints `-> Dict[str, Any]` and the published OpenAPI schema therefore says
nothing useful. Hand-written types drift. This catches the drift.

It compares field NAMES only -- a type checker would need a real TS parser,
and names are where the damage is: the scaffold shipped `classification`,
`eval_cp` and `win_pct` for fields the backend calls `label`, `cp_after` and
`win_after`, and every one of those would have read as `undefined` at
runtime with no error anywhere.

    .venv/bin/python tools/check_frontend_types.py

Payloads come from docs/examples/, captured from real runs by
tools/dump_api_contract.py. Regenerate those first if the backend changed.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TYPES = os.path.join(ROOT, "frontend", "src", "api", "types.ts")
EXAMPLES = os.path.join(ROOT, "docs", "examples")

# interface name -> (example file, path into it)
# A path of () means the file's top level.
CHECKS: List[Tuple[str, str, Tuple[str, ...]]] = [
    ("AppConfig", "config.json", ()),
    ("JobCreated", "analyse-accepted.json", ()),
    ("JobSnapshot", "job-done-game.json", ()),
    ("GameReport", "job-done-game.json", ("result",)),
    ("MoveRecord", "job-done-game.json", ("result", "moves", "0")),
    ("PlayerReport", "job-done-game.json", ("result", "white")),
    ("RatingEstimate", "job-done-game.json", ("result", "white", "rating_estimate")),
    ("PhaseReport", "job-done-game.json", ("result", "white", "phases", "0")),
    ("TimeReport", "job-done-game.json", ("result", "white", "time")),
    ("ErrorProfile", "job-done-game.json", ("result", "white", "error_profile")),
    ("TimeOffender", "job-done-game.json", ("result", "white", "time", "top_wasted", "0")),
    ("Opening", "job-done-game.json", ("result", "opening")),
    ("TimeControl", "job-done-game.json", ("result", "time_control")),
    ("Commentary", "job-done-game.json", ("result", "commentary")),
    # Captured separately: the contract run's game draws no comments, so
    # job-done-game.json has `comments: []` and this interface was never
    # checked against anything. It was wrong — `text`, `san`, `color` and
    # `tier` were all invented — and the UI rendered an empty paragraph.
    ("Comment", "comment.json", ()),
    ("PositionReport", "job-done-fen.json", ("result",)),
    ("Me", "me.json", ()),
    ("UserStats", "me.json", ("stats",)),
    ("NextPuzzle", "puzzle-next.json", ()),
    ("Puzzle", "puzzle-next.json", ("puzzle",)),
    ("StepResult", "puzzle-step.json", ()),
    ("AttemptResponse", "puzzle-attempt-wrong.json", ()),
    ("AttemptVerdict", "puzzle-attempt-wrong.json", ("result",)),
    ("MoveCheck", "puzzle-attempt-wrong.json", ("result", "checks", "0")),
    ("RatingChange", "puzzle-attempt-wrong.json", ("rating",)),
    ("BoardState", "board-moves.json", ()),
    ("LegalMove", "board-moves.json", ("legal", "0")),
    ("AnalysisSummary", "my-analyses.json", ("analyses", "0")),
]

# Fields the UI deliberately does not model. Each needs a reason.
IGNORED: Dict[str, Dict[str, str]] = {
    "AnalysisSummary": {
        f: "denormalised columns the LOCAL account store returns; the "
           "Supabase path (which this example is captured from) sends the "
           "thinner row, so these are optional in types.ts"
        for f in ("white", "black", "result", "played_at", "accuracy_white",
                  "accuracy_black", "plies", "depth", "prompt_version")
    },
    "AttemptResponse": {
        "dev_mode": "only present on the local-sample path, absent with a "
                    "database -- optional in types.ts for that reason",
    },
}

_results: List[Tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, ok, detail))


def ts_interfaces(src: str) -> Dict[str, Set[str]]:
    """
    Field names per `export interface`.

    Brace-counts to find each body, then takes identifiers at depth 1 that
    are followed by `?:` or `:`. Nested object literals and comments are
    skipped, which is why this does not just regex the whole file.
    """
    out: Dict[str, Set[str]] = {}
    for m in re.finditer(r"export interface (\w+)[^{]*\{", src):
        name = m.group(1)
        i = m.end()
        depth = 1
        body_start = i
        while i < len(src) and depth:
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
            i += 1
        body = src[body_start : i - 1]
        body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
        body = re.sub(r"//[^\n]*", "", body)

        fields: Set[str] = set()
        depth = 0
        for line in body.split("\n"):
            stripped = line.strip()
            if depth == 0:
                fm = re.match(r"([A-Za-z_]\w*)\??\s*:", stripped)
                if fm:
                    fields.add(fm.group(1))
            depth += line.count("{") + line.count("[") - line.count("}") - line.count("]")
            depth = max(0, depth)
        out[name] = fields
    return out


def dig(data: Any, path: Tuple[str, ...], where: str) -> Optional[Dict[str, Any]]:
    node = data
    for part in path:
        if isinstance(node, list):
            idx = int(part)
            if idx >= len(node):
                # Deliberately not a skip. An empty array means this
                # interface is unchecked, which is indistinguishable from
                # correct until it reaches a browser.
                print(f"  ! {where}: {'.'.join(path)} -- list is EMPTY, so "
                      f"nothing was verified; capture an example that has one")
                return None
            node = node[idx]
        elif isinstance(node, dict):
            if part not in node:
                print(f"  ! {where}: {'.'.join(path)} -- no key {part!r}")
                return None
            node = node[part]
        else:
            return None
    return node if isinstance(node, dict) else None


def main() -> int:
    if not os.path.isdir(EXAMPLES):
        print("docs/examples/ is missing. Run tools/dump_api_contract.py first.")
        return 1

    src = open(TYPES).read()
    interfaces = ts_interfaces(src)
    print(f"{len(interfaces)} interfaces in types.ts, {len(CHECKS)} checked\n")

    for iface, filename, path in CHECKS:
        label = f"{iface} <- {filename}" + (f":{'.'.join(path)}" if path else "")
        if iface not in interfaces:
            check(label, False, f"no `export interface {iface}` in types.ts")
            continue
        full = os.path.join(EXAMPLES, filename)
        if not os.path.isfile(full):
            check(label, False, f"missing example {filename}")
            continue
        payload = dig(json.load(open(full)), path, iface)
        if payload is None:
            check(label, False, "could not reach that path in the payload")
            continue

        declared = interfaces[iface]
        actual = set(payload)
        ignored = set(IGNORED.get(iface, {}))

        missing = actual - declared - ignored      # backend sends, UI ignores
        extra = declared - actual - ignored        # UI expects, backend omits

        detail = []
        if missing:
            detail.append("not in types.ts: " + ", ".join(sorted(missing)))
        if extra:
            detail.append("not in the payload: " + ", ".join(sorted(extra)))
        check(label, not detail, " | ".join(detail))

    width = max(len(n) for n, _, _ in _results)
    for name, ok, detail in _results:
        line = f"  [{'PASS' if ok else 'FAIL'}] {name:<{width}}"
        if detail and not ok:
            line += f"\n         -> {detail}"
        print(line)

    passed = sum(1 for _, ok, _ in _results if ok)
    print(f"\n{passed}/{len(_results)} interfaces match the captured payloads")
    if passed != len(_results):
        print(
            "\nA mismatch is real: a field the UI names but the backend does not\n"
            "send reads as `undefined` with no error. Fix types.ts, or add the\n"
            "field to IGNORED with a reason."
        )
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    sys.exit(main())
