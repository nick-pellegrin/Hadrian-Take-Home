"""The UI's form models: TOML table <-> form conversions and issue-to-field mapping."""

from pathlib import Path

import pytest

from cnc_warmup.issues import Issue
from cnc_warmup.ui.form import LIMITS, MACHINE, PROFILE, TRAVEL, MachineForm, ProfileForm, field_for
from cnc_warmup.ui.persist import read_tables

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
LIMITS_TABLE = {
    "description": "Datum at the table center",
    "controller": "fanuc",
    "limits": {
        "x": {"min": -400, "max": 400},
        "y": {"min": -300, "max": 300},
        "z": {"min": -550, "max": 0},
    },
    "spindle_max_rpm": 15000,
    "max_feed": 30000,
}


@pytest.mark.parametrize("machine_id", ["M1", "M2", "M3"])
def test_shipped_machines_round_trip_through_the_form(machine_id: str) -> None:
    table = read_tables(CONFIG_DIR / "machines.toml", "machines")[machine_id]

    form = MachineForm.from_table(machine_id, table)

    assert form.coordinates == TRAVEL
    assert form.to_table() == table


def test_explicit_limits_round_trip_through_the_form() -> None:
    form = MachineForm.from_table("M4", LIMITS_TABLE)

    assert (form.coordinates, form.x_min, form.x_max, form.fanuc) == (LIMITS, -400, 400, False)
    assert form.to_table() == LIMITS_TABLE


def test_empty_fields_are_left_out_so_validation_reports_them() -> None:
    form = MachineForm.from_table("M1", {"travel": {"x": 762, "y": 508, "z": 500}, "home": "max"})
    form.travel_y = None

    assert form.to_table() == {
        "controller": "heidenhain",
        "travel": {"x": 762, "z": 500},
        "home": "max",
    }


def test_whole_numbers_are_written_as_integers() -> None:
    form = MachineForm(machine_id="M1", travel_x=762.0, travel_y=508.5, travel_z=500.0)

    assert form.to_table()["travel"] == {"x": 762, "y": 508.5, "z": 500}


@pytest.mark.parametrize(
    ("home", "expected"),
    [("max", (-762, 0)), ("min", (0, 762))],
)
def test_switching_to_limits_starts_from_travel_and_home(
    home: str, expected: tuple[float, float]
) -> None:
    form = MachineForm(travel_x=762, travel_y=508, travel_z=500, home=home)

    form.use_limits()

    assert form.coordinates == LIMITS
    assert (form.x_min, form.x_max) == expected


@pytest.mark.parametrize(("limits", "home"), [((-762, 0), "max"), ((0, 762), "min")])
def test_switching_back_to_travel_keeps_the_lengths(limits: tuple[float, float], home: str) -> None:
    form = MachineForm(coordinates=LIMITS, x_min=limits[0], x_max=limits[1], y_min=0, y_max=10)

    form.use_travel()

    assert (form.coordinates, form.travel_x, form.travel_y, form.home) == (TRAVEL, 762, 10, home)


def test_incomplete_travel_is_not_converted() -> None:
    form = MachineForm(travel_x=None)

    form.use_limits()

    assert (form.x_min, form.x_max) == (None, None)


def test_profile_form_fills_defaults_for_keys_the_file_leaves_out() -> None:
    table = {
        "duration_min": 20,
        "stages": 5,
        "feed_start": 2500,
        "feed_end": 12000,
        "rpm_start": 1000,
        "rpm_end": 10000,
    }

    form = ProfileForm.from_table("short", table)

    assert (form.name, form.ramp, form.coolant, form.pattern) == (
        "short",
        "linear",
        "off",
        ["perimeter", "diagonals", "z_stroke"],
    )
    assert form.to_table() == {
        **table,
        "duration_min": 20,
        "ramp": "linear",
        "coolant": "off",
        "edge_margin_mm": 1,
        "pattern": ["perimeter", "diagonals", "z_stroke"],
        "z_stroke_at": "center",
        "operator_confirm": True,
        "runtime_guards": True,
        "final_rapid_pass": False,
    }


def test_shipped_profiles_round_trip_through_the_form() -> None:
    tables = read_tables(CONFIG_DIR / "profiles.toml", "profiles")

    for name, table in tables.items():
        assert ProfileForm.from_table(name, table).to_table() == table


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("machines.M1", (MACHINE, "machine_id")),
        ("machines.M1.description", (MACHINE, "description")),
        ("machines.M1.travel.x", (MACHINE, "travel_x")),
        ("machines.M1.limits.y", (MACHINE, "y_min")),
        ("machines.M1.limits.z.max", (MACHINE, "z_max")),
        ("machines.M1.fanuc.program_number", (MACHINE, "program_number")),
        ("machines.M1.fanuc.cancel_codes[1]", (MACHINE, "cancel_codes")),
        ("machines.M1.fanuc", (MACHINE, "fanuc")),
        ("profiles.daily", (PROFILE, "name")),
        ("profiles.daily.feed_end", (PROFILE, "feed_end")),
        ("profiles.daily.pattern[0]", (PROFILE, "pattern")),
        ("programs.WARMUP_M1_DAILY.H", None),
        ("machine", None),
    ],
)
def test_issues_map_to_form_fields(path: str, expected: tuple[str, str] | None) -> None:
    assert field_for(Issue(path, "message")) == expected


def test_switching_back_to_travel_keeps_home_if_neither_end_is_zero() -> None:
    form = MachineForm(coordinates=LIMITS, home="min", x_min=-400, x_max=400)

    form.use_travel()

    assert (form.travel_x, form.home) == (800, "min")
