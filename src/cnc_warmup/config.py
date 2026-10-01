"""Load and validate the TOML configuration.

``machines.toml`` describes the fleet and ``profiles.toml`` describes how a
warm-up runs. Every value is checked before any NC code is generated. All
problems are reported together, each located by its dotted TOML path::

    config/machines.toml: machines.M2.travel.x: must be greater than 0 (got -1016)

Unknown keys are errors: a typo in a safety-relevant setting must never be
silently ignored.
"""

import dataclasses
import difflib
import math
import re
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, TypeVar

from cnc_warmup.issues import Issue
from cnc_warmup.model import (
    Axis,
    AxisLimits,
    Controller,
    Coolant,
    FanucCancelCode,
    FanucSettings,
    Home,
    Machine,
    Ramp,
    SweepMove,
    WarmupProfile,
    ZStrokeAt,
)

MACHINES_FILE = "machines.toml"
PROFILES_FILE = "profiles.toml"

# Sanity bounds for profile values. Limits that depend on the machine (feed,
# spindle speed, travel) are checked by check_compatibility().
MAX_DURATION_MIN = 240
MIN_STAGES = 2  # a ramp needs at least a start stage and a finish stage
MAX_STAGES = 20
MAX_EDGE_MARGIN_MM = 25
MIN_SWEEP_TRAVEL_MM = 1  # travel an axis must keep after the edge margin at both ends

# Machine IDs become part of program and file names, e.g. WARMUP_M1.H.
_MACHINE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,15}")
_PROFILE_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")
_FANUC_RESERVED_FROM = 9000  # O9000-O9999 hold the machine tool builder's macros

_AXES = tuple(axis.value for axis in Axis)
_MACHINE_KEYS = (
    "description",
    "controller",
    "travel",
    "home",
    "limits",
    "spindle_max_rpm",
    "max_feed",
    "fanuc",
)
_FANUC_KEYS = ("program_number", "cancel_codes")
_PROFILE_KEYS = tuple(f.name for f in dataclasses.fields(WarmupProfile) if f.name != "name")


def _field_defaults(cls: type[Any]) -> dict[str, Any]:
    """Defaults declared on a model dataclass, so optional keys share one source of truth."""
    return {
        f.name: f.default for f in dataclasses.fields(cls) if f.default is not dataclasses.MISSING
    }


_MACHINE_DEFAULTS: Final = _field_defaults(Machine)
_PROFILE_DEFAULTS: Final = _field_defaults(WarmupProfile)

T = TypeVar("T")
E = TypeVar("E", bound=StrEnum)


class ConfigError(Exception):
    """Invalid configuration. Carries every problem found, not just the first."""

    def __init__(self, issues: Sequence[Issue]) -> None:
        self.issues = tuple(issues)
        super().__init__("\n".join(str(issue) for issue in self.issues))


@dataclass(frozen=True)
class Catalog:
    """Everything loaded from one configuration directory."""

    machines: Mapping[str, Machine]
    profiles: Mapping[str, WarmupProfile]

    def machine(self, machine_id: str) -> Machine:
        return _lookup(self.machines, machine_id, "machine")

    def profile(self, name: str) -> WarmupProfile:
        return _lookup(self.profiles, name, "profile")


def _lookup(entries: Mapping[str, T], key: str, kind: str) -> T:
    try:
        return entries[key]
    except KeyError:
        available = ", ".join(entries) or "none"
        issue = Issue(kind, f"unknown {kind} '{key}' (available: {available})")
        raise ConfigError([issue]) from None


# --- Loading ---------------------------------------------------------------------


def load_catalog(config_dir: Path) -> Catalog:
    """Load machines.toml and profiles.toml, reporting problems in both files together."""
    issues: list[Issue] = []
    machines: dict[str, Machine] = {}
    profiles: dict[str, WarmupProfile] = {}
    try:
        machines = load_machines(config_dir / MACHINES_FILE)
    except ConfigError as error:
        issues.extend(error.issues)
    try:
        profiles = load_profiles(config_dir / PROFILES_FILE)
    except ConfigError as error:
        issues.extend(error.issues)
    if issues:
        raise ConfigError(issues)
    return Catalog(machines, profiles)


def load_machines(path: Path) -> dict[str, Machine]:
    return parse_machines(_read_toml(path), source=path.as_posix())


def load_profiles(path: Path) -> dict[str, WarmupProfile]:
    return parse_profiles(_read_toml(path), source=path.as_posix())


def _read_toml(path: Path) -> dict[str, Any]:
    source = path.as_posix()
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        message = "file not found"
    except UnicodeDecodeError:
        message = "file is not valid UTF-8 text"
    except OSError as error:
        message = f"cannot read file: {error.strerror}"
    except tomllib.TOMLDecodeError as error:
        message = f"invalid TOML: {error}"
    raise ConfigError([Issue("", message, source=source)])


# --- Names -----------------------------------------------------------------------


def machine_id_problem(machine_id: str) -> str | None:
    """Why ``machine_id`` can't name a machine, or None if it can."""
    if _MACHINE_ID.fullmatch(machine_id):
        return None
    return (
        "machine ID must be 1-16 letters, digits, '_' or '-', starting with a letter or "
        "digit, because it becomes part of program and file names"
    )


def profile_name_problem(name: str) -> str | None:
    """Why ``name`` can't name a profile, or None if it can."""
    if _PROFILE_NAME.fullmatch(name):
        return None
    return "profile name must be 1-32 lowercase letters, digits or '_', starting with a letter"


# --- Machines --------------------------------------------------------------------


def parse_machines(
    document: Mapping[str, Any], source: str | None = MACHINES_FILE
) -> dict[str, Machine]:
    """Validate a parsed machines.toml document."""
    check = _Checker(source)
    check.reject_unknown(document, ("machines",), "")
    before = check.count
    entries = check.table(document, "machines", "")
    if check.count == before and not entries:
        check.error("machines", "must define at least one machine")

    machines: dict[str, Machine] = {}
    for machine_id, raw in entries.items():
        machine = _parse_machine(check, machine_id, raw)
        if machine is not None:
            machines[machine_id] = machine
    if check.issues:
        raise ConfigError(check.issues)
    return machines


def _parse_machine(check: "_Checker", machine_id: str, raw: object) -> Machine | None:
    path = _join("machines", machine_id)
    before = check.count
    table = check.as_table(raw, path)
    if check.count > before:
        return None
    check.reject_unknown(table, _MACHINE_KEYS, path)
    if problem := machine_id_problem(machine_id):
        check.error(path, problem)
    x, y, z = _parse_axis_limits(check, table, path)
    machine = Machine(
        id=machine_id,
        x=x,
        y=y,
        z=z,
        spindle_max_rpm=check.number(table, "spindle_max_rpm", path, above=0),
        max_feed=check.number(table, "max_feed", path, above=0),
        description=check.string(
            table, "description", path, default=_MACHINE_DEFAULTS["description"]
        ),
        controller=check.choice(
            table, "controller", path, Controller, default=_MACHINE_DEFAULTS["controller"]
        ),
        fanuc=_parse_fanuc(check, table, path) if "fanuc" in table else None,
    )
    return machine if check.count == before else None


def _parse_axis_limits(
    check: "_Checker", table: Mapping[str, Any], path: str
) -> tuple[AxisLimits, AxisLimits, AxisLimits]:
    """Read either `travel` + `home` (shorthand) or explicit `limits`, never both."""
    placeholder = AxisLimits(0.0, 0.0)
    if ("travel" in table) == ("limits" in table):
        check.error(path, "define exactly one of 'travel' (with 'home') or 'limits'")
        return placeholder, placeholder, placeholder

    if "limits" in table:
        if "home" in table:
            check.error(
                _join(path, "home"),
                "only applies to 'travel'; 'limits' are already in machine coordinates",
            )
        limits_path = _join(path, "limits")
        before = check.count
        limits = check.table(table, "limits", path)
        if check.count > before:
            return placeholder, placeholder, placeholder
        check.reject_unknown(limits, _AXES, limits_path)
        x, y, z = (_parse_min_max(check, limits, axis, limits_path) for axis in _AXES)
        return x, y, z

    travel_path = _join(path, "travel")
    before = check.count
    travel = check.table(table, "travel", path)
    if check.count > before:
        return placeholder, placeholder, placeholder
    check.reject_unknown(travel, _AXES, travel_path)
    home = check.choice(table, "home", path, Home)
    x, y, z = (
        _travel_to_limits(check.number(travel, axis, travel_path, above=0), home) for axis in _AXES
    )
    return x, y, z


def _parse_min_max(
    check: "_Checker", limits: Mapping[str, Any], axis: str, limits_path: str
) -> AxisLimits:
    axis_path = _join(limits_path, axis)
    before = check.count
    bounds = check.table(limits, axis, limits_path)
    if check.count > before:
        return AxisLimits(0.0, 0.0)
    check.reject_unknown(bounds, ("min", "max"), axis_path)
    low = check.number(bounds, "min", axis_path)
    high = check.number(bounds, "max", axis_path)
    if check.count == before and low >= high:
        check.error(axis_path, f"min ({_fmt(low)}) must be less than max ({_fmt(high)})")
    return AxisLimits(low, high)


def _travel_to_limits(length: float, home: Home) -> AxisLimits:
    return AxisLimits(-length, 0.0) if home is Home.MAX else AxisLimits(0.0, length)


def _parse_fanuc(check: "_Checker", machine: Mapping[str, Any], machine_path: str) -> FanucSettings:
    path = _join(machine_path, "fanuc")
    before = check.count
    table = check.table(machine, "fanuc", machine_path)
    if check.count > before:
        return FanucSettings(0)
    check.reject_unknown(table, _FANUC_KEYS, path)
    before = check.count
    program_number = check.integer(table, "program_number", path, at_least=1)
    if check.count == before and program_number >= _FANUC_RESERVED_FROM:
        check.error(
            _join(path, "program_number"),
            "O9000-O9999 are reserved for machine tool builder macros; use 1-8999 "
            f"(got {program_number})",
        )
    cancel_codes = check.choices(table, "cancel_codes", path, FanucCancelCode, default=())
    return FanucSettings(program_number, cancel_codes)


# --- Profiles --------------------------------------------------------------------


def parse_profiles(
    document: Mapping[str, Any], source: str | None = PROFILES_FILE
) -> dict[str, WarmupProfile]:
    """Validate a parsed profiles.toml document."""
    check = _Checker(source)
    check.reject_unknown(document, ("profiles",), "")
    before = check.count
    entries = check.table(document, "profiles", "")
    if check.count == before and not entries:
        check.error("profiles", "must define at least one profile")

    profiles: dict[str, WarmupProfile] = {}
    for name, raw in entries.items():
        profile = _parse_profile(check, name, raw)
        if profile is not None:
            profiles[name] = profile
    if check.issues:
        raise ConfigError(check.issues)
    return profiles


def _parse_profile(check: "_Checker", name: str, raw: object) -> WarmupProfile | None:
    path = _join("profiles", name)
    before = check.count
    table = check.as_table(raw, path)
    if check.count > before:
        return None
    check.reject_unknown(table, _PROFILE_KEYS, path)
    if problem := profile_name_problem(name):
        check.error(path, problem)
    feed_start, feed_end = _parse_rising_pair(check, table, path, "feed")
    rpm_start, rpm_end = _parse_rising_pair(check, table, path, "rpm")
    defaults = _PROFILE_DEFAULTS
    profile = WarmupProfile(
        name=name,
        duration_min=check.number(table, "duration_min", path, above=0, at_most=MAX_DURATION_MIN),
        stages=check.integer(table, "stages", path, at_least=MIN_STAGES, at_most=MAX_STAGES),
        feed_start=feed_start,
        feed_end=feed_end,
        rpm_start=rpm_start,
        rpm_end=rpm_end,
        description=check.string(table, "description", path, default=defaults["description"]),
        ramp=check.choice(table, "ramp", path, Ramp, default=defaults["ramp"]),
        coolant=check.choice(table, "coolant", path, Coolant, default=defaults["coolant"]),
        edge_margin_mm=check.number(
            table,
            "edge_margin_mm",
            path,
            default=defaults["edge_margin_mm"],
            at_least=0,
            at_most=MAX_EDGE_MARGIN_MM,
        ),
        pattern=_parse_pattern(check, table, path),
        z_stroke_at=check.choice(
            table, "z_stroke_at", path, ZStrokeAt, default=defaults["z_stroke_at"]
        ),
        operator_confirm=check.boolean(
            table, "operator_confirm", path, default=defaults["operator_confirm"]
        ),
        runtime_guards=check.boolean(
            table, "runtime_guards", path, default=defaults["runtime_guards"]
        ),
        final_rapid_pass=check.boolean(
            table, "final_rapid_pass", path, default=defaults["final_rapid_pass"]
        ),
    )
    return profile if check.count == before else None


def _parse_rising_pair(
    check: "_Checker", table: Mapping[str, Any], path: str, prefix: str
) -> tuple[float, float]:
    """Read `<prefix>_start` and `<prefix>_end`; a warm-up never ramps down."""
    before = check.count
    start = check.number(table, f"{prefix}_start", path, above=0)
    end = check.number(table, f"{prefix}_end", path, above=0)
    if check.count == before and start > end:
        check.error(
            _join(path, f"{prefix}_start"),
            f"must not exceed {prefix}_end ({_fmt(end)}); the warm-up only ramps up "
            f"(got {_fmt(start)})",
        )
    return start, end


def _parse_pattern(check: "_Checker", table: Mapping[str, Any], path: str) -> tuple[SweepMove, ...]:
    before = check.count
    pattern = check.choices(table, "pattern", path, SweepMove, default=_PROFILE_DEFAULTS["pattern"])
    reaches_xy = SweepMove.PERIMETER in pattern or SweepMove.DIAGONALS in pattern
    if check.count == before and not (reaches_xy and SweepMove.Z_STROKE in pattern):
        check.error(
            _join(path, "pattern"),
            "must reach every axis extreme: include 'z_stroke' and at least one of "
            "'perimeter' or 'diagonals'",
        )
    return pattern


# --- Machine + profile -----------------------------------------------------------


def check_compatibility(machine: Machine, profile: WarmupProfile) -> list[Issue]:
    """Problems that only appear when this profile runs on this machine."""
    path = _join("profiles", profile.name)
    issues = []
    if profile.feed_end > machine.max_feed:
        issues.append(
            Issue(
                _join(path, "feed_end"),
                f"{_fmt(profile.feed_end)} mm/min exceeds the max_feed of machine "
                f"{machine.id} ({_fmt(machine.max_feed)} mm/min)",
            )
        )
    if profile.rpm_end > machine.spindle_max_rpm:
        issues.append(
            Issue(
                _join(path, "rpm_end"),
                f"{_fmt(profile.rpm_end)} rpm exceeds the spindle_max_rpm of machine "
                f"{machine.id} ({_fmt(machine.spindle_max_rpm)} rpm)",
            )
        )
    for axis in Axis:
        travel = machine.limits(axis).travel
        if travel - 2 * profile.edge_margin_mm < MIN_SWEEP_TRAVEL_MM:
            issues.append(
                Issue(
                    _join(path, "edge_margin_mm"),
                    f"{_fmt(profile.edge_margin_mm)} mm on each end leaves less than "
                    f"{MIN_SWEEP_TRAVEL_MM} mm of {axis.upper()} travel on machine {machine.id} "
                    f"({_fmt(travel)} mm)",
                )
            )
    return issues


# --- Overrides (CLI --set, UI edits) ---------------------------------------------


def apply_overrides(profile: WarmupProfile, overrides: Mapping[str, object]) -> WarmupProfile:
    """Return `profile` with some keys replaced, validated exactly like profiles.toml."""
    check = _Checker("overrides")
    result = _parse_profile(check, profile.name, profile_to_raw(profile) | dict(overrides))
    if result is None:
        raise ConfigError(check.issues)
    return result


def profile_to_raw(profile: WarmupProfile) -> dict[str, object]:
    """The profile as plain TOML-compatible values, keyed like profiles.toml."""
    raw: dict[str, object] = {}
    for field in dataclasses.fields(profile):
        if field.name == "name":
            continue
        value = getattr(profile, field.name)
        if isinstance(value, tuple):
            value = [item.value for item in value]
        elif isinstance(value, StrEnum):
            value = value.value
        raw[field.name] = value
    return raw


# --- Checking helpers ------------------------------------------------------------

_MISSING: Final = object()  # marks a required key: no default


class _Checker:
    """Reads values from parsed TOML tables, recording an Issue for each problem.

    Getters always return a value of the requested type (the default, or a
    placeholder when the input is invalid) so parsing continues and every
    problem is reported in one pass. Callers compare ``count`` before and after
    reading an entity to tell whether it is valid.
    """

    def __init__(self, source: str | None) -> None:
        self.source = source
        self.issues: list[Issue] = []

    @property
    def count(self) -> int:
        return len(self.issues)

    def error(self, path: str, message: str) -> None:
        self.issues.append(Issue(path, message, source=self.source))

    def reject_unknown(self, table: Mapping[str, Any], allowed: Iterable[str], path: str) -> None:
        allowed = list(allowed)
        for key in table:
            if key not in allowed:
                close = difflib.get_close_matches(key, allowed, n=1)
                hint = f"did you mean '{close[0]}'?" if close else f"allowed: {', '.join(allowed)}"
                self.error(_join(path, key), f"unknown key; {hint}")

    def as_table(self, value: object, path: str) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        self.error(path, f"must be a table (got {_describe(value)})")
        return {}

    def table(self, parent: Mapping[str, Any], key: str, path: str) -> dict[str, Any]:
        if key not in parent:
            self.error(_join(path, key), "missing required table")
            return {}
        return self.as_table(parent[key], _join(path, key))

    def _get(self, table: Mapping[str, Any], key: str, path: str, default: object) -> object:
        if key in table:
            return table[key]
        if default is _MISSING:
            self.error(_join(path, key), "missing required key")
        return default

    def number(
        self,
        table: Mapping[str, Any],
        key: str,
        path: str,
        *,
        default: object = _MISSING,
        above: float | None = None,
        at_least: float | None = None,
        at_most: float | None = None,
    ) -> float:
        value = self._get(table, key, path, default)
        where = _join(path, key)
        if value is _MISSING:
            return 0.0
        if isinstance(value, bool) or not isinstance(value, int | float):
            self.error(where, f"must be a number (got {_describe(value)})")
            return 0.0
        number = float(value)
        if not math.isfinite(number):
            self.error(where, f"must be a finite number (got {_describe(value)})")
        else:
            self._check_bounds(where, number, above=above, at_least=at_least, at_most=at_most)
        return number

    def integer(
        self,
        table: Mapping[str, Any],
        key: str,
        path: str,
        *,
        at_least: int | None = None,
        at_most: int | None = None,
    ) -> int:
        value = self._get(table, key, path, _MISSING)
        if value is _MISSING:
            return 0
        if isinstance(value, bool) or not isinstance(value, int):
            self.error(_join(path, key), f"must be a whole number (got {_describe(value)})")
            return 0
        self._check_bounds(_join(path, key), value, at_least=at_least, at_most=at_most)
        return value

    def _check_bounds(
        self,
        where: str,
        value: float,
        *,
        above: float | None = None,
        at_least: float | None = None,
        at_most: float | None = None,
    ) -> None:
        got = f"(got {_fmt(value)})"
        if above is not None and value <= above:
            self.error(where, f"must be greater than {_fmt(above)} {got}")
        elif at_least is not None and value < at_least:
            self.error(where, f"must be at least {_fmt(at_least)} {got}")
        elif at_most is not None and value > at_most:
            self.error(where, f"must be at most {_fmt(at_most)} {got}")

    def boolean(self, table: Mapping[str, Any], key: str, path: str, *, default: bool) -> bool:
        value = self._get(table, key, path, default)
        if isinstance(value, bool):
            return value
        self.error(_join(path, key), f"must be true or false (got {_describe(value)})")
        return default

    def string(self, table: Mapping[str, Any], key: str, path: str, *, default: str) -> str:
        value = self._get(table, key, path, default)
        if isinstance(value, str):
            return value
        self.error(_join(path, key), f"must be a string (got {_describe(value)})")
        return default

    def choice(
        self,
        table: Mapping[str, Any],
        key: str,
        path: str,
        enum_type: type[E],
        *,
        default: object = _MISSING,
    ) -> E:
        value = self._get(table, key, path, default)
        if isinstance(value, enum_type):
            return value
        if value is _MISSING:
            return next(iter(enum_type))
        return self._member(value, _join(path, key), enum_type)

    def choices(
        self,
        table: Mapping[str, Any],
        key: str,
        path: str,
        enum_type: type[E],
        *,
        default: tuple[E, ...],
    ) -> tuple[E, ...]:
        value = self._get(table, key, path, default)
        where = _join(path, key)
        if isinstance(value, tuple):
            return value
        if not isinstance(value, list):
            self.error(where, f"must be an array (got {_describe(value)})")
            return default
        members: list[E] = []
        for index, item in enumerate(value):
            before = self.count
            member = self._member(item, f"{where}[{index}]", enum_type)
            if self.count > before:
                continue
            if member in members:
                self.error(where, f"lists '{member}' more than once")
            else:
                members.append(member)
        return tuple(members)

    def _member(self, value: object, where: str, enum_type: type[E]) -> E:
        if isinstance(value, str) and value in {member.value for member in enum_type}:
            return enum_type(value)
        allowed = ", ".join(f"'{member.value}'" for member in enum_type)
        self.error(where, f"must be one of {allowed} (got {_describe(value)})")
        return next(iter(enum_type))


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _fmt(value: float) -> str:
    """Format a number for a message: 1016.0 -> '1016', 0.5 -> '0.5'."""
    number = float(value)
    return str(int(number)) if number.is_integer() else repr(number)


def _describe(value: object) -> str:
    """Describe a raw TOML value the way the user wrote it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        return "a table"
    if isinstance(value, list):
        return "an array"
    return repr(value)
