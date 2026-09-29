# UI specification: warm-up configurator

**Status:** draft, for review. Nothing in this document has been built yet.

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
- Editing machine definitions (read-only in v1; see §11).
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

One page, two columns, sticky action bar.

```
┌──────────────────────────────────────────────────────────────────────────────────────┐
│ CNC Warm-Up Generator   Machine [M1 ▾]   Profile [daily ▾]   Output [☑ Heidenhain ☑ Fanuc] │
├────────────────────────────────┬─────────────────────────────────────────────────────┤
│ MACHINE  (read-only)           │  [ Summary ] [ Heidenhain .H ] [ Fanuc .nc ]        │
│  X −762 … 0   Y −508 … 0       │ ┌─────────────────────────────────────────────────┐ │
│  Z −500 … 0   (machine coords) │ │ Est. 15.4 min · 5 stages · Heidenhain 88 blocks │ │
│  Spindle max 12 000 rpm        │ │                                                 │ │
│  Feed max 20 000 mm/min        │ │ Stage   RPM    Feed   Passes  Dwell   Time      │ │
│  Edit config/machines.toml ↗   │ │   1     1000   2000     1      0 s    3:08      │ │
│                                │ │   2     3250   4500     1     37 s    3:00      │ │
│ AXIS WARM-UP                   │ │   …                                             │ │
│  Start feed   [  2000 ] mm/min │ │                                                 │ │
│  Finish feed  [ 12000 ] mm/min │ │ ⚠ Stage 1 runs 3:08, over its 3:00 budget       │ │
│                                │ │   (raise the start feed or the duration)        │ │
│ SPINDLE WARM-UP                │ └─────────────────────────────────────────────────┘ │
│  Start RPM    [  1000 ]        │                                                     │
│  Finish RPM   [ 10000 ]        │  Code tabs: read-only, monospace, line numbers,     │
│                                │  copy button; shows the exact bytes that will be    │
│ TIMING                         │  written                                            │
│  Duration  [ 15 ] min          │                                                     │
│  Stages    [  5 ]              │                                                     │
│  Ramp      (•) linear  ( ) geometric                                                 │
│                                │                                                     │
│ SWEEP PATTERN                  │                                                     │
│  ☑ Perimeter  ☑ XY diagonals  ☑ Z stroke   Z stroke at (•) centre ( ) start corner   │
│  Edge margin  [ 1.0 ] mm       │                                                     │
│                                │                                                     │
│ OPTIONS                        │                                                     │
│  Coolant  [ off ▾ ]            │                                                     │
│  ☑ Operator confirmation stop  │                                                     │
│  ☑ Runtime soft-limit guard    │                                                     │
│  ☐ Final rapid pass            │                                                     │
├────────────────────────────────┴─────────────────────────────────────────────────────┤
│ ● Unsaved changes   [Reset]   [Save profile ▾]   [Generate files]   [Download ▾]   [⧉ CLI command] │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

## 5. Fields

Defaults come from the selected profile. **Ranges marked † are proposals to confirm.**
Every limit that depends on the machine is enforced by the **core** validator. The UI
only mirrors those limits in the widgets' min/max so bad values are hard to type.

| Section | Label | Config key | Control | Unit | Validation (core) | Help text (tooltip) |
|---|---|---|---|---|---|---|
| Header | Machine | `--machine` | select | — | must exist in `machines.toml` | — |
| Header | Profile | `--profile` | select | — | must exist in `profiles.toml` | "Starting values; edits are unsaved until you save." |
| Header | Output | `--controller` | 2 toggles | — | at least one on | "Heidenhain TNC 640 Klartext (.H) and/or Fanuc 31i (.nc)" |
| Axis | Start feed | `feed_start` | number, step 10 | mm/min | `0 < feed_start ≤ feed_end` | "Feed of the first, gentlest sweep." |
| Axis | Finish feed | `feed_end` | number, step 10 | mm/min | `≤ machine.max_feed` | — |
| Spindle | Start RPM | `rpm_start` | number, step 10 | rpm | `0 < rpm_start ≤ rpm_end` | "Never start a cold spindle at high speed." |
| Spindle | Finish RPM | `rpm_end` | number, step 10 | rpm | `≤ machine.spindle_max_rpm` | — |
| Timing | Duration | `duration_min` | number | min | 1–240 † | "Target; each stage holds its RPM for at least duration ÷ stages." |
| Timing | Stages | `stages` | number | — | 2–20 † | — |
| Timing | Ramp | `ramp` | radio | — | `linear` \| `geometric` | "Geometric spends more time at low speed, which is gentler on bearings." |
| Pattern | Perimeter / XY diagonals / Z stroke | `pattern` | checkboxes | — | together they must reach **all six** axis extremes | — |
| Pattern | Z stroke at | `z_stroke_at` | radio | — | `center` \| `start` | — |
| Pattern | Edge margin | `edge_margin_mm` | number, step 0.1 | mm | `0 ≤ m`, and `2m <` smallest travel; 0–25 † | "Stops this far inside each limit so servo overshoot can't trip an overtravel alarm." |
| Options | Coolant | `coolant` | select | — | `off` \| `flood` | "Flood with an empty spindle sprays the enclosure." |
| Options | Operator confirmation stop | `operator_confirm` | switch | — | — | "STOP / #3006 checklist prompt before the spindle starts." |
| Options | Runtime soft-limit guard | `runtime_guards` | switch | — | — | "Program reads the control's soft limits and alarms if they don't match this machine." |
| Options | Final rapid pass | `final_rapid_pass` | switch | — | — | — |

The machine panel shows read-only data from `machines.toml`: limits, spindle max, max
feed, Fanuc program number, and Fanuc cancel codes. It links to the file rather than
editing it.

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

**The application service** is new in the core, and **the CLI uses it too**:

```python
@dataclass(frozen=True)
class GenerationRequest:
    machine_id: str
    profile_name: str
    overrides: Mapping[str, object]  # e.g. {"feed_end": 12000}
    controllers: tuple[Controller, ...]


@dataclass(frozen=True)
class Issue:
    path: str  # dotted, e.g. "profile.feed_end"
    message: str
    severity: Literal["error", "warning"]


def load_catalog(config_dir: Path) -> Catalog:
    """Machines, profiles, and any load issues."""


def preview(catalog: Catalog, req: GenerationRequest) -> Preview:
    """Stage table, estimates, {controller: program text}, and issues."""


def write_programs(preview: Preview, out_dir: Path) -> list[Path]: ...


def cli_command(req: GenerationRequest) -> str: ...
```

- Each `Issue.path` maps to a form field, so the message appears **under that field**.
  Issues with no matching field (plan warnings) appear in the Summary tab.
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
4. **Unsaved changes:** a dot plus "Unsaved changes" shows when the form differs from the
   selected profile. **Reset** reverts to the profile. Switching machine or profile
   while there are unsaved changes asks for confirmation.
5. **Generate files:**
   - Writes the selected controllers' programs to the output directory (default `out/`).
   - Asks before overwriting, then lists the written paths.
   - Uses the same file names as the CLI (`WARMUP_M1.H`, `O8001.nc`).
6. **Download:** a browser download of one program. It contains the same bytes as
   Generate, including CRLF for `.H` files.
7. **Save profile:**
   - "Save to current profile" or "Save as new profile…" (name matches `^[a-z][a-z0-9_]{0,31}$`).
   - Only allowed when there are no errors. Asks before overwriting.
   - Writes to `config/profiles.toml` and keeps its comments.
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
- **Machine editor**, with the same validation and TOML persistence.
- **Diff view:** generated program vs the committed `examples/` file.
- **Send to control** over LSV2 (`pyLSV2`) or FOCAS. Deliberately out of scope for a
  take-home.

## 12. Open questions

1. Is NiceGUI acceptable, or would you prefer a zero-dependency option (Tkinter) or a
   terminal UI (Textual)?
2. Should the UI be part of the submission or a stretch goal? Adding it to the roadmap
   costs about 4–6 h after the core is done.
3. Should Save profile write into `profiles.toml` (proposed), or to a separate
   `profiles.local.toml` that overrides it?
4. Should a read-only machine panel be enough for v1, or is machine editing needed?
