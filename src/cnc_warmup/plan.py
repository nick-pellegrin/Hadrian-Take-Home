"""Turn a machine and a warm-up profile into a controller-neutral plan.

The plan fixes everything about the warm-up except the NC dialect:
- the order of operations, as a sequence of steps;
- every position, in machine coordinates;
- each stage's spindle speed, feed, number of passes and dwell.

Post-processors only translate the plan into Klartext or Fanuc code. The
safety-relevant decisions therefore live here, in one place, where tests can
check them.

Program outline::

    safe start: modal reset, spindle stop, coolant off
    [runtime guard]  control's soft limits must contain every commanded position
    [operator stop]  checklist confirmation, before anything moves or spins
    retract Z to the top of travel, then move XY to the sweep start corner
    spindle on [+ flood coolant]
    stages 1..N: set speed and feed, repeat the sweep pass, dwell to fill the stage
    [final sweep at rapid traverse]
    [coolant off], spindle stop, retract Z, park XY, end
"""

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import assert_never

from cnc_warmup.config import ConfigError, check_compatibility
from cnc_warmup.issues import Issue, Severity
from cnc_warmup.model import (
    Axis,
    AxisLimits,
    Coolant,
    Machine,
    SweepMove,
    WarmupProfile,
    ZStrokeAt,
)
from cnc_warmup.ramp import ramp_values

# Positions are rounded to the finest resolution the posts write (0.001 mm), so the
# plan and the rendered programs agree exactly.
_STEPS_PER_MM = 1000

# Allowed slack before a stage counts as running over its share of the duration.
_OVERRUN_TOLERANCE_S = 1.0

CHECKLIST = (
    "Table clear: no part, fixture or vise anywhere in the travel",
    "Spindle empty: no tool in the spindle",
    "Doors closed",
    "Feed and spindle overrides at 100%",
)
COOLANT_CHECK = "Coolant tank filled: flood coolant runs during the warm-up"


@dataclass(frozen=True)
class Point:
    """A position in machine coordinates, mm."""

    x: float
    y: float
    z: float


@dataclass(frozen=True)
class Position:
    """A partial position: ``None`` means the axis is not commanded or not yet known."""

    x: float | None = None
    y: float | None = None
    z: float | None = None


@dataclass(frozen=True)
class Envelope:
    """The box the sweeps cover: the machine's travel inset by the edge margin."""

    x: AxisLimits
    y: AxisLimits
    z: AxisLimits

    @property
    def start(self) -> Point:
        """Sweep start corner: X and Y minimum, Z at the top of travel."""
        return Point(self.x.min, self.y.min, self.z.max)

    @property
    def center(self) -> Point:
        """XY center of travel, Z at the top."""
        return Point(
            _round((self.x.min + self.x.max) / 2), _round((self.y.min + self.y.max) / 2), self.z.max
        )

    def limits(self, axis: Axis) -> AxisLimits:
        return {Axis.X: self.x, Axis.Y: self.y, Axis.Z: self.z}[axis]


@dataclass(frozen=True)
class Segment:
    """One straight move of the sweep pass."""

    target: Point
    label: str  # e.g. "X+ full stroke", used for NC comments


@dataclass(frozen=True)
class Stage:
    number: int  # 1-based
    rpm: float
    feed: float  # mm/min
    passes: int
    pass_seconds: float  # estimated time of one sweep pass at `feed`
    dwell_seconds: float  # spindle-only time that tops the stage up to its share of the duration

    @property
    def seconds(self) -> float:
        return self.passes * self.pass_seconds + self.dwell_seconds


# --- Steps: the program, in order -----------------------------------------------


@dataclass(frozen=True)
class Section:
    """Start of a program section. Posts render the title as a heading comment."""

    title: str


@dataclass(frozen=True)
class SafeStart:
    """Cancel leftover modal state. Each post emits its controller's reset block."""


@dataclass(frozen=True)
class SpindleStop:
    pass


@dataclass(frozen=True)
class CoolantOff:
    pass


@dataclass(frozen=True)
class CoolantOn:
    pass


@dataclass(frozen=True)
class RuntimeGuard:
    """Stop with an alarm unless the control's soft limits contain ``envelope``.

    This catches a program generated for a larger machine before anything moves.
    """

    envelope: Envelope


@dataclass(frozen=True)
class OperatorStop:
    checklist: tuple[str, ...]


@dataclass(frozen=True)
class RapidMove:
    target: Position
    label: str


@dataclass(frozen=True)
class SpindleOn:
    rpm: float


@dataclass(frozen=True)
class RunStage:
    """Set the stage's speed and feed, repeat the sweep pass, then dwell."""

    stage: Stage


@dataclass(frozen=True)
class RapidSweep:
    """One more sweep pass at rapid traverse."""


@dataclass(frozen=True)
class ProgramEnd:
    pass


Step = (
    Section
    | SafeStart
    | SpindleStop
    | CoolantOff
    | CoolantOn
    | RuntimeGuard
    | OperatorStop
    | RapidMove
    | SpindleOn
    | RunStage
    | RapidSweep
    | ProgramEnd
)


@dataclass(frozen=True)
class WarmupPlan:
    machine: Machine
    profile: WarmupProfile
    envelope: Envelope
    sweep: tuple[Segment, ...]  # one closed pass: starts and ends at envelope.start
    stages: tuple[Stage, ...]
    steps: tuple[Step, ...]
    warnings: tuple[Issue, ...]

    @property
    def park(self) -> Point:
        return self.envelope.center

    @property
    def sweep_length_mm(self) -> float:
        return _path_length(self.envelope.start, self.sweep)

    @property
    def stage_budget_seconds(self) -> float:
        """Each stage's share of the profile duration."""
        return self.profile.duration_min * 60 / self.profile.stages

    @property
    def estimated_seconds(self) -> float:
        """Stage time plus the optional rapid pass.

        Rapid positioning and acceleration are not included. The rapid pass is
        estimated at max_feed, because the machine's rapid rate is not configured.
        """
        rapid = self.sweep_length_mm / self.machine.max_feed * 60
        return sum(stage.seconds for stage in self.stages) + (
            rapid if self.profile.final_rapid_pass else 0.0
        )


def build_plan(machine: Machine, profile: WarmupProfile) -> WarmupPlan:
    """Plan a warm-up. Raises ConfigError if the profile does not fit the machine."""
    problems = check_compatibility(machine, profile)
    if problems:
        raise ConfigError(problems)

    envelope = _envelope(machine, profile.edge_margin_mm)
    sweep = _sweep(envelope, profile)
    stages = _stages(profile, _path_length(envelope.start, sweep))
    return WarmupPlan(
        machine=machine,
        profile=profile,
        envelope=envelope,
        sweep=sweep,
        stages=stages,
        steps=_steps(profile, envelope, stages),
        warnings=_overrun_warnings(machine, profile, stages),
    )


def _envelope(machine: Machine, margin: float) -> Envelope:
    def inset(limits: AxisLimits) -> AxisLimits:
        # Round inward: rounding may tighten the margin but must never eat into it.
        return AxisLimits(
            _round(limits.min + margin, math.ceil), _round(limits.max - margin, math.floor)
        )

    return Envelope(inset(machine.x), inset(machine.y), inset(machine.z))


def _sweep(envelope: Envelope, profile: WarmupProfile) -> tuple[Segment, ...]:
    """One closed pass that visits every travel extreme, built from the profile's pattern."""
    x0, x1 = envelope.x.min, envelope.x.max
    y0, y1 = envelope.y.min, envelope.y.max
    top = envelope.z.max
    segments: list[Segment] = []
    for move in profile.pattern:
        match move:
            case SweepMove.PERIMETER:
                segments += [
                    Segment(Point(x1, y0, top), "X+ full stroke"),
                    Segment(Point(x1, y1, top), "Y+ full stroke"),
                    Segment(Point(x0, y1, top), "X- full stroke"),
                    Segment(Point(x0, y0, top), "Y- full stroke"),
                ]
            case SweepMove.DIAGONALS:
                segments += [
                    Segment(Point(x1, y1, top), "XY diagonal, both axes together"),
                    Segment(Point(x0, y0, top), "XY diagonal back"),
                ]
            case SweepMove.Z_STROKE:
                segments += _z_stroke(envelope, profile.z_stroke_at)
            case _:
                assert_never(move)
    return tuple(segments)


def _z_stroke(envelope: Envelope, at: ZStrokeAt) -> list[Segment]:
    start = envelope.start
    bottom = envelope.z.min
    if at is ZStrokeAt.START:
        return [
            Segment(Point(start.x, start.y, bottom), "Z- full stroke"),
            Segment(start, "Z+ full stroke"),
        ]
    center = envelope.center
    return [
        Segment(center, "to the XY center"),
        Segment(Point(center.x, center.y, bottom), "Z- full stroke"),
        Segment(center, "Z+ full stroke"),
        Segment(start, "back to the start corner"),
    ]


def _stages(profile: WarmupProfile, sweep_length_mm: float) -> tuple[Stage, ...]:
    """Split the duration into stages; each fills its share with whole passes plus a dwell."""
    feeds = ramp_values(profile.feed_start, profile.feed_end, profile.stages, profile.ramp)
    rpms = ramp_values(profile.rpm_start, profile.rpm_end, profile.stages, profile.ramp)
    budget = profile.duration_min * 60 / profile.stages
    stages = []
    for number, (feed, rpm) in enumerate(zip(feeds, rpms, strict=True), start=1):
        pass_seconds = sweep_length_mm / feed * 60
        passes = max(1, math.floor(budget / pass_seconds))
        dwell_seconds = max(0.0, float(math.floor(budget - passes * pass_seconds)))
        stages.append(Stage(number, rpm, feed, passes, pass_seconds, dwell_seconds))
    return tuple(stages)


def _steps(
    profile: WarmupProfile, envelope: Envelope, stages: tuple[Stage, ...]
) -> tuple[Step, ...]:
    flood = profile.coolant is Coolant.FLOOD
    top = envelope.z.max
    start, park = envelope.start, envelope.center

    steps: list[Step] = [
        Section("Safe start: spindle and coolant off"),
        SafeStart(),
        SpindleStop(),
        CoolantOff(),
    ]
    if profile.runtime_guards:
        steps += [
            Section("Travel check: the soft limits of the control must contain the sweep"),
            RuntimeGuard(envelope),
        ]
    if profile.operator_confirm:
        steps += [
            Section("Operator check"),
            OperatorStop((*CHECKLIST, COOLANT_CHECK) if flood else CHECKLIST),
        ]
    steps += [
        Section("To the start corner: Z up first, then XY"),
        RapidMove(Position(z=top), "retract Z to the top of travel first"),
        RapidMove(Position(x=start.x, y=start.y), "to the sweep start corner"),
        Section("Spindle on, flood coolant on" if flood else "Spindle on"),
        SpindleOn(stages[0].rpm),
    ]
    if flood:
        steps.append(CoolantOn())
    for stage in stages:
        steps += [Section(_stage_title(stage, len(stages))), RunStage(stage)]
    if profile.final_rapid_pass:
        steps += [Section("Final sweep at rapid traverse"), RapidSweep()]
    steps.append(Section(f"Shutdown: {'coolant and ' if flood else ''}spindle off, Z up, park"))
    if flood:
        steps.append(CoolantOff())
    steps += [
        SpindleStop(),
        RapidMove(Position(z=top), "retract Z"),
        RapidMove(Position(x=park.x, y=park.y), "park at the XY center"),
        ProgramEnd(),
    ]
    return tuple(steps)


def _stage_title(stage: Stage, total: int) -> str:
    passes = f"{stage.passes} pass" + ("es" if stage.passes > 1 else "")
    dwell = f", then {format_number(stage.dwell_seconds)} s dwell" if stage.dwell_seconds else ""
    return (
        f"Stage {stage.number} of {total}: {format_number(stage.rpm)} rpm, "
        f"{format_number(stage.feed)} mm/min, {passes}{dwell}"
    )


def _overrun_warnings(
    machine: Machine, profile: WarmupProfile, stages: tuple[Stage, ...]
) -> tuple[Issue, ...]:
    budget = profile.duration_min * 60 / profile.stages
    return tuple(
        Issue(
            f"profiles.{profile.name}.feed_start",
            f"stage {stage.number} runs {format_duration(stage.seconds)}, over its "
            f"{format_duration(budget)} share of the duration: one sweep pass at "
            f"{format_number(stage.feed)} mm/min takes that long on machine {machine.id}. "
            "Raise feed_start or duration_min",
            Severity.WARNING,
        )
        for stage in stages
        if stage.seconds > budget + _OVERRUN_TOLERANCE_S
    )


# --- Flattening --------------------------------------------------------------------


@dataclass(frozen=True)
class Motion:
    """One commanded move and the state it runs under."""

    position: Position  # absolute position after the move; None = axis not yet commanded
    feed: float | None  # mm/min; None = rapid traverse
    spindle_rpm: float  # 0 = stopped
    coolant: bool


@dataclass(frozen=True)
class Dwell:
    seconds: float
    spindle_rpm: float


Event = Motion | Dwell


def trace(plan: WarmupPlan) -> tuple[Event, ...]:
    """Every move and dwell the program commands, in order.

    Tests check the plan's safety properties on this. The round-trip verifier
    compares it with what the post-processors actually wrote.
    """
    events: list[Event] = []
    position = Position()
    rpm, coolant = 0.0, False

    def sweep(feed: float | None) -> None:
        nonlocal position
        for segment in plan.sweep:
            target = segment.target
            position = Position(target.x, target.y, target.z)
            events.append(Motion(position, feed, rpm, coolant))

    for step in plan.steps:
        match step:
            case SpindleOn(rpm=speed):
                rpm = speed
            case SpindleStop():
                rpm = 0.0
            case CoolantOn():
                coolant = True
            case CoolantOff():
                coolant = False
            case RapidMove(target=target):
                position = Position(
                    position.x if target.x is None else target.x,
                    position.y if target.y is None else target.y,
                    position.z if target.z is None else target.z,
                )
                events.append(Motion(position, None, rpm, coolant))
            case RunStage(stage=stage):
                rpm = stage.rpm
                for _ in range(stage.passes):
                    sweep(stage.feed)
                if stage.dwell_seconds:
                    events.append(Dwell(stage.dwell_seconds, rpm))
            case RapidSweep():
                sweep(None)
            case Section() | SafeStart() | RuntimeGuard() | OperatorStop() | ProgramEnd():
                pass
            case _:
                assert_never(step)
    return tuple(events)


# --- Helpers -----------------------------------------------------------------------


def format_duration(seconds: float) -> str:
    """Format as m:ss, e.g. 208.4 -> '3:28'."""
    minutes, secs = divmod(round(seconds), 60)
    return f"{minutes}:{secs:02d}"


def format_number(value: float) -> str:
    """Format at the plan's 0.001 resolution without trailing zeros: 2500.0 -> '2500'."""
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def _path_length(start: Point, segments: tuple[Segment, ...]) -> float:
    length, here = 0.0, start
    for segment in segments:
        length += math.dist(
            (here.x, here.y, here.z), (segment.target.x, segment.target.y, segment.target.z)
        )
        here = segment.target
    return length


def _round(value: float, direction: Callable[[float], int] = round) -> float:
    """Round to the posts' resolution (0.001 mm), to nearest or with ``math.ceil``/``floor``."""
    # Snap float noise first, so 761.0000000001 doesn't round up to 761.001.
    return direction(round(value * _STEPS_PER_MM, 6)) / _STEPS_PER_MM
