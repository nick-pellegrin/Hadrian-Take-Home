"""Domain model: machines and warm-up profiles.

Instances are built by :mod:`cnc_warmup.config`, which validates every value.
All classes are immutable, so a loaded configuration can be shared freely.
Units are millimetres, mm/min, rpm and minutes throughout.
"""

from dataclasses import dataclass
from enum import StrEnum


class Axis(StrEnum):
    X = "x"
    Y = "y"
    Z = "z"


class Controller(StrEnum):
    """Target CNC control, which selects the post-processor."""

    HEIDENHAIN = "heidenhain"  # TNC 640, Klartext programs (.H)
    FANUC = "fanuc"  # 31i, ISO programs (.nc)


class Home(StrEnum):
    """End of each axis where the machine's reference point (home) sits."""

    MAX = "max"  # usual VMC convention: machine coordinates run from -travel to 0
    MIN = "min"  # machine coordinates run from 0 to +travel


class Ramp(StrEnum):
    """How feed and spindle speed step from their start to their finish values."""

    LINEAR = "linear"  # equal steps
    GEOMETRIC = "geometric"  # equal ratios: smaller steps and more time at low speed


class Coolant(StrEnum):
    OFF = "off"
    FLOOD = "flood"


class SweepMove(StrEnum):
    """Building blocks of one full-travel sweep pass."""

    PERIMETER = "perimeter"  # X and Y full strokes around the XY travel rectangle
    DIAGONALS = "diagonals"  # corner-to-corner moves, so X and Y interpolate together
    Z_STROKE = "z_stroke"  # Z from the top of travel to the bottom and back


class ZStrokeAt(StrEnum):
    """XY position where the Z stroke runs."""

    CENTER = "center"
    START = "start"  # the start corner of the sweep


class FanucCancelCode(StrEnum):
    """Optional modal-cancel codes for the Fanuc safe-start block.

    Each code belongs to a control option, and a control without that option
    raises an alarm on it, so each machine opts in to the codes it supports.
    """

    G15 = "G15"  # polar coordinate command cancel
    G50 = "G50"  # scaling cancel
    G50_1 = "G50.1"  # programmable mirror image cancel
    G69 = "G69"  # coordinate system rotation cancel


@dataclass(frozen=True)
class AxisLimits:
    """Travel of one axis in machine coordinates (Heidenhain M91, Fanuc G53)."""

    min: float
    max: float

    @property
    def travel(self) -> float:
        return self.max - self.min


@dataclass(frozen=True)
class FanucSettings:
    program_number: int  # the O-number, 1-8999
    cancel_codes: tuple[FanucCancelCode, ...] = ()


@dataclass(frozen=True)
class Machine:
    id: str
    x: AxisLimits
    y: AxisLimits
    z: AxisLimits
    spindle_max_rpm: float
    max_feed: float  # mm/min
    description: str = ""
    controller: Controller = Controller.HEIDENHAIN  # default output when none is requested
    fanuc: FanucSettings | None = None  # required only to generate Fanuc programs

    def limits(self, axis: Axis) -> AxisLimits:
        return {Axis.X: self.x, Axis.Y: self.y, Axis.Z: self.z}[axis]


@dataclass(frozen=True)
class WarmupProfile:
    """How a warm-up runs, independent of the machine that runs it.

    The run is split into ``stages``. Each stage turns the spindle at one speed and
    sweeps the axes at one feed, stepping from the start to the finish values.
    The defaults below are also the defaults for keys left out of profiles.toml.
    """

    name: str
    duration_min: float
    stages: int
    feed_start: float  # mm/min
    feed_end: float  # mm/min
    rpm_start: float
    rpm_end: float
    description: str = ""
    ramp: Ramp = Ramp.LINEAR
    coolant: Coolant = Coolant.OFF
    edge_margin_mm: float = 1.0  # distance kept from every travel limit
    pattern: tuple[SweepMove, ...] = (SweepMove.PERIMETER, SweepMove.DIAGONALS, SweepMove.Z_STROKE)
    z_stroke_at: ZStrokeAt = ZStrokeAt.CENTER
    operator_confirm: bool = True  # checklist stop before the spindle starts
    runtime_guards: bool = True  # program checks the control's soft limits match the machine
    final_rapid_pass: bool = False  # finish with full strokes at rapid traverse
