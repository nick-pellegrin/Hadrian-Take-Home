"""NiceGUI configurator: edit machines and warm-up profiles, preview, and generate.

Everything the page shows comes from `service.preview_config`: the same
validation, planning, rendering and round-trip verification the CLI uses. The UI
holds no generation logic of its own. Edits stay in the page until saved, and
saving writes machines.toml / profiles.toml with their comments intact.

The page follows three steps (1 Machine, 2 Warm-up profile, 3 Generate), as cards
side by side. Each card shows only the common settings; everything else sits in a
collapsed "Advanced" section. The output (summary, then the generated programs) is
in a drawer on the right, opened from the Output button in the header. One Save changes /
Discard pair in the footer covers both cards, and appears only when something is
unsaved. Each card's ⋮ menu can also save just that card.
"""

from collections.abc import Callable, Coroutine, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from nicegui import ui
from nicegui.elements.mixins.validation_element import ValidationElement
from nicegui.elements.mixins.value_element import ValueElement

from cnc_warmup.config import (
    MACHINES_FILE,
    PROFILES_FILE,
    ConfigError,
    machine_id_problem,
    parse_machines,
    parse_profiles,
    profile_name_problem,
    profile_to_raw,
)
from cnc_warmup.issues import Issue, Severity
from cnc_warmup.model import Axis, Controller, FanucCancelCode
from cnc_warmup.plan import format_duration, format_number
from cnc_warmup.posts.base import Program
from cnc_warmup.service import (
    GenerationRequest,
    Preview,
    cli_command,
    preview_config,
    write_programs,
)
from cnc_warmup.ui import persist
from cnc_warmup.ui.form import LIMITS, MACHINE, PROFILE, TRAVEL, MachineForm, ProfileForm, field_for

TITLE = "CNC Warm-Up Generator"
PRIMARY_COLOR = "#002548"  # Hadrian's navy
# Cards are 320-400 px wide, so all three still fit beside the open drawer on a 1920 px screen.
CARD_SIZE = "grow basis-[320px] max-w-[400px]"
ADVANCED = "w-full mt-auto"  # the Advanced section sits at the bottom of its card
DRAWER_WIDTH = 860
# Quasar's desktop tooltips are 10 px; use the 14 px it gives small screens everywhere, and
# wrap long tooltips instead of letting them run across the page.
TOOLTIP_CSS = ".q-tooltip--style { font-size: 14px; max-width: 320px; overflow-wrap: anywhere; }"
CONTROLLER_NAMES = {Controller.HEIDENHAIN: "Heidenhain TNC 640", Controller.FANUC: "Fanuc 31i"}
FANUC_UNAVAILABLE = (
    "This machine isn't set up for Fanuc 31i. To generate Fanuc programs, turn on "
    '"This machine can run Fanuc 31i programs" in the machine\'s Advanced settings.'
)
ZERO_OPTIONS = {
    "max": "At the + end of each axis (most VMCs): coordinates run from -travel to 0",
    "min": "At the - end of each axis: coordinates run from 0 to +travel",
    LIMITS: "Somewhere else: enter each axis's min and max",
}


@dataclass(frozen=True)
class Settings:
    config_dir: Path
    out_dir: Path


def run(settings: Settings, *, host: str, port: int, show: bool) -> None:
    """Serve the configurator until stopped (Ctrl+C)."""
    ui.run(lambda: Editor(settings), host=host, port=port, show=show, reload=False, title=TITLE)


@dataclass
class _Kind:
    """What differs between editing machines and editing profiles."""

    key: str  # MACHINE or PROFILE: which form an issue belongs to
    noun: str  # in messages: "machine", "profile"
    title: str  # on the card: "Machine", "Warm-up profile"
    section: str  # the TOML table the entries live under
    path: Path
    form: MachineForm | ProfileForm
    id_field: str  # the form field holding the entry's ID
    parse: Callable[..., Mapping[str, object]]
    name_problem: Callable[[str], str | None]  # the config's naming rule
    saved: dict[str, dict[str, Any]] = field(default_factory=dict)
    loaded: str = ""  # the saved entry the form was loaded from

    @property
    def entry_id(self) -> str:
        return str(getattr(self.form, self.id_field))

    def dirty(self) -> bool:
        saved = type(self.form).from_table(self.loaded, self.saved[self.loaded])
        return self.entry_id != self.loaded or self.form.to_table() != saved.to_table()

    def name_error(self, name: str, *, allowed: str = "") -> str | None:
        """Why ``name`` can't be used for this entry: the naming rule, or already taken.

        ``allowed`` is a saved name that is fine to reuse (the entry's own, when renaming).
        """
        if problem := self.name_problem(name):
            return problem[0].upper() + problem[1:]
        if name in self.saved and name != allowed:
            return f"Another saved {self.noun} is already called {name}."
        return None

    def problems(self) -> list[Issue]:
        """Issues in this entry on its own (a machine or profile can be saved without the other)."""
        try:
            self.parse({self.section: {self.entry_id: self.form.to_table()}}, source=None)
        except ConfigError as error:
            return list(error.issues)
        return []


@dataclass(frozen=True)
class _EntryBar:
    """A card's title, unsaved-changes badge, saved-entry select, and its menu's items."""

    title: ui.label
    badge: ui.badge
    select: ui.select
    save: ui.menu_item
    duplicate: ui.menu_item
    delete: ui.menu_item


class Editor:
    """One browser tab: the machine and profile forms, the live preview, and the actions."""

    drawer: ui.right_drawer  # the output panel; the header's icon is built before it

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.machine, self.profile = MachineForm(), ProfileForm()
        self.machines = _Kind(
            MACHINE,
            "machine",
            "Machine",
            "machines",
            settings.config_dir / MACHINES_FILE,
            self.machine,
            "machine_id",
            parse_machines,
            machine_id_problem,
        )
        self.profiles = _Kind(
            PROFILE,
            "profile",
            "Warm-up profile",
            "profiles",
            settings.config_dir / PROFILES_FILE,
            self.profile,
            "name",
            parse_profiles,
            profile_name_problem,
        )
        self.inputs: dict[tuple[str, str], ValueElement[Any]] = {}
        self.outputs: dict[Controller, ui.checkbox] = {}
        self.bars: dict[str, _EntryBar] = {}
        self.result: Preview | None = None
        self._loading = False  # set while the code fills the inputs, to ignore their events

        for kind in (self.machines, self.profiles):
            kind.saved = persist.read_tables(kind.path, kind.section)
        self._build()
        self._load(self.machines, next(iter(self.machines.saved)))
        self._load(self.profiles, next(iter(self.profiles.saved)))
        with self._quiet():
            self.outputs[Controller.HEIDENHAIN].value = True  # Generate's checkboxes pick the rest
        self._refresh()

    # --- Layout ------------------------------------------------------------------------------

    def _build(self) -> None:
        ui.colors(primary=PRIMARY_COLOR)
        ui.add_css(TOOLTIP_CSS)
        with ui.header().classes("items-center justify-between"):
            ui.label(TITLE).classes("text-xl font-bold")
            with (
                ui.button("Output", icon="description", on_click=lambda: self.drawer.toggle())
                .props("flat no-caps color=white")
                .tooltip("Summary and generated programs")
                .mark("output-toggle")
            ):
                self.problem_count = (
                    ui.badge(color="negative").props("floating").mark("problem-count")
                )
        # The page content fills the space between header and footer, so the cards can be
        # centered vertically.
        ui.context.client.content.classes("justify-center").style("min-height: inherit")
        # The three steps side by side, all as tall as the tallest, wrapping when the window
        # (or the open drawer) leaves too little room.
        with ui.row().classes("w-full justify-center items-stretch gap-4"):
            self._machine_card()
            self._profile_card()
            self._generate_card()
        with ui.right_drawer(value=False, bordered=True).props(
            f"width={DRAWER_WIDTH}"
        ) as self.drawer:
            self._output_panel()
        self.drawer.mark("output-drawer")
        with ui.footer().classes("items-center justify-end gap-4") as self.footer:
            self.unsaved = ui.label().mark("unsaved-summary")
            ui.button("Discard", on_click=self._discard).props("flat color=white").mark("discard")
            self.save_button = (
                ui.button("Save changes", icon="save", on_click=self._save_changes)
                .props("color=white text-color=primary")
                .mark("save")
            )

    def _card_header(self, kind: _Kind, step: int) -> None:
        with ui.row().classes("w-full items-center no-wrap"):
            ui.label(f"{step}").classes(
                "rounded-full bg-primary text-white w-7 h-7 text-center leading-7 font-bold"
            )
            title = ui.label().classes("text-lg font-bold").mark(f"{kind.key}-title")
            badge = ui.badge(color="orange").mark(f"{kind.key}-unsaved")
        with ui.row().classes("w-full items-center no-wrap"):
            select = (
                ui.select(
                    [],
                    label=f"Saved {kind.noun}s",
                    on_change=lambda e: self._on_select(kind, e.value),
                )
                .classes("grow")
                .mark(f"{kind.key}-select")
            )
            ui.button(icon="edit", on_click=lambda: self._rename(kind)).props("flat round").tooltip(
                f"Rename this {kind.noun}"
            ).mark(f"{kind.key}-rename")
            more = ui.button(icon="more_vert").props("flat round").tooltip("More actions")
            with more, ui.menu():
                save = ui.menu_item("Save", on_click=lambda: self._save((kind,))).mark(
                    f"{kind.key}-save"
                )
                duplicate = ui.menu_item(
                    "Duplicate...", on_click=lambda: self._duplicate(kind)
                ).mark(f"{kind.key}-duplicate")
                delete = ui.menu_item("Delete...", on_click=lambda: self._delete(kind)).mark(
                    f"{kind.key}-delete"
                )
        self.bars[kind.key] = _EntryBar(title, badge, select, save, duplicate, delete)

    def _machine_card(self) -> None:
        with ui.card().classes(CARD_SIZE):
            self._card_header(self.machines, 1)
            self._text(MACHINE, "description", "Description").classes("w-full")
            with ui.row().classes("w-full no-wrap") as self.travel_box:
                for axis in Axis:
                    self._number(MACHINE, f"travel_{axis}", f"{axis.upper()} travel", suffix="mm")
            with ui.column().classes("w-full gap-0") as self.limits_box:
                for axis in Axis:
                    with ui.row().classes("w-full no-wrap"):
                        self._number(MACHINE, f"{axis}_min", f"{axis.upper()} min", suffix="mm")
                        self._number(MACHINE, f"{axis}_max", f"{axis.upper()} max", suffix="mm")
            with ui.row().classes("w-full no-wrap"):
                self._number(MACHINE, "spindle_max_rpm", "Spindle max", suffix="rpm").tooltip(
                    "The highest speed a warm-up may use on this machine."
                )
                self._number(MACHINE, "max_feed", "Max feed", suffix="mm/min").tooltip(
                    "The highest feed a warm-up may use on this machine."
                )
            with ui.expansion("Advanced", icon="tune").classes(ADVANCED).mark("machine-advanced"):
                ui.label("Where is machine zero? (Heidenhain M91 / Fanuc G53 coordinates)").classes(
                    "text-sm font-medium"
                )
                self.zero = ui.radio(ZERO_OPTIONS, on_change=self._on_zero).mark("machine-zero")
                self._switch(MACHINE, "fanuc", "This machine can run Fanuc 31i programs").classes(
                    "mt-2"
                )
                with ui.row().classes("w-full no-wrap") as self.fanuc_box:
                    self._number(
                        MACHINE, "program_number", "Program number", prefix="O", precision=0
                    )
                    self._select(
                        MACHINE,
                        "cancel_codes",
                        [code.value for code in FanucCancelCode],
                        "Cancel codes",
                    ).classes("grow").tooltip(
                        "Only the codes this control has options for: others raise alarms."
                    )

    def _profile_card(self) -> None:
        with ui.card().classes(CARD_SIZE):
            self._card_header(self.profiles, 2)
            self._text(PROFILE, "description", "Description").classes("w-full")
            with ui.row().classes("w-full no-wrap"):
                self._number(PROFILE, "feed_start", "Feed at start", suffix="mm/min")
                self._number(PROFILE, "feed_end", "Feed at finish", suffix="mm/min")
            with ui.row().classes("w-full no-wrap"):
                self._number(PROFILE, "rpm_start", "Spindle at start", suffix="rpm").tooltip(
                    "Never start a cold spindle at high speed."
                )
                self._number(PROFILE, "rpm_end", "Spindle at finish", suffix="rpm")
            with ui.row().classes("w-full items-center"):  # in a narrow card, coolant wraps below
                self._number(PROFILE, "duration_min", "Duration", suffix="min").classes(
                    "basis-[120px]"
                )
                self._choice(PROFILE, "coolant", {"off": "Coolant off", "flood": "Flood coolant"})
            with ui.expansion("Advanced", icon="tune").classes(ADVANCED).mark("profile-advanced"):
                with ui.row().classes("w-full no-wrap"):
                    self._number(PROFILE, "stages", "Stages", precision=0).tooltip(
                        "Steps from start to finish. Each holds its speed for duration / stages."
                    )
                    self._number(PROFILE, "edge_margin_mm", "Edge margin", suffix="mm").tooltip(
                        "Stops this far inside every limit, so servo overshoot can't trip an "
                        "overtravel alarm."
                    )
                ui.label("How speeds step up from stage to stage").classes("text-sm font-medium")
                self._choice(
                    PROFILE,
                    "ramp",
                    {"linear": "Equal steps", "geometric": "Smaller steps at low speed"},
                    spread=True,
                )
                self._select(
                    PROFILE,
                    "pattern",
                    {"perimeter": "Perimeter", "diagonals": "XY diagonals", "z_stroke": "Z stroke"},
                    "Moves in each pass",
                ).classes("w-full")
                self._choice(
                    PROFILE,
                    "z_stroke_at",
                    {"center": "Z stroke at the XY center", "start": "at the start corner"},
                    spread=True,
                )
                ui.label("Safety").classes("text-sm font-medium mt-2")
                self._switch(
                    PROFILE, "operator_confirm", "Checklist stop before the spindle starts"
                )
                self._switch(
                    PROFILE, "runtime_guards", "Check the control's soft limits match this machine"
                )
                self._switch(PROFILE, "final_rapid_pass", "Finish with one pass at rapid traverse")

    def _generate_card(self) -> None:
        with ui.card().classes(CARD_SIZE):
            with ui.row().classes("w-full items-center no-wrap"):
                ui.label("3").classes(
                    "rounded-full bg-primary text-white w-7 h-7 text-center leading-7 font-bold"
                )
                ui.label("Generate").classes("text-lg font-bold")
            with ui.row():
                for controller, name in CONTROLLER_NAMES.items():
                    with ui.element("div"):  # takes the hover for a tooltip when disabled
                        self.outputs[controller] = ui.checkbox(
                            name, on_change=lambda e, c=controller: self._on_output(c, e.value)
                        ).mark(f"output-{controller}")
                        if controller is Controller.FANUC:
                            self.fanuc_unavailable = ui.tooltip(FANUC_UNAVAILABLE).mark(
                                "fanuc-unavailable"
                            )
            self.generate_button = (
                ui.button("Generate files", icon="play_arrow", on_click=self._generate)
                .classes("w-full mt-auto")  # it and the command line sit at the card's bottom
                .tooltip(f"Writes to {self.settings.out_dir.as_posix()}/<controller>/")
                .mark("generate")
            )
            with (
                ui.expansion("Command-line equivalent", icon="terminal").classes("w-full"),
                ui.row().classes("w-full no-wrap items-center"),
            ):
                self.command = (
                    ui.label().classes("font-mono text-xs break-all grow").mark("cli-command")
                )
                self.copy_button = (
                    ui.button(icon="content_copy", on_click=self._copy_command)
                    .props("flat round")
                    .mark("copy-command")
                )

    def _output_panel(self) -> None:
        """The drawer's content: the summary on top, the generated programs below."""
        with ui.row().classes("w-full items-center no-wrap"):
            ui.label("Output").classes("text-lg font-bold grow")
            ui.button(icon="close", on_click=self.drawer.hide).props("flat round").tooltip(
                "Close"
            ).mark("output-close")
        self.status = ui.label().classes("text-lg font-medium").mark("status")
        self.issues = ui.column().classes("w-full gap-1")
        self.overview = ui.label().classes("whitespace-pre-line").mark("overview")
        self.stage_table = (
            ui.table(
                columns=[
                    {"name": key, "label": label, "field": key, "align": "right"}
                    for key, label in (
                        ("stage", "Stage"),
                        ("rpm", "RPM"),
                        ("feed", "Feed mm/min"),
                        ("passes", "Passes"),
                        ("pass_time", "Pass time"),
                        ("dwell", "Dwell"),
                        ("time", "Stage time"),
                    )
                ],
                rows=[],
                row_key="stage",
            )
            .props("dense flat bordered")
            .classes("w-full")
            .mark("stage-table")
        )
        with ui.tabs().classes("w-full") as tabs:
            program_tabs = {c: ui.tab(CONTROLLER_NAMES[c]) for c in Controller}
        with ui.tab_panels(tabs, value=program_tabs[Controller.HEIDENHAIN]).classes("w-full"):
            self.programs: dict[Controller, tuple[ui.label, ui.button, ui.code]] = {}
            for controller, tab in program_tabs.items():
                with ui.tab_panel(tab):
                    with ui.row().classes("items-center"):
                        name = ui.label().classes("font-mono").mark(f"filename-{controller}")
                        download = ui.button(
                            "Download",
                            icon="download",
                            on_click=lambda c=controller: self._download(c),
                        ).mark(f"download-{controller}")
                    code = ui.code(language="text").classes("w-full").mark(f"program-{controller}")
                    self.programs[controller] = (name, download, code)

    # --- Input helpers ---------------------------------------------------------------------

    def _register(self, form: str, name: str, element: ValueElement[Any]) -> None:
        element.mark(f"{form}-{name}")
        if isinstance(element, ValidationElement):
            # The core validator is the only source of errors (see _show_field_errors). NiceGUI's
            # own auto-validation would clear them again after every change.
            element.without_auto_validation()
        self.inputs[(form, name)] = element

    def _text(self, form: str, name: str, label: str) -> ui.input:
        element = ui.input(label, on_change=self._on_input)
        self._register(form, name, element)
        return element

    def _number(self, form: str, name: str, label: str, **options: Any) -> ui.number:
        element = ui.number(label, on_change=self._on_input, **options).classes("grow")
        self._register(form, name, element)
        return element

    def _choice(
        self, form: str, name: str, options: dict[str, str], *, spread: bool = False
    ) -> ui.toggle:
        """A toggle: compact, or with ``spread`` its buttons share the full width.

        Spread buttons grow in proportion to their labels (unlike Quasar's equal-width
        ``spread``), so a long label keeps to one line.
        """
        element = ui.toggle(options, on_change=self._on_input).props("no-caps dense")
        element.classes("w-full [&>*]:grow" if spread else "whitespace-nowrap")
        self._register(form, name, element)
        return element

    def _select(
        self, form: str, name: str, options: list[str] | dict[str, str], label: str
    ) -> ui.select:
        element = ui.select(options, label=label, multiple=True, on_change=self._on_input).props(
            "use-chips"
        )
        self._register(form, name, element)
        return element

    def _switch(self, form: str, name: str, label: str) -> ui.switch:
        element = ui.switch(label, on_change=self._on_input)
        self._register(form, name, element)
        return element

    def _form(self, key: str) -> MachineForm | ProfileForm:
        return self.machine if key == MACHINE else self.profile

    @contextmanager
    def _quiet(self) -> Iterator[None]:
        """While active, input events don't trigger a refresh: the code is filling the inputs."""
        previous, self._loading = self._loading, True
        try:
            yield
        finally:
            self._loading = previous

    def _read_inputs(self) -> None:
        for (form, name), element in self.inputs.items():
            setattr(self._form(form), name, element.value)

    def _write_inputs(self) -> None:
        with self._quiet():
            for (form, name), element in self.inputs.items():
                element.value = getattr(self._form(form), name)
            self.zero.value = self.machine.zero

    # --- Events ------------------------------------------------------------------------------

    def _on_input(self) -> None:
        if self._loading:
            return
        self._read_inputs()
        self._refresh()

    def _on_zero(self) -> None:
        """Where machine zero is: converts the travel fields to match, then shows them."""
        if self._loading:
            return
        self._read_inputs()  # keep any other edit made since the last refresh
        self.machine.set_zero(self.zero.value)
        self._write_inputs()
        self._refresh()

    def _on_output(self, controller: Controller, checked: bool) -> None:
        if self._loading:
            return
        if not checked and not any(box.value for box in self.outputs.values()):
            with self._quiet():
                self.outputs[controller].value = True
            ui.notify("At least one controller is needed.", type="warning")
            return
        self._refresh()

    def _on_select(self, kind: _Kind, entry_id: str | None) -> Coroutine[Any, Any, None] | None:
        """Load another saved entry: at once, or after confirming if there are unsaved changes.

        Only the confirmation needs to wait for the user, so only then is a coroutine
        returned, which NiceGUI awaits.
        """
        if self._loading or entry_id is None or entry_id == kind.loaded:
            return None
        if kind.dirty():
            return self._switch_after_confirming(kind, entry_id)
        self._load(kind, entry_id)
        self._refresh()
        return None

    async def _switch_after_confirming(self, kind: _Kind, entry_id: str) -> None:
        if await self._confirm(f"Discard the unsaved changes to {kind.noun} {kind.entry_id}?"):
            self._load(kind, entry_id)
            self._refresh()
        else:
            with self._quiet():
                self.bars[kind.key].select.value = kind.loaded

    async def _rename(self, kind: _Kind) -> None:
        """Give the entry a new name. Like any edit, it takes effect in the file on Save."""
        current = kind.entry_id
        name = await self._ask_name(
            kind, f"Rename {kind.noun} {current}", current, "Rename", allowed=kind.loaded
        )
        if name is not None and name != current:
            setattr(kind.form, kind.id_field, name)
            self._refresh()

    async def _duplicate(self, kind: _Kind) -> None:
        """Save a copy of the entry (with any unsaved edits) under a new name, and switch to it.

        The original keeps its saved settings.
        """
        if problems := kind.problems():  # a click that raced the menu item being disabled
            ui.notify(f"Fix the {kind.noun} first: {problems[0]}", type="negative")
            return
        base, number = kind.entry_id, 2
        while f"{base}_{number}" in kind.saved:
            number += 1
        name = await self._ask_name(
            kind,
            f"Duplicate {kind.noun} {base} as",
            f"{base}_{number}",
            "Duplicate",
            note=f"The copy is saved right away, with any unsaved changes. "
            f"{kind.loaded} keeps its saved settings.",
        )
        if name is None:
            return
        persist.save_entry(kind.path, kind.section, name, kind.form.to_table())
        kind.saved = persist.read_tables(kind.path, kind.section)
        setattr(kind.form, kind.id_field, name)
        kind.loaded = name
        self._update_select(kind)
        self._refresh()
        ui.notify(f"Saved {kind.noun} {name}, a copy of {base}.", type="positive")

    async def _ask_name(
        self,
        kind: _Kind,
        title: str,
        initial: str,
        action: str,
        *,
        allowed: str = "",
        note: str = "",
    ) -> str | None:
        """Ask for a name in a dialog, checked as you type. None if cancelled."""
        with ui.dialog() as dialog, ui.card().classes("min-w-[340px] max-w-[420px]"):
            ui.label(title).classes("text-lg font-bold")
            if note:
                ui.label(note).classes("text-sm").mark("name-note")
            name = (
                ui.input(f"{kind.noun.capitalize()} name", value=initial)
                .props("autofocus")
                .classes("w-full")
                .without_auto_validation()
                .mark("name-input")
            )
            with ui.row().classes("w-full justify-end"):
                ui.button("Cancel", on_click=lambda: dialog.submit(None)).props("flat").mark(
                    "name-cancel"
                )
                confirm = ui.button(action, on_click=lambda: dialog.submit(name.value)).mark(
                    "name-ok"
                )

        def check() -> None:
            name.error = kind.name_error(name.value or "", allowed=allowed)
            confirm.set_enabled(name.error is None)

        def submit_on_enter() -> None:
            if confirm.enabled:
                dialog.submit(name.value)

        name.on_value_change(check)
        name.on("keydown.enter", submit_on_enter)
        check()
        result = await dialog
        dialog.delete()
        return None if result is None else str(result)

    def _save_changes(self) -> None:
        """The footer's Save changes: the machine, the profile or both, whichever changed."""
        self._save((self.machines, self.profiles))

    def _save(self, kinds: tuple[_Kind, ...]) -> None:
        """Save those of ``kinds`` that changed, or nothing if any of them is invalid."""
        changed = [kind for kind in kinds if kind.dirty()]
        if not changed:
            return
        for kind in changed:
            if problems := kind.problems():
                ui.notify(f"Fix the {kind.noun} first: {problems[0]}", type="negative")
                return
            if kind.entry_id != kind.loaded and kind.entry_id in kind.saved:
                ui.notify(
                    f"Another saved {kind.noun} is already called {kind.entry_id}.",
                    type="negative",
                )
                return
        for kind in changed:
            persist.save_entry(
                kind.path,
                kind.section,
                kind.entry_id,
                kind.form.to_table(),
                renamed_from=kind.loaded,
            )
            kind.saved = persist.read_tables(kind.path, kind.section)
            kind.loaded = kind.entry_id
            self._update_select(kind)
        self._refresh()
        names = " and ".join(f"{kind.noun} {kind.entry_id}" for kind in changed)
        ui.notify(f"Saved {names}.", type="positive")

    async def _discard(self) -> None:
        if not await self._confirm("Discard all unsaved changes?"):
            return
        for kind in (self.machines, self.profiles):
            self._load(kind, kind.loaded)  # a no-op for an entry without changes
        self._refresh()
        ui.notify("Unsaved changes discarded.")

    async def _delete(self, kind: _Kind) -> None:
        if not await self._confirm(
            f"Delete {kind.noun} {kind.loaded} from {kind.path.as_posix()}? This cannot be undone."
        ):
            return
        persist.delete_entry(kind.path, kind.section, kind.loaded)
        deleted, kind.saved = kind.loaded, persist.read_tables(kind.path, kind.section)
        self._load(kind, next(iter(kind.saved)))
        self._refresh()
        ui.notify(f"Deleted {kind.noun} {deleted}.", type="positive")

    def _generate(self) -> None:
        if self.result is None or self.result.errors:
            return
        paths = write_programs(self.result, self.settings.out_dir)
        ui.notify(f"Wrote {', '.join(path.as_posix() for path in paths)}", type="positive")
        self.drawer.show()  # the programs just written, with their summary

    def _download(self, controller: Controller) -> None:
        program = self._program(controller)
        if program is not None:
            ui.download.content(program.data, program.filename)

    def _copy_command(self) -> None:
        if command := self._cli_command():
            ui.clipboard.write(command)
            ui.notify("Command copied.")

    async def _confirm(self, message: str) -> bool:
        with ui.dialog() as dialog, ui.card():
            ui.label(message)
            with ui.row().classes("w-full justify-end"):
                ui.button("Cancel", on_click=lambda: dialog.submit(False)).props("flat").mark(
                    "confirm-cancel"
                )
                ui.button("Continue", on_click=lambda: dialog.submit(True)).mark("confirm-ok")
        answer = await dialog
        dialog.delete()
        return bool(answer)

    # --- State --------------------------------------------------------------------------------

    def _load(self, kind: _Kind, entry_id: str) -> None:
        loaded = type(kind.form).from_table(entry_id, kind.saved[entry_id])
        for item in fields(loaded):
            setattr(kind.form, item.name, getattr(loaded, item.name))
        kind.loaded = entry_id
        self._write_inputs()
        self._update_select(kind)

    def _update_select(self, kind: _Kind) -> None:
        with self._quiet():
            self.bars[kind.key].select.set_options(list(kind.saved), value=kind.loaded)

    def _program(self, controller: Controller) -> Program | None:
        if self.result is None or self.result.errors:
            return None
        return next((p for p in self.result.programs if p.controller is controller), None)

    def _cli_command(self) -> str | None:
        """The CLI command reproducing the preview: the saved machine, profile edits as --set.

        Only called for an error-free preview, so its plan holds the parsed profile.
        """
        machine, profile = self.machines, self.profiles
        if machine.dirty() or profile.entry_id not in profile.saved or self.result is None:
            return None
        assert self.result.plan is not None
        name = profile.entry_id
        saved = parse_profiles({"profiles": {name: profile.saved[name]}}, source=None)[name]
        before, after = profile_to_raw(saved), profile_to_raw(self.result.plan.profile)
        overrides = {key: value for key, value in after.items() if before[key] != value}
        controllers = tuple(c for c, box in self.outputs.items() if box.value)
        return cli_command(
            GenerationRequest(machine.entry_id, profile.entry_id, overrides, controllers)
        )

    # --- Preview ------------------------------------------------------------------------------

    def _refresh(self) -> None:
        machine = self.machine
        self.travel_box.set_visibility(machine.coordinates == TRAVEL)
        self.limits_box.set_visibility(machine.coordinates == LIMITS)
        self.fanuc_box.set_visibility(machine.fanuc)
        self._offer_fanuc(machine.fanuc)

        controllers = tuple(c for c, box in self.outputs.items() if box.value)
        self.result = preview_config(
            self.machines.entry_id,
            machine.to_table(),
            self.profiles.entry_id,
            self.profiles.form.to_table(),
            controllers,
        )
        self._show_field_errors(self.result.issues)
        self._show_summary(self.result)
        self._show_programs()
        self._show_actions()

    def _offer_fanuc(self, available: bool) -> None:
        """Fanuc output needs the machine's Fanuc settings; without them it can't be picked."""
        fanuc = self.outputs[Controller.FANUC]
        if not available and fanuc.value:
            with self._quiet():
                fanuc.value = False
                self.outputs[Controller.HEIDENHAIN].value = True  # one output stays selected
        fanuc.set_enabled(available)
        if available:
            self.fanuc_unavailable.props("disable")
        else:
            self.fanuc_unavailable.props(remove="disable")

    def _show_field_errors(self, issues: tuple[Issue, ...]) -> None:
        messages: dict[tuple[str, str], list[str]] = {}
        for issue in issues:
            if issue.severity is Severity.ERROR and (target := field_for(issue)) is not None:
                messages.setdefault(target, []).append(issue.message)
        for key, element in self.inputs.items():
            if isinstance(element, ValidationElement):
                element.error = "; ".join(messages[key]) if key in messages else None

    def _show_summary(self, result: Preview) -> None:
        errors, warnings = result.errors, result.warnings
        self.problem_count.text = str(len(errors))  # on the header's output icon
        self.problem_count.set_visibility(bool(errors))
        if errors:
            self.status.text = f"{len(errors)} problem(s) to fix before generating"
            self.status.classes(replace="text-lg font-medium text-negative")
        else:
            count = len(result.programs)
            self.status.text = f"{count} program(s) generated and verified against the plan"
            self.status.classes(replace="text-lg font-medium text-positive")

        self.issues.clear()
        with self.issues:
            for issue in (*errors, *warnings):
                color = "text-negative" if issue.severity is Severity.ERROR else "text-warning"
                ui.label(f"{issue.severity.value}: {issue.path}: {issue.message}").classes(
                    color
                ).mark("issue")

        plan = result.plan
        if plan is None:
            self.overview.text = ""
            self.stage_table.rows = []
        else:
            envelope, n = plan.envelope, format_number
            self.overview.text = (
                f"Sweep envelope X {n(envelope.x.min)}..{n(envelope.x.max)}   "
                f"Y {n(envelope.y.min)}..{n(envelope.y.max)}   "
                f"Z {n(envelope.z.min)}..{n(envelope.z.max)}\n"
                f"One sweep pass: {n(round(plan.sweep_length_mm))} mm\n"
                f"Estimated run time {format_duration(plan.estimated_seconds)}, "
                "plus rapid positioning"
            )
            self.stage_table.rows = [
                {
                    "stage": stage.number,
                    "rpm": n(stage.rpm),
                    "feed": n(stage.feed),
                    "passes": stage.passes,
                    "pass_time": format_duration(stage.pass_seconds),
                    "dwell": f"{n(stage.dwell_seconds)} s",
                    "time": format_duration(stage.seconds),
                }
                for stage in plan.stages
            ]
        self.stage_table.update()

    def _show_programs(self) -> None:
        for controller, (name, download, code) in self.programs.items():
            program = self._program(controller)
            if program is None:
                if controller is Controller.FANUC and not self.machine.fanuc:
                    name.text = FANUC_UNAVAILABLE
                elif self.outputs[controller].value:
                    name.text = "Fix the problems to see this program."
                else:
                    name.text = "Not selected under Generate."
                code.set_content("")
            else:
                name.text = program.filename
                code.set_content(program.text.replace("\r\n", "\n"))
            download.set_enabled(program is not None)

    def _show_actions(self) -> None:
        errors = bool(self.result is None or self.result.errors)
        self.generate_button.set_enabled(not errors)

        changed, all_savable = [], True
        for kind in (self.machines, self.profiles):
            bar = self.bars[kind.key]
            bar.title.text = f"{kind.title} {kind.entry_id}"
            if kind.entry_id != kind.loaded:
                bar.badge.text = f"renamed from {kind.loaded}, not saved yet"
            else:
                bar.badge.text = "unsaved changes"
            dirty, valid = kind.dirty(), not kind.problems()
            if dirty:
                changed.append(kind)
                all_savable &= valid
            bar.badge.set_visibility(dirty)
            bar.save.set_enabled(dirty and valid)
            bar.duplicate.set_enabled(valid)
            bar.delete.set_enabled(len(kind.saved) > 1)

        self.footer.set_visibility(bool(changed))
        self.unsaved.text = "Unsaved changes to " + " and ".join(
            f"{kind.noun} {kind.entry_id}" for kind in changed
        )
        self.save_button.set_enabled(all_savable)

        command = None if errors else self._cli_command()
        self.command.text = command or "Save your changes to generate the same from the CLI."
        self.copy_button.set_enabled(command is not None)
