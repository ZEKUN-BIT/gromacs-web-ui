"""Rebuild the fixed, test-only CJK fonts used by visual regression screenshots.

Requires the upstream Noto CJK 2.004 TTC files and the locked fontTools package.
The application itself continues to use its native system Chinese font.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parents[1]


def build(source: Path) -> None:
    text = "".join(
        path.read_text()
        for folder in (ROOT / "app", ROOT / "scripts")
        for path in folder.rglob("*")
        if path.suffix in {".html", ".js", ".py"}
    )
    chars = {ord(char) for char in text if ord(char) > 127}
    chars.update(range(32, 127))
    for style, weight in (("Regular", 400), ("Medium", 500), ("Bold", 600)):
        font = TTFont(source / f"NotoSansCJK-{style}.ttc", fontNumber=2, recalcTimestamp=False)
        options = subset.Options()
        options.name_IDs = ["*"]
        options.name_legacy = True
        options.name_languages = ["*"]
        shrink = subset.Subsetter(options=options)
        shrink.populate(unicodes=chars)
        shrink.subset(font)
        family = "Gromacs Snapshot CJK"
        for record in font["name"].names:
            if record.nameID in {1, 4, 6, 16}:
                name = f"GromacsSnapshotCJK-{style}" if record.nameID == 6 else family if record.nameID in {1, 16} else f"{family} {style}"
                record.string = name.encode(record.getEncoding())
        font["OS/2"].usWeightClass = weight
        target = ROOT / f"tests/browser_assets/cjk-{weight}.otf"
        font.save(target)
        print(f"{target.relative_to(ROOT)}: {target.stat().st_size} bytes")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("/usr/share/fonts/opentype/noto"))
    build(parser.parse_args().source)
