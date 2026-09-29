"""Checks on the shipped configuration data (not the config loader)."""

import tomllib
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"

# Travel in mm, copied from the machine table in the assignment.
ASSIGNMENT_TRAVEL = {
    "M1": {"x": 762, "y": 508, "z": 500},
    "M2": {"x": 1016, "y": 660, "z": 500},
    "M3": {"x": 1270, "y": 508, "z": 500},
}


def load(name: str) -> dict[str, Any]:
    return tomllib.loads((CONFIG_DIR / name).read_text(encoding="utf-8"))


def test_machines_match_assignment_table() -> None:
    machines = load("machines.toml")["machines"]

    assert {machine_id: m["travel"] for machine_id, m in machines.items()} == ASSIGNMENT_TRAVEL


def test_fanuc_program_numbers_are_unique() -> None:
    machines = load("machines.toml")["machines"].values()
    numbers = [m["fanuc"]["program_number"] for m in machines]

    assert len(numbers) == len(set(numbers))


def test_daily_and_extended_profiles_exist() -> None:
    assert {"daily", "extended"} <= load("profiles.toml")["profiles"].keys()
