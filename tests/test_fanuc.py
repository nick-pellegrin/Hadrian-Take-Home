"""Fanuc 31i post-processor."""

import re
from dataclasses import replace
from pathlib import Path

import pytest
from hypothesis import given, settings

from cnc_warmup.config import Catalog, ConfigError, apply_overrides
from cnc_warmup.model import Axis, Controller, Coolant, FanucCancelCode, FanucSettings
from cnc_warmup.plan import WarmupPlan, build_plan
from cnc_warmup.posts import fanuc
from cnc_warmup.posts.base import Program
from strategies import plans

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples" / "fanuc"
SHIPPED = [
    (machine, profile) for machine in ("M1", "M2", "M3") for profile in ("daily", "extended")
]

_COMMENT = re.compile(r"\([^()]*\)")
_AXIS_WORD = re.compile(r"[XYZ][-+]?[\d.]+")


def plan_with(catalog: Catalog, machine_id: str = "M1", **changes: object) -> WarmupPlan:
    profile = apply_overrides(catalog.profile("daily"), changes)
    return build_plan(catalog.machine(machine_id), profile)


def code_lines(program: Program) -> list[str]:
    """Blocks between the % lines, with comments removed and comment-only blocks dropped."""
    codes = (_COMMENT.sub("", line).strip() for line in program.text.split("\n")[1:-2])
    return [code for code in codes if code]


def fanuc_problems(program: Program, plan: WarmupPlan) -> list[str]:
    """Rules every generated Fanuc program must follow, checked by simulating its moves."""
    problems = []
    text = program.text
    if "\r" in text:
        problems.append("line endings must be LF")
    if not text.isascii():
        problems.append("program must be ASCII")
    if not text.startswith("%\n") or not text.endswith("\n%\n"):
        problems.append("program must be wrapped in % lines")
        return problems

    lines = text.split("\n")[1:-2]
    if not re.fullmatch(r"O\d{4} \([A-Z0-9 _-]+\)", lines[0]):
        problems.append(f"first block must be the O-number: {lines[0]!r}")

    absolute, motion, feed, spindle = True, "", None, False
    position: dict[str, float | None] = {"X": None, "Y": None, "Z": None}
    sequence_numbers: set[str] = set()
    for number, line in enumerate(lines, start=2):
        where = f"line {number}: {line!r}"
        if any(char.islower() for char in line):
            problems.append(f"{where}: lower case")
        code = _COMMENT.sub("", line)
        if "(" in code or ")" in code:
            problems.append(f"{where}: unbalanced or nested parentheses")
            continue
        if code.startswith("IF "):  # travel check: tested separately
            continue
        words = code.split()
        codes = {word for word in words if word[0] == "G"}
        absolute = "G91" not in codes and ("G90" in codes or absolute)
        motion = next((word for word in words if word in ("G00", "G01")), motion)
        feed = next((word for word in words if word[0] == "F"), feed)
        spindle = "M03" in words or (spindle and "M05" not in words)

        for word in words:
            if word[0] == "N":
                if word in sequence_numbers:
                    problems.append(f"{where}: sequence number used twice")
                sequence_numbers.add(word)
            if word[0] == "S" and not re.fullmatch(r"S\d+", word):
                problems.append(f"{where}: spindle speed must be whole rpm")
        if "G28" in codes:
            problems.append(f"{where}: G28 depends on where the reference point is")
        if "G04" in codes and not re.fullmatch(r"G04 P\d+", code.strip()):
            problems.append(f"{where}: dwell must be G04 P<milliseconds>")

        axis_words = [word for word in words if _AXIS_WORD.fullmatch(word)]
        if any("." not in word for word in axis_words):
            problems.append(f"{where}: axis word without decimal point")
        if not axis_words or "G50.1" in codes:  # G50.1 X0. Y0. Z0. names axes, no motion
            continue
        if "G53" in codes:
            if "G90" not in codes:
                problems.append(f"{where}: G53 must be written with G90 (ignored in G91)")
            for word in axis_words:
                position[word[0]] = float(word[1:])
        elif absolute:
            problems.append(f"{where}: move in work coordinates")
        else:
            for word in axis_words:
                start = position[word[0]]
                if start is None:
                    problems.append(f"{where}: incremental move from an unknown position")
                    continue
                position[word[0]] = start + float(word[1:])
            if motion == "G01" and (feed is None or not spindle):
                problems.append(f"{where}: feed move without a feed or a running spindle")
        for axis in Axis:
            value = position[axis.upper()]
            limits = plan.envelope.limits(axis)
            if value is not None and not limits.min - 1e-6 <= value <= limits.max + 1e-6:
                problems.append(f"{where}: {axis.upper()}{value:g} outside the sweep envelope")
    return problems


# --- The committed examples ---------------------------------------------------------


@pytest.mark.parametrize(("machine_id", "profile_name"), SHIPPED)
def test_example_programs_are_up_to_date(
    catalog: Catalog, update_golden: bool, machine_id: str, profile_name: str
) -> None:
    plan = build_plan(catalog.machine(machine_id), catalog.profile(profile_name))
    program = fanuc.render(plan)
    path = EXAMPLES_DIR / program.filename

    if update_golden:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(program.data)

    assert path.read_bytes() == program.data, "outdated: run `uv run pytest --update-golden`"
    assert fanuc_problems(program, plan) == []


@settings(max_examples=100)
@given(plans)
def test_any_plan_renders_a_well_formed_program(plan: WarmupPlan) -> None:
    assert fanuc_problems(fanuc.render(plan), plan) == []


@pytest.mark.parametrize(
    ("old", "new", "problem"),
    [
        ("Y506. (Y+", "Y506 (Y+", "axis word without decimal point"),
        ("G90 G53 G00 Z-1.", "G91 G53 G00 Z-1.", "G53 must be written with G90"),
        ("G90 G53 G00 X-761. Y-507. (TO", "G90 G00 X-761. Y-507. (TO", "move in work coordinates"),
        ("Z-498. (Z-", "Z-499. (Z-", "outside the sweep envelope"),
        ("(SPINDLE OFF)", "(spindle off)", "lower case"),
        ("(SPINDLE OFF)", "(SPINDLE (OFF)", "unbalanced or nested parentheses"),
        ("\n", "\r\n", "line endings must be LF"),
        ("G04 P89000", "G04 X89.", "dwell must be G04 P<milliseconds>"),
        ("S1000 M03 (SPINDLE ON", "S1000.5 M03 (SPINDLE ON", "spindle speed must be whole rpm"),
        ("N200 ", "N100 ", "sequence number used twice"),
        ("G90 G53 G00 Z-1. (RETRACT Z)", "G91 G28 Z0. (RETRACT Z)", "G28 depends on"),
        ("S1000 M03", "S1000 M05", "without a feed or a running spindle"),
        ("O8001 (", "O801 (", "first block must be the O-number"),
        ("M30 (END OF PROGRAM)\n%\n", "M30 (END OF PROGRAM)\n", "wrapped in % lines"),
    ],
)
def test_linter_catches_broken_programs(catalog: Catalog, old: str, new: str, problem: str) -> None:
    plan = plan_with(catalog)
    program = fanuc.render(plan)
    broken = replace(program, text=program.text.replace(old, new))  # every occurrence

    assert any(problem in found for found in fanuc_problems(broken, plan))


# --- Behaviour ---------------------------------------------------------------------------


def test_program_is_named_by_its_o_number(catalog: Catalog) -> None:
    program = fanuc.render(plan_with(catalog, "M2"))

    assert (program.name, program.filename) == ("O8002", "O8002_M2_DAILY.nc")
    assert program.controller is Controller.FANUC
    assert program.text.startswith("%\nO8002 (WARMUP M2 DAILY)\n")


def test_machine_without_fanuc_settings_is_rejected(catalog: Catalog) -> None:
    machine = replace(catalog.machine("M1"), fanuc=None)

    with pytest.raises(ConfigError, match=r"machines\.M1\.fanuc: missing"):
        fanuc.render(build_plan(machine, catalog.profile("daily")))


def test_safe_start_sets_units_and_cancels_modal_state(catalog: Catalog) -> None:
    codes = code_lines(fanuc.render(plan_with(catalog)))

    assert codes[1:6] == ["G21", "G17 G40 G49 G80 G90 G94", "G69", "M05", "M09"]


def test_machine_opts_in_to_each_cancel_code(catalog: Catalog) -> None:
    every_code = FanucSettings(8001, tuple(FanucCancelCode))
    machine = replace(catalog.machine("M1"), fanuc=every_code)
    codes = code_lines(fanuc.render(build_plan(machine, catalog.profile("daily"))))

    assert codes[3:7] == ["G15", "G50", "G50.1 X0. Y0. Z0.", "G69"]


def test_travel_check_reads_the_stroke_limits(catalog: Catalog) -> None:
    codes = code_lines(fanuc.render(plan_with(catalog)))
    checks = [code for code in codes if code.startswith("IF ")]

    assert len(checks) == 6
    assert checks[:2] == [
        "IF [PRM[1321]/[1] GT -761.] THEN #3000=1",
        "IF [PRM[1320]/[1] LT -1.] THEN #3000=1",
    ]


def test_guards_and_stop_can_be_left_out(catalog: Catalog) -> None:
    program = fanuc.render(plan_with(catalog, runtime_guards=False, operator_confirm=False))

    assert "PRM[" not in program.text
    assert "M00" not in code_lines(program)


def test_positioning_uses_machine_coordinates_z_first(catalog: Catalog) -> None:
    codes = code_lines(fanuc.render(plan_with(catalog)))
    first_move = next(i for i, code in enumerate(codes) if "G53" in code)

    assert codes[first_move : first_move + 3] == [
        "G90 G53 G00 Z-1.",
        "G90 G53 G00 X-761. Y-507.",
        "S1000 M03",
    ]
    assert codes.index("M00") < first_move


def test_stage_reanchors_restates_speed_and_sweeps_incrementally(catalog: Catalog) -> None:
    program = fanuc.render(plan_with(catalog))
    lines = program.text.split("\n")
    stage_2 = next(i for i, line in enumerate(lines) if line.startswith("N200 "))

    assert lines[stage_2 : stage_2 + 4] == [
        "N200 G90 G53 G00 X-761. Y-507. Z-1. (AT THE SWEEP START CORNER)",
        "S3250 M03 (SPINDLE SPEED FOR THIS STAGE)",
        "(PASS 1 OF 3)",
        "G91 G01 X760. F4880 (X+ FULL STROKE)",
    ]
    assert "(PASS 3 OF 3)" in lines
    assert "G04 P8000 (DWELL 8 S, SPINDLE ON)" in lines


def test_flood_coolant_is_restated_by_every_stage(catalog: Catalog) -> None:
    codes = code_lines(fanuc.render(plan_with(catalog, coolant=Coolant.FLOOD)))

    assert codes.count("M08") == 1 + 5  # at spindle start, then once per stage
    end = codes.index("M30")
    assert codes[end - 4 : end - 2] == ["M09", "M05"]


def test_final_rapid_pass_moves_incrementally_at_rapid(catalog: Catalog) -> None:
    codes = code_lines(fanuc.render(plan_with(catalog, final_rapid_pass=True)))
    rapid = codes.index("G91 G00 X760.")

    assert codes[rapid - 1] == "G90 G53 G00 X-761. Y-507. Z-1."


def test_single_pass_stage_without_dwell_has_no_pass_labels_or_dwell(catalog: Catalog) -> None:
    # 5 minutes per stage at 800 mm/min: stage 1 is one pass that overruns, so no dwell.
    program = fanuc.render(plan_with(catalog, feed_start=800, duration_min=10, stages=2))
    lines = program.text.split("\n")
    stage_1 = lines.index("N100 G90 G53 G00 X-761. Y-507. Z-1. (AT THE SWEEP START CORNER)")
    stage_2 = next(i for i, line in enumerate(lines) if line.startswith("N200 "))

    assert not any(line.startswith(("(PASS", "G04")) for line in lines[stage_1:stage_2])


def test_speeds_are_whole_rpm_and_coordinates_keep_their_decimals(catalog: Catalog) -> None:
    plan = plan_with(catalog, rpm_start=1234.6, edge_margin_mm=1.5)
    codes = code_lines(fanuc.render(plan))

    assert "S1235 M03" in codes
    assert "G90 G53 G00 X-760.5 Y-506.5" in codes


def test_comments_are_sanitised_for_fanuc(catalog: Catalog) -> None:
    machine = replace(catalog.machine("M1"), description="Mill (bay 2): 100% ; #1 résumé")
    program = fanuc.render(build_plan(machine, catalog.profile("daily")))

    assert "(WARM-UP FOR MACHINE M1 - MILL BAY 2 - 100 PERCENT , 1 RESUME)" in program.text
