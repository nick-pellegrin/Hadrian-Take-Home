"""Reader for the Klartext subset the Heidenhain post writes.

It follows the program the way the TNC would:
- subprogram calls return at LBL 0;
- program-section repeats run REP more times;
- QL parameters hold values, including feeds read with FQL;
- FN 18 reads the given soft limits, and FN 11/12 jump;
- FN 14 stops the program with an alarm.
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

_NUMBERED = re.compile(r"(\d+) (.*)")
_LABEL_NAME = r'("\w+"|\d+)'
_OPERAND = r"([+-](?:QL\d+|\d+(?:\.\d+)?))"

_MOVE = re.compile(r"L((?: [XYZ][+-]\d+(?:\.\d+)?)+) R0 (FMAX|FQL\d+|F\d+(?:\.\d+)?) M91")
_AXIS_WORD = re.compile(r"([XYZ])([+-]\d+(?:\.\d+)?)")
_ASSIGN = re.compile(r"(QL\d+) = ([+-]?\d+(?:\.\d+)?)")
_SPEED = re.compile(r"TOOL CALL S(\d+(?:\.\d+)?)")
_CALL = re.compile(rf"CALL LBL {_LABEL_NAME}(?: REP (\d+))?")
_LABEL = re.compile(rf"LBL {_LABEL_NAME}")
_SYSREAD = re.compile(r"FN 18: SYSREAD (QL\d+) = ID230 NR([23]) IDX([123])")
_JUMP = re.compile(rf"FN (11|12): IF {_OPERAND} (GT|LT) {_OPERAND} GOTO LBL {_LABEL_NAME}")
_ERROR = re.compile(r"FN 14: ERROR = (\d+)")
_DWELL = re.compile(r"CYCL DEF 9\.1 DWELL (\d+(?:\.\d+)?)")
_M_FUNCTIONS = re.compile(r"M\d+(?: M\d+)*")
_NO_EFFECT = ("BEGIN PGM ", "CYCL DEF 9.0 DWELL TIME", "STOP")


def read_klartext(text: str, soft_limits: SoftLimits, *, max_blocks: int = MAX_BLOCKS) -> Run:
    blocks = _code_blocks(text)
    labels = {
        match.group(1): index
        for index, code in enumerate(blocks)
        if (match := _LABEL.fullmatch(code)) and match.group(1) != "0"
    }
    machine = SimulatedMachine(soft_limits, max_blocks)
    params: dict[str, float] = {}
    returns: list[int] = []  # block to resume at after each open subprogram call
    repeats_left: dict[int, int] = {}  # CALL ... REP block -> repeats still to run

    def jump_target(label: str) -> int:
        if label not in labels:
            raise ReaderError(f"jump to undefined label {label}")
        return labels[label] + 1

    def value(operand: str) -> float:
        sign, body = (-1 if operand[0] == "-" else 1), operand[1:]
        if body.startswith("QL"):
            if body not in params:
                raise ReaderError(f"{body} is read before it is set")
            return sign * params[body]
        return sign * float(body)

    index = 0
    while index < len(blocks):
        machine.tick()
        code = blocks[index]
        here, index = index, index + 1

        if not code or code.startswith(_NO_EFFECT):  # comments, headings, no-ops
            continue
        if code.startswith("END PGM "):
            raise ReaderError("reached END PGM without M30")
        if match := _MOVE.fullmatch(code):
            _move(machine, params, match)
        elif match := _SPEED.fullmatch(code):
            machine.programmed_rpm = float(match.group(1))
        elif _M_FUNCTIONS.fullmatch(code):
            if _m_functions(machine, code.split()):
                return machine.result()
        elif match := _ASSIGN.fullmatch(code):
            params[match.group(1)] = float(match.group(2))
        elif match := _CALL.fullmatch(code):
            label, repeats = match.group(1), match.group(2)
            if repeats is None:  # subprogram call
                returns.append(index)
                index = jump_target(label)
            else:  # program-section repeat: jump back REP more times, then carry on
                left = repeats_left.get(here, int(repeats))
                if left:
                    repeats_left[here] = left - 1
                    index = jump_target(label)
                else:
                    repeats_left.pop(here, None)
        elif match := _LABEL.fullmatch(code):
            if match.group(1) == "0":
                if not returns:
                    raise ReaderError("LBL 0 reached outside a subprogram")
                index = returns.pop()
        elif match := _SYSREAD.fullmatch(code):
            parameter, number, axis_index = match.groups()
            limits = soft_limits[list(Axis)[int(axis_index) - 1]]
            params[parameter] = limits.min if number == "2" else limits.max
        elif match := _JUMP.fullmatch(code):
            function, left_operand, comparison, right_operand, label = match.groups()
            if (function, comparison) not in {("11", "GT"), ("12", "LT")}:
                raise ReaderError(f"FN {function} does not compare with {comparison}")
            left_value, right_value = value(left_operand), value(right_operand)
            jumps = left_value > right_value if comparison == "GT" else left_value < right_value
            if jumps:
                index = jump_target(label)
        elif match := _ERROR.fullmatch(code):
            return machine.result(alarm=f"FN 14: ERROR = {match.group(1)}")
        elif match := _DWELL.fullmatch(code):
            machine.dwell(float(match.group(1)))
        else:
            raise ReaderError(f"block {here}: not in the supported subset: {code!r}")
    raise ReaderError("ran past the last block")


def _code_blocks(text: str) -> list[str]:
    """Each block's code: block numbers checked and removed, comments emptied."""
    blocks = []
    for index, line in enumerate(text.split("\r\n")[:-1]):
        match = _NUMBERED.fullmatch(line)
        if match is None or int(match.group(1)) != index:
            raise ReaderError(f"block {index}: bad block number: {line!r}")
        code = match.group(2).split(";", 1)[0].strip()
        blocks.append("" if code.startswith("*") else code)
    return blocks


def _move(machine: SimulatedMachine, params: dict[str, float], match: re.Match[str]) -> None:
    axes = {
        axis_from_letter(letter): float(value) for letter, value in _AXIS_WORD.findall(match[1])
    }
    feed_word = match[2]
    feed: float | None
    if feed_word == "FMAX":
        feed = None
    elif feed_word.startswith("FQL"):
        if feed_word[1:] not in params:
            raise ReaderError(f"{feed_word[1:]} is read before it is set")
        feed = params[feed_word[1:]]
    else:
        feed = float(feed_word[1:])
    here = machine.position
    machine.move(
        Position(axes.get(Axis.X, here.x), axes.get(Axis.Y, here.y), axes.get(Axis.Z, here.z)),
        feed,
    )


def _m_functions(machine: SimulatedMachine, words: list[str]) -> bool:
    """Apply M functions. Returns True at the end of the program."""
    ended = False
    for word in words:
        match word:
            case "M3":
                machine.spindle_on = True
            case "M5":
                machine.spindle_on = False
            case "M8":
                machine.coolant = True
            case "M9":
                machine.coolant = False
            case "M30" | "M2":
                ended = True
            case _:
                raise ReaderError(f"unsupported miscellaneous function {word}")
    return ended
