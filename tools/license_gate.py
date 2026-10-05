"""Fails when a runtime dependency of fixwire (with its extras) has a
license we can't ship with: anything source-available (FSL, BUSL, SSPL,
Elastic), copyleft (GPL, AGPL, LGPL) or unknown.

    python tools/license_gate.py
"""

from __future__ import annotations

import re
import sys
from importlib import metadata

ALLOWED = (
    "MIT",
    "Apache-2.0",
    "Apache Software License",
    "BSD",
    "ISC",
    "PSF",
    "Python Software Foundation",
    "0BSD",
    "MPL-2.0",
    "Mozilla Public License 2.0",
)
DENIED = re.compile(r"\b(FSL|BUSL|Business Source|SSPL|Elastic|Commons Clause|A?GPL|LGPL|General Public)\b", re.I)


def license_of(dist: metadata.Distribution) -> str:
    m = dist.metadata
    expr = m.get("License-Expression") or ""
    classifiers = " ".join(
        c.split("::")[-1].strip() for c in m.get_all("Classifier") or () if c.startswith("License ::")
    )
    text = (m.get("License") or "").splitlines()[0] if m.get("License") else ""
    return " | ".join(x for x in (expr, classifiers, text) if x) or "unknown"


def closure(name: str, extras: tuple = ()) -> dict:
    seen: dict = {}
    todo = [(name, extras)]
    while todo:
        n, ex = todo.pop()
        key = re.sub(r"[-_.]+", "-", n).lower()
        if key in seen:
            continue
        try:
            dist = metadata.distribution(n)
        except metadata.PackageNotFoundError:
            continue
        seen[key] = dist
        for req in dist.requires or ():
            spec, _, marker = req.partition(";")
            extra = re.search(r'extra\s*==\s*["\']([^"\']+)', marker)
            if extra and extra.group(1) not in ex:
                continue
            dep = re.match(r"[A-Za-z0-9_.\-]+", spec.strip()).group(0)
            todo.append((dep, ()))
    return seen


def main() -> int:
    bad = 0
    for key, dist in sorted(closure("fixwire", ("async",)).items()):
        if key == "fixwire":
            continue
        lic = license_of(dist)
        ok = not DENIED.search(lic) and any(a.lower() in lic.lower() for a in ALLOWED)
        print("%-4s %-16s %s" % ("ok" if ok else "FAIL", key, lic))
        bad += not ok
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
