"""Chart geometry -- presentation helpers that turn dashboard data into
render-ready SVG primitives. No DB access, no HTTP: pure functions the UI
layer calls after fetching data from ``DashboardRepo``.
"""

from __future__ import annotations

import math

#: Day ranges offered by the task-flow chart's range toggle.
FLOW_CHART_RANGES = (30, 90)

#: Radius of the objective progress ring, in SVG units.
RING_RADIUS = 26
#: Ring circumference -- the ``stroke-dasharray`` total.
RING_CIRC = round(2 * math.pi * RING_RADIUS, 2)


def ring_dash(pct: int) -> float:
    """Filled arc length for a progress ring at ``pct`` percent."""
    return round(RING_CIRC * pct / 100, 2)


def smooth_path(pts: list[tuple[float, float]]) -> str:
    """Build an SVG path ``d`` through ``pts`` using a Catmull-Rom spline
    converted to cubic beziers, so the line reads as a smooth curve."""
    if not pts:
        return ""
    if len(pts) == 1:
        return f"M {pts[0][0]:.1f},{pts[0][1]:.1f}"
    n = len(pts)
    d = [f"M {pts[0][0]:.1f},{pts[0][1]:.1f}"]
    for i in range(n - 1):
        p0 = pts[i - 1] if i > 0 else pts[0]
        p1 = pts[i]
        p2 = pts[i + 1]
        p3 = pts[i + 2] if i + 2 < n else pts[n - 1]
        c1x = p1[0] + (p2[0] - p0[0]) / 6
        c1y = p1[1] + (p2[1] - p0[1]) / 6
        c2x = p2[0] - (p3[0] - p1[0]) / 6
        c2y = p2[1] - (p3[1] - p1[1]) / 6
        d.append(
            f"C {c1x:.1f},{c1y:.1f} {c2x:.1f},{c2y:.1f} {p2[0]:.1f},{p2[1]:.1f}"
        )
    return " ".join(d)


def _nice_max(value: int) -> int:
    if value <= 5:
        return 5
    if value <= 10:
        return 10
    step = 10 ** (len(str(value)) - 1)
    return ((value + step - 1) // step) * step


def build_flow_chart(
    rows: list[dict], width: int = 800, height: int = 220
) -> dict:
    """Turn a task-flow series (``[{day, opened, closed}, ...]``) into a
    render-ready dict of SVG geometry for ``task_flow_chart.html``."""
    pad_top, pad_right, pad_bottom, pad_left = 16, 12, 26, 36
    inner_w = width - pad_left - pad_right
    inner_h = height - pad_top - pad_bottom

    if not rows:
        return {"empty": True}

    raw_max = max(
        max((r["opened"] for r in rows), default=0),
        max((r["closed"] for r in rows), default=0),
        1,
    )
    max_y = _nice_max(raw_max)
    n = len(rows)

    def x_for(i: int) -> float:
        return pad_left + (inner_w * i / max(n - 1, 1))

    def y_for(v: int) -> float:
        return pad_top + inner_h - (inner_h * v / max_y)

    opened_xy = [(x_for(i), y_for(r["opened"])) for i, r in enumerate(rows)]
    closed_xy = [(x_for(i), y_for(r["closed"])) for i, r in enumerate(rows)]

    opened_dots = [
        {"x": x_for(i), "y": y_for(r["opened"]), "day": r["day"].isoformat(),
         "n": r["opened"]}
        for i, r in enumerate(rows)
    ]
    closed_dots = [
        {"x": x_for(i), "y": y_for(r["closed"]), "day": r["day"].isoformat(),
         "n": r["closed"]}
        for i, r in enumerate(rows)
    ]

    y_ticks = [
        {"y": y_for(round(max_y * k / 4)), "value": round(max_y * k / 4)}
        for k in range(5)
    ]

    n_labels = min(6, n)
    x_ticks = []
    for k in range(n_labels):
        i = (n - 1) * k // (n_labels - 1) if n_labels > 1 else 0
        x_ticks.append({"x": x_for(i), "label": rows[i]["day"].strftime("%b %d")})

    return {
        "empty": False,
        "width": width,
        "height": height,
        "pad_top": pad_top,
        "pad_left": pad_left,
        "inner_w": inner_w,
        "inner_h": inner_h,
        "opened_path": smooth_path(opened_xy),
        "closed_path": smooth_path(closed_xy),
        "opened_dots": opened_dots,
        "closed_dots": closed_dots,
        "y_ticks": y_ticks,
        "x_ticks": x_ticks,
        "max_y": max_y,
        "total_opened": sum(r["opened"] for r in rows),
        "total_closed": sum(r["closed"] for r in rows),
    }
