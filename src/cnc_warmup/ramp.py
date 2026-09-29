"""Stepped values for a warm-up that ramps gradually from a start to a finish value."""

import math

from cnc_warmup.model import Ramp


def ramp_values(
    start: float, end: float, count: int, kind: Ramp, *, step: float = 10.0
) -> tuple[float, ...]:
    """Return ``count`` values rising from ``start`` to ``end``.

    Linear ramps take equal steps. Geometric ramps take equal ratios, which puts
    more of the stages at low speed. Intermediate values are rounded to a
    multiple of ``step`` so programs show round numbers (S3250, not S3249.99).
    The endpoints stay exactly as configured, and rounding never moves a value
    outside ``[start, end]`` or out of order.
    """
    if count < 2:
        raise ValueError(f"a ramp needs at least 2 values (got {count})")
    if not 0 < start <= end:
        raise ValueError(f"a ramp needs 0 < start <= end (got {start}, {end})")

    values = []
    for index in range(count):
        fraction = index / (count - 1)
        if kind is Ramp.LINEAR:
            raw = start + (end - start) * fraction
        else:
            raw = start * (end / start) ** fraction
        rounded = math.floor(raw / step + 0.5) * step  # round half up
        values.append(min(max(rounded, start), end))
    values[0], values[-1] = start, end
    return tuple(values)
