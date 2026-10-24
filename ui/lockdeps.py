"""Write ui/requirements.txt: the portal's runtime dependencies, each pinned with its wheels' sha256 hashes.

    venv-portal/bin/python -m ui.lockdeps        # after upgrading packages in venv-portal

Reads the versions installed in this interpreter (ROOTS and everything they require), asks PyPI for the
hashes of each version's wheels, and lists any advisory OSV knows for the locked set. Installing with
``pip install --require-hashes --only-binary=:all: -r ui/requirements.txt`` then refuses any file whose
hash differs, so a re-uploaded or tampered release can't slip in under a pinned version.

Only the wheels uploaded with the one installed here are locked: its own file (found by the installed dist-info's
WHEEL tags) and the release's other wheels uploaded within UPLOAD_WINDOW of it (the other platforms' builds). A wheel
added to an old release later — what a stolen maintainer token would add, after the install's 14-day cooldown kept
it out — is never locked, so a rebuild without the cooldown can't pick it.
"""
import importlib.metadata as md
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name, parse_wheel_filename

# What ui/ and the engine import (pytest is a dev tool, not locked)
ROOTS = ("numpy", "pandas", "scipy", "matplotlib", "pillow", "yfinance", "curl_cffi", "requests", "google-genai",
         "tweepy", "psutil", "certifi")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requirements.txt")
UPLOAD_WINDOW = timedelta(days=1)  # a release's wheels are uploaded together (CI); later additions aren't locked


def closure(roots):
    """{name: version} of the roots and their runtime requirements, as installed."""
    seen, stack = {}, list(roots)
    while stack:
        name = canonicalize_name(stack.pop())
        if name in seen:
            continue
        dist = md.distribution(name)
        seen[name] = dist.version
        for raw in dist.requires or []:
            req = Requirement(raw)
            if req.marker is None or req.marker.evaluate({"extra": ""}):
                stack.append(req.name)
    return dict(sorted(seen.items()))


def _json(url, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data, {"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def installed_tags(name):
    """The installed wheel's tags, from its dist-info WHEEL file (``Tag: cp313-cp313-macosx_11_0_arm64``)."""
    text = md.distribution(name).read_text("WHEEL") or ""
    return {line.split(":", 1)[1].strip() for line in text.splitlines() if line.startswith("Tag:")}


def wheel_hashes(name, version, tags=None, files=None):
    """sha256 of the installed wheel (the PyPI file with ``tags``, default: the installed ones) and of the release's
    wheels uploaded within UPLOAD_WINDOW of it."""
    tags = installed_tags(name) if tags is None else set(tags)
    files = _json(f"https://pypi.org/pypi/{name}/{version}/json")["urls"] if files is None else files
    wheels = [f for f in files if f["packagetype"] == "bdist_wheel"]
    if not wheels:
        raise SystemExit(f"{name}=={version}: no wheels on PyPI")
    mine = [f for f in wheels if {str(t) for t in parse_wheel_filename(f["filename"])[3]} == tags]
    if not mine:
        raise SystemExit(f"{name}=={version}: the installed wheel ({', '.join(sorted(tags)) or 'no tags'}) is not on PyPI")
    at = lambda f: datetime.fromisoformat(f["upload_time_iso_8601"].replace("Z", "+00:00"))
    first = min(at(f) for f in mine)
    keep = [f for f in wheels if f in mine or abs(at(f) - first) <= UPLOAD_WINDOW]
    late = [f["filename"] for f in wheels if f not in keep]
    if late:
        print(f"ℹ️  {name}=={version}: not locked (uploaded apart from the installed wheel): {', '.join(late)}")
    return sorted(f["digests"]["sha256"] for f in keep)


def advisories(pins):
    queries = [{"package": {"name": n, "ecosystem": "PyPI"}, "version": v} for n, v in pins.items()]
    results = _json("https://api.osv.dev/v1/querybatch", {"queries": queries})["results"]
    return {n: [v["id"] for v in r["vulns"]] for n, r in zip(pins, results) if r.get("vulns")}


def main():
    pins = closure(ROOTS)
    lines = [f"# The portal's runtime dependencies for Python {sys.version_info.major}.{sys.version_info.minor}, "
             "written by `python -m ui.lockdeps` (do not edit by hand).",
             "# Install: pip install --require-hashes --only-binary=:all: -r ui/requirements.txt", ""]
    for name, version in pins.items():
        hashes = wheel_hashes(name, version)
        lines.append(f"{name}=={version} \\")
        lines += [f"    --hash=sha256:{h}" + (" \\" if i < len(hashes) - 1 else "") for i, h in enumerate(hashes)]
    with open(OUT, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"{len(pins)} packages → {os.path.relpath(OUT)}")
    for name, ids in advisories(pins).items():
        print(f"⚠️  {name}=={pins[name]}: {', '.join(ids)}")


if __name__ == "__main__":
    main()
