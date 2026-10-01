"""Fanuc 31i post-processor: renders a plan as an ISO G-code program (.nc).

Program layout::

    %
    O8001 (WARMUP M1 DAILY), header comments with the stage table
    safe start: G21, modal reset, the machine's cancel codes, M05, M09
    [travel check: stroke limits PRM[1320]/PRM[1321] against the sweep, #3000 alarm]
    [operator checklist, M00]
    G90 G53 G00 to the start corner, spindle on
    stages N100, N200, ...: re-anchor at the start corner (Z first), S.. M03, the
        passes as G91 G01 moves, G90, G04 dwell
    shutdown, M30
    %

How positions stay independent of work offsets and tool length:
- G53 selects machine coordinates, so work offsets (G54-G59, G52, the external
  offset) don't apply to it.
- G53 is one-shot, always moves at rapid traverse, and is ignored in G91. So
  every positioning move is written as ``G90 G53 G00``.
- The sweep needs feed moves, which G53 can't do. They are G91 increments that
  start at a point just reached with G53, and no work offset can shift an
  increment either.
- Tool length compensation is cancelled with G49.
- Every coordinate carries a decimal point: with parameter 3401#0 (DPI) = 0,
  X760 would mean 0.760 mm.

Each stage re-anchors at the start corner and restates the spindle speed (and
coolant). An operator can therefore restart at any stage N-number, but must
never restart in the middle of the incremental passes.
"""

import re
import string
import textwrap
from typing import assert_never

from cnc_warmup.config import ConfigError
from cnc_warmup.issues import Issue
from cnc_warmup.model import Axis, Controller, FanucCancelCode, FanucSettings
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

# Stored stroke check 1: axis-type parameters, positive and negative limit per axis.
STROKE_LIMIT_PLUS = 1320
STROKE_LIMIT_MINUS = 1321

# The optional cancel codes, and how each is written. G50.1 names the axes it resets.
_CANCEL_CODES = {
    FanucCancelCode.G15: ("G15", "cancel polar coordinate command"),
    FanucCancelCode.G50: ("G50", "cancel scaling"),
    FanucCancelCode.G50_1: ("G50.1 X0. Y0. Z0.", "cancel programmable mirror image"),
    FanucCancelCode.G69: ("G69", "cancel coordinate system rotation"),
}

_COMMENT_WIDTH = 72
# Parentheses would end the comment early, ';' and '%' mean end-of-block and
# end-of-tape to some transfer tools, and ':' starts a program number.
_DURATION = re.compile(r"\b(\d+):(\d\d)\b")  # 19:57 -> 19M57S
_COMMENT_REPLACEMENTS = {":": " -", ";": ",", "%": " PERCENT"}
_COMMENT_CHARACTERS = set(string.ascii_uppercase + string.digits + " .,-+/=*?")


def program_name(plan: WarmupPlan) -> str:
    return f"O{_settings(plan).program_number:04d}"


def render(plan: WarmupPlan) -> Program:
    settings = _settings(plan)
    name = program_name(plan)
    out = _Lines()
    out.add(name, f"Warmup {plan.machine.id} {plan.profile.name}")
    for line in describe(plan):
        out.comment(line)
    out.comment(
        "Moves use G53 machine coordinates, and G91 increments from points just reached "
        "with G53, so work offsets and tool length do not apply."
    )
    out.comment("Restart only at a stage N-number: the sweep passes are incremental.")
    for row in stage_table(plan):
        out.comment(row, wrap=False)

    coolant = False  # flood coolant state, restated by every stage
    for step in plan.steps:
        match step:
            case Section(title=title):
                out.heading(title)
            case SafeStart():
                _safe_start(out, settings)
            case SpindleStop():
                out.add("M05", "spindle off")
            case CoolantOff():
                out.add("M09", "coolant off")
                coolant = False
            case CoolantOn():
                out.add("M08", "flood coolant on")
                coolant = True
            case RuntimeGuard(envelope=envelope):
                _travel_check(out, envelope)
            case OperatorStop(checklist=checklist):
                out.comment("Before cycle start, check")
                for item in checklist:
                    out.comment(f"- {item}")
                out.add("M00", "press cycle start to run the warm-up")
            case RapidMove(target=target, label=label):
                out.add(f"G90 G53 G00 {_absolute(target)}", label)
            case SpindleOn(rpm=rpm):
                out.add(f"S{_rpm(rpm)} M03", "spindle on clockwise")
            case RunStage(stage=stage):
                _stage(out, plan, stage, coolant)
            case RapidSweep():
                _anchor(out, plan.envelope.start)
                _sweep(out, plan, "G00", feed=None, labels=True)
                out.add("G90", "back to absolute")
            case ProgramEnd():
                out.add("M30", "end of program")
            case _:
                assert_never(step)
    return Program(
        Controller.FANUC,
        name,
        f"{name}_{plan.machine.id}_{plan.profile.name}".upper() + ".nc",
        out.text(),
    )


def _settings(plan: WarmupPlan) -> FanucSettings:
    if plan.machine.fanuc is None:
        path = f"machines.{plan.machine.id}.fanuc"
        message = "missing: add this table with a program_number to generate Fanuc programs"
        raise ConfigError([Issue(path, message)])
    return plan.machine.fanuc


def _safe_start(out: "_Lines", settings: FanucSettings) -> None:
    out.add("G21", "metric - at the start, before any coordinates")
    out.add(
        "G17 G40 G49 G80 G90 G94",
        "XY plane, no cutter/length comp, no canned cycle, absolute, mm/min",
    )
    for code in settings.cancel_codes:
        out.add(*_CANCEL_CODES[code])


def _travel_check(out: "_Lines", envelope: Envelope) -> None:
    """Alarm before anything moves if a stored stroke limit lies inside the sweep."""
    out.comment(
        f"Parameters {STROKE_LIMIT_PLUS} and {STROKE_LIMIT_MINUS} hold the + and - stroke "
        "limits. Reading them needs custom macro and PRM, 30i-B or later."
    )
    for index, axis in enumerate(Axis, start=1):
        limits = envelope.limits(axis)
        name = axis.upper()
        out.add(
            f"IF [PRM[{STROKE_LIMIT_MINUS}]/[{index}] GT {_coord(limits.min)}] "
            f"THEN #3000=1(SWEEP EXCEEDS {name}- LIMIT)"
        )
        out.add(
            f"IF [PRM[{STROKE_LIMIT_PLUS}]/[{index}] LT {_coord(limits.max)}] "
            f"THEN #3000=1(SWEEP EXCEEDS {name}+ LIMIT)"
        )


def _anchor(out: "_Lines", start: Point, block: str = "") -> None:
    """Back to the start corner, Z up first.

    In a normal run the machine is already there, so nothing moves. After a restart at
    a stage, Z clears the travel before X and Y move, like the program's first moves.
    """
    out.add(f"{block}G90 G53 G00 Z{_coord(start.z)}", "Z up first")
    out.add(f"G90 G53 G00 X{_coord(start.x)} Y{_coord(start.y)}", "at the sweep start corner")


def _stage(out: "_Lines", plan: WarmupPlan, stage: Stage, coolant: bool) -> None:
    _anchor(out, plan.envelope.start, f"N{stage.number * 100} ")
    out.add(f"S{_rpm(stage.rpm)} M03", "spindle speed for this stage")
    if coolant:
        out.add("M08", "flood coolant on")
    for number in range(1, stage.passes + 1):
        if stage.passes > 1:
            out.comment(f"Pass {number} of {stage.passes}")
        _sweep(out, plan, "G01", feed=stage.feed, labels=number == 1)
    out.add("G90", "back to absolute")
    if stage.dwell_seconds:
        seconds = format_number(stage.dwell_seconds)
        out.add(f"G04 P{round(stage.dwell_seconds * 1000)}", f"dwell {seconds} s, spindle on")


def _sweep(
    out: "_Lines", plan: WarmupPlan, motion: str, *, feed: float | None, labels: bool
) -> None:
    """One sweep pass as increments from the start corner, writing only the axes that move.

    Every segment moves at least one axis by at least 1 mm (the planner guarantees
    the sweep never has zero-length segments), so every line carries an axis word.
    """
    modal = f"G91 {motion} "  # written on the pass's first move
    speed = f" F{format_number(feed)}" if feed is not None else ""
    here = plan.envelope.start
    for segment in plan.sweep:
        out.add(
            f"{modal}{_increments(here, segment.target)}{speed}", segment.label if labels else ""
        )
        here = segment.target
        modal = speed = ""


def _absolute(target: Position) -> str:
    axes = ((Axis.X, target.x), (Axis.Y, target.y), (Axis.Z, target.z))
    return " ".join(f"{axis.upper()}{_coord(value)}" for axis, value in axes if value is not None)


def _increments(here: Point, target: Point) -> str:
    deltas = (
        (Axis.X, target.x - here.x),
        (Axis.Y, target.y - here.y),
        (Axis.Z, target.z - here.z),
    )
    return " ".join(
        f"{axis.upper()}{_coord(delta)}" for axis, delta in deltas if format_number(delta) != "0"
    )


def _coord(value: float) -> str:
    """Always with a decimal point: 760 -> '760.', -380.5 -> '-380.5'."""
    text = format_number(value)
    return text if "." in text else f"{text}."


def _rpm(rpm: float) -> str:
    return str(round(rpm))  # Fanuc S words are whole rpm


class _Lines:
    """Collects the program's blocks between the two '%' lines."""

    def __init__(self) -> None:
        self._lines: list[str] = []

    def add(self, code: str, comment: str = "") -> None:
        self._lines.append(f"{code} ({_comment(comment)})" if comment else code)

    def comment(self, text: str, *, wrap: bool = True) -> None:
        if wrap:
            lines = textwrap.wrap(_comment(text), _COMMENT_WIDTH)
        else:
            lines = [_comment(text, keep_spacing=True)]
        self._lines.extend(f"({line})" for line in lines)

    def heading(self, text: str) -> None:
        self._lines.append(f"(--- {_comment(text)} ---)")

    def text(self) -> str:
        return "%\n" + "".join(f"{line}\n" for line in self._lines) + "%\n"


def _comment(text: str, *, keep_spacing: bool = False) -> str:
    upper = _DURATION.sub(r"\1M\2S", ascii_upper(text))
    for old, new in _COMMENT_REPLACEMENTS.items():
        upper = upper.replace(old, new)
    cleaned = "".join(char if char in _COMMENT_CHARACTERS else " " for char in upper)
    return cleaned.rstrip() if keep_spacing else " ".join(cleaned.split())
