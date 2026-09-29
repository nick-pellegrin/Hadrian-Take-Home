"""Heidenhain TNC 640 post-processor: renders a plan as a Klartext (.H) program.

Syntax references are to the TNC 640 Klartext Programming User's Manual,
NC SW 34059x-11. docs/DESIGN.md tracks which constructs are verified in the
programming station. Program layout:

    BEGIN PGM ... MM, header comments with the stage table
    main program: safe start, travel check, operator STOP, positioning,
        stages (TOOL CALL S, QL1 = feed, CALL LBL "SWEEP" [with REP], dwell), shutdown, M30
    LBL "SWEEP": one full-travel pass at feed QL1, written once and called by every stage
    LBL "TRAVEL_ERR": FN 14 error, reached only if the travel check fails
    END PGM ... MM

Every move is an L block with R0 and M91. M91 positions in machine
coordinates, ignoring presets, datum shifts and tool length (manual §7.3). R0
because radius compensation would otherwise still apply.
"""

import textwrap
from typing import assert_never

from cnc_warmup.model import Axis, Controller
from cnc_warmup.plan import (
    CoolantOff,
    CoolantOn,
    Envelope,
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
    Stage,
    WarmupPlan,
    format_number,
)
from cnc_warmup.posts.base import Program, ascii_upper, describe, stage_table

# Stage feed handed to the sweep subprogram. QL parameters are local to this program,
# so they can't clash with Q parameters used by OEM or HEIDENHAIN cycles (manual §9).
FEED_PARAM = "QL1"
SWEEP_LABEL = '"SWEEP"'
TRAVEL_ERROR_LABEL = '"TRAVEL_ERR"'
RANGE_EXCEEDED = 1004  # FN 14 error text predefined by HEIDENHAIN: "Range exceeded"

_COMMENT_WIDTH = 72
# ';' would start a nested comment, '~' continues a block, and quotes delimit labels.
_COMMENT_FIXES = str.maketrans({";": ",", "~": "-", '"': "", "'": ""})


def program_name(plan: WarmupPlan) -> str:
    return f"WARMUP_{plan.machine.id}_{plan.profile.name}".upper()


def render(plan: WarmupPlan) -> Program:
    name = program_name(plan)
    out = _Blocks()
    out.add(f"BEGIN PGM {name} MM")
    for line in describe(plan):
        out.comment(line)
    for row in stage_table(plan):
        out.comment(row, wrap=False)

    rpm: float | None = None  # programmed spindle speed, to skip redundant speed changes
    for step in plan.steps:
        match step:
            case Section(title=title):
                out.heading(title)
            case SafeStart():
                out.comment(
                    "Nothing to reset: every move uses M91, which ignores presets, datum "
                    "shifts, coordinate transformations and tool length."
                )
            case SpindleStop():
                out.add("M5", "spindle off")
            case CoolantOff():
                out.add("M9", "coolant off")
            case CoolantOn():
                out.add("M8", "flood coolant on")
            case RuntimeGuard(envelope=envelope):
                _travel_check(out, envelope)
            case OperatorStop(checklist=checklist):
                out.comment("Before NC start, confirm:")
                for item in checklist:
                    out.comment(f"- {item}")
                out.add("STOP", "press NC start to run the warm-up")
            case RapidMove(target=target, label=label):
                out.add(f"L {_axes(target)} R0 FMAX M91", label)
            case SpindleOn(rpm=speed):
                out.add(f"TOOL CALL S{format_number(speed)}", "speed only - no tool change")
                out.add("M3", "spindle on clockwise")
                rpm = speed
            case RunStage(stage=stage):
                if stage.rpm != rpm:
                    out.add(f"TOOL CALL S{format_number(stage.rpm)}", "speed only - no tool change")
                    rpm = stage.rpm
                _stage(out, stage)
            case RapidSweep():
                _sweep(out, plan, "FMAX")
            case ProgramEnd():
                out.add("M30", "end of program")
            case _:
                assert_never(step)

    out.heading(f"Subprogram: one full-travel sweep pass at feed {FEED_PARAM}")
    out.add(f"LBL {SWEEP_LABEL}")
    _sweep(out, plan, f"F{FEED_PARAM}")
    out.add("LBL 0", "end of subprogram")
    if any(isinstance(step, RuntimeGuard) for step in plan.steps):
        _travel_error(out)
    out.add(f"END PGM {name} MM")
    return Program(Controller.HEIDENHAIN, name, f"{name}.H", out.text())


def _stage(out: "_Blocks", stage: Stage) -> None:
    out.add(f"{FEED_PARAM} = {format_number(stage.feed)}", "sweep feed for this stage, mm/min")
    if stage.passes == 1:
        out.add(f"CALL LBL {SWEEP_LABEL}", "1 pass")
    else:
        # Program section repeat: the section runs once, then REP more times (manual §8.3).
        # Stage numbers are unique and never 0, so they serve as the section labels.
        out.add(f"LBL {stage.number}", "repeat from here")
        out.add(f"CALL LBL {SWEEP_LABEL}")
        out.add(
            f"CALL LBL {stage.number} REP {stage.passes - 1}", f"{stage.passes} passes in total"
        )
    if stage.dwell_seconds:
        out.add("CYCL DEF 9.0 DWELL TIME")
        out.add(f"CYCL DEF 9.1 DWELL {format_number(stage.dwell_seconds)}", "spindle keeps turning")


def _sweep(out: "_Blocks", plan: WarmupPlan, feed: str) -> None:
    """One sweep pass from the start corner, writing only the axes that change."""
    here = plan.envelope.start
    for segment in plan.sweep:
        out.add(f"L {_changed_axes(here, segment.target)} R0 {feed} M91", segment.label)
        here = segment.target


def _travel_check(out: "_Blocks", envelope: Envelope) -> None:
    """Read the soft limits into QL10-QL15 and jump to the error label if any sits inside the sweep.

    FN 18 ID230 NR2/NR3 are the negative/positive software limit switches, index 1-3 = X, Y, Z,
    always in mm. QL values stay visible in the Q parameter status display for diagnosis.
    """
    for index, axis in enumerate(Axis, start=1):
        limits = envelope.limits(axis)
        low, high = f"QL{8 + 2 * index}", f"QL{9 + 2 * index}"
        name = axis.upper()
        out.add(f"FN 18: SYSREAD {low} = ID230 NR2 IDX{index}", f"{name} negative soft limit")
        out.add(
            f"FN 11: IF +{low} GT {_signed(limits.min)} GOTO LBL {TRAVEL_ERROR_LABEL}",
            f"limit inside the {name} sweep",
        )
        out.add(f"FN 18: SYSREAD {high} = ID230 NR3 IDX{index}", f"{name} positive soft limit")
        out.add(
            f"FN 12: IF +{high} LT {_signed(limits.max)} GOTO LBL {TRAVEL_ERROR_LABEL}",
            f"limit inside the {name} sweep",
        )


def _travel_error(out: "_Blocks") -> None:
    out.heading("Travel check failed")
    out.add(f"LBL {TRAVEL_ERROR_LABEL}")
    out.comment(
        "The sweep reaches past a soft limit of this machine, so this program was made for "
        "a different or larger machine. QL10-QL15 hold the limits read from the control. "
        "Regenerate the program for this machine."
    )
    out.add(f"FN 14: ERROR = {RANGE_EXCEEDED}", "range exceeded - stops the program")


def _axes(target: Position) -> str:
    words = [
        f"{axis.upper()}{_signed(value)}"
        for axis, value in ((Axis.X, target.x), (Axis.Y, target.y), (Axis.Z, target.z))
        if value is not None
    ]
    return " ".join(words)


def _changed_axes(here: Point, target: Point) -> str:
    return _axes(
        Position(
            None if target.x == here.x else target.x,
            None if target.y == here.y else target.y,
            None if target.z == here.z else target.z,
        )
    )


def _signed(value: float) -> str:
    """Klartext coordinates carry an explicit sign: X+762, X-761.5."""
    text = format_number(value)
    return text if text.startswith("-") else f"+{text}"


class _Blocks:
    """Collects Klartext blocks and numbers them from 0."""

    def __init__(self) -> None:
        self._blocks: list[str] = []

    def add(self, code: str, comment: str = "") -> None:
        self._blocks.append(f"{code} ; {_comment(comment)}" if comment else code)

    def comment(self, text: str, *, wrap: bool = True) -> None:
        lines = textwrap.wrap(_comment(text), _COMMENT_WIDTH) if wrap else [_comment(text)]
        self._blocks.extend(f"; {line}" for line in lines)

    def heading(self, text: str) -> None:
        """A structure item: listed in the TNC's program structure window."""
        self._blocks.append(f"* - {_comment(text)}")

    def text(self) -> str:
        # The TNC stores programs with CRLF line endings.
        return "".join(f"{number} {block}\r\n" for number, block in enumerate(self._blocks))


def _comment(text: str) -> str:
    return ascii_upper(text).translate(_COMMENT_FIXES).rstrip()
