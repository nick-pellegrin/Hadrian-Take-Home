import pytest
from hypothesis import given, settings

from cnc_warmup.config import Catalog, ConfigError, apply_overrides
from cnc_warmup.issues import Severity
from cnc_warmup.model import Axis, Coolant, ZStrokeAt
from cnc_warmup.plan import (
    CHECKLIST,
    COOLANT_CHECK,
    CoolantOff,
    CoolantOn,
    Dwell,
    Motion,
    OperatorStop,
    Point,
    Position,
    ProgramEnd,
    RapidMove,
    RapidSweep,
    RunStage,
    RuntimeGuard,
    SafeStart,
    Section,
    SpindleOn,
    SpindleStop,
    WarmupPlan,
    build_plan,
    format_duration,
    format_number,
    trace,
)
from strategies import plans


@pytest.fixture
def m1_daily(catalog: Catalog) -> WarmupPlan:
    return build_plan(catalog.machine("M1"), catalog.profile("daily"))


def plan_with(catalog: Catalog, machine_id: str = "M1", **changes: object) -> WarmupPlan:
    profile = apply_overrides(catalog.profile("daily"), changes)
    return build_plan(catalog.machine(machine_id), profile)


def step_types(plan: WarmupPlan) -> list[type]:
    """The program's steps without the section headings."""
    return [type(step) for step in plan.steps if not isinstance(step, Section)]


# --- Examples on the shipped configuration ----------------------------------------


def test_sweep_visits_every_extreme_of_the_inset_travel(m1_daily: WarmupPlan) -> None:
    # M1 travel is X -762..0, Y -508..0, Z -500..0; the daily profile keeps 1 mm away.
    assert [(s.label, s.target) for s in m1_daily.sweep] == [
        ("X+ full stroke", Point(-1, -507, -1)),
        ("Y+ full stroke", Point(-1, -1, -1)),
        ("X- full stroke", Point(-761, -1, -1)),
        ("Y- full stroke", Point(-761, -507, -1)),
        ("XY diagonal, both axes together", Point(-1, -1, -1)),
        ("XY diagonal back", Point(-761, -507, -1)),
        ("to the XY center", Point(-381, -254, -1)),
        ("Z- full stroke", Point(-381, -254, -499)),
        ("Z+ full stroke", Point(-381, -254, -1)),
        ("back to the start corner", Point(-761, -507, -1)),
    ]
    assert m1_daily.sweep_length_mm == pytest.approx(6267.11, abs=0.01)


def test_stage_table(m1_daily: WarmupPlan) -> None:
    table = [(s.rpm, s.feed, s.passes, s.dwell_seconds) for s in m1_daily.stages]

    assert table == [
        (1000, 2500, 1, 89),
        (3250, 4880, 3, 8),
        (5500, 7250, 4, 32),
        (7750, 9630, 6, 5),
        (10000, 12000, 7, 20),
    ]
    assert format_duration(m1_daily.estimated_seconds) == "19:57"
    assert m1_daily.warnings == ()


def test_step_order(m1_daily: WarmupPlan) -> None:
    assert step_types(m1_daily) == [
        SafeStart,
        SpindleStop,
        CoolantOff,
        RuntimeGuard,
        OperatorStop,
        RapidMove,  # retract Z
        RapidMove,  # to the start corner
        SpindleOn,
        *[RunStage] * 5,
        SpindleStop,
        RapidMove,  # retract Z
        RapidMove,  # park
        ProgramEnd,
    ]


def test_section_titles(m1_daily: WarmupPlan) -> None:
    titles = [step.title for step in m1_daily.steps if isinstance(step, Section)]

    assert titles == [
        "Safe start: spindle and coolant off",
        "Travel check: the soft limits of the control must contain the sweep",
        "Operator check",
        "To the start corner: Z up first, then XY",
        "Spindle on",
        "Stage 1 of 5: 1000 rpm, 2500 mm/min, 1 pass, then 89 s dwell",
        "Stage 2 of 5: 3250 rpm, 4880 mm/min, 3 passes, then 8 s dwell",
        "Stage 3 of 5: 5500 rpm, 7250 mm/min, 4 passes, then 32 s dwell",
        "Stage 4 of 5: 7750 rpm, 9630 mm/min, 6 passes, then 5 s dwell",
        "Stage 5 of 5: 10000 rpm, 12000 mm/min, 7 passes, then 20 s dwell",
        "Shutdown: spindle off, Z up, park",
    ]


def test_first_move_retracts_z_only(m1_daily: WarmupPlan) -> None:
    first = next(step for step in m1_daily.steps if isinstance(step, RapidMove))

    assert first.target == Position(z=-1)


def test_guard_covers_the_commanded_envelope(m1_daily: WarmupPlan) -> None:
    guard = next(step for step in m1_daily.steps if isinstance(step, RuntimeGuard))

    assert guard.envelope == m1_daily.envelope


def test_disabled_guard_and_operator_stop_are_left_out(catalog: Catalog) -> None:
    plan = plan_with(catalog, runtime_guards=False, operator_confirm=False)

    assert not any(isinstance(step, RuntimeGuard | OperatorStop) for step in plan.steps)


def test_flood_coolant_runs_only_while_the_spindle_does(catalog: Catalog) -> None:
    plan = plan_with(catalog, coolant=Coolant.FLOOD)
    kinds = step_types(plan)

    assert kinds[kinds.index(SpindleOn) + 1] is CoolantOn
    assert kinds[-5:] == [CoolantOff, SpindleStop, RapidMove, RapidMove, ProgramEnd]
    stop = next(step for step in plan.steps if isinstance(step, OperatorStop))
    assert stop.checklist == (*CHECKLIST, COOLANT_CHECK)


def test_final_rapid_pass_follows_the_last_stage(catalog: Catalog) -> None:
    plan = plan_with(catalog, final_rapid_pass=True)
    kinds = step_types(plan)
    base = plan_with(catalog)

    assert kinds[kinds.index(RapidSweep) - 1] is RunStage
    assert plan.estimated_seconds - base.estimated_seconds == pytest.approx(
        plan.sweep_length_mm / plan.machine.max_feed * 60
    )


def test_z_stroke_at_the_start_corner_skips_the_center(catalog: Catalog) -> None:
    plan = plan_with(catalog, z_stroke_at=ZStrokeAt.START)

    assert [(s.label, s.target) for s in plan.sweep[-2:]] == [
        ("Z- full stroke", Point(-761, -507, -499)),
        ("Z+ full stroke", Point(-761, -507, -1)),
    ]
    assert all("center" not in segment.label for segment in plan.sweep)


def test_stage_too_slow_for_its_share_of_the_duration_warns(catalog: Catalog) -> None:
    plan = plan_with(catalog, "M3", feed_start=1000)

    [warning] = plan.warnings
    assert warning.path == "profiles.daily.feed_start"
    assert warning.severity is Severity.WARNING
    assert warning.message.startswith(
        "stage 1 runs 8:38, over its 4:00 share of the duration: one sweep pass at "
        "1000 mm/min takes that long on machine M3"
    )


def test_profile_the_machine_cannot_run_is_rejected(catalog: Catalog) -> None:
    with pytest.raises(ConfigError, match=r"profiles\.daily\.feed_end: 30000 mm/min exceeds"):
        plan_with(catalog, feed_end=30000)


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "0:00"), (59.6, "1:00"), (208.4, "3:28"), (3600, "60:00")],
)
def test_format_duration(seconds: float, expected: str) -> None:
    assert format_duration(seconds) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(2500.0, "2500"), (-761.5, "-761.5"), (0.1236, "0.124"), (-0.0001, "0"), (1e-9, "0")],
)
def test_format_number(value: float, expected: str) -> None:
    assert format_number(value) == expected


# --- Properties over random machines and profiles ---------------------------------


def motions(plan: WarmupPlan) -> list[Motion]:
    return [event for event in trace(plan) if isinstance(event, Motion)]


def coordinates(position: Position) -> dict[Axis, float | None]:
    return {Axis.X: position.x, Axis.Y: position.y, Axis.Z: position.z}


@settings(max_examples=150)
@given(plans)
def test_every_commanded_position_stays_inside_the_travel_less_the_margin(plan: WarmupPlan) -> None:
    margin = plan.profile.edge_margin_mm
    for motion in motions(plan):
        for axis, value in coordinates(motion.position).items():
            limits = plan.machine.limits(axis)
            if value is not None:
                assert limits.min + margin - 1e-6 <= value <= limits.max - margin + 1e-6


@settings(max_examples=150)
@given(plans)
def test_every_stage_sweeps_the_entire_travel(plan: WarmupPlan) -> None:
    targets = [segment.target for segment in plan.sweep]
    visited = {
        Axis.X: {target.x for target in targets},
        Axis.Y: {target.y for target in targets},
        Axis.Z: {target.z for target in targets},
    }

    for axis in Axis:
        limits = plan.envelope.limits(axis)
        assert {limits.min, limits.max} <= visited[axis]
    assert plan.sweep[-1].target == plan.envelope.start  # closed, so passes chain


@settings(max_examples=150)
@given(plans)
def test_feed_and_speed_ramp_up_from_start_to_finish(plan: WarmupPlan) -> None:
    profile = plan.profile
    feeds = [stage.feed for stage in plan.stages]
    rpms = [stage.rpm for stage in plan.stages]

    assert len(plan.stages) == profile.stages
    assert (feeds[0], feeds[-1], rpms[0], rpms[-1]) == (
        profile.feed_start,
        profile.feed_end,
        profile.rpm_start,
        profile.rpm_end,
    )
    assert feeds == sorted(feeds)
    assert rpms == sorted(rpms)


@settings(max_examples=150)
@given(plans)
def test_program_starts_safely(plan: WarmupPlan) -> None:
    first_move = next(i for i, step in enumerate(plan.steps) if isinstance(step, RapidMove))
    before_motion = {type(step) for step in plan.steps[:first_move]}
    all_motions = motions(plan)

    # Nothing moves until spindle and coolant are off and any guard/checklist has run.
    assert {SafeStart, SpindleStop, CoolantOff} <= before_motion
    assert (RuntimeGuard in before_motion) == plan.profile.runtime_guards
    assert (OperatorStop in before_motion) == plan.profile.operator_confirm
    # The first move lifts Z straight to the top of travel, with the spindle stopped.
    assert all_motions[0] == Motion(Position(z=plan.envelope.z.max), None, 0.0, False)
    # Feed moves only ever run with the spindle turning.
    assert all(m.spindle_rpm > 0 for m in all_motions if m.feed is not None)


@settings(max_examples=150)
@given(plans)
def test_program_ends_safely(plan: WarmupPlan) -> None:
    *_, retract, park = motions(plan)

    assert retract == Motion(
        Position(retract.position.x, retract.position.y, plan.envelope.z.max), None, 0.0, False
    )
    assert park.position == Position(plan.park.x, plan.park.y, plan.park.z)
    assert (park.spindle_rpm, park.coolant) == (0.0, False)
    assert isinstance(plan.steps[-1], ProgramEnd)


@settings(max_examples=150)
@given(plans)
def test_coolant_runs_only_when_configured(plan: WarmupPlan) -> None:
    flood = plan.profile.coolant is Coolant.FLOOD

    assert any(m.coolant for m in motions(plan)) == flood
    assert all(m.spindle_rpm > 0 for m in motions(plan) if m.coolant)


@settings(max_examples=150)
@given(plans)
def test_each_stage_fills_its_share_of_the_duration_or_warns(plan: WarmupPlan) -> None:
    budget = plan.stage_budget_seconds
    warned = {w.message.split(" runs ")[0] for w in plan.warnings}

    for stage in plan.stages:
        if stage.pass_seconds <= budget:
            assert budget - 1 < stage.seconds <= budget + 1e-6
        else:  # one pass alone overruns: run it once, with no dwell on top
            assert (stage.passes, stage.dwell_seconds) == (1, 0)
        assert (f"stage {stage.number}" in warned) == (stage.seconds > budget + 1)


@settings(max_examples=50)
@given(plans)
def test_trace_dwells_follow_the_stage_table(plan: WarmupPlan) -> None:
    dwells = [event.seconds for event in trace(plan) if isinstance(event, Dwell)]

    assert dwells == [stage.dwell_seconds for stage in plan.stages if stage.dwell_seconds]
