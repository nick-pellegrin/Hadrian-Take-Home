"""The UI's form models: flat, widget-friendly fields <-> TOML tables.

No NiceGUI here, so the conversions are testable on their own. A form never
validates anything itself: `to_table()` produces exactly what would go into the
TOML file, and the core validates that the same way it validates the files.
Empty fields are left out of the table, so they come back as "missing required
key" issues on the right field.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from typing import Any

from cnc_warmup.config import parse_profiles, profile_to_raw
from cnc_warmup.issues import Issue
from cnc_warmup.model import Axis, Controller, Home

TRAVEL = "travel"  # coordinates given as travel + home (the machines.toml shorthand)
LIMITS = "limits"  # coordinates given as explicit min/max per axis
MACHINE, PROFILE = "machine", "profile"

Number = float | int | None  # what a number field holds; None when it is empty


@dataclass
class MachineForm:
    machine_id: str = ""
    description: str = ""
    controller: str = Controller.HEIDENHAIN.value
    coordinates: str = TRAVEL
    home: str = Home.MAX.value
    travel_x: Number = None
    travel_y: Number = None
    travel_z: Number = None
    x_min: Number = None
    x_max: Number = None
    y_min: Number = None
    y_max: Number = None
    z_min: Number = None
    z_max: Number = None
    spindle_max_rpm: Number = None
    max_feed: Number = None
    fanuc: bool = False
    program_number: Number = None
    cancel_codes: list[str] = field(default_factory=list)

    @classmethod
    def from_table(cls, machine_id: str, table: Mapping[str, Any]) -> "MachineForm":
        """Fill the form from a machines.toml table, keeping its travel-or-limits style."""
        form = cls(
            machine_id=machine_id,
            description=table.get("description", ""),
            controller=table.get("controller", Controller.HEIDENHAIN.value),
            spindle_max_rpm=table.get("spindle_max_rpm"),
            max_feed=table.get("max_feed"),
        )
        if "limits" in table:
            form.coordinates = LIMITS
            for axis in Axis:
                bounds = table["limits"].get(axis.value, {})
                setattr(form, f"{axis.value}_min", bounds.get("min"))
                setattr(form, f"{axis.value}_max", bounds.get("max"))
        else:
            form.home = table.get("home", Home.MAX.value)
            for axis in Axis:
                setattr(form, f"travel_{axis.value}", table.get("travel", {}).get(axis.value))
        if "fanuc" in table:
            form.fanuc = True
            form.program_number = table["fanuc"].get("program_number")
            form.cancel_codes = list(table["fanuc"].get("cancel_codes", []))
        return form

    def to_table(self) -> dict[str, object]:
        """The machines.toml table this form describes, in the file's key order."""
        table: dict[str, object] = {}
        if self.description:
            table["description"] = self.description
        table["controller"] = self.controller
        if self.coordinates == LIMITS:
            table["limits"] = {
                axis.value: _present(
                    {
                        "min": getattr(self, f"{axis.value}_min"),
                        "max": getattr(self, f"{axis.value}_max"),
                    }
                )
                for axis in Axis
            }
        else:
            table["travel"] = _present(
                {axis.value: getattr(self, f"travel_{axis.value}") for axis in Axis}
            )
            table["home"] = self.home
        table |= _present({"spindle_max_rpm": self.spindle_max_rpm, "max_feed": self.max_feed})
        if self.fanuc:
            fanuc: dict[str, object] = _present({"program_number": self.program_number})
            fanuc["cancel_codes"] = list(self.cancel_codes)
            table["fanuc"] = fanuc
        return table

    def use_limits(self) -> None:
        """Switch to explicit limits, starting from what travel + home describe."""
        self.coordinates = LIMITS
        for axis in Axis:
            travel = getattr(self, f"travel_{axis.value}")
            if travel is None:
                continue
            low, high = (-travel, 0) if self.home == Home.MAX.value else (0, travel)
            setattr(self, f"{axis.value}_min", _tidy(low))
            setattr(self, f"{axis.value}_max", _tidy(high))

    def use_travel(self) -> None:
        """Switch to travel + home, keeping the travel lengths of the limits."""
        self.coordinates = TRAVEL
        for axis in Axis:
            low, high = getattr(self, f"{axis.value}_min"), getattr(self, f"{axis.value}_max")
            if low is not None and high is not None:
                setattr(self, f"travel_{axis.value}", _tidy(high - low))
        if self.x_min == 0:
            self.home = Home.MIN.value
        elif self.x_max == 0:
            self.home = Home.MAX.value


@dataclass
class ProfileForm:
    name: str = ""
    description: str = ""
    duration_min: Number = None
    stages: Number = None
    feed_start: Number = None
    feed_end: Number = None
    rpm_start: Number = None
    rpm_end: Number = None
    ramp: str = ""
    coolant: str = ""
    edge_margin_mm: Number = None
    pattern: list[str] = field(default_factory=list)
    z_stroke_at: str = ""
    operator_confirm: bool = True
    runtime_guards: bool = True
    final_rapid_pass: bool = False

    @classmethod
    def from_table(cls, name: str, table: Mapping[str, Any]) -> "ProfileForm":
        """Fill the form from a profiles.toml table, with defaults for keys it leaves out."""
        profile = parse_profiles({"profiles": {name: dict(table)}}, source=None)[name]
        form = cls(name=name)
        for key, value in profile_to_raw(profile).items():
            setattr(form, key, value)
        return form

    def to_table(self) -> dict[str, object]:
        """The profiles.toml table this form describes."""
        table: dict[str, object] = {}
        for item in fields(self):
            value = getattr(self, item.name)
            if item.name == "name" or value is None or (item.name == "description" and not value):
                continue
            table[item.name] = list(value) if isinstance(value, list) else _tidy(value)
        return table


def field_for(issue: Issue) -> tuple[str, str] | None:
    """The form and field an issue belongs to, e.g. ("machine", "travel_x")."""
    kind, _, rest = issue.path.partition(".")
    _, _, key_path = rest.partition(".")  # drop the machine ID or profile name
    keys = [key.split("[")[0] for key in key_path.split(".")] if key_path else []  # x[1] -> x
    if kind == "profiles":
        return PROFILE, keys[0] if keys else "name"
    if kind != "machines":
        return None
    match keys:
        case []:
            return MACHINE, "machine_id"
        case ["travel", axis]:
            return MACHINE, f"travel_{axis}"
        case ["limits", axis]:
            return MACHINE, f"{axis}_min"
        case ["limits", axis, bound]:
            return MACHINE, f"{axis}_{bound}"
        case ["fanuc", key]:
            return MACHINE, key
        case _:
            return MACHINE, keys[0]


def _present(values: dict[str, object]) -> dict[str, object]:
    """Drop empty fields, so validation reports them as missing."""
    return {key: _tidy(value) for key, value in values.items() if value is not None}


def _tidy(value: object) -> object:
    """Whole floats become ints, so the TOML reads 762 rather than 762.0."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value
