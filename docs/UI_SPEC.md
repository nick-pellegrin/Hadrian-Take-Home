# UI specification: warm-up configurator

**Status:** implemented in `src/cnc_warmup/ui/`. Launch it with `uv run cnc-warmup ui`.
The build differs from the first draft in three ways:
- **Machines are fully editable** (moved up from §11). You can edit travel or explicit
  limits, spindle and feed caps, and the Fanuc program number and cancel codes. You can
  also duplicate, rename, save and delete machines, not just profiles.
- **The §12 questions are decided:** NiceGUI; part of the submission; Save writes into
  `machines.toml` / `profiles.toml`, keeping their comments.
- **The layout was simplified** after a first build showed every setting at once. §4,
  §5 and §7 describe the page as built.

§13 records how the implementation meets the acceptance criteria.

A small local web UI for setting a warm-up program's parameters, checking the result as
you go, and writing the programs. It sits on top of the same core as the `cnc-warmup` CLI.

---

## 1. Purpose and users

**Users:**
- **Main user:** a controls or manufacturing engineer preparing a warm-up for a machine,
  or tuning a profile for part of the fleet.
- **Secondary user:** a reviewer or lead checking a program before it goes to the control.

This is **not** an operator HMI. It never talks to a machine.

**Goals:**
1. Change any warm-up parameter and see the effect **immediately**: validation messages,
   the stage table, estimated runtime, block count, and the generated program text.
2. Write the programs for one or both controllers to disk, or download them.
3. Save the settings back as a named profile in `config/profiles.toml`, so the CLI can
   reproduce the output exactly.

**Non-goals for v1:**
- ~~Editing machine definitions~~ (built after all; see the status note).
- Sending programs to a control (DNC, LSV2, FOCAS).
- Authentication, multiple users, or network hosting by default.
- Mobile layout.
- Translation, and inch units (programs are metric: `MM` / `G21`).

## 2. Principles

| Principle | Consequence |
|---|---|
| **Thin adapter.** The UI holds no generation logic | The UI calls the same application service as the CLI (§6). All validation, planning and rendering stay in the core |
| **TOML is the source of truth** | Anything the UI generates can be reproduced from the CLI. The UI shows the equivalent CLI command, and the program header records the settings |
| **Safety stays visible** | Errors block generation. Warnings are shown prominently. The UI never removes the safety comments, guards or checklist from the program |
| **Local and offline** | Binds to `127.0.0.1` by default. No CDN (NiceGUI serves its own assets). No telemetry |
| **Core stays dependency-free** | UI packages are an optional extra. A test enforces that the core never imports them |

## 3. Technology

**Recommendation: [NiceGUI](https://nicegui.io) 3.x, as an optional `ui` extra.**

```bash
uv run --extra ui cnc-warmup ui                 # opens http://127.0.0.1:8080
uv run --extra ui cnc-warmup ui --port 9000 --no-browser
```

Why NiceGUI:
- It is Python-only and event-driven. Unlike Streamlit, it doesn't re-run the whole
  script on every interaction, which suits a form with live preview.
- It has the building blocks we need: number inputs with validation, toggles, tabs,
  tables, and a code viewer (`ui.codemirror`, read-only).
- It has `ui.echart` for a ramp chart and `ui.scene` (three.js) for a 3D toolpath
  preview, both for v2.
- Its pytest plugin provides a `user` fixture that simulates the browser in Python,
  so UI tests run at unit-test speed with no Selenium.
- It needs Python ≥3.10, which fits the project floor of 3.11. The dev interpreter is
  3.13. Python 3.14 support is unconfirmed, which is why `.python-version` pins 3.13.

**Alternatives considered:**

| Option | Why not chosen |
|---|---|
| Streamlit | Re-runs the script on every interaction, which is awkward for a stateful form. Heavy dependencies (pandas, pyarrow). Reads as a data-science demo |
| Textual (terminal UI) | Fits CLI tooling well, but code preview and charts are weaker, and it is less familiar to reviewers |
| Tkinter | No dependencies, but dated look, harder to test, and no charts without matplotlib |
| FastAPI + HTML/htmx | Cleanest API separation, but roughly twice the code (templates, JS) for the same result. Worth revisiting if the UI ever becomes a factory service |

## 4. Layout

The three steps sit side by side as cards of equal height, centered on the page both
ways. The output is in a drawer on the right.

- **Common settings first.** Each card shows only what most warm-ups change. Everything
  else sits in a collapsed **Advanced** section, one click away, at the bottom of the
  card, so the machine and profile cards' Advanced sections line up. The Generate card
  keeps **Generate files** and **Command-line equivalent** at its bottom the same way.
- **Wide controls where there's room.** **Generate files** spans its card. In the
  profile's Advanced section, the ramp and Z-stroke toggles span the card too; their
  buttons grow in proportion to their labels, so a long label stays on one line.
- **The output is one click away.** The **Output** button (icon and label) at the right
  of the header opens an 860 px drawer. The summary is on top (status, errors and
  warnings, sweep envelope, run time, stage table) and the generated programs are below,
  one tab per controller.
  - While the drawer is closed, a red count on the button shows how many problems block
    generation. Each problem is also shown under its field.
  - The drawer pushes the cards aside rather than covering them, so the preview stays
    live while you edit. The cards shrink from 400 px to 320 px wide to keep all three
    beside the open drawer on a 1920 px screen. On a narrower window they wrap.
- **Unsaved work is hard to miss.** A footer with **Save changes** and **Discard** appears
  only while something is unsaved. It covers both cards, and each card's title shows an
  orange badge when that card has unsaved changes.
- **Rarely used actions are tucked away.** Renaming is the ✏ button next to the
  saved-entry select. The ⋮ ("more") menu beside it has **Save** (just this card),
  **Duplicate…** and **Delete…**.
- **Hadrian's navy (`#002548`)** is the primary colour: header, footer, step numbers,
  buttons and selected toggles.

The page, with the drawer closed:

```
┌──────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ CNC Warm-Up Generator                                                                    [Output](1) │
├──────────────────────────────────────────────────────────────────────────────────────────────────────┤
│  ┌ (1) Machine M1 ──────────────┐ ┌ (2) Warm-up profile daily ───┐ ┌ (3) Generate ────────────────┐  │
│  │ [M1        v] [edit] [more]  │ │ [daily     v] [edit] [more]  │ │ [x] Heidenhain TNC 640       │  │
│  │ Description [3-axis VMC…]    │ │ Description [Daily warm-up…] │ │ [ ] Fanuc 31i                │  │
│  │ X [762]  Y [508]  Z [500] mm │ │ Feed [2500] → [12000] mm/min │ │                              │  │
│  │ Spindle max [12000] rpm      │ │ Spindle [1000] → [10000] rpm │ │                              │  │
│  │ Max feed [20000] mm/min      │ │ Duration [20] min            │ │                              │  │
│  │                              │ │ [Coolant off|Flood coolant]  │ │ [>     Generate files      ] │  │
│  │ > Advanced: machine zero,    │ │ > Advanced: stages, margin,  │ │ > Command-line equivalent    │  │
│  │   Fanuc settings             │ │   ramp, moves, Z stroke,     │ │                              │  │
│  │                              │ │   safety                     │ │                              │  │
│  └──────────────────────────────┘ └──────────────────────────────┘ └──────────────────────────────┘  │
├──────────────────────────────────────────────────────────────────────────────────────────────────────┤
│                                           Unsaved changes to machine M1   [Discard]   [Save changes] │
│                                                              (only shown while something is unsaved) │
└──────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

The drawer, opened from the header icon:

```
┌ Output ───────────────────────────── [x] ┐
│ 1 program(s) generated and verified      │
│ Errors and warnings, if any              │
│                                          │
│ Sweep envelope X -761..-1 Y … Z …        │
│ One sweep pass: 6267 mm                  │
│ Estimated run time 19:57                 │
│                                          │
│ Stage  RPM   Feed  Passes  Dwell  Time   │
│   1   1000   2500     1    89 s   3:59   │
│   …                                      │
│                                          │
│ [Heidenhain TNC 640] [Fanuc 31i]         │
│ WARMUP_M1_DAILY.H   [Download]           │
│ 0 BEGIN PGM WARMUP_M1_DAILY MM           │
│ 1 ; WARM-UP FOR MACHINE M1 - …           │
│ …                                        │
└──────────────────────────────────────────┘
```

## 5. Fields

Every limit that depends on the machine is enforced by the **core** validator, which
reports each problem under the field it belongs to. "› Advanced" fields are in the
card's collapsed Advanced section.

**Step 1, Machine** (`machines.toml`):

| Where | Label | Config key | Control | Unit | Validation (core) |
|---|---|---|---|---|---|
| Machine | Saved machines | machine ID | select, ✏ rename | — | 1-16 letters, digits, `_` or `-`, starting with a letter or digit; unique |
| Machine | Description | `description` | text | — | optional |
| Machine | X / Y / Z travel | `travel` | numbers | mm | > 0, keeping at least 1 mm inside the edge margins |
| Machine | Spindle max | `spindle_max_rpm` | number | rpm | > 0 |
| Machine | Max feed | `max_feed` | number | mm/min | > 0 |
| › Advanced | Where is machine zero? | `home`, or `limits` | radio: **+ end** of each axis (most VMCs), **− end**, or **somewhere else** (enter each axis's min and max) | mm | each axis has min < max |
| › Advanced | This machine can run Fanuc 31i programs | `[fanuc]` table | switch | — | needed for Fanuc output |
| › Advanced | Program number, Cancel codes | `fanuc.program_number`, `fanuc.cancel_codes` | number (`O` prefix), chips | — | 1-8999 (O9000 and up are the builder's macros); only codes the control has options for |

**Step 2, Warm-up profile** (`profiles.toml`):

| Where | Label | Config key | Control | Unit | Validation (core) | Tooltip |
|---|---|---|---|---|---|---|
| Profile | Saved profiles | profile name | select, ✏ rename | — | 1-32 lowercase letters, digits or `_`, starting with a letter; unique | — |
| Profile | Description | `description` | text | — | optional | — |
| Profile | Feed at start / finish | `feed_start`, `feed_end` | numbers | mm/min | `0 < start ≤ finish ≤ machine.max_feed` | — |
| Profile | Spindle at start / finish | `rpm_start`, `rpm_end` | numbers | rpm | `0 < start ≤ finish ≤ machine.spindle_max_rpm` | "Never start a cold spindle at high speed." |
| Profile | Duration | `duration_min` | number | min | > 0, at most 240 | — |
| Profile | Coolant off / Flood coolant | `coolant` | toggle | — | `off` \| `flood` | — |
| › Advanced | Stages | `stages` | number | — | 2-20 | "Each holds its speed for duration / stages." |
| › Advanced | Edge margin | `edge_margin_mm` | number | mm | 0 to 25 mm, keeping at least 1 mm of travel | "Stops this far inside every limit, so servo overshoot can't trip an overtravel alarm." |
| › Advanced | How speeds step up: Equal steps / Smaller steps at low speed | `ramp` | toggle | — | `linear` \| `geometric` | — |
| › Advanced | Moves in each pass | `pattern` | chips | — | together they must reach **all six** axis extremes | — |
| › Advanced | Z stroke at the XY center / at the start corner | `z_stroke_at` | toggle | — | `center` \| `start` | — |
| › Advanced | Checklist stop before the spindle starts | `operator_confirm` | switch | — | — | — |
| › Advanced | Check the control's soft limits match this machine | `runtime_guards` | switch | — | — | — |
| › Advanced | Finish with one pass at rapid traverse | `final_rapid_pass` | switch | — | — | — |

A machine's `controller` key (the CLI's default output) has no field. The UI always
starts with Heidenhain checked under Generate, and saving keeps whatever the file says.

**Step 3, Generate:** a checkbox per controller (Heidenhain checked at start, at least
one always checked), a full-width **Generate files** button (its tooltip names the
output folder), and a collapsed **Command-line equivalent** with a copy button.

Fanuc 31i can only be picked for a machine that can run Fanuc programs. While the
machine's "This machine can run Fanuc 31i programs" switch is off, the Fanuc checkbox
is unchecked and disabled. Hovering it, or opening the drawer's Fanuc tab, says where to
turn it on. Turning the switch back on makes Fanuc available again without picking it.

## 6. Architecture

```
            ┌───────────────────┐        ┌──────────────────────────┐
  browser ◄─┤ cnc_warmup.ui     │        │ cnc_warmup (core, no deps)│
            │  app.py   (page)  │ calls  │  service.py  ◄── cli.py   │
            │  state.py (form)  ├───────►│  config / plan / posts    │
            │  persist.py(TOML) │        │  verify                   │
            └───────────────────┘        └──────────────────────────┘
               nicegui, tomlkit             stdlib only
```

**The application service** (`cnc_warmup.service`) already exists, and the CLI uses it:

```python
@dataclass(frozen=True)
class GenerationRequest:
    machine_id: str
    profile_name: str
    overrides: Mapping[str, object] = field(default_factory=dict)  # {"feed_end": 12000}
    controllers: tuple[Controller, ...] = ()  # empty: the machine's controller


@dataclass(frozen=True)
class Preview:
    request: GenerationRequest
    plan: WarmupPlan | None  # None when the request itself is invalid
    programs: tuple[Program, ...]  # rendered and verified by round-trip
    issues: tuple[Issue, ...]  # .errors / .warnings


def preview(catalog: Catalog, request: GenerationRequest) -> Preview: ...


def write_programs(result: Preview, out_dir: Path) -> list[Path]: ...


def cli_command(request: GenerationRequest) -> str: ...
```

`Issue` (`cnc_warmup.issues`) has `path`, `message`, `severity` and `source`.

- Each `Issue.path` maps to a form field, so the message appears **under that field**.
  Profile paths look like `profiles.daily.feed_end`, so the field is the last segment.
  Issues with no matching field (plan warnings, verification failures) appear in the
  Summary tab.
- `persist.py` saves profiles with `tomlkit`, which keeps the comments and layout in
  `profiles.toml`. This lives in the UI extra because the CLI never writes config.
- **State:** one `FormState` dataclass per browser tab (NiceGUI `@ui.page`), converted
  to and from `GenerationRequest.overrides`. It holds no other application state.

## 7. Behavior

1. **Start-up:**
   - `cnc-warmup ui [--config DIR] [--out DIR] [--host 127.0.0.1] [--port 8080] [--no-browser]`.
   - If the config has load errors, show an error page listing each issue by path.
     Do not open a half-working form.
2. **Live preview:**
   - Any change triggers `service.preview()` after a **300 ms debounce**. Generation
     takes milliseconds, so no background job is needed.
   - Summary and code tabs update in place. The scroll position of the code view is
     preserved.
3. **Validation:**
   - Errors mark their field red with the message, and disable **Generate**, **Download**
     and **Save**.
   - Warnings (e.g. "stage 1 over budget", "Heidenhain output is 104 blocks, over the
     100-block demo limit") are shown but do not block anything.
4. **Unsaved changes:**
   - A card's title gets an orange badge ("unsaved changes", "renamed from M1, not saved
     yet" or "new, not saved yet") when it differs from the file.
   - The footer appears, naming what's unsaved. **Discard** (after a confirmation)
     reloads both cards from the files.
   - Switching to another saved entry while there are unsaved changes asks first.
5. **Generate files:**
   - Writes the selected controllers' programs to `<output directory>/<controller>/`
     (default `out/`), replacing earlier files of the same name, and lists the written
     paths.
   - Uses the same file names as the CLI (`WARMUP_M1_DAILY.H`, `O8001_M1_DAILY.nc`).
   - Opens the Output drawer, showing the programs just written and their summary.
6. **Download:** a browser download of one program. It contains the same bytes as
   Generate, including CRLF for `.H` files.
7. **Save changes:**
   - Saves whatever changed: the machine, the profile, or both, each into its own file
     with its comments kept.
   - Disabled while a changed entry has errors, and saves nothing if either is invalid.
     A machine or profile is checked on its own, so a valid machine can be saved even
     when the profile doesn't fit it yet.
   - Each card's ⋮ menu has **Save**, which saves just that card. It is enabled only
     while the card has valid unsaved changes.
   - Names come from the rename and duplicate dialogs, which reject names already in
     use, so a save never overwrites another entry.
8. **CLI command:** copies the equivalent command to the clipboard, e.g.
   `uv run cnc-warmup generate --machine M1 --profile daily --set feed_end=12000 --controller heidenhain fanuc`.

## 8. Security and safety

- Binds to `127.0.0.1` by default. `--host 0.0.0.0` needs an explicit flag and logs a
  warning, because there is no authentication.
- The server writes only inside the configured output and config directories. Browser
  input never supplies a path. File names come from sanitised program names.
- No input is ever evaluated as code. Numbers are parsed and range-checked by the core.

## 9. Testing

- **Service tests** (core, no UI): `preview()` on valid and invalid requests, issue paths,
  and CLI-command round-trip. The generated command reproduces byte-identical programs.
- **UI tests**, using NiceGUI's `user` fixture (pytest plugin, simulated browser):
  - The page opens and shows M1's stage table.
  - Changing Finish feed updates the table and code.
  - An invalid value shows an error under the field and disables Generate.
  - Generate writes the expected files to `tmp_path`.
  - Save profile round-trips and keeps the TOML comments.
- **Dependency boundary test:** importing `cnc_warmup` and running a generation never
  loads `nicegui` or `tomlkit` (check `sys.modules`).
- **Manual smoke test:** the checklist in the README, plus a screenshot for the README.

## 10. Acceptance criteria (v1)

- [ ] `uv run --extra ui cnc-warmup ui` opens the page with no other setup.
- [ ] Every field in §5 is present, with its units and a tooltip.
- [ ] The preview updates within about 0.5 s of an edit.
- [ ] Errors appear under the offending field and block Generate, Download and Save.
      Warnings don't block.
- [ ] Generated files are byte-identical to the CLI output for the same settings.
- [ ] Saving a profile keeps `profiles.toml`'s comments, and the CLI reproduces the
      output from it.
- [ ] Nothing binds beyond localhost unless `--host` is given.
- [ ] The core runs without the `ui` extra installed.

## 11. Later (v2 candidates)

- **Ramp chart** (`ui.echart`): RPM and feed per stage over time.
- **3D toolpath preview** (`ui.scene`): the travel envelope as a box, the sweep path,
  and the start and park points. Useful for spotting a bad coordinate convention at a
  glance.
- ~~**Machine editor**, with the same validation and TOML persistence.~~ Built in v1.
- **Diff view:** generated program vs the committed `examples/` file.
- **Send to control** over LSV2 (`pyLSV2`) or FOCAS. Deliberately out of scope for a
  take-home.

## 12. Decisions (formerly open questions)

1. **NiceGUI**, as the optional `ui` extra.
2. The UI is **part of the submission**.
3. Save writes **into `machines.toml` / `profiles.toml`**, with tomlkit keeping every comment.
4. **Machine editing is included** (full configurability).

## 13. Implementation notes

| Module | Role |
|---|---|
| `ui/form.py` | Flat form fields ↔ TOML tables (`MachineForm`, `ProfileForm`), travel ↔ limits conversion, and `field_for(issue)`. No NiceGUI, so it's unit-tested on its own |
| `ui/persist.py` | Reads the files with tomlkit and updates entries **key by key in place**, so inline comments and inline-table style survive. Unchanged values keep their exact text (`1.0` stays `1.0`); a changed value keeps the file's number style and its inline comment's column. Files are read and written as bytes to keep CRLF/LF. New entries follow the file's layout (inline `travel`/`limits`, a `[machines.X.fanuc]` section) |
| `ui/app.py` | The page (`Editor`, one per browser tab). Every change calls `service.preview_config`, the same validation, planning, rendering and **round-trip verification** as the CLI |

| Acceptance criterion (§10) | How it's met / tested (`tests/test_ui*.py`) |
|---|---|
| Opens with one command | `cnc-warmup ui` (CLI test). Checked against a live server: HTTP 200 with the program in the page |
| Every field present, with units and tooltips | All `machines.toml` and `profiles.toml` keys have a widget. Numbers carry units as suffixes |
| Live preview | Recomputed on every change (about 10 ms), with no debounce needed |
| Errors under the field, blocking Generate/Download/Save; warnings don't block | `_show_field_errors` maps each `Issue.path` to its field. NiceGUI's own auto-validation is disabled so it can't clear core errors. Tested with an out-of-range feed, an empty field, a bad machine ID, and a warning |
| Byte-identical to the CLI | Generate and Download are compared with `examples/` in tests |
| Saving keeps comments; the CLI reproduces the output | The persist tests check the exact changed lines. The shown CLI command expresses profile edits as `--set` overrides |
| Localhost only unless `--host` | The default host is `127.0.0.1`. Any other host prints a warning (CLI test) |
| The core runs without the extra | `tests/test_dependencies.py` generates in a subprocess and asserts no UI package was imported |

Some interaction details:
- **Names** (machine ID, profile name) aren't form fields. Each card's title shows the
  current name, and the **✏ rename** button next to the saved-entry select opens a
  dialog. The dialog checks the name as you type against the config's naming rule and
  the existing names, and Enter confirms it.
- A rename is an unsaved change ("renamed from M1, not saved yet"). **Save changes**
  renames the entry in the file: only its `[section.name]` header lines change, so it
  keeps its place and comments.
- **Duplicate…** (⋮ menu) asks for the copy's name, with a free one suggested. The copy
  is marked "new, not saved yet", and Save changes adds it next to the original.
- **Where is machine zero?** replaces a travel-or-limits toggle and a home toggle with
  one question an engineer can answer at the machine. Choosing "somewhere else" converts
  the travel to explicit min/max fields, and choosing an end converts back, keeping the
  lengths. `MachineForm.set_zero` does the conversion and is unit-tested.
- Switching entries is **instant** when there are no unsaved changes. Otherwise a
  confirmation dialog appears first.
- Deleting an entry and discarding changes ask for confirmation. Delete is disabled for
  an unsaved copy and for the last entry in a file.
- At least one output controller always stays selected.
- Actions ignore a click that raced a button being disabled; a test covers this.
