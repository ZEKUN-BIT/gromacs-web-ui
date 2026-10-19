from __future__ import annotations

import shutil
from pathlib import Path

from scripts.sync_frontend_assets import synchronize


def make_graph(root: Path) -> None:
    (root / "app/static/js/nested").mkdir(parents=True)
    (root / "app/templates").mkdir()
    (root / "app/static/js/main.js").write_text('import "./nested/part.js?v=old";\n')
    (root / "app/static/js/nested/part.js").write_text('export const message = "first";\n')
    (root / "app/static/styles.css").write_text("body { color: black; }\n")
    (root / "app/templates/index.html").write_text(
        '<link href="/static/styles.css?v=older"><script src="/static/js/main.js?v=older"></script>\n'
    )


def test_check_is_read_only_and_sync_is_stable_across_checkout_paths(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    make_graph(first)
    page = first / "app/templates/index.html"
    original = page.read_bytes()
    assert not synchronize(first, check=True)
    assert page.read_bytes() == original
    assert synchronize(first)
    generated = page.read_bytes()
    assert generated != original
    assert synchronize(first, check=True)
    assert synchronize(first)
    assert page.read_bytes() == generated
    shutil.copytree(first, second)
    assert synchronize(second, check=True)


def test_nested_module_or_stylesheet_change_invalidates_all_entrypoints(tmp_path):
    make_graph(tmp_path)
    assert synchronize(tmp_path)
    page = tmp_path / "app/templates/index.html"
    main = tmp_path / "app/static/js/main.js"
    previous_page, previous_main = page.read_text(), main.read_text()
    (tmp_path / "app/static/js/nested/part.js").write_text('export const message = "changed";\n')
    assert not synchronize(tmp_path, check=True)
    assert synchronize(tmp_path)
    assert page.read_text() != previous_page
    assert main.read_text() != previous_main
    previous_page = page.read_text()
    (tmp_path / "app/static/styles.css").write_text("body { color: green; }\n")
    assert not synchronize(tmp_path, check=True)
    assert synchronize(tmp_path)
    assert page.read_text() != previous_page
