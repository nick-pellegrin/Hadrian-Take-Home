import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from cnc_warmup import __version__
from cnc_warmup.cli import main, parse_override

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scratch working directory holding a copy of the shipped config/."""
    shutil.copytree(ROOT / "config", tmp_path / "config")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    status = main(list(argv))
    out, err = capsys.readouterr()
    return status, out, err


def test_version_flag_prints_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"cnc-warmup {__version__}"


def test_no_arguments_prints_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert capsys.readouterr().out.startswith("usage: cnc-warmup")


def test_module_entry_point_runs() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "cnc_warmup", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == f"cnc-warmup {__version__}"


def test_list_shows_machines_and_profiles(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status, out, _ = run(capsys, "list")

    assert status == 0
    assert "M1  heidenhain  -762..0   -508..0  -500..0  12000 rpm    20000 mm/min  O8001" in out
    assert "daily     20 min    5       1000-10000  2500-12000   linear     off" in out


def test_show_plan_prints_the_stage_table(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status, out, err = run(capsys, "show-plan", "--machine", "M1")

    assert status == 0
    assert "5      10000  12000  7       0:31       20 s   3:59" in out
    assert "Estimated run time 19:57, plus rapid positioning." in out
    assert err == ""


def test_show_plan_reports_warnings_on_stderr(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status, _, err = run(capsys, "show-plan", "--machine", "M3", "--set", "feed_start=1000")

    assert status == 0
    assert err.startswith("warning: profiles.daily.feed_start: stage 1 runs 8:38")


def test_show_plan_refuses_what_the_machine_cannot_do(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status, out, err = run(capsys, "show-plan", "--machine", "M1", "--set", "feed_end=30000")

    assert status == 1
    assert out == ""
    assert err.startswith("error: profiles.daily.feed_end: 30000 mm/min exceeds the max_feed")


def test_generate_writes_the_machines_controller_by_default(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status, out, _ = run(capsys, "generate", "--machine", "M1")

    assert status == 0
    written = sorted(path.relative_to(project).as_posix() for path in project.rglob("*.*"))
    assert [path for path in written if path.startswith("out/")] == [
        "out/heidenhain/WARMUP_M1_DAILY.H"
    ]
    example = ROOT / "examples" / "heidenhain" / "WARMUP_M1_DAILY.H"
    assert (project / "out/heidenhain/WARMUP_M1_DAILY.H").read_bytes() == example.read_bytes()
    assert "Wrote 1 program to out/." in out


def test_generate_all_with_one_profile(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    status, _, _ = run(capsys, "generate", "--all", "--profile", "daily", "-o", "programs")

    assert status == 0
    assert sorted(path.name for path in (project / "programs" / "heidenhain").iterdir()) == [
        "WARMUP_M1_DAILY.H",
        "WARMUP_M2_DAILY.H",
        "WARMUP_M3_DAILY.H",
    ]


def test_generate_writes_nothing_if_any_request_fails(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status, _, err = run(capsys, "generate", "--machine", "M1", "M9")

    assert status == 1
    assert err == (
        "Nothing written.\nerror: machine: unknown machine 'M9' (available: M1, M2, M3)\n"
    )
    assert not (project / "out").exists()


def test_validate_checks_every_machine_and_profile(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status, out, _ = run(capsys, "validate")

    assert status == 0
    assert out == (
        "Configuration OK: 3 machines, 2 profiles. All 12 programs plan, render and verify.\n"
    )


def test_validate_fails_on_a_profile_a_machine_cannot_run(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profiles = project / "config" / "profiles.toml"
    profiles.write_text(
        profiles.read_text(encoding="utf-8").replace("feed_end = 12000", "feed_end = 25000", 1),
        encoding="utf-8",
    )

    status, _, err = run(capsys, "validate")

    assert status == 1
    assert err.count("error: profiles.daily.feed_end: 25000 mm/min exceeds") == 3


def test_broken_configuration_is_reported(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status, _, err = run(capsys, "list", "--config", "nowhere")

    assert status == 1
    assert err == (
        "error: nowhere/machines.toml: file not found\n"
        "error: nowhere/profiles.toml: file not found\n"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("feed_end=12000", ("feed_end", 12000)),
        ("edge_margin_mm = 1.5", ("edge_margin_mm", 1.5)),
        ("final_rapid_pass=true", ("final_rapid_pass", True)),
        ("coolant=flood", ("coolant", "flood")),
        ('pattern=["perimeter", "z_stroke"]', ("pattern", ["perimeter", "z_stroke"])),
        ('description="a = b"', ("description", "a = b")),
    ],
)
def test_parse_override(text: str, expected: tuple[str, object]) -> None:
    assert parse_override(text) == expected


@pytest.mark.parametrize("text", ["feed_end", "=12000"])
def test_parse_override_needs_key_and_value(text: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="expected KEY=VALUE"):
        parse_override(text)


def test_bad_override_syntax_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["show-plan", "--machine", "M1", "--set", "feed_end"])

    assert exit_info.value.code == 2
    assert "argument --set: expected KEY=VALUE, got 'feed_end'" in capsys.readouterr().err
