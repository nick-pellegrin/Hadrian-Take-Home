"""Round-trip verification: generated programs run on a simulated control match the plan."""

from dataclasses import replace

import pytest
from hypothesis import given, settings

from cnc_warmup.config import Catalog, apply_overrides
from cnc_warmup.model import Axis, AxisLimits, Controller
from cnc_warmup.plan import Dwell, Motion, Position, WarmupPlan, build_plan, trace
from cnc_warmup.posts import render
from cnc_warmup.verify import ReaderError, compare, run_program, verify
from cnc_warmup.verify.fanuc import read_fanuc
from cnc_warmup.verify.klartext import read_klartext
from cnc_warmup.verify.machine import SoftLimits
from strategies import plans

SHIPPED = [
    (machine, profile, controller)
    for machine in ("M1", "M2", "M3")
    for profile in ("daily", "extended")
    for controller in Controller
]
WIDE = {axis: AxisLimits(-5000, 5000) for axis in Axis}  # soft limits no program reaches


def plan_with(catalog: Catalog, **changes: object) -> WarmupPlan:
    return build_plan(catalog.machine("M1"), apply_overrides(catalog.profile("daily"), changes))


def own_limits(plan: WarmupPlan) -> dict[Axis, AxisLimits]:
    return {axis: plan.machine.limits(axis) for axis in Axis}


def klartext(*codes: str) -> str:
    blocks = ["BEGIN PGM T MM", *codes, "END PGM T MM"]
    return "".join(f"{number} {block}\r\n" for number, block in enumerate(blocks))


def fanuc_program(*lines: str) -> str:
    return "%\n" + "".join(f"{line}\n" for line in ("O1000", *lines)) + "%\n"


# --- Round trips -------------------------------------------------------------------------


@pytest.mark.parametrize(("machine_id", "profile_name", "controller"), SHIPPED)
def test_shipped_programs_do_exactly_what_the_plan_says(
    catalog: Catalog, machine_id: str, profile_name: str, controller: Controller
) -> None:
    plan = build_plan(catalog.machine(machine_id), catalog.profile(profile_name))

    assert verify(plan, render(plan, controller)) == []


@settings(max_examples=100)
@given(plans)
def test_any_plan_round_trips_through_both_posts(plan: WarmupPlan) -> None:
    for controller in Controller:
        assert verify(plan, render(plan, controller)) == []


# --- The travel check, run on a simulated control ------------------------------------------


@pytest.mark.parametrize("controller", list(Controller))
@pytest.mark.parametrize("axis", list(Axis))
@pytest.mark.parametrize("side", ["-", "+"])
def test_travel_check_stops_before_any_move_on_a_smaller_machine(
    catalog: Catalog, controller: Controller, axis: Axis, side: str
) -> None:
    plan = plan_with(catalog)
    limits = own_limits(plan)
    sweep = plan.envelope.limits(axis)
    limits[axis] = (
        replace(limits[axis], min=sweep.min + 0.5)
        if side == "-"
        else replace(limits[axis], max=sweep.max - 0.5)
    )

    run = run_program(render(plan, controller), limits)

    expected = {
        Controller.HEIDENHAIN: "FN 14: ERROR = 1004",
        Controller.FANUC: f"SWEEP EXCEEDS {axis.upper()}{side} LIMIT",
    }
    assert run.alarm == expected[controller]
    assert run.events == ()


@pytest.mark.parametrize("controller", list(Controller))
def test_travel_check_passes_when_the_limits_equal_the_sweep(
    catalog: Catalog, controller: Controller
) -> None:
    plan = plan_with(catalog)
    exact: SoftLimits = {axis: plan.envelope.limits(axis) for axis in Axis}

    run = run_program(render(plan, controller), exact)

    assert run.alarm is None
    assert compare(trace(plan), run.events) == []


@pytest.mark.parametrize("controller", list(Controller))
def test_without_the_guard_the_program_runs_on_any_limits(
    catalog: Catalog, controller: Controller
) -> None:
    plan = plan_with(catalog, runtime_guards=False)
    tiny = {axis: AxisLimits(0, 1) for axis in Axis}

    run = run_program(render(plan, controller), tiny)

    assert run.alarm is None
    assert run.events


# --- Tampered programs are caught ----------------------------------------------------------


@pytest.mark.parametrize(
    ("controller", "old", "new"),
    [
        (Controller.HEIDENHAIN, "L X-1 R0 FQL1 M91 ; X+", "L X-2 R0 FQL1 M91 ; X+"),
        (Controller.HEIDENHAIN, "QL1 = 4880", "QL1 = 4800"),
        (Controller.HEIDENHAIN, "CALL LBL 3 REP 3", "CALL LBL 3 REP 2"),
        (Controller.HEIDENHAIN, "CYCL DEF 9.1 DWELL 89", "CYCL DEF 9.1 DWELL 88"),
        (Controller.HEIDENHAIN, "M3 ; SPINDLE ON", "M5 ; SPINDLE ON"),
        (Controller.HEIDENHAIN, "TOOL CALL S3250", "TOOL CALL S3000"),
        (Controller.FANUC, "G91 G01 X760. F4880", "G91 G01 X760. F4800"),
        (Controller.FANUC, "N200 G90 G53", "N200 G91 G53"),  # G53 is ignored in G91
        (Controller.FANUC, "Y506. (Y+", "Y505. (Y+"),
        (Controller.FANUC, "G04 P89000", "G04 P8900"),
        (Controller.FANUC, "S3250 M03", "S3000 M03"),
    ],
)
def test_any_change_to_a_program_is_caught(
    catalog: Catalog, controller: Controller, old: str, new: str
) -> None:
    plan = plan_with(catalog)
    program = render(plan, controller)
    tampered = replace(program, text=program.text.replace(old, new, 1))
    assert tampered.text != program.text

    problems = verify(plan, tampered)

    assert problems
    assert problems[0].startswith(f"{program.filename}: ")


def test_a_program_that_alarms_on_its_own_machine_fails(catalog: Catalog) -> None:
    plan = plan_with(catalog)
    program = render(plan, Controller.HEIDENHAIN)
    too_strict = program.text.replace("IF +QL10 GT -761", "IF +QL10 GT -800")

    problems = verify(plan, replace(program, text=too_strict))

    assert problems[0] == (
        "WARMUP_M1_DAILY.H: stops with an alarm on its own machine: FN 14: ERROR = 1004"
    )


def test_coolant_state_is_part_of_the_comparison(catalog: Catalog) -> None:
    plan = plan_with(catalog, coolant="flood")
    program = render(plan, Controller.HEIDENHAIN)
    tampered = replace(program, text=program.text.replace("M8 ;", "M9 ;"))

    [problem, *_] = verify(plan, tampered)

    assert "coolant on, program does" in problem


@pytest.mark.parametrize(
    ("controller", "old", "new", "reason"),
    [
        (
            Controller.HEIDENHAIN,
            "L Z-1 R0 FMAX M91",
            "L Z-1 R0 FMAX",
            "not in the supported subset",
        ),
        (
            Controller.FANUC,
            "G90 G53 G00 X-761. Y-507. (TO",
            "G90 G00 X-761. Y-507. (TO",
            "move in work coordinates",
        ),
    ],
)
def test_unreadable_programs_are_reported(
    catalog: Catalog, controller: Controller, old: str, new: str, reason: str
) -> None:
    plan = plan_with(catalog)
    program = render(plan, controller)

    [problem] = verify(plan, replace(program, text=program.text.replace(old, new, 1)))

    assert "cannot read the program back" in problem
    assert reason in problem


# --- Klartext reader ---------------------------------------------------------------------------


def moves(events: tuple[Motion | Dwell, ...]) -> list[Position]:
    return [event.position for event in events if isinstance(event, Motion)]


def test_klartext_section_repeat_runs_rep_more_times() -> None:
    text = klartext("LBL 1", "L X+1 R0 F100 M91", "L X+2 R0 F100 M91", "CALL LBL 1 REP 2", "M30")

    assert len(moves(read_klartext(text, WIDE).events)) == 6


def test_klartext_subprogram_call_returns_after_lbl_0() -> None:
    text = klartext(
        'CALL LBL "SUB"', 'CALL LBL "SUB"', "M30", 'LBL "SUB"', "L X+1 R0 F100 M91", "LBL 0"
    )

    assert moves(read_klartext(text, WIDE).events) == [Position(x=1), Position(x=1)]


@pytest.mark.parametrize(
    ("codes", "reason"),
    [
        (("L X+0 R0 FQL1 M91",), "QL1 is read before it is set"),
        (("FN 12: IF +QL5 LT +0 GOTO LBL 1",), "QL5 is read before it is set"),
        (('CALL LBL "NOWHERE"',), "jump to undefined label"),
        (("LBL 0",), "LBL 0 reached outside a subprogram"),
        (("M4",), "unsupported miscellaneous function M4"),
        (("L X+0 R0 FMAX M91",), "reached END PGM without M30"),
        (("FN 11: IF +1 LT +2 GOTO LBL 1", "LBL 1", "M30"), "FN 11 does not compare with LT"),
        (("TOOL CALL 5 Z S1000",), "not in the supported subset"),
    ],
)
def test_klartext_reader_rejects_what_it_cannot_follow(codes: tuple[str, ...], reason: str) -> None:
    with pytest.raises(ReaderError, match=reason):
        read_klartext(klartext(*codes), WIDE)


def test_klartext_reader_stops_an_endless_program() -> None:
    text = klartext("LBL 1", "FN 12: IF +0 LT +1 GOTO LBL 1")

    with pytest.raises(ReaderError, match="does not end"):
        read_klartext(text, WIDE, max_blocks=100)


def test_klartext_reader_checks_block_numbers() -> None:
    with pytest.raises(ReaderError, match="bad block number"):
        read_klartext("0 BEGIN PGM T MM\r\n2 M30\r\n", WIDE)


def test_klartext_reader_reports_running_off_the_end() -> None:
    with pytest.raises(ReaderError, match="ran past the last block"):
        read_klartext("0 BEGIN PGM T MM\r\n1 L X+0 R0 FMAX M91\r\n", WIDE)


# --- Fanuc reader --------------------------------------------------------------------------------


def test_fanuc_g53_moves_at_rapid_even_in_g01_mode() -> None:
    text = fanuc_program("G90 G53 G00 X0. Y0. Z0.", "G91 G01 X10. F500", "G90 G53 X5.", "M30")

    last = read_fanuc(text, WIDE).events[-1]

    assert last == Motion(Position(5, 0, 0), None, 0.0, False)


def test_fanuc_g53_in_g91_is_ignored_like_on_the_control() -> None:
    text = fanuc_program("G90 G53 G00 X10. Y0. Z0.", "G91 G53 G00 X5.", "M30")

    last = read_fanuc(text, WIDE).events[-1]

    assert isinstance(last, Motion)
    assert last.position == Position(15, 0, 0)  # an increment, not machine X5


@pytest.mark.parametrize(
    ("lines", "reason"),
    [
        (("G91 G01 X10. F100", "M30"), "incremental X move from an unknown position"),
        (("G90 G53 G00 X0.", "G91 G01 X10.", "M30"), "without a motion mode or feed rate"),
        (("G90 G00 X0.", "M30"), "move in work coordinates"),
        (("G04 X5.", "M30"), "dwell must be G04 P"),
        (("T1 M06",), "unsupported word T1"),
        (("G43 H1",), "unsupported word G43"),
        (("X10..",), "not in the supported subset"),
        (("G90 G53 G00 Z0.",), "without M30"),
    ],
)
def test_fanuc_reader_rejects_what_it_cannot_follow(lines: tuple[str, ...], reason: str) -> None:
    with pytest.raises(ReaderError, match=reason):
        read_fanuc(fanuc_program(*lines), WIDE)


def test_fanuc_reader_needs_the_percent_lines() -> None:
    with pytest.raises(ReaderError, match="wrapped in % lines"):
        read_fanuc("O1000\nM30\n", WIDE)


def test_fanuc_reader_stops_an_endless_program() -> None:
    with pytest.raises(ReaderError, match="does not end"):
        read_fanuc(fanuc_program("G21", "G90", "M30"), WIDE, max_blocks=2)


# --- Comparison --------------------------------------------------------------------------------


def test_compare_ignores_moves_that_go_nowhere() -> None:
    corner = Motion(Position(0, 0, 0), None, 0.0, False)
    again = Motion(Position(0, 0, 0), None, 1000.0, True)  # state differs, but nothing moves

    assert compare([corner], [corner, again]) == []


def test_compare_reports_a_different_number_of_steps() -> None:
    move = Motion(Position(1, 0, 0), 100.0, 1000.0, False)

    assert compare([move], [move, Dwell(5, 1000.0)]) == [
        "expected 1 moves and dwells, the program has 2"
    ]


def test_compare_reports_at_most_five_differences() -> None:
    expected = [Dwell(float(seconds), 0.0) for seconds in range(1, 11)]
    actual = [Dwell(float(seconds) + 1, 0.0) for seconds in range(1, 11)]

    assert len(compare(expected, actual)) == 5
