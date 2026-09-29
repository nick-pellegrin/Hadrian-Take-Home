import pytest
from hypothesis import given
from hypothesis import strategies as st

from cnc_warmup.model import Ramp
from cnc_warmup.ramp import ramp_values


@pytest.mark.parametrize(
    ("start", "end", "count", "kind", "expected"),
    [
        pytest.param(
            2500, 12000, 5, Ramp.LINEAR, (2500, 4880, 7250, 9630, 12000), id="linear-rounds-half-up"
        ),
        pytest.param(
            500, 10000, 6, Ramp.GEOMETRIC, (500, 910, 1660, 3020, 5490, 10000), id="geometric"
        ),
        pytest.param(1234, 5678, 3, Ramp.LINEAR, (1234, 3460, 5678), id="endpoints-kept-exactly"),
        pytest.param(3000, 3000, 4, Ramp.GEOMETRIC, (3000, 3000, 3000, 3000), id="flat"),
    ],
)
def test_ramp_values(
    start: float, end: float, count: int, kind: Ramp, expected: tuple[float, ...]
) -> None:
    assert ramp_values(start, end, count, kind) == expected


@pytest.mark.parametrize(
    ("start", "end", "count"),
    [(1000, 2000, 1), (0, 2000, 5), (2000, 1000, 5)],
    ids=["one-value", "zero-start", "descending"],
)
def test_invalid_ramps_are_rejected(start: float, end: float, count: int) -> None:
    with pytest.raises(ValueError, match="a ramp needs"):
        ramp_values(start, end, count, Ramp.LINEAR)


@given(
    bounds=st.lists(st.floats(min_value=1, max_value=50_000), min_size=2, max_size=2).map(sorted),
    count=st.integers(min_value=2, max_value=20),
    kind=st.sampled_from(Ramp),
)
def test_ramps_rise_monotonically_between_exact_endpoints(
    bounds: list[float], count: int, kind: Ramp
) -> None:
    start, end = bounds

    values = ramp_values(start, end, count, kind)

    assert len(values) == count
    assert (values[0], values[-1]) == (start, end)
    assert all(start <= value <= end for value in values)
    assert list(values) == sorted(values)
