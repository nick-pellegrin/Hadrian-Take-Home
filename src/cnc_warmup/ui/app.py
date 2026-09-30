"""NiceGUI configurator: edit machines and warm-up profiles, preview, and generate.

Everything the page shows comes from `service.preview_config`: the same
validation, planning, rendering and round-trip verification the CLI uses. The UI
holds no generation logic of its own. Edits stay in the page until saved, and
saving writes machines.toml / profiles.toml with their comments intact.
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
    parse_machines,
    parse_profiles,
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
CONTROLLER_NAMES = {Controller.HEIDENHAIN: "Heidenhain TNC 640", Controller.FANUC: "Fanuc 31i"}


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
    noun: str
    section: str  # the TOML table the entries live under
    path: Path
    form: MachineForm | ProfileForm
    id_field: str  # the form field holding the entry's ID
    parse: Callable[..., Mapping[str, object]]
    saved: dict[str, dict[str, Any]] = field(default_factory=dict)
    loaded: str = ""  # the saved entry the form was loaded from

    @property
    def entry_id(self) -> str:
        return str(getattr(self.form, self.id_field))

    def saved_form(self) -> MachineForm | ProfileForm | None:
        table = self.saved.get(self.loaded)
        return None if table is None else type(self.form).from_table(self.loaded, table)

    def dirty(self) -> bool:
        saved = self.saved_form()
        return (
            saved is None
            or self.entry_id != self.loaded
            or self.form.to_table() != saved.to_table()
        )

    def problems(self) -> list[Issue]:
        """Issues in this entry on its own (a machine or profile can be saved without the other)."""
        try:
            self.parse({self.section: {self.entry_id: self.form.to_table()}}, source=None)
        except ConfigError as error:
            return list(error.issues)
        return []


@dataclass(frozen=True)
class _EntryBar:
    """The select / save / delete controls above a machine or profile form."""

    select: ui.select
    badge: ui.badge
    save: ui.button
    delete: ui.button


class Editor:
    """One browser tab: the machine and profile forms, the live preview, and the actions."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.machine, self.profile = MachineForm(), ProfileForm()
        self.machines = _Kind(
            MACHINE,
            "machine",
            "machines",
            settings.config_dir / MACHINES_FILE,
            self.machine,
            "machine_id",
            parse_machines,
        )
        self.profiles = _Kind(
            PROFILE,
            "profile",
            "profiles",
            settings.config_dir / PROFILES_FILE,
            self.profile,
            "name",
            parse_profiles,
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
            self.outputs[Controller(self.machine.controller)].value = True
        self._refresh()

    # --- Layout ------------------------------------------------------------------------------

    def _build(self) -> None:
        with ui.header().classes("items-center justify-between"):
            ui.label(TITLE).classes("text-xl font-bold")
            ui.label(
                f"config: {self.settings.config_dir.as_posix()}  |  "
                f"output: {self.settings.out_dir.as_posix()}"
            ).classes("text-sm")
        with ui.row().classes("w-full no-wrap items-start gap-4"):
            with ui.column().classes("w-[460px] shrink-0 gap-4"):
                self._machine_card()
                self._profile_card()
                self._output_card()
            with ui.column().classes("grow min-w-0"):
                self._preview_panel()

    def _entry_bar(self, kind: _Kind) -> None:
        with ui.row().classes("w-full items-center"):
            ui.label(kind.noun.capitalize()).classes("text-lg font-bold")
            badge = ui.badge("unsaved changes", color="orange").mark(f"{kind.key}-unsaved")
        with ui.row().classes("w-full items-center no-wrap"):
            select = (
                ui.select(
                    [],
                    label=f"Saved {kind.noun}",
                    on_change=lambda e: self._on_select(kind, e.value),
                )
                .classes("grow")
                .mark(f"{kind.key}-select")
            )
            ui.button(icon="content_copy", on_click=lambda: self._duplicate(kind)).props(
                "flat round"
            ).tooltip(f"Duplicate as a new {kind.noun}").mark(f"{kind.key}-duplicate")
            save = ui.button("Save", icon="save", on_click=lambda: self._save(kind)).mark(
                f"{kind.key}-save"
            )
            delete = (
                ui.button(icon="delete", color="negative", on_click=lambda: self._delete(kind))
                .props("flat round")
                .tooltip(f"Delete this {kind.noun} from the file")
                .mark(f"{kind.key}-delete")
            )
        self.bars[kind.key] = _EntryBar(select, badge, save, delete)

    def _machine_card(self) -> None:
        with ui.card().classes("w-full"):
            self._entry_bar(self.machines)
            with ui.row().classes("w-full no-wrap"):
                self._text(MACHINE, "machine_id", "Machine ID").classes("w-32").tooltip(
                    "Letters, digits, _ or -. Becomes part of program and file names."
                )
                self._text(MACHINE, "description", "Description").classes("grow")
            self._choice(MACHINE, "controller", {c.value: n for c, n in CONTROLLER_NAMES.items()})

            ui.label("Travel, in machine coordinates (Heidenhain M91 / Fanuc G53)").classes(
                "text-sm font-medium mt-2"
            )
            self._choice(
                MACHINE, "coordinates", {TRAVEL: "Travel + home end", LIMITS: "Explicit limits"}
            )
            with ui.column().classes("w-full") as self.travel_box:
                self._choice(
                    MACHINE,
                    "home",
                    {"max": "Home at + end: -travel..0", "min": "Home at - end: 0..travel"},
                )
                with ui.row().classes("w-full no-wrap"):
                    for axis in Axis:
                        self._number(
                            MACHINE, f"travel_{axis}", f"{axis.upper()} travel", suffix="mm"
                        )
            with ui.column().classes("w-full") as self.limits_box:
                for axis in Axis:
                    with ui.row().classes("w-full no-wrap"):
                        self._number(MACHINE, f"{axis}_min", f"{axis.upper()} min", suffix="mm")
                        self._number(MACHINE, f"{axis}_max", f"{axis.upper()} max", suffix="mm")

            with ui.row().classes("w-full no-wrap"):
                self._number(MACHINE, "spindle_max_rpm", "Spindle max", suffix="rpm").tooltip(
                    "Caps the profile's finish RPM. Not written to programs."
                )
                self._number(MACHINE, "max_feed", "Max feed", suffix="mm/min").tooltip(
                    "Caps the profile's finish feed. Not written to programs."
                )
            self._switch(MACHINE, "fanuc", "Fanuc 31i settings")
            with ui.row().classes("w-full no-wrap") as self.fanuc_box:
                self._number(MACHINE, "program_number", "Program number", prefix="O", precision=0)
                self._select(
                    MACHINE,
                    "cancel_codes",
                    [code.value for code in FanucCancelCode],
                    "Cancel codes",
                ).classes("grow").tooltip(
                    "Only the codes this control has options for: others raise alarms."
                )

    def _profile_card(self) -> None:
        with ui.card().classes("w-full"):
            self._entry_bar(self.profiles)
            with ui.row().classes("w-full no-wrap"):
                self._text(PROFILE, "name", "Profile name").classes("w-32")
                self._text(PROFILE, "description", "Description").classes("grow")
            with ui.row().classes("w-full no-wrap"):
                self._number(PROFILE, "feed_start", "Start feed", suffix="mm/min")
                self._number(PROFILE, "feed_end", "Finish feed", suffix="mm/min")
            with ui.row().classes("w-full no-wrap"):
                self._number(PROFILE, "rpm_start", "Start spindle", suffix="rpm").tooltip(
                    "Never start a cold spindle at high speed."
                )
                self._number(PROFILE, "rpm_end", "Finish spindle", suffix="rpm")
            with ui.row().classes("w-full no-wrap"):
                self._number(PROFILE, "duration_min", "Duration", suffix="min").tooltip(
                    "Each stage holds its speed for at least duration / stages."
                )
                self._number(PROFILE, "stages", "Stages", precision=0)
                self._number(PROFILE, "edge_margin_mm", "Edge margin", suffix="mm").tooltip(
                    "Stops this far inside every limit, so servo overshoot can't trip an "
                    "overtravel alarm."
                )
            self._choice(PROFILE, "ramp", {"linear": "Linear ramp", "geometric": "Geometric ramp"})
            self._select(
                PROFILE,
                "pattern",
                {"perimeter": "Perimeter", "diagonals": "XY diagonals", "z_stroke": "Z stroke"},
                "Sweep pattern",
            ).classes("w-full")
            self._choice(
                PROFILE,
                "z_stroke_at",
                {"center": "Z stroke at XY center", "start": "at start corner"},
            )
            self._choice(PROFILE, "coolant", {"off": "Coolant off", "flood": "Flood coolant"})
            self._switch(
                PROFILE, "operator_confirm", "Operator checklist stop before the spindle starts"
            )
            self._switch(
                PROFILE, "runtime_guards", "Travel check against the control's soft limits"
            )
            self._switch(PROFILE, "final_rapid_pass", "Finish with a sweep at rapid traverse")

    def _output_card(self) -> None:
        with ui.card().classes("w-full"):
            ui.label("Output").classes("text-lg font-bold")
            with ui.row():
                for controller, name in CONTROLLER_NAMES.items():
                    self.outputs[controller] = ui.checkbox(
                        name, on_change=lambda e, c=controller: self._on_output(c, e.value)
                    ).mark(f"output-{controller}")
            with ui.row().classes("items-center"):
                self.generate_button = ui.button(
                    "Generate files", icon="play_arrow", on_click=self._generate
                ).mark("generate")
                ui.label(f"to {self.settings.out_dir.as_posix()}/<controller>/").classes("text-sm")
            ui.label("Same result from the command line:").classes("text-sm mt-2")
            with ui.row().classes("w-full no-wrap items-center"):
                self.command = (
                    ui.label().classes("font-mono text-xs break-all grow").mark("cli-command")
                )
                self.copy_button = (
                    ui.button(icon="content_copy", on_click=self._copy_command)
                    .props("flat round")
                    .mark("copy-command")
                )

    def _preview_panel(self) -> None:
        self.status = ui.label().classes("text-lg font-medium").mark("status")
        with ui.tabs().classes("w-full") as tabs:
            summary_tab = ui.tab("Summary")
            program_tabs = {c: ui.tab(CONTROLLER_NAMES[c]) for c in Controller}
        with ui.tab_panels(tabs, value=summary_tab).classes("w-full"):
            with ui.tab_panel(summary_tab):
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
                    .classes("w-full")
                    .mark("stage-table")
                )
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

    def _choice(self, form: str, name: str, options: dict[str, str]) -> ui.toggle:
        element = ui.toggle(options, on_change=self._on_input).props("no-caps dense")
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

    # --- Events ------------------------------------------------------------------------------

    def _on_input(self) -> None:
        if self._loading:
            return
        machine = self.machine
        before = machine.coordinates
        self._read_inputs()
        if machine.coordinates != before:  # convert between travel + home and explicit limits
            wanted, machine.coordinates = machine.coordinates, before
            if wanted == LIMITS:
                machine.use_limits()
            else:
                machine.use_travel()
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

    def _duplicate(self, kind: _Kind) -> None:
        base, number = kind.entry_id or kind.noun, 2
        while f"{base}_{number}" in kind.saved:
            number += 1
        setattr(kind.form, kind.id_field, f"{base}_{number}")
        self._write_inputs()
        self._refresh()

    async def _save(self, kind: _Kind) -> None:
        if problems := kind.problems():
            ui.notify(f"Fix the {kind.noun} first: {problems[0]}", type="negative")
            return
        entry_id = kind.entry_id
        replaces = entry_id != kind.loaded and entry_id in kind.saved
        if replaces and not await self._confirm(f"Replace the saved {kind.noun} {entry_id}?"):
            return
        persist.save_entry(kind.path, kind.section, entry_id, kind.form.to_table())
        kind.saved = persist.read_tables(kind.path, kind.section)
        kind.loaded = entry_id
        self._update_select(kind)
        self._refresh()
        ui.notify(f"Saved {kind.noun} {entry_id} to {kind.path.as_posix()}", type="positive")

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
                selected = self.outputs[controller].value
                name.text = (
                    "Fix the problems to see this program."
                    if selected
                    else ("Not selected under Output.")
                )
                code.set_content("")
            else:
                name.text = program.filename
                code.set_content(program.text.replace("\r\n", "\n"))
            download.set_enabled(program is not None)

    def _show_actions(self) -> None:
        errors = bool(self.result is None or self.result.errors)
        self.generate_button.set_enabled(not errors)
        for kind in (self.machines, self.profiles):
            bar = self.bars[kind.key]
            bar.badge.set_visibility(kind.dirty())
            bar.save.set_enabled(not kind.problems())
            bar.delete.set_enabled(kind.loaded in kind.saved and len(kind.saved) > 1)
        command = None if errors else self._cli_command()
        self.command.text = command or "Save the machine and profile to generate from the CLI."
        self.copy_button.set_enabled(command is not None)
