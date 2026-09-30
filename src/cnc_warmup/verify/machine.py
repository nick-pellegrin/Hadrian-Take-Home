"""Simulated machine state shared by the program readers."""

from collections.abc import Mapping
from dataclasses import dataclass

from cnc_warmup.model import Axis, AxisLimits
from cnc_warmup.plan import Dwell, Event, Motion, Position

SoftLimits = Mapping[Axis, AxisLimits]

# Executed blocks before a program is declared endless. Large enough for any real
# warm-up (tens of thousands of blocks), small enough to fail fast on a loop.
MAX_BLOCKS = 2_000_000


class ReaderError(Exception):
    """The program uses something outside the subset the readers understand."""


@dataclass(frozen=True)
class Run:
    """What a program made the simulated machine do."""

    events: tuple[Event, ...]
    alarm: str | None = None  # set when the program stopped itself (FN 14, #3000)


class SimulatedMachine:
    """Tracks position, spindle and coolant, and records every move and dwell."""

    def __init__(self, soft_limits: SoftLimits, max_blocks: int = MAX_BLOCKS) -> None:
        self.soft_limits = soft_limits
        self.position = Position()
        self.programmed_rpm = 0.0
        self.spindle_on = False
        self.coolant = False
        self._events: list[Event] = []
        self._blocks_left = max_blocks

    @property
    def rpm(self) -> float:
        return self.programmed_rpm if self.spindle_on else 0.0

    def tick(self) -> None:
        """Count one executed block."""
        self._blocks_left -= 1
        if self._blocks_left < 0:
            raise ReaderError("the program does not end (too many blocks executed)")

    def move(self, target: Position, feed: float | None) -> None:
        self.position = target
        self._events.append(Motion(target, feed, self.rpm, self.coolant))

    def dwell(self, seconds: float) -> None:
        self._events.append(Dwell(seconds, self.rpm))

    def result(self, alarm: str | None = None) -> Run:
        return Run(tuple(self._events), alarm)


def axis_from_letter(letter: str) -> Axis:
    return Axis(letter.lower())
