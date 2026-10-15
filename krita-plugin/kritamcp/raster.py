"""
Pure-Python geometry and the fallback "pixel" brush engine.

No Krita imports, so it can be unit-tested. Pixel buffers use Krita's
8-bit RGBA layout, which is BGRA byte order.
"""

import math

MAX_SAMPLES = 50000


def catmull_rom(p0, p1, p2, p3, t):
    t2 = t * t
    t3 = t2 * t
    return tuple(
        0.5 * ((2 * b) + (-a + c) * t + (2 * a - 5 * b + 4 * c - d) * t2
               + (-a + 3 * b - 3 * c + d) * t3)
        for a, b, c, d in zip(p0, p1, p2, p3)
    )


def smooth_polyline(points, pressures=None, spacing=3.0, smooth=True):
    """Resample a stroke into evenly spaced (x, y, pressure) samples.

    With smooth=True the path is a Catmull-Rom spline through the points,
    so a few control points give a flowing, hand-drawn curve.
    """
    n = len(points)
    if pressures is None:
        pressures = [1.0] * n
    spacing = max(0.5, float(spacing))
    pts = [(float(p[0]), float(p[1])) for p in points]
    samples = [(pts[0][0], pts[0][1], float(pressures[0]))]

    for i in range(n - 1):
        p1, p2 = pts[i], pts[i + 1]
        seg_len = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
        steps = max(1, int(math.ceil(seg_len / spacing)))
        p0 = pts[i - 1] if i > 0 else p1
        p3 = pts[i + 2] if i + 2 < n else p2
        pr1, pr2 = float(pressures[i]), float(pressures[i + 1])
        for s in range(1, steps + 1):
            t = s / steps
            if smooth and n > 2:
                x, y = catmull_rom(p0, p1, p2, p3, t)
            else:
                x = p1[0] + (p2[0] - p1[0]) * t
                y = p1[1] + (p2[1] - p1[1]) * t
            samples.append((x, y, pr1 + (pr2 - pr1) * t))
            if len(samples) > MAX_SAMPLES:
                raise ValueError("Stroke too long; split it into several strokes")
    return samples


def pressure_runs(samples, step=0.05):
    """Group consecutive samples whose quantized pressure is equal.

    Each run after the first starts at the previous run's last point, so the
    pieces join without gaps. Returns [(pressure, [(x, y), ...]), ...].
    """
    runs = []
    current_q = None
    current = []
    for x, y, p in samples:
        q = round(max(0.0, min(1.0, p)) / step) * step
        if current_q is None:
            current_q, current = q, [(x, y)]
        elif q == current_q:
            current.append((x, y))
        else:
            runs.append((current_q, current))
            current_q, current = q, [current[-1], (x, y)]
    if current:
        runs.append((current_q, current))
    return runs


def resample(samples, spacing):
    """Re-space (x, y, p) samples so consecutive ones are <= spacing apart."""
    out = [samples[0]]
    for (x1, y1, p1), (x2, y2, p2) in zip(samples, samples[1:]):
        d = math.hypot(x2 - x1, y2 - y1)
        steps = max(1, int(math.ceil(d / spacing)))
        for s in range(1, steps + 1):
            t = s / steps
            out.append((x1 + (x2 - x1) * t, y1 + (y2 - y1) * t, p1 + (p2 - p1) * t))
    return out


def falloff(dist_ratio, hardness):
    """Brush tip alpha (0..1) at a normalized distance from the centre."""
    if dist_ratio >= 1.0:
        return 0.0
    if hardness >= 1.0 or dist_ratio <= hardness:
        return 1.0
    x = (dist_ratio - hardness) / (1.0 - hardness)
    return 1.0 - x * x * (3 - 2 * x)  # smoothstep for a softer, natural edge


def stroke_mask(samples, radius, hardness, affects, bbox):
    """Coverage mask (list of floats, row-major in bbox) for a stroke.

    Uses max() rather than accumulating dabs, so `opacity` behaves like a
    real stroke's opacity instead of building up where dabs overlap.
    """
    x0, y0, w, h = bbox
    mask = [0.0] * (w * h)
    size_on = affects in ("size", "both")
    alpha_on = affects in ("opacity", "both")
    max_r = max(0.5, radius)
    for cx, cy, p in resample(samples, max(0.75, max_r / 4.0)):
        p = max(0.0, min(1.0, p))
        r = max_r * p if size_on else max_r
        if r < 0.5:
            continue
        a = p if alpha_on else 1.0
        if a <= 0:
            continue
        rr = int(math.ceil(r)) + 1
        icx, icy = int(round(cx)), int(round(cy))
        for py in range(max(y0, icy - rr), min(y0 + h, icy + rr + 1)):
            dy = py + 0.5 - cy
            row = (py - y0) * w
            for px in range(max(x0, icx - rr), min(x0 + w, icx + rr + 1)):
                dx = px + 0.5 - cx
                d = math.sqrt(dx * dx + dy * dy)
                cov = falloff(d / r, hardness) if d < r else 0.0
                if hardness >= 1.0 and r - 1 < d < r + 1:
                    cov = max(cov, min(1.0, r + 0.5 - d))  # 1px anti-aliased rim
                if cov > 0:
                    idx = row + (px - x0)
                    v = cov * a
                    if v > mask[idx]:
                        mask[idx] = v
    return mask


def polygon_mask(poly, bbox):
    """Even-odd scanline fill of a polygon into a coverage mask."""
    x0, y0, w, h = bbox
    mask = [0.0] * (w * h)
    n = len(poly)
    for py in range(h):
        yc = y0 + py + 0.5
        xs = []
        for i in range(n):
            ax, ay = poly[i]
            bx, by = poly[(i + 1) % n]
            if (ay <= yc < by) or (by <= yc < ay):
                xs.append(ax + (yc - ay) * (bx - ax) / (by - ay))
        xs.sort()
        row = py * w
        for i in range(0, len(xs) - 1, 2):
            start = max(0, int(math.ceil(xs[i] - 0.5)) - x0)
            end = min(w, int(math.floor(xs[i + 1] - 0.5)) - x0 + 1)
            for px in range(start, end):
                mask[row + px] = 1.0
    return mask


def ellipse_polygon(x, y, w, h):
    cx, cy, rx, ry = x + w / 2.0, y + h / 2.0, w / 2.0, h / 2.0
    n = max(24, int(math.pi * (rx + ry) / 3))
    return [(cx + rx * math.cos(2 * math.pi * i / n), cy + ry * math.sin(2 * math.pi * i / n))
            for i in range(n)]


def composite(pixels, mask, color, opacity=1.0, erase=False):
    """Composite `color` through `mask` onto a BGRA bytearray (source-over)."""
    r, g, b = color
    for i, m in enumerate(mask):
        if m <= 0:
            continue
        sa = m * opacity
        if sa <= 0:
            continue
        k = i * 4
        da = pixels[k + 3] / 255.0
        if erase:
            pixels[k + 3] = int(round(da * (1.0 - sa) * 255))
            continue
        oa = sa + da * (1.0 - sa)
        if oa <= 0:
            continue
        keep = da * (1.0 - sa)
        pixels[k] = int(round((b * sa + pixels[k] * keep) / oa))
        pixels[k + 1] = int(round((g * sa + pixels[k + 1] * keep) / oa))
        pixels[k + 2] = int(round((r * sa + pixels[k + 2] * keep) / oa))
        pixels[k + 3] = int(round(oa * 255))


def clamp_bbox(min_x, min_y, max_x, max_y, width, height):
    x0 = max(0, int(math.floor(min_x)))
    y0 = max(0, int(math.floor(min_y)))
    x1 = min(width, int(math.ceil(max_x)))
    y1 = min(height, int(math.ceil(max_y)))
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1 - x0, y1 - y0
