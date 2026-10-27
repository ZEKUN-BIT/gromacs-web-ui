# Browser test assets

These files are used only by the isolated browser quality harness. They are
checked into the repository so tests require no third-party network requests.
They are not application assets and are excluded from the application wheel and
Windows application payload. Keep their licenses with any redistributed test
fixtures.

| Asset | Source and version | License |
| --- | --- | --- |
| `axe.min.js` | [Deque axe-core 4.10.3 source](https://github.com/dequelabs/axe-core/tree/v4.10.3), unmodified browser distribution | Mozilla Public License 2.0; `axe-LICENSE.txt` |
| `cjk-400.otf`, `cjk-500.otf`, `cjk-600.otf` | [Noto Sans CJK 2.004](https://github.com/notofonts/noto-cjk/releases/tag/Sans2.004), Simplified Chinese face, modified subsets | SIL Open Font License 1.1; `Noto-CJK-LICENSE.txt` |

The axe distribution retains its upstream copyright and license header. The
linked source release is the corresponding source for this vendored version.
`Noto-CJK-LICENSE.txt` preserves the Debian/Ubuntu package copyright record,
including the full SIL OFL text for the font files. Its separate Debian packaging
license section does not change the font license.

The CJK fonts are derived from the SC face (TTC index 2) of
`NotoSansCJK-Regular.ttc`, `NotoSansCJK-Medium.ttc` and
`NotoSansCJK-Bold.ttc`. Their family is renamed to `Gromacs Snapshot CJK` and
their PostScript names use the `GromacsSnapshotCJK-` prefix. Test CSS maps them
to weights 400, 500 and 600 respectively; the 600 asset uses the upstream Bold
outlines with its weight metadata set to 600. These modifications are test
fixtures, not an upstream Noto release.

`scripts/build_snapshot_fonts.py` collects non-ASCII characters from `.html`,
`.js` and `.py` files under `app/` and `scripts/`, plus printable ASCII, and uses
the locked fontTools dependency to subset the fonts. It preserves the upstream
font timestamp, so identical inputs produce identical bytes. The current files contain
915 mapped Unicode code points each. To rebuild after adding visible text:

```bash
python scripts/build_snapshot_fonts.py --source /path/to/noto-cjk-2.004-ttc
sha256sum tests/browser_assets/axe.min.js tests/browser_assets/*LICENSE.txt tests/browser_assets/cjk-*.otf
```

Use the 2.004 TTC inputs, retain the notices and renamed families, inspect the
generated font changes, and update the hashes below. A glyph or rendering change
also requires the explicit snapshot review described in
[FRONTEND-QUALITY.md](../../docs/FRONTEND-QUALITY.md). Font generation and snapshot
acceptance are maintenance operations; CI performs neither automatically.

SHA-256 of the checked-in assets:

| File | SHA-256 |
| --- | --- |
| `axe.min.js` | `880970c081707360e64f34cea25ff91892f5bc95675b0776925b9709dd8a68bb` |
| `axe-LICENSE.txt` | `af175b9d96ee93c21a036152e1b905b0b95304d4ae8c2c921c7609100ba8df7e` |
| `Noto-CJK-LICENSE.txt` | `849f4ea9c214fa4ac3593b770c699f387534b11ce671264c1b10d85bdcb5997b` |
| `cjk-400.otf` | `d4b80c9cee46023ee104479ba968fe1df442229f086331183026e395872348ac` |
| `cjk-500.otf` | `ae650633d1fbbe0042d38dad0649e0ceb6d7e2fb4bd0a1c67a413174f55f4b92` |
| `cjk-600.otf` | `3dff849ebf47e72663ca9b58e5f55636b0650628234a2a2d90df21ccc4dd349a` |
