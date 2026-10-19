"""Generate a deterministic cache version for the complete frontend module graph."""

from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = re.compile(r"(\.(?:js|css)\?v=)[A-Za-z0-9_-]+")


def synchronize(root: Path = ROOT, *, check: bool = False) -> bool:
    static = root / "app/static"
    files = sorted([*static.rglob("*.js"), *static.rglob("*.css"), root / "app/templates/index.html"])
    digest = hashlib.sha256()
    sources = {}
    for path in files:
        source = path.read_text(encoding="utf-8")
        sources[path] = source
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(VERSION.sub(r"\g<1>VERSION", source).encode() + b"\0")
    version = digest.hexdigest()[:16]
    changed = False
    for path, source in sources.items():
        updated = VERSION.sub(lambda match: match[1] + version, source)
        if source != updated:
            changed = True
            if not check:
                path.write_text(updated, encoding="utf-8")
    print(f"Frontend asset version {version}: {'needs regeneration' if changed and check else 'ready'}")
    return not (check and changed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    raise SystemExit(0 if synchronize(check=parser.parse_args().check) else 1)
