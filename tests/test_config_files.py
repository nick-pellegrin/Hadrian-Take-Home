"""Checks on the configuration shipped in config/, loaded through the real loader."""

from pathlib import Path

import pytest

from cnc_warmup.config import Catalog, ConfigError, check_compatibility, load_catalog
from cnc_warmup.model import Axis

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"

# Travel in mm, copied from the machine table in the assignment.
ASSIGNMENT_TRAVEL = {
    "M1": {"x": 762, "y": 508, "z": 500},
    "M2": {"x": 1016, "y": 660, "z": 500},
    "M3": {"x": 1270, "y": 508, "z": 500},
}


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    return load_catalog(CONFIG_DIR)


def test_machines_match_assignment_table(catalog: Catalog) -> None:
    travel = {
        machine_id: {axis.value: machine.limits(axis).travel for axis in Axis}
        for machine_id, machine in catalog.machines.items()
    }

    assert travel == ASSIGNMENT_TRAVEL


def test_fanuc_program_numbers_are_unique(catalog: Catalog) -> None:
    numbers = [m.fanuc.program_number for m in catalog.machines.values() if m.fanuc is not None]

    assert len(numbers) == len(catalog.machines)
    assert len(set(numbers)) == len(numbers)


def test_daily_and_extended_profiles_exist(catalog: Catalog) -> None:
    assert {"daily", "extended"} <= catalog.profiles.keys()


def test_every_profile_fits_every_machine(catalog: Catalog) -> None:
    issues = [
        str(issue)
        for machine in catalog.machines.values()
        for profile in catalog.profiles.values()
        for issue in check_compatibility(machine, profile)
    ]

    assert issues == []


def test_unknown_machine_lists_the_available_ones(catalog: Catalog) -> None:
    with pytest.raises(ConfigError) as error:
        catalog.machine("M9")

    assert str(error.value) == "machine: unknown machine 'M9' (available: M1, M2, M3)"


def test_unknown_profile_lists_the_available_ones(catalog: Catalog) -> None:
    with pytest.raises(ConfigError) as error:
        catalog.profile("weekly")

    assert str(error.value) == "profile: unknown profile 'weekly' (available: daily, extended)"
