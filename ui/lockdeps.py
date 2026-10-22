"""Write ui/requirements.txt: the portal's runtime dependencies, each pinned with its wheels' sha256 hashes.

    venv-portal/bin/python -m ui.lockdeps        # after upgrading packages in venv-portal

Reads the versions installed in this interpreter (ROOTS and everything they require), asks PyPI for the
hashes of each version's wheels, and lists any advisory OSV knows for the locked set. Installing with
``pip install --require-hashes --only-binary=:all: -r ui/requirements.txt`` then refuses any file whose
hash differs, so a re-uploaded or tampered release can't slip in under a pinned version.
"""
import importlib.metadata as md
import json
import os
import sys
import urllib.request

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

# What ui/ and the engine import (pytest is a dev tool, not locked)
ROOTS = ("numpy", "pandas", "scipy", "matplotlib", "pillow", "yfinance", "curl_cffi", "requests", "google-genai",
         "tweepy", "psutil", "certifi")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requirements.txt")


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


def wheel_hashes(name, version):
    files = _json(f"https://pypi.org/pypi/{name}/{version}/json")["urls"]
    hashes = sorted(f["digests"]["sha256"] for f in files if f["packagetype"] == "bdist_wheel")
    if not hashes:
        raise SystemExit(f"{name}=={version}: no wheels on PyPI")
    return hashes


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
