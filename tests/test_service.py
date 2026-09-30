import shlex
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cnc_warmup import service
from cnc_warmup.cli import build_parser
from cnc_warmup.config import Catalog
from cnc_warmup.issues import Severity
from cnc_warmup.model import Controller
from cnc_warmup.plan import WarmupPlan
from cnc_warmup.posts import render
from cnc_warmup.posts.base import Program
from cnc_warmup.service import (
    GenerationRequest,
    Preview,
    cli_command,
    preview,
    toml_literal,
    write_programs,
)

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
BOTH = (Controller.HEIDENHAIN, Controller.FANUC)


def request(
    machine_id: str = "M1",
    profile_name: str = "daily",
    overrides: dict[str, object] | None = None,
    controllers: tuple[Controller, ...] = (),
) -> GenerationRequest:
    return GenerationRequest(machine_id, profile_name, overrides or {}, controllers)


def messages(result: Preview) -> list[str]:
    return [f"{issue.path}: {issue.message}" for issue in result.issues]


def test_preview_plans_renders_and_verifies(catalog: Catalog) -> None:
    result = preview(catalog, request(controllers=BOTH))

    assert result.plan is not None
    assert [program.filename for program in result.programs] == [
        "WARMUP_M1_DAILY.H",
        "O8001_M1_DAILY.nc",
    ]
    assert result.issues == ()


def test_preview_defaults_to_the_machines_controller(catalog: Catalog) -> None:
    result = preview(catalog, request())

    assert [program.controller for program in result.programs] == [Controller.HEIDENHAIN]


@pytest.mark.parametrize(
    ("invalid", "expected"),
    [
        (request(machine_id="M9"), "machine: unknown machine 'M9' (available: M1, M2, M3)"),
        (request(profile_name="weekly"), "profile: unknown profile 'weekly'"),
        (request(overrides={"stages": 1}), "profiles.daily.stages: must be at least 2 (got 1)"),
        (request(overrides={"feed_end": 30000}), "profiles.daily.feed_end: 30000 mm/min exceeds"),
    ],
)
def test_invalid_requests_come_back_as_errors(
    catalog: Catalog, invalid: GenerationRequest, expected: str
) -> None:
    result = preview(catalog, invalid)

    assert result.plan is None
    assert result.programs == ()
    assert messages(result)[0].startswith(expected)


def test_fanuc_needs_the_machines_fanuc_settings(catalog: Catalog) -> None:
    machines = {**catalog.machines, "M1": replace(catalog.machine("M1"), fanuc=None)}
    without_fanuc = Catalog(machines, catalog.profiles)

    result = preview(without_fanuc, request(controllers=(Controller.FANUC,)))

    assert messages(result)[0].startswith("machines.M1.fanuc: missing")


def test_warnings_do_not_stop_generation(catalog: Catalog) -> None:
    result = preview(catalog, request(machine_id="M3", overrides={"feed_start": 1000}))

    assert result.errors == ()
    assert [issue.severity for issue in result.warnings] == [Severity.WARNING]
    assert result.programs


def test_a_program_that_fails_verification_is_an_error(
    catalog: Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    def buggy_post(plan: WarmupPlan, controller: Controller) -> Program:
        program = render(plan, controller)
        return replace(program, text=program.text.replace("QL1 = 4880", "QL1 = 4800"))

    monkeypatch.setattr(service, "render", buggy_post)
    result = preview(catalog, request())

    assert result.errors
    assert {error.path for error in result.errors} == {"programs.WARMUP_M1_DAILY.H"}
    assert result.errors[0].message.startswith("failed verification: WARMUP_M1_DAILY.H: step ")


def test_write_programs_puts_each_controller_in_its_own_folder(
    catalog: Catalog, tmp_path: Path
) -> None:
    result = preview(catalog, request(controllers=BOTH))

    paths = write_programs(result, tmp_path)

    assert paths == [
        tmp_path / "heidenhain" / "WARMUP_M1_DAILY.H",
        tmp_path / "fanuc" / "O8001_M1_DAILY.nc",
    ]
    for path in paths:
        assert path.read_bytes() == (EXAMPLES_DIR / path.relative_to(tmp_path)).read_bytes()


def test_write_programs_refuses_a_preview_with_errors(catalog: Catalog, tmp_path: Path) -> None:
    result = preview(catalog, request(machine_id="M9"))

    with pytest.raises(ValueError, match="refusing to write"):
        write_programs(result, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_cli_command_reproduces_the_request(catalog: Catalog) -> None:
    original = request(
        controllers=BOTH,
        overrides={
            "feed_end": 8000,
            "edge_margin_mm": 1.5,
            "coolant": "flood",
            "final_rapid_pass": True,
            "pattern": ["perimeter", "z_stroke"],
            "description": "two words",
        },
    )

    command = cli_command(original)
    args = build_parser().parse_args(shlex.split(command)[3:])  # drop "uv run cnc-warmup"

    assert command == (
        "uv run cnc-warmup generate --machine M1 --profile daily --controller heidenhain fanuc "
        "--set feed_end=8000 --set edge_margin_mm=1.5 --set coolant=flood "
        '--set final_rapid_pass=true --set \'pattern=["perimeter", "z_stroke"]\' '
        "--set 'description=\"two words\"'"
    )
    assert (args.machine, args.profile, args.controller) == (
        ["M1"],
        ["daily"],
        [Controller.HEIDENHAIN, Controller.FANUC],
    )
    assert dict(args.overrides) == original.overrides


_override_values = st.one_of(
    st.booleans(),
    st.integers(min_value=-(10**12), max_value=10**12),
    st.floats(allow_nan=False),
    st.text(),
    st.lists(st.text(), max_size=3),
)


@given(_override_values)
def test_override_values_survive_the_command_line(value: object) -> None:
    command = cli_command(request(overrides={"key": value}))
    [override] = build_parser().parse_args(shlex.split(command)[3:]).overrides

    assert override == ("key", value)


@given(
    st.one_of(
        st.integers(min_value=-(2**63), max_value=2**63 - 1),  # TOML integers are 64-bit
        st.floats(allow_nan=False),
        st.text(),
        st.booleans(),
    )
)
def test_quoted_literals_are_valid_toml(value: object) -> None:
    literal = toml_literal(value, bare_strings=False)

    assert tomllib.loads(f"value = {literal}")["value"] == value


def test_unsupported_override_values_are_rejected() -> None:
    with pytest.raises(TypeError, match="cannot write"):
        toml_literal({"not": "a scalar"})
