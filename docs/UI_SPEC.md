# Configurator UI

`cnc-warmup ui` opens a small local web page for setting up machines and warm-up
profiles, checking the result as you go, and writing the programs. It runs on top of the
same core as the `cnc-warmup` command line, so it can't produce anything the command line
couldn't.

```sh
uv run cnc-warmup ui                              # opens http://127.0.0.1:8080
uv run cnc-warmup ui --port 9000 --no-browser     # another port, without opening a tab
```

## 1. Purpose and users

- **Main user:** a controls or manufacturing engineer preparing a warm-up for a machine,
  or tuning a profile for part of the fleet.
- **Secondary user:** a reviewer or lead checking a program before it goes to the control.

It is **not** an operator HMI and never talks to a machine.

**Goals:**
1. Change any machine or warm-up setting and see the effect at once: validation
   messages, the stage table, the estimated run time and the generated program text.
2. Write the programs for one or both controllers to disk, or download them.
3. Save the settings back to `config/machines.toml` and `config/profiles.toml`, so the
   command line reproduces the same programs.

**Out of scope:** sending programs to a control (DNC, LSV2, FOCAS), user accounts or
network hosting, mobile layouts, translation, and inch units (programs are metric:
`MM` / `G21`).

## 2. Principles

| Principle | Consequence |
|---|---|
| **Thin adapter.** The UI holds no generation logic | It calls the same application service as the CLI (§7). All validation, planning, rendering and verification stay in the core |
| **The TOML files are the source of truth** | The UI shows the equivalent CLI command, and every program header records its settings |
| **Safety stays visible** | Errors block generation, warnings are shown, and the UI never removes the safety comments, checks or checklist from a program |
| **Local and offline** | Binds to `127.0.0.1` by default. NiceGUI serves its own assets: no CDN, no telemetry |
| **The core stays dependency-free** | The UI packages are an optional extra, and a test checks that the core never imports them |

## 3. Technology

[NiceGUI](https://nicegui.io) 3.x, installed as the optional `ui` extra (`uv sync`
installs it with the development tools).

- It is Python-only and event-driven. Unlike Streamlit, it doesn't re-run a script on
  every interaction, which suits a form with a live preview.
- It has the building blocks needed: inputs with validation, toggles, tables, a drawer
  and a code viewer.
- Its pytest plugin simulates a browser in Python, so the UI tests run at unit-test
  speed without Selenium.

| Alternative | Why not |
|---|---|
| Streamlit | Re-runs the script on every interaction, which is awkward for a stateful form. Heavy dependencies (pandas, pyarrow) |
| Textual (terminal UI) | A good fit for CLI tools, but weaker for previewing programs and less familiar to reviewers |
| Tkinter | No dependencies, but dated, harder to test, and no charts without matplotlib |
| FastAPI + HTML/htmx | The cleanest API separation, but about twice the code (templates, JavaScript) for the same result |

## 4. Layout

The three steps sit side by side as cards of equal height, centered on the page both
ways. The output is in a drawer on the right.

- **Common settings first.** Each card shows only what most warm-ups change. Everything
  else sits in a collapsed **Advanced** section at the bottom of the card, so the machine
  and profile cards' Advanced sections line up. The Generate card keeps **Generate
  files** and **Command-line equivalent** at its bottom the same way.
- **Wide controls where there's room.** **Generate files** spans its card. In the
  profile's Advanced section, the ramp and Z-stroke toggles span the card too; their
  buttons grow in proportion to their labels, so a long label stays on one line.
- **The output is one click away.** The **Output** button at the right of the header
  opens an 860 px drawer. The summary is on top (status, errors and warnings, sweep
  envelope, run time, stage table) and the generated programs are below, one tab per
  controller.
  - While the drawer is closed, a red count on the button shows how many problems block
    generation. Each problem is also shown under its field.
  - The drawer pushes the cards aside rather than covering them, so the preview stays
    live while you edit. The cards shrink from 400 px to 320 px wide to keep all three
    beside the open drawer on a 1920 px screen. On a narrower window they wrap.
- **Unsaved work is hard to miss.** A footer with **Save changes** and **Discard** appears
  only while something is unsaved. It covers both cards, and each card's title shows an
  orange badge when that card has unsaved changes.
- **Rarely used actions are tucked away.** Renaming is the ✏ button next to the
  saved-entry list. The ⋮ ("more") menu beside it has **Save** (just this card),
  **Duplicate…** and **Delete…**.
- **Hadrian's navy (`#002548`)** is the primary colour: header, footer, step numbers,
  buttons and selected toggles. Tooltips use 14 px text and wrap at 320 px.

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

The drawer, opened from the **Output** button:

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

Every limit is enforced by the **core** validator, which reports each problem under the
field it belongs to. "› Advanced" fields are in the card's collapsed Advanced section.

**Step 1, Machine** (`machines.toml`):

| Where | Label | Config key | Control | Unit | Validation |
|---|---|---|---|---|---|
| Machine | Saved machines | machine ID | list, ✏ rename | — | 1-16 letters, digits, `_` or `-`, starting with a letter or digit; unique |
| Machine | Description | `description` | text | — | optional |
| Machine | X / Y / Z travel | `travel` | numbers | mm | > 0, keeping at least 1 mm inside the edge margins |
| Machine | Spindle max | `spindle_max_rpm` | number | rpm | > 0 |
| Machine | Max feed | `max_feed` | number | mm/min | > 0 |
| › Advanced | Where is machine zero? | `home`, or `limits` | radio: **+ end** of each axis (most VMCs), **− end**, or **somewhere else** (enter each axis's min and max) | mm | each axis has min < max |
| › Advanced | This machine can run Fanuc 31i programs | `[fanuc]` table | switch | — | needed for Fanuc output |
| › Advanced | Program number, Cancel codes | `fanuc.program_number`, `fanuc.cancel_codes` | number (`O` prefix), chips | — | 1-8999 (O9000 and up are the builder's macros); only codes the control has options for |

A machine's `controller` key (the CLI's default output) has no field. The UI always
starts with Heidenhain checked under Generate, and saving keeps whatever the file says.

**Step 2, Warm-up profile** (`profiles.toml`):

| Where | Label | Config key | Control | Unit | Validation | Tooltip |
|---|---|---|---|---|---|---|
| Profile | Saved profiles | profile name | list, ✏ rename | — | 1-32 lowercase letters, digits or `_`, starting with a letter; unique | — |
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

**Step 3, Generate:** a checkbox per controller (Heidenhain checked at start, at least
one always checked), a full-width **Generate files** button (its tooltip names the
output folder), and a collapsed **Command-line equivalent** with a copy button.

Fanuc 31i can only be picked for a machine that can run Fanuc programs. While the
machine's "This machine can run Fanuc 31i programs" switch is off, the Fanuc checkbox
is unchecked and disabled. Hovering it, or opening the drawer's Fanuc tab, says where to
turn it on. Turning the switch back on makes Fanuc available again without picking it.

## 6. Behavior

1. **Start-up.** `cnc-warmup ui [--config DIR] [--out DIR] [--host 127.0.0.1]
   [--port 8080] [--no-browser]`. If the config files have errors, the command lists
   them and exits rather than opening a half-working page.
2. **Live preview.** Every edit re-runs the preview at once (it takes about 10 ms), so
   the summary and the programs always match the form. Generation uses what is on
   screen, saved or not.
3. **Validation.** Errors mark their field red with the message, and disable **Generate**
   and **Download**. Warnings (e.g. "stage 1 runs 8:38, over its 4:00 share of the
   duration") are shown in the drawer but don't block anything.
4. **Unsaved changes.** A card's title gets an orange badge ("unsaved changes" or
   "renamed from M1, not saved yet") when it differs from the file, and the footer
   appears, naming what's unsaved. **Discard** (after a confirmation) reloads both cards
   from the files. Switching to another saved entry while there are unsaved changes asks
   first; otherwise it is instant.
5. **Save changes** saves whatever changed (the machine, the profile, or both), each into
   its own file with its comments kept. It is disabled while a changed entry has errors,
   and saves nothing if either is invalid. A machine or profile is checked on its own,
   so a valid machine can be saved even when the profile doesn't fit it yet. Each card's
   ⋮ menu also has **Save**, for just that card.
6. **Names** (machine ID, profile name) are edited in a dialog, not a form field. The
   dialog checks the name as you type against the config's naming rule and the existing
   names, and Enter confirms it. A rename is an unsaved change; on save only the entry's
   `[section.name]` header lines change, so it keeps its place and comments.
7. **Duplicate…** asks for the copy's name, with a free one suggested, then saves the copy
   right away and switches to it. The copy includes any unsaved changes; the original
   keeps its saved settings (the dialog says so). It is disabled while the entry has
   errors.
8. **Delete…** asks for confirmation, and is disabled for the last entry in a file.
9. **Where is machine zero?** is one question an engineer can answer at the machine, in
   place of a travel-or-limits toggle plus a home toggle. Choosing "somewhere else"
   converts the travel to explicit min/max fields; choosing an end converts back,
   keeping the lengths.
10. **Generate files** writes the selected controllers' programs to
    `<output directory>/<controller>/` (default `out/`), with the same file names as the
    CLI (`WARMUP_M1_DAILY.H`, `O8001_M1_DAILY.nc`), lists the written paths, and opens
    the Output drawer.
11. **Download** sends one program to the browser, with the same bytes as Generate
    (including CRLF line endings for `.H` files).
12. **Command-line equivalent** shows, and copies, the command that reproduces the
    preview, e.g. `uv run cnc-warmup generate --machine M1 --profile daily --set
    feed_end=11000 --controller heidenhain fanuc`. Profile edits become `--set`
    overrides; machine edits must be saved first.
13. A click that races a button being disabled (for example, Save just after an edit
    made the form invalid) is ignored safely.

## 7. Architecture

```
            ┌────────────────────┐        ┌───────────────────────────┐
  browser ◄─┤ cnc_warmup.ui      │        │ cnc_warmup (core, no deps)│
            │  app.py     (page) │ calls  │  service.py  ◄── cli.py   │
            │  form.py   (forms) ├───────►│  config / plan / posts    │
            │  persist.py (TOML) │        │  verify                   │
            └────────────────────┘        └───────────────────────────┘
               nicegui, tomlkit              standard library only
```

| Module | Role |
|---|---|
| `ui/app.py` | The page: one `Editor` per browser tab. Every change calls `service.preview_config`, the same validation, planning, rendering and **round-trip verification** as the CLI |
| `ui/form.py` | Flat form fields ↔ TOML tables (`MachineForm`, `ProfileForm`), the travel ↔ limits conversion, and `field_for(issue)`, which maps each problem's TOML path to its field. No NiceGUI, so it is unit-tested on its own |
| `ui/persist.py` | Reads and writes the TOML files with tomlkit, updating entries **key by key in place**. Comments, inline tables and line endings survive; unchanged values keep their exact text (`1.0` stays `1.0`), and a changed value keeps the file's number style and its comment's column. New entries follow the file's layout |

From `cnc_warmup.service`, the UI uses:
- `preview_config(machine_id, machine_table, profile_name, profile_table, controllers)`:
  validates the form's tables, builds the plan, renders and verifies each program, and
  returns them with every `Issue` (error or warning, each with its TOML path).
- `write_programs(preview, out_dir)`: writes a preview's programs, refusing one with
  errors.
- `cli_command(request)`: the shell command that reproduces a request.

## 8. Security and safety

- Binds to `127.0.0.1` by default. Any other `--host` prints a warning, because there is
  no authentication.
- The server writes only inside the configured output and config directories. Browser
  input never supplies a path, and file names come from sanitised program names.
- No input is ever evaluated as code. Numbers are parsed and range-checked by the core.

## 9. Testing

The UI is tested with NiceGUI's simulated browser, against a scratch copy of the config:

| File | Covers |
|---|---|
| `tests/test_ui.py` | The page end to end: the live preview, errors under their fields, the Output drawer and problem count, saving (per card and both), discard, rename, duplicate, delete, Fanuc availability, generated and downloaded files byte-identical to `examples/`, the displayed program following unsaved edits, and clicks that race a disabled button |
| `tests/test_ui_form.py` | Form ↔ TOML conversion for the shipped machines and profiles, the machine-zero conversions, and mapping problems to fields |
| `tests/test_ui_persist.py` | Saving keeps comments, layout, number style, comment alignment and line endings; renames change only the header lines; new entries match the file's layout |
| `tests/test_dependencies.py` | Generating programs never imports `nicegui` or `tomlkit` |

The layout was also checked in Chrome at 1920 × 1080, with the drawer open and closed.

## 10. Possible extensions

- **Ramp chart:** RPM and feed per stage over time.
- **3D toolpath preview:** the travel envelope, the sweep path, and the start and park
  points, to spot a wrong coordinate convention at a glance.
- **Diff view:** a generated program against the committed `examples/` file.
- **Send to control** over LSV2 or FOCAS.
