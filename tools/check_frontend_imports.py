#!/usr/bin/env python3
"""
Resolve every local import in frontend/src against what the target exports.

There is no Node on this machine, so `tsc` cannot run. This catches the
failure mode that costs the most time without it: an import of a name that
no longer exists, or a path that does not resolve. It is not a type checker
and does not pretend to be -- run `npm run typecheck` once Node is installed.

    .venv/bin/python tools/check_frontend_imports.py
"""

from __future__ import annotations

import os
import re
import sys
from typing import Dict, List, Set, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "frontend", "src")

_results: List[Tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, ok, detail))


def strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"^\s*//[^\n]*$", "", text, flags=re.M)


def exports_of(path: str) -> Set[str]:
    """Named exports: interfaces, types, consts, functions, classes."""
    src = strip_comments(open(path).read())
    names: Set[str] = set()
    for m in re.finditer(
        r"export\s+(?:declare\s+)?"
        r"(?:interface|type|class|function|const|let|var|enum)\s+(\w+)",
        src,
    ):
        names.add(m.group(1))
    for m in re.finditer(r"export\s+(?:async\s+)?function\s+(\w+)", src):
        names.add(m.group(1))
    # export { a, b as c }
    for m in re.finditer(r"export\s*\{([^}]*)\}", src):
        for part in m.group(1).split(","):
            part = part.strip()
            if not part:
                continue
            names.add(part.split(" as ")[-1].strip())
    if re.search(r"export\s+default", src):
        names.add("default")
    return names


def imports_of(path: str) -> List[Tuple[str, List[str], bool]]:
    """(module, names, is_default_import) for local (./ or ../) imports."""
    src = strip_comments(open(path).read())
    out: List[Tuple[str, List[str], bool]] = []
    # The optional `type` keyword matters: an earlier version of this pattern
    # did not allow it, so every `import type { ... }` line was skipped and
    # the checker reported a clean run over files it had not read.
    pattern = re.compile(
        r"import\s+(?:type\s+)?"
        r"(?:(\w+)\s*,\s*)?"
        r"(?:\{([^}]*)\})?\s*(\w+)?\s*"
        r"from\s*['\"](\.[^'\"]*)['\"]",
        re.S,
    )
    for m in re.finditer(pattern, src):
        default_a, braced, default_b, module = m.groups()
        names: List[str] = []
        if braced:
            for part in braced.split(","):
                part = part.strip()
                if not part:
                    continue
                part = re.sub(r"^type\s+", "", part)
                names.append(part.split(" as ")[0].strip())
        has_default = bool(default_a or default_b)
        out.append((module, names, has_default))
    # Side-effect imports (CSS) are resolved too, but export nothing.
    for m in re.finditer(r"import\s+['\"](\.[^'\"]*)['\"]", src):
        out.append((m.group(1), [], False))
    return out


def resolve(from_file: str, module: str) -> str | None:
    base = os.path.normpath(os.path.join(os.path.dirname(from_file), module))
    if os.path.isfile(base):
        return base
    for ext in (".ts", ".tsx", ".d.ts"):
        if os.path.isfile(base + ext):
            return base + ext
    for ext in (".ts", ".tsx"):
        idx = os.path.join(base, "index" + ext)
        if os.path.isfile(idx):
            return idx
    return None


def main() -> int:
    files: List[str] = []
    for dirpath, _dirs, names in os.walk(SRC):
        for n in names:
            if n.endswith((".ts", ".tsx")):
                files.append(os.path.join(dirpath, n))
    files.sort()

    cache: Dict[str, Set[str]] = {}
    print(f"{len(files)} TypeScript files under frontend/src\n")

    # A file with local imports that produced no checks means the pattern
    # missed them -- the failure that let `import type` slip through.
    seen_imports: Dict[str, int] = {}

    for f in files:
        rel = os.path.relpath(f, ROOT)
        found = imports_of(f)
        seen_imports[rel] = len(found)
        for module, names, has_default in found:
            target = resolve(f, module)
            if target is None:
                check(f"{rel} -> {module}", False, "path does not resolve")
                continue
            if target.endswith(".css"):
                check(f"{rel} -> {module}", True)
                continue
            if target not in cache:
                cache[target] = exports_of(target)
            available = cache[target]
            missing = [n for n in names if n not in available]
            if has_default and "default" not in available:
                missing.append("default")
            check(
                f"{rel} -> {module}",
                not missing,
                ("not exported by " + os.path.relpath(target, ROOT) + ": "
                 + ", ".join(missing)) if missing else "",
            )

    # Anything referencing api.<name> must match client.ts's api object.
    client = os.path.join(SRC, "api", "client.ts")
    body = strip_comments(open(client).read())
    m = re.search(r"export const api = \{(.*?)\n\};", body, re.S)
    methods = set(re.findall(r"^\s{2}(\w+)\s*:", m.group(1), re.M)) if m else set()
    used: Set[str] = set()
    for f in files:
        if f == client:
            continue
        used |= set(re.findall(r"\bapi\.(\w+)\s*\(", strip_comments(open(f).read())))
    # Every file that mentions a relative import must have produced checks.
    silent = sorted(
        rel
        for rel, n in seen_imports.items()
        if n == 0
        and re.search(r"from\s*['\"]\.", strip_comments(open(os.path.join(ROOT, rel)).read()))
    )
    check(
        "no file's imports were skipped by the parser",
        not silent,
        "parsed no imports from: " + ", ".join(silent) if silent else "",
    )

    unknown = sorted(used - methods)
    check(
        "every api.<method> call exists in client.ts",
        not unknown,
        "missing from the api object: " + ", ".join(unknown) if unknown else "",
    )
    print(f"  api methods defined: {len(methods)}, used: {len(used)}\n")

    width = max(len(n) for n, _, _ in _results)
    for name, ok, detail in _results:
        line = f"  [{'PASS' if ok else 'FAIL'}] {name:<{width}}"
        if detail and not ok:
            line += f"\n         -> {detail}"
        print(line)

    passed = sum(1 for _, ok, _ in _results if ok)
    print(f"\n{passed}/{len(_results)} imports resolve")
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    sys.exit(main())
