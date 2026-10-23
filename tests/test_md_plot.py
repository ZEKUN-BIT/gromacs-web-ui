"""Scientific invariants of the raw-data and centered-trend export style."""

from pathlib import Path

import numpy as np
import pytest

from scripts import md_plot


def test_centered_mean_keeps_event_at_its_time_and_omits_incomplete_windows():
    x = np.arange(1001) * 0.01
    y = np.zeros(len(x))
    y[500] = 101
    tx, ty, details = md_plot.centered_trend(x, y, 1.0)
    assert details["window_points"] == 101
    assert tx[0] == pytest.approx(0.5)
    assert tx[-1] == pytest.approx(9.5)
    nonzero_times = np.asarray(tx)[np.asarray(ty) > 0]
    assert (nonzero_times[0] + nonzero_times[-1]) / 2 == pytest.approx(5.0)
    assert max(ty) == pytest.approx(1.0)


def test_rounded_xvg_times_do_not_create_artificial_gaps():
    x = np.arange(10001, dtype=np.float32) / np.float32(100)
    tx, ty, details = md_plot.centered_trend(x, np.ones(len(x)), 1.0)
    assert len(tx) == 9901
    assert details["window_points"] == 101
    assert np.isfinite(ty).all()
    np.testing.assert_allclose(ty, 1)


def test_real_missing_interval_is_not_smoothed_across():
    x = np.concatenate((np.arange(201) * 0.01, 4 + np.arange(201) * 0.01))
    y = np.concatenate((np.zeros(201), np.ones(201) * 10))
    tx, ty, _ = md_plot.centered_trend(x, y, 1.0)
    assert np.isnan(tx).sum() == 1
    finite = np.asarray(ty)[np.isfinite(ty)]
    assert set(finite) == {0, 10}


def test_style_preserves_raw_and_handles_units_and_repeated_saves():
    plt, _ = md_plot.plotting_modules()
    fig, axes = plt.subplots(1, 2)
    x = np.arange(10001) * 0.01
    y = np.sin(x)
    for ax, scale, label in zip(axes, [1, 1000], ["Time (ns)", "Time (ps)"], strict=True):
        ax.plot(x * scale, y, color="#67899C", label="Sample A")
        ax.set_xlabel(label)
        ax.set_title("Omit")
        ax.legend()
    records = md_plot.apply_raw_trend_style(fig)
    assert len(records) == 2
    assert all(r["window_ns"] == 1 for r in records)
    for ax in axes:
        np.testing.assert_array_equal(ax.lines[0].get_ydata(), y)
        assert ax.get_legend() is None and not ax.get_title()
    np.testing.assert_allclose(axes[0].lines[1].get_ydata(), axes[1].lines[1].get_ydata())
    assert md_plot.apply_raw_trend_style(fig) == records
    assert [len(ax.lines) for ax in axes] == [2, 2]
    plt.close(fig)


def test_non_time_and_sparse_series_do_not_gain_a_trend():
    plt, _ = md_plot.plotting_modules()
    fig, axes = plt.subplots(1, 2)
    for ax, label in zip(axes, ["Residue", "Time (ns)"], strict=True):
        ax.plot(np.arange(101), np.ones(101))
        ax.set_xlabel(label)
    records = md_plot.apply_raw_trend_style(fig)
    assert all(not r["smoothed"] for r in records)
    assert [len(ax.lines) for ax in axes] == [1, 1]
    plt.close(fig)


def test_full_resolution_reader_selects_named_column_without_preview_sampling(tmp_path: Path):
    path = tmp_path / "observables.xvg"
    path.write_text('@ s0 legend "Energy"\n@ s1 legend "Temperature"\n' + "".join(f"{i * 0.01} {-i} {300 + i}\n" for i in range(10001)))
    points = md_plot.full_resolution_plot_points(tmp_path, {"path": path.name, "series": ["Temperature"]})
    assert len(points) == 10001
    assert points[0] == [0, 300]
    assert points[-1] == [100, 10300]


def test_indented_and_ambiguous_xvg_legends(tmp_path):
    path = tmp_path / "columns.xvg"
    path.write_text('  @ s0 legend "First"\n  @ s1 legend "Second"\n0 10 100\n1 20 200\n')
    plot = {"path": path.name, "series": ["Second"]}
    assert md_plot.full_resolution_plot_points(tmp_path, plot) == [[0, 100], [1, 200]]
    path.write_text(path.read_text().replace('"First"', '"Second"'))
    with pytest.raises(ValueError, match="ambiguous"):
        md_plot.full_resolution_plot_points(tmp_path, plot)
    assert md_plot.full_resolution_plot_points(tmp_path, plot, y_column=2) == [[0, 100], [1, 200]]


@pytest.mark.parametrize("window", [2, 20, 51])
def test_sample_count_override_is_per_curve_and_even_windows_are_centered(window):
    plt, _ = md_plot.plotting_modules()
    fig, ax = plt.subplots()
    for dt in [0.01, 0.1]:
        x = np.arange(1001) * dt
        (line,) = ax.plot(x, 2 * x)
        line._md_smooth_points = window
    ax.set_xlabel("Time (ns)")
    records = md_plot.apply_raw_trend_style(fig)
    assert [r["smoothing"]["window_points"] for r in records] == [window, window]
    for line in ax.lines[2:]:
        np.testing.assert_allclose(line.get_ydata(), 2 * line.get_xdata(), rtol=1e-11)
        assert len(line.get_xdata()) == 1002 - window
    plt.close(fig)


@pytest.mark.parametrize(
    "ylabel,series,expected_trend",
    [
        ("Hydrogen bonds (count)", "ASP", False),
        ("Protein–ASP H-bonds", "ASP", False),
        ("Number", "Hydrogen bonds", False),
        ("H-bond distance (nm)", "ASP", True),
        ("Hydrogen-bond angle (degrees)", "ASP", True),
        ("Contacts (count)", "ASP", True),
    ],
)
def test_hbond_counts_stay_raw_while_other_time_metrics_keep_their_trend(ylabel, series, expected_trend):
    plt, _ = md_plot.plotting_modules()
    fig, ax = plt.subplots()
    x = np.arange(10001) * 0.01
    values = np.arange(len(x)) % 4
    ax.plot(x, values, label=series)
    ax.set(xlabel="Time (ns)", ylabel=ylabel)
    records = md_plot.apply_raw_trend_style(fig)
    assert records[0]["smoothed"] == expected_trend
    assert len(ax.lines) == (2 if expected_trend else 1)
    np.testing.assert_array_equal(ax.lines[0].get_ydata(), values)
    assert ax.get_legend() is None
    plt.close(fig)
