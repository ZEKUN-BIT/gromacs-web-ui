"""Regenerate Windows branding with Pillow; no graphics dependency is needed to build."""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

ASSETS = Path(__file__).resolve().parent
INK = (24, 33, 30)
ACID = (183, 233, 87)
PAPER = (255, 254, 248)
ORBIT = (225, 240, 228)


def orbit_points(cx: float, cy: float, rx: float, ry: float, angle: float) -> list[tuple[float, float]]:
    rotation = math.radians(angle)
    points = []
    for step in range(193):
        theta = step * math.tau / 192
        x, y = rx * math.cos(theta), ry * math.sin(theta)
        points.append((cx + x * math.cos(rotation) - y * math.sin(rotation), cy + x * math.sin(rotation) + y * math.cos(rotation)))
    return points


def mark(size: int) -> Image.Image:
    # Draw at four times the requested size to keep small icon edges clean.
    scale = size * 4 / 256
    image = Image.new("RGBA", (size * 4, size * 4))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        tuple(value * scale for value in (8, 8, 248, 248)), radius=52 * scale, fill=INK, outline=(57, 76, 65), width=max(1, round(scale))
    )
    for angle in (-45, 45):
        points = [(x * scale, y * scale) for x, y in orbit_points(128, 128, 86, 32, angle)]
        draw.line(points, fill=ORBIT, width=round(6 * scale), joint="curve")
    for x, y, radius in ((128, 128, 11), (128 + 86 / math.sqrt(2), 128 + 86 / math.sqrt(2), 8)):
        draw.ellipse(tuple(value * scale for value in (x - radius - 4, y - radius - 4, x + radius + 4, y + radius + 4)), fill=INK)
        draw.ellipse(tuple(value * scale for value in (x - radius, y - radius, x + radius, y + radius)), fill=ACID)
    return image.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    icon = mark(256)
    icon.save(ASSETS / "GromacsConsole.ico", sizes=[(size, size) for size in (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)])
    # The wizard artwork has no baked-in text; every label remains a native control.
    sidebar = Image.new("RGB", (656, 1256))
    draw = ImageDraw.Draw(sidebar)
    for y in range(sidebar.height):
        fraction = y / (sidebar.height - 1)
        color = tuple(round(start + (end - start) * fraction) for start, end in zip(INK, (15, 43, 30), strict=True))
        draw.line((0, y, sidebar.width, y), fill=color)
    for angle, color in ((-40, (42, 69, 48)), (40, (39, 62, 46))):
        draw.line(orbit_points(328, 920, 390, 130, angle), fill=color, width=3, joint="curve")
    draw.line((80, 660, 576, 660), fill=(62, 84, 62), width=2)
    draw.line((80, 660, 176, 660), fill=ACID, width=4)
    sidebar.paste(mark(352), (152, 208), mark(352))
    sidebar.save(ASSETS / "wizard-sidebar.bmp")
    header = Image.new("RGB", (600, 228), PAPER)
    header.paste(mark(160), (404, 34), mark(160))
    header.save(ASSETS / "wizard-header.bmp")
    # The browser favicon uses the same geometry and colors as the Windows icon.
    electron = 128 + 86 / math.sqrt(2)
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256">\n'
        '  <rect x="8" y="8" width="240" height="240" rx="52" fill="#18211e" stroke="#394c41"/>\n'
        '  <g fill="none" stroke="#e1f0e4" stroke-width="6">\n'
        '    <ellipse cx="128" cy="128" rx="86" ry="32" transform="rotate(-45 128 128)"/>\n'
        '    <ellipse cx="128" cy="128" rx="86" ry="32" transform="rotate(45 128 128)"/>\n'
        "  </g>\n"
        '  <circle cx="128" cy="128" r="15" fill="#18211e"/>\n'
        '  <circle cx="128" cy="128" r="11" fill="#b7e957"/>\n'
        f'  <circle cx="{electron:.4f}" cy="{electron:.4f}" r="12" fill="#18211e"/>\n'
        f'  <circle cx="{electron:.4f}" cy="{electron:.4f}" r="8" fill="#b7e957"/>\n'
        "</svg>\n"
    )
    (ASSETS.parents[1] / "app/static/gromacs-console.svg").write_text(svg, encoding="utf-8")


if __name__ == "__main__":
    main()
