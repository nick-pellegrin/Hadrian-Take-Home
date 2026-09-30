"""Heidenhain TNC 640 Klartext post-processor."""

import re
from dataclasses import replace
from pathlib import Path

import pytest
from hypothesis import given, settings

from cnc_warmup import posts
from cnc_warmup.config import Catalog, apply_overrides
from cnc_warmup.model import Axis, Controller, Coolant
from cnc_warmup.plan import WarmupPlan, build_plan
from cnc_warmup.posts import heidenhain
from cnc_warmup.posts.base import Program
from strategies import plans

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples" / "heidenhain"
SHIPPED = [
    (machine, profile) for machine in ("M1", "M2", "M3") for profile in ("daily", "extended")
]

_BLOCK = re.compile(r"(\d+) (.*)")
_MOVE = re.compile(r"L(?: [XYZ][+-]\d+(?:\.\d{1,3})?)+ R0 (?:FMAX|FQL1) M91")
_COORDINATE = re.compile(r"([XYZ])([+-]\d+(?:\.\d+)?)")
_LABEL_DEFINITION = re.compile(r'LBL ("\w+"|\d+)')
_LABEL_REFERENCE = re.compile(r'(?:CALL|GOTO) LBL ("\w+"|\d+)')


def plan_with(catalog: Catalog, machine_id: str = "M1", **changes: object) -> WarmupPlan:
    profile = apply_overrides(catalog.profile("daily"), changes)
    return build_plan(catalog.machine(machine_id), profile)


def blocks(program: Program) -> list[str]:
    """Block contents without block numbers (checked by klartext_problems)."""
    return [line.split(" ", 1)[1] for line in program.text.split("\r\n")[:-1]]


def code_blocks(program: Program) -> list[str]:
    """Blocks with their trailing comments removed, comment-only blocks dropped."""
    codes = (block.partition(" ; ")[0] for block in blocks(program))
    return [code for code in codes if not code.startswith((";", "*"))]


def klartext_problems(program: Program, plan: WarmupPlan) -> list[str]:
    """Structural rules every generated Klartext program must follow."""
    problems = []
    text = program.text
    if not text.endswith("\r\n") or "\n" in text.replace("\r\n", ""):
        problems.append("every block must end with CRLF")
    if not text.isascii():
        problems.append("program must be ASCII")

    lines = text.split("\r\n")[:-1]
    codes: list[str] = []
    for index, line in enumerate(lines):
        match = _BLOCK.fullmatch(line)
        if match is None or int(match.group(1)) != index:
            problems.append(f"block {index}: numbering broken: {line!r}")
            continue
        code, _, comment = match.group(2).partition(" ; ")
        if code.startswith(";"):
            code, comment = "", code[1:]
        if ";" in comment or comment.rstrip().endswith("~"):
            problems.append(f"block {index}: comment breaks Klartext: {line!r}")
        if code.startswith("L ") and not _MOVE.fullmatch(code):
            problems.append(f"block {index}: move is not an M91 R0 line: {line!r}")
        for axis, value in _COORDINATE.findall(code if code.startswith("L ") else ""):
            limits = plan.envelope.limits(Axis(axis.lower()))
            if not limits.min - 1e-9 <= float(value) <= limits.max + 1e-9:
                problems.append(f"block {index}: {axis}{value} outside the sweep envelope")
        codes.append(code)
    if len(codes) < len(lines):
        return problems  # block structure is broken; later checks would only add noise

    name = program.filename.removesuffix(".H")
    if codes[0] != f"BEGIN PGM {name} MM" or codes[-1] != f"END PGM {name} MM":
        problems.append("BEGIN/END PGM must name the file")
    defined = [label for code in codes if (label := _match(_LABEL_DEFINITION, code))]
    if len(defined) != len(set(defined)):
        problems.append(f"labels defined twice: {defined}")
    referenced = {label for code in codes for label in _LABEL_REFERENCE.findall(code)}
    if not referenced <= set(defined):
        problems.append(f"undefined labels: {referenced - set(defined)}")
    if 'LBL "SWEEP"' not in codes or "M30" not in codes:
        problems.append('missing LBL "SWEEP" or M30')
    elif codes.index('LBL "SWEEP"') < codes.index("M30"):
        problems.append("subprograms must follow M30")
    return problems


def _match(pattern: re.Pattern[str], code: str) -> str | None:
    match = pattern.fullmatch(code.split(" ; ")[0])
    return match.group(1) if match and match.group(1) != "0" else None


# --- The committed examples ---------------------------------------------------------


@pytest.mark.parametrize(("machine_id", "profile_name"), SHIPPED)
def test_example_programs_are_up_to_date(
    catalog: Catalog, update_golden: bool, machine_id: str, profile_name: str
) -> None:
    plan = build_plan(catalog.machine(machine_id), catalog.profile(profile_name))
    program = heidenhain.render(plan)
    path = EXAMPLES_DIR / program.filename

    if update_golden:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(program.data)

    assert path.read_bytes() == program.data, "outdated: run `uv run pytest --update-golden`"
    assert klartext_problems(program, plan) == []


@settings(max_examples=100)
@given(plans)
def test_any_plan_renders_a_well_formed_program(plan: WarmupPlan) -> None:
    assert klartext_problems(heidenhain.render(plan), plan) == []


@pytest.mark.parametrize(
    ("old", "new", "problem"),
    [
        ("L Z-1 R0 FMAX M91", "L Z-1 FMAX M91", "move is not an M91 R0 line"),
        ("L X-1 R0 FQL1 M91", "L X-1 R0 FQL1", "move is not an M91 R0 line"),
        ("L Z-499 R0", "L Z-500 R0", "Z-500 outside the sweep envelope"),
        ("; SPINDLE OFF", "; SPINDLE; OFF", "comment breaks Klartext"),
        ("\r\n5 ", "\r\n6 ", "numbering broken"),
        ("\r\n", "\n", "every block must end with CRLF"),
        ('GOTO LBL "TRAVEL_ERR"', 'GOTO LBL "TRAVEL_ERRX"', "undefined labels"),
        ("LBL 3 ;", "LBL 2 ;", "labels defined twice"),
        ("END PGM WARMUP_M1_DAILY", "END PGM WARMUP_M1", "BEGIN/END PGM must name the file"),
        ("M30 ; END OF PROGRAM", "M2 ; END OF PROGRAM", 'missing LBL "SWEEP" or M30'),
    ],
)
def test_linter_catches_broken_programs(catalog: Catalog, old: str, new: str, problem: str) -> None:
    plan = plan_with(catalog)
    program = heidenhain.render(plan)
    broken = replace(program, text=program.text.replace(old, new, 1))

    assert any(problem in found for found in klartext_problems(broken, plan))


# --- Behaviour ---------------------------------------------------------------------------


def test_program_is_named_after_machine_and_profile(catalog: Catalog) -> None:
    program = heidenhain.render(plan_with(catalog, "M2"))

    assert (program.name, program.filename) == ("WARMUP_M2_DAILY", "WARMUP_M2_DAILY.H")
    assert program.controller is Controller.HEIDENHAIN


def test_program_starts_by_stopping_everything_then_retracting_z(catalog: Catalog) -> None:
    codes = code_blocks(heidenhain.render(plan_with(catalog)))
    first_move = next(i for i, code in enumerate(codes) if code.startswith("L "))

    assert codes[first_move] == "L Z-1 R0 FMAX M91"
    assert {"M5", "M9", "STOP"} <= set(codes[:first_move])
    assert codes.index("M3") > first_move


def test_travel_check_compares_each_soft_limit_with_the_sweep(catalog: Catalog) -> None:
    codes = code_blocks(heidenhain.render(plan_with(catalog)))
    checks = [code for code in codes if code.startswith("FN 1")]

    assert checks[:4] == [
        "FN 18: SYSREAD QL10 = ID230 NR2 IDX1",
        'FN 11: IF +QL10 GT -761 GOTO LBL "TRAVEL_ERR"',
        "FN 18: SYSREAD QL11 = ID230 NR3 IDX1",
        'FN 12: IF +QL11 LT -1 GOTO LBL "TRAVEL_ERR"',
    ]
    assert codes[-3:] == ['LBL "TRAVEL_ERR"', "FN 14: ERROR = 1004", "END PGM WARMUP_M1_DAILY MM"]


def test_guards_and_stop_can_be_left_out(catalog: Catalog) -> None:
    codes = code_blocks(
        heidenhain.render(plan_with(catalog, runtime_guards=False, operator_confirm=False))
    )

    assert not [code for code in codes if code.startswith(("FN", "STOP")) or "TRAVEL" in code]


def test_stage_changes_speed_sets_feed_and_repeats_the_sweep(catalog: Catalog) -> None:
    codes = code_blocks(heidenhain.render(plan_with(catalog)))
    stage_2 = codes.index("TOOL CALL S3250")

    assert codes[stage_2 : stage_2 + 7] == [
        "TOOL CALL S3250",
        "QL1 = 4880",
        "LBL 2",
        'CALL LBL "SWEEP"',
        "CALL LBL 2 REP 2",
        "CYCL DEF 9.0 DWELL TIME",
        "CYCL DEF 9.1 DWELL 8",
    ]


def test_first_stage_does_not_repeat_the_spindle_on_speed(catalog: Catalog) -> None:
    codes = code_blocks(heidenhain.render(plan_with(catalog)))

    assert codes.count("TOOL CALL S1000") == 1
    assert codes[codes.index("M3") + 1 : codes.index("M3") + 3] == [
        "QL1 = 2500",
        'CALL LBL "SWEEP"',
    ]


def test_flood_coolant_is_switched_with_the_spindle(catalog: Catalog) -> None:
    codes = code_blocks(heidenhain.render(plan_with(catalog, coolant=Coolant.FLOOD)))

    assert codes[codes.index("M3") + 1] == "M8"
    end = codes.index("M30")
    assert codes[end - 4 : end] == ["M9", "M5", "L Z-1 R0 FMAX M91", "L X-381 Y-254 R0 FMAX M91"]


def test_final_rapid_pass_sweeps_at_fmax(catalog: Catalog) -> None:
    codes = code_blocks(heidenhain.render(plan_with(catalog, final_rapid_pass=True)))
    last_dwell = max(i for i, code in enumerate(codes) if code.startswith("CYCL DEF 9.1"))

    assert codes[last_dwell + 1 : last_dwell + 3] == ["L X-1 R0 FMAX M91", "L Y-1 R0 FMAX M91"]


def test_single_pass_stage_without_dwell_is_a_plain_call(catalog: Catalog) -> None:
    # 5 minutes per stage at 800 mm/min: stage 1 is one pass that overruns, so no dwell.
    plan = plan_with(catalog, feed_start=800, duration_min=10, stages=2)
    codes = code_blocks(heidenhain.render(plan))
    stage_1 = codes.index("QL1 = 800")

    assert codes[stage_1 + 1 : stage_1 + 3] == ['CALL LBL "SWEEP"', "TOOL CALL S10000"]


def test_comments_are_sanitised_for_klartext(catalog: Catalog) -> None:
    machine = replace(catalog.machine("M1"), description="Café; \"fast\" ~ 'mill'")
    program = heidenhain.render(build_plan(machine, catalog.profile("daily")))

    assert "; WARM-UP FOR MACHINE M1 - CAFE, FAST - MILL" in program.text


@pytest.mark.parametrize("controller", list(Controller))
def test_registry_renders_every_controller(catalog: Catalog, controller: Controller) -> None:
    program = posts.render(plan_with(catalog), controller)

    assert program.controller is controller
