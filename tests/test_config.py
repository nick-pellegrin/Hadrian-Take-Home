import dataclasses
import math
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cnc_warmup.config import (
    ConfigError,
    apply_overrides,
    check_compatibility,
    load_catalog,
    load_machines,
    parse_machines,
    parse_profiles,
    profile_to_raw,
)
from cnc_warmup.model import (
    AxisLimits,
    Controller,
    Coolant,
    FanucCancelCode,
    FanucSettings,
    Machine,
    Ramp,
    SweepMove,
    WarmupProfile,
    ZStrokeAt,
)

REMOVE = object()  # marks a key to delete from the base table

BASE_MACHINE: dict[str, Any] = {
    "travel": {"x": 762, "y": 508, "z": 500},
    "home": "max",
    "spindle_max_rpm": 12000,
    "max_feed": 20000,
}

BASE_PROFILE: dict[str, Any] = {
    "duration_min": 20,
    "stages": 5,
    "feed_start": 2500,
    "feed_end": 12000,
    "rpm_start": 1000,
    "rpm_end": 10000,
}

EXPLICIT_LIMITS = {
    "x": {"min": -400, "max": 400},
    "y": {"min": -300, "max": 300},
    "z": {"min": -550, "max": 0},
}


def _merge(base: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    merged = {**base, **changes}
    return {key: value for key, value in merged.items() if value is not REMOVE}


def machine_doc(machine_id: str = "M1", **changes: Any) -> dict[str, Any]:
    return {"machines": {machine_id: _merge(BASE_MACHINE, changes)}}


def profile_doc(name: str = "daily", **changes: Any) -> dict[str, Any]:
    return {"profiles": {name: _merge(BASE_PROFILE, changes)}}


def issues_of(error: pytest.ExceptionInfo[ConfigError]) -> list[str]:
    return [f"{issue.path}: {issue.message}" for issue in error.value.issues]


def machine_issues(document: dict[str, Any]) -> list[str]:
    with pytest.raises(ConfigError) as error:
        parse_machines(document)
    return issues_of(error)


def profile_issues(document: dict[str, Any]) -> list[str]:
    with pytest.raises(ConfigError) as error:
        parse_profiles(document)
    return issues_of(error)


# --- Machines --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("home", "expected_x"),
    [("max", AxisLimits(-762, 0)), ("min", AxisLimits(0, 762))],
)
def test_travel_shorthand_derives_limits_from_home(home: str, expected_x: AxisLimits) -> None:
    machine = parse_machines(machine_doc(home=home))["M1"]

    assert machine.x == expected_x


def test_explicit_limits_are_used_as_given() -> None:
    document = machine_doc(travel=REMOVE, home=REMOVE, limits=EXPLICIT_LIMITS)

    machine = parse_machines(document)["M1"]

    assert (machine.x, machine.y, machine.z) == (
        AxisLimits(-400, 400),
        AxisLimits(-300, 300),
        AxisLimits(-550, 0),
    )


def test_optional_machine_keys_take_model_defaults() -> None:
    machine = parse_machines(machine_doc())["M1"]

    assert (machine.description, machine.controller, machine.fanuc) == (
        "",
        Controller.HEIDENHAIN,
        None,
    )


def test_fanuc_settings_are_parsed() -> None:
    fanuc = {"program_number": 8001, "cancel_codes": ["G69", "G50.1"]}

    machine = parse_machines(machine_doc(fanuc=fanuc))["M1"]

    assert machine.fanuc == FanucSettings(8001, (FanucCancelCode.G69, FanucCancelCode.G50_1))


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        pytest.param(
            {"max_feeds": 20000},
            "machines.M1.max_feeds: unknown key; did you mean 'max_feed'?",
            id="typo-suggests-key",
        ),
        pytest.param(
            {"spindle_max_rpm": REMOVE},
            "machines.M1.spindle_max_rpm: missing required key",
            id="missing-key",
        ),
        pytest.param(
            {"spindle_max_rpm": "12000"},
            "machines.M1.spindle_max_rpm: must be a number (got '12000')",
            id="string-for-number",
        ),
        pytest.param(
            {"max_feed": True},
            "machines.M1.max_feed: must be a number (got true)",
            id="bool-for-number",
        ),
        pytest.param(
            {"max_feed": math.inf},
            "machines.M1.max_feed: must be a finite number (got inf)",
            id="infinite",
        ),
        pytest.param(
            {"max_feed": 0},
            "machines.M1.max_feed: must be greater than 0 (got 0)",
            id="zero-feed",
        ),
        pytest.param(
            {"travel": {"x": -762, "y": 508, "z": 500}},
            "machines.M1.travel.x: must be greater than 0 (got -762)",
            id="negative-travel",
        ),
        pytest.param(
            {"travel": {"x": 762, "y": 508}},
            "machines.M1.travel.z: missing required key",
            id="missing-axis",
        ),
        pytest.param(
            {"travel": {"x": 762, "y": 508, "z": 500, "a": 360}},
            "machines.M1.travel.a: unknown key; allowed: x, y, z",
            id="unknown-axis",
        ),
        pytest.param(
            {"travel": 762},
            "machines.M1.travel: must be a table (got 762)",
            id="travel-not-table",
        ),
        pytest.param(
            {"home": REMOVE},
            "machines.M1.home: missing required key",
            id="travel-without-home",
        ),
        pytest.param(
            {"home": "top"},
            "machines.M1.home: must be one of 'max', 'min' (got 'top')",
            id="unknown-home",
        ),
        pytest.param(
            {"limits": EXPLICIT_LIMITS},
            "machines.M1: define exactly one of 'travel' (with 'home') or 'limits'",
            id="travel-and-limits",
        ),
        pytest.param(
            {"travel": REMOVE, "home": REMOVE},
            "machines.M1: define exactly one of 'travel' (with 'home') or 'limits'",
            id="no-travel-or-limits",
        ),
        pytest.param(
            {"travel": REMOVE, "limits": EXPLICIT_LIMITS},
            "machines.M1.home: only applies to 'travel'; 'limits' are already in machine "
            "coordinates",
            id="home-with-limits",
        ),
        pytest.param(
            {
                "travel": REMOVE,
                "home": REMOVE,
                "limits": {**EXPLICIT_LIMITS, "x": {"min": 0, "max": -762}},
            },
            "machines.M1.limits.x: min (0) must be less than max (-762)",
            id="inverted-limits",
        ),
        pytest.param(
            {
                "travel": REMOVE,
                "home": REMOVE,
                "limits": {"x": EXPLICIT_LIMITS["x"], "y": EXPLICIT_LIMITS["y"]},
            },
            "machines.M1.limits.z: missing required table",
            id="limits-missing-axis",
        ),
        pytest.param(
            {"controller": "siemens"},
            "machines.M1.controller: must be one of 'heidenhain', 'fanuc' (got 'siemens')",
            id="unknown-controller",
        ),
        pytest.param(
            {"description": 42},
            "machines.M1.description: must be a string (got 42)",
            id="description-not-string",
        ),
        pytest.param(
            {"max_feed": [20000]},
            "machines.M1.max_feed: must be a number (got an array)",
            id="array-for-number",
        ),
        pytest.param(
            {"description": {"text": "VMC"}},
            "machines.M1.description: must be a string (got a table)",
            id="table-for-string",
        ),
        pytest.param(
            {"travel": REMOVE, "home": REMOVE, "limits": "all"},
            "machines.M1.limits: must be a table (got 'all')",
            id="limits-not-table",
        ),
        pytest.param(
            {"fanuc": "yes"},
            "machines.M1.fanuc: must be a table (got 'yes')",
            id="fanuc-not-table",
        ),
        pytest.param(
            {"fanuc": {"program_number": 9001}},
            "machines.M1.fanuc.program_number: O9000-O9999 are reserved for machine tool "
            "builder macros; use 1-8999 (got 9001)",
            id="reserved-o-number",
        ),
        pytest.param(
            {"fanuc": {"program_number": 0}},
            "machines.M1.fanuc.program_number: must be at least 1 (got 0)",
            id="zero-o-number",
        ),
        pytest.param(
            {"fanuc": {"program_number": 8001.0}},
            "machines.M1.fanuc.program_number: must be a whole number (got 8001.0)",
            id="fractional-o-number",
        ),
        pytest.param(
            {"fanuc": {"program_number": 8001, "cancel_codes": ["G69", "G68"]}},
            "machines.M1.fanuc.cancel_codes[1]: must be one of 'G15', 'G50', 'G50.1', 'G69' "
            "(got 'G68')",
            id="unknown-cancel-code",
        ),
        pytest.param(
            {"fanuc": {"program_number": 8001, "cancel_codes": ["G69", "G69"]}},
            "machines.M1.fanuc.cancel_codes: lists 'G69' more than once",
            id="duplicate-cancel-code",
        ),
    ],
)
def test_invalid_machine_reports_one_precise_issue(changes: dict[str, Any], expected: str) -> None:
    assert machine_issues(machine_doc(**changes)) == [expected]


def test_machine_id_must_be_usable_in_program_names() -> None:
    [issue] = machine_issues(machine_doc(machine_id="M 1"))

    assert issue.startswith("machines.M 1: machine ID must be 1-16 letters")


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        pytest.param({}, ["machines: missing required table"], id="no-table"),
        pytest.param(
            {"machines": {}}, ["machines: must define at least one machine"], id="empty-table"
        ),
        pytest.param(
            {"machines": {"M1": 5}}, ["machines.M1: must be a table (got 5)"], id="entry-not-table"
        ),
        pytest.param(
            {"machine": {}},
            ["machine: unknown key; did you mean 'machines'?", "machines: missing required table"],
            id="misspelled-table",
        ),
    ],
)
def test_machines_file_structure(document: dict[str, Any], expected: list[str]) -> None:
    assert machine_issues(document) == expected


def test_all_machine_problems_are_reported_together() -> None:
    document = machine_doc(max_feed=-1, controller="siemens", travel={"x": 762, "y": 508})

    assert len(machine_issues(document)) == 3


# --- Profiles --------------------------------------------------------------------


def test_optional_profile_keys_take_model_defaults() -> None:
    profile = parse_profiles(profile_doc())["daily"]

    assert profile == WarmupProfile(
        name="daily",
        duration_min=20,
        stages=5,
        feed_start=2500,
        feed_end=12000,
        rpm_start=1000,
        rpm_end=10000,
    )


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        pytest.param(
            {"feed_ends": 1},
            "profiles.daily.feed_ends: unknown key; did you mean 'feed_end'?",
            id="typo-suggests-key",
        ),
        pytest.param(
            {"feed_end": REMOVE},
            "profiles.daily.feed_end: missing required key",
            id="missing-key",
        ),
        pytest.param(
            {"stages": REMOVE},
            "profiles.daily.stages: missing required key",
            id="missing-stages",
        ),
        pytest.param(
            {"feed_start": 13000},
            "profiles.daily.feed_start: must not exceed feed_end (12000); the warm-up only "
            "ramps up (got 13000)",
            id="feed-ramps-down",
        ),
        pytest.param(
            {"rpm_start": 11000},
            "profiles.daily.rpm_start: must not exceed rpm_end (10000); the warm-up only "
            "ramps up (got 11000)",
            id="rpm-ramps-down",
        ),
        pytest.param(
            {"stages": 1},
            "profiles.daily.stages: must be at least 2 (got 1)",
            id="too-few-stages",
        ),
        pytest.param(
            {"stages": 21},
            "profiles.daily.stages: must be at most 20 (got 21)",
            id="too-many-stages",
        ),
        pytest.param(
            {"stages": 5.0},
            "profiles.daily.stages: must be a whole number (got 5.0)",
            id="fractional-stages",
        ),
        pytest.param(
            {"duration_min": 0},
            "profiles.daily.duration_min: must be greater than 0 (got 0)",
            id="zero-duration",
        ),
        pytest.param(
            {"duration_min": 500},
            "profiles.daily.duration_min: must be at most 240 (got 500)",
            id="excessive-duration",
        ),
        pytest.param(
            {"edge_margin_mm": -1},
            "profiles.daily.edge_margin_mm: must be at least 0 (got -1)",
            id="negative-margin",
        ),
        pytest.param(
            {"edge_margin_mm": math.nan},
            "profiles.daily.edge_margin_mm: must be a finite number (got nan)",
            id="nan-margin",
        ),
        pytest.param(
            {"pattern": ["perimeter", "diagonals"]},
            "profiles.daily.pattern: must reach every axis extreme: include 'z_stroke' and at "
            "least one of 'perimeter' or 'diagonals'",
            id="pattern-misses-z",
        ),
        pytest.param(
            {"pattern": []},
            "profiles.daily.pattern: must reach every axis extreme: include 'z_stroke' and at "
            "least one of 'perimeter' or 'diagonals'",
            id="empty-pattern",
        ),
        pytest.param(
            {"pattern": ["zigzag"]},
            "profiles.daily.pattern[0]: must be one of 'perimeter', 'diagonals', 'z_stroke' "
            "(got 'zigzag')",
            id="unknown-move",
        ),
        pytest.param(
            {"pattern": ["z_stroke", "perimeter", "z_stroke"]},
            "profiles.daily.pattern: lists 'z_stroke' more than once",
            id="duplicate-move",
        ),
        pytest.param(
            {"pattern": "perimeter"},
            "profiles.daily.pattern: must be an array (got 'perimeter')",
            id="pattern-not-array",
        ),
        pytest.param(
            {"coolant": "mist"},
            "profiles.daily.coolant: must be one of 'off', 'flood' (got 'mist')",
            id="unknown-coolant",
        ),
        pytest.param(
            {"ramp": "exponential"},
            "profiles.daily.ramp: must be one of 'linear', 'geometric' (got 'exponential')",
            id="unknown-ramp",
        ),
        pytest.param(
            {"operator_confirm": "yes"},
            "profiles.daily.operator_confirm: must be true or false (got 'yes')",
            id="string-for-bool",
        ),
    ],
)
def test_invalid_profile_reports_one_precise_issue(changes: dict[str, Any], expected: str) -> None:
    assert profile_issues(profile_doc(**changes)) == [expected]


def test_profile_name_must_be_lowercase_identifier() -> None:
    [issue] = profile_issues(profile_doc(name="Daily"))

    assert issue.startswith("profiles.Daily: profile name must be")


def test_profile_must_be_a_table() -> None:
    assert profile_issues({"profiles": {"daily": 5}}) == ["profiles.daily: must be a table (got 5)"]


# Hypothesis strategy for profiles that pass validation.
_positive = st.floats(min_value=0.001, max_value=1e5, allow_nan=False, allow_infinity=False)
_xy_moves = st.lists(
    st.sampled_from([SweepMove.PERIMETER, SweepMove.DIAGONALS]), min_size=1, max_size=2, unique=True
)


@st.composite
def valid_profiles(draw: st.DrawFn) -> WarmupProfile:
    feed_start, feed_end = sorted(draw(st.tuples(_positive, _positive)))
    rpm_start, rpm_end = sorted(draw(st.tuples(_positive, _positive)))
    pattern = draw(st.permutations([*draw(_xy_moves), SweepMove.Z_STROKE]))
    return WarmupProfile(
        name=draw(st.from_regex(r"[a-z][a-z0-9_]{0,31}", fullmatch=True)),
        duration_min=draw(st.floats(min_value=0.01, max_value=240)),
        stages=draw(st.integers(min_value=2, max_value=20)),
        feed_start=feed_start,
        feed_end=feed_end,
        rpm_start=rpm_start,
        rpm_end=rpm_end,
        description=draw(st.text(max_size=40)),
        ramp=draw(st.sampled_from(Ramp)),
        coolant=draw(st.sampled_from(Coolant)),
        edge_margin_mm=draw(st.floats(min_value=0, max_value=25)),
        pattern=tuple(pattern),
        z_stroke_at=draw(st.sampled_from(ZStrokeAt)),
        operator_confirm=draw(st.booleans()),
        runtime_guards=draw(st.booleans()),
        final_rapid_pass=draw(st.booleans()),
    )


@given(valid_profiles())
def test_valid_profiles_round_trip_through_raw_values(profile: WarmupProfile) -> None:
    document = {"profiles": {profile.name: profile_to_raw(profile)}}

    assert parse_profiles(document)[profile.name] == profile
    assert apply_overrides(profile, {}) == profile


# --- Machine + profile -----------------------------------------------------------


def test_compatibility_flags_what_the_machine_cannot_do() -> None:
    machine = Machine(
        id="SMALL",
        x=AxisLimits(-400, 0),
        y=AxisLimits(-300, 0),
        z=AxisLimits(-40, 0),
        spindle_max_rpm=8000,
        max_feed=10000,
    )
    profile = parse_profiles(profile_doc(edge_margin_mm=25))["daily"]

    issues = [f"{issue.path}: {issue.message}" for issue in check_compatibility(machine, profile)]

    assert issues == [
        "profiles.daily.feed_end: 12000 mm/min exceeds the max_feed of machine SMALL "
        "(10000 mm/min)",
        "profiles.daily.rpm_end: 10000 rpm exceeds the spindle_max_rpm of machine SMALL (8000 rpm)",
        "profiles.daily.edge_margin_mm: 25 mm on each end leaves no Z travel on machine SMALL "
        "(40 mm)",
    ]


# --- Overrides -------------------------------------------------------------------


def test_overrides_replace_only_the_given_keys() -> None:
    profile = parse_profiles(profile_doc())["daily"]

    result = apply_overrides(profile, {"feed_end": 8000, "coolant": "flood"})

    assert result == dataclasses.replace(profile, feed_end=8000, coolant=Coolant.FLOOD)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        pytest.param(
            {"stages": 0},
            "overrides: profiles.daily.stages: must be at least 2 (got 0)",
            id="invalid-value",
        ),
        pytest.param(
            {"feed_ends": 8000},
            "overrides: profiles.daily.feed_ends: unknown key; did you mean 'feed_end'?",
            id="unknown-key",
        ),
    ],
)
def test_invalid_overrides_are_rejected(overrides: dict[str, object], expected: str) -> None:
    profile = parse_profiles(profile_doc())["daily"]

    with pytest.raises(ConfigError) as error:
        apply_overrides(profile, overrides)

    assert str(error.value) == expected


# --- Files -----------------------------------------------------------------------


def test_profiles_file_must_define_a_profile() -> None:
    assert profile_issues({"profiles": {}}) == ["profiles: must define at least one profile"]


def test_non_utf8_file_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "machines.toml"
    path.write_bytes(b"description = '\xff'\n")

    with pytest.raises(ConfigError) as error:
        load_machines(path)

    assert str(error.value) == f"{path.as_posix()}: file is not valid UTF-8 text"


def test_unreadable_file_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "machines.toml"
    path.mkdir()  # a directory cannot be read as a file

    with pytest.raises(ConfigError) as error:
        load_machines(path)

    assert str(error.value).startswith(f"{path.as_posix()}: cannot read file: ")


def test_catalog_reports_problems_in_both_files(tmp_path: Path) -> None:
    (tmp_path / "machines.toml").write_text("[machines.M1\n", encoding="utf-8")

    with pytest.raises(ConfigError) as error:
        load_catalog(tmp_path)

    machines_issue, profiles_issue = (str(issue) for issue in error.value.issues)
    assert machines_issue.startswith(f"{(tmp_path / 'machines.toml').as_posix()}: invalid TOML")
    assert profiles_issue == f"{(tmp_path / 'profiles.toml').as_posix()}: file not found"


def test_file_issues_name_their_source(tmp_path: Path) -> None:
    (tmp_path / "machines.toml").write_text(
        '[machines.M1]\ntravel = { x = 762, y = 508, z = 500 }\nhome = "max"\n'
        "spindle_max_rpm = 12000\nmax_feed = -1\n",
        encoding="utf-8",
    )
    (tmp_path / "profiles.toml").write_text(
        "[profiles.daily]\nduration_min = 20\nstages = 5\nfeed_start = 2500\n"
        "feed_end = 12000\nrpm_start = 1000\nrpm_end = 10000\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as error:
        load_catalog(tmp_path)

    assert str(error.value) == (
        f"{(tmp_path / 'machines.toml').as_posix()}: machines.M1.max_feed: "
        "must be greater than 0 (got -1)"
    )
