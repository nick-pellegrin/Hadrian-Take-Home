"""The NiceGUI configurator, driven by NiceGUI's simulated user (no browser needed)."""

import shutil
import tomllib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import nicegui
import pytest
from nicegui.testing import User
from nicegui.testing.user_simulation import user_simulation

from cnc_warmup.model import Controller
from cnc_warmup.ui import app
from cnc_warmup.ui.app import Editor, Settings

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    shutil.copytree(ROOT / "config", tmp_path / "config")
    return tmp_path


@pytest.fixture
async def session(project: Path) -> AsyncIterator[tuple[User, Editor]]:
    """A simulated user on the page, and the page's Editor behind it."""
    settings = Settings(project / "config", project / "out")
    editors: list[Editor] = []

    def root() -> None:
        editors.append(Editor(settings))

    async with user_simulation(root=root) as user:
        await user.open("/")
        yield user, editors[-1]


@pytest.fixture
def editor(session: tuple[User, Editor]) -> User:
    return session[0]


def element(user: User, marker: str) -> Any:
    [found] = user.find(marker=marker).elements
    return found


def change(user: User, marker: str, value: object) -> None:
    """Set an input's value the way a user edit would, firing its change handlers."""
    with user:
        element(user, marker).value = value


def text(user: User, marker: str) -> str:
    return str(element(user, marker).text)


def machines_file(project: Path) -> str:
    return (project / "config" / "machines.toml").read_text(encoding="utf-8")


def profiles_file(project: Path) -> str:
    return (project / "config" / "profiles.toml").read_text(encoding="utf-8")


# --- Opening the page ----------------------------------------------------------------------


async def test_page_opens_on_the_first_machine_and_profile_verified(editor: User) -> None:
    assert element(editor, "machine-select").value == "M1"
    assert element(editor, "profile-select").value == "daily"
    assert text(editor, "status") == "1 program(s) generated and verified against the plan"
    assert "X -761..-1" in text(editor, "overview")
    assert "Estimated run time 19:57" in text(editor, "overview")
    assert len(element(editor, "stage-table").rows) == 5
    program = element(editor, "program-heidenhain").content
    assert program.startswith("0 BEGIN PGM WARMUP_M1_DAILY MM\n")
    assert text(editor, "cli-command") == (
        "uv run cnc-warmup generate --machine M1 --profile daily --controller heidenhain"
    )


# --- Live preview and validation ------------------------------------------------------------


async def test_errors_show_under_their_field_and_block_generation(editor: User) -> None:
    change(editor, "profile-feed_end", 30000)

    assert element(editor, "profile-feed_end").error.startswith(
        "30000 mm/min exceeds the max_feed of machine M1"
    )
    assert text(editor, "status") == "1 problem(s) to fix before generating"
    assert not element(editor, "generate").enabled
    assert not element(editor, "download-heidenhain").enabled
    await editor.should_see("error: profiles.daily.feed_end: 30000 mm/min exceeds")


async def test_an_empty_field_is_reported_as_missing(editor: User) -> None:
    change(editor, "machine-travel_y", None)

    assert element(editor, "machine-travel_y").error == "missing required key"
    assert not element(editor, "machine-save").enabled


async def test_an_invalid_machine_id_is_reported_on_the_id_field(editor: User) -> None:
    change(editor, "machine-machine_id", "M 1")

    assert element(editor, "machine-machine_id").error.startswith("machine ID must be")


async def test_profile_edits_become_cli_overrides(editor: User) -> None:
    change(editor, "profile-feed_end", 11000)

    assert text(editor, "cli-command").endswith("--set feed_end=11000")
    await editor.should_see(marker="profile-unsaved")
    await editor.should_not_see(marker="machine-unsaved")


async def test_machine_edits_update_the_preview(editor: User) -> None:
    change(editor, "machine-travel_x", 900)

    assert "X -899..-1" in text(editor, "overview")
    await editor.should_see(marker="machine-unsaved")
    assert text(editor, "cli-command").startswith("Save the machine and profile")


async def test_switching_to_explicit_limits_converts_the_travel(editor: User) -> None:
    change(editor, "machine-coordinates", "limits")

    assert (element(editor, "machine-x_min").value, element(editor, "machine-x_max").value) == (
        -762,
        0,
    )
    assert "X -761..-1" in text(editor, "overview")  # the same machine, described differently

    change(editor, "machine-coordinates", "travel")

    assert (element(editor, "machine-travel_x").value, element(editor, "machine-home").value) == (
        762,
        "max",
    )


async def test_fanuc_output_is_rendered_alongside(editor: User) -> None:
    change(editor, "output-fanuc", True)

    assert text(editor, "status") == "2 program(s) generated and verified against the plan"
    assert element(editor, "program-fanuc").content.startswith("%\nO8001 (WARMUP M1 DAILY)\n")
    assert text(editor, "cli-command").endswith("--controller heidenhain fanuc")


async def test_at_least_one_controller_stays_selected(editor: User) -> None:
    change(editor, "output-heidenhain", False)

    assert element(editor, "output-heidenhain").value
    await editor.should_see("At least one controller is needed.")


# --- Saving, deleting, switching ------------------------------------------------------------


async def test_duplicate_and_save_adds_a_machine_and_keeps_the_comments(
    editor: User, project: Path
) -> None:
    editor.find(marker="machine-duplicate").click()
    assert element(editor, "machine-machine_id").value == "M1_2"
    change(editor, "machine-travel_x", 900)

    editor.find(marker="machine-save").click()
    await editor.should_see("Saved machine M1_2")

    saved = machines_file(project)
    assert "# ASSUMPTIONS - not stated in the assignment" in saved
    machine = tomllib.loads(saved)["machines"]["M1_2"]
    assert machine["travel"] == {"x": 900, "y": 508, "z": 500}
    assert machine["fanuc"] == {"program_number": 8001, "cancel_codes": ["G69"]}
    assert element(editor, "machine-select").options == ["M1", "M2", "M3", "M1_2"]
    await editor.should_not_see(marker="machine-unsaved")

    change(editor, "machine-select", "M1")
    editor.find(marker="machine-duplicate").click()
    assert element(editor, "machine-machine_id").value == "M1_3"  # M1_2 is taken now


async def test_saving_a_profile_updates_it_in_place(editor: User, project: Path) -> None:
    change(editor, "profile-feed_end", 11000)

    editor.find(marker="profile-save").click()
    await editor.should_see("Saved profile daily")

    assert "feed_end = 11000                         # mm/min, capped by machine max_feed" in (
        profiles_file(project)
    )
    assert text(editor, "cli-command").endswith("--profile daily --controller heidenhain")


async def test_replacing_another_entry_asks_first(editor: User, project: Path) -> None:
    before = machines_file(project)
    change(editor, "machine-machine_id", "M2")

    editor.find(marker="machine-save").click()
    await editor.should_see("Replace the saved machine M2?")
    editor.find(marker="confirm-cancel").click()

    await editor.should_not_see("Replace the saved machine M2?")
    assert machines_file(project) == before


async def test_deleting_a_machine_removes_it_from_the_file(editor: User, project: Path) -> None:
    change(editor, "machine-select", "M3")

    editor.find(marker="machine-delete").click()
    await editor.should_see("Delete machine M3")
    editor.find(marker="confirm-ok").click()
    await editor.should_see("Deleted machine M3.")

    assert "[machines.M3]" not in machines_file(project)
    assert element(editor, "machine-select").options == ["M1", "M2"]


async def test_switching_machines_with_unsaved_changes_asks_first(editor: User) -> None:
    change(editor, "machine-travel_x", 900)

    change(editor, "machine-select", "M2")
    await editor.should_see("Discard the unsaved changes to machine M1?")
    editor.find(marker="confirm-cancel").click()
    await editor.should_not_see("Discard the unsaved changes")
    assert element(editor, "machine-select").value == "M1"
    assert element(editor, "machine-travel_x").value == 900

    change(editor, "machine-select", "M2")
    await editor.should_see("Discard the unsaved changes to machine M1?")
    editor.find(marker="confirm-ok").click()
    await editor.should_not_see("Discard the unsaved changes")
    assert element(editor, "machine-travel_x").value == 1016


# --- Output ---------------------------------------------------------------------------------


async def test_generate_writes_the_verified_programs(editor: User, project: Path) -> None:
    editor.find(marker="generate").click()
    await editor.should_see("Wrote")

    written = project / "out" / "heidenhain" / "WARMUP_M1_DAILY.H"
    example = ROOT / "examples" / "heidenhain" / "WARMUP_M1_DAILY.H"
    assert written.read_bytes() == example.read_bytes()


async def test_download_sends_the_program(editor: User) -> None:
    editor.find(marker="download-heidenhain").click()

    response = await editor.download.next()

    example = ROOT / "examples" / "heidenhain" / "WARMUP_M1_DAILY.H"
    assert response.content == example.read_bytes()


async def test_copy_command_confirms(editor: User) -> None:
    editor.find(marker="copy-command").click()

    await editor.should_see("Command copied.")


async def test_clearing_the_description_is_a_cli_override(editor: User) -> None:
    change(editor, "profile-description", "")

    assert text(editor, "cli-command").endswith("--set 'description=\"\"'")


async def test_warnings_are_shown_but_do_not_block(editor: User) -> None:
    change(editor, "machine-select", "M3")
    change(editor, "profile-feed_start", 1000)

    assert text(editor, "status") == "1 program(s) generated and verified against the plan"
    await editor.should_see("warning: profiles.daily.feed_start: stage 1 runs 8:38")
    assert element(editor, "profile-feed_start").error is None  # warnings stay in the summary


async def test_cancelling_a_delete_keeps_the_machine(editor: User, project: Path) -> None:
    before = machines_file(project)

    editor.find(marker="machine-delete").click()
    await editor.should_see("Delete machine M1")
    editor.find(marker="confirm-cancel").click()
    await editor.should_not_see("Delete machine M1")

    assert machines_file(project) == before


async def test_actions_ignore_a_click_that_raced_the_button_being_disabled(
    session: tuple[User, Editor], project: Path
) -> None:
    user, page = session
    before = machines_file(project)
    change(user, "machine-travel_y", None)  # invalid: Save, Generate and Download are disabled

    with user:
        await page._save(page.machines)
        page._generate()
        page._download(Controller.HEIDENHAIN)
        page._copy_command()

    await user.should_see("Fix the machine first: machines.M1.travel.y: missing required key")
    assert machines_file(project) == before
    assert not (project / "out").exists()


def test_run_serves_the_editor_without_auto_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(nicegui.ui, "run", lambda root, **options: calls.append(options))

    app.run(Settings(Path("config"), Path("out")), host="127.0.0.1", port=9000, show=False)

    assert calls == [
        {"host": "127.0.0.1", "port": 9000, "show": False, "reload": False, "title": app.TITLE}
    ]
