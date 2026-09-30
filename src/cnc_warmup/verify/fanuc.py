"""Reader for the ISO G-code subset the Fanuc post writes.

It follows the program the way a 31i would:
- G90/G91 and G00/G01 are modal;
- G53 is one-shot, always moves at rapid, and is **ignored in G91**, so a
  G53 written without G90 becomes an incremental move, exactly as on the
  control;
- a move in work coordinates (G90 without G53) is rejected, because a program
  that depends on work offsets can't be checked, or trusted to be safe;
- the travel check reads PRM[1320]/PRM[1321] from the given soft limits and
  stops with the #3000 message.
"""

import re

from cnc_warmup.model import Axis
from cnc_warmup.plan import Position
from cnc_warmup.verify.machine import (
    MAX_BLOCKS,
    ReaderError,
    Run,
    SimulatedMachine,
    SoftLimits,
    axis_from_letter,
)

_COMMENT = re.compile(r"\([^()]*\)")
_WORD = re.compile(r"([A-Z])([-+]?\d+(?:\.\d*)?)")
_GUARD = re.compile(
    r"IF \[PRM\[(1320|1321)\]/\[([123])\] (GT|LT) ([-+]?\d+(?:\.\d*)?)\] "
    r"THEN #3000=\d+\(([^()]*)\)"
)
# Modal-state G codes the program may set, which don't change what the reader tracks.
_NO_EFFECT = {"21", "17", "40", "49", "80", "94", "15", "50", "69"}


def read_fanuc(text: str, soft_limits: SoftLimits, *, max_blocks: int = MAX_BLOCKS) -> Run:
    lines = text.split("\n")
    if lines[0] != "%" or lines[-2:] != ["%", ""]:
        raise ReaderError("the program must be wrapped in % lines")

    machine = SimulatedMachine(soft_limits, max_blocks)
    absolute, motion, feed = True, "", None
    for number, line in enumerate(lines[1:-2], start=2):
        machine.tick()
        if guard := _GUARD.fullmatch(line):
            parameter, axis_index, comparison, limit, message = guard.groups()
            limits = soft_limits[list(Axis)[int(axis_index) - 1]]
            actual = limits.max if parameter == "1320" else limits.min
            alarms = actual > float(limit) if comparison == "GT" else actual < float(limit)
            if alarms:
                return machine.result(alarm=message)
            continue

        code = _COMMENT.sub("", line).strip()
        if not code:
            continue
        words = []
        for token in code.split():
            match = _WORD.fullmatch(token)
            if match is None:
                raise ReaderError(f"line {number}: not in the supported subset: {line!r}")
            words.append((match[1], match[2]))
        if words[0][0] == "O":
            continue  # program number

        machine_coordinates = dwell = names_axes = False
        for letter, value in words:
            match letter, value:
                case "G", "90":
                    absolute = True
                case "G", "91":
                    absolute = False
                case "G", ("00" | "01"):
                    motion = value
                case "G", "53":
                    machine_coordinates = True
                case "G", "04":
                    dwell = True
                case "G", "50.1":
                    names_axes = True  # mirror cancel: its axis words are not a move
                case "G", code_value if code_value in _NO_EFFECT:
                    pass
                case "F", _:
                    feed = float(value)
                case "S", _:
                    machine.programmed_rpm = float(value)
                case "M", "03":
                    machine.spindle_on = True
                case "M", "05":
                    machine.spindle_on = False
                case "M", "08":
                    machine.coolant = True
                case "M", "09":
                    machine.coolant = False
                case ("M", ("00" | "30")) | ("N" | "P" | "X" | "Y" | "Z", _):
                    pass
                case _:
                    raise ReaderError(f"line {number}: unsupported word {letter}{value}")

        axes = {
            axis_from_letter(letter): float(value) for letter, value in words if letter in "XYZ"
        }
        if dwell:
            pause = next((float(value) for letter, value in words if letter == "P"), None)
            if pause is None or axes:
                raise ReaderError(f"line {number}: dwell must be G04 P<milliseconds>")
            machine.dwell(pause / 1000)
        elif axes and not names_axes:
            _move(machine, number, axes, machine_coordinates and absolute, absolute, motion, feed)
        if ("M", "30") in words:
            return machine.result()
    raise ReaderError("reached the end of the program without M30")


def _move(
    machine: SimulatedMachine,
    number: int,
    axes: dict[Axis, float],
    in_machine_coordinates: bool,
    absolute: bool,
    motion: str,
    feed: float | None,
) -> None:
    here = machine.position
    if in_machine_coordinates:  # G90 G53: always rapid, whatever the motion mode
        target = Position(
            axes.get(Axis.X, here.x), axes.get(Axis.Y, here.y), axes.get(Axis.Z, here.z)
        )
        machine.move(target, None)
        return
    if absolute:
        raise ReaderError(f"line {number}: move in work coordinates (G90 without G53)")

    # G91. A G53 on this line is ignored by the control, so it lands here too.
    coordinates = {Axis.X: here.x, Axis.Y: here.y, Axis.Z: here.z}
    for axis, delta in axes.items():
        start = coordinates[axis]
        if start is None:
            raise ReaderError(
                f"line {number}: incremental {axis.upper()} move from an unknown position"
            )
        coordinates[axis] = start + delta
    target = Position(coordinates[Axis.X], coordinates[Axis.Y], coordinates[Axis.Z])
    if motion == "00":
        machine.move(target, None)
    elif motion == "01" and feed is not None:
        machine.move(target, feed)
    else:
        raise ReaderError(f"line {number}: feed move without a motion mode or feed rate")
