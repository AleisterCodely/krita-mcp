import math

from kritamcp import raster


def test_smooth_polyline_hits_endpoints_and_interpolates_pressure():
    s = raster.smooth_polyline([(0, 0), (50, 20), (100, 0)], [0.0, 1.0, 0.0], spacing=2)
    assert s[0][:2] == (0.0, 0.0) and math.isclose(s[-1][0], 100) and math.isclose(s[-1][1], 0, abs_tol=1e-9)
    assert max(p for _, _, p in s) == 1.0
    assert all(math.hypot(b[0] - a[0], b[1] - a[1]) < 4 for a, b in zip(s, s[1:]))


def test_pressure_runs_are_continuous():
    s = raster.smooth_polyline([(0, 0), (100, 0)], [0.2, 1.0], spacing=1)
    runs = raster.pressure_runs(s, 0.1)
    assert len(runs) > 3
    for (_, a), (_, b) in zip(runs, runs[1:]):
        assert a[-1] == b[0]  # pieces join without gaps


def test_uniform_pressure_is_single_run():
    s = raster.smooth_polyline([(0, 0), (30, 40), (90, 10)], None, spacing=2)
    assert len(raster.pressure_runs(s)) == 1


def _paint(pixels, w, h, mask_fn, color, opacity=1.0, erase=False):
    raster.composite(pixels, mask_fn((0, 0, w, h)), color, opacity, erase)


def test_no_dark_fringe_on_transparent_layer():
    w = h = 20
    px = bytearray(w * h * 4)  # transparent black
    samples = [(5, 10, 1.0), (15, 10, 1.0)]
    _paint(px, w, h, lambda b: raster.stroke_mask(samples, 4, 0.0, "none", b), (200, 100, 50))
    for i in range(0, len(px), 4):
        if px[i + 3]:
            assert (px[i + 2], px[i + 1], px[i]) == (200, 100, 50)


def test_opacity_does_not_build_up():
    w, h = 60, 20
    px = bytearray([255, 255, 255, 255] * (w * h))
    samples = [(5, 10, 1.0), (55, 10, 1.0)]
    _paint(px, w, h, lambda b: raster.stroke_mask(samples, 5, 1.0, "none", b), (0, 0, 0), opacity=0.5)
    centre = (10 * w + 30) * 4
    assert abs(px[centre] - 128) <= 1  # one 50% coat, not many


def test_erase():
    w = h = 10
    px = bytearray([10, 20, 30, 255] * (w * h))
    _paint(px, w, h, lambda b: raster.polygon_mask([(0, 0), (10, 0), (10, 10), (0, 10)], b),
           (0, 0, 0), erase=True)
    assert all(px[i + 3] == 0 for i in range(0, len(px), 4))


def test_polygon_and_ellipse_area():
    m = raster.polygon_mask([(0, 0), (10, 0), (10, 10), (0, 10)], (0, 0, 20, 20))
    assert sum(m) == 100
    m = raster.polygon_mask(raster.ellipse_polygon(0, 0, 40, 40), (0, 0, 40, 40))
    assert abs(sum(m) - math.pi * 400) / (math.pi * 400) < 0.03


def test_clamp_bbox():
    assert raster.clamp_bbox(-5, -5, 10, 10, 8, 8) == (0, 0, 8, 8)
    assert raster.clamp_bbox(20, 20, 30, 30, 8, 8) is None
