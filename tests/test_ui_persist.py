"""Saving UI edits to the TOML files: comments, layout and line endings survive."""

import shutil
import tomllib
from pathlib import Path

import pytest

from cnc_warmup.config import load_catalog
from cnc_warmup.ui.persist import delete_entry, read_tables, save_entry

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


@pytest.fixture
def config(tmp_path: Path) -> Path:
    shutil.copytree(CONFIG_DIR, tmp_path / "config")
    return tmp_path / "config"


def changed_lines(before: str, after: str) -> list[tuple[str, str]]:
    return [
        (old, new)
        for old, new in zip(before.splitlines(), after.splitlines(), strict=True)
        if old != new
    ]


def test_read_tables_returns_plain_values(config: Path) -> None:
    tables = read_tables(config / "machines.toml", "machines")

    assert list(tables) == ["M1", "M2", "M3"]
    assert tables["M1"]["fanuc"] == {"program_number": 8001, "cancel_codes": ["G69"]}
    assert type(tables["M1"]["travel"]) is dict


def test_updating_an_entry_rewrites_only_the_changed_values(config: Path) -> None:
    path = config / "machines.toml"
    before = path.read_text(encoding="utf-8")
    table = read_tables(path, "machines")["M1"]
    table["max_feed"] = 25000
    table["fanuc"] = {"program_number": 8101, "cancel_codes": ["G69"]}

    save_entry(path, "machines", "M1", table)

    assert changed_lines(before, path.read_text(encoding="utf-8")) == [
        ("max_feed = 20000", "max_feed = 25000"),
        (
            "program_number = 8001                    # O8001 - the O8000-O8999 range can be "
            "edit-protected",
            "program_number = 8101                    # O8001 - the O8000-O8999 range can be "
            "edit-protected",
        ),
    ]


def test_inline_tables_keep_their_style(config: Path) -> None:
    path = config / "machines.toml"
    table = read_tables(path, "machines")["M1"]
    table["travel"] = {"x": 900, "y": 508, "z": 500}

    save_entry(path, "machines", "M1", table)

    assert "travel = { x = 900, y = 508, z = 500 }" in path.read_text(encoding="utf-8")


def test_a_new_entry_matches_the_files_layout_and_loads(config: Path) -> None:
    path = config / "machines.toml"
    new_machine = {
        "description": "Test machine",
        "controller": "heidenhain",
        "limits": {
            "x": {"min": -400, "max": 400},
            "y": {"min": -300, "max": 300},
            "z": {"min": -550, "max": 0},
        },
        "spindle_max_rpm": 15000,
        "max_feed": 30000,
        "fanuc": {"program_number": 8004, "cancel_codes": ["G69"]},
    }

    save_entry(path, "machines", "M4", new_machine)

    text = path.read_text(encoding="utf-8")
    assert "\n[machines.M4]\n" in text
    assert "\n[machines.M4.fanuc]\n" in text
    assert "limits = {x = {min = -400, max = 400}" in text
    assert "# ASSUMPTIONS - not stated in the assignment" in text
    assert load_catalog(config).machine("M4").x.max == 400


def test_switching_travel_to_limits_replaces_the_keys(config: Path) -> None:
    path = config / "machines.toml"
    table = read_tables(path, "machines")["M2"]
    del table["travel"], table["home"]
    table["limits"] = {
        "x": {"min": -1016, "max": 0},
        "y": {"min": -660, "max": 0},
        "z": {"min": -500, "max": 0},
    }

    save_entry(path, "machines", "M2", table)

    saved = tomllib.loads(path.read_text(encoding="utf-8"))["machines"]["M2"]
    assert "travel" not in saved
    assert "home" not in saved
    assert load_catalog(config).machine("M2") == load_catalog(CONFIG_DIR).machine("M2")


def test_deleting_an_entry_removes_its_tables(config: Path) -> None:
    path = config / "machines.toml"

    delete_entry(path, "machines", "M3")

    text = path.read_text(encoding="utf-8")
    assert "[machines.M3]" not in text
    assert "[machines.M3.fanuc]" not in text
    assert list(read_tables(path, "machines")) == ["M1", "M2"]


def test_saving_a_profile_keeps_its_comments(config: Path) -> None:
    path = config / "profiles.toml"
    table = read_tables(path, "profiles")["daily"]
    table["coolant"] = "flood"

    save_entry(path, "profiles", "daily", table)

    assert (
        'coolant = "flood"                          # "off" or "flood" - flood with an empty '
        "spindle sprays the enclosure\n"
    ) in path.read_text(encoding="utf-8")


def test_a_missing_section_is_created(tmp_path: Path) -> None:
    path = tmp_path / "profiles.toml"
    path.write_text("# Profiles\n", encoding="utf-8")

    save_entry(path, "profiles", "quick", {"duration_min": 5})

    assert tomllib.loads(path.read_text(encoding="utf-8")) == {
        "profiles": {"quick": {"duration_min": 5}}
    }


def test_crlf_line_endings_are_kept(config: Path) -> None:
    path = config / "profiles.toml"
    path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    table = read_tables(path, "profiles")["daily"]
    table["stages"] = 6

    save_entry(path, "profiles", "daily", table)

    data = path.read_bytes()
    assert b"stages = 6" in data
    assert data.count(b"\n") == data.count(b"\r\n")


@pytest.mark.parametrize("old", ["M1", "M2", "M3"])
def test_renaming_changes_only_the_header_lines(config: Path, old: str) -> None:
    path = config / "machines.toml"
    before = path.read_text(encoding="utf-8")
    table = read_tables(path, "machines")[old]

    save_entry(path, "machines", "MILL_X", table, renamed_from=old)

    expected = before.replace(f"[machines.{old}]", "[machines.MILL_X]").replace(
        f"[machines.{old}.fanuc]", "[machines.MILL_X.fanuc]"
    )
    assert path.read_text(encoding="utf-8") == expected
    assert list(read_tables(path, "machines")) == [
        "MILL_X" if key == old else key for key in ("M1", "M2", "M3")
    ]


def test_a_rename_and_an_edit_are_saved_together(config: Path) -> None:
    path = config / "machines.toml"
    table = read_tables(path, "machines")["M2"]
    table["max_feed"] = 25000

    save_entry(path, "machines", "MILL_B", table, renamed_from="M2")

    tables = read_tables(path, "machines")
    assert list(tables) == ["M1", "MILL_B", "M3"]
    assert tables["MILL_B"]["max_feed"] == 25000
    assert "[machines.M2]" not in path.read_text(encoding="utf-8")


def test_renaming_a_profile_keeps_its_comments(config: Path) -> None:
    path = config / "profiles.toml"
    table = read_tables(path, "profiles")["daily"]

    save_entry(path, "profiles", "morning", table, renamed_from="daily")

    text = path.read_text(encoding="utf-8")
    assert "[profiles.morning]" in text
    assert "duration_min = 20                        # Haas and Mazak recommend 20-30 min" in text
    assert list(read_tables(path, "profiles")) == ["morning", "extended"]


def test_renaming_in_a_file_without_section_headers_moves_the_entry(tmp_path: Path) -> None:
    path = tmp_path / "machines.toml"
    path.write_text(
        "[machines]\n"
        'A = { travel = { x = 100, y = 100, z = 100 }, home = "max" }  # first\n'
        'B = { travel = { x = 200, y = 200, z = 200 }, home = "max" }\n',
        encoding="utf-8",
    )
    table = read_tables(path, "machines")["A"]

    save_entry(path, "machines", "RENAMED", table, renamed_from="A")

    tables = read_tables(path, "machines")
    assert list(tables) == ["B", "RENAMED"]  # kept its contents, but not its place
    assert tables["RENAMED"]["travel"] == {"x": 100, "y": 100, "z": 100}
