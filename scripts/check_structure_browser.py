"""Verify structure PNG downloads with real WebGL rendering and isolated API fixtures."""

from __future__ import annotations

import argparse
import base64
import io
import json
import math
from pathlib import Path

from check_product_browser import check_viewport
from PIL import Image, ImageChops, ImageStat
from playwright.sync_api import expect, sync_playwright


def structures() -> tuple[bytes, bytes]:
    atoms = []
    for residue in range(1, 13):
        angle = residue * math.radians(100)
        x, y, z = 2.3 * math.cos(angle), 2.3 * math.sin(angle), residue * 1.5
        for atom, dx, dy, dz, element in (
            ("N", -0.6, -0.4, -0.5, "N"),
            ("CA", 0, 0, 0, "C"),
            ("C", 0.6, 0.4, 0.5, "C"),
            ("O", 1.3, 0.7, 0.5, "O"),
        ):
            atoms.append((residue, atom, x + dx, y + dy, z + dz, element))
    pdb = (
        "\n".join(
            f"ATOM  {index:5d} {atom:^4s} ALA A{residue:4d}    {x:8.3f}{y:8.3f}{z:8.3f}  1.00  0.00          {element:>2s}"
            for index, (residue, atom, x, y, z, element) in enumerate(atoms, 1)
        )
        + "\nEND\n"
    )
    gro = (
        "Synthetic peptide\n"
        + str(len(atoms))
        + "\n"
        + "\n".join(
            f"{residue:5d}{'ALA':<5s}{atom:>5s}{index:5d}{x / 10:8.3f}{y / 10:8.3f}{z / 10:8.3f}"
            for index, (residue, atom, x, y, z, _) in enumerate(atoms, 1)
        )
        + "\n   3.0   3.0   3.0\n"
    )
    return pdb.encode(), gro.encode()


def snapshot(panel) -> dict:
    return panel.locator("canvas").evaluate("""canvas => {
        const viewer = canvas._3dmol_viewer;
        viewer.render();
        return {view: viewer.getView(), width: viewer.getWidth(), height: viewer.getHeight(),
            canvasWidth: canvas.width, canvasHeight: canvas.height, png: viewer.pngURI()};
    }""")


def check_download(page, panel, name: str, artifact_dir: Path | None) -> None:
    panel.locator("[data-structure-canvas]").press("ArrowRight")
    panel.locator('[data-structure-action="in"]').click()
    before = snapshot(panel)
    with page.expect_download() as pending:
        panel.locator("[data-structure-export]").click()
    download = pending.value
    assert download.failure() is None
    image = Image.open(download.path())
    assert image.format == "PNG" and max(image.size) == 4096, image.size
    expected = tuple(max(1, round(value * 4096 / max(before["width"], before["height"]))) for value in (before["width"], before["height"]))
    assert all(abs(actual - target) <= 1 for actual, target in zip(image.size, expected, strict=True)), (image.size, expected)
    assert download.suggested_filename == f"{name}-{image.width}x{image.height}.png"
    assert image.convert("RGBA").getextrema()[3] == (255, 255), "Background must stay opaque"
    reference = Image.open(io.BytesIO(base64.b64decode(before["png"].split(",", 1)[1])))
    rgb = image.convert("RGB")
    assert rgb.getpixel((0, 0)) == reference.convert("RGB").getpixel((0, 0)), "Background color changed"
    foreground = ImageChops.difference(rgb, Image.new("RGB", image.size, rgb.getpixel((0, 0)))).convert("L").histogram()
    assert sum(foreground[16:]) > 100, "Export is blank"
    resized = image.convert("RGB").resize((128, 128), Image.Resampling.LANCZOS)
    difference = ImageStat.Stat(ImageChops.difference(resized, reference.convert("RGB").resize((128, 128), Image.Resampling.LANCZOS)))
    assert max(difference.rms) < 15, difference.rms
    after = snapshot(panel)
    assert after == before, "Export changed the interactive view or canvas size"
    expect(panel.locator("[data-structure-export]")).to_be_enabled()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Export controls overflow"
    if artifact_dir:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        download.save_as(artifact_dir / f"{page.viewport_size['width']}-{download.suggested_filename}")


def inspect(page, artifact_dir: Path | None, scheme: str) -> None:
    pdb, gro = structures()
    compose = page.locator("#compose-structure")
    detail = page.locator("#detail-structure")
    expect(compose.locator("[data-structure-export]")).to_be_disabled()
    expect(detail.locator("[data-structure-export]")).to_be_disabled()
    with page.expect_worker() as started:
        page.locator("#files").set_input_files({"name": "peptide.PDB", "mimeType": "text/plain", "buffer": pdb})
    assert "/structure-worker.js?" in started.value.url
    expect(compose).to_have_attribute("data-state", "ready", timeout=20000)
    if scheme == "dark":
        compose.locator("[data-structure-style]").select_option("line")
    check_download(page, compose, "peptide", artifact_dir)

    # A failed encoding restores the view and allows another attempt.
    before = snapshot(compose)
    compose.locator("canvas").evaluate("""canvas => {
        const original = canvas.toBlob.bind(canvas);
        canvas.toBlob = callback => { canvas.toBlob = original; callback(null); };
    }""")
    compose.locator("[data-structure-export]").click()
    expect(page.locator("#toast")).to_contain_text("PNG 导出失败")
    expect(compose.locator("[data-structure-export]")).to_be_enabled()
    assert snapshot(compose) == before

    # Simulate an allocation/rendering failure after the canvas has been resized.
    compose.locator("canvas").evaluate("""canvas => {
        const viewer = canvas._3dmol_viewer, original = viewer.render.bind(viewer);
        viewer.render = () => { viewer.render = original; throw new Error('Synthetic render failure'); };
    }""")
    compose.locator("[data-structure-export]").click()
    expect(page.locator("#toast")).to_contain_text("Synthetic render failure")
    expect(compose.locator("[data-structure-export]")).to_be_enabled()
    assert snapshot(compose) == before

    # Collapse only a confirmed empty listing, not an error or a real structure.
    page.locator('[data-workspace-view="detail"]').click()
    note = detail.locator('.structure-empty-note')
    expect(note).to_be_visible()
    assert detail.bounding_box()['height'] < 64
    files_url = "**/api/jobs/product-1/files?structures_only=true&limit=200"
    page.route(files_url, lambda route: route.fulfill(status=503, json={'detail': 'synthetic failure'}))
    detail.locator('[data-structure-refresh]').click()
    expect(note).to_be_hidden()
    expect(detail.locator('[data-structure-message]')).to_contain_text('读取失败')
    page.unroute(files_url)

    # A refresh discovers the new structure without reselecting the job.
    page.route(
        "**/api/jobs/product-1/files?structures_only=true&limit=200",
        lambda route: route.fulfill(
            content_type="application/json",
            body=json.dumps(
                {
                    "files": [{"path": "results/final.gro", "size": len(gro), "modified": 1}],
                    "has_more": False,
                }
            ),
        ),
    )
    page.context.route("**/api/jobs/product-1/download?**", lambda route: route.fulfill(content_type="text/plain", body=gro))
    detail.locator('[data-structure-refresh]').click()
    expect(detail).to_have_attribute("data-state", "ready", timeout=20000)
    assert detail.bounding_box()['height'] > 400
    detail.locator("[data-structure-style]").select_option("stick")
    check_download(page, detail, "final", artifact_dir)
    page.locator('[data-workspace-view="compose"]').click()
    expect(compose).to_have_attribute("data-state", "ready")
    compose.locator('[data-structure-action="out"]').click()
    page.locator("#files").set_input_files({"name": "invalid.pdb", "mimeType": "text/plain", "buffer": b"END\n"})
    expect(compose).to_have_attribute("data-state", "error")
    expect(compose.locator("[data-structure-export]")).to_be_disabled()
    if scheme == "light":
        # Exercise near-limit reading/filtering in a real Worker with a small renderable result.
        water = pdb.splitlines(keepends=True)[0].replace(b"ALA", b"HOH")
        large = water * ((19 * 1024 * 1024) // len(water)) + pdb
        with page.expect_worker() as started:
            page.locator("#files").set_input_files({"name": "hydrated.pdb", "mimeType": "text/plain", "buffer": large})
        assert "/structure-worker.js?" in started.value.url
        expect(compose).to_have_attribute("data-state", "ready", timeout=25000)
        expect(compose.locator("[data-structure-summary]")).to_contain_text("水分子已隐藏")


def check_browser(artifact_dir: Path | None = None) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--enable-unsafe-swiftshader"])
        try:
            for width, height, scheme, ratio in ((1440, 1000, "light", 1), (1280, 800, "dark", 2.5)):
                check_viewport(
                    browser,
                    width,
                    height,
                    scheme,
                    None,
                    inspect=lambda page: inspect(page, artifact_dir, scheme),
                    context_options={"device_scale_factor": ratio},
                )
        finally:
            browser.close()
    print(
        "Structure PNG exports passed: real 4096px rendering, PDB/GRO, both panels, camera/style/background, high DPI and failure recovery."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path)
    check_browser(parser.parse_args().artifact_dir)
