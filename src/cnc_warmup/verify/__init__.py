"""Read generated programs back and check they do exactly what the plan says.

Each reader is a small interpreter for the subset of its dialect that the post
writes. It runs the program the way the control would and records every move and
dwell. `verify` compares that record with `plan.trace()`: a wrong coordinate,
feed, repeat count, spindle state or dwell anywhere in the program makes the two
sequences differ.

The plan's safety properties are proven on its trace by property-based tests. A
program that reproduces the trace exactly therefore has them too.
"""

from collections.abc import Callable, Mapping, Sequence

from cnc_warmup.model import Axis, Controller
from cnc_warmup.plan import Dwell, Event, Motion, Position, WarmupPlan, format_number, trace
from cnc_warmup.posts.base import Program
from cnc_warmup.verify.fanuc import read_fanuc
from cnc_warmup.verify.klartext import read_klartext
from cnc_warmup.verify.machine import ReaderError, Run, SoftLimits

__all__ = ["ReaderError", "Run", "compare", "run_program", "verify"]

READERS: Mapping[Controller, Callable[[str, SoftLimits], Run]] = {
    Controller.HEIDENHAIN: read_klartext,
    Controller.FANUC: read_fanuc,
}

# Programs write positions and feeds at 0.001 resolution and Fanuc spindle speeds
# as whole rpm, so compare within those resolutions.
_POSITION_TOLERANCE = 1e-6
_FEED_TOLERANCE = 1e-3
_RPM_TOLERANCE = 0.5
_TIME_TOLERANCE = 1e-3
_MAX_REPORTED = 5


def run_program(program: Program, soft_limits: SoftLimits) -> Run:
    """Run a program on a simulated machine with the given soft limits."""
    return READERS[program.controller](program.text, soft_limits)


def verify(plan: WarmupPlan, program: Program) -> list[str]:
    """Problems found running `program` on its own machine. Empty means it matches the plan."""
    soft_limits = {axis: plan.machine.limits(axis) for axis in Axis}
    try:
        run = run_program(program, soft_limits)
    except ReaderError as error:
        return [f"{program.filename}: cannot read the program back: {error}"]
    problems = []
    if run.alarm is not None:
        problems.append(f"{program.filename}: stops with an alarm on its own machine: {run.alarm}")
    problems += [f"{program.filename}: {problem}" for problem in compare(trace(plan), run.events)]
    return problems


def compare(expected: Sequence[Event], actual: Sequence[Event]) -> list[str]:
    """Differences between two event sequences, ignoring moves that go nowhere.

    A move to the current position (the Fanuc post's re-anchoring at each stage,
    a retract when Z is already up) changes nothing on the machine.
    """
    wanted, done = _effective(expected), _effective(actual)
    problems = []
    for index, (want, got) in enumerate(zip(wanted, done, strict=False)):
        if not _same(want, got):
            problems.append(
                f"step {index}: expected {_describe(want)}, program does {_describe(got)}"
            )
            if len(problems) == _MAX_REPORTED:
                break
    if len(wanted) != len(done):
        problems.append(f"expected {len(wanted)} moves and dwells, the program has {len(done)}")
    return problems


def _effective(events: Sequence[Event]) -> list[Event]:
    kept: list[Event] = []
    position = Position()
    for event in events:
        if isinstance(event, Motion):
            if _same_position(event.position, position):
                continue
            position = event.position
        kept.append(event)
    return kept


def _same(want: Event, got: Event) -> bool:
    match want, got:
        case Motion(), Motion():
            return (
                _same_position(want.position, got.position)
                and _close(want.feed, got.feed, _FEED_TOLERANCE)  # None = rapid
                and _close(want.spindle_rpm, got.spindle_rpm, _RPM_TOLERANCE)
                and want.coolant == got.coolant
            )
        case Dwell(), Dwell():
            return _close(want.seconds, got.seconds, _TIME_TOLERANCE) and _close(
                want.spindle_rpm, got.spindle_rpm, _RPM_TOLERANCE
            )
    return False


def _same_position(a: Position, b: Position) -> bool:
    return all(_close(p, q, _POSITION_TOLERANCE) for p, q in ((a.x, b.x), (a.y, b.y), (a.z, b.z)))


def _close(a: float | None, b: float | None, tolerance: float) -> bool:
    """Equal within `tolerance`; None (unknown axis, rapid feed) only equals None."""
    if a is None or b is None:
        return a is b
    return abs(a - b) <= tolerance


def _describe(event: Event) -> str:
    if isinstance(event, Dwell):
        return f"dwell {format_number(event.seconds)} s at {format_number(event.spindle_rpm)} rpm"
    axes = " ".join(
        f"{name}{format_number(value)}"
        for name, value in (
            ("X", event.position.x),
            ("Y", event.position.y),
            ("Z", event.position.z),
        )
        if value is not None
    )
    speed = "rapid" if event.feed is None else f"F{format_number(event.feed)}"
    coolant = ", coolant on" if event.coolant else ""
    return f"move to {axes} at {speed}, {format_number(event.spindle_rpm)} rpm{coolant}"
