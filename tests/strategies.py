"""Hypothesis strategies for random, valid machines, profiles and plans."""

from hypothesis import strategies as st

from cnc_warmup.model import (
    AxisLimits,
    Coolant,
    Machine,
    Ramp,
    SweepMove,
    WarmupProfile,
    ZStrokeAt,
)
from cnc_warmup.plan import WarmupPlan, build_plan


@st.composite
def machines(draw: st.DrawFn) -> Machine:
    def axis() -> AxisLimits:
        low = draw(st.integers(min_value=-3000, max_value=0))
        return AxisLimits(low, low + draw(st.integers(min_value=200, max_value=3000)))

    return Machine(id="MX", x=axis(), y=axis(), z=axis(), spindle_max_rpm=30_000, max_feed=30_000)


@st.composite
def profiles(draw: st.DrawFn) -> WarmupProfile:
    feeds = sorted(draw(st.lists(st.floats(500, 20_000), min_size=2, max_size=2)))
    rpms = sorted(draw(st.lists(st.floats(100, 20_000), min_size=2, max_size=2)))
    xy = draw(
        st.lists(
            st.sampled_from([SweepMove.PERIMETER, SweepMove.DIAGONALS]),
            min_size=1,
            max_size=2,
            unique=True,
        )
    )
    return WarmupProfile(
        name="random",
        duration_min=draw(st.floats(1, 60)),
        stages=draw(st.integers(2, 20)),
        feed_start=feeds[0],
        feed_end=feeds[1],
        rpm_start=rpms[0],
        rpm_end=rpms[1],
        ramp=draw(st.sampled_from(Ramp)),
        coolant=draw(st.sampled_from(Coolant)),
        edge_margin_mm=draw(st.floats(0, 25)),
        pattern=tuple(draw(st.permutations([*xy, SweepMove.Z_STROKE]))),
        z_stroke_at=draw(st.sampled_from(ZStrokeAt)),
        operator_confirm=draw(st.booleans()),
        runtime_guards=draw(st.booleans()),
        final_rapid_pass=draw(st.booleans()),
    )


plans: st.SearchStrategy[WarmupPlan] = st.builds(build_plan, machines(), profiles())
