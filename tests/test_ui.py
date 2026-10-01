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


async def name_in_dialog(user: User, button: str, name: str) -> None:
    """Open a rename/duplicate dialog, type ``name`` and confirm it."""
    user.find(marker=button).click()
    await user.should_see(marker="name-input")
    change(user, "name-input", name)
    user.find(marker="name-ok").click()
    await user.should_not_see(marker="name-input")


def machines_file(project: Path) -> str:
    return (project / "config" / "machines.toml").read_text(encoding="utf-8")


def profiles_file(project: Path) -> str:
    return (project / "config" / "profiles.toml").read_text(encoding="utf-8")


# --- Opening the page ----------------------------------------------------------------------


async def test_page_opens_on_the_first_machine_and_profile_verified(editor: User) -> None:
    assert element(editor, "machine-select").value == "M1"
    assert element(editor, "profile-select").value == "daily"
    assert (text(editor, "machine-title"), text(editor, "profile-title")) == (
        "Machine M1",
        "Warm-up profile daily",
    )
    await editor.should_not_see(marker="save")  # the Save/Discard footer: nothing to save
    assert not element(editor, "machine-save").enabled  # nor in the cards' menus
    assert element(editor, "output-drawer").value is False  # the output starts closed
    await editor.should_not_see(marker="problem-count")
    [colors] = editor.find(kind=nicegui.ui.colors).elements
    assert colors.props["primary"] == "#002548"  # Hadrian's navy
    assert text(editor, "status") == "1 program(s) generated and verified against the plan"
    assert "X -761..-1" in text(editor, "overview")
    assert "Estimated run time 19:57" in text(editor, "overview")
    assert len(element(editor, "stage-table").rows) == 5
    program = element(editor, "program-heidenhain").content
    assert program.startswith("0 BEGIN PGM WARMUP_M1_DAILY MM\n")
    assert text(editor, "cli-command") == (
        "uv run cnc-warmup generate --machine M1 --profile daily --controller heidenhain"
    )


@pytest.fixture
def m1_defaults_to_fanuc(project: Path) -> None:
    machines = project / "config" / "machines.toml"
    data = machines.read_bytes()
    machines.write_bytes(data.replace(b'controller = "heidenhain"', b'controller = "fanuc"', 1))


async def test_output_starts_as_heidenhain_and_saving_keeps_the_machine_controller(
    m1_defaults_to_fanuc: None, editor: User, project: Path
) -> None:
    assert element(editor, "output-heidenhain").value
    assert not element(editor, "output-fanuc").value

    change(editor, "machine-travel_x", 900)
    editor.find(marker="save").click()
    await editor.should_see("Saved machine M1.")

    assert tomllib.loads(machines_file(project))["machines"]["M1"]["controller"] == "fanuc"


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
    await editor.should_see(marker="problem-count")  # shown on the closed drawer's icon
    assert text(editor, "problem-count") == "1"


async def test_the_output_button_opens_and_closes_the_drawer(editor: User) -> None:
    assert text(editor, "output-toggle") == "Output"
    editor.find(marker="output-toggle").click()
    assert element(editor, "output-drawer").value is True

    editor.find(marker="output-close").click()
    assert element(editor, "output-drawer").value is False


async def test_an_empty_field_is_reported_as_missing(editor: User) -> None:
    change(editor, "machine-travel_y", None)

    assert element(editor, "machine-travel_y").error == "missing required key"
    await editor.should_see(marker="save")
    assert not element(editor, "save").enabled


async def test_rename_dialog_checks_the_name_as_you_type(editor: User) -> None:
    editor.find(marker="machine-rename").click()
    await editor.should_see("Rename machine M1")
    assert element(editor, "name-input").value == "M1"

    change(editor, "name-input", "M 1")
    assert element(editor, "name-input").error.startswith("Machine ID must be 1-16 letters")
    assert not element(editor, "name-ok").enabled

    change(editor, "name-input", "M2")
    assert element(editor, "name-input").error == "Another saved machine is already called M2."
    assert not element(editor, "name-ok").enabled

    change(editor, "name-input", "MILL_A")
    assert element(editor, "name-input").error is None
    assert element(editor, "name-ok").enabled


async def test_profile_edits_become_cli_overrides(editor: User) -> None:
    change(editor, "profile-feed_end", 11000)

    assert text(editor, "cli-command").endswith("--set feed_end=11000")
    await editor.should_see(marker="profile-unsaved")
    await editor.should_not_see(marker="machine-unsaved")
    assert text(editor, "unsaved-summary") == "Unsaved changes to profile daily"


async def test_machine_edits_update_the_preview(editor: User) -> None:
    change(editor, "machine-travel_x", 900)

    assert "X -899..-1" in text(editor, "overview")
    await editor.should_see(marker="machine-unsaved")
    assert text(editor, "cli-command").startswith("Save your changes")


async def test_custom_machine_zero_converts_the_travel_to_limits(editor: User) -> None:
    assert element(editor, "machine-zero").value == "max"
    change(editor, "machine-zero", "limits")

    await editor.should_see(marker="machine-x_min")
    await editor.should_not_see(marker="machine-travel_x")
    assert (element(editor, "machine-x_min").value, element(editor, "machine-x_max").value) == (
        -762,
        0,
    )
    assert "X -761..-1" in text(editor, "overview")  # the same machine, described differently

    change(editor, "machine-zero", "max")

    assert element(editor, "machine-travel_x").value == 762
    await editor.should_not_see(marker="machine-x_min")
    await editor.should_not_see(marker="machine-unsaved")  # back to what the file says


async def test_machine_zero_at_the_minus_end_moves_the_envelope(editor: User) -> None:
    change(editor, "machine-zero", "min")

    assert "X 1..761" in text(editor, "overview")
    await editor.should_see(marker="machine-unsaved")


async def test_fanuc_output_is_rendered_alongside(editor: User) -> None:
    change(editor, "output-fanuc", True)

    assert text(editor, "status") == "2 program(s) generated and verified against the plan"
    assert element(editor, "program-fanuc").content.startswith("%\nO8001 (WARMUP M1 DAILY)\n")
    assert text(editor, "cli-command").endswith("--controller heidenhain fanuc")


async def test_at_least_one_controller_stays_selected(editor: User) -> None:
    change(editor, "output-heidenhain", False)

    assert element(editor, "output-heidenhain").value
    await editor.should_see("At least one controller is needed.")


async def test_fanuc_output_needs_a_machine_that_can_run_fanuc(editor: User) -> None:
    fanuc, tooltip = element(editor, "output-fanuc"), element(editor, "fanuc-unavailable")
    assert fanuc.enabled
    assert "disable" in tooltip.props  # no explanation needed
    change(editor, "output-fanuc", True)
    change(editor, "output-heidenhain", False)  # Fanuc only

    change(editor, "machine-fanuc", False)

    assert (fanuc.value, fanuc.enabled) == (False, False)
    assert element(editor, "output-heidenhain").value  # one output stays selected
    assert "disable" not in tooltip.props  # hovering explains where to turn Fanuc on
    assert tooltip.text == app.FANUC_UNAVAILABLE
    assert text(editor, "filename-fanuc") == app.FANUC_UNAVAILABLE  # in the Output drawer too
    assert text(editor, "status") == "1 program(s) generated and verified against the plan"

    change(editor, "machine-fanuc", True)

    assert (fanuc.value, fanuc.enabled) == (False, True)  # pick it again if wanted
    assert "disable" in tooltip.props


# --- Saving, deleting, switching ------------------------------------------------------------


async def test_duplicate_and_save_adds_a_machine_and_keeps_the_comments(
    editor: User, project: Path
) -> None:
    editor.find(marker="machine-duplicate").click()
    await editor.should_see("Duplicate machine M1 as")
    assert element(editor, "name-input").value == "M1_2"  # a free name is suggested
    editor.find(marker="name-ok").click()
    await editor.should_not_see(marker="name-input")
    assert text(editor, "machine-title") == "Machine M1_2"
    assert text(editor, "machine-unsaved") == "new, not saved yet"
    assert not element(editor, "machine-delete").enabled  # nothing in the file to delete yet
    change(editor, "machine-travel_x", 900)

    editor.find(marker="save").click()
    await editor.should_see("Saved machine M1_2.")

    saved = machines_file(project)
    assert "# ASSUMPTIONS - not stated in the assignment" in saved
    machine = tomllib.loads(saved)["machines"]["M1_2"]
    assert machine["travel"] == {"x": 900, "y": 508, "z": 500}
    assert machine["fanuc"] == {"program_number": 8001, "cancel_codes": ["G69"]}
    assert element(editor, "machine-select").options == ["M1", "M2", "M3", "M1_2"]
    await editor.should_not_see(marker="machine-unsaved")

    assert "[machines.M1]" in saved  # the original is untouched

    change(editor, "machine-select", "M1")
    editor.find(marker="machine-duplicate").click()
    await editor.should_see("Duplicate machine M1 as")
    assert element(editor, "name-input").value == "M1_3"  # M1_2 is taken now
    editor.find(marker="name-cancel").click()
    await editor.should_not_see(marker="name-input")
    assert text(editor, "machine-title") == "Machine M1"


async def test_saving_a_profile_updates_it_in_place(editor: User, project: Path) -> None:
    change(editor, "profile-feed_end", 11000)

    editor.find(marker="save").click()
    await editor.should_see("Saved profile daily.")

    assert "feed_end = 11000                         # mm/min, capped by machine max_feed" in (
        profiles_file(project)
    )
    assert text(editor, "cli-command").endswith("--profile daily --controller heidenhain")


async def test_renaming_a_machine_renames_it_in_place_on_save(editor: User, project: Path) -> None:
    before = machines_file(project)

    await name_in_dialog(editor, "machine-rename", "MILL_A")

    assert text(editor, "machine-title") == "Machine MILL_A"
    assert text(editor, "machine-unsaved") == "renamed from M1, not saved yet"
    assert "WARMUP_MILL_A_DAILY.H" in text(editor, "filename-heidenhain")
    assert machines_file(project) == before  # nothing written until Save

    editor.find(marker="save").click()
    await editor.should_see("Saved machine MILL_A.")

    expected = before.replace("[machines.M1]", "[machines.MILL_A]").replace(
        "[machines.M1.fanuc]", "[machines.MILL_A.fanuc]"
    )
    assert machines_file(project) == expected  # only the headers change: same place, comments
    assert element(editor, "machine-select").options == ["MILL_A", "M2", "M3"]
    await editor.should_not_see(marker="machine-unsaved")


async def test_renaming_a_profile_renames_it_on_save(editor: User, project: Path) -> None:
    await name_in_dialog(editor, "profile-rename", "morning")
    editor.find(marker="save").click()
    await editor.should_see("Saved profile morning.")

    profiles = tomllib.loads(profiles_file(project))["profiles"]
    assert list(profiles) == ["morning", "extended"]
    assert text(editor, "cli-command").startswith(
        "uv run cnc-warmup generate --machine M1 --profile morning"
    )


async def test_enter_does_not_accept_an_invalid_name_and_cancel_changes_nothing(
    editor: User,
) -> None:
    editor.find(marker="machine-rename").click()
    await editor.should_see("Rename machine M1")
    change(editor, "name-input", "M 1")

    editor.find(marker="name-input").trigger("keydown.enter")
    await editor.should_see(marker="name-input")  # still open: the name is invalid

    editor.find(marker="name-cancel").click()
    await editor.should_not_see(marker="name-input")
    assert text(editor, "machine-title") == "Machine M1"
    await editor.should_not_see(marker="machine-unsaved")


async def test_a_rename_can_go_back_to_the_saved_name(editor: User) -> None:
    await name_in_dialog(editor, "machine-rename", "MILL_A")

    editor.find(marker="machine-rename").click()
    await editor.should_see("Rename machine MILL_A")
    change(editor, "name-input", "M1")  # the entry's own saved name is allowed
    assert element(editor, "name-input").error is None
    editor.find(marker="name-input").trigger("keydown.enter")  # Enter confirms
    await editor.should_not_see(marker="name-input")

    await editor.should_not_see(marker="machine-unsaved")


async def test_deleting_a_machine_removes_it_from_the_file(editor: User, project: Path) -> None:
    change(editor, "machine-select", "M3")

    editor.find(marker="machine-delete").click()
    await editor.should_see("Delete machine M3")
    editor.find(marker="confirm-ok").click()
    await editor.should_see("Deleted machine M3.")

    assert "[machines.M3]" not in machines_file(project)
    assert element(editor, "machine-select").options == ["M1", "M2"]


async def test_a_cards_menu_saves_just_that_card(editor: User, project: Path) -> None:
    profiles = profiles_file(project)
    change(editor, "machine-travel_x", 900)
    change(editor, "profile-feed_end", 11000)

    editor.find(marker="machine-save").click()
    await editor.should_see("Saved machine M1.")

    assert tomllib.loads(machines_file(project))["machines"]["M1"]["travel"]["x"] == 900
    assert profiles_file(project) == profiles
    assert text(editor, "unsaved-summary") == "Unsaved changes to profile daily"
    assert not element(editor, "machine-save").enabled
    assert element(editor, "profile-save").enabled


async def test_a_cards_menu_cannot_save_an_invalid_entry(editor: User) -> None:
    change(editor, "profile-stages", None)

    assert not element(editor, "profile-save").enabled
    assert not element(editor, "machine-save").enabled  # unchanged


async def test_save_changes_saves_the_machine_and_the_profile_together(
    editor: User, project: Path
) -> None:
    change(editor, "machine-travel_x", 900)
    change(editor, "profile-feed_end", 11000)
    assert text(editor, "unsaved-summary") == "Unsaved changes to machine M1 and profile daily"

    editor.find(marker="save").click()
    await editor.should_see("Saved machine M1 and profile daily.")

    assert tomllib.loads(machines_file(project))["machines"]["M1"]["travel"]["x"] == 900
    assert tomllib.loads(profiles_file(project))["profiles"]["daily"]["feed_end"] == 11000
    await editor.should_not_see(marker="save")


async def test_save_changes_saves_nothing_if_either_is_invalid(editor: User, project: Path) -> None:
    before = machines_file(project), profiles_file(project)
    change(editor, "machine-travel_x", 900)
    change(editor, "profile-stages", None)  # a profile on its own can be invalid

    assert not element(editor, "save").enabled
    assert (machines_file(project), profiles_file(project)) == before


async def test_discard_restores_the_saved_entries(editor: User, project: Path) -> None:
    before = machines_file(project)
    await name_in_dialog(editor, "machine-duplicate", "MILL_B")
    change(editor, "profile-feed_end", 11000)

    editor.find(marker="discard").click()
    await editor.should_see("Discard all unsaved changes?")
    editor.find(marker="confirm-cancel").click()
    await editor.should_not_see("Discard all unsaved changes?")
    assert text(editor, "machine-title") == "Machine MILL_B"  # cancelling keeps the edits

    editor.find(marker="discard").click()
    await editor.should_see("Discard all unsaved changes?")
    editor.find(marker="confirm-ok").click()
    await editor.should_see("Unsaved changes discarded.")

    assert text(editor, "machine-title") == "Machine M1"
    assert element(editor, "profile-feed_end").value == 12000
    await editor.should_not_see(marker="save")
    assert machines_file(project) == before


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


async def test_generate_writes_the_verified_programs_and_shows_them(
    editor: User, project: Path
) -> None:
    editor.find(marker="generate").click()
    await editor.should_see("Wrote")

    written = project / "out" / "heidenhain" / "WARMUP_M1_DAILY.H"
    example = ROOT / "examples" / "heidenhain" / "WARMUP_M1_DAILY.H"
    assert written.read_bytes() == example.read_bytes()
    assert element(editor, "output-drawer").value is True  # the output opens


async def test_the_shown_and_generated_programs_include_unsaved_edits(
    editor: User, project: Path
) -> None:
    change(editor, "profile-feed_end", 11000)  # not saved

    shown = element(editor, "program-heidenhain").markdown.content  # the text on the page
    assert "FEED 2500-11000 MM/MIN" in shown
    editor.find(marker="generate").click()
    await editor.should_see("Wrote")

    written = project / "out" / "heidenhain" / "WARMUP_M1_DAILY.H"
    assert "FEED 2500-11000 MM/MIN" in written.read_text(encoding="utf-8")
    assert tomllib.loads(profiles_file(project))["profiles"]["daily"]["feed_end"] == 12000


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
        page._save_changes()
        page._generate()
        page._download(Controller.HEIDENHAIN)
        page._copy_command()

    await user.should_see("Fix the machine first: machines.M1.travel.y: missing required key")
    assert machines_file(project) == before
    assert not (project / "out").exists()


async def test_save_refuses_a_name_another_entry_already_has(
    session: tuple[User, Editor], project: Path
) -> None:
    user, page = session
    before = machines_file(project)
    page.machine.machine_id = "M2"  # the rename dialog never allows this; guard anyway

    with user:
        page._save_changes()

    await user.should_see("Another saved machine is already called M2.")
    assert machines_file(project) == before


async def test_save_with_nothing_changed_does_nothing(
    session: tuple[User, Editor], project: Path
) -> None:
    user, page = session
    before = machines_file(project)

    with user:
        page._save_changes()  # e.g. a click that raced the footer hiding

    assert not user.notify.contains("Saved")
    assert machines_file(project) == before


def test_run_serves_the_editor_without_auto_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(nicegui.ui, "run", lambda root, **options: calls.append(options))

    app.run(Settings(Path("config"), Path("out")), host="127.0.0.1", port=9000, show=False)

    assert calls == [
        {"host": "127.0.0.1", "port": 9000, "show": False, "reload": False, "title": app.TITLE}
    ]
